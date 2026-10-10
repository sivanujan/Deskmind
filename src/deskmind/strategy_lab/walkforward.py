"""Walk-Forward Evaluation pipeline for Strategy Lab."""

from __future__ import annotations

import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from deskmind.strategies.tas.phase2_runner import load_universe_symbols
from deskmind.strategy_lab.checks import run_strategy_checks
from deskmind.strategy_lab.rules import WalkForwardRules, load_walkforward_rules


def run_walkforward_pipeline_sync(
    job: Any,
    rules_version: str = "walkforward_v1",
    num_workers: int | None = None,
    data_dir: Path | str | None = None,
    meta_path: Path | str | None = None,
) -> Any:
    """Execute 8-quarter rolling walk-forward evaluation.

    For each quarter:
      1. Train/select coins on previous 12 months using rules_v1 selection rules
         scaled to 12 months (positions >= 15, PF >= 1.3, mean R > 0, DD <= 20%).
      2. Trade ONLY those selected coins on the subsequent 3-month out-of-sample quarter.
      3. Aggregate quarter metrics: coins selected, positions, mean R, PF, net profit.

    After 8 quarters:
      Compute pooled mean R, pooled PF, and count profitable quarters.
      Verdict PASS iff pooled mean R > 0, pooled PF >= 1.2, and >= 5 of 8 quarters profitable.
    """
    from deskmind.strategy_lab.pipeline import (
        JobCancelled,
        _JOBS,
        _scan_coins,
        append_job_log,
        save_job,
    )

    rules: WalkForwardRules = load_walkforward_rules(rules_version)
    strat_path = Path(job.strategy_path)
    tf = job.timeframe

    append_job_log(
        job.job_id,
        f"Starting Walk-Forward pipeline (8 quarters) for {job.strategy_name} on {tf}",
    )

    def _check_cancelled() -> None:
        if job.job_id not in _JOBS or _JOBS[job.job_id].status == "CANCELLED":
            raise JobCancelled(f"Job {job.job_id} cancelled")

    def update(
        status: str | None,
        progress: float,
        step: str,
        current_coin: str = "",
        coins: tuple[int, int] | None = None,
        log: bool = True,
    ) -> None:
        _check_cancelled()
        if status:
            job.status = status
        job.progress_pct = round(progress, 1)
        job.current_step = step
        job.current_coin = current_coin
        if coins:
            job.coins_done, job.coins_total = coins
        now_ts = time.time()
        job.last_update_ts = now_ts
        job.updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        job.elapsed_seconds = round(now_ts - job.last_update_ts, 1)
        save_job(job)
        if log:
            append_job_log(job.job_id, f"[{job.progress_pct:.1f}%] {step}")

    try:
        # ── Step a) Run safety and syntax checks ────────────────────────────
        update("RUNNING_CHECKS", 5.0, "Running safety, look-ahead, and accounting checks")
        if data_dir is not None:
            check_res = run_strategy_checks(
                strat_path,
                data_dir=data_dir,
                meta_path=meta_path or "data/meta/symbol_filters_universe.json",
            )
        else:
            check_res = run_strategy_checks(strat_path)

        if check_res.get("status") == "REJECTED":
            job.status = "REJECTED"
            job.verdict = "REJECTED"
            job.reject_reason = check_res.get("reason", "Failed strategy checks")
            job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            save_job(job)
            append_job_log(job.job_id, f"Strategy rejected by checks: {job.reject_reason}")
            return job

        append_job_log(job.job_id, "Strategy checks PASSED")

        # ── Step b) Discover universe symbols ────────────────────────────────
        if data_dir:
            d_path = Path(data_dir)
            files = list(d_path.glob(f"*_{tf}.parquet"))
            universe_symbols = sorted({f.stem.replace(f"_{tf}", "") for f in files})
        else:
            universe_symbols = load_universe_symbols(rules.universe.path)
            universe_symbols = [
                s for s in universe_symbols if s not in rules.universe.excluded_symbols
            ]

        total_universe = len(universe_symbols)
        job.funnel.total_universe = total_universe
        append_job_log(job.job_id, f"Eligible universe candidate pool: {total_universe} coins")

        if total_universe == 0:
            job.status = "FAILED"
            job.verdict = "FAIL"
            job.reject_reason = f"No cached OHLCV data found for timeframe {tf}"
            job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            save_job(job)
            return job

        if num_workers is None:
            num_workers = min(os.cpu_count() or 4, 8)

        # ── Step c) 8-Quarter Rolling Walk-Forward Loop ───────────────────────
        quarters = rules.quarters
        total_q = len(quarters)
        quarter_rows: list[dict[str, Any]] = []
        all_oos_pos_records: list[dict[str, Any]] = []

        sel_rules = rules.selection_rules

        for q_idx, q in enumerate(quarters, 1):
            _check_cancelled()
            base_pct = 10.0 + ((q_idx - 1) / total_q) * 85.0
            half_step = 85.0 / total_q / 2.0

            # 1. Train scan on previous 12 months
            job.scan_phase = f"TRAIN_{q.id}"
            update(
                "TRAIN_SCAN",
                base_pct,
                f"[{q_idx}/{total_q}] {q.label}: 12m selection scan ({q.train_start} → {q.train_end})",
                coins=(0, total_universe),
            )

            train_results: list[dict[str, Any]] = []

            def on_train(idx: int, res_sym: dict[str, Any]) -> None:
                train_results.append(res_sym)
                sym = res_sym["symbol"]
                update(
                    None,
                    base_pct + (idx / max(total_universe, 1)) * half_step,
                    f"{q.label} train scan: {idx}/{total_universe} ({sym})",
                    current_coin=f"{idx}/{total_universe} {sym}",
                    coins=(idx, total_universe),
                    log=idx % 10 == 0 or idx == total_universe,
                )

            _scan_coins(
                num_workers,
                universe_symbols,
                (
                    tf,
                    str(strat_path),
                    q.train_start,
                    q.train_end,
                    rules.liquidity.min_30d_median_quote_volume_usd,
                    rules.slippage.tiered,
                    rules.liquidity.volume_data_path,
                    str(data_dir) if data_dir else None,
                    str(meta_path) if meta_path else None,
                ),
                on_train,
            )

            # 2. Select coins meeting frozen scaled selection rules
            selected_coins: list[str] = []
            for r in train_results:
                if not r["is_eligible"]:
                    continue
                passes_r = r["mean_r"] >= sel_rules.min_mean_r
                passes_pf = r["profit_factor"] >= sel_rules.min_profit_factor
                passes_positions = r["positions"] >= sel_rules.min_positions
                passes_dd = r["max_drawdown_pct"] <= sel_rules.max_drawdown_pct
                if passes_r and passes_pf and passes_positions and passes_dd:
                    selected_coins.append(r["symbol"])

            append_job_log(
                job.job_id,
                f"{q.label}: selected {len(selected_coins)} coins based on {q.train_start} → {q.train_end}",
            )

            # 3. Trade ONLY selected coins on the subsequent 3-month test quarter
            test_results: dict[str, Any] = {}
            q_pos_records: list[dict[str, Any]] = []

            if selected_coins:
                job.scan_phase = f"TEST_{q.id}"
                update(
                    "VALIDATION_SCAN",
                    base_pct + half_step,
                    f"[{q_idx}/{total_q}] {q.label}: testing {len(selected_coins)} coins ({q.test_start} → {q.test_end})",
                    coins=(0, len(selected_coins)),
                )

                def on_test(idx: int, res_sym: dict[str, Any]) -> None:
                    test_results[res_sym["symbol"]] = res_sym
                    q_pos_records.extend(res_sym.get("positions_records", []))
                    sym = res_sym["symbol"]
                    update(
                        None,
                        base_pct + half_step + (idx / max(len(selected_coins), 1)) * half_step,
                        f"{q.label} test scan: {idx}/{len(selected_coins)} ({sym})",
                        current_coin=f"{idx}/{len(selected_coins)} {sym}",
                        coins=(idx, len(selected_coins)),
                        log=idx % 5 == 0 or idx == len(selected_coins),
                    )

                _scan_coins(
                    num_workers,
                    selected_coins,
                    (
                        tf,
                        str(strat_path),
                        q.test_start,
                        q.test_end,
                        rules.liquidity.min_30d_median_quote_volume_usd,
                        rules.slippage.tiered,
                        rules.liquidity.volume_data_path,
                        str(data_dir) if data_dir else None,
                        str(meta_path) if meta_path else None,
                    ),
                    on_test,
                )

                q_positions = sum(r.get("positions", 0) for r in test_results.values())
                q_gp = sum(r.get("gross_profit", 0.0) for r in test_results.values())
                q_gl = sum(r.get("gross_loss", 0.0) for r in test_results.values())
                q_profit = sum(r.get("net_profit", 0.0) for r in test_results.values())
                q_r_vals = [
                    p["r_multiple"]
                    for p in q_pos_records
                    if not np.isnan(p.get("r_multiple", np.nan))
                ]
                q_mean_r = float(np.mean(q_r_vals)) if q_r_vals else 0.0
                q_pf = (q_gp / q_gl) if q_gl > 0 else (999.0 if q_gp > 0 else 0.0)
            else:
                q_positions = 0
                q_gp = 0.0
                q_gl = 0.0
                q_profit = 0.0
                q_mean_r = 0.0
                q_pf = 0.0

            q_row = {
                "quarter_id": q.id,
                "quarter": q.label,
                "train_start": q.train_start,
                "train_end": q.train_end,
                "test_start": q.test_start,
                "test_end": q.test_end,
                "coins_selected": len(selected_coins),
                "coins": selected_coins,
                "positions": q_positions,
                "mean_r": round(q_mean_r, 4),
                "profit_factor": round(q_pf, 2),
                "profit": round(q_profit, 2),
                "gross_profit": round(q_gp, 2),
                "gross_loss": round(q_gl, 2),
                "is_profitable": q_profit > 0,
            }
            quarter_rows.append(q_row)
            all_oos_pos_records.extend(q_pos_records)

            append_job_log(
                job.job_id,
                f"{q.label} results: {len(selected_coins)} coins, {q_positions} pos, "
                f"Mean R: {q_mean_r:.4f}, PF: {q_pf:.2f}, Profit: ${q_profit:.2f}",
            )

        # ── Step d) Aggregate combined results and compute verdict ───────────
        update("COMPLETING", 96.0, "Aggregating walk-forward metrics and evaluating verdict")

        total_positions = sum(r["positions"] for r in quarter_rows)
        total_gp = sum(r["gross_profit"] for r in quarter_rows)
        total_gl = sum(r["gross_loss"] for r in quarter_rows)
        total_net_profit = sum(r["profit"] for r in quarter_rows)

        pooled_r_vals = [
            p["r_multiple"]
            for p in all_oos_pos_records
            if not np.isnan(p.get("r_multiple", np.nan))
        ]
        pooled_mean_r = float(np.mean(pooled_r_vals)) if pooled_r_vals else 0.0
        pooled_pf = (total_gp / total_gl) if total_gl > 0 else (999.0 if total_gp > 0 else 0.0)
        profitable_quarters = sum(1 for r in quarter_rows if r["profit"] > 0)

        all_unique_selected = sorted({s for r in quarter_rows for s in r["coins"]})

        v_rules = rules.verdict_rules
        rule_results: dict[str, Any] = {
            "pooled_mean_r": {
                "value": round(pooled_mean_r, 4),
                "threshold": v_rules.min_pooled_mean_r,
                "passed": pooled_mean_r > v_rules.min_pooled_mean_r,
                "rule": f"Pooled mean R > {v_rules.min_pooled_mean_r}",
            },
            "pooled_profit_factor": {
                "value": round(pooled_pf, 2),
                "threshold": v_rules.min_pooled_profit_factor,
                "passed": pooled_pf >= v_rules.min_pooled_profit_factor,
                "rule": f"Pooled profit factor >= {v_rules.min_pooled_profit_factor}",
            },
            "profitable_quarters": {
                "value": profitable_quarters,
                "threshold": v_rules.min_profitable_quarters,
                "passed": profitable_quarters >= v_rules.min_profitable_quarters,
                "rule": f"At least {v_rules.min_profitable_quarters} of {v_rules.total_quarters} quarters profitable",
            },
        }

        overall_pass = all(r["passed"] for r in rule_results.values())
        job.rules_verdict = rule_results
        job.verdict = "PASS" if overall_pass else "FAIL"

        combined_row = {
            "quarter": f"Combined ({len(quarter_rows)} Quarters)",
            "quarter_id": "COMBINED",
            "coins_selected": len(all_unique_selected),
            "positions": total_positions,
            "mean_r": round(pooled_mean_r, 4),
            "profit_factor": round(pooled_pf, 2),
            "profit": round(total_net_profit, 2),
            "gross_profit": round(total_gp, 2),
            "gross_loss": round(total_gl, 2),
            "profitable_quarters": f"{profitable_quarters}/{len(quarter_rows)}",
            "verdict": job.verdict,
        }

        job.walkforward_rows = quarter_rows
        job.walkforward_combined = combined_row
        job.selected_symbols = all_unique_selected
        job.funnel.selected_coins = len(all_unique_selected)
        job.funnel.profitable_2025 = profitable_quarters

        job.comparison_row = {
            "tf": tf,
            "eligible_coins": total_universe,
            "train_positions": 0,
            "train_pooled_mean_r": 0.0,
            "train_pooled_pf": 0.0,
            "avg_cost_in_r": 0.0,
            "selected": len(all_unique_selected),
            "val_positions": total_positions,
            "val_pooled_mean_r": round(pooled_mean_r, 4),
            "val_pooled_pf": round(pooled_pf, 2),
            "pct_selected_profitable_2025": round(
                (profitable_quarters / max(len(quarter_rows), 1)) * 100.0, 1
            ),
            "verdict": job.verdict,
        }

        job.progress_pct = 100.0
        job.status = "COMPLETED"
        job.current_step = f"Walk-forward evaluation complete: {job.verdict}"
        job.current_coin = ""
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)

        append_job_log(
            job.job_id,
            f"Walk-Forward Complete: Verdict={job.verdict} | Pooled Mean R={pooled_mean_r:.4f} "
            f"| Pooled PF={pooled_pf:.2f} | Profitable Qs={profitable_quarters}/{len(quarter_rows)}",
        )

        return job

    except JobCancelled:
        job.status = "CANCELLED"
        job.verdict = "CANCELLED"
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)
        append_job_log(job.job_id, "Job was CANCELLED by user")
        return job

    except Exception as e:
        job.status = "FAILED"
        job.verdict = "FAIL"
        job.reject_reason = f"Pipeline execution crashed: {e}"
        job.completed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)
        append_job_log(job.job_id, f"Pipeline execution error: {e}")
        return job
