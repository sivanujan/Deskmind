"""Bar-close aggregation: exactly one event per (symbol, close_time), in time order.

Boundaries are every `base` ms (the smallest timeframe, 5m). At boundary T the expected
timeframes are those with T % interval == 0 (10:00 -> 5m, 15m, 1h, 2h; 10:05 -> 5m only).

Per symbol a cursor walks the boundaries strictly in order:
- all expected candles (open_time = T - interval) have arrived  -> emit, advance
- not yet, and T + timeout has not passed, or the feed is paused  -> wait
- timeout passed -> backfill the missing candles from REST, then emit; anything REST does not
  have either is listed in `missing` with `degraded: true`
- a REST error (e.g. network down) is not "missing data": the boundary waits and retries.

Because the cursor never skips ahead, gaps (e.g. after a reconnect) are backfilled and emitted
before any newer event. A Redis SET NX marker per (symbol, close_time) guarantees an event is
never published twice, also across restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B
from deskmind.data.live.buffers import Kline
from deskmind.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT_MS = 10_000
CATCHUP_MAX_MS = 6 * 3600 * 1000  # on restart, re-emit missed boundaries up to this far back

# (symbol, [(timeframe, open_time), ...]) -> fetch those candles from REST and store them
Resolver = Callable[[str, list[tuple[str, int]]], Awaitable[None]]


class BarCloseAggregator:
    def __init__(
        self,
        redis: Redis,
        symbols: Iterable[str],
        timeframes: Iterable[str],
        resolver: Resolver,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        clock: Callable[[], int] = B.now_ms,
        catchup_max_ms: int = CATCHUP_MAX_MS,
    ) -> None:
        self.redis = redis
        self.symbols = list(symbols)
        self.timeframes = list(timeframes)
        self.intervals = {tf: TIMEFRAME_MS[tf] for tf in self.timeframes}
        self.base = min(self.intervals.values())
        if any(iv % self.base for iv in self.intervals.values()):
            raise ValueError("every timeframe must be a multiple of the smallest one")
        self.resolver = resolver
        self.timeout_ms = timeout_ms
        self.clock = clock
        self.catchup_max_ms = catchup_max_ms
        self.paused = True  # until the feed is connected and gaps are backfilled
        self.cursor: dict[str, int] = {}
        self.seen: dict[tuple[str, str], set[int]] = {
            (s, tf): set() for s in self.symbols for tf in self.timeframes
        }
        self._locks = {s: asyncio.Lock() for s in self.symbols}
        self.emitted = 0
        self.degraded = 0
        self.duplicates_suppressed = 0

    # ------------------------------------------------------------ state

    async def load_state(self) -> None:
        """Resume after the last emitted boundary (if recent), else start at the next one."""
        now = self.clock()
        next_boundary = (now // self.base) * self.base + self.base
        for s in self.symbols:
            raw = await self.redis.get(B.last_emitted_key(s))
            last = int(raw) if raw else None
            if last is not None and now - last <= self.catchup_max_ms:
                self.cursor[s] = last + self.base
            else:
                self.cursor[s] = next_boundary
            log.info("bar_close_cursor", symbol=s, next=_iso(self.cursor[s]), resumed=bool(last))

    def expected(self, close_time: int) -> list[str]:
        return [tf for tf in self.timeframes if close_time % self.intervals[tf] == 0]

    def mark_seen(self, k: Kline) -> None:
        key = (k.symbol, k.timeframe)
        if key in self.seen and k.is_closed:
            self.seen[key].add(k.open_time)

    def missing(self, symbol: str, close_time: int) -> list[str]:
        return [
            tf
            for tf in self.expected(close_time)
            if close_time - self.intervals[tf] not in self.seen[(symbol, tf)]
        ]

    # ------------------------------------------------------------ processing

    async def process(self, symbol: str) -> int:
        """Emit every boundary that is ready, in order. Returns number emitted."""
        emitted = 0
        async with self._locks[symbol]:
            while True:
                t = self.cursor[symbol]
                now = self.clock()
                if t > now:
                    break
                missing = self.missing(symbol, t)
                if missing:
                    if self.paused or now < t + self.timeout_ms:
                        break
                    try:
                        await self.resolver(
                            symbol, [(tf, t - self.intervals[tf]) for tf in missing]
                        )
                    except Exception as exc:  # network trouble: retry on a later tick
                        log.warning(
                            "bar_close_backfill_failed",
                            symbol=symbol,
                            close=_iso(t),
                            error=str(exc),
                        )
                        break
                    missing = self.missing(symbol, t)
                if await self._emit(symbol, t, missing):
                    emitted += 1
                self.cursor[symbol] = t + self.base
                self._prune(symbol)
        return emitted

    async def tick(self) -> int:
        return sum([await self.process(s) for s in self.symbols])

    async def run(self, stop: asyncio.Event, interval_s: float = 0.5) -> None:
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)

    async def _emit(self, symbol: str, close_time: int, missing: list[str]) -> bool:
        expected = self.expected(close_time)
        closed = [tf for tf in expected if tf not in missing]
        first = await self.redis.set(B.sent_key(symbol, close_time), "1", nx=True, ex=B.SENT_TTL_S)
        if not first:
            self.duplicates_suppressed += 1
            log.info("bar_close_duplicate_suppressed", symbol=symbol, close=_iso(close_time))
            return False
        payload: dict[str, Any] = {
            "symbol": symbol,
            "close_time": close_time,
            "close_time_iso": _iso(close_time),
            "closed_timeframes": closed,
            "degraded": bool(missing),
            "missing": missing,
            "schema_version": B.SCHEMA_VERSION,
            "emitted_at": B.now_ms(),
        }
        msg = json.dumps(payload, separators=(",", ":"))
        pipe = self.redis.pipeline(transaction=False)
        pipe.publish(B.BAR_CLOSE_CHANNEL, msg)
        pipe.xadd(B.EVENT_LOG, {"event": msg}, maxlen=B.EVENT_LOG_MAXLEN, approximate=True)
        pipe.set(B.last_emitted_key(symbol), close_time)
        await pipe.execute()
        self.emitted += 1
        if missing:
            self.degraded += 1
            log.warning(
                "bar_close_degraded", symbol=symbol, close=_iso(close_time), missing=missing
            )
        return True

    def _prune(self, symbol: str) -> None:
        horizon = self.cursor[symbol] - 2 * max(self.intervals.values())
        for tf in self.timeframes:
            s = self.seen[(symbol, tf)]
            if len(s) > 1000:
                self.seen[(symbol, tf)] = {t for t in s if t >= horizon}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
