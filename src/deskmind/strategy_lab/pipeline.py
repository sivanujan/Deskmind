"""Strategy Lab execution pipeline running checks, TRAIN scan, freeze, and VALIDATION."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.phase2_runner import build_volume_map, load_universe_symbols
from deskmind.strategy_lab.base import load_strategy, run_strategy_backtest
from deskmind.strategy_lab.checks import run_strategy_checks
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.rules import StrategyLabRules, load_rules
from deskmind.strategy_lab.workers import scan_symbol_worker

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
    status: str = "QUEUED"  # QUEUED, RUNNING_CHECKS, ESTIMATING, TRAIN_SCAN, FREEZING, VALIDATION_SCAN, COMPLETED, REJECTED, FAILED
    progress_pct: float = 0.0
    current_step: str = "Queued"
    reject_reason: str | None = None
    time_per_coin_s: float | None = None
    estimated_seconds: float | None = None
    elapsed_seconds: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))
    completed_at: str | None = None
    verdict: str = "PENDING"  # PASS, FAIL, REJECTED, PENDING
    rules_verdict: dict[str, Any] = field(default_factory=dict)
    funnel: JobFunnel = field(default_factory=JobFunnel)
    selected_symbols: list[str] = field(default_factory=list)
    candidate_coins: list[dict[str, Any]] = field(default_factory=list)
    not_tradeable: list[dict[str, Any]] = field(default_factory=list)
    per_coin_results: list[dict[str, Any]] = field(default_factory=list)
    concentration_warning: ConcentrationWarning = field(default_factory=ConcentrationWarning)
    comparison_row: dict[str, Any] = field(default_factory=dict)
    test_2026_status: str = "LOCKED"  # LOCKED (if verdict != PASS), READY, RUNNING, COMPLETED


_JOBS: dict[str, StrategyLabJob] = {}
_JOB_QUEUE: list[tuple[str, str]] = []  # [(job_id, rules_version)]
_QUEUE_LOCK = threading.Lock()
_QUEUE_THREAD: threading.Thread | None = None


def get_job(job_id: str) -> StrategyLabJob | None:
    if job_id in _JOBS:
        return _JOBS[job_id]
    disk_path = Path("data/strategy_lab/jobs") / job_id / "job.json"
    if disk_path.exists():
        try:
            data = json.loads(disk_path.read_text(encoding="utf-8"))
            funnel_data = data.pop("funnel", {})
            cw_data = data.pop("concentration_warning", {})
            job = StrategyLabJob(
                **data,
                funnel=JobFunnel(**funnel_data),
                concentration_warning=ConcentrationWarning(**cw_data),
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
    job_dir = Path("data/strategy_lab/jobs") / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(job)
    (job_dir / "job.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_pipeline_sync(job_id: str, rules_version: str = "v1") -> None:
    job = get_job(job_id)
    if job is None:
        return

    rules = load_rules(rules_version)
    t_start_total = time.time()

    def update(
        status: str | None = None,
        progress: float | None = None,
        step: str | None = None,
    ) -> None:
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress_pct = round(progress, 1)
        if step is not None:
            job.current_step = step
        job.elapsed_seconds = round(time.time() - t_start_total, 1)
        save_job(job)

    try:
        # ── Step a) Run checks.py (look-ahead, accounting, determinism) ──────
        update("RUNNING_CHECKS", 5.0, "Running integrity checks (look-ahead, accounting, determinism)")
        strat_path = Path(job.strategy_path)
        check_res = run_strategy_checks(strat_path)

        if check_res.get("status") == "REJECTED":
            reason = check_res.get("reason", "Failed strategy checks")
            job.status = "REJECTED"
            job.verdict = "REJECTED"
            job.reject_reason = reason
            job.current_step = f"REJECTED: {reason}"
            job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            save_job(job)
            return

        # ── Step b) Time 1 coin & estimate scan time ─────────────────────────
        update("ESTIMATING", 10.0, "Timing benchmark coin for duration estimate")
        strat = load_strategy(strat_path)
        tf = job.timeframe or getattr(strat, "timeframe", "4h")
        strat.timeframe = tf

        vol_map = build_volume_map(rules.liquidity.volume_data_path)
        symbols = load_universe_symbols(rules.universe.path)
        symbols = [s for s in symbols if s not in rules.universe.excluded_symbols]
        job.funnel.total_universe = len(symbols)

        # Benchmark on benchmark coin
        bench_sym = next((s for s in symbols if "ETH" in s), symbols[0])
        t0 = time.time()
        try:
            bench_df = load_ohlcv(bench_sym, tf)
            bench_df["date_str"] = bench_df["open_time"].dt.strftime("%Y-%m-%d")
            bench_df["vol_30d_med"] = [vol_map.get((bench_sym, d), float("nan")) for d in bench_df["date_str"]]
            bench_tick = get_tick_size(bench_sym)
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

        num_workers = max(1, (os.cpu_count() or 2) - 1)
        job.time_per_coin_s = round(t_coin, 3)
        job.estimated_seconds = round((t_coin * len(symbols)) / num_workers, 1)

        # ── Step c) TRAIN scan on all coins with process parallelism ─────────
        eta_min = job.estimated_seconds / 60.0
        update(
            "TRAIN_SCAN",
            15.0,
            f"Scanning {len(symbols)} coins on {tf} (ETA ~{eta_min:.1f}m across {num_workers} cores)",
        )

        train_results: list[dict[str, Any]] = []
        all_train_pos_records: list[dict[str, float]] = []

        with ProcessPoolExecutor(max_workers=num_workers) as pool:
            futures = [
                pool.submit(
                    scan_symbol_worker,
                    sym,
                    tf,
                    str(strat_path),
                    rules.splits.train.start,
                    rules.splits.train.end,
                    rules.liquidity.min_30d_median_quote_volume_usd,
                    rules.slippage.tiered,
                    rules.liquidity.volume_data_path,
                )
                for sym in symbols
            ]
            for idx, fut in enumerate(futures, 1):
                res_sym = fut.result()
                train_results.append(res_sym)
                if res_sym["is_eligible"]:
                    all_train_pos_records.extend(res_sym["positions_records"])
                if idx % 20 == 0 or idx == len(symbols):
                    pct = 15.0 + (idx / len(symbols)) * 45.0
                    update(None, pct, f"TRAIN scan: processed {idx}/{len(symbols)} coins ({res_sym['symbol']})")

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

        for row in train_results:
            sym = row["symbol"]
            if not row["is_eligible"]:
                not_tradeable.append({"symbol": sym, "reason": "30-day quote volume < $20M"})
                continue

            pos = row["positions"]
            pf = row["profit_factor"]
            mr = row["mean_r"]
            dd = row["max_drawdown_pct"]

            reasons: list[str] = []
            if pos < sel_rules.min_positions:
                reasons.append(f"Positions < {sel_rules.min_positions} ({pos})")
            if pf < sel_rules.min_profit_factor:
                reasons.append(f"Profit Factor < {sel_rules.min_profit_factor:.2f} ({pf:.2f})")
            if mr <= sel_rules.min_mean_r:
                reasons.append(f"Mean R <= {sel_rules.min_mean_r:.2f} ({mr:.4f})")
            if dd > sel_rules.max_drawdown_pct:
                reasons.append(f"Max DD > {sel_rules.max_drawdown_pct:.1f}% ({dd:.1f}%)")

            if reasons:
                not_tradeable.append({"symbol": sym, "reason": "; ".join(reasons)})
            else:
                selected_symbols.append(sym)

        job.selected_symbols = selected_symbols
        job.funnel.selected_coins = len(selected_symbols)
        job.not_tradeable = not_tradeable

        # Save frozen selected list
        job_dir = Path("data/strategy_lab/jobs") / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "selected_coins.json").write_text(
            json.dumps({"selected_symbols": selected_symbols, "frozen_at": datetime.now(UTC).isoformat()}, indent=2),
            encoding="utf-8",
        )

        # ── Step e) VALIDATION on selected coins ──────────────────────────────
        update("VALIDATION_SCAN", 70.0, f"Validating {len(selected_symbols)} selected coins on 2025")

        val_selected_results: dict[str, dict[str, Any]] = {}
        all_val_positions: list[dict[str, float]] = []

        if selected_symbols:
            with ProcessPoolExecutor(max_workers=min(num_workers, len(selected_symbols))) as pool:
                futures = [
                    pool.submit(
                        scan_symbol_worker,
                        sym,
                        tf,
                        str(strat_path),
                        rules.splits.validation.start,
                        rules.splits.validation.end,
                        rules.liquidity.min_30d_median_quote_volume_usd,
                        rules.slippage.tiered,
                        rules.liquidity.volume_data_path,
                    )
                    for sym in selected_symbols
                ]
                for idx, fut in enumerate(futures, 1):
                    val_res = fut.result()
                    sym = val_res["symbol"]
                    val_selected_results[sym] = val_res
                    all_val_positions.extend(val_res["positions_records"])
                    pct = 70.0 + (idx / len(selected_symbols)) * 20.0
                    update(None, pct, f"Validation: processed {idx}/{len(selected_symbols)} selected coins ({sym})")

        # ── Step f) Verdict PASS / FAIL by rules_v1 ───────────────────────────
        update("VALIDATION_SCAN", 95.0, "Evaluating validation pass/fail criteria")

        if all_val_positions:
            val_r_vals = [p["r_multiple"] for p in all_val_positions if not np.isnan(p.get("r_multiple", np.nan))]
            pooled_mean_r = round(float(np.mean(val_r_vals)), 4) if val_r_vals else 0.0
        else:
            pooled_mean_r = 0.0

        val_gp = sum(v.get("gross_profit", 0.0) for v in val_selected_results.values())
        val_gl = sum(v.get("gross_loss", 0.0) for v in val_selected_results.values())
        pooled_pf = round((val_gp / val_gl) if val_gl > 0 else (999.0 if val_gp > 0 else 0.0), 3)

        profitable_coins = [
            sym for sym in selected_symbols if val_selected_results.get(sym, {}).get("net_profit", 0.0) > 0
        ]
        job.funnel.profitable_2025 = len(profitable_coins)
        profitable_pct = round(
            100.0 * len(profitable_coins) / max(len(selected_symbols), 1), 1
        )

        v_rules = rules.validation_pass
        pass_mean_r = pooled_mean_r > v_rules.min_pooled_mean_r
        pass_pf = pooled_pf >= v_rules.min_pooled_profit_factor
        pass_cohort = profitable_pct >= v_rules.min_profitable_cohort_pct
        overall_pass = pass_mean_r and pass_pf and pass_cohort

        job.verdict = "PASS" if overall_pass else "FAIL"
        job.rules_verdict = {
            "pooled_mean_r": {
                "value": pooled_mean_r,
                "threshold": v_rules.min_pooled_mean_r,
                "passed": pass_mean_r,
                "rule": f"pooled mean R > {v_rules.min_pooled_mean_r:.2f}",
            },
            "pooled_profit_factor": {
                "value": pooled_pf,
                "threshold": v_rules.min_pooled_profit_factor,
                "passed": pass_pf,
                "rule": f"pooled PF >= {v_rules.min_pooled_profit_factor:.2f}",
            },
            "profitable_cohort_pct": {
                "value": profitable_pct,
                "threshold": v_rules.min_profitable_cohort_pct,
                "passed": pass_cohort,
                "rule": f">= {v_rules.min_profitable_cohort_pct:.0f}% selected coins profitable ({len(profitable_coins)}/{len(selected_symbols)})",
            },
        }

        # Multi-timeframe comparison row (exact STEP 3 columns)
        job.comparison_row = {
            "tf": job.timeframe,
            "eligible_coins": eligible_count,
            "train_positions": len(all_train_pos_records),
            "train_pooled_mean_r": round(train_pooled_mean_r, 4),
            "train_pooled_pf": round(train_pooled_pf, 3),
            "avg_cost_in_r": round(train_avg_cost_in_r, 4),
            "selected": len(selected_symbols),
            "val_positions": len(all_val_positions),
            "val_pooled_mean_r": round(pooled_mean_r, 4),
            "val_pooled_pf": round(pooled_pf, 3),
            "pct_selected_profitable_2025": profitable_pct,
            "verdict": job.verdict,
        }

        # Candidate coins (selected AND profitable in 2025)
        train_map = {r["symbol"]: r for r in train_results}
        candidate_coins = []
        for sym in profitable_coins:
            candidate_coins.append({
                "symbol": sym,
                "label": "needs 2026 test — not for live trading yet",
                "train_positions": train_map.get(sym, {}).get("positions", 0),
                "train_pf": train_map.get(sym, {}).get("profit_factor", 0.0),
                "train_mean_r": train_map.get(sym, {}).get("mean_r", 0.0),
                "val_positions": val_selected_results.get(sym, {}).get("positions", 0),
                "val_pf": val_selected_results.get(sym, {}).get("profit_factor", 0.0),
                "val_mean_r": val_selected_results.get(sym, {}).get("mean_r", 0.0),
                "val_net_profit": val_selected_results.get(sym, {}).get("net_profit", 0.0),
            })
        candidate_coins.sort(key=lambda c: c["val_net_profit"], reverse=True)
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

        update("COMPLETED", 100.0, f"Completed with verdict: {job.verdict}")
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)

    except Exception as e:
        job.status = "FAILED"
        job.reject_reason = str(e)
        job.current_step = f"Failed with exception: {e}"
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)


def _queue_worker_loop() -> None:
    global _QUEUE_THREAD
    while True:
        with _QUEUE_LOCK:
            if not _JOB_QUEUE:
                _QUEUE_THREAD = None
                return
            job_id, rules_ver = _JOB_QUEUE.pop(0)
        try:
            run_pipeline_sync(job_id, rules_ver)
        except Exception:
            pass


def _trigger_queue_worker() -> None:
    global _QUEUE_THREAD
    with _QUEUE_LOCK:
        if _QUEUE_THREAD is None or not _QUEUE_THREAD.is_alive():
            _QUEUE_THREAD = threading.Thread(
                target=_queue_worker_loop,
                daemon=True,
                name="pipeline-sequential-queue",
            )
            _QUEUE_THREAD.start()


def start_pipeline_jobs(
    strategy_entry: Any,
    timeframes: list[str] | None = None,
    rules_version: str = "v1",
    auto_start: bool = True,
) -> list[StrategyLabJob]:
    """Create a separate pipeline job for each selected timeframe in TF_ORDER.

    Job order: 4h -> 2h -> 1h -> 30m -> 15m -> 5m (slowest last).
    Each selected timeframe = a separate job = 1 trial in K.
    Jobs run sequentially one after another.
    """
    registry = StrategyRegistry()
    raw_tfs = timeframes if timeframes else [getattr(strategy_entry, "timeframe", "4h")]

    # Sort in specified order: 4h -> 2h -> 1h -> 30m -> 15m -> 5m
    ordered_tfs = [tf for tf in TF_ORDER if tf in raw_tfs] + [tf for tf in raw_tfs if tf not in TF_ORDER]

    created_jobs: list[StrategyLabJob] = []
    with _QUEUE_LOCK:
        for tf in ordered_tfs:
            trial_k = registry.allocate_trial_k()
            job_id = str(uuid4())[:12]
            job = StrategyLabJob(
                job_id=job_id,
                strategy_id=strategy_entry.strategy_id,
                strategy_name=strategy_entry.name,
                strategy_version=strategy_entry.version,
                strategy_path=strategy_entry.file_path,
                timeframe=tf,
                trial_k=trial_k,
                status="QUEUED",
                current_step=f"Queued for execution on {tf}",
            )
            _JOBS[job_id] = job
            save_job(job)
            created_jobs.append(job)
            if auto_start:
                _JOB_QUEUE.append((job_id, rules_version))

    if auto_start:
        _trigger_queue_worker()

    return created_jobs


def start_pipeline_job(
    strategy_entry: Any,
    rules_version: str = "v1",
    auto_start: bool = True,
    timeframe: str | None = None,
) -> StrategyLabJob:
    """Instantiate a new pipeline job and queue background worker."""
    tf = timeframe or getattr(strategy_entry, "timeframe", "4h")
    jobs = start_pipeline_jobs(
        strategy_entry,
        timeframes=[tf],
        rules_version=rules_version,
        auto_start=auto_start,
    )
    return jobs[0]


def get_comparison_report(strategy_id: str | None = None) -> list[dict[str, Any]]:
    """Retrieve comparison rows across timeframes ordered by 4h -> 2h -> 1h -> 30m -> 15m -> 5m."""
    jobs = list_jobs()
    if strategy_id:
        jobs = [j for j in jobs if j.strategy_id == strategy_id or j.strategy_id.endswith(strategy_id)]

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
