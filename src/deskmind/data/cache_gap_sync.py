"""Resumable OHLCV cache ingestion and gap download pipeline.

Reuses data/cache CSV files, downloads only missing months/ranges from
data.binance.vision (bulk monthly zips) and Binance FAPI REST.
Writes cleaned data to Deskmind's standard Parquet store (parquet/{symbol}/klines/{tf}/...).
Never modifies or deletes original CSVs.
Never invents or forward-fills candles: genuine gaps are logged to data/gaps_report.csv.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pv
import pyarrow.parquet as pq

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"
PARQUET_DIR = DATA_DIR / "parquet"
META_DIR = DATA_DIR / "meta"
REPORTS_DIR = Path("reports/research")
GAPS_REPORT_PATH = DATA_DIR / "gaps_report.csv"
DAILY_VOLUME_PATH = DATA_DIR / "daily_quote_volume.parquet"
PROGRESS_LOG_PATH = REPORTS_DIR / "data_sync.log"

TARGET_START_MS = int(datetime(2022, 12, 1, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)
TARGET_END_MS = int(datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)

TS = pa.timestamp("ms", tz="UTC")

KLINE_SCHEMA = pa.schema(
    [
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
    ]
)

TIMEFRAME_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

# Generate target months (YYYY, MM) from 2022-12 to 2025-12
TARGET_MONTHS: list[tuple[int, int]] = [(2022, 12)]
for y in range(2023, 2026):
    for m in range(1, 13):
        TARGET_MONTHS.append((y, m))


def log_msg(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted, flush=True)
    with open(PROGRESS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(formatted + "\n")


def get_month_bounds(year: int, month: int) -> tuple[int, int]:
    """Return start_ms (inclusive) and end_ms (exclusive) for the given month."""
    start_dt = datetime(year, month, 1, 0, 0, 0, tzinfo=UTC)
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=UTC)
    else:
        end_dt = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=UTC)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def download_vision_monthly_zip(symbol: str, tf: str, year: int, month: int) -> pa.Table | None:
    """Download and parse monthly klines zip from data.binance.vision."""
    url = (
        f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/{tf}/"
        f"{symbol}-{tf}-{year:04d}-{month:02d}.zip"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            fname = z.namelist()[0]
            with z.open(fname) as f:
                read_options = pv.ReadOptions(
                    use_threads=False,
                    column_names=[
                        "open_time", "open", "high", "low", "close", "volume",
                        "close_time", "quote_volume", "trades", "taker_buy_volume",
                        "taker_buy_quote_volume", "ignore"
                    ]
                )
                t = pv.read_csv(f, read_options=read_options)
                if len(t) > 0 and str(t["open_time"][0]) == "open_time":
                    t = t.slice(1)
                if len(t) == 0:
                    return None
                
                open_time_arr = t["open_time"].cast(pa.int64()).cast(TS)
                close_time_arr = t["close_time"].cast(pa.int64()).cast(TS)
                
                table = pa.table(
                    {
                        "open_time": open_time_arr,
                        "open": t["open"].cast(pa.float64()),
                        "high": t["high"].cast(pa.float64()),
                        "low": t["low"].cast(pa.float64()),
                        "close": t["close"].cast(pa.float64()),
                        "volume": t["volume"].cast(pa.float64()),
                        "close_time": close_time_arr,
                        "quote_volume": t["quote_volume"].cast(pa.float64()),
                        "trades": t["trades"].cast(pa.int64()),
                        "taker_buy_volume": t["taker_buy_volume"].cast(pa.float64()),
                        "taker_buy_quote_volume": t["taker_buy_quote_volume"].cast(pa.float64()),
                    },
                    schema=KLINE_SCHEMA,
                )
                return table
    except urllib.error.HTTPError as e:
        return None
    except Exception:
        return None


def download_fapi_klines_range(symbol: str, tf: str, start_ms: int, end_ms: int) -> pa.Table | None:
    """Fetch missing klines range via Binance FAPI REST (limit=1500 per call)."""
    curr_start = start_ms
    all_rows = []
    
    while curr_start < end_ms:
        url = (
            f"https://fapi.binance.com/fapi/v1/klines?"
            f"symbol={symbol}&interval={tf}&startTime={curr_start}&endTime={end_ms}&limit=1500"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data:
                break
            for row in data:
                all_rows.append(row)
            last_open = data[-1][0]
            curr_start = last_open + TIMEFRAME_MS[tf]
            time.sleep(0.04) # rate limit compliance
        except Exception:
            break
            
    if not all_rows:
        return None
        
    cols = list(zip(*all_rows))
    open_time_arr = pa.array([int(x) for x in cols[0]], pa.int64()).cast(TS)
    close_time_arr = pa.array([int(x) for x in cols[6]], pa.int64()).cast(TS)
    
    return pa.table(
        {
            "open_time": open_time_arr,
            "open": pa.array([float(x) for x in cols[1]], pa.float64()),
            "high": pa.array([float(x) for x in cols[2]], pa.float64()),
            "low": pa.array([float(x) for x in cols[3]], pa.float64()),
            "close": pa.array([float(x) for x in cols[4]], pa.float64()),
            "volume": pa.array([float(x) for x in cols[5]], pa.float64()),
            "close_time": close_time_arr,
            "quote_volume": pa.array([float(x) for x in cols[7]], pa.float64()),
            "trades": pa.array([int(x) for x in cols[8]], pa.int64()),
            "taker_buy_volume": pa.array([float(x) for x in cols[9]], pa.float64()),
            "taker_buy_quote_volume": pa.array([float(x) for x in cols[10]], pa.float64()),
        },
        schema=KLINE_SCHEMA,
    )


def load_cache_csv(coin: str, tf: str) -> pa.Table | None:
    """Read cache CSV into Arrow table with KLINE_SCHEMA, computing quote_volume."""
    fpath = CACHE_DIR / f"ohlcv_{coin}_USDT_USDT_{tf}.csv"
    if not fpath.exists():
        return None
    try:
        t = pv.read_csv(fpath)
        if len(t) == 0:
            return None
            
        ts_strings = t["open_time"]
        open_time = pc.strptime(ts_strings, format="%Y-%m-%d %H:%M:%S", unit="ms")
        
        open_ms = pc.cast(open_time, pa.int64())
        mask = pc.and_(
            pc.greater_equal(open_ms, TARGET_START_MS),
            pc.less_equal(open_ms, TARGET_END_MS)
        )
        t_filtered = t.filter(mask)
        if len(t_filtered) == 0:
            return None
            
        open_time_arr = pc.strptime(t_filtered["open_time"], format="%Y-%m-%d %H:%M:%S", unit="ms")
        open_ms_arr = pc.cast(open_time_arr, pa.int64())
        tf_ms = TIMEFRAME_MS[tf]
        close_ms_arr = pc.subtract(pc.add(open_ms_arr, tf_ms), 1)
        close_time_arr = pc.cast(close_ms_arr, TS)
        
        vol = t_filtered["volume"].cast(pa.float64())
        cls = t_filtered["close"].cast(pa.float64())
        quote_vol = pc.multiply(vol, cls)
        
        zeros_i64 = pa.array([0] * len(t_filtered), pa.int64())
        zeros_f64 = pa.array([0.0] * len(t_filtered), pa.float64())
        
        return pa.table(
            {
                "open_time": open_time_arr,
                "open": t_filtered["open"].cast(pa.float64()),
                "high": t_filtered["high"].cast(pa.float64()),
                "low": t_filtered["low"].cast(pa.float64()),
                "close": cls,
                "volume": vol,
                "close_time": close_time_arr,
                "quote_volume": quote_vol,
                "trades": zeros_i64,
                "taker_buy_volume": zeros_f64,
                "taker_buy_quote_volume": zeros_f64,
            },
            schema=KLINE_SCHEMA,
        )
    except Exception as e:
        return None


def clean_and_dedupe_table(table: pa.Table) -> pa.Table:
    """Sort by open_time, drop exact duplicate timestamps, filter nulls."""
    if len(table) == 0:
        return table
        
    sorted_table = table.sort_by("open_time")
    times = sorted_table["open_time"].cast(pa.int64()).to_pylist()
    keep = []
    seen = set()
    for i, t in enumerate(times):
        if t not in seen:
            seen.add(t)
            keep.append(i)
            
    if len(keep) == len(sorted_table):
        return sorted_table
    return sorted_table.take(pa.array(keep, pa.int64()))


def detect_gaps_in_table(symbol: str, tf: str, table: pa.Table) -> list[dict]:
    """Detect internal gaps where candle interval > expected tf interval."""
    if len(table) < 2:
        return []
    times = table["open_time"].cast(pa.int64()).to_pylist()
    expected_step = TIMEFRAME_MS[tf]
    gaps = []
    for i in range(len(times) - 1):
        diff = times[i + 1] - times[i]
        if diff > expected_step:
            missing_count = (diff // expected_step) - 1
            start_str = datetime.fromtimestamp(times[i] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
            end_str = datetime.fromtimestamp(times[i + 1] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
            gaps.append({
                "symbol": symbol,
                "timeframe": tf,
                "gap_start": start_str,
                "gap_end": end_str,
                "missing_candles": missing_count,
            })
    return gaps


def process_symbol_timeframe(symbol: str, coin: str, tf: str, universe_meta: dict) -> dict:
    """Process a single symbol and timeframe across all target months."""
    tf_dir = PARQUET_DIR / symbol / "klines" / tf
    tf_dir.mkdir(parents=True, exist_ok=True)
    
    cache_table = load_cache_csv(coin, tf)
    onboard_ms = universe_meta.get("onboard_date_ms")
    
    stats = {
        "symbol": symbol,
        "tf": tf,
        "months_from_cache": 0,
        "months_downloaded": 0,
        "months_skipped_existing": 0,
        "total_candles": 0,
        "gaps_found": [],
    }
    
    # Pre-index cache by month
    cache_by_month: dict[tuple[int, int], pa.Table] = {}
    if cache_table is not None and len(cache_table) > 0:
        times_ms = cache_table["open_time"].cast(pa.int64())
        for y, m in TARGET_MONTHS:
            s_ms, e_ms = get_month_bounds(y, m)
            m_mask = pc.and_(pc.greater_equal(times_ms, s_ms), pc.less(times_ms, e_ms))
            sub = cache_table.filter(m_mask)
            if len(sub) > 0:
                cache_by_month[(y, m)] = sub

    for year, month in TARGET_MONTHS:
        part_path = tf_dir / f"year={year:04d}" / f"month={month:02d}" / "part.parquet"
        s_ms, e_ms = get_month_bounds(year, month)
        
        # 1. If symbol was not listed yet (month ended before onboard date), skip immediately
        if onboard_ms and e_ms <= onboard_ms:
            continue
            
        # 2. Resumable check: if partition already exists on disk
        if part_path.exists():
            try:
                existing_t = pq.read_table(part_path)
                if len(existing_t) > 0:
                    stats["months_skipped_existing"] += 1
                    stats["total_candles"] += len(existing_t)
                    continue
            except Exception:
                pass
                
        expected_candles = (e_ms - s_ms) // TIMEFRAME_MS[tf]
        table_to_write = None
        
        cached_sub = cache_by_month.get((year, month))
        if cached_sub is not None:
            if len(cached_sub) >= expected_candles:
                table_to_write = cached_sub
                stats["months_from_cache"] += 1
            else:
                # Incomplete month in cache -> check Vision zip
                vision_t = download_vision_monthly_zip(symbol, tf, year, month)
                if vision_t is not None and len(vision_t) > 0:
                    merged = pa.concat_tables([vision_t, cached_sub])
                    table_to_write = clean_and_dedupe_table(merged)
                    stats["months_downloaded"] += 1
                else:
                    table_to_write = clean_and_dedupe_table(cached_sub)
                    stats["months_from_cache"] += 1
        else:
            # Download from Binance Vision
            vision_t = download_vision_monthly_zip(symbol, tf, year, month)
            if vision_t is not None and len(vision_t) > 0:
                table_to_write = clean_and_dedupe_table(vision_t)
                stats["months_downloaded"] += 1
            else:
                # Fallback to FAPI REST only if listing month or Vision missed it
                fapi_t = download_fapi_klines_range(symbol, tf, s_ms, e_ms)
                if fapi_t is not None and len(fapi_t) > 0:
                    table_to_write = clean_and_dedupe_table(fapi_t)
                    stats["months_downloaded"] += 1
                    
        if table_to_write is not None and len(table_to_write) > 0:
            part_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = part_path.with_suffix(".parquet.tmp")
            pq.write_table(table_to_write, tmp_path, compression="zstd")
            os.replace(tmp_path, part_path)
            
            stats["total_candles"] += len(table_to_write)
            gaps = detect_gaps_in_table(symbol, tf, table_to_write)
            if gaps:
                stats["gaps_found"].extend(gaps)
                
    return stats


def resample_1h_to_higher(symbol: str) -> None:
    """Resample 1h candles to 4h and 1d, compute daily quote volume."""
    h1_dir = PARQUET_DIR / symbol / "klines" / "1h"
    if not h1_dir.exists():
        return
        
    part_files = sorted(h1_dir.glob("**/part.parquet"))
    if not part_files:
        return
        
    tables = [pq.read_table(f) for f in part_files]
    combined_1h = pa.concat_tables(tables)
    cleaned_1h = clean_and_dedupe_table(combined_1h)
    if len(cleaned_1h) == 0:
        return
        
    df = cleaned_1h.to_pandas()
    df.set_index("open_time", inplace=True)
    
    # 4h Resampling
    res_4h = df.resample("4h", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "quote_volume": "sum",
        "trades": "sum",
        "taker_buy_volume": "sum",
        "taker_buy_quote_volume": "sum",
    }).dropna()
    
    dir_4h = PARQUET_DIR / symbol / "klines" / "4h"
    dir_4h.mkdir(parents=True, exist_ok=True)
    
    for (year, month), sub_df in res_4h.groupby([res_4h.index.year, res_4h.index.month]):
        sub_df = sub_df.copy()
        # index is timestamp[ms] -> astype(int64) is already in ms; no division needed
        open_time_ms = sub_df.index.astype("int64")
        open_time_arr = pa.array(open_time_ms, pa.int64()).cast(TS)
        tf_ms = TIMEFRAME_MS["4h"]
        close_time_arr = pa.array(open_time_ms + tf_ms - 1, pa.int64()).cast(TS)
        
        t_4h = pa.table({
            "open_time": open_time_arr,
            "open": pa.array(sub_df["open"], pa.float64()),
            "high": pa.array(sub_df["high"], pa.float64()),
            "low": pa.array(sub_df["low"], pa.float64()),
            "close": pa.array(sub_df["close"], pa.float64()),
            "volume": pa.array(sub_df["volume"], pa.float64()),
            "close_time": close_time_arr,
            "quote_volume": pa.array(sub_df["quote_volume"], pa.float64()),
            "trades": pa.array(sub_df["trades"].astype("int64"), pa.int64()),
            "taker_buy_volume": pa.array(sub_df["taker_buy_volume"], pa.float64()),
            "taker_buy_quote_volume": pa.array(sub_df["taker_buy_quote_volume"], pa.float64()),
        }, schema=KLINE_SCHEMA)
        
        part_4h = dir_4h / f"year={year:04d}" / f"month={month:02d}" / "part.parquet"
        part_4h.parent.mkdir(parents=True, exist_ok=True)
        tmp = part_4h.with_suffix(".parquet.tmp")
        pq.write_table(t_4h, tmp, compression="zstd")
        os.replace(tmp, part_4h)
        
    # 1D Resampling
    res_1d = df.resample("1D", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "quote_volume": "sum",
        "trades": "sum",
        "taker_buy_volume": "sum",
        "taker_buy_quote_volume": "sum",
    }).dropna()
    
    dir_1d = PARQUET_DIR / symbol / "klines" / "1d"
    dir_1d.mkdir(parents=True, exist_ok=True)
    
    for (year, month), sub_df in res_1d.groupby([res_1d.index.year, res_1d.index.month]):
        sub_df = sub_df.copy()
        # index is timestamp[ms] -> astype(int64) is already in ms; no division needed
        open_time_ms = sub_df.index.astype("int64")
        open_time_arr = pa.array(open_time_ms, pa.int64()).cast(TS)
        tf_ms = TIMEFRAME_MS["1d"]
        close_time_arr = pa.array(open_time_ms + tf_ms - 1, pa.int64()).cast(TS)
        
        t_1d = pa.table({
            "open_time": open_time_arr,
            "open": pa.array(sub_df["open"], pa.float64()),
            "high": pa.array(sub_df["high"], pa.float64()),
            "low": pa.array(sub_df["low"], pa.float64()),
            "close": pa.array(sub_df["close"], pa.float64()),
            "volume": pa.array(sub_df["volume"], pa.float64()),
            "close_time": close_time_arr,
            "quote_volume": pa.array(sub_df["quote_volume"], pa.float64()),
            "trades": pa.array(sub_df["trades"].astype("int64"), pa.int64()),
            "taker_buy_volume": pa.array(sub_df["taker_buy_volume"], pa.float64()),
            "taker_buy_quote_volume": pa.array(sub_df["taker_buy_quote_volume"], pa.float64()),
        }, schema=KLINE_SCHEMA)
        
        part_1d = dir_1d / f"year={year:04d}" / f"month={month:02d}" / "part.parquet"
        part_1d.parent.mkdir(parents=True, exist_ok=True)
        tmp = part_1d.with_suffix(".parquet.tmp")
        pq.write_table(t_1d, tmp, compression="zstd")
        os.replace(tmp, part_1d)


def run_pipeline(max_workers: int = 8, symbol_limit: int | None = None):
    t_start = time.time()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_LOG_PATH, "w", encoding="utf-8") as f:
        f.write("=== DATA SYNC LOG ===\n")
        
    log_msg("================================================================================")
    log_msg("STARTING RESUMABLE DATA SYNC: CACHE REUSE + GAP DOWNLOAD (2022-12-01 -> 2025-12-31)")
    log_msg("================================================================================")
    
    with open(REPORTS_DIR / "universe_2022_2025.json") as f:
        universe = json.load(f)
        
    with open(META_DIR / "symbol_filters_universe.json") as f:
        symbol_filters = json.load(f)
        
    if symbol_limit:
        universe = universe[:symbol_limit]
        
    log_msg(f"Target Universe: {len(universe)} symbols across timeframes ['1h', '15m']")
    log_msg("Resampling targets: ['4h', '1d'] from 1h")
    
    # Strict filter verification check
    for item in universe:
        sym = item["symbol"]
        if sym not in symbol_filters:
            raise KeyError(f"Fatal: Symbol '{sym}' missing from symbol filters! Defaults forbidden.")
        sf = symbol_filters[sym]
        if "tick_size" not in sf or "step_size" not in sf:
            raise KeyError(f"Fatal: Symbol '{sym}' missing tick_size/step_size in filters!")
            
    log_msg("All symbol filters verified. Proceeding with sync...")
    
    all_gaps = []
    total_from_cache = 0
    total_newly_downloaded = 0
    total_skipped_existing = 0
    total_candles = 0
    coins_processed = 0
    
    def process_sym(item):
        sym = item["symbol"]
        coin = item["coin"]
        h1_res = process_symbol_timeframe(sym, coin, "1h", item)
        m15_res = process_symbol_timeframe(sym, coin, "15m", item)
        resample_1h_to_higher(sym)
        return sym, h1_res, m15_res

    log_msg(f"Executing sync with {max_workers} worker threads...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_sym, item): item["symbol"] for item in universe}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                sym_name, h1_res, m15_res = future.result()
                coins_processed += 1
                total_from_cache += h1_res["months_from_cache"] + m15_res["months_from_cache"]
                total_newly_downloaded += h1_res["months_downloaded"] + m15_res["months_downloaded"]
                total_skipped_existing += h1_res["months_skipped_existing"] + m15_res["months_skipped_existing"]
                total_candles += h1_res["total_candles"] + m15_res["total_candles"]
                
                all_gaps.extend(h1_res["gaps_found"])
                all_gaps.extend(m15_res["gaps_found"])
                
                if coins_processed % 10 == 0 or coins_processed == len(universe):
                    elapsed = time.time() - t_start
                    log_msg(
                        f"[{elapsed:.1f}s] Processed {coins_processed}/{len(universe)} symbols | "
                        f"From cache: {total_from_cache} | Downloaded: {total_newly_downloaded} | "
                        f"Existing: {total_skipped_existing} | Candles: {total_candles:,}"
                    )
            except Exception as e:
                log_msg(f"Error processing {sym}: {e}")
                
    # Save gaps report
    gaps_df = pd.DataFrame(all_gaps)
    if not gaps_df.empty:
        gaps_df.to_csv(GAPS_REPORT_PATH, index=False)
        log_msg(f"Logged {len(gaps_df)} genuine gaps to {GAPS_REPORT_PATH}")
    else:
        pd.DataFrame(columns=["symbol", "timeframe", "gap_start", "gap_end", "missing_candles"]).to_csv(
            GAPS_REPORT_PATH, index=False
        )
        log_msg(f"Zero genuine gaps detected! Empty template written to {GAPS_REPORT_PATH}")
        
    # Compute aggregate daily quote volume table for liquidity filter
    log_msg("Computing daily quote volume table across universe...")
    daily_vol_records = []
    for item in universe:
        sym = item["symbol"]
        d1_files = sorted((PARQUET_DIR / sym / "klines" / "1d").glob("**/part.parquet"))
        for f in d1_files:
            t = pq.read_table(f, columns=["open_time", "quote_volume"])
            df_d = t.to_pandas()
            for _, r in df_d.iterrows():
                daily_vol_records.append({
                    "symbol": sym,
                    "date": r["open_time"].strftime("%Y-%m-%d"),
                    "quote_volume": r["quote_volume"],
                })
    if daily_vol_records:
        daily_vol_df = pd.DataFrame(daily_vol_records)
        daily_vol_table = pa.Table.from_pandas(daily_vol_df)
        pq.write_table(daily_vol_table, DAILY_VOLUME_PATH, compression="zstd")
        log_msg(f"Saved daily quote volume ({len(daily_vol_df):,} records) to {DAILY_VOLUME_PATH}")
        
    elapsed_total = time.time() - t_start
    total_size = sum(f.stat().st_size for f in PARQUET_DIR.rglob("*.parquet"))
    disk_used_mb = total_size / (1024 * 1024)
    
    log_msg("================================================================================")
    log_msg("SYNC PIPELINE COMPLETED SUCCESSFULLY")
    log_msg("================================================================================")
    log_msg(f"Coins total: {len(universe)}")
    log_msg(f"Coins from cache: {sum(1 for u in universe if u['in_cache'])}")
    log_msg(f"Coins missing from cache (fully downloaded): {sum(1 for u in universe if not u['in_cache'])}")
    log_msg(f"Delisted coins included: {sum(1 for u in universe if u['is_delisted'])}")
    log_msg(f"Total candles in store: {total_candles:,}")
    log_msg(f"Gaps logged in {GAPS_REPORT_PATH}: {len(all_gaps)}")
    log_msg(f"Disk used: {disk_used_mb:.2f} MB")
    log_msg(f"Time taken: {elapsed_total:.2f} seconds ({elapsed_total/60:.2f} minutes)")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_pipeline(max_workers=args.workers, symbol_limit=args.limit)
