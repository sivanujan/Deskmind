"""Tests for Strategy Lab Phase A & B (indicators, broker, base protocol, validation checks, rules, registry, API)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from deskmind.api.app import create_app
from deskmind.config import get_settings
from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.tas_strategy import run_backtest as run_old_backtest
from deskmind.strategy_lab.base import (
    StrategyValidationError,
    load_strategy,
    run_strategy_backtest,
    validate_strategy_ast,
)
from deskmind.strategy_lab.checks import assert_strategy_passes
from deskmind.strategy_lab.registry import StrategyRegistry
from deskmind.strategy_lab.rules import load_rules

TAS_STRATEGY_PATH = Path("src/deskmind/strategy_lab/strategies/tas_v1.py")
FIXTURE_2000_BARS = Path("tests/fixtures/ethusdt_4h_2000.parquet")


def test_validator_rejects_forbidden_imports():
    code_bad_os = """
import os
class Strategy:
    name = "bad"
"""
    with pytest.raises(StrategyValidationError, match="Forbidden import"):
        validate_strategy_ast(code_bad_os)

    code_bad_requests = """
from requests import get
class Strategy:
    name = "bad"
"""
    with pytest.raises(StrategyValidationError, match="Forbidden import"):
        validate_strategy_ast(code_bad_requests)


def test_validator_rejects_missing_strategy_class():
    code_no_class = """
import numpy as np
a = 1
"""
    with pytest.raises(StrategyValidationError, match="must define a 'Strategy' class"):
        validate_strategy_ast(code_no_class)


def test_validator_rejects_missing_attributes():
    code_missing_attr = """
import numpy as np
class Strategy:
    name = "Incomplete"
    # missing version, timeframe, params, indicators, on_bar
"""
    with pytest.raises(StrategyValidationError, match="missing required attributes"):
        validate_strategy_ast(code_missing_attr)


def test_validator_accepts_clean_code():
    code_clean = """
import numpy as np
import pandas as pd
import math
from deskmind.strategy_lab import ExitBracket, TradingViewBroker

class Strategy:
    name: str = "Clean"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict = {}

    def indicators(self, df):
        return df

    def on_bar(self, i, ctx, broker):
        pass
"""
    # Should not raise
    validate_strategy_ast(code_clean)


def test_tas_v1_strategy_checks_fixture():
    """Fast integrity check on the 2,000-bar ETHUSDT fixture."""
    df_fixture = pd.read_parquet(FIXTURE_2000_BARS)
    res = assert_strategy_passes(TAS_STRATEGY_PATH, df=df_fixture, tick=0.01)
    assert res["status"] == "PASSED"
    assert res["checks"]["execution"] is True
    assert res["checks"]["accounting"] is True
    assert res["checks"]["determinism"] is True
    assert res["checks"]["no_lookahead"] is True


def test_rules_v1_configuration():
    """Verify strategy_lab/rules_v1.yaml loads with exact Phase 2 criteria."""
    rules = load_rules("v1")
    assert rules.version == "v1"
    assert rules.liquidity.min_30d_median_quote_volume_usd == 20_000_000.0
    assert rules.slippage.tiered is True
    assert len(rules.slippage.tiers) == 3

    # TRAIN gates
    assert rules.train_selection.min_positions == 30
    assert rules.train_selection.min_profit_factor == 1.30
    assert rules.train_selection.min_mean_r == 0.0
    assert rules.train_selection.max_drawdown_pct == 20.0

    # VALIDATION pass
    assert rules.validation_pass.min_pooled_mean_r == 0.0
    assert rules.validation_pass.min_pooled_profit_factor == 1.20
    assert rules.validation_pass.min_profitable_cohort_pct == 60.0
    assert rules.splits.test.locked is True


def test_registry_trial_count_k_increment(tmp_path):
    """Verify registry saves file to uploads/ and increments trial count K only for new hashes."""
    reg = StrategyRegistry(
        registry_file=tmp_path / "registry.json",
        uploads_dir=tmp_path / "uploads",
    )
    assert reg.trial_count_k == 0

    code_v1 = """
import numpy as np
import pandas as pd

class Strategy:
    name: str = "StratA"
    version: str = "1.0.0"
    timeframe: str = "4h"
    params: dict = {}

    def indicators(self, df): return df
    def on_bar(self, i, ctx, broker): pass
"""
    entry1, is_new1 = reg.register_code(code_v1.encode("utf-8"))
    assert is_new1 is True
    assert reg.trial_count_k == 1
    assert entry1.trial_k == 1
    assert Path(entry1.file_path).exists()

    # Re-uploading the exact same file must NOT increment K
    entry1_re, is_new1_re = reg.register_code(code_v1.encode("utf-8"))
    assert is_new1_re is False
    assert reg.trial_count_k == 1
    assert entry1_re.trial_k == 1

    # Uploading a new file with different code must increment K
    code_v2 = code_v1.replace("StratA", "StratB")
    entry2, is_new2 = reg.register_code(code_v2.encode("utf-8"))
    assert is_new2 is True
    assert reg.trial_count_k == 2
    assert entry2.trial_k == 2


def test_fastapi_strategy_lab_endpoints(tmp_path):
    """Test FastAPI upload, status, and trial count tracking."""
    app = create_app(get_settings())
    client = TestClient(app)

    # 1. Status endpoint
    status_res = client.get("/api/strategy-lab/status")
    assert status_res.status_code == 200
    assert "trial_count_k" in status_res.json()

    # 2. Upload valid strategy (auto_start=false for fast unit test)
    with open(TAS_STRATEGY_PATH, "rb") as f:
        up_res = client.post(
            "/api/strategy-lab/upload?auto_start=false",
            files={"file": ("tas_v1.py", f, "text/x-python")},
        )
    assert up_res.status_code == 200
    data = up_res.json()
    assert data["name"] == "TAS"
    assert data["version"] == "1.0.0"
    assert "trial_k" in data

    # 3. Upload invalid file extension -> 400
    bad_ext_res = client.post(
        "/api/strategy-lab/upload",
        files={"file": ("notes.txt", b"plain text", "text/plain")},
    )
    assert bad_ext_res.status_code == 400

    # 4. Upload forbidden code -> 422
    bad_code = b"import os\nclass Strategy:\n    name='bad'"
    bad_code_res = client.post(
        "/api/strategy-lab/upload",
        files={"file": ("bad.py", bad_code, "text/x-python")},
    )
    assert bad_code_res.status_code == 422


def test_tas_v1_phase2_regression_exact_numbers():
    """Regression test: Phase 2 numbers reproduction.
    Expected:
      - Eligible coins: 364
      - Selected coins: 36
      - 2025 pooled mean R: +0.0651
      - 2025 pooled PF: 1.132
      - Verdict: FAIL
    """
    import sqlite3
    rules = load_rules("v1")

    # Load frozen Phase 2 database table
    conn = sqlite3.connect("data/strategy_coin_results.db")
    df = pd.read_sql("SELECT * FROM strategy_coin_results", conn)
    conn.close()

    # 1. Train split eligibility & selection
    df_train = df[df["split"] == "train"]
    eligible_count = int(df_train["is_eligible"].sum())
    assert eligible_count == 364, f"Expected 364 eligible coins, got {eligible_count}"

    sel_mask = (
        (df_train["is_eligible"])
        & (df_train["positions"] >= rules.train_selection.min_positions)
        & (df_train["profit_factor"] >= rules.train_selection.min_profit_factor)
        & (df_train["mean_r"] > rules.train_selection.min_mean_r)
        & (df_train["max_drawdown_pct"] <= rules.train_selection.max_drawdown_pct)
    )
    selected_df = df_train[sel_mask]
    assert len(selected_df) == 36, f"Expected 36 selected coins, got {len(selected_df)}"

    # 2. Validation split on selected coins (2025)
    df_val_sel = df[df["split"] == "val_selected"]
    assert len(df_val_sel) == 36

    # Weighted mean R
    pooled_mean_r = (df_val_sel["mean_r"] * df_val_sel["positions"]).sum() / df_val_sel["positions"].sum()
    assert round(pooled_mean_r, 4) == 0.0651, f"Expected pooled mean R +0.0651, got {pooled_mean_r:.4f}"

    # Pooled Profit Factor
    wins = df_val_sel["gross_profit"].sum()
    losses = df_val_sel["gross_loss"].sum()
    pooled_pf = wins / losses
    assert round(pooled_pf, 3) == 1.132, f"Expected pooled PF 1.132, got {pooled_pf:.3f}"

    # Profitable cohort breadth
    profitable_count = int((df_val_sel["net_profit"] > 0).sum())
    cohort_breadth_pct = 100.0 * profitable_count / len(df_val_sel)
    assert profitable_count == 15
    assert round(cohort_breadth_pct, 1) == 41.7

    # Verdict evaluation by rules_v1:
    # Requires: mean R > 0 (PASS), PF >= 1.20 (FAIL: 1.132 < 1.20), cohort >= 60% (FAIL: 41.7% < 60%)
    pass_mean_r = pooled_mean_r > rules.validation_pass.min_pooled_mean_r
    pass_pf = pooled_pf >= rules.validation_pass.min_pooled_profit_factor
    pass_cohort = cohort_breadth_pct >= rules.validation_pass.min_profitable_cohort_pct
    verdict = "PASS" if (pass_mean_r and pass_pf and pass_cohort) else "FAIL"

    assert bool(pass_mean_r) is True
    assert bool(pass_pf) is False
    assert bool(pass_cohort) is False
    assert verdict == "FAIL", f"Expected FAIL verdict, got {verdict}"


@pytest.mark.slow
def test_tas_v1_strategy_checks_full():
    """Verify that tas_v1 passes all four integrity checks on full ETHUSDT history."""
    res = assert_strategy_passes(TAS_STRATEGY_PATH)
    assert res["status"] == "PASSED"
    assert res["checks"]["execution"] is True
    assert res["checks"]["accounting"] is True
    assert res["checks"]["determinism"] is True
    assert res["checks"]["no_lookahead"] is True


@pytest.mark.slow
def test_tas_v1_regression_vs_old_tas():
    """Regression test: ETHUSDT 4h 2023-01-01 -> 2024-12-31 must match old TAS exactly."""
    df = load_ohlcv("ETHUSDT", "4h")
    tick = get_tick_size("ETHUSDT")

    res_old = run_old_backtest(df, tick=tick, start="2023-01-01", end="2024-12-31")
    strat = load_strategy(TAS_STRATEGY_PATH)
    res_new = run_strategy_backtest(strat, df, tick=tick, start="2023-01-01", end="2024-12-31")

    t_old = res_old["trades"]
    t_new = res_new["trades"]
    assert len(t_old) == 187
    assert len(t_new) == 187

    # Check trade columns match within 1e-9 tolerance
    for col in t_old.columns:
        assert col in t_new.columns
        if pd.api.types.is_numeric_dtype(t_old[col]):
            diff = (t_old[col] - t_new[col]).abs().max()
            assert diff <= 1e-9, f"Column {col} differs by {diff}"
        else:
            assert (t_old[col] == t_new[col]).all(), f"Column {col} string mismatch"

    # Check positions match within 1e-9 tolerance
    p_old = res_old["positions"]
    p_new = res_new["positions"]
    assert len(p_old) == 103
    assert len(p_new) == 103

    for col in p_old.columns:
        assert col in p_new.columns
        if pd.api.types.is_numeric_dtype(p_old[col]):
            diff = (p_old[col] - p_new[col]).abs().max()
            assert diff <= 1e-9, f"Pos column {col} differs by {diff}"
        else:
            assert (p_old[col] == p_new[col]).all(), f"Pos column {col} string mismatch"

    # Check equity curve
    assert (res_old["equity"] - res_new["equity"]).abs().max() <= 1e-9

    # Check key stats
    for k in ("closed_trades", "positions", "net_profit", "profit_factor", "max_drawdown", "mean_r"):
        v_old = float(res_old["stats"][k])
        v_new = float(res_new["stats"][k])
        assert abs(v_old - v_new) <= 1e-6, f"Stat {k}: old={v_old}, new={v_new}"
