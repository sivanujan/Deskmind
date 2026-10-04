"""Subprocess worker for running a strategy on a single symbol with strict timeout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategy_lab.base import load_strategy, run_strategy_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run single-coin backtest in subprocess")
    parser.add_argument("--strategy", required=True, help="Path to strategy file")
    parser.add_argument("--symbol", required=True, help="Symbol name (e.g. BTCUSDT)")
    parser.add_argument("--timeframe", default="4h", help="Timeframe (e.g. 4h)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--min-volume", type=float, default=20_000_000.0)
    parser.add_argument("--vol-cache", help="Path to precomputed symbol vol parquet")
    args = parser.parse_args()

    try:
        tick = get_tick_size(args.symbol)
        df = load_ohlcv(args.symbol, args.timeframe)

        if args.vol_cache and Path(args.vol_cache).exists():
            vol_df = pd.read_parquet(args.vol_cache)
            if "date_str" not in df.columns:
                df["date_str"] = df["open_time"].dt.strftime("%Y-%m-%d")
            vol_map = dict(zip(vol_df["date_str"], vol_df["vol_30d_med"], strict=False))
            df["vol_30d_med"] = [vol_map.get(d, float("nan")) for d in df["date_str"]]

        strat = load_strategy(args.strategy)
        res = run_strategy_backtest(
            strat,
            df,
            tick=tick,
            start=args.start,
            end=args.end,
            min_quote_volume_30d=args.min_volume,
            tiered_slippage=True,
        )

        st = res["stats"]
        positions_df = res["positions"]
        trades_df = res["trades"]

        # Serialized output
        output = {
            "status": "SUCCESS",
            "symbol": args.symbol,
            "stats": {
                "closed_trades": int(st.get("closed_trades", 0)),
                "win_rate_pct": float(st.get("win_rate_pct", 0.0)),
                "net_profit": float(st.get("net_profit", 0.0)),
                "net_profit_pct": float(st.get("net_profit_pct", 0.0)),
                "gross_profit": float(st.get("gross_profit", 0.0)),
                "gross_loss": float(st.get("gross_loss", 0.0)),
                "profit_factor": (
                    float(st.get("profit_factor", 0.0))
                    if not np.isinf(st.get("profit_factor", 0.0))
                    else 999.0
                ),
                "max_drawdown": float(st.get("max_drawdown", 0.0)),
                "max_drawdown_pct": float(st.get("max_drawdown_pct", 0.0)),
                "positions": int(st.get("positions", 0)),
                "position_win_rate_pct": float(st.get("position_win_rate_pct", 0.0)),
                "mean_r": float(st.get("mean_r", 0.0)),
                "median_r": float(st.get("median_r", 0.0)),
            },
            "positions_count": len(positions_df),
            "trades_count": len(trades_df),
        }
        print(json.dumps(output))
    except Exception as e:
        err_out = {
            "status": "ERROR",
            "symbol": args.symbol,
            "error": str(e),
        }
        print(json.dumps(err_out))
        sys.exit(1)


if __name__ == "__main__":
    main()
