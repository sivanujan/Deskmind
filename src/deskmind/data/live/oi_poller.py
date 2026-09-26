"""Open interest has no WebSocket stream: poll REST /fapi/v1/openInterest per symbol."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

from redis.asyncio import Redis

from deskmind.data.http_client import BinanceIPBanned, RateLimitedClient
from deskmind.data.live import buffers as B
from deskmind.data.live.rest import fetch_open_interest
from deskmind.logging import get_logger

log = get_logger(__name__)

OI_POLL_INTERVAL_S = 300


class OpenInterestPoller:
    def __init__(
        self,
        redis: Redis,
        client: RateLimitedClient,
        symbols: Iterable[str],
        interval_s: float = OI_POLL_INTERVAL_S,
    ) -> None:
        self.redis = redis
        self.client = client
        self.symbols = list(symbols)
        self.interval_s = interval_s
        self.polls = 0
        self.errors = 0

    async def poll_once(self) -> None:
        for symbol in self.symbols:
            try:
                oi = await fetch_open_interest(self.client, symbol)
            except BinanceIPBanned:
                raise
            except Exception as exc:
                self.errors += 1
                log.warning("open_interest_poll_failed", symbol=symbol, error=str(exc))
                continue
            await B.put_json_if_newer(
                self.redis, B.oi_key(symbol), {**oi, "received_at": B.now_ms()}, "time"
            )
        self.polls += 1

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
