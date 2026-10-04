"""Audit 5m cache files and compute missing data estimate for 2022-12-01 -> 2025-12-31."""

import json
import random
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path("data/cache")
REPORTS_DIR = Path("reports/research")
UNIVERSE_PATH = REPORTS_DIR / "universe_2022_2025.json"

TARGET_START = pd.Timestamp("2022-12-01 00:00:00", tz="UTC")
TARGET_END = pd.Timestamp("2025-12-31 23:55:00", tz="UTC")
CANDLE_5M_MS = 5 * 60 * 1000

AUDIT_SAMPLE_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT"]


def fetch_fapi_5m_kline(symbol: str, open_time_ms: int) -> dict:
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=5m&startTime={open_time_ms}&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data:
                    raise ValueError(f"No kline returned for {symbol} 5m at {open_time_ms}")
                row = data[0]
                return {
                    "open_time_ms": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.0)


def main():
    print("=" * 70, flush=True)
    print("STEP 1b: AUDIT 5M CACHE AND COMPUTE MISSING DATA", flush=True)
    print("=" * 70, flush=True)

    with open(UNIVERSE_PATH, "r", encoding="utf-8") as f:
        universe = json.load(f)
    print(f"Total universe symbols: {len(universe)}", flush=True)

    cache_files = {p.stem.replace("ohlcv_", "").replace("_USDT_USDT_5m", "USDT"): p for p in CACHE_DIR.glob("*_5m.csv")}
    # Also handle some name variants if any
    print(f"5m files found in cache: {len(cache_files)}", flush=True)

    # 1. Audit Cache Files
    total_candles_in_cache = 0
    total_duplicates = 0
    total_ohlc_invalid = 0
    total_flat_zero_vol = 0
    total_internal_gaps = 0
    total_internal_gap_candles = 0

    print("Running audit across 5m cache files...", flush=True)
    symbol_cache_meta = {}

    for i, (sym, cpath) in enumerate(cache_files.items(), 1):
        if i % 100 == 0 or i == len(cache_files):
            print(f"  Audited {i}/{len(cache_files)} files...", flush=True)

        try:
            df = pd.read_csv(cpath)
        except Exception as e:
            print(f"Error reading {cpath}: {e}", flush=True)
            continue

        if df.empty or "open_time" not in df.columns:
            continue

        total_candles_in_cache += len(df)
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)

        # Check duplicates
        dups = df["open_time"].duplicated().sum()
        total_duplicates += dups
        if dups > 0:
            df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
        else:
            df = df.sort_values("open_time").reset_index(drop=True)

        # Check OHLC sanity
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        v = df["volume"].to_numpy(float)

        invalid_mask = (
            (h < l)
            | (h < o)
            | (h < c)
            | (l > o)
            | (l > c)
            | (o <= 0)
            | (c <= 0)
            | (np.isnan(o))
            | (np.isnan(h))
            | (np.isnan(l))
            | (np.isnan(c))
        )
        total_ohlc_invalid += int(invalid_mask.sum())

        # Check flat zero-volume candles
        flat_zero = ((o == h) & (h == l) & (l == c) & (v == 0)).sum()
        total_flat_zero_vol += int(flat_zero)

        # Check internal gaps within the file span
        t_ms = df["open_time"].astype("int64") // 1_000_000
        diffs = np.diff(t_ms)
        gaps = diffs > CANDLE_5M_MS
        gap_count = int(gaps.sum())
        missing_in_gaps = int(((diffs[gaps] // CANDLE_5M_MS) - 1).sum()) if gap_count > 0 else 0

        total_internal_gaps += gap_count
        total_internal_gap_candles += missing_in_gaps

        symbol_cache_meta[sym] = {
            "start": df["open_time"].iloc[0],
            "end": df["open_time"].iloc[-1],
            "candles": len(df),
            "missing_in_gaps": missing_in_gaps,
            "df": df if sym in AUDIT_SAMPLE_COINS else None,
        }

    print("\n--- CACHE AUDIT RESULTS ---", flush=True)
    print(f"Total cache files:              {len(cache_files)}", flush=True)
    print(f"Total candles in cache:          {total_candles_in_cache:,}", flush=True)
    print(f"Timestamp duplicates:            {total_duplicates:,}", flush=True)
    print(f"OHLC sanity violations:          {total_ohlc_invalid:,}", flush=True)
    print(f"Flat zero-volume candles:        {total_flat_zero_vol:,}", flush=True)
    print(f"Internal gaps detected:          {total_internal_gaps:,} (totaling {total_internal_gap_candles:,} missing candles)", flush=True)

    # 2. Fapi check on 5 coins
    print("\n--- BINANCE FAPI VERIFICATION ON 5 SAMPLE COINS (10 candles each) ---", flush=True)
    fapi_matches = 0
    fapi_total = 0
    rng = random.Random(42)

    for sym in AUDIT_SAMPLE_COINS:
        meta = symbol_cache_meta.get(sym)
        if not meta or meta["df"] is None:
            print(f"  {sym}: Not in cache or empty", flush=True)
            continue
        df = meta["df"]
        sample_indices = rng.sample(range(50, len(df) - 50), 10)
        print(f"  Verifying {sym} (10 random 5m candles): ", end="", flush=True)

        sym_matches = 0
        for s_idx in sample_indices:
            row = df.iloc[s_idx]
            t_ms = int(row["open_time"].timestamp() * 1000)
            remote = fetch_fapi_5m_kline(sym, t_ms)
            max_diff = max(
                abs(row["open"] - remote["open"]),
                abs(row["high"] - remote["high"]),
                abs(row["low"] - remote["low"]),
                abs(row["close"] - remote["close"]),
            )
            if max_diff < 1e-4:
                sym_matches += 1
                fapi_matches += 1
            print(".", end="", flush=True)
            time.sleep(0.02)
            fapi_total += 1

        print(f" {sym_matches}/10 MATCHED", flush=True)

    print(f"FAPI Verification Summary: {fapi_matches}/{fapi_total} candles matched exactly.", flush=True)

    # 3. Missing Data Calculation for 2022-12-01 -> 2025-12-31
    print("\n--- COMPUTING MISSING DATA FOR RANGE 2022-12-01 -> 2025-12-31 ---", flush=True)
    coins_not_in_cache = []
    coins_partial_in_cache = []
    coins_fully_covered = []

    total_missing_candles = 0

    for item in universe:
        sym = item["symbol"]
        onboard_ms = item.get("onboard_date_ms")
        onboard_ts = (
            pd.Timestamp(onboard_ms, unit="ms", tz="UTC")
            if onboard_ms
            else TARGET_START
        )
        eff_start = max(TARGET_START, onboard_ts)
        eff_end = TARGET_END

        if eff_start >= eff_end:
            # Coin was onboarded after 2025-12-31
            continue

        expected_total_candles = int((eff_end - eff_start).total_seconds() // 300) + 1

        if sym not in symbol_cache_meta:
            # Entire history is missing
            coins_not_in_cache.append({
                "symbol": sym,
                "missing_range": f"{eff_start.strftime('%Y-%m-%d')} -> {eff_end.strftime('%Y-%m-%d')}",
                "missing_candles": expected_total_candles,
            })
            total_missing_candles += expected_total_candles
        else:
            meta = symbol_cache_meta[sym]
            c_start = meta["start"]
            c_end = meta["end"]

            missing_candles = 0
            missing_ranges = []

            # Missing prefix
            if c_start > eff_start:
                prefix_candles = int((c_start - eff_start).total_seconds() // 300)
                missing_candles += prefix_candles
                missing_ranges.append(f"prefix {eff_start.strftime('%Y-%m-%d')} -> {c_start.strftime('%Y-%m-%d')} ({prefix_candles:,})")

            # Missing suffix
            if c_end < eff_end:
                suffix_candles = int((eff_end - c_end).total_seconds() // 300)
                missing_candles += suffix_candles
                missing_ranges.append(f"suffix {c_end.strftime('%Y-%m-%d')} -> {eff_end.strftime('%Y-%m-%d')} ({suffix_candles:,})")

            # Missing internal gaps
            missing_candles += meta["missing_in_gaps"]
            if meta["missing_in_gaps"] > 0:
                missing_ranges.append(f"internal gaps ({meta['missing_in_gaps']:,})")

            if missing_candles > 0:
                coins_partial_in_cache.append({
                    "symbol": sym,
                    "missing_candles": missing_candles,
                    "details": ", ".join(missing_ranges),
                })
                total_missing_candles += missing_candles
            else:
                coins_fully_covered.append(sym)

    # Estimates
    # Parquet zstd storage size: ~18 bytes per row
    # Uncompressed CSV: ~60 bytes per row
    est_parquet_bytes = total_missing_candles * 18
    est_parquet_mb = est_parquet_bytes / (1024 * 1024)
    est_parquet_gb = est_parquet_bytes / (1024 * 1024 * 1024)

    # Download time:
    # Monthly vision archives contain ~8,640 5m candles per month (~100 KB zip).
    # Using 8-10 parallel threads, downloading Vision zips typically achieves ~10-20 MB/s or ~50-100 months/sec.
    # Total months missing roughly: total_missing_candles / 8640 ≈ number of symbol-months
    est_symbol_months = total_missing_candles / 8640
    # Average ~0.2s per monthly zip download
    est_seconds = (est_symbol_months / 8.0) * 0.25
    est_minutes = est_seconds / 60.0

    print(f"Coins fully covered in cache:    {len(coins_fully_covered)}", flush=True)
    print(f"Coins partially covered:        {len(coins_partial_in_cache)}", flush=True)
    print(f"Coins completely missing:       {len(coins_not_in_cache)}", flush=True)
    print("-" * 70, flush=True)
    print(f"TOTAL MISSING 5M CANDLES:       {total_missing_candles:,}", flush=True)
    print(f"ESTIMATED PARQUET DISK SIZE:    {est_parquet_mb:.1f} MB ({est_parquet_gb:.2f} GB)", flush=True)
    print(f"ESTIMATED DOWNLOAD TIME:        ~{est_minutes:.1f} minutes ({est_seconds:.0f}s using Vision zips)", flush=True)
    print("=" * 70, flush=True)
    print("STATUS: STOPPING AND WAITING FOR USER OK BEFORE DOWNLOADING ANYTHING.", flush=True)


if __name__ == "__main__":
    main()
