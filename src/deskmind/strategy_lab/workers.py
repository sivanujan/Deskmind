"""Worker functions for process-level parallel coin scans in Strategy Lab."""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np
import pandas as pd

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.phase2_runner import build_volume_map
from deskmind.strategy_lab.base import load_strategy, run_strategy_backtest

# Module-level cache per process
_WORKER_VOL_MAP: dict[tuple[str, str], float] | None = None
_WORKER_VOL_PATH: str | None = None


def lower_worker_priority() -> None:
    """Pool initializer: run scan workers below normal priority so the desktop, browser and API
    stay responsive while a backtest uses the CPU."""
    try:
        if sys.platform == "win32":
            import ctypes

            below_normal = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal)
        else:
            os.nice(10)
    except Exception:
        pass  # priority is a nicety; never fail a scan over it


def get_worker_vol_map(vol_path: str) -> dict[tuple[str, str], float]:
    global _WORKER_VOL_MAP, _WORKER_VOL_PATH
    if _WORKER_VOL_MAP is None or _WORKER_VOL_PATH != vol_path:
        _WORKER_VOL_MAP = build_volume_map(vol_path)
        _WORKER_VOL_PATH = vol_path
    return _WORKER_VOL_MAP


def scan_symbol_worker(
    sym: str,
    tf: str,
    strat_path: str,
    start: str,
    end: str,
    min_quote_volume_30d: float,
    tiered_slippage: bool,
    vol_data_path: str,
    data_dir: str | None = None,
    meta_path: str | None = None,
) -> dict[str, Any]:
    """Execute backtest for a single symbol and timeframe inside a worker process.

    Returns deterministic dictionary of stats and positions.
    """
    vol_map = get_worker_vol_map(vol_data_path)
    try:
        tick = get_tick_size(sym, meta_path=meta_path) if meta_path else get_tick_size(sym)
        df = load_ohlcv(sym, tf, data_dir=data_dir) if data_dir else load_ohlcv(sym, tf)
    except Exception:
        return {
            "symbol": sym,
            "is_eligible": False,
            "positions": 0,
            "closed_trades": 0,
            "profit_factor": 0.0,
            "mean_r": 0.0,
            "max_drawdown_pct": 0.0,
            "net_profit": 0.0,
            "win_rate_pct": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "fees": 0.0,
            "risk_usd": 0.0,
            "not_tradeable_reason": "Data not found or unreadable",
            "positions_records": [],
        }

    df["date_str"] = df["open_time"].dt.strftime("%Y-%m-%d")
    df["vol_30d_med"] = [vol_map.get((sym, d), float("nan")) for d in df["date_str"]]

    # Check eligibility in window
    t_start_ts = pd.Timestamp(start, tz="UTC")
    t_end_ts = pd.Timestamp(end, tz="UTC")
    window_mask = (df["open_time"] >= t_start_ts) & (df["open_time"] <= t_end_ts)
    window_vols = df.loc[window_mask, "vol_30d_med"]
    has_eligible_bars = (window_vols >= min_quote_volume_30d).any()

    if not has_eligible_bars:
        return {
            "symbol": sym,
            "is_eligible": False,
            "positions": 0,
            "closed_trades": 0,
            "profit_factor": 0.0,
            "mean_r": 0.0,
            "max_drawdown_pct": 0.0,
            "net_profit": 0.0,
            "win_rate_pct": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "fees": 0.0,
            "risk_usd": 0.0,
            "not_tradeable_reason": "30-day quote volume < $20M",
            "positions_records": [],
        }

    strat = load_strategy(strat_path)
    res = run_strategy_backtest(
        strat,
        df,
        tick=tick,
        start=start,
        end=end,
        min_quote_volume_30d=min_quote_volume_30d,
        tiered_slippage=tiered_slippage,
    )
    st = res["stats"]
    pf_val = float(st.get("profit_factor", 0.0))
    if np.isinf(pf_val):
        pf_val = 999.0

    trades_df = res["trades"]
    fees_sum = float(trades_df["fees"].sum()) if "fees" in trades_df.columns and not trades_df.empty else 0.0
    risk_sum = float(trades_df["risk_usd"].sum()) if "risk_usd" in trades_df.columns and not trades_df.empty else 0.0

    gp = float(st.get("gross_profit", 0.0))
    gl = float(st.get("gross_loss", 0.0))
    net_pnl = float(st.get("net_profit", 0.0))

    # Extract positions records for pooled calculations
    pos_records: list[dict[str, float]] = []
    if "positions" in res and not res["positions"].empty:
        pos_df = res["positions"]
        for _, p_row in pos_df.iterrows():
            pos_records.append({
                "net_pnl": float(p_row.get("net_pnl", 0.0)),
                "r_multiple": float(p_row.get("r_multiple", 0.0)),
                "cost_in_r": float(p_row.get("cost_in_r", 0.0)),
                "cost_usd": float(p_row.get("cost_usd", 0.0)),
                "risk_unit": float(p_row.get("risk_unit", 0.0)),
            })

    cost_in_r_vals = [p["cost_in_r"] for p in pos_records]
    avg_cost_in_r = float(np.mean(cost_in_r_vals)) if cost_in_r_vals else 0.0

    return {
        "symbol": sym,
        "is_eligible": True,
        "positions": int(st.get("positions", 0)),
        "closed_trades": int(st.get("closed_trades", 0)),
        "profit_factor": round(pf_val, 3),
        "mean_r": round(float(st.get("mean_r", 0.0)), 4),
        "max_drawdown_pct": round(float(st.get("max_drawdown_pct", 0.0)), 2),
        "net_profit": round(net_pnl, 2),
        "win_rate_pct": round(float(st.get("win_rate_pct", 0.0)), 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "fees": fees_sum,
        "risk_usd": risk_sum,
        "avg_cost_in_r": round(avg_cost_in_r, 4),
        "not_tradeable_reason": None,
        "positions_records": pos_records,
    }
