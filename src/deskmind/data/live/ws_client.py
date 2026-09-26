"""Combined-stream WebSocket client with reconnect, proactive rotation and staleness tracking.

Binance USDT-M market streams (checked against the current docs):
- kline and markPrice streams are routed on `wss://fstream.binance.com/market`; unrouted
  connections no longer receive them. Combined mode: `/market/stream?streams=a/b/c`, messages
  wrapped as {"stream": name, "data": payload}. Stream names are lower-case.
- A connection lives at most 24h: we rotate proactively before that (`max_age_s`).
- The server pings every 3 minutes; the websockets library answers pongs automatically.
- Max 1024 streams per connection, max 10 incoming client messages/s (we send none).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from deskmind.logging import get_logger

log = get_logger(__name__)

MAX_STREAMS_PER_CONNECTION = 1024
DEFAULT_MAX_AGE_S = 23 * 3600  # rotate well before Binance's 24h cut-off
DEFAULT_SILENCE_S = 60.0  # no message at all for this long -> connection is dead, reconnect


class WsConnection(Protocol):
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str], AbstractAsyncContextManager[WsConnection]]
MessageHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


def default_connector(url: str) -> AbstractAsyncContextManager[WsConnection]:
    from websockets.asyncio.client import connect

    return connect(url, ping_interval=60, ping_timeout=60, max_queue=4096, open_timeout=15)


class CombinedStreamClient:
    def __init__(
        self,
        base_url: str,
        streams: list[str],
        on_message: MessageHandler,
        *,
        on_connect: Callable[[], Awaitable[None]] | None = None,
        on_disconnect: Callable[[str], Awaitable[None]] | None = None,
        connector: Connector = default_connector,
        blocked_until: Callable[[], Awaitable[int]] | None = None,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        silence_s: float = DEFAULT_SILENCE_S,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_backoff_s: float = 60.0,
    ) -> None:
        if not streams or len(streams) > MAX_STREAMS_PER_CONNECTION:
            raise ValueError(f"need 1..{MAX_STREAMS_PER_CONNECTION} streams, got {len(streams)}")
        # Stream names are case-sensitive after the symbol (e.g. `btcusdt@markPrice@1s`):
        # callers pass them exactly; only the symbol part is lower-case.
        self.url = f"{base_url.rstrip('/')}/stream?streams=" + "/".join(streams)
        self.streams = list(streams)
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.connector = connector
        self.blocked_until = blocked_until
        self.max_age_s = max_age_s
        self.silence_s = silence_s
        self.clock = clock
        self.sleep = sleep
        self.max_backoff_s = max_backoff_s
        self.state = "disconnected"
        self.connected_since: float | None = None
        self.last_message_at: dict[str, float] = {}
        self.reconnects = 0
        self.messages = 0
        self.last_disconnect_reason = ""
        self._block_checked_at = float("-inf")
        self._block_until_ms = 0

    def backoff_s(self, attempt: int) -> float:
        return min(self.max_backoff_s, 1.0 * 2**attempt) + random.uniform(0, 0.5)

    async def _blocked(self) -> bool:
        """Test hook (simulated outage). Checked at most once per second."""
        if self.blocked_until is None:
            return False
        now = self.clock()
        if now - self._block_checked_at >= 1.0:
            self._block_checked_at = now
            self._block_until_ms = await self.blocked_until()
        return self._block_until_ms > int(now * 1000)

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            if await self._blocked():
                self.state = "blocked"
                await self.sleep(1.0)
                continue
            reason = "closed"
            self.state = "connecting"
            try:
                async with self.connector(self.url) as ws:
                    self.state = "connected"
                    self.connected_since = self.clock()
                    attempt = 0
                    log.info("ws_connected", url=self.url.split("?")[0], streams=len(self.streams))
                    if self.on_connect:
                        await self.on_connect()
                    reason = await self._receive(ws, stop)
                    if reason != "stop":
                        await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            self.state = "disconnected"
            self.connected_since = None
            if stop.is_set():
                break
            self.reconnects += 1
            self.last_disconnect_reason = reason
            log.warning("ws_disconnected", reason=reason, reconnects=self.reconnects)
            if self.on_disconnect:
                await self.on_disconnect(reason)
            if reason == "rotate":
                continue  # planned rotation: reconnect immediately
            await self.sleep(self.backoff_s(attempt))
            attempt += 1

    async def _receive(self, ws: WsConnection, stop: asyncio.Event) -> str:
        last_any = self.clock()
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                raw = None
            now = self.clock()
            if raw is not None:
                last_any = now
                msg = json.loads(raw)
                stream, data = msg.get("stream"), msg.get("data")
                if stream and isinstance(data, dict):
                    self.messages += 1
                    self.last_message_at[stream] = now
                    await self.on_message(stream, data)
            if self.connected_since is not None and now - self.connected_since >= self.max_age_s:
                log.info("ws_rotating", age_h=round((now - self.connected_since) / 3600, 2))
                return "rotate"
            if now - last_any >= self.silence_s:
                return f"silent for {self.silence_s:.0f}s"
            if await self._blocked():
                return "blocked by control key (simulated outage)"
        return "stop"
