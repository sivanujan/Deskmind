"""Where universe states and daily run summaries are kept."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

from deskmind.universe.history import MembershipHistory, MembershipRow
from deskmind.universe.models import SymbolRecord

_FIELDS = (
    "state",
    "rank",
    "median_quote_volume",
    "onboard_ms",
    "member",
    "data_verified",
    "warmed_up",
    "reason",
    "entered_at_ms",
    "exited_at_ms",
    "state_changed_at_ms",
    "history",
)


class UniverseStore(Protocol):
    async def load(self) -> dict[str, SymbolRecord]: ...
    async def save(self, rec: SymbolRecord) -> None: ...
    async def save_run(self, started: datetime, finished: datetime, summary: dict) -> None: ...
    async def runs(self, limit: int = 30) -> list[dict[str, Any]]: ...
    async def save_membership(self, rows: list[MembershipRow], source: str) -> None: ...
    async def save_delistings(self, delisted_at_ms: dict[str, int], source: str) -> None: ...
    async def load_history(self, start: date, end: date) -> MembershipHistory: ...


class MemoryUniverseStore:
    def __init__(self) -> None:
        self.records: dict[str, SymbolRecord] = {}
        self.run_log: list[dict[str, Any]] = []
        self.membership: dict[tuple[date, str], MembershipRow] = {}
        self.delistings: dict[str, int] = {}

    async def load(self) -> dict[str, SymbolRecord]:
        return copy.deepcopy(self.records)

    async def save(self, rec: SymbolRecord) -> None:
        self.records[rec.symbol] = copy.deepcopy(rec)

    async def save_run(self, started: datetime, finished: datetime, summary: dict) -> None:
        self.run_log.append(
            {"started_at": started.isoformat(), "finished_at": finished.isoformat(), **summary}
        )

    async def runs(self, limit: int = 30) -> list[dict[str, Any]]:
        return list(reversed(self.run_log))[:limit]

    async def save_membership(self, rows: list[MembershipRow], source: str) -> None:
        for r in rows:
            self.membership[(r.day, r.symbol)] = r

    async def save_delistings(self, delisted_at_ms: dict[str, int], source: str) -> None:
        self.delistings.update(delisted_at_ms)

    async def load_history(self, start: date, end: date) -> MembershipHistory:
        rows = [r for (d, _), r in self.membership.items() if start <= d <= end]
        return MembershipHistory(rows, dict(self.delistings))


class PostgresUniverseStore:
    async def load(self) -> dict[str, SymbolRecord]:
        from sqlalchemy import select

        from deskmind.db.models import UniverseSymbol
        from deskmind.db.session import session_scope

        async with session_scope() as s:
            rows = (await s.execute(select(UniverseSymbol))).scalars().all()
        out = {}
        for r in rows:
            vals = {f: getattr(r, f) for f in _FIELDS}
            if vals["median_quote_volume"] is not None:
                vals["median_quote_volume"] = float(vals["median_quote_volume"])
            out[r.symbol] = SymbolRecord(symbol=r.symbol, **vals)
        return out

    async def save(self, rec: SymbolRecord) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from deskmind.db.models import UniverseSymbol
        from deskmind.db.session import session_scope

        vals = {f: getattr(rec, f) for f in _FIELDS}
        if vals["median_quote_volume"] is not None:
            vals["median_quote_volume"] = Decimal(str(round(vals["median_quote_volume"], 4)))
        vals["updated_at"] = datetime.now(UTC)
        stmt = insert(UniverseSymbol).values(symbol=rec.symbol, **vals)
        stmt = stmt.on_conflict_do_update(index_elements=["symbol"], set_=vals)
        async with session_scope() as s:
            await s.execute(stmt)

    async def save_run(self, started: datetime, finished: datetime, summary: dict) -> None:
        from deskmind.db.models import UniverseRun
        from deskmind.db.session import session_scope

        async with session_scope() as s:
            s.add(UniverseRun(started_at=started, finished_at=finished, summary=summary))

    async def runs(self, limit: int = 30) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from deskmind.db.models import UniverseRun
        from deskmind.db.session import session_scope

        async with session_scope() as s:
            rows = (
                (
                    await s.execute(
                        select(UniverseRun).order_by(UniverseRun.started_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                **r.summary,
            }
            for r in rows
        ]

    async def save_membership(self, rows: list[MembershipRow], source: str) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from deskmind.db.models import UniverseMembership
        from deskmind.db.session import session_scope

        if not rows:
            return
        async with session_scope() as s:
            for i in range(0, len(rows), 2000):
                values = [
                    {
                        "day": r.day,
                        "symbol": r.symbol,
                        "rank": r.rank,
                        "median_quote_volume": Decimal(str(round(r.median_quote_volume, 4))),
                        "tradeable": r.tradeable,
                        "source": source,
                    }
                    for r in rows[i : i + 2000]
                ]
                stmt = insert(UniverseMembership).values(values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["day", "symbol"],
                    set_={
                        "rank": stmt.excluded.rank,
                        "median_quote_volume": stmt.excluded.median_quote_volume,
                        "tradeable": stmt.excluded.tradeable,
                        "source": stmt.excluded.source,
                    },
                )
                await s.execute(stmt)

    async def save_delistings(self, delisted_at_ms: dict[str, int], source: str) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from deskmind.db.models import UniverseDelisting
        from deskmind.db.session import session_scope

        if not delisted_at_ms:
            return
        stmt = insert(UniverseDelisting).values(
            [
                {"symbol": k, "delisted_at_ms": v, "source": source}
                for k, v in delisted_at_ms.items()
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"], set_={"delisted_at_ms": stmt.excluded.delisted_at_ms}
        )
        async with session_scope() as s:
            await s.execute(stmt)

    async def load_history(self, start: date, end: date) -> MembershipHistory:
        from sqlalchemy import select

        from deskmind.db.models import UniverseDelisting, UniverseMembership
        from deskmind.db.session import session_scope

        async with session_scope() as s:
            rows = (
                (
                    await s.execute(
                        select(UniverseMembership).where(
                            UniverseMembership.day >= start, UniverseMembership.day <= end
                        )
                    )
                )
                .scalars()
                .all()
            )
            delisted = (await s.execute(select(UniverseDelisting))).scalars().all()
        return MembershipHistory(
            [
                MembershipRow(r.day, r.symbol, r.rank, float(r.median_quote_volume), r.tradeable)
                for r in rows
            ],
            {d.symbol: d.delisted_at_ms for d in delisted},
        )


async def get_universe(as_of_date: date, store: UniverseStore | None = None) -> set[str]:
    """Tradeable universe members on `as_of_date` (point-in-time, from universe_membership)."""
    st = store or PostgresUniverseStore()
    return (await st.load_history(as_of_date, as_of_date)).get_universe(as_of_date)
