"""Real side effects of the daily job, built on the existing data pipeline, verify step and
trend-agent backfill. Public market data only; no API keys."""

from __future__ import annotations

import asyncio
import shutil
from datetime import date
from typing import Any

from deskmind.config import Settings
from deskmind.data.storage import Store
from deskmind.universe.models import SymbolStat
from deskmind.universe.ranking import UniverseConfig, fetch_liquidity_stats


def dir_size_bytes(path: Any) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


class LiveDataOps:
    def __init__(self, settings: Settings, cfg: UniverseConfig) -> None:
        self.settings = settings
        self.cfg = cfg

    async def fetch_stats(self, include: set[str], now_ms: int) -> list[SymbolStat]:
        from deskmind.data.http_client import RateLimitedClient

        async with RateLimitedClient(
            self.settings.binance_market_data_url, max_concurrency=4
        ) as client:
            return await fetch_liquidity_stats(client, self.cfg, now_ms, include)

    def has_data(self, symbol: str) -> bool:
        root = Store(self.settings.data_dir, symbol).root
        return root.exists() and any(root.rglob("*.parquet"))

    async def _sync(self, symbol: str, start: date | None) -> None:
        from deskmind.data.pipeline import Syncer, make_clients
        from deskmind.data.reconcile import reconcile

        fapi, vision = make_clients(self.settings.binance_market_data_url)
        async with fapi, vision:
            store = Store(self.settings.data_dir, symbol)
            syncer = Syncer(store, fapi, vision)
            await syncer.run(start)
            await reconcile(store, fapi)
            await syncer.snapshot_filters()

    async def download(self, symbol: str, start: date) -> None:
        await self._sync(symbol, start)

    async def update(self, symbol: str) -> None:
        await self._sync(symbol, None)

    async def verify(self, symbol: str) -> tuple[bool, str]:
        from deskmind.data.verify import verify_store

        rep = await asyncio.to_thread(verify_store, Store(self.settings.data_dir, symbol))
        fails = [
            f"{d.key}: {c.name}: {c.detail}"
            for d in rep.datasets
            for c in d.checks
            if c.status == "FAIL"
        ]
        return rep.status != "FAIL", "; ".join(fails) or rep.status

    async def warm_up(self, symbol: str, start_ms: int, end_ms: int) -> None:
        from deskmind.agents.trend.runner import backfill
        from deskmind.agents.trend.store import PostgresTrendStore
        from deskmind.api.candles import PostgresRecentCandles
        from deskmind.db.session import create_engine

        engine = create_engine(self.settings)
        try:
            await backfill(
                symbol,
                list(self.settings.live_timeframes),
                start_ms,
                end_ms,
                data_dir=self.settings.data_dir,
                recent=PostgresRecentCandles(engine),
                store=PostgresTrendStore(engine),
            )
        finally:
            await engine.dispose()

    def disk_usage(self) -> dict[str, Any]:
        d = self.settings.data_dir
        d.mkdir(parents=True, exist_ok=True)
        u = shutil.disk_usage(d)
        return {
            "used_pct": round(u.used / u.total * 100, 2),
            "free_bytes": u.free,
            "total_bytes": u.total,
            "data_dir_bytes": dir_size_bytes(d / "parquet"),
        }
