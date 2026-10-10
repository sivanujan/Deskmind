"""Run Walk-Forward Evaluation on 4h for TAS and TPRE strategies."""

from __future__ import annotations

import json
from pathlib import Path

from deskmind.strategy_lab.pipeline import (
    _JOBS,
    get_job,
    start_pipeline_jobs,
)
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.walkforward import run_walkforward_pipeline_sync


def print_walkforward_report(job) -> None:
    print("=" * 80)
    print(f"WALK-FORWARD REPORT: {job.strategy_name} (Timeframe: {job.timeframe})")
    print(f"Job ID: {job.job_id} | Trial K: {job.trial_k} | Status: {job.status} | Verdict: {job.verdict}")
    print("=" * 80)
    print(
        f"{'Quarter':<10} | {'Train Dates':<23} | {'Test Dates':<23} | "
        f"{'Coins':<6} | {'Pos':<5} | {'Mean R':<8} | {'PF':<6} | {'Profit ($)':<12} | {'Result'}"
    )
    print("-" * 115)
    for row in job.walkforward_rows:
        res = "PROFITABLE" if row["is_profitable"] else "UNPROFITABLE"
        print(
            f"{row['quarter']:<10} | {row['train_start']}->{row['train_end']} | {row['test_start']}->{row['test_end']} | "
            f"{row['coins_selected']:<6} | {row['positions']:<5} | {row['mean_r']:<8.4f} | {row['profit_factor']:<6.2f} | "
            f"${row['profit']:<11.2f} | {res}"
        )
    print("-" * 115)
    comb = job.walkforward_combined
    if comb:
        print(
            f"{comb['quarter']:<10} | {'8 Rolling Quarters':<23} | {'Out-of-Sample':<23} | "
            f"{comb['coins_selected']:<6} | {comb['positions']:<5} | {comb['mean_r']:<8.4f} | {comb['profit_factor']:<6.2f} | "
            f"${comb['profit']:<11.2f} | {comb['verdict']} ({comb['profitable_quarters']} Qs)"
        )
    print("=" * 80)
    print("Rule Checks (rules_walkforward_v1.yaml):")
    for k, v in job.rules_verdict.items():
        status = "PASS" if v["passed"] else "FAIL"
        print(f"  - [{status}] {k}: {v['value']} (threshold: {v['threshold']}) -> {v['rule']}")
    print("=" * 80 + "\n")


def main():
    registry = StrategyRegistry()
    print(f"Initial Trial Count K: {registry.trial_count_k}")

    # 1. TAS strategy
    tas_path = Path("src/deskmind/strategy_lab/strategies/tas_v1.py")
    if not tas_path.exists():
        tas_path = Path("uploads/TAS_1.0.0_2e590f53.py")

    tas_entry, _ = registry.register_code(tas_path.read_bytes(), filename_hint=tas_path.name)
    print(f"\nRegistered TAS: {tas_entry.strategy_id} (sha: {tas_entry.short_hash})")

    # Queue walk-forward job for TAS (do not auto_start on background thread; run synchronously here)
    tas_jobs = start_pipeline_jobs(
        tas_entry,
        timeframes=["4h"],
        rules_version="walkforward_v1",
        auto_start=False,
        eval_mode="walk_forward",
    )
    tas_job = tas_jobs[0]
    print(f"Running Walk-Forward for TAS (Job {tas_job.job_id}, Trial K={tas_job.trial_k})...")
    run_walkforward_pipeline_sync(tas_job, rules_version="walkforward_v1")
    print_walkforward_report(tas_job)

    # 2. TPRE strategy
    tpre_path = Path("uploads/TPRE_1.0.0_37c12aca.py")
    tpre_entry, _ = registry.register_code(tpre_path.read_bytes(), filename_hint=tpre_path.name)
    print(f"\nRegistered TPRE: {tpre_entry.strategy_id} (sha: {tpre_entry.short_hash})")

    tpre_jobs = start_pipeline_jobs(
        tpre_entry,
        timeframes=["4h"],
        rules_version="walkforward_v1",
        auto_start=False,
        eval_mode="walk_forward",
    )
    tpre_job = tpre_jobs[0]
    print(f"Running Walk-Forward for TPRE (Job {tpre_job.job_id}, Trial K={tpre_job.trial_k})...")
    run_walkforward_pipeline_sync(tpre_job, rules_version="walkforward_v1")
    print_walkforward_report(tpre_job)

    print(f"Final Trial Count K: {registry.trial_count_k}")


if __name__ == "__main__":
    main()
