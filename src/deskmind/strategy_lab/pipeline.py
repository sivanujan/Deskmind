"""Strategy Lab execution pipeline running checks, TRAIN scan, freeze, and VALIDATION."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.phase2_runner import build_volume_map, load_universe_symbols
from deskmind.strategy_lab.base import load_strategy, run_strategy_backtest
from deskmind.strategy_lab.checks import run_strategy_checks
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.rules import load_rules
from deskmind.config import get_settings
from deskmind.strategy_lab.workers import lower_worker_priority, scan_symbol_worker

TF_ORDER: list[str] = ["4h", "2h", "1h", "30m", "15m", "5m"]


@dataclass
class JobFunnel:
    total_universe: int = 0
    eligible_coins: int = 0
    selected_coins: int = 0
    profitable_2025: int = 0


@dataclass
class ConcentrationWarning:
    top_4_profit_share_pct: float = 0.0
    top_4_coins: list[str] = field(default_factory=list)
    is_concentrated: bool = False


@dataclass
class StrategyLabJob:
    job_id: str
    strategy_id: str
    strategy_name: str
    strategy_version: str
    strategy_path: str
    trial_k: int
    timeframe: str = "4h"
    status: str = "QUEUED"  # QUEUED, RUNNING_CHECKS, ESTIMATING, TRAIN_SCAN, FREEZING, VALIDATION_SCAN, COMPLETED, REJECTED, FAILED, CANCELLED
    progress_pct: float = 0.0
    current_step: str = "Queued"
    current_coin: str = ""
    coins_done: int = 0  # coins finished in the current scan phase
    coins_total: int = 0  # coins in the current scan phase
    scan_phase: str = ""  # "TRAIN" or "VALIDATION" while a coin scan runs
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))
    last_update_ts: float = field(default_factory=time.time)
    reject_reason: str | None = None
    time_per_coin_s: float | None = None
    estimated_seconds: float | None = None
    elapsed_seconds: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))
    completed_at: str | None = None
    verdict: str = "PENDING"  # PASS, FAIL, REJECTED, CANCELLED, PENDING
    rules_verdict: dict[str, Any] = field(default_factory=dict)
    funnel: JobFunnel = field(default_factory=JobFunnel)
    selected_symbols: list[str] = field(default_factory=list)
    candidate_coins: list[dict[str, Any]] = field(default_factory=list)
    not_tradeable: list[dict[str, Any]] = field(default_factory=list)
    per_coin_results: list[dict[str, Any]] = field(default_factory=list)
    concentration_warning: ConcentrationWarning = field(default_factory=ConcentrationWarning)
    comparison_row: dict[str, Any] = field(default_factory=dict)
    test_2026_status: str = "LOCKED"  # LOCKED, READY, RUNNING, COMPLETED
    eval_mode: str = "standard"  # "standard" or "walk_forward"
    walkforward_rows: list[dict[str, Any]] = field(default_factory=list)
    walkforward_combined: dict[str, Any] = field(default_factory=dict)


_JOBS: dict[str, StrategyLabJob] = {}
_JOB_QUEUE: list[tuple[str, str]] = []  # [(job_id, rules_version)]
_QUEUE_LOCK = threading.Lock()
_QUEUE_THREAD: threading.Thread | None = None
_CURRENT_JOB_ID: str | None = None
_STOP_EVENT = threading.Event()


class JobCancelled(Exception):
    """Raised inside a running pipeline when its job was cancelled or cleared."""


def append_job_log(job_id: str, message: str) -> None:
    """Append a timestamped log line to the job's log file."""
    if job_id not in _JOBS:  # cleared: do not recreate its directory
        return
    try:
        job_dir = Path("data/strategy_lab/jobs") / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_file = job_dir / "job.log"
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] {message}\n")
    except Exception:
        pass


def get_job_log_lines(job_id: str, max_lines: int = 50) -> list[str]:
    """Retrieve the last N lines from the job's log file."""
    log_file = Path("data/strategy_lab/jobs") / job_id / "job.log"
    if not log_file.exists():
        job = get_job(job_id)
        if job:
            return [f"[{job.created_at}] Status: {job.status} - {job.current_step}"]
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


def job_to_dict(job: StrategyLabJob) -> dict[str, Any]:
    """Serialize a job with dynamic stalled detection and elapsed age."""
    d = asdict(job)
    now_ts = time.time()
    last_ts = getattr(job, "last_update_ts", 0.0)
    if last_ts > 0.0:
        age = round(now_ts - last_ts, 1)
    else:
        try:
            dt = datetime.strptime(job.created_at.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            age = round(datetime.now(UTC).timestamp() - dt.timestamp(), 1)
        except Exception:
            age = 0.0

    d["last_update_age_s"] = max(0.0, age)
    # Stalled if in active processing state with no progress for > 120s,
    # or if queued while the background queue runner is not running.
    status_upper = (job.status or "").upper()
    is_active = status_upper in (
        "RUNNING",
        "RUNNING_CHECKS",
        "ESTIMATING",
        "TRAIN_SCAN",
        "FREEZING",
        "VALIDATION_SCAN",
    )
    is_queued_dead = status_upper == "QUEUED" and not is_worker_running()
    d["is_stalled"] = bool((is_active or is_queued_dead) and age > 120.0)
    return d


# Per-coin detail (hundreds of rows per finished job). Left out of the polled status list: with
# 30 jobs it made /status ~2 MB every 2 s. Full detail: GET /jobs/{id} and the CSV/JSON downloads.
_HEAVY_JOB_FIELDS = ("per_coin_results", "candidate_coins", "not_tradeable", "selected_symbols")


def job_summary_dict(job: StrategyLabJob) -> dict[str, Any]:
    """job_to_dict without the per-coin lists (adds `selected_count`)."""
    d = job_to_dict(job)
    d["selected_count"] = len(d.get("selected_symbols") or [])
    for key in _HEAVY_JOB_FIELDS:
        d.pop(key, None)
    return d


def get_job(job_id: str) -> StrategyLabJob | None:
    if job_id in _JOBS:
        return _JOBS[job_id]
    disk_path = Path("data/strategy_lab/jobs") / job_id / "job.json"
    if disk_path.exists():
        try:
            data = json.loads(disk_path.read_text(encoding="utf-8"))
            funnel_data = data.pop("funnel", {})
            cw_data = data.pop("concentration_warning", {})
            created_at = data.get("created_at", "")
            try:
                dt = datetime.strptime(created_at.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                default_ts = dt.timestamp()
            except Exception:
                default_ts = time.time()

            job = StrategyLabJob(
                job_id=data.get("job_id", job_id),
                strategy_id=data.get("strategy_id", ""),
                strategy_name=data.get("strategy_name", ""),
                strategy_version=data.get("strategy_version", "1.0.0"),
                strategy_path=data.get("strategy_path", ""),
                trial_k=int(data.get("trial_k", 0)),
                timeframe=data.get("timeframe", "4h"),
                status=data.get("status", "QUEUED"),
                progress_pct=float(data.get("progress_pct", 0.0)),
                current_step=data.get("current_step", "Queued"),
                current_coin=data.get("current_coin", ""),
                coins_done=int(data.get("coins_done", 0)),
                coins_total=int(data.get("coins_total", 0)),
                scan_phase=data.get("scan_phase", ""),
                updated_at=data.get("updated_at") or created_at,
                last_update_ts=float(data.get("last_update_ts", default_ts)),
                reject_reason=data.get("reject_reason"),
                time_per_coin_s=data.get("time_per_coin_s"),
                estimated_seconds=data.get("estimated_seconds"),
                elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
                created_at=created_at,
                completed_at=data.get("completed_at"),
                verdict=data.get("verdict", "PENDING"),
                rules_verdict=data.get("rules_verdict", {}),
                funnel=JobFunnel(**funnel_data),
                selected_symbols=data.get("selected_symbols", []),
                candidate_coins=data.get("candidate_coins", []),
                not_tradeable=data.get("not_tradeable", []),
                per_coin_results=data.get("per_coin_results", []),
                concentration_warning=ConcentrationWarning(**cw_data),
                comparison_row=data.get("comparison_row", {}),
                test_2026_status=data.get("test_2026_status", "LOCKED"),
                eval_mode=data.get("eval_mode", "standard"),
                walkforward_rows=data.get("walkforward_rows", []),
                walkforward_combined=data.get("walkforward_combined", {}),
            )
            _JOBS[job_id] = job
            return job
        except Exception:
            return None
    return None


def list_jobs() -> list[StrategyLabJob]:
    jobs_dir = Path("data/strategy_lab/jobs")
    if jobs_dir.exists():
        for sub in jobs_dir.iterdir():
            if sub.is_dir() and (sub / "job.json").exists():
                get_job(sub.name)
    return sorted(_JOBS.values(), key=lambda j: j.created_at, reverse=True)


def save_job(job: StrategyLabJob) -> None:
    if job.job_id not in _JOBS:  # cleared while running: do not write it back
        return
    job_dir = Path("data/strategy_lab/jobs") / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(job)
    (job_dir / "job.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def scan_worker_count() -> int:
    """Worker processes for coin scans: STRATEGY_LAB_WORKERS, or auto = half the CPU threads,
    at most 6. Using every core starved the desktop and filled RAM (each worker holds a coin)."""
    configured = get_settings().strategy_lab_workers
    if configured > 0:
        return configured
    return max(1, min((os.cpu_count() or 2) // 2, 6))


# A single coin never takes this long; no result by then means a worker hung or died.
COIN_RESULT_TIMEOUT_S = 15 * 60


def _scan_pool(num_workers: int) -> ProcessPoolExecutor:
    """Below-normal priority workers. No max_tasks_per_child: on Python 3.12 replacing retired
    workers can silently fail, which hung every scan at workers x tasks coins (6 x 25 = 150)."""
    return ProcessPoolExecutor(max_workers=num_workers, initializer=lower_worker_priority)


def _scan_coins(
    num_workers: int,
    symbols: list[str],
    worker_args: tuple[Any, ...],
    on_result: Any,
) -> None:
    """Scan `symbols` in parallel and call on_result(idx, result) in symbol order.

    A coin with no result after COIN_RESULT_TIMEOUT_S fails the job with a clear reason instead
    of leaving it stalled forever. On any error (incl. JobCancelled) the workers are stopped.
    """
    pool = _scan_pool(num_workers)
    finished = False
    try:
        futures = [pool.submit(scan_symbol_worker, sym, *worker_args) for sym in symbols]
        for idx, fut in enumerate(futures, 1):
            try:
                res = fut.result(timeout=COIN_RESULT_TIMEOUT_S)
            except FuturesTimeout:
                raise RuntimeError(
                    f"No result for {symbols[idx - 1]} after {COIN_RESULT_TIMEOUT_S // 60} min: "
                    "a scan worker hung or died. Retry the job."
                ) from None
            on_result(idx, res)
        finished = True
    finally:
        if finished:
            pool.shutdown(wait=True)
        else:
            procs = list((getattr(pool, "_processes", None) or {}).values())
            pool.shutdown(wait=False, cancel_futures=True)
            for proc in procs:
                proc.terminate()  # a hung worker would otherwise keep its CPU/RAM

def run_pipeline_sync(
    job_id: str,
    rules_version: str | Any = "v1",
    data_dir: str | Path | None = None,
    meta_path: str | Path | None = None,
    benchmark_symbol: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    if getattr(job, "eval_mode", "standard") == "walk_forward":
        from deskmind.strategy_lab.walkforward import run_walkforward_pipeline_sync

        run_walkforward_pipeline_sync(
            job,
            rules_version=rules_version if rules_version != "v1" else "walkforward_v1",
            num_workers=num_workers,
            data_dir=data_dir,
            meta_path=meta_path,
        )
        return

    rules = rules_version if hasattr(rules_version, "splits") else load_rules(rules_version)
    t_start_total = time.time()

    def update(
        status: str | None = None,
        progress: float | None = None,
        step: str | None = None,
        current_coin: str | None = None,
        coins: tuple[int, int] | None = None,
        log: bool = True,
    ) -> None:
        if job.status == "CANCELLED" or job.job_id not in _JOBS:
            raise JobCancelled(job.job_id)
        now_ts = time.time()
        job.last_update_ts = now_ts
        job.updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress_pct = round(progress, 1)
        if step is not None:
            job.current_step = step
        if current_coin is not None:
            job.current_coin = current_coin
        if coins is not None:
            job.coins_done, job.coins_total = coins
        job.elapsed_seconds = round(now_ts - t_start_total, 1)
        save_job(job)
        if log:
            append_job_log(job.job_id, f"[{job.status} {job.progress_pct}%] {job.current_step}")

    try:
        append_job_log(job.job_id, f"Pipeline execution started for {job.strategy_name} on {job.timeframe} (Job {job_id})")

        vol_map = build_volume_map(rules.liquidity.volume_data_path)
        symbols = load_universe_symbols(rules.universe.path)
        symbols = [s for s in symbols if s not in rules.universe.excluded_symbols]
        job.funnel.total_universe = len(symbols)

        bench_sym = benchmark_symbol or next((s for s in symbols if "ETH" in s), symbols[0])

        # ── Step a) Run checks.py (look-ahead, accounting, determinism) ──────
        update("RUNNING_CHECKS", 5.0, "Running integrity checks (look-ahead, accounting, determinism)")
        strat_path = Path(job.strategy_path)
        if data_dir is not None:
            check_res = run_strategy_checks(
                strat_path,
                symbol=bench_sym,
                data_dir=data_dir,
                meta_path=meta_path or "data/meta/symbol_filters_universe.json",
            )
        else:
            check_res = run_strategy_checks(strat_path)

        if check_res.get("status") == "REJECTED":
            reason = check_res.get("reason", "Failed strategy checks")
            job.status = "REJECTED"
            job.verdict = "REJECTED"
            job.reject_reason = reason
            job.current_step = f"REJECTED: {reason}"
            job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            save_job(job)
            append_job_log(job.job_id, f"REJECTED: {reason}")
            return

        # ── Step b) Time 1 coin & estimate scan time ─────────────────────────
        update("ESTIMATING", 10.0, "Timing benchmark coin for duration estimate")
        strat = load_strategy(strat_path)
        tf = job.timeframe or getattr(strat, "timeframe", "4h")
        strat.timeframe = tf

        # Benchmark on benchmark coin
        t0 = time.time()
        try:
            bench_df = (
                load_ohlcv(bench_sym, tf, data_dir=data_dir)
                if data_dir
                else load_ohlcv(bench_sym, tf)
            )
            bench_df["date_str"] = bench_df["open_time"].dt.strftime("%Y-%m-%d")
            bench_df["vol_30d_med"] = [vol_map.get((bench_sym, d), float("nan")) for d in bench_df["date_str"]]
            bench_tick = (
                get_tick_size(bench_sym, meta_path=meta_path)
                if meta_path
                else get_tick_size(bench_sym)
            )
            _ = run_strategy_backtest(
                strat,
                bench_df,
                tick=bench_tick,
                start=rules.splits.train.start,
                end=rules.splits.train.end,
                min_quote_volume_30d=rules.liquidity.min_30d_median_quote_volume_usd,
                tiered_slippage=rules.slippage.tiered,
            )
            t_coin = max(time.time() - t0, 0.05)
        except Exception:
            t_coin = 0.2

        num_workers = scan_worker_count()
        job.time_per_coin_s = round(t_coin, 3)
        job.estimated_seconds = round((t_coin * len(symbols)) / num_workers, 1)

        # ── Step c) TRAIN scan on all coins with process parallelism ─────────
        eta_min = job.estimated_seconds / 60.0
        job.scan_phase = "TRAIN"
        update(
            "TRAIN_SCAN",
            15.0,
            f"Scanning {len(symbols)} coins on {tf} (ETA ~{eta_min:.1f}m across {num_workers} cores)",
            coins=(0, len(symbols)),
        )

        train_results: list[dict[str, Any]] = []
        all_train_pos_records: list[dict[str, float]] = []

        def on_train(idx: int, res_sym: dict[str, Any]) -> None:
            train_results.append(res_sym)
            if res_sym["is_eligible"]:
                all_train_pos_records.extend(res_sym["positions_records"])
            update(
                None,
                15.0 + (idx / len(symbols)) * 45.0,
                f"TRAIN scan: processed {idx}/{len(symbols)} coins ({res_sym['symbol']})",
                current_coin=f"{idx}/{len(symbols)} {res_sym['symbol']}",
                coins=(idx, len(symbols)),
                log=idx % 5 == 0 or idx == len(symbols),
            )

        _scan_coins(
            num_workers,
            symbols,
            (
                tf,
                str(strat_path),
                rules.splits.train.start,
                rules.splits.train.end,
                rules.liquidity.min_30d_median_quote_volume_usd,
                rules.slippage.tiered,
                rules.liquidity.volume_data_path,
                str(data_dir) if data_dir else None,
                str(meta_path) if meta_path else None,
            ),
            on_train,
        )
        eligible_count = sum(1 for r in train_results if r["is_eligible"])
        job.funnel.eligible_coins = eligible_count

        # Compute TRAIN pooled metrics
        if all_train_pos_records:
            train_r_vals = [p["r_multiple"] for p in all_train_pos_records if not np.isnan(p.get("r_multiple", np.nan))]
            train_pooled_mean_r = float(np.mean(train_r_vals)) if train_r_vals else 0.0
            train_cost_vals = [p["cost_in_r"] for p in all_train_pos_records if not np.isnan(p.get("cost_in_r", np.nan))]
            train_avg_cost_in_r = float(np.mean(train_cost_vals)) if train_cost_vals else 0.0
        else:
            train_pooled_mean_r = 0.0
            train_avg_cost_in_r = 0.0

        train_gp = sum(r.get("gross_profit", 0.0) for r in train_results if r["is_eligible"])
        train_gl = sum(r.get("gross_loss", 0.0) for r in train_results if r["is_eligible"])
        train_pooled_pf = (train_gp / train_gl) if train_gl > 0 else (999.0 if train_gp > 0 else 0.0)

        # ── Step d) Freeze selected list BEFORE validation ───────────────────
        update("FREEZING", 65.0, "Freezing selected coins list by TRAIN criteria")

        sel_rules = rules.train_selection
        selected_symbols: list[str] = []
        not_tradeable: list[dict[str, Any]] = []

        for r in train_results:
            if not r["is_eligible"]:
                continue
            sym = r["symbol"]
            # Frozen TRAIN selection rules (rules_v1.yaml: train_selection).
            passes_r = r["mean_r"] >= sel_rules.min_mean_r
            passes_pf = r["profit_factor"] >= sel_rules.min_profit_factor
            passes_positions = r["positions"] >= sel_rules.min_positions
            passes_dd = r["max_drawdown_pct"] <= sel_rules.max_drawdown_pct

            if passes_r and passes_pf and passes_positions and passes_dd:
                selected_symbols.append(sym)
            else:
                reasons = []
                if not passes_r:
                    reasons.append(f"mean_r {r['mean_r']:.3f} < {sel_rules.min_mean_r}")
                if not passes_pf:
                    reasons.append(f"PF {r['profit_factor']:.2f} < {sel_rules.min_profit_factor}")
                if not passes_positions:
                    reasons.append(f"positions {r['positions']} < {sel_rules.min_positions}")
                if not passes_dd:
                    reasons.append(
                        f"max DD {r['max_drawdown_pct']:.1f}% > {sel_rules.max_drawdown_pct}%"
                    )
                not_tradeable.append({"symbol": sym, "reason": "; ".join(reasons)})

        job.funnel.selected_coins = len(selected_symbols)
        job.selected_symbols = selected_symbols
        job.not_tradeable = not_tradeable

        # ── Step e) VALIDATION 2025 scan on frozen selected list ────────────
        job.scan_phase = "VALIDATION"
        update(
            "VALIDATION_SCAN",
            70.0,
            f"Scanning {len(selected_symbols)} selected coins on 2025 OOS split",
            coins=(0, len(selected_symbols)),
        )

        val_selected_results: dict[str, Any] = {}
        all_val_pos_records: list[dict[str, float]] = []

        def on_val(idx: int, res_sym: dict[str, Any]) -> None:
            sym = res_sym["symbol"]
            val_selected_results[sym] = res_sym
            all_val_pos_records.extend(res_sym["positions_records"])
            update(
                None,
                70.0 + (idx / max(len(selected_symbols), 1)) * 25.0,
                f"2025 scan: processed {idx}/{len(selected_symbols)} coins ({sym})",
                current_coin=f"{idx}/{len(selected_symbols)} {sym}",
                coins=(idx, len(selected_symbols)),
                log=idx % 5 == 0 or idx == len(selected_symbols),
            )

        _scan_coins(
            num_workers,
            selected_symbols,
            (
                tf,
                str(strat_path),
                rules.splits.validation.start,
                rules.splits.validation.end,
                rules.liquidity.min_30d_median_quote_volume_usd,
                rules.slippage.tiered,
                rules.liquidity.volume_data_path,
                str(data_dir) if data_dir else None,
                str(meta_path) if meta_path else None,
            ),
            on_val,
        )
        profitable_coins = [sym for sym, r in val_selected_results.items() if r.get("net_profit", 0.0) > 0]
        job.funnel.profitable_2025 = len(profitable_coins)

        # Compute VALIDATION pooled metrics
        if all_val_pos_records:
            val_r_vals = [p["r_multiple"] for p in all_val_pos_records if not np.isnan(p.get("r_multiple", np.nan))]
            val_pooled_mean_r = float(np.mean(val_r_vals)) if val_r_vals else 0.0
        else:
            val_pooled_mean_r = 0.0

        val_gp = sum(r.get("gross_profit", 0.0) for r in val_selected_results.values())
        val_gl = sum(r.get("gross_loss", 0.0) for r in val_selected_results.values())
        val_pooled_pf = (val_gp / val_gl) if val_gl > 0 else (999.0 if val_gp > 0 else 0.0)

        # ── Step f) Evaluation against rules_v1.yaml ────────────────────────
        val_rules = rules.validation_pass
        pct_profitable = (
            round((len(profitable_coins) / len(selected_symbols)) * 100.0, 1)
            if selected_symbols
            else 0.0
        )

        rule_results: dict[str, Any] = {
            "selected_coins": {
                "value": len(selected_symbols),
                "threshold": 1,
                "passed": len(selected_symbols) >= 1,
                "rule": "At least 1 coin picked on TRAIN (nothing to validate otherwise)",
            },
            "min_val_pooled_mean_r": {
                "value": round(val_pooled_mean_r, 4),
                "threshold": val_rules.min_pooled_mean_r,
                "passed": val_pooled_mean_r >= val_rules.min_pooled_mean_r,
                "rule": f"2025 pooled mean R >= {val_rules.min_pooled_mean_r}",
            },
            "min_val_pooled_pf": {
                "value": round(val_pooled_pf, 2),
                "threshold": val_rules.min_pooled_profit_factor,
                "passed": val_pooled_pf >= val_rules.min_pooled_profit_factor,
                "rule": f"2025 pooled profit factor >= {val_rules.min_pooled_profit_factor}",
            },
            "pct_selected_profitable": {
                "value": pct_profitable,
                "threshold": val_rules.min_profitable_cohort_pct,
                "passed": pct_profitable >= val_rules.min_profitable_cohort_pct,
                "rule": f"% picked coins profitable in 2025 >= {val_rules.min_profitable_cohort_pct}%",
            },
        }

        overall_pass = all(r["passed"] for r in rule_results.values())
        job.rules_verdict = rule_results
        job.verdict = "PASS" if overall_pass else "FAIL"

        # Multi-timeframe comparison row
        job.comparison_row = {
            "tf": tf,
            "eligible_coins": eligible_count,
            "train_positions": len(all_train_pos_records),
            "train_pooled_mean_r": round(train_pooled_mean_r, 4),
            "train_pooled_pf": round(train_pooled_pf, 2),
            "avg_cost_in_r": round(train_avg_cost_in_r, 4),
            "selected": len(selected_symbols),
            "val_positions": len(all_val_pos_records),
            "val_pooled_mean_r": round(val_pooled_mean_r, 4),
            "val_pooled_pf": round(val_pooled_pf, 2),
            "pct_selected_profitable_2025": pct_profitable,
            "verdict": job.verdict,
        }

        # Build Candidate Coins card
        candidate_coins = []
        for sym in selected_symbols:
            t = next((r for r in train_results if r["symbol"] == sym), {})
            v = val_selected_results.get(sym, {})
            label = "TRAIN+VAL" if sym in profitable_coins else "TRAIN_ONLY"
            candidate_coins.append({
                "symbol": sym,
                "label": label,
                "train_positions": t.get("positions", 0),
                "train_pf": t.get("profit_factor", 0.0),
                "train_mean_r": t.get("mean_r", 0.0),
                "val_positions": v.get("positions", 0),
                "val_pf": v.get("profit_factor", 0.0),
                "val_mean_r": v.get("mean_r", 0.0),
                "val_net_profit": v.get("net_profit", 0.0),
            })
        candidate_coins.sort(key=lambda x: x["val_net_profit"], reverse=True)
        job.candidate_coins = candidate_coins

        # Concentration warning: share of 2025 profit from top 4 coins
        pos_profits = [c["val_net_profit"] for c in candidate_coins if c["val_net_profit"] > 0]
        if pos_profits:
            total_pos_profit = sum(pos_profits)
            top_4_profit = sum(pos_profits[:4])
            top_4_share = round(100.0 * top_4_profit / total_pos_profit, 1)
            top_4_names = [c["symbol"] for c in candidate_coins[:4]]
            job.concentration_warning = ConcentrationWarning(
                top_4_profit_share_pct=top_4_share,
                top_4_coins=top_4_names,
                is_concentrated=top_4_share >= 70.0,
            )
        else:
            job.concentration_warning = ConcentrationWarning()

        # Build per-coin results table combining train and val
        per_coin_results = []
        for r in train_results:
            sym = r["symbol"]
            v = val_selected_results.get(sym, {})
            per_coin_results.append({
                "symbol": sym,
                "is_eligible": r["is_eligible"],
                "is_selected": sym in selected_symbols,
                "is_profitable_2025": sym in profitable_coins,
                "train_positions": r["positions"],
                "train_pf": r["profit_factor"],
                "train_mean_r": r["mean_r"],
                "train_max_dd_pct": r["max_drawdown_pct"],
                "train_net_profit": r["net_profit"],
                "val_positions": v.get("positions", "-"),
                "val_pf": v.get("profit_factor", "-"),
                "val_mean_r": v.get("mean_r", "-"),
                "val_net_profit": v.get("net_profit", "-"),
                "val_max_dd_pct": v.get("max_drawdown_pct", "-"),
            })
        job.per_coin_results = per_coin_results
        job.test_2026_status = "READY" if overall_pass else "LOCKED"

        job.scan_phase = ""
        update("COMPLETED", 100.0, f"Completed with verdict: {job.verdict}")
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)
        append_job_log(job.job_id, f"Execution completed with verdict: {job.verdict}")

    except JobCancelled:
        job.scan_phase = ""
        save_job(job)
        append_job_log(job.job_id, "Stopped: job was cancelled.")
    except Exception as e:
        job.scan_phase = ""
        job.status = "FAILED"
        job.reject_reason = str(e)
        job.current_step = f"Failed with exception: {e}"
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)
        append_job_log(job.job_id, f"ERROR: Execution failed: {e}")


def _queue_worker_loop() -> None:
    global _CURRENT_JOB_ID
    while not _STOP_EVENT.is_set():
        job_item = None
        with _QUEUE_LOCK:
            if _JOB_QUEUE:
                job_item = _JOB_QUEUE.pop(0)
            else:
                _CURRENT_JOB_ID = None

        if job_item is None:
            # Queue is empty: wait for new jobs or stop event without busy-waiting
            if _STOP_EVENT.wait(timeout=0.2):
                break
            continue

        job_id, rules_ver = job_item
        _CURRENT_JOB_ID = job_id
        try:
            run_pipeline_sync(job_id, rules_ver)
        except Exception as e:
            append_job_log(job_id, f"Unhandled exception in queue worker: {e}")
        finally:
            _CURRENT_JOB_ID = None


def is_worker_running() -> bool:
    global _QUEUE_THREAD
    return _QUEUE_THREAD is not None and _QUEUE_THREAD.is_alive()


is_queue_runner_running = is_worker_running


def get_worker_status() -> dict[str, Any]:
    global _CURRENT_JOB_ID
    running = is_worker_running()
    with _QUEUE_LOCK:
        queued_ids = [j_id for j_id, _ in _JOB_QUEUE]
    return {
        "running": running,
        "status": "running" if running else "stopped",
        "current_job_id": _CURRENT_JOB_ID if running else None,
        "queued_count": len(queued_ids),
        "queued_job_ids": queued_ids,
    }


def start_queue_runner() -> None:
    """Auto-start the queue runner and recover queued jobs from disk."""
    global _QUEUE_THREAD, _STOP_EVENT
    _STOP_EVENT.clear()

    # Recover QUEUED jobs from disk
    jobs = list_jobs()
    now_ts = time.time()
    with _QUEUE_LOCK:
        existing_queued_ids = {j_id for j_id, _ in _JOB_QUEUE}
        for j in sorted(jobs, key=lambda x: x.created_at):
            if j.status == "QUEUED" and j.job_id not in existing_queued_ids:
                _JOB_QUEUE.append((j.job_id, "v1"))
                existing_queued_ids.add(j.job_id)
            elif j.status in ("RUNNING_CHECKS", "ESTIMATING", "TRAIN_SCAN", "FREEZING", "VALIDATION_SCAN"):
                # Check if it was stalled from a previous dead process
                age = now_ts - getattr(j, "last_update_ts", 0)
                if age > 120 or j.last_update_ts == 0:
                    j.status = "FAILED"
                    j.verdict = "FAIL"
                    j.reject_reason = "Interrupted by server restart"
                    j.current_step = "Interrupted by server restart (stalled)"
                    j.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                    save_job(j)

    if _QUEUE_THREAD is None or not _QUEUE_THREAD.is_alive():
        _QUEUE_THREAD = threading.Thread(
            target=_queue_worker_loop,
            daemon=True,
            name="strategy-lab-queue-runner",
        )
        _QUEUE_THREAD.start()


def stop_queue_runner(timeout: float = 2.0) -> None:
    """Signal queue runner to stop and optionally wait for thread to join."""
    global _STOP_EVENT, _QUEUE_THREAD
    _STOP_EVENT.set()
    if _QUEUE_THREAD is not None and _QUEUE_THREAD.is_alive():
        _QUEUE_THREAD.join(timeout=timeout)
        if not _QUEUE_THREAD.is_alive():
            _QUEUE_THREAD = None


def retry_job(job_id: str) -> StrategyLabJob:
    """Re-queue a failed, cancelled, or stalled job."""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    job.status = "QUEUED"
    job.verdict = "PENDING"
    job.progress_pct = 0.0
    job.coins_done = 0
    job.coins_total = 0
    job.scan_phase = ""
    job.current_step = f"Queued for execution on {job.timeframe}"
    job.reject_reason = None
    job.last_update_ts = time.time()
    job.updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    save_job(job)
    append_job_log(job_id, "Job retried by user.")

    with _QUEUE_LOCK:
        if (job_id, "v1") not in _JOB_QUEUE:
            _JOB_QUEUE.append((job_id, "v1"))

    start_queue_runner()
    return job


def cancel_job(job_id: str) -> StrategyLabJob:
    """Cancel a queued or running job."""
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")

    with _QUEUE_LOCK:
        _JOB_QUEUE[:] = [(j_id, v) for j_id, v in _JOB_QUEUE if j_id != job_id]

    job.status = "CANCELLED"
    job.verdict = "CANCELLED"
    job.current_step = "Cancelled by user"
    job.last_update_ts = time.time()
    job.updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    save_job(job)
    append_job_log(job_id, "Job cancelled by user.")
    return job


def clear_all_jobs() -> int:
    """Delete every Strategy Lab job (queued, running and finished) and its logs/results.

    A running job stops at its next coin (it sees it was cleared). Uploaded strategies are
    removed from the list, but trial count K is NOT reset: K counts every strategy x timeframe
    ever tried, which is what keeps a lucky pass honest.
    """
    import shutil

    jobs_dir = Path("data/strategy_lab/jobs")
    with _QUEUE_LOCK:
        _JOB_QUEUE.clear()
        if jobs_dir.exists():  # make sure jobs only on disk are counted too
            for sub in jobs_dir.iterdir():
                if sub.is_dir() and (sub / "job.json").exists() and sub.name not in _JOBS:
                    get_job(sub.name)
        removed = len(_JOBS)
        for job in _JOBS.values():
            job.status = "CANCELLED"  # a running pipeline raises JobCancelled on its next update
        _JOBS.clear()
    if jobs_dir.exists():
        shutil.rmtree(jobs_dir, ignore_errors=True)
    StrategyRegistry().clear_entries()
    return removed


def start_pipeline_jobs(
    strategy_entry: Any,
    timeframes: list[str] | None = None,
    rules_version: str = "v1",
    auto_start: bool = True,
    eval_mode: str = "standard",
) -> list[StrategyLabJob]:
    """Create a separate pipeline job for each selected timeframe in TF_ORDER.

    Job order: 4h -> 2h -> 1h -> 30m -> 15m -> 5m (slowest last).
    Each selected timeframe = a separate job = 1 trial in K (if not run before).
    Jobs run sequentially one after another.
    """
    registry = StrategyRegistry()
    raw_tfs = timeframes if timeframes else [getattr(strategy_entry, "timeframe", "4h")]

    # Sort in specified order: 4h -> 2h -> 1h -> 30m -> 15m -> 5m
    ordered_tfs = [tf for tf in TF_ORDER if tf in raw_tfs] + [tf for tf in raw_tfs if tf not in TF_ORDER]

    created_jobs: list[StrategyLabJob] = []
    with _QUEUE_LOCK:
        for tf in ordered_tfs:
            trial_k = registry.allocate_trial_k(
                strategy_entry.sha256,
                tf,
                strategy_name=strategy_entry.name,
                mode=eval_mode,
            )
            job_id = str(uuid4())[:12]
            job = StrategyLabJob(
                job_id=job_id,
                strategy_id=strategy_entry.strategy_id,
                strategy_name=strategy_entry.name,
                strategy_version=strategy_entry.version,
                strategy_path=strategy_entry.file_path,
                timeframe=tf,
                trial_k=trial_k,
                eval_mode=eval_mode,
                status="QUEUED",
                current_step=f"Queued for execution on {tf}",
                last_update_ts=time.time(),
                updated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            )
            _JOBS[job_id] = job
            save_job(job)
            append_job_log(job_id, f"Created and queued for {tf} [{eval_mode}] (Trial K: {trial_k})")
            created_jobs.append(job)
            effective_rules = "walkforward_v1" if eval_mode == "walk_forward" else rules_version
            if auto_start:
                _JOB_QUEUE.append((job_id, effective_rules))

    if auto_start:
        start_queue_runner()

    return created_jobs


def start_pipeline_job(
    strategy_entry: Any,
    rules_version: str = "v1",
    auto_start: bool = True,
    timeframe: str | None = None,
    eval_mode: str = "standard",
) -> StrategyLabJob:
    """Instantiate a new pipeline job and queue background worker."""
    tf = timeframe or getattr(strategy_entry, "timeframe", "4h")
    jobs = start_pipeline_jobs(
        strategy_entry,
        timeframes=[tf],
        rules_version=rules_version,
        auto_start=auto_start,
        eval_mode=eval_mode,
    )
    return jobs[0]


def get_comparison_report(strategy_id: str | None = None) -> list[dict[str, Any]]:
    """Retrieve comparison rows across timeframes ordered by 4h -> 2h -> 1h -> 30m -> 15m -> 5m."""
    jobs = list_jobs()
    if strategy_id:
        jobs = [j for j in jobs if j.strategy_id == strategy_id or j.strategy_id.endswith(strategy_id)]
    elif jobs:
        latest_strategy_id = jobs[0].strategy_id
        jobs = [j for j in jobs if j.strategy_id == latest_strategy_id]

    tf_map: dict[str, dict[str, Any]] = {}
    for j in sorted(jobs, key=lambda x: x.created_at):
        if j.status == "COMPLETED" and j.comparison_row:
            tf = j.timeframe or j.comparison_row.get("tf", "4h")
            tf_map[tf] = j.comparison_row

    res: list[dict[str, Any]] = []
    for tf in TF_ORDER:
        if tf in tf_map:
            res.append(tf_map[tf])
    for tf, row in tf_map.items():
        if tf not in TF_ORDER:
            res.append(row)
    return res


def export_comparison_report_csv(strategy_id: str | None = None) -> str:
    """Generate CSV string of the comparison report table."""
    rows = get_comparison_report(strategy_id)
    headers = [
        "TF",
        "eligible coins",
        "TRAIN positions (all eligible)",
        "TRAIN pooled mean R (all eligible)",
        "TRAIN pooled PF (all eligible)",
        "avg cost in R",
        "selected",
        "2025 positions",
        "2025 pooled mean R",
        "2025 pooled PF",
        "% selected profitable 2025",
        "verdict",
    ]
    key_mapping = [
        ("TF", "tf"),
        ("eligible coins", "eligible_coins"),
        ("TRAIN positions (all eligible)", "train_positions"),
        ("TRAIN pooled mean R (all eligible)", "train_pooled_mean_r"),
        ("TRAIN pooled PF (all eligible)", "train_pooled_pf"),
        ("avg cost in R", "avg_cost_in_r"),
        ("selected", "selected"),
        ("2025 positions", "val_positions"),
        ("2025 pooled mean R", "val_pooled_mean_r"),
        ("2025 pooled PF", "val_pooled_pf"),
        ("% selected profitable 2025", "pct_selected_profitable_2025"),
        ("verdict", "verdict"),
    ]
    lines = [",".join(headers)]
    for r in rows:
        vals = [str(r.get(k, "")) for _, k in key_mapping]
        lines.append(",".join(vals))
    return "\n".join(lines) + "\n"
