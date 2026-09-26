"""Rate-limited async HTTP client for Binance public endpoints.

- Tracks request weight per minute (local accounting + the X-MBX-USED-WEIGHT-1M header) and waits
  for the next minute before exceeding a safety budget.
- 429: honours Retry-After, then retries with backoff.
- 418 (IP banned): raises BinanceIPBanned immediately; never retried.
- 5xx / network errors: exponential backoff with jitter, bounded retries.

Deliberately unauthenticated: there is no way to pass API keys or sign requests through this client.
It is reused later by the live feed; signed execution calls will get their own client.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from deskmind.logging import get_logger

log = get_logger(__name__)

WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
DEFAULT_WEIGHT_LIMIT = 2400  # USDT-M futures REQUEST_WEIGHT per minute per IP


class BinanceHTTPError(Exception):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class BinanceIPBanned(BinanceHTTPError):
    """HTTP 418: the IP is banned for ignoring 429s. Stop everything."""


class WeightTracker:
    """Per-minute request-weight budget shared by all requests of one client."""

    def __init__(
        self,
        limit: int = DEFAULT_WEIGHT_LIMIT,
        safety: float = 0.8,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.budget = int(limit * safety)
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._minute = -1
        self.used = 0

    def _roll(self) -> None:
        minute = int(self._clock() // 60)
        if minute != self._minute:
            self._minute = minute
            self.used = 0

    async def acquire(self, weight: int) -> None:
        if weight <= 0:
            return
        async with self._lock:
            while True:
                self._roll()
                if self.used + weight <= self.budget:
                    self.used += weight
                    return
                wait = 60 - (self._clock() % 60) + 0.5
                log.info(
                    "rate_limit_wait", used=self.used, budget=self.budget, wait_s=round(wait, 1)
                )
                await self._sleep(wait)

    def observe(self, header_value: str | None) -> None:
        """Server-reported usage for the current minute wins if it is higher than our count."""
        if header_value is None:
            return
        try:
            reported = int(header_value)
        except ValueError:
            return
        self._roll()
        self.used = max(self.used, reported)


class RateLimitedClient:
    def __init__(
        self,
        base_url: str,
        *,
        weight_limit: int | None = DEFAULT_WEIGHT_LIMIT,
        max_retries: int = 5,
        max_concurrency: int = 8,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, transport=transport, follow_redirects=True
        )
        self.tracker = (
            WeightTracker(weight_limit, clock=clock, sleep=sleep) if weight_limit else None
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._sleep = sleep
        self.max_retries = max_retries
        self.request_count = 0

    async def __aenter__(self) -> RateLimitedClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def backoff_s(attempt: int) -> float:
        return min(0.5 * 2**attempt, 30.0) + random.uniform(0, 0.25)

    async def _request(
        self, url: str, params: dict[str, Any] | None, weight: int, allow_404: bool
    ) -> httpx.Response | None:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.tracker:
                await self.tracker.acquire(weight)
            try:
                async with self._sem:
                    self.request_count += 1
                    resp = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("http_transport_error", url=url, attempt=attempt, error=str(exc))
                await self._sleep(self.backoff_s(attempt))
                continue

            if self.tracker:
                self.tracker.observe(resp.headers.get(WEIGHT_HEADER))
            status = resp.status_code
            if status == 418:
                raise BinanceIPBanned(status, str(resp.url), resp.text)
            if status == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else self.backoff_s(attempt + 3)
                log.warning("http_429", url=url, attempt=attempt, wait_s=wait)
                last_exc = BinanceHTTPError(status, str(resp.url), resp.text)
                await self._sleep(wait)
                continue
            if status >= 500:
                log.warning("http_5xx", url=url, status=status, attempt=attempt)
                last_exc = BinanceHTTPError(status, str(resp.url), resp.text)
                await self._sleep(self.backoff_s(attempt))
                continue
            if status == 404 and allow_404:
                return None
            if status >= 400:
                raise BinanceHTTPError(status, str(resp.url), resp.text)
            return resp
        assert last_exc is not None
        raise last_exc

    async def get_json(
        self, path: str, params: dict[str, Any] | None = None, weight: int = 1
    ) -> Any:
        resp = await self._request(path, params, weight, allow_404=False)
        assert resp is not None
        return resp.json()

    async def get_bytes(self, url: str, weight: int = 0) -> bytes | None:
        """GET raw bytes; returns None on 404."""
        resp = await self._request(url, None, weight, allow_404=True)
        return None if resp is None else resp.content
