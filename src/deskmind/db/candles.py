"""Persist closed candles into the `candles` hypertable (idempotent upsert on the PK)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deskmind.db.models import Candle

if TYPE_CHECKING:
    from deskmind.data.live.buffers import Kline

UPSERT_BATCH = 1000


class CandleSink(Protocol):
    async def upsert(self, klines: Sequence[Kline]) -> None: ...


class PostgresCandleSink:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def upsert(self, klines: Sequence[Kline]) -> None:
        rows = [
            {
                "symbol": k.symbol,
                "timeframe": k.timeframe,
                "open_time": datetime.fromtimestamp(k.open_time / 1000, UTC),
                "open": k.open,
                "high": k.high,
                "low": k.low,
                "close": k.close,
                "volume": k.volume,
                "quote_volume": k.quote_volume,
            }
            for k in klines
            if k.is_closed
        ]
        if not rows:
            return
        async with self._sessions() as session:
            for i in range(0, len(rows), UPSERT_BATCH):
                stmt = insert(Candle).values(rows[i : i + UPSERT_BATCH])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol", "timeframe", "open_time"],
                    set_={
                        c: stmt.excluded[c]
                        for c in ("open", "high", "low", "close", "volume", "quote_volume")
                    },
                )
                await session.execute(stmt)
            await session.commit()

    async def fetch(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Candle]:
        async with self._sessions() as session:
            result = await session.execute(
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.open_time >= start,
                    Candle.open_time < end,
                )
                .order_by(Candle.open_time)
            )
            return list(result.scalars())


class MemoryCandleSink:
    """In-memory sink for tests."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], Kline] = {}
        self.calls = 0

    async def upsert(self, klines: Sequence[Kline]) -> None:
        self.calls += 1
        for k in klines:
            if k.is_closed:
                self.rows[(k.symbol, k.timeframe, k.open_time)] = k
