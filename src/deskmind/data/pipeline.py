"""Download / update orchestration: bulk zips first, REST for whatever is still missing.

Idempotent and incremental:
- Data lives in month partitions (funding: year partitions). Each run loads the partition, fetches
  only missing candles, merges with de-duplication and rewrites atomically.
- `_manifest.json` marks fully closed months as complete; later runs skip them without any request.
  Gaps that remain in a complete month were absent from both the bulk files and the REST API
  (genuine exchange gaps). They are reported by `verify`, never invented or filled.
- The currently forming candle is never stored.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa

from deskmind.data import bulk_download as bulk
from deskmind.data import rest_backfill as rest
from deskmind.data.datasets import (
    DAY_MS,
    DEFAULT_DATASETS,
    Dataset,
    date_ms,
    from_ms,
    iter_days,
    iter_months,
    last_closed_open_ms,
    month_bounds_ms,
    to_ms,
)
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.storage import (
    Store,
    filter_time,
    merge_tables,
    read_parquet,
    schema_for,
    time_values_ms,
    write_json,
    write_parquet,
)
from deskmind.logging import get_logger

log = get_logger(__name__)


@dataclass
class DatasetResult:
    key: str
    months_checked: int = 0
    months_skipped: int = 0
    monthly_zips: int = 0
    daily_zips: int = 0
    rest_ranges: int = 0
    rows_added: int = 0
    missing_candles: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def missing_runs(missing: list[int], interval_ms: int) -> list[tuple[int, int]]:
    """Sorted missing open_times -> [(start, end_exclusive), ...] contiguous runs."""
    runs: list[tuple[int, int]] = []
    for t in missing:
        if runs and runs[-1][1] == t:
            runs[-1] = (runs[-1][0], t + interval_ms)
        else:
            runs.append((t, t + interval_ms))
    return runs


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Syncer:
    def __init__(
        self,
        store: Store,
        fapi: RateLimitedClient,
        vision: RateLimitedClient,
        *,
        now: datetime | None = None,
        month_concurrency: int = 6,
    ) -> None:
        self.store = store
        self.symbol = store.symbol
        self.fapi = fapi
        self.vision = vision
        self.now_ms = to_ms(now or datetime.now(UTC))
        self._sem = asyncio.Semaphore(month_concurrency)

    async def run(
        self, start: date | None, datasets: tuple[Dataset, ...] = DEFAULT_DATASETS
    ) -> list[DatasetResult]:
        manifest = self.store.load_manifest()
        stored = date.fromisoformat(manifest["start"]) if manifest.get("start") else None
        if start is None and stored is None:
            raise ValueError("no start date: run `data download --start YYYY-MM-DD` first")
        effective = min(d for d in (start, stored) if d is not None)
        manifest["start"] = effective.isoformat()
        start_ms = date_ms(effective)

        results = []
        for ds in datasets:
            entries = manifest["datasets"].setdefault(ds.key, {})
            if ds.is_candles:
                res = await self._sync_candles(ds, start_ms, entries)
            else:
                res = await self._sync_funding(ds, start_ms, entries)
            self.store.save_manifest(manifest)
            log.info("dataset_synced", **res.as_dict())
            results.append(res)
        return results

    async def snapshot_filters(self) -> str:
        snap = await rest.fetch_symbol_filters(self.fapi, self.symbol)
        day = from_ms(self.now_ms).date().isoformat()
        path = self.store.meta_dir / f"{self.symbol}_filters_{day}.json"
        await asyncio.to_thread(write_json, path, snap)
        return str(path)

    # ------------------------------------------------------------------ candles

    async def _sync_candles(
        self, ds: Dataset, start_ms: int, entries: dict[str, Any]
    ) -> DatasetResult:
        res = DatasetResult(ds.key)
        end_ms = last_closed_open_ms(self.now_ms, ds.interval_ms)
        if end_ms <= start_ms:
            return res
        months = list(iter_months(from_ms(start_ms).date(), from_ms(end_ms - 1).date()))
        async with asyncio.TaskGroup() as tg:
            for y, m in months:
                tg.create_task(self._sync_candle_month(ds, y, m, start_ms, end_ms, entries, res))
        return res

    async def _sync_candle_month(
        self,
        ds: Dataset,
        year: int,
        month: int,
        start_ms: int,
        end_ms: int,
        entries: dict[str, Any],
        res: DatasetResult,
    ) -> None:
        mkey = _month_key(year, month)
        if entries.get(mkey, {}).get("complete"):
            res.months_skipped += 1
            return
        async with self._sem:
            res.months_checked += 1
            interval = ds.interval_ms
            m_start, m_end = month_bounds_ms(year, month)
            lo, hi = max(m_start, start_ms), min(m_end, end_ms)
            month_closed = m_end <= end_ms
            path = self.store.partition_path(ds, year, month)
            existing = await asyncio.to_thread(read_parquet, path, schema_for(ds))
            before = existing.num_rows if existing is not None else 0

            have: set[int] = (
                set(time_values_ms(existing, "open_time")) if existing is not None else set()
            )
            parts: list[pa.Table] = []
            sources: set[str] = set(entries.get(mkey, {}).get("sources", []))

            def missing() -> list[int]:
                return [t for t in range(lo, hi, interval) if t not in have]

            def absorb(tbl: pa.Table | None, source: str) -> None:
                if tbl is not None and tbl.num_rows:
                    parts.append(tbl)
                    have.update(time_values_ms(tbl, "open_time"))
                    sources.add(source)

            got_monthly = False
            if missing() and month_closed:
                tbl = await bulk.fetch_month(self.vision, self.symbol, ds, year, month)
                if tbl is not None:
                    got_monthly = True
                    res.monthly_zips += 1
                    absorb(tbl, "bulk_monthly")

            if missing() and not got_monthly:
                today_ms = (self.now_ms // DAY_MS) * DAY_MS
                gaps = set(missing())
                days = [
                    d
                    for d in iter_days(lo, min(hi, today_ms))
                    if any(t in gaps for t in range(date_ms(d), date_ms(d) + DAY_MS, interval))
                ]
                tables = await asyncio.gather(
                    *(bulk.fetch_day(self.vision, self.symbol, ds, d) for d in days)
                )
                for tbl in tables:
                    if tbl is not None:
                        res.daily_zips += 1
                        absorb(tbl, "bulk_daily")

            for a, b in missing_runs(missing(), interval):
                res.rest_ranges += 1
                absorb(await rest.fetch_candles_range(self.fapi, self.symbol, ds, a, b), "rest")

            merged = filter_time(ds, merge_tables(ds, existing, *parts), m_start, hi)
            if parts or merged.num_rows != before:
                await asyncio.to_thread(write_parquet, path, merged)
            still_missing = len(missing())
            res.rows_added += merged.num_rows - before
            res.missing_candles += still_missing
            entries[mkey] = {
                "complete": month_closed,
                "rows": merged.num_rows,
                "missing": still_missing,
                "sources": sorted(sources),
                "updated_at": _now_iso(),
            }

    # ------------------------------------------------------------------ funding

    async def _sync_funding(
        self, ds: Dataset, start_ms: int, entries: dict[str, Any]
    ) -> DatasetResult:
        res = DatasetResult(ds.key)
        months = list(iter_months(from_ms(start_ms).date(), from_ms(self.now_ms).date()))
        # Sequential: several months share one year partition file.
        for y, m in months:
            mkey = _month_key(y, m)
            if entries.get(mkey, {}).get("complete"):
                res.months_skipped += 1
                continue
            res.months_checked += 1
            m_start, m_end = month_bounds_ms(y, m)
            lo, hi = max(m_start, start_ms), min(m_end, self.now_ms)
            month_closed = m_end <= self.now_ms
            path = self.store.partition_path(ds, y, m)
            year_tbl = await asyncio.to_thread(read_parquet, path, schema_for(ds))
            before = year_tbl.num_rows if year_tbl is not None else 0
            sources: set[str] = set(entries.get(mkey, {}).get("sources", []))

            parts: list[pa.Table] = []
            got_monthly = False
            if month_closed:
                tbl = await bulk.fetch_month(self.vision, self.symbol, ds, y, m)
                if tbl is not None:
                    got_monthly = True
                    res.monthly_zips += 1
                    parts.append(tbl)
                    sources.add("bulk_monthly")
            if not got_monthly:
                in_month = filter_time(ds, year_tbl, lo, hi) if year_tbl is not None else None
                times = time_values_ms(in_month, "funding_time") if in_month is not None else []
                rest_start = max(times) + 1000 if times else lo
                if rest_start < hi:
                    res.rest_ranges += 1
                    tbl = await rest.fetch_funding_range(self.fapi, self.symbol, rest_start, hi)
                    if tbl.num_rows:
                        parts.append(tbl)
                        sources.add("rest")

            merged = merge_tables(ds, year_tbl, *parts)
            if parts:
                await asyncio.to_thread(write_parquet, path, merged)
            res.rows_added += merged.num_rows - before
            entries[mkey] = {
                "complete": month_closed,
                "rows": filter_time(ds, merged, m_start, m_end).num_rows,
                "sources": sorted(sources),
                "updated_at": _now_iso(),
            }
        return res


MAX_PARALLEL_DOWNLOADS = 4


def make_clients(market_data_url: str) -> tuple[RateLimitedClient, RateLimitedClient]:
    """(fapi REST client with weight tracking, data.binance.vision client).

    Each client allows at most MAX_PARALLEL_DOWNLOADS requests in flight, far below Binance limits.
    Symbols are synced one after another and share these clients (and the weight budget).
    """
    fapi = RateLimitedClient(market_data_url, max_concurrency=MAX_PARALLEL_DOWNLOADS)
    vision = RateLimitedClient(
        bulk.VISION_BASE_URL, weight_limit=None, max_concurrency=MAX_PARALLEL_DOWNLOADS
    )
    return fapi, vision
