"""Kline stream handling: partial candles for display, closed candles for decisions.

Closed candles (x == true) go to the Redis buffer, the Postgres `candles` hypertable and the
bar-close aggregator. All three are idempotent, so duplicates and replays are harmless; an
older candle arriving late simply fills its slot (out-of-order safe).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from redis.asyncio import Redis

from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.live import buffers as B
from deskmind.data.live.bar_close import BarCloseAggregator
from deskmind.data.live.buffers import Kline
from deskmind.data.live.rest import fetch_closed_klines
from deskmind.db.candles import CandleSink
from deskmind.logging import get_logger

log = get_logger(__name__)


class KlineHandler:
    def __init__(
        self,
        redis: Redis,
        sink: CandleSink,
        aggregator: BarCloseAggregator,
        rest: RateLimitedClient,
    ) -> None:
        self.redis = redis
        self.sink = sink
        self.aggregator = aggregator
        self.rest = rest
        self._pending_db: list[Kline] = []  # retried on the next write if Postgres failed
        self.closed_received = 0
        self.backfilled = 0
        self.db_errors = 0

    async def on_message(self, data: dict[str, Any]) -> None:
        """One kline event (`data` of a combined-stream message, e == "kline")."""
        k = Kline.from_ws(data["k"], data.get("s"))
        if not k.is_closed:
            await B.put_open(self.redis, k, int(data.get("E", 0)))
            return
        self.closed_received += 1
        await self.store_closed([k])

    async def store_closed(self, klines: Sequence[Kline], *, notify: bool = True) -> None:
        """Buffer + persist + mark seen. notify=False when called from inside the aggregator
        (its per-symbol lock is held; it re-checks by itself)."""
        klines = sorted((k for k in klines if k.is_closed), key=lambda k: (k.symbol, k.open_time))
        if not klines:
            return
        await B.put_closed(self.redis, list(klines))
        await self._persist(klines)
        for k in klines:
            self.aggregator.mark_seen(k)
        if not notify:
            return
        for symbol in dict.fromkeys(k.symbol for k in klines):
            if symbol in self.aggregator.cursor:
                await self.aggregator.process(symbol)

    async def _persist(self, klines: Sequence[Kline]) -> None:
        batch = [*self._pending_db, *klines]
        try:
            await self.sink.upsert(batch)
            self._pending_db = []
        except Exception as exc:
            self.db_errors += 1
            self._pending_db = batch[-10_000:]
            log.error("candles_db_upsert_failed", error=str(exc), pending=len(self._pending_db))

    # ------------------------------------------------------------ REST paths

    async def warm_up(self, symbol: str, tf: str, n: int = B.BUFFER_SIZE) -> int:
        """Load the last n closed candles (startup)."""
        klines = await fetch_closed_klines(self.rest, symbol, tf, limit=n)
        await self.store_closed(klines)
        return len(klines)

    async def backfill_gap(self, symbol: str, tf: str) -> int:
        """Fetch every closed candle after the newest one in the buffer (after reconnects)."""
        last = await B.get_last_closed(self.redis, symbol, tf)
        if last is None:
            return await self.warm_up(symbol, tf)
        klines = await fetch_closed_klines(
            self.rest, symbol, tf, start_ms=last.open_time + TIMEFRAME_MS[tf]
        )
        if klines:
            self.backfilled += len(klines)
            log.info(
                "gap_backfilled",
                symbol=symbol,
                tf=tf,
                candles=len(klines),
                first=klines[0].open_time,
                last=klines[-1].open_time,
            )
        await self.store_closed(klines)
        return len(klines)

    async def resolve_missing(self, symbol: str, items: list[tuple[str, int]]) -> None:
        """Aggregator timeout path: fetch specific candles by open_time from REST."""
        for tf, open_time in items:
            klines = await fetch_closed_klines(
                self.rest, symbol, tf, start_ms=open_time, end_ms=open_time + 1
            )
            klines = [k for k in klines if k.open_time == open_time]
            log.warning(
                "bar_close_timeout_backfill",
                symbol=symbol,
                tf=tf,
                open_time=open_time,
                found=bool(klines),
            )
            if klines:
                self.backfilled += 1
                await self.store_closed(klines, notify=False)
