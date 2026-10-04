"""Automated quality and integrity checks for Strategy Lab strategies.

Every uploaded strategy must pass:
  1. Execution on benchmark symbol (e.g. ETHUSDT) for its timeframe without errors.
  2. Accounting: sum of trade PnL == sum of position PnL.
  3. Determinism: identical trades across two consecutive runs.
  4. No-look-ahead: data truncated at 2/3 yields identical indicator values
     and identical closed positions before the cut.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from deskmind.config import DEFAULT_DATA_SYMBOLS
from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategy_lab.base import Strategy, load_strategy, run_strategy_backtest

DEFAULT_TEST_SYMBOL = next((s for s in DEFAULT_DATA_SYMBOLS if "ETH" in s), DEFAULT_DATA_SYMBOLS[0])


class StrategyCheckError(Exception):
    """Raised when a strategy fails any validation check."""

    def __init__(self, check_name: str, reason: str):
        super().__init__(f"Strategy REJECTED [{check_name}]: {reason}")
        self.check_name = check_name
        self.reason = reason


def run_strategy_checks(
    strategy_or_path: Strategy | Path | str,
    symbol: str = DEFAULT_TEST_SYMBOL,
    data_dir: Path | str = "data/parquet",
    meta_path: Path | str = "data/meta/symbol_filters_universe.json",
    df: pd.DataFrame | None = None,
    tick: float | None = None,
) -> dict[str, Any]:
    """Run the 4 standard integrity checks on a strategy.

    Returns:
        dict with status ('PASSED' or 'REJECTED'), details for each check, and stats.
    """
    # 0. Load strategy if path provided
    if isinstance(strategy_or_path, (str, Path)):
        try:
            strategy = load_strategy(strategy_or_path)
        except Exception as e:
            return {
                "status": "REJECTED",
                "failed_check": "syntax_and_import",
                "reason": str(e),
            }
    else:
        strategy = strategy_or_path

    # Check 1: Execution on benchmark symbol
    tf = strategy.timeframe
    try:
        if df is None:
            df = load_ohlcv(symbol, tf, data_dir=data_dir)
        if tick is None:
            tick = get_tick_size(symbol, meta_path=meta_path)
    except Exception as e:
        return {
            "status": "REJECTED",
            "failed_check": "data_loading",
            "reason": f"Failed to load data for {symbol} ({tf}): {e}",
        }

    try:
        res1 = run_strategy_backtest(strategy, df, tick=tick)
    except Exception as e:
        return {
            "status": "REJECTED",
            "failed_check": "execution",
            "reason": f"Strategy raised an exception during backtest: {e}",
        }

    trades1 = res1["trades"]
    positions1 = res1["positions"]

    # Check 2: Accounting (sum of trade PnL == sum of position PnL)
    if not positions1.empty:
        trade_pnl_sum = trades1["pnl"].sum()
        pos_pnl_sum = positions1["net_pnl"].sum()
        diff = abs(trade_pnl_sum - pos_pnl_sum)
        if diff >= 1e-6:
            return {
                "status": "REJECTED",
                "failed_check": "accounting",
                "reason": (
                    f"Trade PnL sum ({trade_pnl_sum:.6f}) does not match "
                    f"position net PnL sum ({pos_pnl_sum:.6f}), diff: {diff:.6e}"
                ),
            }

    # Check 3: Determinism (two runs produce identical results)
    try:
        res2 = run_strategy_backtest(strategy, df, tick=tick)
        if not trades1.empty or not res2["trades"].empty:
            pd.testing.assert_frame_equal(trades1, res2["trades"])
        if not positions1.empty or not res2["positions"].empty:
            pd.testing.assert_frame_equal(positions1, res2["positions"])
    except Exception as e:
        return {
            "status": "REJECTED",
            "failed_check": "determinism",
            "reason": f"Consecutive runs produced divergent trades or positions: {e}",
        }

    # Check 4: No-Look-Ahead (truncate at 2/3)
    cut = len(df) * 2 // 3
    df_cut = df.iloc[:cut].copy().reset_index(drop=True)
    try:
        # Check indicators before cut
        ind_full = strategy.indicators(df.copy().reset_index(drop=True)).iloc[:cut]
        ind_cut = strategy.indicators(df_cut.copy().reset_index(drop=True))

        for col in ind_cut.columns:
            if pd.api.types.is_numeric_dtype(ind_cut[col]):
                val_full = ind_full[col].to_numpy(float)
                val_cut = ind_cut[col].to_numpy(float)
                np.testing.assert_allclose(val_full, val_cut, equal_nan=True)
            else:
                assert (ind_full[col] == ind_cut[col]).all(), f"Mismatch in non-numeric column {col}"

        # Check closed positions before cut
        res_cut = run_strategy_backtest(strategy, df_cut, tick=tick)
        cut_time = df["open_time"].iloc[cut - 1]

        pos_full_before_cut = positions1[positions1["exit_time"] < cut_time].reset_index(drop=True)
        pos_cut_before_cut = res_cut["positions"][
            res_cut["positions"]["exit_time"] < cut_time
        ].reset_index(drop=True)

        pd.testing.assert_frame_equal(pos_full_before_cut, pos_cut_before_cut)
    except Exception as e:
        return {
            "status": "REJECTED",
            "failed_check": "no_lookahead",
            "reason": f"Look-ahead detected on data truncation: {e}",
        }

    return {
        "status": "PASSED",
        "strategy": strategy.name,
        "version": strategy.version,
        "timeframe": strategy.timeframe,
        "checks": {
            "execution": True,
            "accounting": True,
            "determinism": True,
            "no_lookahead": True,
        },
        "stats": res1["stats"],
    }


def assert_strategy_passes(
    strategy_or_path: Strategy | Path | str,
    symbol: str = DEFAULT_TEST_SYMBOL,
    data_dir: Path | str = "data/parquet",
    meta_path: Path | str = "data/meta/symbol_filters_universe.json",
    df: pd.DataFrame | None = None,
    tick: float | None = None,
) -> dict[str, Any]:
    """Run checks and raise StrategyCheckError if rejected."""
    result = run_strategy_checks(
        strategy_or_path,
        symbol=symbol,
        data_dir=data_dir,
        meta_path=meta_path,
        df=df,
        tick=tick,
    )
    if result["status"] == "REJECTED":
        raise StrategyCheckError(result["failed_check"], result["reason"])
    return result
