"""Where the universe lives: `universe_symbols` (one row per symbol ever in the universe) and
`universe_runs` (one row per daily run, with its summary). Migration 0007.

`InMemoryUniverseStore` has the same behaviour and is used by the tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from deskmind.universe.lifecycle import BlockedReason, SymbolRecord, SymbolState

RUN_RUNNING, RUN_COMPLETED, RUN_FAILED = "running", "completed", "failed"


class UniverseStore(Protocol):
    async def load_all(self) -> dict[str, SymbolRecord]: ...

    async def save(self, rec: SymbolRecord) -> None: ...

    async def run_status(self, run_date: date) -> str | None: ...

    async def start_run(self, run_date: date, started_at: datetime) -> None: ...

    async def finish_run(
        self, run_date: date, status: str, finished_at: datetime, summary: dict[str, Any]
    ) -> None: ...

    async def recent_runs(self, limit: int = 7) -> list[dict[str, Any]]: ...


class InMemoryUniverseStore:
    def __init__(self) -> None:
        self.records: dict[str, SymbolRecord] = {}
        self.runs: dict[date, dict[str, Any]] = {}

    async def load_all(self) -> dict[str, SymbolRecord]:
        return {s: replace(r, history=list(r.history)) for s, r in self.records.items()}

    async def save(self, rec: SymbolRecord) -> None:
        self.records[rec.symbol] = replace(rec, history=list(rec.history))

    async def run_status(self, run_date: date) -> str | None:
        run = self.runs.get(run_date)
        return run["status"] if run else None

    async def start_run(self, run_date: date, started_at: datetime) -> None:
        self.runs[run_date] = {
            "run_date": run_date.isoformat(),
            "status": RUN_RUNNING,
            "started_at": started_at.isoformat(),
            "finished_at": None,
            "summary": None,
        }

    async def finish_run(
        self, run_date: date, status: str, finished_at: datetime, summary: dict[str, Any]
    ) -> None:
        self.runs[run_date].update(
            status=status, finished_at=finished_at.isoformat(), summary=summary
        )

    async def recent_runs(self, limit: int = 7) -> list[dict[str, Any]]:
        return [dict(self.runs[d]) for d in sorted(self.runs, reverse=True)[:limit]]


# ---------------------------------------------------------------- Postgres


def _to_row(rec: SymbolRecord) -> dict[str, Any]:
    return {
        "symbol": rec.symbol,
        "state": str(rec.state),
        "in_universe": rec.in_universe,
        "rank": rec.rank,
        "quote_volume_24h": rec.quote_volume_24h,
        "onboard_at": rec.onboard_at,
        "entered_at": rec.entered_at,
        "exited_at": rec.exited_at,
        "state_changed_at": rec.state_changed_at,
        "verified_at": rec.verified_at,
        "warmed_up_at": rec.warmed_up_at,
        "blocked_reason": str(rec.blocked_reason) if rec.blocked_reason else None,
        "last_error": rec.last_error,
        "history": rec.history,
    }


def _from_row(row: Any) -> SymbolRecord:
    return SymbolRecord(
        symbol=row.symbol,
        state=SymbolState(row.state),
        in_universe=row.in_universe,
        rank=row.rank,
        quote_volume_24h=Decimal(row.quote_volume_24h)
        if row.quote_volume_24h is not None
        else None,
        onboard_at=row.onboard_at,
        entered_at=row.entered_at,
        exited_at=row.exited_at,
        state_changed_at=row.state_changed_at,
        verified_at=row.verified_at,
        warmed_up_at=row.warmed_up_at,
        blocked_reason=BlockedReason(row.blocked_reason) if row.blocked_reason else None,
        last_error=row.last_error,
        history=list(row.history or []),
    )


def run_json(row: Any) -> dict[str, Any]:
    return {
        "run_date": row.run_date.isoformat(),
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "summary": row.summary,
    }


class PostgresUniverseStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sm = async_sessionmaker(engine, expire_on_commit=False)

    async def load_all(self) -> dict[str, SymbolRecord]:
        from deskmind.db.models import UniverseSymbol

        async with self._sm() as s:
            rows = (await s.execute(select(UniverseSymbol))).scalars().all()
        return {r.symbol: _from_row(r) for r in rows}

    async def save(self, rec: SymbolRecord) -> None:
        from deskmind.db.models import UniverseSymbol

        row = _to_row(rec)
        stmt = insert(UniverseSymbol).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"], set_={k: v for k, v in row.items() if k != "symbol"}
        )
        async with self._sm() as s, s.begin():
            await s.execute(stmt)

    async def run_status(self, run_date: date) -> str | None:
        from deskmind.db.models import UniverseRun

        async with self._sm() as s:
            return (
                await s.execute(select(UniverseRun.status).where(UniverseRun.run_date == run_date))
            ).scalar_one_or_none()

    async def start_run(self, run_date: date, started_at: datetime) -> None:
        from deskmind.db.models import UniverseRun

        values = {
            "run_date": run_date,
            "status": RUN_RUNNING,
            "started_at": started_at,
            "finished_at": None,
            "summary": None,
        }
        stmt = insert(UniverseRun).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_date"], set_={k: v for k, v in values.items() if k != "run_date"}
        )
        async with self._sm() as s, s.begin():
            await s.execute(stmt)

    async def finish_run(
        self, run_date: date, status: str, finished_at: datetime, summary: dict[str, Any]
    ) -> None:
        from deskmind.db.models import UniverseRun

        async with self._sm() as s, s.begin():
            run = await s.get(UniverseRun, run_date)
            if run is None:
                raise ValueError(f"universe run {run_date} was never started")
            run.status, run.finished_at, run.summary = status, finished_at, summary

    async def recent_runs(self, limit: int = 7) -> list[dict[str, Any]]:
        from deskmind.db.models import UniverseRun

        async with self._sm() as s:
            rows = (
                (
                    await s.execute(
                        select(UniverseRun).order_by(UniverseRun.run_date.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [run_json(r) for r in rows]
