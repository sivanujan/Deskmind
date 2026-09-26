"""Candle source for the chart: Parquet history (DuckDB) + recent Postgres rows, merged.

Parquet is the verified history (updated by `deskmind data update`); Postgres `candles` holds
what the live service stored since. Rows are merged by open time (Postgres wins on overlap,
it is the newer copy of the same exchange candle), de-duplicated, closed candles only.
Read-only: no writes anywhere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deskmind.api.schemas import CandleOut
from deskmind.data.datasets import TIMEFRAME_MS, Dataset
from deskmind.data.storage import Store, duckdb_connect, scan_sql
from deskmind.db.models import Candle

MAX_LIMIT = 5000


class RecentCandleSource(Protocol):
    async def fetch(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int, limit: int
    ) -> list[CandleOut]: ...


class PostgresRecentCandles:
    """Latest `limit` candles with start <= open_time < end from the `candles` hypertable."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def fetch(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int, limit: int
    ) -> list[CandleOut]:
        q = select(Candle).where(
            Candle.symbol == symbol,
            Candle.timeframe == tf,
            Candle.open_time < datetime.fromtimestamp(end_ms / 1000, UTC),
        )
        if start_ms is not None:
            q = q.where(Candle.open_time >= datetime.fromtimestamp(start_ms / 1000, UTC))
        q = q.order_by(Candle.open_time.desc()).limit(limit)
        async with self._sessions() as session:
            rows = list((await session.execute(q)).scalars())
        return [
            CandleOut(
                time=int(r.open_time.timestamp()),
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(r.volume),
                source="postgres",
            )
            for r in reversed(rows)
        ]


def parquet_candles(
    data_dir: Path, symbol: str, tf: str, start_ms: int | None, end_ms: int, limit: int
) -> list[CandleOut]:
    store = Store(data_dir, symbol)
    ds = Dataset("klines", tf)
    if not store.files(ds):
        return []
    where = [f"epoch_ms(open_time) < {int(end_ms)}"]
    if start_ms is not None:
        where.append(f"epoch_ms(open_time) >= {int(start_ms)}")
    con = duckdb_connect()
    try:
        rows = con.execute(
            f"""SELECT epoch_ms(open_time), open, high, low, close, volume
                FROM {scan_sql(store, ds)} WHERE {" AND ".join(where)}
                ORDER BY open_time DESC LIMIT {int(limit)}"""
        ).fetchall()
    finally:
        con.close()
    return [
        CandleOut(time=t // 1000, open=o, high=h, low=lo, close=c, volume=v, source="parquet")
        for t, o, h, lo, c, v in reversed(rows)
    ]


class CandleService:
    def __init__(self, data_dir: Path, recent: RecentCandleSource | None) -> None:
        self.data_dir = data_dir
        self.recent = recent

    async def candles(
        self,
        symbol: str,
        tf: str,
        *,
        start_ms: int | None,
        end_ms: int | None,
        limit: int,
        now_ms: int,
    ) -> list[CandleOut]:
        iv = TIMEFRAME_MS[tf]
        closed_before = (now_ms // iv) * iv  # the forming candle is never returned
        end = min(end_ms, closed_before) if end_ms is not None else closed_before
        limit = max(1, min(limit, MAX_LIMIT))
        hist = await asyncio.to_thread(
            parquet_candles, self.data_dir, symbol, tf, start_ms, end, limit
        )
        recent = await self.recent.fetch(symbol, tf, start_ms, end, limit) if self.recent else []
        merged = {c.time: c for c in hist}
        merged.update({c.time: c for c in recent})
        return [merged[t] for t in sorted(merged)][-limit:]
