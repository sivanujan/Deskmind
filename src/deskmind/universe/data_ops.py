"""Real implementations behind the daily job: the existing data pipeline (Syncer + reconcile +
filters snapshot), the existing verify step, the trend agent's backfill as warm-up, and disk usage.

Every symbol here comes from the universe (exchange data), never from a hardcoded list.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from deskmind.config import Settings
from deskmind.data.datasets import DAY_MS, DEFAULT_DATASETS, to_ms
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.pipeline import Syncer
from deskmind.data.reconcile import reconcile
from deskmind.data.storage import Store
from deskmind.data.verify import FAIL, render_run_markdown, verify_store
from deskmind.universe.job import DiskUsage, VerifyOutcome, WarmUp


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class ParquetDataOps:
    def __init__(self, settings: Settings, fapi: RateLimitedClient, vision: RateLimitedClient):
        self.data_dir = Path(settings.data_dir)
        self.fapi = fapi
        self.vision = vision

    def _store(self, symbol: str) -> Store:
        return Store(self.data_dir, symbol)

    def has_history(self, symbol: str, start: date) -> bool:
        """Stored history starts at or before `start` and every dataset has files."""
        store = self._store(symbol)
        stored = store.load_manifest().get("start")
        if not stored or date.fromisoformat(stored) > start:
            return False
        return all(store.files(ds) for ds in DEFAULT_DATASETS)

    async def _sync(self, symbol: str, start: date | None) -> None:
        store = self._store(symbol)
        syncer = Syncer(store, self.fapi, self.vision)
        results = await syncer.run(start)
        errors = [f"{r.key}: {e}" for r in results for e in r.errors]
        if errors:
            raise RuntimeError("; ".join(errors)[:500])
        await reconcile(store, self.fapi)
        await syncer.snapshot_filters()

    async def download(self, symbol: str, start: date) -> None:
        await self._sync(symbol, start)

    async def update(self, symbol: str) -> None:
        await self._sync(symbol, None)

    async def verify(self, symbol: str) -> VerifyOutcome:
        store = self._store(symbol)
        rep = await asyncio.to_thread(verify_store, store)
        failures = [
            f"{d.key}: {c.name}: {c.detail}"
            for d in rep.datasets
            for c in d.checks
            if c.status == FAIL
        ]
        failures += [
            f"{c.source} -> {c.target}: cross-timeframe mismatch"
            for c in rep.cross_tf
            if c.status == FAIL
        ]
        day = rep.generated_at.date().isoformat()
        path = store.reports_dir / f"universe_verify_{symbol}_{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, render_run_markdown([rep]), encoding="utf-8")
        return VerifyOutcome(rep.status, failures, str(path))

    def disk_usage(self) -> DiskUsage:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        du = shutil.disk_usage(self.data_dir)
        return DiskUsage(du.total, du.used, du.free, dir_bytes(self.data_dir / "parquet"))


def trend_warm_up(settings: Settings, engine: AsyncEngine) -> WarmUp:
    """Warm-up = the trend agent's backfill over the last day (it replays `warmup_bars` candles per
    timeframe first, the same code path as live), storing reports and items for that symbol."""
    from deskmind.agents.trend.runner import backfill
    from deskmind.agents.trend.store import PostgresTrendStore

    store = PostgresTrendStore(engine)

    async def warm(symbol: str) -> int:
        end_ms = to_ms(datetime.now(UTC))
        r = await backfill(
            symbol,
            list(settings.live_timeframes),
            end_ms - DAY_MS,
            end_ms,
            data_dir=settings.data_dir,
            recent=None,
            store=store,
        )
        return int(r["reports"])

    return warm
