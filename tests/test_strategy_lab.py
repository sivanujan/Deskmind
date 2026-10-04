"""Tests for Strategy Lab Phase A & B (indicators, broker, base protocol, and validation checks)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.tas_strategy import run_backtest as run_old_backtest
from deskmind.strategy_lab.base import (
    StrategyValidationError,
    load_strategy,
    run_strategy_backtest,
    validate_strategy_ast,
)
from deskmind.strategy_lab.checks import assert_strategy_passes

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
