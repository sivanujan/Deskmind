"""Strategy Lab execution pipeline running checks, TRAIN scan, freeze, and VALIDATION."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
from deskmind.strategy_lab.rules import StrategyLabRules, load_rules


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
    is_concentrated: bool = False  # e.g. > 70% from top 4


@dataclass
class StrategyLabJob:
    job_id: str
    strategy_id: str
    strategy_name: str
    strategy_version: str
    strategy_path: str
    trial_k: int
    status: str  # QUEUED, RUNNING_CHECKS, ESTIMATING, TRAIN_SCAN, FREEZING, VALIDATION_SCAN, COMPLETED, REJECTED, FAILED
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
_EXECUTOR = ThreadPoolExecutor(max_workers=2)


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
    # Also load from disk
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
        tf = strat.timeframe

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

        job.time_per_coin_s = round(t_coin, 3)
        job.estimated_seconds = round(t_coin * len(symbols), 1)

        # ── Step c) TRAIN scan on all eligible coins (per-symbol independent) ─
        update("TRAIN_SCAN", 15.0, f"Scanning {len(symbols)} coins on TRAIN ({rules.splits.train.start} -> {rules.splits.train.end})")

        train_results: list[dict[str, Any]] = []
        eligible_count = 0
        total_syms = len(symbols)

        for idx, sym in enumerate(symbols, 1):
            pct = 15.0 + (idx / total_syms) * 45.0
            if idx % 20 == 0 or idx == total_syms:
                update(None, pct, f"TRAIN scan: processed {idx}/{total_syms} coins ({sym})")

            try:
                tick = get_tick_size(sym)
                df = load_ohlcv(sym, tf)
            except Exception:
                continue

            df["date_str"] = df["open_time"].dt.strftime("%Y-%m-%d")
            df["vol_30d_med"] = [vol_map.get((sym, d), float("nan")) for d in df["date_str"]]

            # Check eligibility in window
            t_start_ts = pd.Timestamp(rules.splits.train.start, tz="UTC")
            t_end_ts = pd.Timestamp(rules.splits.train.end, tz="UTC")
            window_mask = (df["open_time"] >= t_start_ts) & (df["open_time"] <= t_end_ts)
            window_vols = df.loc[window_mask, "vol_30d_med"]
            has_eligible_bars = (window_vols >= rules.liquidity.min_30d_median_quote_volume_usd).any()

            if not has_eligible_bars:
                train_results.append({
                    "symbol": sym,
                    "is_eligible": False,
                    "positions": 0,
                    "closed_trades": 0,
                    "profit_factor": 0.0,
                    "mean_r": 0.0,
                    "max_drawdown_pct": 0.0,
                    "net_profit": 0.0,
                    "win_rate_pct": 0.0,
                    "not_tradeable_reason": "30-day quote volume < $20M",
                })
                continue

            eligible_count += 1
            # Run backtest
            res = run_strategy_backtest(
                strat,
                df,
                tick=tick,
                start=rules.splits.train.start,
                end=rules.splits.train.end,
                min_quote_volume_30d=rules.liquidity.min_30d_median_quote_volume_usd,
                tiered_slippage=rules.slippage.tiered,
            )
            st = res["stats"]
            pf_val = float(st.get("profit_factor", 0.0))
            if np.isinf(pf_val):
                pf_val = 999.0

            train_results.append({
                "symbol": sym,
                "is_eligible": True,
                "positions": int(st.get("positions", 0)),
                "closed_trades": int(st.get("closed_trades", 0)),
                "profit_factor": round(pf_val, 3),
                "mean_r": round(float(st.get("mean_r", 0.0)), 4),
                "max_drawdown_pct": round(float(st.get("max_drawdown_pct", 0.0)), 2),
                "net_profit": round(float(st.get("net_profit", 0.0)), 2),
                "win_rate_pct": round(float(st.get("win_rate_pct", 0.0)), 2),
                "not_tradeable_reason": None,
            })

        job.funnel.eligible_coins = eligible_count

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

            # Evaluate selection gates
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
        all_val_positions: list[pd.DataFrame] = []
        all_val_trades: list[pd.DataFrame] = []

        for idx, sym in enumerate(selected_symbols, 1):
            pct = 70.0 + (idx / max(len(selected_symbols), 1)) * 15.0
            update(None, pct, f"Validation: processed {idx}/{len(selected_symbols)} selected coins ({sym})")

            try:
                tick = get_tick_size(sym)
                df = load_ohlcv(sym, tf)
                df["date_str"] = df["open_time"].dt.strftime("%Y-%m-%d")
                df["vol_30d_med"] = [vol_map.get((sym, d), float("nan")) for d in df["date_str"]]
            except Exception:
                continue

            res_val = run_strategy_backtest(
                strat,
                df,
                tick=tick,
                start=rules.splits.validation.start,
                end=rules.splits.validation.end,
                min_quote_volume_30d=rules.liquidity.min_30d_median_quote_volume_usd,
                tiered_slippage=rules.slippage.tiered,
            )
            st_val = res_val["stats"]
            p_df = res_val["positions"]
            t_df = res_val["trades"]

            if not p_df.empty:
                all_val_positions.append(p_df)
            if not t_df.empty:
                all_val_trades.append(t_df)

            pf_val = float(st_val.get("profit_factor", 0.0))
            if np.isinf(pf_val):
                pf_val = 999.0

            val_selected_results[sym] = {
                "positions": int(st_val.get("positions", 0)),
                "closed_trades": int(st_val.get("closed_trades", 0)),
                "profit_factor": round(pf_val, 3),
                "mean_r": round(float(st_val.get("mean_r", 0.0)), 4),
                "net_profit": round(float(st_val.get("net_profit", 0.0)), 2),
                "max_drawdown_pct": round(float(st_val.get("max_drawdown_pct", 0.0)), 2),
                "win_rate_pct": round(float(st_val.get("win_rate_pct", 0.0)), 2),
            }

        # Also run all eligible coins on 2025 for comparison row
        update(None, 88.0, "Computing 2025 universe comparison row across all eligible coins")
        eligible_symbols = [r["symbol"] for r in train_results if r["is_eligible"]]
        comp_positions: list[pd.DataFrame] = []
        comp_trades: list[pd.DataFrame] = []

        for sym in eligible_symbols:
            try:
                tick = get_tick_size(sym)
                df = load_ohlcv(sym, tf)
                df["date_str"] = df["open_time"].dt.strftime("%Y-%m-%d")
                df["vol_30d_med"] = [vol_map.get((sym, d), float("nan")) for d in df["date_str"]]
                c_res = run_strategy_backtest(
                    strat,
                    df,
                    tick=tick,
                    start=rules.splits.validation.start,
                    end=rules.splits.validation.end,
                    min_quote_volume_30d=rules.liquidity.min_30d_median_quote_volume_usd,
                    tiered_slippage=rules.slippage.tiered,
                )
                if not c_res["positions"].empty:
                    comp_positions.append(c_res["positions"])
                if not c_res["trades"].empty:
                    comp_trades.append(c_res["trades"])
            except Exception:
                continue

        # Compute comparison row pooled metrics
        if comp_positions:
            c_pos_all = pd.concat(comp_positions, ignore_index=True)
            c_tr_all = pd.concat(comp_trades, ignore_index=True) if comp_trades else pd.DataFrame()
            c_wins = c_tr_all[c_tr_all["pnl"] > 0]["pnl"].sum() if not c_tr_all.empty else 0.0
            c_losses = -c_tr_all[c_tr_all["pnl"] <= 0]["pnl"].sum() if not c_tr_all.empty else 0.0
            c_pf = (c_wins / c_losses) if c_losses > 0 else (999.0 if c_wins > 0 else 0.0)
            c_r = c_pos_all["r_multiple"].dropna()
            job.comparison_row = {
                "name": "All Eligible Coins (2025)",
                "coins_count": len(eligible_symbols),
                "pooled_positions": len(c_pos_all),
                "pooled_trades": len(c_tr_all),
                "pooled_mean_r": round(float(c_r.mean()), 4) if len(c_r) > 0 else 0.0,
                "pooled_profit_factor": round(float(c_pf), 3),
                "pooled_net_profit": round(float(c_pos_all["net_pnl"].sum()), 2),
            }
        else:
            job.comparison_row = {
                "name": "All Eligible Coins (2025)",
                "coins_count": len(eligible_symbols),
                "pooled_positions": 0,
                "pooled_trades": 0,
                "pooled_mean_r": 0.0,
                "pooled_profit_factor": 0.0,
                "pooled_net_profit": 0.0,
            }

        # ── Step f) Verdict PASS / FAIL by rules_v1 ───────────────────────────
        update("VALIDATION_SCAN", 95.0, "Evaluating validation pass/fail criteria")

        # Selected pooled metrics
        if all_val_positions:
            v_pos_all = pd.concat(all_val_positions, ignore_index=True)
            v_tr_all = pd.concat(all_val_trades, ignore_index=True) if all_val_trades else pd.DataFrame()
            v_wins = v_tr_all[v_tr_all["pnl"] > 0]["pnl"].sum() if not v_tr_all.empty else 0.0
            v_losses = -v_tr_all[v_tr_all["pnl"] <= 0]["pnl"].sum() if not v_tr_all.empty else 0.0
            v_pf = (v_wins / v_losses) if v_losses > 0 else (999.0 if v_wins > 0 else 0.0)
            v_r = v_pos_all["r_multiple"].dropna()
            pooled_mean_r = round(float(v_r.mean()), 4) if len(v_r) > 0 else 0.0
            pooled_pf = round(float(v_pf), 3)
        else:
            pooled_mean_r = 0.0
            pooled_pf = 0.0

        # Profitable selected coins in 2025
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

        # Build candidate coins (selected AND profitable in 2025)
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


def start_pipeline_job(
    strategy_entry: Any, rules_version: str = "v1", auto_start: bool = True
) -> StrategyLabJob:
    """Instantiate a new pipeline job and queue background worker."""
    job_id = str(uuid4())[:12]
    job = StrategyLabJob(
        job_id=job_id,
        strategy_id=strategy_entry.strategy_id,
        strategy_name=strategy_entry.name,
        strategy_version=strategy_entry.version,
        strategy_path=strategy_entry.file_path,
        trial_k=strategy_entry.trial_k,
        status="QUEUED",
        current_step="Queued for execution",
    )
    _JOBS[job_id] = job
    save_job(job)

    if auto_start:
        import threading

        thread = threading.Thread(
            target=run_pipeline_sync,
            args=(job_id, rules_version),
            daemon=True,
            name=f"pipeline-{job_id}",
        )
        thread.start()
    return job
