"""Resample 15m -> 30m and 1h -> 2h candles across all symbols in data/parquet."""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

PARQUET_DIR = Path("data/parquet")
TS = pa.timestamp("ms", tz="UTC")

KLINE_SCHEMA = pa.schema([
    ("open_time", TS),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("close_time", TS),
    ("quote_volume", pa.float64()),
    ("trades", pa.int64()),
    ("taker_buy_volume", pa.float64()),
    ("taker_buy_quote_volume", pa.float64()),
])

TF_MS = {
    "30m": 30 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
}


def resample_dataframe(
    df: pd.DataFrame,
    bucket_freq: str,
    expected_sub_candles: int,
) -> pd.DataFrame:
    """Resample DataFrame with UTC-aligned buckets, strictly dropping buckets with missing sub-candles."""
    if df.empty or "open_time" not in df.columns:
        return pd.DataFrame()

    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    df["bucket"] = df["open_time"].dt.floor(bucket_freq)
    grp = df.groupby("bucket")

    counts = grp["open"].count()
    valid_buckets = counts[counts == expected_sub_candles].index
    if len(valid_buckets) == 0:
        return pd.DataFrame()

    agg_dict = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "quote_volume": "sum",
    }
    for col in ("trades", "taker_buy_volume", "taker_buy_quote_volume"):
        if col in df.columns:
            agg_dict[col] = "sum"

    agg_df = grp.agg(agg_dict).loc[valid_buckets].reset_index().rename(columns={"bucket": "open_time"})
    return agg_df


def resample_and_store_tf(
    symbol: str,
    source_tf: str,
    target_tf: str,
    bucket_freq: str,
    expected_sub_candles: int,
) -> int:
    """Build target_tf from source_tf for a symbol, dropping buckets with missing sub-candles.

    Returns:
        Number of resampled candles written.
    """
    src_dir = PARQUET_DIR / symbol / "klines" / source_tf
    if not src_dir.exists():
        return 0

    part_files = sorted(src_dir.glob("**/part.parquet"))
    if not part_files:
        return 0

    try:
        tables = [pq.read_table(f) for f in part_files]
        combined = pa.concat_tables(tables)
        if len(combined) == 0:
            return 0
        df = combined.to_pandas()
    except Exception as e:
        print(f"[{symbol}] Error reading {source_tf}: {e}", file=sys.stderr)
        return 0

    if df.empty or "open_time" not in df.columns:
        return 0

    agg_df = resample_dataframe(df, bucket_freq, expected_sub_candles)
    if agg_df.empty:
        return 0

    target_dir = PARQUET_DIR / symbol / "klines" / target_tf
    target_dir.mkdir(parents=True, exist_ok=True)
    tf_ms = TF_MS[target_tf]

    # Write partitioned by year and month
    for (year, month), sub_df in agg_df.groupby([agg_df["open_time"].dt.year, agg_df["open_time"].dt.month]):
        sub_df = sub_df.copy()
        # sub_df['open_time'] is datetime64[ms, UTC] -> astype('int64') is already in ms
        open_time_ms = sub_df["open_time"].astype("int64")
        open_time_arr = pa.array(open_time_ms, pa.int64()).cast(TS)
        close_time_arr = pa.array(open_time_ms + tf_ms - 1, pa.int64()).cast(TS)

        t_out = pa.table({
            "open_time": open_time_arr,
            "open": pa.array(sub_df["open"], pa.float64()),
            "high": pa.array(sub_df["high"], pa.float64()),
            "low": pa.array(sub_df["low"], pa.float64()),
            "close": pa.array(sub_df["close"], pa.float64()),
            "volume": pa.array(sub_df["volume"], pa.float64()),
            "close_time": close_time_arr,
            "quote_volume": pa.array(sub_df["quote_volume"], pa.float64()),
            "trades": pa.array(sub_df["trades"].fillna(0).astype("int64"), pa.int64()),
            "taker_buy_volume": pa.array(sub_df["taker_buy_volume"], pa.float64()),
            "taker_buy_quote_volume": pa.array(sub_df["taker_buy_quote_volume"], pa.float64()),
        }, schema=KLINE_SCHEMA)

        part_path = target_dir / f"year={year:04d}" / f"month={month:02d}" / "part.parquet"
        part_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = part_path.with_suffix(".parquet.tmp")
        pq.write_table(t_out, tmp, compression="zstd")
        os.replace(tmp, part_path)

    return len(agg_df)


def process_symbol(symbol: str) -> tuple[str, int, int]:
    """Process both 30m and 2h for one symbol."""
    n_30m = resample_and_store_tf(symbol, "15m", "30m", "30min", 2)
    n_2h = resample_and_store_tf(symbol, "1h", "2h", "2h", 2)
    return symbol, n_30m, n_2h


def main():
    symbols = sorted([d.name for d in PARQUET_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")])
    print(f"Starting 30m and 2h resampling for {len(symbols)} symbols...")
    t0 = time.time()

    workers = max(1, (os.cpu_count() or 4) - 1)
    print(f"Using {workers} parallel processes (CPU cores - 1)")

    total_30m = 0
    total_2h = 0
    completed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_symbol, sym): sym for sym in symbols}
        for f in as_completed(futures):
            sym, n_30m, n_2h = f.result()
            total_30m += n_30m
            total_2h += n_2h
            completed += 1
            if completed % 50 == 0 or completed == len(symbols):
                elapsed = time.time() - t0
                print(f"[{completed}/{len(symbols)}] Processed {completed} symbols in {elapsed:.1f}s...")

    elapsed = time.time() - t0
    print(f"Finished in {elapsed:.1f}s. Total 30m candles: {total_30m:,}, Total 2h candles: {total_2h:,}")


if __name__ == "__main__":
    main()
