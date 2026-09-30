"""Persistence for Price Action items: items + transitions, live reports.

Follows the ICT/SMC store pattern.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deskmind.agents.price_action.config import AGENT_NAME, AGENT_VERSION
from deskmind.agents.price_action.models import as_of as item_as_of
from deskmind.db.models import AgentItemTransition, AgentReport, PriceActionItem

BATCH_ITEMS = 500
BATCH_REPORTS = 100
BATCH_TRANSITIONS = 1000


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _relevant(item: dict[str, Any], tf: str, start_ms: int | None, end_ms: int | None) -> bool:
    if item.get("timeframe") and item["timeframe"] != tf:
        return False
    pivot = item.get("created_time", 0)
    if end_ms is not None and pivot > end_ms:
        return False
    if start_ms is None or pivot >= start_ms:
        return True
    stop = item.get("end_time") or item.get("retired_at")
    return stop is None or stop >= start_ms


class PriceActionStore(Protocol):
    async def upsert_items(
        self, symbol: str, tf: str, items: Iterable[dict[str, Any]], version: str
    ) -> int: ...
    async def upsert_reports(self, symbol: str, tf: str, reports: Iterable[dict[str, Any]]) -> int: ...
    async def items(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None, as_of_ms: int | None
    ) -> list[dict[str, Any]]: ...
    async def latest_report(
        self, symbol: str, tf: str, as_of_ms: int | None
    ) -> dict[str, Any] | None: ...
    async def replace_transitions(
        self, symbol: str, version: str, tf: str, from_ms: int, rows: list[dict[str, Any]]
    ) -> int: ...
    async def append_transitions(
        self, symbol: str, version: str, tf: str, rows: list[dict[str, Any]]
    ) -> int: ...
    async def transitions(
        self, symbol: str, version: str, tf: str, upto_ms: int
    ) -> list[dict[str, Any]]: ...
    async def prune_reports(self, symbol: str, before_ms: int) -> int: ...


def _sorted_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (r["at"], r["ord"]))


class MemoryPriceActionStore:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str, str], dict[str, Any]] = {}  # (sym, tf, item_id)
        self.reports: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.trans: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    async def replace_transitions(
        self, symbol: str, version: str, tf: str, from_ms: int, rows: list[dict[str, Any]]
    ) -> int:
        key = (symbol, version, tf)
        kept = [r for r in self.trans.get(key, []) if r["at"] < from_ms]
        self.trans[key] = _sorted_rows([*kept, *rows])
        return len(rows)

    async def append_transitions(
        self, symbol: str, version: str, tf: str, rows: list[dict[str, Any]]
    ) -> int:
        key = (symbol, version, tf)
        cur = self.trans.setdefault(key, [])
        seen = {(r["at"], r["ord"]) for r in cur}
        new = [r for r in rows if (r["at"], r["ord"]) not in seen]
        self.trans[key] = _sorted_rows([*cur, *new])
        return len(new)

    async def transitions(
        self, symbol: str, version: str, tf: str, upto_ms: int
    ) -> list[dict[str, Any]]:
        from deskmind.agents.transitions import STATE_BASELINE
        rows = [
            r for r in self.trans.get((symbol, version, tf), []) if r["at"] <= upto_ms
        ]
        starts = [i for i, r in enumerate(rows) if r["item_id"] == STATE_BASELINE]
        return rows[starts[-1]:] if starts else rows

    async def prune_reports(self, symbol: str, before_ms: int) -> int:
        old = [k for k in self.reports if k[0] == symbol and k[2] < before_ms]
        for k in old:
            del self.reports[k]
        return len(old)

    async def upsert_items(
        self, symbol: str, tf: str, items: Iterable[dict[str, Any]], version: str
    ) -> int:
        n = 0
        for item in items:
            self.events[(symbol, tf, item["id"])] = item
            n += 1
        return n

    async def upsert_reports(
        self, symbol: str, tf: str, reports: Iterable[dict[str, Any]]
    ) -> int:
        n = 0
        for r in reports:
            self.reports[(symbol, tf, r["as_of_ms"])] = r
            n += 1
        return n

    async def items(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None, as_of_ms: int | None
    ) -> list[dict[str, Any]]:
        out = []
        for (s, t, _), item in self.events.items():
            if s != symbol or t != tf or not _relevant(item, tf, start_ms, end_ms):
                continue
            view = item_as_of(item, as_of_ms) if as_of_ms is not None else item
            if view is not None:
                out.append(view)
        return sorted(out, key=lambda i: i.get("created_time", 0))

    async def latest_report(
        self, symbol: str, tf: str, as_of_ms: int | None
    ) -> dict[str, Any] | None:
        keys = [
            t for s, f, t in self.reports
            if s == symbol and f == tf and (as_of_ms is None or t <= as_of_ms)
        ]
        return self.reports[(symbol, tf, max(keys))] if keys else None


class PostgresPriceActionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def upsert_items(
        self, symbol: str, tf: str, items: Iterable[dict[str, Any]], version: str
    ) -> int:
        rows = [
            {
                "symbol": symbol,
                "timeframe": tf,
                "agent_version": version,
                "item_id": item["id"],
                "kind": item["kind"],
                "pivot_time": _dt(item.get("created_time", 0)),
                "confirmed_at": _dt(item["confirmed_at"]),
                "payload": item,
            }
            for item in items
        ]
        async with self._sessions() as session:
            for i in range(0, len(rows), BATCH_ITEMS):
                stmt = insert(PriceActionItem).values(rows[i: i + BATCH_ITEMS])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol", "timeframe", "agent_version", "item_id", "confirmed_at"],
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

    async def upsert_reports(
        self, symbol: str, tf: str, reports: Iterable[dict[str, Any]]
    ) -> int:
        rows = [
            {
                "symbol": symbol,
                "timeframe": tf,
                "agent_name": AGENT_NAME,
                "payload": r,
                "version": r["agent_version"],
                "as_of": _dt(r["as_of_ms"]),
            }
            for r in reports
        ]
        async with self._sessions() as session:
            for i in range(0, len(rows), BATCH_REPORTS):
                stmt = insert(AgentReport).values(rows[i: i + BATCH_REPORTS])
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
        q = select(PriceActionItem.payload).where(
            PriceActionItem.agent_version == AGENT_VERSION,
            PriceActionItem.symbol == symbol,
            PriceActionItem.timeframe == tf,
        )
        if as_of_ms is not None:
            q = q.where(PriceActionItem.confirmed_at <= _dt(as_of_ms))
        if end_ms is not None:
            q = q.where(PriceActionItem.pivot_time <= _dt(end_ms))
        async with self._sessions() as session:
            payloads = list((await session.execute(q)).scalars())
        out = []
        for item in payloads:
            if not _relevant(item, tf, start_ms, end_ms):
                continue
            view = item_as_of(item, as_of_ms) if as_of_ms is not None else item
            if view is not None:
                out.append(view)
        return sorted(out, key=lambda i: i.get("created_time", 0))

    async def latest_report(
        self, symbol: str, tf: str, as_of_ms: int | None
    ) -> dict[str, Any] | None:
        q = select(AgentReport.payload).where(
            AgentReport.agent_name == AGENT_NAME,
            AgentReport.symbol == symbol,
            AgentReport.timeframe == tf,
            AgentReport.version == AGENT_VERSION,
            AgentReport.as_of.is_not(None),
        )
        if as_of_ms is not None:
            q = q.where(AgentReport.as_of <= _dt(as_of_ms))
        q = q.order_by(AgentReport.as_of.desc()).limit(1)
        async with self._sessions() as session:
            return (await session.execute(q)).scalar_one_or_none()

    def _tr_values(
        self, symbol: str, version: str, tf: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "agent": AGENT_NAME,
                "symbol": symbol,
                "agent_version": version,
                "timeframe": tf,
                "at": _dt(r["at"]),
                "ord": r["ord"],
                "item_id": r["item_id"],
                "kind": r["kind"],
                "payload": r["payload"],
            }
            for r in rows
        ]

    async def _ins(self, session: Any, values: list[dict[str, Any]]) -> None:
        for i in range(0, len(values), BATCH_TRANSITIONS):
            stmt = insert(AgentItemTransition).values(values[i: i + BATCH_TRANSITIONS])
            await session.execute(
                stmt.on_conflict_do_nothing(
                    index_elements=["agent", "symbol", "agent_version", "timeframe", "at", "ord"]
                )
            )

    async def replace_transitions(
        self, symbol: str, version: str, tf: str, from_ms: int, rows: list[dict[str, Any]]
    ) -> int:
        async with self._sessions() as session:
            await session.execute(
                delete(AgentItemTransition).where(
                    AgentItemTransition.agent == AGENT_NAME,
                    AgentItemTransition.symbol == symbol,
                    AgentItemTransition.agent_version == version,
                    AgentItemTransition.timeframe == tf,
                    AgentItemTransition.at >= _dt(from_ms),
                )
            )
            await self._ins(session, self._tr_values(symbol, version, tf, rows))
            await session.commit()
        return len(rows)

    async def append_transitions(
        self, symbol: str, version: str, tf: str, rows: list[dict[str, Any]]
    ) -> int:
        if not rows:
            return 0
        async with self._sessions() as session:
            await self._ins(session, self._tr_values(symbol, version, tf, rows))
            await session.commit()
        return len(rows)

    async def transitions(
        self, symbol: str, version: str, tf: str, upto_ms: int
    ) -> list[dict[str, Any]]:
        base = AgentItemTransition
        last_baseline = (
            select(base.at)
            .where(
                base.agent == AGENT_NAME,
                base.symbol == symbol,
                base.agent_version == version,
                base.timeframe == tf,
                base.item_id == "state:baseline",
                base.at <= _dt(upto_ms),
            )
            .order_by(base.at.desc())
            .limit(1)
            .scalar_subquery()
        )
        q = (
            select(
                AgentItemTransition.at,
                AgentItemTransition.ord,
                AgentItemTransition.item_id,
                AgentItemTransition.kind,
                AgentItemTransition.payload,
            )
            .where(
                AgentItemTransition.agent == AGENT_NAME,
                AgentItemTransition.symbol == symbol,
                AgentItemTransition.agent_version == version,
                AgentItemTransition.timeframe == tf,
                AgentItemTransition.at <= _dt(upto_ms),
                AgentItemTransition.at >= last_baseline,
            )
            .order_by(AgentItemTransition.at, AgentItemTransition.ord)
        )
        async with self._sessions() as session:
            res = (await session.execute(q)).all()
        return [
            {"tf": tf, "at": int(at.timestamp() * 1000), "ord": o, "item_id": i, "kind": k, "payload": p}
            for at, o, i, k, p in res
        ]

    async def prune_reports(self, symbol: str, before_ms: int) -> int:
        async with self._sessions() as session:
            res = await session.execute(
                delete(AgentReport).where(
                    AgentReport.agent_name == AGENT_NAME,
                    AgentReport.symbol == symbol,
                    AgentReport.version == AGENT_VERSION,
                    AgentReport.as_of < _dt(before_ms),
                )
            )
            await session.commit()
        return res.rowcount or 0
