"""The live market-data service: wires WebSocket, handlers, aggregator, pollers and health.

Startup order:
1. schema version + health "starting"; clock check against Binance server time
2. restore bar-close cursors; warm buffers with the last 500 closed candles per (symbol, tf)
3. connect the combined stream; on every (re)connect, backfill gaps from REST BEFORE the
   aggregator may resolve timeouts again (it stays paused while disconnected)
4. run: WebSocket, aggregator ticks, open-interest poller, watchdog (staleness, heartbeat,
   hourly clock check) until stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis

from deskmind.data.http_client import RateLimitedClient
from deskmind.data.live import buffers as B
from deskmind.data.live.bar_close import BarCloseAggregator
from deskmind.data.live.kline_handler import KlineHandler
from deskmind.data.live.mark_handler import MarkHandler
from deskmind.data.live.oi_poller import OpenInterestPoller
from deskmind.data.live.rest import server_time_drift_ms
from deskmind.data.live.ws_client import CombinedStreamClient, Connector, default_connector
from deskmind.db.candles import CandleSink
from deskmind.logging import get_logger

log = get_logger(__name__)

STALE_KLINE_S = 30.0  # majors trade constantly; kline streams push every 250ms
STALE_MARK_S = 10.0  # markPrice@1s
CLOCK_CHECK_S = 3600
MAX_CLOCK_DRIFT_MS = 1000
WATCHDOG_S = 5.0


def stream_names(symbols: list[str], timeframes: list[str]) -> list[str]:
    kl = [f"{s.lower()}@kline_{tf}" for s in symbols for tf in timeframes]
    mk = [f"{s.lower()}@markPrice@1s" for s in symbols]
    return kl + mk


class LiveService:
    def __init__(
        self,
        redis: Redis,
        sink: CandleSink,
        rest: RateLimitedClient,
        *,
        symbols: list[str],
        timeframes: list[str],
        ws_url: str,
        connector: Connector = default_connector,
        timeout_ms: int = 10_000,
        oi_interval_s: float = 300,
        clock: Callable[[], int] = B.now_ms,
        health_key: str = B.HEALTH_SERVICE,
    ) -> None:
        self.redis = redis
        # Connection 0 reports on the main health hash; extra connections on their own hash.
        self.health_key = health_key
        self.symbols = symbols
        self.timeframes = timeframes
        self.rest = rest
        self.aggregator = BarCloseAggregator(
            redis, symbols, timeframes, self._resolve, timeout_ms=timeout_ms, clock=clock
        )
        self.klines = KlineHandler(redis, sink, self.aggregator, rest)
        self.marks = MarkHandler(redis)
        self.oi = OpenInterestPoller(redis, rest, symbols, oi_interval_s)
        self.streams = stream_names(symbols, timeframes)
        self.ws = CombinedStreamClient(
            ws_url,
            self.streams,
            self._dispatch,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            connector=connector,
            blocked_until=self._blocked_until,
        )
        self.started_at = B.now_ms()
        self.clock_drift_ms: int | None = None
        self.stale: list[str] = []
        self.state = "starting"

    # ------------------------------------------------------------ callbacks

    async def _resolve(self, symbol: str, items: list[tuple[str, int]]) -> None:
        await self.klines.resolve_missing(symbol, items)

    async def _dispatch(self, stream: str, data: dict[str, Any]) -> None:
        event = data.get("e")
        if event == "kline":
            await self.klines.on_message(data)
        elif event == "markPriceUpdate":
            await self.marks.on_message(data)

    async def _on_connect(self) -> None:
        """Runs before any message of the new connection is processed."""
        counts = await asyncio.gather(
            *(self.klines.backfill_gap(s, tf) for s in self.symbols for tf in self.timeframes)
        )
        total = sum(counts)
        self.aggregator.paused = False
        await self.aggregator.tick()
        log.info("ws_ready", gap_candles_backfilled=total)
        await self._write_health()

    async def _on_disconnect(self, reason: str) -> None:
        self.aggregator.paused = True
        await self._write_health()

    async def _blocked_until(self) -> int:
        raw = await self.redis.get(B.WS_BLOCK_UNTIL)
        return int(raw) if raw else 0

    # ------------------------------------------------------------ health

    async def check_clock(self) -> None:
        try:
            drift, rtt = await server_time_drift_ms(self.rest)
        except Exception as exc:
            log.warning("clock_check_failed", error=str(exc))
            return
        self.clock_drift_ms = drift
        if abs(drift) > MAX_CLOCK_DRIFT_MS:
            log.warning("clock_drift", drift_ms=drift, rtt_ms=rtt, limit_ms=MAX_CLOCK_DRIFT_MS)
        else:
            log.info("clock_ok", drift_ms=drift, rtt_ms=rtt)
        await self.redis.hset(
            self.health_key, mapping={"clock_drift_ms": drift, "clock_checked_at": B.now_ms()}
        )

    def _stale_streams(self) -> list[str]:
        if self.ws.state != "connected" or self.ws.connected_since is None:
            return []
        now = self.ws.clock()
        out = []
        for stream in self.streams:
            limit = STALE_MARK_S if "@markprice" in stream.lower() else STALE_KLINE_S
            # Measure from the newer of last message and (re)connect: messages from before an
            # outage must not make a freshly reconnected stream look stale.
            last = max(self.ws.last_message_at.get(stream, 0.0), self.ws.connected_since)
            if now - last > limit:
                out.append(stream)
        return out

    async def _write_health(self) -> None:
        stale = self._stale_streams()
        if stale and stale != self.stale:
            log.error("streams_stale", streams=stale)
        self.stale = stale
        degraded = bool(stale) or self.ws.state != "connected"
        if self.state != "stopped":
            self.state = "degraded" if degraded else "running"
        streams = {s: int(t * 1000) for s, t in self.ws.last_message_at.items()}
        pipe = self.redis.pipeline(transaction=False)
        if streams:
            pipe.hset(B.HEALTH_STREAMS, mapping=streams)
        pipe.hset(
            self.health_key,
            mapping={
                "state": self.state,
                "ws_state": self.ws.state,
                "heartbeat": B.now_ms(),
                "started_at": self.started_at,
                "connected_since": int(self.ws.connected_since * 1000)
                if self.ws.connected_since
                else 0,
                "reconnects": self.ws.reconnects,
                "last_disconnect_reason": self.ws.last_disconnect_reason,
                "stale_streams": json.dumps(stale),
                "messages": self.ws.messages,
                "events_emitted": self.aggregator.emitted,
                "events_degraded": self.aggregator.degraded,
                "duplicates_suppressed": self.aggregator.duplicates_suppressed,
                "candles_backfilled": self.klines.backfilled,
                "db_errors": self.klines.db_errors,
                "pid": os.getpid(),
                "symbols": json.dumps(self.symbols),
                "timeframes": json.dumps(self.timeframes),
            },
        )
        await pipe.execute()

    async def _watchdog(self, stop: asyncio.Event) -> None:
        last_clock = B.now_ms()
        while not stop.is_set():
            await self._write_health()
            if B.now_ms() - last_clock >= CLOCK_CHECK_S * 1000:
                last_clock = B.now_ms()
                await self.check_clock()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_S)

    # ------------------------------------------------------------ run

    async def start(self) -> None:
        await self.redis.set("deskmind:schema_version", B.SCHEMA_VERSION)
        await self.redis.hset(self.health_key, mapping={"state": "starting"})
        await self.check_clock()
        await self.aggregator.load_state()
        warmed = sum(
            await asyncio.gather(
                *(self.klines.warm_up(s, tf) for s in self.symbols for tf in self.timeframes)
            )
        )
        log.info("buffers_warm", candles=warmed, streams=len(self.streams))

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        tasks = [
            asyncio.create_task(self.ws.run(stop), name="ws"),
            asyncio.create_task(self.aggregator.run(stop), name="bar_close"),
            asyncio.create_task(self.oi.run(stop), name="open_interest"),
            asyncio.create_task(self._watchdog(stop), name="watchdog"),
        ]
        waiter = asyncio.create_task(stop.wait(), name="stop")
        try:
            done, _ = await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
            for t in done:  # a crashed task stops the service loudly
                if t is not waiter and not t.cancelled() and t.exception():
                    raise t.exception()  # type: ignore[misc]
        finally:
            stop.set()
            for t in [*tasks, waiter]:
                t.cancel()
            await asyncio.gather(*tasks, waiter, return_exceptions=True)
            self.state = "stopped"
            await self.redis.hset(
                self.health_key,
                mapping={"state": "stopped", "ws_state": "disconnected", "heartbeat": B.now_ms()},
            )
            log.info("live_service_stopped", events=self.aggregator.emitted)
