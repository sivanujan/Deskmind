"""Closed candles for agents: Parquet history + recent Postgres rows (same merge as the API),
and the Redis live buffers. Floats are fine here: analysis only, never money logic."""

from __future__ import annotations

import asyncio
from pathlib import Path

from redis.asyncio import Redis

from deskmind.agents.trend.models import Bar
from deskmind.api.candles import RecentCandleSource, parquet_candles
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B

ALL = 10**9


async def load_bars(
    data_dir: Path,
    recent: RecentCandleSource | None,
    symbol: str,
    tf: str,
    *,
    start_ms: int | None = None,
    end_ms: int,
    last_n: int | None = None,
) -> list[Bar]:
    """Closed candles with start <= open_time and close_time <= end_ms, oldest first.

    last_n: only the newest n of them (warm-up).
    """
    iv = TIMEFRAME_MS[tf]
    open_before = end_ms - iv + 1  # close_time <= end_ms
    limit = last_n or ALL
    hist = await asyncio.to_thread(
        parquet_candles, data_dir, symbol, tf, start_ms, open_before, limit
    )
    rows = {c.time * 1000: c for c in hist}
    if recent is not None:
        for c in await recent.fetch(symbol, tf, start_ms, open_before, limit):
            rows[c.time * 1000] = c
    bars = [Bar(t, t + iv, c.open, c.high, c.low, c.close) for t, c in sorted(rows.items())]
    return bars[-last_n:] if last_n else bars


async def redis_bars(redis: Redis, symbol: str, tf: str, n: int) -> list[Bar]:
    """Newest n closed candles from the live buffer."""
    iv = TIMEFRAME_MS[tf]
    return [
        Bar(
            k.open_time,
            k.open_time + iv,
            float(k.open),
            float(k.high),
            float(k.low),
            float(k.close),
        )
        for k in await B.get_candles(redis, symbol, tf, n)
    ]
