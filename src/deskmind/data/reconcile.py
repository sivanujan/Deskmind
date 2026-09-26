"""Reconcile bulk-archive data against the live REST API.

Why: data.binance.vision occasionally contains flat, zero-trade placeholder candles during
incidents where the REST API has the real candles (e.g. BTCUSDT 5m 2023-11-10 15:10-16:40), and
some higher-timeframe bulk rows disagree with REST. REST is the exchange's authoritative API, so:

1. Every zero-trade kline is re-downloaded from REST once; REST replaces the row if it differs.
2. Every cross-timeframe mismatch (5m resampled vs 15m/1h/2h, mark 5m vs 1h) is re-downloaded
   for both timeframes; REST replaces rows that differ. Mismatches that survive are the
   exchange's own data: recorded as confirmed and reported as WARN, never "fixed" by us.

All actions are recorded in the manifest (`reconcile` section) so they are auditable and never
repeated. Nothing is interpolated or invented.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from deskmind.data import rest_backfill as rest
from deskmind.data.datasets import CROSS_TF_CHECKS, DEFAULT_DATASETS, Dataset, from_ms
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.pipeline import missing_runs
from deskmind.data.storage import (
    Store,
    create_view,
    duckdb_connect,
    merge_tables,
    read_parquet,
    schema_for,
    time_key,
    write_parquet,
)
from deskmind.data.verify import check_cross_tf
from deskmind.logging import get_logger

log = get_logger(__name__)

MAX_ROUNDS = 3


@dataclass
class ReconcileResult:
    zero_trade_checked: dict[str, int] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)
    cross_tf_rechecked: dict[str, int] = field(default_factory=dict)
    cross_tf_confirmed: dict[str, int] = field(default_factory=dict)


def _rows_by_key(table: pa.Table, key: str) -> dict[int, tuple[Any, ...]]:
    def plain(col: pa.ChunkedArray) -> list[Any]:
        return (col.cast(pa.int64()) if pa.types.is_timestamp(col.type) else col).to_pylist()

    keys = plain(table.column(key))
    cols = [plain(table.column(n)) for n in table.column_names if n != key]
    return {k: tuple(c[i] for c in cols) for i, k in enumerate(keys)}


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


async def reconcile(
    store: Store, fapi: RateLimitedClient, datasets: tuple[Dataset, ...] = DEFAULT_DATASETS
) -> ReconcileResult:
    manifest = store.load_manifest()
    section: dict[str, Any] = manifest.setdefault("reconcile", {})
    result = ReconcileResult()

    # 1. zero-trade klines -> REST
    con = duckdb_connect()
    for ds in (d for d in datasets if d.kind == "klines"):
        entry = section.setdefault(ds.key, {"rest_checked": [], "replaced": []})
        if not create_view(con, "z", store, ds):
            continue
        checked = set(entry["rest_checked"])
        zero = [
            r[0]
            for r in con.execute(
                "SELECT epoch_ms(open_time) FROM z WHERE trades = 0 ORDER BY 1"
            ).fetchall()
        ]
        todo = [t for t in zero if t not in checked]
        replaced: list[int] = []
        for a, b in missing_runs(todo, ds.interval_ms):
            fresh = await rest.fetch_candles_range(fapi, store.symbol, ds, a, b)
            replaced += await replace_rows(store, ds, fresh)
        entry["rest_checked"] = sorted(checked | set(todo))
        entry["replaced"] = sorted(set(entry["replaced"]) | set(replaced))
        result.zero_trade_checked[ds.key] = len(todo)
        result.replaced[ds.key] = len(replaced)
        store.save_manifest(manifest)
    con.close()

    # 2. cross-timeframe mismatches -> re-download both sides, then confirm what remains.
    # Replacing 5m rows for one pair can expose a mismatch in another pair, so rounds repeat
    # until no pair has an unchecked mismatch (bounded).
    confirmed_all: dict[str, list[int]] = section.setdefault("cross_tf_confirmed", {})
    keys = {d.key for d in datasets}
    pairs = [(s, d) for s, d in CROSS_TF_CHECKS if s.key in keys and d.key in keys]
    refetched: dict[str, set[int]] = {f"{s.key}->{d.key}": set() for s, d in pairs}
    for _round in range(MAX_ROUNDS):
        progressed = False
        for src, dst in pairs:
            pair = f"{src.key}->{dst.key}"
            skip = set(confirmed_all.get(pair, [])) | refetched[pair]
            buckets = _mismatch_buckets(store, src, dst, skip)
            if not buckets:
                continue
            progressed = True
            result.cross_tf_rechecked[pair] = result.cross_tf_rechecked.get(pair, 0) + len(buckets)
            for bucket in buckets:
                for ds in (src, dst):
                    fresh = await rest.fetch_candles_range(
                        fapi, store.symbol, ds, bucket, bucket + dst.interval_ms
                    )
                    changed = await replace_rows(store, ds, fresh)
                    entry = section.setdefault(ds.key, {"rest_checked": [], "replaced": []})
                    entry["replaced"] = sorted(set(entry["replaced"]) | set(changed))
                    result.replaced[ds.key] = result.replaced.get(ds.key, 0) + len(changed)
            refetched[pair] |= set(buckets)
        if not progressed:
            break

    # Whatever still mismatches after its own REST re-download is the exchange's data.
    for src, dst in pairs:
        pair = f"{src.key}->{dst.key}"
        confirmed = set(confirmed_all.get(pair, []))
        still = set(_mismatch_buckets(store, src, dst, confirmed)) & refetched[pair]
        confirmed_all[pair] = sorted(confirmed | still)
        result.cross_tf_confirmed[pair] = len(still)
        log.info("cross_tf_reconciled", pair=pair, confirmed=len(still))
    store.save_manifest(manifest)
    return result


def _mismatch_buckets(store: Store, src: Dataset, dst: Dataset, confirmed: set[int]) -> list[int]:
    con = duckdb_connect()
    try:
        if not (create_view(con, "s", store, src) and create_view(con, "d", store, dst)):
            return []
        rep = check_cross_tf(con, "s", "d", src, dst, confirmed)
        return sorted(set(rep.mismatch_buckets) | set(rep.volume_mismatch_buckets))
    finally:
        con.close()
