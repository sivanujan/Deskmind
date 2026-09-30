"""Persistence of trend items (trend_items), transitions (agent_item_transitions) and live reports
(agent_reports, retention window). Postgres (compressed hypertables) + in-memory."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deskmind.agents.transitions import MemoryTransitions, PgTransitions
from deskmind.agents.trend.agent import REPORT_TIMEFRAME
from deskmind.agents.trend.config import AGENT_NAME, AGENT_VERSION
from deskmind.agents.trend.models import as_of as item_as_of
from deskmind.db.models import AgentReport, TrendItem

BATCH = 1000


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _relevant(item: dict[str, Any], start_ms: int | None, end_ms: int | None) -> bool:
    """Item overlaps the [start, end] window (trend lines span from anchor to break)."""
    if end_ms is not None and item["pivot_time"] > end_ms:
        return False
    if start_ms is None or item["pivot_time"] >= start_ms:
        return True
    if item["kind"].startswith("trendline"):
        stop = item.get("broken_time") or item.get("expired_at")
        return stop is None or stop >= start_ms
    return False


class TrendStore(Protocol):
    async def upsert_items(
        self, symbol: str, items: Iterable[tuple[str, dict[str, Any]]], version: str
    ) -> int: ...
    async def upsert_reports(self, symbol: str, reports: Iterable[dict[str, Any]]) -> int: ...
    async def items(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None, as_of_ms: int | None
    ) -> list[dict[str, Any]]: ...
    async def latest_report(self, symbol: str, as_of_ms: int | None) -> dict[str, Any] | None: ...
    async def replace_transitions(self, symbol, version, from_ms, rows) -> int: ...
    async def append_transitions(self, symbol, version, rows) -> int: ...
    async def transitions(self, symbol, version, tf, upto_ms) -> list[dict[str, Any]]: ...
    async def prune_reports(self, symbol: str, before_ms: int) -> int: ...


class MemoryTrendStore(MemoryTransitions):
    def __init__(self) -> None:
        super().__init__()
        self.events: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.reports: dict[tuple[str, int], dict[str, Any]] = {}

    async def prune_reports(self, symbol: str, before_ms: int) -> int:
        old = [k for k in self.reports if k[0] == symbol and k[1] < before_ms]
        for k in old:
            del self.reports[k]
        return len(old)

    async def upsert_items(self, symbol, items, version) -> int:
        n = 0
        for tf, item in items:
            self.events[(symbol, tf, item["id"])] = item
            n += 1
        return n

    async def upsert_reports(self, symbol, reports) -> int:
        n = 0
        for r in reports:
            self.reports[(symbol, r["as_of_ms"])] = r
            n += 1
        return n

    async def items(self, symbol, tf, start_ms, end_ms, as_of_ms):
        out = []
        for (s, t, _), item in self.events.items():
            if s != symbol or t != tf or not _relevant(item, start_ms, end_ms):
                continue
            view = item_as_of(item, as_of_ms) if as_of_ms is not None else item
            if view is not None:
                out.append(view)
        return sorted(out, key=lambda i: (i["pivot_time"], i["id"]))

    async def latest_report(self, symbol, as_of_ms):
        keys = [t for s, t in self.reports if s == symbol and (as_of_ms is None or t <= as_of_ms)]
        return self.reports[(symbol, max(keys))] if keys else None


class PostgresTrendStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._trans = PgTransitions(self._sessions, AGENT_NAME)

    async def replace_transitions(self, symbol, version, from_ms, rows) -> int:
        return await self._trans.replace(symbol, version, from_ms, rows)

    async def append_transitions(self, symbol, version, rows) -> int:
        return await self._trans.append(symbol, version, rows)

    async def transitions(self, symbol, version, tf, upto_ms) -> list[dict[str, Any]]:
        return await self._trans.load(symbol, version, tf, upto_ms)

    async def prune_reports(self, symbol: str, before_ms: int) -> int:
        return await self._trans.prune_reports(symbol, AGENT_VERSION, before_ms)

    async def upsert_items(self, symbol, items, version) -> int:
        rows = [
            {
                "symbol": symbol,
                "timeframe": tf,
                "agent_version": version,
                "item_id": item["id"],
                "kind": item["kind"],
                "pivot_time": _dt(item["pivot_time"]),
                "confirmed_at": _dt(item["confirmed_at"]),
                "payload": item,
            }
            for tf, item in items
        ]
        async with self._sessions() as session:
            for i in range(0, len(rows), BATCH):
                stmt = insert(TrendItem).values(rows[i : i + BATCH])
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        "symbol",
                        "timeframe",
                        "agent_version",
                        "item_id",
                        "confirmed_at",
                    ],
                    set_={
                        "kind": stmt.excluded.kind,
                        "pivot_time": stmt.excluded.pivot_time,
                        "payload": stmt.excluded.payload,
                        "updated_at": datetime.now(UTC),
                    },
                )
                await session.execute(stmt)
            await session.commit()
        return len(rows)

    async def upsert_reports(self, symbol, reports) -> int:
        rows = [
            {
                "symbol": symbol,
                "timeframe": REPORT_TIMEFRAME,
                "agent_name": AGENT_NAME,
                "payload": r,
                "version": r["agent_version"],
                "as_of": _dt(r["as_of_ms"]),
            }
            for r in reports
        ]
        async with self._sessions() as session:
            for i in range(0, len(rows), BATCH):
                stmt = insert(AgentReport).values(rows[i : i + BATCH])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["agent_name", "symbol", "timeframe", "version", "as_of"],
                    index_where=AgentReport.as_of.is_not(None),
                    set_={"payload": stmt.excluded.payload},
                )
                await session.execute(stmt)
            await session.commit()
        return len(rows)

    async def items(self, symbol, tf, start_ms, end_ms, as_of_ms):
        q = select(TrendItem.payload).where(
            TrendItem.agent_version == AGENT_VERSION,  # never mix output of older rule sets
            TrendItem.symbol == symbol,
            TrendItem.timeframe == tf,
        )
        if as_of_ms is not None:
            q = q.where(TrendItem.confirmed_at <= _dt(as_of_ms))
        if end_ms is not None:
            q = q.where(TrendItem.pivot_time <= _dt(end_ms))
        if start_ms is not None:
            q = q.where(
                or_(TrendItem.pivot_time >= _dt(start_ms), TrendItem.kind.like("trendline%"))
            )
        async with self._sessions() as session:
            payloads = list((await session.execute(q)).scalars())
        out = []
        for item in payloads:
            if not _relevant(item, start_ms, end_ms):
                continue
            view = item_as_of(item, as_of_ms) if as_of_ms is not None else item
            if view is not None:
                out.append(view)
        return sorted(out, key=lambda i: (i["pivot_time"], i["id"]))

    async def latest_report(self, symbol, as_of_ms):
        q = select(AgentReport.payload).where(
            AgentReport.agent_name == AGENT_NAME,
            AgentReport.symbol == symbol,
            AgentReport.timeframe == REPORT_TIMEFRAME,
            AgentReport.version == AGENT_VERSION,
            AgentReport.as_of.is_not(None),
        )
        if as_of_ms is not None:
            q = q.where(AgentReport.as_of <= _dt(as_of_ms))
        q = q.order_by(AgentReport.as_of.desc()).limit(1)
        async with self._sessions() as session:
            return (await session.execute(q)).scalar_one_or_none()
