"""Where universe states and daily run summaries are kept."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

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


class MemoryUniverseStore:
    def __init__(self) -> None:
        self.records: dict[str, SymbolRecord] = {}
        self.run_log: list[dict[str, Any]] = []

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
