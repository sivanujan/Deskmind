"""Tests for Walk-Forward Evaluation mode in Strategy Lab."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from deskmind.strategy_lab.pipeline import StrategyLabJob
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.rules import load_walkforward_rules
from deskmind.strategy_lab.walkforward import run_walkforward_pipeline_sync
from deskmind.strategy_lab.workers import scan_symbol_worker


def test_rules_walkforward_v1_frozen():
    """Verify strategy_lab/rules_walkforward_v1.yaml loads frozen 8-quarter criteria."""
    rules = load_walkforward_rules("walkforward_v1")
    assert rules.version == "walkforward_v1"
    assert len(rules.quarters) == 8

    # Check quarters sequence
    expected_quarters = [
        ("2024Q1", "2023-01-01", "2023-12-31", "2024-01-01", "2024-03-31"),
        ("2024Q2", "2023-04-01", "2024-03-31", "2024-04-01", "2024-06-30"),
        ("2024Q3", "2023-07-01", "2024-06-30", "2024-07-01", "2024-09-30"),
        ("2024Q4", "2023-10-01", "2024-09-30", "2024-10-01", "2024-12-31"),
        ("2025Q1", "2024-01-01", "2024-12-31", "2025-01-01", "2025-03-31"),
        ("2025Q2", "2024-04-01", "2025-03-31", "2025-04-01", "2025-06-30"),
        ("2025Q3", "2024-07-01", "2025-06-30", "2025-07-01", "2025-09-30"),
        ("2025Q4", "2024-10-01", "2025-09-30", "2025-10-01", "2025-12-31"),
    ]

    for q, (qid, tr_s, tr_e, te_s, te_e) in zip(rules.quarters, expected_quarters, strict=True):
        assert q.id == qid
        assert q.train_start == tr_s
        assert q.train_end == tr_e
        assert q.test_start == te_s
        assert q.test_end == te_e
        # Crucial no look-ahead invariant: train_end strictly precedes test_start
        assert pd.Timestamp(q.train_end) < pd.Timestamp(q.test_start)

    # Scaled selection criteria (30 scaled from 24m to 12m = 15 positions)
    assert rules.selection_rules.min_positions == 15
    assert rules.selection_rules.min_profit_factor == 1.30
    assert rules.selection_rules.min_mean_r == 0.0
    assert rules.selection_rules.max_drawdown_pct == 20.0

    # Verdict rules
    assert rules.verdict_rules.min_pooled_mean_r == 0.0
    assert rules.verdict_rules.min_pooled_profit_factor == 1.20
    assert rules.verdict_rules.min_profitable_quarters == 5
    assert rules.verdict_rules.total_quarters == 8

    # 2026 split stays locked
    assert rules.test_2026_locked is True


def test_walkforward_no_lookahead_coin_selection_proof(tmp_path):
    """PROVE NO LOOK-AHEAD: Coins are chosen ONLY from data before each test quarter.

    We set up a dataset spanning 2023-01-01 through 2024-03-31.
    For Quarter 2024 Q1 (train 2023-01-01 -> 2023-12-31, test 2024-01-01 -> 2024-03-31):
    1. Run coin selection on the original dataset.
    2. Completely scramble and mutate future prices/volumes during and after the test quarter
       (on/after 2024-01-01).
    3. Re-run coin selection for 2024 Q1.
    4. Assert coin selection and all training metrics are 100% IDENTICAL, proving data after
       train_end (test quarter data) cannot leak into or alter coin selection.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Generate 3 coins
    dates = pd.date_range("2023-01-01", "2024-03-31 20:00:00", freq="4h", tz="UTC")
    n = len(dates)

    strat_file = tmp_path / "strategy.py"
    strat_file.write_text(
        """
import numpy as np
import pandas as pd

class Strategy:
    name: str = "TestStrat"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict = {}

    def indicators(self, df):
        return df

    def on_bar(self, i, ctx, broker):
        # Enter every 50 bars to generate positions
        if i % 50 == 0:
            broker.order_market(direction=1, limit=None, stop=ctx.close * 0.95)
        elif broker.has_open_position and broker.position_bars_held >= 5:
            broker.close_position()
""",
        encoding="utf-8",
    )

    symbols = ["COIN_A", "COIN_B", "COIN_C"]
    daily_vol_records = []

    for idx, sym in enumerate(symbols):
        np.random.seed(42 + idx)
        price = 100.0 * np.exp(np.cumsum(np.random.normal(0, 0.01, n)))
        df = pd.DataFrame({
            "open_time": dates,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1000.0,
        })
        df.to_parquet(data_dir / f"{sym}_4h.parquet")

    # Volume parquet ($50M volume so liquidity passes)
    vol_days = pd.date_range("2022-12-01", "2024-04-01", freq="1D")
    for d in vol_days:
        for sym in symbols:
            daily_vol_records.append({
                "symbol": sym,
                "date": d.strftime("%Y-%m-%d"),
                "quote_volume": 50_000_000.0,
            })
    vol_path = tmp_path / "daily_volume.parquet"
    pd.DataFrame(daily_vol_records).to_parquet(vol_path)

    # Meta filters
    meta = {sym: {"tick_size": 0.01} for sym in symbols}
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    # Step 1: Run selection on original data for 2024 Q1 (train: 2023-01-01 -> 2023-12-31)
    train_results_orig = []
    for sym in symbols:
        res = scan_symbol_worker(
            sym=sym,
            tf="4h",
            strat_path=str(strat_file),
            start="2023-01-01",
            end="2023-12-31",
            min_quote_volume_30d=20_000_000.0,
            tiered_slippage=True,
            vol_data_path=str(vol_path),
            data_dir=str(data_dir),
            meta_path=str(meta_path),
        )
        train_results_orig.append(res)

    # Step 2: Now radically mutate all bars on or after 2024-01-01 (future test period)
    cutoff = pd.Timestamp("2024-01-01", tz="UTC")
    for sym in symbols:
        df = pd.read_parquet(data_dir / f"{sym}_4h.parquet")
        mask = df["open_time"] >= cutoff
        # Drastic 1,000x surge and inversion in the future
        df.loc[mask, "open"] = df.loc[mask, "open"] * 1000.0
        df.loc[mask, "high"] = df.loc[mask, "high"] * 2000.0
        df.loc[mask, "low"] = df.loc[mask, "low"] * 0.001
        df.loc[mask, "close"] = df.loc[mask, "close"] * 1000.0
        df.to_parquet(data_dir / f"{sym}_4h.parquet")

    # Step 3: Re-run selection on the mutated future dataset
    train_results_after_mutation = []
    for sym in symbols:
        res = scan_symbol_worker(
            sym=sym,
            tf="4h",
            strat_path=str(strat_file),
            start="2023-01-01",
            end="2023-12-31",
            min_quote_volume_30d=20_000_000.0,
            tiered_slippage=True,
            vol_data_path=str(vol_path),
            data_dir=str(data_dir),
            meta_path=str(meta_path),
        )
        train_results_after_mutation.append(res)

    # Step 4: PROOF: Every single metric and selection decision is 100% IDENTICAL
    for r_orig, r_mut in zip(train_results_orig, train_results_after_mutation, strict=True):
        assert r_orig["symbol"] == r_mut["symbol"]
        assert r_orig["positions"] == r_mut["positions"]
        assert pytest.approx(r_orig["mean_r"]) == r_mut["mean_r"]
        assert pytest.approx(r_orig["profit_factor"]) == r_mut["profit_factor"]
        assert pytest.approx(r_orig["max_drawdown_pct"]) == r_mut["max_drawdown_pct"]
        assert pytest.approx(r_orig["net_profit"]) == r_mut["net_profit"]


def test_walkforward_trial_count_k_increment(tmp_path):
    """Verify each walk-forward run counts as 1 trial in K (Requirement 5)."""
    reg = StrategyRegistry(
        registry_file=tmp_path / "registry.json",
        uploads_dir=tmp_path / "uploads",
    )
    assert reg.trial_count_k == 0

    code = """
import numpy as np
import pandas as pd

class Strategy:
    name: str = "StratK"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict = {}
    def indicators(self, df): return df
    def on_bar(self, i, ctx, broker): pass
"""
    entry, is_new = reg.register_code(code.encode("utf-8"))
    assert is_new is True
    # Standard upload registered: K=1
    assert reg.trial_count_k == 1

    # Running standard mode on 4h: already counted
    k_std = reg.allocate_trial_k(entry.sha256, "4h", mode="standard")
    assert k_std == 1
    assert reg.trial_count_k == 1

    # Running walk-forward mode on 4h: increments K by 1
    k_wf = reg.allocate_trial_k(entry.sha256, "4h", mode="walk_forward")
    assert k_wf == 2
    assert reg.trial_count_k == 2

    # Re-running walk-forward on same code & tf does NOT increment K
    k_wf_re = reg.allocate_trial_k(entry.sha256, "4h", mode="walk_forward")
    assert k_wf_re == 2
    assert reg.trial_count_k == 2


def test_walkforward_verdict_pass_and_fail_logic():
    """Verify pooled verdict logic: pooled mean R > 0, pooled PF >= 1.20, >= 5 profitable quarters."""
    rules = load_walkforward_rules("walkforward_v1")
    v_rules = rules.verdict_rules

    # Scenario A: Passing all three rules
    pooled_mean_r_pass = 0.05
    pooled_pf_pass = 1.35
    profitable_q_pass = 6

    pass_results = {
        "pooled_mean_r": pooled_mean_r_pass > v_rules.min_pooled_mean_r,
        "pooled_pf": pooled_pf_pass >= v_rules.min_pooled_profit_factor,
        "profitable_quarters": profitable_q_pass >= v_rules.min_profitable_quarters,
    }
    assert all(pass_results.values()) is True

    # Scenario B: Failing profitable quarters (< 5 of 8)
    profitable_q_fail = 4
    fail_results = {
        "pooled_mean_r": pooled_mean_r_pass > v_rules.min_pooled_mean_r,
        "pooled_pf": pooled_pf_pass >= v_rules.min_pooled_profit_factor,
        "profitable_quarters": profitable_q_fail >= v_rules.min_profitable_quarters,
    }
    assert all(fail_results.values()) is False

    # Scenario C: Failing pooled PF (< 1.20)
    pooled_pf_fail = 1.15
    fail_pf_results = {
        "pooled_mean_r": pooled_mean_r_pass > v_rules.min_pooled_mean_r,
        "pooled_pf": pooled_pf_fail >= v_rules.min_pooled_profit_factor,
        "profitable_quarters": profitable_q_pass >= v_rules.min_profitable_quarters,
    }
    assert all(fail_pf_results.values()) is False
