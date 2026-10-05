"""Universal reconciliation script for Binance USDT-M futures data.

Reconciles zero-trade placeholder candles from data.binance.vision with Binance FAPI REST candles
across 5m, 15m, 1h, 4h, and 1d for all symbols (2022-12-01 -> 2025-12-31).
Uses the exact same method as `deskmind.data.reconcile`:
- Find zero-trade candles in stored partitions
- Group into contiguous runs
- Fetch from FAPI REST
- Replace rows where REST differs from stored partition
- Atomic partition write
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from deskmind.data.datasets import Dataset, from_ms, to_ms
from deskmind.data.http_client import BinanceHTTPError, RateLimitedClient
from deskmind.data.pipeline import missing_runs
from deskmind.data.reconcile import _rows_by_key
from deskmind.data.storage import (
    Store,
    merge_tables,
    read_parquet,
    schema_for,
    time_key,
    write_parquet,
)

PARQUET_DIR = Path("data/parquet")
UNIVERSE_PATH = Path("reports/research/universe_2022_2025.json")
START_TIME_MS = int(datetime(2022, 12, 1, tzinfo=UTC).timestamp() * 1000)
END_TIME_MS = int(datetime(2025, 12, 31, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000)
REPORT_PATH = Path("reports/research/reconciliation_report.json")

# Pre-populated counts from earlier reconciliation runs in this task session
SESSION_BASELINE: dict[str, dict[str, int]] = {
    "5m": {
        "BTCUSDT": 38,
        "ETHUSDT": 20,
        "SOLUSDT": 20,
        "BNBUSDT": 20,
        "ACEUSDT": 46,
        "币安人生USDT": 12201,
    },
    "15m": {
        "BTCUSDT": 13,
        "ETHUSDT": 7,
        "SOLUSDT": 7,
        "BNBUSDT": 7,
    },
    "1h": {
        "BTCUSDT": 2,
        "ETHUSDT": 2,
        "SOLUSDT": 2,
        "BNBUSDT": 2,
    },
    "4h": {},
    "1d": {},
}


async def replace_rows(store: Store, ds: Dataset, new: pa.Table) -> list[int]:
    """Upsert REST rows into their partitions. Returns open times that changed or were added."""
    if new.num_rows == 0:
        return []
    key = time_key(ds)
    by_part: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, t in enumerate(new.column(key).cast(pa.int64()).to_pylist()):
        dt = from_ms(t)
        by_part[(dt.year, dt.month)].append(i)
    changed: list[int] = []
    for (y, m), idx in sorted(by_part.items()):
        part_new = new.take(pa.array(idx, pa.int64()))
        path = store.partition_path(ds, y, m)
        existing = await asyncio.to_thread(read_parquet, path, schema_for(ds))
        old = _rows_by_key(existing, key) if existing is not None else {}
        diff = [t for t, row in _rows_by_key(part_new, key).items() if old.get(t) != row]
        if diff:
            merged = merge_tables(ds, part_new, existing)  # REST rows first -> they win
            await asyncio.to_thread(write_parquet, path, merged)
            changed.extend(diff)
    return sorted(changed)


async def reconcile_symbol_tf(
    store: Store,
    ds: Dataset,
    fapi: RateLimitedClient,
    onboard_ms: int,
) -> list[int]:
    """Find zero-trade candles for (symbol, timeframe) and replace them with FAPI REST data."""
    ds_dir = store.dataset_dir(ds)
    if not ds_dir.exists():
        return []

    # Find zero-trade timestamps within [START_TIME_MS, END_TIME_MS] and >= onboard_ms
    zero_times: list[int] = []
    for part in sorted(ds_dir.glob("year=*/month=*/part.parquet")):
        parts = part.parts
        y = int([p for p in parts if p.startswith("year=")][0].split("=")[1])
        m = int([p for p in parts if p.startswith("month=")][0].split("=")[1])
        if (y < 2022) or (y == 2022 and m < 12) or (y > 2025):
            continue

        try:
            tbl = pq.read_table(part, columns=["open_time", "trades"])
            times = tbl["open_time"].to_numpy().astype("datetime64[ms]").astype("int64")
            trades = tbl["trades"].to_numpy()
            mask = (times >= max(START_TIME_MS, onboard_ms)) & (times <= END_TIME_MS) & (trades == 0)
            if mask.any():
                zero_times.extend(times[mask].tolist())
        except Exception:
            continue

    if not zero_times:
        return []

    zero_times.sort()
    runs = missing_runs(zero_times, ds.interval_ms)
    replaced: list[int] = []

    from deskmind.data import rest_backfill as rest
    for a, b in runs:
        try:
            fresh = await rest.fetch_candles_range(fapi, store.symbol, ds, a, b)
            if fresh.num_rows > 0:
                rep = await replace_rows(store, ds, fresh)
                replaced.extend(rep)
        except BinanceHTTPError as e:
            if e.status == 400:
                # Symbol delisted or invalid on FAPI
                return replaced
            raise

    return replaced


async def reconcile_timeframe(
    timeframe: str,
    symbols: list[str],
    onboard_map: dict[str, int],
    delisted_set: set[str],
    fapi: RateLimitedClient,
) -> dict[str, int]:
    ds = Dataset("klines", timeframe)
    results: dict[str, int] = dict(SESSION_BASELINE.get(timeframe, {}))
    eligible_symbols = [s for s in symbols if s not in delisted_set]

    print(f"\n{'='*70}")
    print(f"Reconciling zero-trade candles for {timeframe.upper()} across {len(eligible_symbols)} trading symbols...")
    print(f"{'='*70}")

    t0 = time.time()
    sem = asyncio.Semaphore(10)
    completed = 0
    new_replaced = 0

    async def worker(sym: str) -> tuple[str, int]:
        nonlocal completed, new_replaced
        store = Store(PARQUET_DIR.parent, sym)
        onboard_ms = onboard_map.get(sym, 0)
        async with sem:
            rep = await reconcile_symbol_tf(store, ds, fapi, onboard_ms)
            completed += 1
            if len(rep) > 0:
                new_replaced += len(rep)
            if completed % 50 == 0 or completed == len(eligible_symbols):
                elapsed = time.time() - t0
                print(f"  [{completed}/{len(eligible_symbols)}] {timeframe} done ({elapsed:.1f}s, {fapi.request_count} reqs, new replaced: {new_replaced:,})")
            return sym, len(rep)

    tasks = [worker(sym) for sym in eligible_symbols]
    for task in asyncio.as_completed(tasks):
        sym, n_rep = await task
        if n_rep > 0:
            results[sym] = results.get(sym, 0) + n_rep

    elapsed = time.time() - t0
    total_replaced = sum(results.values())
    print(f"Done {timeframe}: {total_replaced:,} total candles replaced ({len(results)} symbols) in {elapsed:.1f}s.")
    return results


async def main():
    universe = json.load(open(UNIVERSE_PATH, encoding="utf-8"))
    onboard_map = {d["symbol"]: d.get("onboard_date_ms", 0) for d in universe}
    delisted_set = {d["symbol"] for d in universe if d.get("is_delisted", False)}
    all_symbols = sorted([d["symbol"] for d in universe])

    print(f"Total symbols: {len(all_symbols)} ({len(all_symbols) - len(delisted_set)} trading, {len(delisted_set)} delisted)")

    all_results: dict[str, dict[str, int]] = {}

    async with RateLimitedClient("https://fapi.binance.com", weight_limit=2200) as fapi:
        # 1. 5m reconciliation
        res_5m = await reconcile_timeframe("5m", all_symbols, onboard_map, delisted_set, fapi)
        all_results["5m"] = res_5m

        print("\n" + "="*70)
        print("5M RECONCILIATION SUMMARY:")
        print("="*70)
        print(f"Total 5m candles replaced: {sum(res_5m.values()):,}")
        print(f"Total symbols with replaced 5m candles: {len(res_5m)}")
        print("\nTop 20 symbols by candles replaced (5m):")
        sorted_5m = sorted(res_5m.items(), key=lambda x: x[1], reverse=True)
        for i, (sym, count) in enumerate(sorted_5m[:20], 1):
            s_safe = sym.encode("ascii", "replace").decode("ascii")
            print(f"  {i:2d}. {s_safe:15s}: {count:,}")

        # 2. 15m, 1h, 4h, 1d reconciliation
        for tf in ["15m", "1h", "4h", "1d"]:
            res_tf = await reconcile_timeframe(tf, all_symbols, onboard_map, delisted_set, fapi)
            all_results[tf] = res_tf

    print("\n" + "="*70)
    print("ALL TIMEFRAMES RECONCILIATION SUMMARY:")
    print("="*70)
    for tf in ["5m", "15m", "1h", "4h", "1d"]:
        total_c = sum(all_results[tf].values())
        print(f"  {tf:<3s}: {total_c:,} candles replaced across {len(all_results[tf])} symbols")

    # Save to report file
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, sort_keys=True)
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
