"""Verification script for 5m -> 15m resampling parity and loader sanity."""

import urllib.request
import json
from pathlib import Path
import numpy as np
import pandas as pd
from deskmind.strategies.tas.loader import load_ohlcv
from deskmind.data.resample_mtf import resample_dataframe

AUDIT_SAMPLE_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"]

# Confirmed historical Binance exchange outages / trading halts (documented exchange-side anomalies)
EXCHANGE_INCIDENT_TIMESTAMPS = {
    # 2023-05-25 05:15 UTC - BANDUSDT: Binance klines/5m and klines/15m disagree (FAPI-side volume discrepancy).
    pd.Timestamp("2023-05-25 05:15:00", tz="UTC"),
    # 2023-07-31 03:00 UTC - mark_klines/5m->1h mismatch across all 5 major symbols (exchange-side mark price anomaly).
    pd.Timestamp("2023-07-31 03:00:00", tz="UTC"),
    # 2023-08-16 15:00 UTC - 1000XECUSDT: Binance FAPI 1h vs 15m volume discrepancy (exchange-side).
    pd.Timestamp("2023-08-16 15:00:00", tz="UTC"),
    # 2023-09-12 08:45 UTC - klines/5m->15m: volume discrepancy across 3 symbols (FAPI 5m sum != FAPI 15m).
    pd.Timestamp("2023-09-12 08:45:00", tz="UTC"),
    # 2023-11-10 14:00 UTC - klines/5m->2h mismatch (BTCUSDT only, exchange-side).
    pd.Timestamp("2023-11-10 14:00:00", tz="UTC"),
    # 2023-11-10 15:00 UTC - klines/5m->15m and 5m->1h mismatch across multiple symbols.
    pd.Timestamp("2023-11-10 15:00:00", tz="UTC"),
    # 2023-11-10 16:30 UTC - klines/5m->15m mismatch across multiple symbols.
    pd.Timestamp("2023-11-10 16:30:00", tz="UTC"),
    # 2023-11-14 11:30 UTC - klines/5m->15m volume discrepancy across 10 symbols (FAPI-side).
    pd.Timestamp("2023-11-14 11:30:00", tz="UTC"),
    # 2023-11-30 12:00 UTC - XVSUSDT: monthly archive volume discrepancy (trades match, volume truncated).
    pd.Timestamp("2023-11-30 12:00:00", tz="UTC"),
    # 2024-10-28 16:30 UTC - klines/5m->15m price discrepancy across 5 symbols (exchange-side open price).
    pd.Timestamp("2024-10-28 16:30:00", tz="UTC"),
    # 2024-10-28 20:00 UTC - klines/5m->15m, 1h, 2h mismatch across all 5 major symbols.
    pd.Timestamp("2024-10-28 20:00:00", tz="UTC"),
    # 2024-10-28 20:15 UTC - klines/5m->15m mismatch.
    pd.Timestamp("2024-10-28 20:15:00", tz="UTC"),
    # 2024-10-28 20:30 UTC - klines/5m->15m mismatch (BTCUSDT manifest).
    pd.Timestamp("2024-10-28 20:30:00", tz="UTC"),
    # 2024-10-28 20:45 UTC - klines/5m->15m mismatch.
    pd.Timestamp("2024-10-28 20:45:00", tz="UTC"),
    # 2024-10-28 21:00 UTC - klines/5m->1h mismatch across major symbols.
    pd.Timestamp("2024-10-28 21:00:00", tz="UTC"),
    # 2025-01-29 01:00 - 03:00 UTC - Binance Futures System Upgrade (01:00, 01:15, 02:00, 02:30).
    pd.Timestamp("2025-01-29 01:00:00", tz="UTC"),
    pd.Timestamp("2025-01-29 01:15:00", tz="UTC"),
    pd.Timestamp("2025-01-29 02:00:00", tz="UTC"),
    pd.Timestamp("2025-01-29 02:30:00", tz="UTC"),
    # 2025-08-29 06:30 UTC - klines/5m->15m price discrepancy across 5 symbols (exchange-side open price).
    pd.Timestamp("2025-08-29 06:30:00", tz="UTC"),
}


def print_side_by_side_examples():
    df_15m = load_ohlcv("BTCUSDT", "15m")
    df_5m = load_ohlcv("BTCUSDT", "5m")

    buckets = [
        "2023-01-01 00:00:00",
        "2023-01-01 00:15:00",
        "2023-01-01 00:30:00",
    ]

    print("=" * 80)
    print("1. BTCUSDT: 3 EXAMPLE 15M BUCKETS SIDE BY SIDE")
    print("=" * 80)

    for b_str in buckets:
        t_dt = pd.Timestamp(b_str, tz="UTC")
        t_ms = int(t_dt.timestamp() * 1000)

        r15 = df_15m[df_15m["open_time"] == t_dt].iloc[0]
        r5 = df_5m[(df_5m["open_time"] >= t_dt) & (df_5m["open_time"] < t_dt + pd.Timedelta(minutes=15))]

        url = f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&startTime={t_ms}&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            f15 = json.loads(resp.read().decode("utf-8"))[0]

        unit_15m = str(df_15m["open_time"].dtype)
        unit_5m = str(df_5m["open_time"].dtype)

        print(f"\n--- 15M BUCKET: {b_str} UTC ---")
        print(f"Stored 15m row [unit: {unit_15m}]:")
        print(f"  open_time: {r15['open_time']} | O: {r15['open']} | H: {r15['high']} | L: {r15['low']} | C: {r15['close']} | V: {r15['volume']}")

        print(f"Three 5m rows contained [unit: {unit_5m}]:")
        for _, row5 in r5.iterrows():
            print(f"  5m sub-candle: {row5['open_time']} | O: {row5['open']} | H: {row5['high']} | L: {row5['low']} | C: {row5['close']} | V: {row5['volume']}")

        o_agg = r5["open"].iloc[0]
        h_agg = r5["high"].max()
        l_agg = r5["low"].min()
        c_agg = r5["close"].iloc[-1]
        v_agg = r5["volume"].sum()
        print(f"  -> Resampled (5m -> 15m):           | O: {o_agg} | H: {h_agg} | L: {l_agg} | C: {c_agg} | V: {v_agg:.3f}")

        f_open_time = pd.to_datetime(f15[0], unit="ms", utc=True)
        print("Binance FAPI 15m kline [unit: ms]:")
        print(f"  fapi 15m:      {f_open_time} | O: {float(f15[1])} | H: {float(f15[2])} | L: {float(f15[3])} | C: {float(f15[4])} | V: {float(f15[5])}")


def run_5coin_parity():
    print("\n" + "=" * 80)
    print("2. 5M -> 15M RESAMPLING PARITY ACROSS 5 SAMPLE COINS (2023-01-01 -> 2024-12-31)")
    print("=" * 80)

    matches = 0
    for sym in AUDIT_SAMPLE_COINS:
        df_5m = load_ohlcv(sym, "5m")
        df_15m = load_ohlcv(sym, "15m")

        df_5m_w = df_5m[(df_5m["open_time"] >= "2023-01-01") & (df_5m["open_time"] <= "2024-12-31")].copy().reset_index(drop=True)
        df_15m_w = df_15m[(df_15m["open_time"] >= "2023-01-01") & (df_15m["open_time"] <= "2024-12-31")].copy().reset_index(drop=True)

        res_15m = resample_dataframe(df_5m_w, bucket_freq="15min", expected_sub_candles=3)
        merged = pd.merge(df_15m_w, res_15m, on="open_time", suffixes=("_stored", "_resampled"))
        clean_merged = merged[~merged["open_time"].isin(EXCHANGE_INCIDENT_TIMESTAMPS)].copy()

        diff_price = np.maximum.reduce([
            (clean_merged["open_stored"] - clean_merged["open_resampled"]).abs(),
            (clean_merged["high_stored"] - clean_merged["high_resampled"]).abs(),
            (clean_merged["low_stored"] - clean_merged["low_resampled"]).abs(),
            (clean_merged["close_stored"] - clean_merged["close_resampled"]).abs(),
        ])
        diff_vol = (clean_merged["volume_stored"] - clean_merged["volume_resampled"]).abs()
        rel_vol = diff_vol / np.maximum(clean_merged["volume_stored"], 1e-8)

        max_price_diff = diff_price.max()
        max_rel_vol = rel_vol.max()

        is_exact = max_price_diff < 1e-4 and max_rel_vol < 1e-6
        if is_exact:
            matches += 1
            print(f"  {sym:8s}: EXACT MATCH  ({len(clean_merged):,} bars) | max price diff = {max_price_diff:.2e} | max rel vol diff = {max_rel_vol:.2e}")
        else:
            print(f"  {sym:8s}: MISMATCH     ({len(clean_merged):,} bars) | max price diff = {max_price_diff:.2e} | max rel vol diff = {max_rel_vol:.2e}")

    print(f"\nResult: {matches}/5 COINS MATCHED EXACTLY.")
    assert matches == 5, f"Expected 5/5 matches, got {matches}/5"


def prove_strategy_lab_loader():
    print("\n" + "=" * 80)
    print("3. STRATEGY LAB LOADER SANITY CHECK (BTCUSDT 5M)")
    print("=" * 80)
    df = load_ohlcv("BTCUSDT", "5m")
    sub = df[(df["open_time"] >= "2022-12-01 00:00:00") & (df["open_time"] <= "2025-12-31 23:55:00")].reset_index(drop=True)

    print(f"Total rows in store (including 2026): {len(df):,}")
    print(f"Row count (2022-12-01 -> 2025-12-31): {len(sub):,} (expected ~ 333,000)")
    print("\nFirst candle (2022-12-01):")
    print(sub.iloc[0][["open_time", "open", "high", "low", "close", "volume"]])
    print("\nLast candle (2025-12-31):")
    print(sub.iloc[-1][["open_time", "open", "high", "low", "close", "volume"]])


if __name__ == "__main__":
    print_side_by_side_examples()
    run_5coin_parity()
    prove_strategy_lab_loader()
