"""Persistence of ICT/SMC items (agent_events) and reports (agent_reports). Postgres + in-memory."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deskmind.agents.ict_smc.agent import REPORT_TIMEFRAME
from deskmind.agents.ict_smc.config import AGENT_NAME, AGENT_VERSION
from deskmind.agents.ict_smc.models import as_of as item_as_of
from deskmind.db.models import AgentEvent, AgentReport

BATCH = 1000


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _relevant(item: dict[str, Any], start_ms: int | None, end_ms: int | None) -> bool:
    """Item overlaps the [start, end] window."""
    pivot = item.get("pivot_time", item.get("created_time", 0))
    if end_ms is not None and pivot > end_ms:
        return False
    if start_ms is None or pivot >= start_ms:
        return True
    stop = item.get("expired_at") or item.get("taken_at") or item.get("filled_at")
    return stop is None or stop >= start_ms


class IctSmcStore(Protocol):
    async def upsert_items(
        self, symbol: str, items: Iterable[tuple[str, dict[str, Any]]], version: str
    ) -> int: ...
    async def upsert_reports(self, symbol: str, reports: Iterable[dict[str, Any]]) -> int: ...
    async def items(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None, as_of_ms: int | None
    ) -> list[dict[str, Any]]: ...
    async def latest_report(self, symbol: str, as_of_ms: int | None) -> dict[str, Any] | None: ...


class MemoryIctSmcStore:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.reports: dict[tuple[str, int], dict[str, Any]] = {}

    async def upsert_items(
        self, symbol: str, items: Iterable[tuple[str, dict[str, Any]]], version: str
    ) -> int:
        n = 0
        for tf, item in items:
            self.events[(symbol, tf, item["id"])] = item
            n += 1
        return n

    async def upsert_reports(self, symbol: str, reports: Iterable[dict[str, Any]]) -> int:
        n = 0
        for r in reports:
            self.reports[(symbol, r["as_of_ms"])] = r
            n += 1
        return n

    async def items(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None, as_of_ms: int | None
    ) -> list[dict[str, Any]]:
        out = []
        for (s, t, _), item in self.events.items():
            if s != symbol or t != tf or not _relevant(item, start_ms, end_ms):
                continue
            view = item_as_of(item, as_of_ms) if as_of_ms is not None else item
            if view is not None:
                out.append(view)
        return sorted(out, key=lambda i: (i.get("pivot_time", i.get("created_time", 0)), i["id"]))

    async def latest_report(self, symbol: str, as_of_ms: int | None) -> dict[str, Any] | None:
        keys = [t for s, t in self.reports if s == symbol and (as_of_ms is None or t <= as_of_ms)]
        return self.reports[(symbol, max(keys))] if keys else None


class PostgresIctSmcStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def upsert_items(
        self, symbol: str, items: Iterable[tuple[str, dict[str, Any]]], version: str
    ) -> int:
        rows = [
            {
                "agent": AGENT_NAME,
                "symbol": symbol,
                "timeframe": tf,
                "item_id": item["id"],
                "kind": item["kind"],
                "pivot_time": _dt(item.get("pivot_time", item.get("created_time", 0))),
                "confirmed_at": _dt(item["confirmed_at"]),
                "agent_version": version,
                "payload": item,
            }
            for tf, item in items
        ]
        async with self._sessions() as session:
            for i in range(0, len(rows), BATCH):
                stmt = insert(AgentEvent).values(rows[i : i + BATCH])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["agent", "symbol", "timeframe", "item_id"],
                    set_={
                        "kind": stmt.excluded.kind,
                        "pivot_time": stmt.excluded.pivot_time,
                        "confirmed_at": stmt.excluded.confirmed_at,
                        "agent_version": stmt.excluded.agent_version,
                        "payload": stmt.excluded.payload,
                        "updated_at": datetime.now(UTC),
                    },
                )
                await session.execute(stmt)
            await session.commit()
        return len(rows)

    async def upsert_reports(self, symbol: str, reports: Iterable[dict[str, Any]]) -> int:
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

    async def items(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None, as_of_ms: int | None
    ) -> list[dict[str, Any]]:
        q = select(AgentEvent.payload).where(
            AgentEvent.agent == AGENT_NAME,
            AgentEvent.agent_version == AGENT_VERSION,
            AgentEvent.symbol == symbol,
            AgentEvent.timeframe == tf,
        )
        if as_of_ms is not None:
            q = q.where(AgentEvent.confirmed_at <= _dt(as_of_ms))
        if end_ms is not None:
            q = q.where(AgentEvent.pivot_time <= _dt(end_ms))
        if start_ms is not None:
            q = q.where(
                or_(
                    AgentEvent.pivot_time >= _dt(start_ms),
                    AgentEvent.kind.like("pool%"),
                    AgentEvent.kind.like("fvg%"),
                    AgentEvent.kind.like("ob%"),
                    AgentEvent.kind.like("breaker%"),
                )
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
        return sorted(out, key=lambda i: (i.get("pivot_time", i.get("created_time", 0)), i["id"]))

    async def latest_report(self, symbol: str, as_of_ms: int | None) -> dict[str, Any] | None:
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
