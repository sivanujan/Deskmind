"""TAS Phase 2 — Multi-coin Universe Scan (4h).

Pre-registered protocol execution:
  - 4h timeframe
  - Universe: universe_2022_2025.json (non-crypto excluded)
  - Eligibility: 30-day rolling median daily quote volume >= $20M at signal time
  - Tiered slippage: >= $500M 1 tick, $100M-$500M 0.02%, $20M-$100M 0.05%
  - TRAIN: 2023-01-01 -> 2024-12-31
  - Selection: positions >= 30, PF >= 1.3, mean R > 0, max DD <= 20%
  - VALIDATION: 2025-01-01 -> 2025-12-31
  - Pass criteria: pooled mean R > 0, pooled PF >= 1.2, >= 60% coins profitable in 2025
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from deskmind.strategies.tas.loader import get_tick_size, load_ohlcv
from deskmind.strategies.tas.tas_strategy import TASParams, run_backtest


def get_timestamp_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_volume_map(
    daily_vol_path: Path | str = "data/daily_quote_volume.parquet",
) -> dict[tuple[str, str], float]:
    """Pre-compute rolling 30-day median daily quote volume shifted by 1 day."""
    print(f"[{get_timestamp_str()}] Loading daily quote volume from {daily_vol_path}...")
    daily = pd.read_parquet(daily_vol_path).sort_values(["symbol", "date"])
    daily["vol_30d_med"] = daily.groupby("symbol")["quote_volume"].transform(
        lambda s: s.rolling(30, min_periods=1).median().shift(1)
    )
    pairs = zip(daily["symbol"], daily["date"], strict=False)
    vol_map = dict(zip(pairs, daily["vol_30d_med"], strict=False))
    print(f"[{get_timestamp_str()}] Built volume lookup for {len(vol_map)} symbol-days.")
    return vol_map


def save_to_databases(df: pd.DataFrame, table_name: str = "strategy_coin_results") -> None:
    """Save results dataframe to DuckDB and SQLite."""
    # 1. DuckDB
    duckdb_path = Path("data/research.duckdb")
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {table_name} AS SELECT * FROM df WHERE 1=0")
        # Upsert or append
        con.register("df_view", df)
        # Delete existing entries for this strategy, split, and symbol to avoid duplication
        for (split, sym), _ in df.groupby(["split", "symbol"]):
            con.execute(
                f"DELETE FROM {table_name} WHERE strategy_name='TAS' AND split=? AND symbol=?",
                [split, sym],
            )
        con.execute(f"INSERT INTO {table_name} SELECT * FROM df_view")
    finally:
        con.close()

    # 2. SQLite
    sqlite_path = Path("data/strategy_coin_results.db")
    sqlite_conn = sqlite3.connect(str(sqlite_path))
    try:
        # Create table if not exists
        df.to_sql(table_name, sqlite_conn, if_exists="append", index=False)
    finally:
        sqlite_conn.close()


def run_coin_scan(
    symbols: list[str],
    vol_map: dict[tuple[str, str], float],
    start: str,
    end: str,
    split_name: str,
    min_volume: float = 20_000_000,
) -> tuple[pd.DataFrame, list[pd.DataFrame], list[pd.DataFrame]]:
    """Run TAS 4h backtest on a list of symbols with liquidity filtering and tiered slippage."""
    results = []
    all_positions = []
    all_trades = []

    total = len(symbols)
    print(
        f"[{get_timestamp_str()}] Starting scan: {total} symbols ({split_name}: {start} -> {end})"
    )

    for idx, sym in enumerate(symbols, 1):
        if idx % 50 == 0 or idx == total:
            print(f"[{get_timestamp_str()}] Progress: {idx}/{total} symbols processed...")

        try:
            tick = get_tick_size(sym)
        except Exception:
            continue

        try:
            df = load_ohlcv(sym, "4h")
        except Exception:
            continue

        df["date_str"] = df["open_time"].dt.strftime("%Y-%m-%d")
        df["vol_30d_med"] = [vol_map.get((sym, d), float("nan")) for d in df["date_str"]]

        # Check eligibility in window
        window_mask = (df["open_time"] >= pd.Timestamp(start, tz="UTC")) & (
            df["open_time"] <= pd.Timestamp(end, tz="UTC")
        )
        window_vols = df.loc[window_mask, "vol_30d_med"]
        has_eligible_bars = (window_vols >= min_volume).any()

        if not has_eligible_bars:
            results.append(
                {
                    "strategy_name": "TAS",
                    "timeframe": "4h",
                    "split": split_name,
                    "symbol": sym,
                    "is_eligible": False,
                    "closed_trades": 0,
                    "win_rate_pct": 0.0,
                    "net_profit": 0.0,
                    "net_profit_pct": 0.0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "profit_factor": 0.0,
                    "max_drawdown": 0.0,
                    "max_drawdown_pct": 0.0,
                    "positions": 0,
                    "position_win_rate_pct": 0.0,
                    "mean_r": 0.0,
                    "median_r": 0.0,
                }
            )
            continue

        res = run_backtest(
            df,
            tick=tick,
            params=TASParams(),
            start=start,
            end=end,
            min_quote_volume_30d=min_volume,
            tiered_slippage=True,
        )

        st = res["stats"]
        p_df = res["positions"]
        t_df = res["trades"]

        if not p_df.empty:
            p_df["symbol"] = sym
            p_df["split"] = split_name
            all_positions.append(p_df)

        if not t_df.empty:
            t_df["symbol"] = sym
            t_df["split"] = split_name
            all_trades.append(t_df)

        results.append(
            {
                "strategy_name": "TAS",
                "timeframe": "4h",
                "split": split_name,
                "symbol": sym,
                "is_eligible": True,
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
            }
        )

    res_df = pd.DataFrame(results)
    return res_df, all_positions, all_trades


def compute_pooled_metrics(positions: list[pd.DataFrame], trades: list[pd.DataFrame]) -> dict:
    """Compute pooled portfolio-level metrics across all positions and trades."""
    if not positions:
        return {
            "pooled_positions": 0,
            "pooled_trades": 0,
            "pooled_mean_r": 0.0,
            "pooled_median_r": 0.0,
            "pooled_net_profit": 0.0,
            "pooled_profit_factor": 0.0,
            "position_win_rate_pct": 0.0,
        }

    pos_all = pd.concat(positions, ignore_index=True)
    tr_all = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()

    r = pos_all["r_multiple"].dropna()
    wins = tr_all[tr_all["pnl"] > 0]["pnl"].sum() if not tr_all.empty else 0.0
    losses = -tr_all[tr_all["pnl"] <= 0]["pnl"].sum() if not tr_all.empty else 0.0
    pf = (wins / losses) if losses > 0 else (999.0 if wins > 0 else 0.0)

    return {
        "pooled_positions": len(pos_all),
        "pooled_trades": len(tr_all),
        "pooled_mean_r": round(float(r.mean()), 4) if len(r) > 0 else 0.0,
        "pooled_median_r": round(float(r.median()), 4) if len(r) > 0 else 0.0,
        "pooled_net_profit": round(float(pos_all["net_pnl"].sum()), 2),
        "pooled_profit_factor": round(float(pf), 3),
        "position_win_rate_pct": round(float(100.0 * (pos_all["net_pnl"] > 0).mean()), 2),
    }


def load_universe_symbols(
    universe_path: Path | str = "reports/research/universe_2022_2025.json",
) -> list[str]:
    with open(universe_path, encoding="utf-8") as f:
        data = json.load(f)
    symbols = [item["symbol"] for item in data]
    # Ensure excluded non-crypto are omitted
    excluded = {"TSLAUSDT", "AMZNUSDT", "GOOGLUSDT", "XAUUSDT", "XAGUSDT", "SPXUSDT", "BTCDOMUSDT"}
    return [s for s in symbols if s not in excluded]


def run_phase2_train() -> pd.DataFrame:
    """Execute TRAIN split for all coins, save DB/CSV, and freeze selected coins."""
    out_dir = Path("reports/research/tas")
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = load_universe_symbols()
    vol_map = build_volume_map()

    print(f"[{get_timestamp_str()}] Executing TRAIN (2023-01-01 -> 2024-12-31)...")
    res_df, _, _ = run_coin_scan(
        symbols=symbols,
        vol_map=vol_map,
        start="2023-01-01",
        end="2024-12-31",
        split_name="train",
    )

    # Save to databases
    print(f"[{get_timestamp_str()}] Saving TRAIN stats to DuckDB and SQLite...")
    save_to_databases(res_df)

    # Save CSV
    train_csv_path = out_dir / "tas_phase2_train_results.csv"
    res_df.to_csv(train_csv_path, index=False)
    print(f"[{get_timestamp_str()}] Saved TRAIN results to {train_csv_path}")

    # Selection gate
    selected_mask = (
        (res_df["is_eligible"])
        & (res_df["positions"] >= 30)
        & (res_df["profit_factor"] >= 1.30)
        & (res_df["mean_r"] > 0)
        & (res_df["max_drawdown_pct"] <= 20.0)
    )
    selected_df = res_df[selected_mask].copy().sort_values("profit_factor", ascending=False)
    selected_symbols = selected_df["symbol"].tolist()

    # Save selected coins
    selected_json_path = out_dir / "tas_phase2_selected_coins.json"
    with open(selected_json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "created_at": get_timestamp_str(),
                "selection_criteria": {
                    "min_positions": 30,
                    "min_profit_factor": 1.30,
                    "min_mean_r": 0.0,
                    "max_drawdown_pct": 20.0,
                },
                "total_universe": len(symbols),
                "eligible_coins_count": int(res_df["is_eligible"].sum()),
                "selected_count": len(selected_symbols),
                "selected_symbols": selected_symbols,
                "selected_details": selected_df.to_dict(orient="records"),
            },
            f,
            indent=2,
        )

    selected_csv_path = out_dir / "tas_phase2_selected_coins.csv"
    selected_df.to_csv(selected_csv_path, index=False)
    print(f"[{get_timestamp_str()}] Saved {len(selected_symbols)} selected coins to:")
    print(f"  - {selected_json_path}")
    print(f"  - {selected_csv_path}")

    return res_df


def run_phase2_validation() -> tuple[pd.DataFrame, pd.DataFrame, dict, dict]:
    """Execute VALIDATION split on selected coins and all eligible coins."""
    out_dir = Path("reports/research/tas")
    selected_json_path = out_dir / "tas_phase2_selected_coins.json"
    if not selected_json_path.exists():
        err = f"Selected coins file {selected_json_path} not found. Run TRAIN first."
        raise FileNotFoundError(err)

    with open(selected_json_path, encoding="utf-8") as f:
        data = json.load(f)
    selected_symbols = data["selected_symbols"]

    all_symbols = load_universe_symbols()
    vol_map = build_volume_map()

    print(f"[{get_timestamp_str()}] Running VALIDATION (2025-01-01 -> 2025-12-31)...")

    # 1. Selected coins validation
    print(f"[{get_timestamp_str()}] Validating {len(selected_symbols)} SELECTED coins...")
    val_selected_df, sel_pos, sel_tr = run_coin_scan(
        symbols=selected_symbols,
        vol_map=vol_map,
        start="2025-01-01",
        end="2025-12-31",
        split_name="val_selected",
    )
    pooled_selected = compute_pooled_metrics(sel_pos, sel_tr)

    # 2. All eligible coins validation (for comparison)
    print(
        f"[{get_timestamp_str()}] Validating ALL {len(all_symbols)} coins for comparison..."
    )
    val_all_df, all_pos, all_tr = run_coin_scan(
        symbols=all_symbols,
        vol_map=vol_map,
        start="2025-01-01",
        end="2025-12-31",
        split_name="val_all",
    )
    val_all_eligible_df = val_all_df[val_all_df["is_eligible"]].copy()
    eligible_pos = [p for p in all_pos if p["symbol"].iloc[0] in set(val_all_eligible_df["symbol"])]
    eligible_tr = [t for t in all_tr if t["symbol"].iloc[0] in set(val_all_eligible_df["symbol"])]
    pooled_all = compute_pooled_metrics(eligible_pos, eligible_tr)

    # Save to databases
    print(f"[{get_timestamp_str()}] Saving VALIDATION stats to DuckDB and SQLite...")
    save_to_databases(val_selected_df)
    save_to_databases(val_all_df)

    # Save CSVs
    val_selected_csv = out_dir / "tas_phase2_val_selected_results.csv"
    val_all_csv = out_dir / "tas_phase2_val_all_results.csv"
    val_selected_df.to_csv(val_selected_csv, index=False)
    val_all_df.to_csv(val_all_csv, index=False)

    return val_selected_df, val_all_eligible_df, pooled_selected, pooled_all


if __name__ == "__main__":
    import sys

    stage = sys.argv[1] if len(sys.argv) > 1 else "train"
    if stage == "train":
        run_phase2_train()
    elif stage == "val":
        run_phase2_validation()
    else:
        print(f"Unknown stage: {stage}")
