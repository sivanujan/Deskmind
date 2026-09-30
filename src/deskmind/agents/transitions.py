"""Point-in-time storage shared by analyst agents: items + state transitions, no report snapshots.

Rows (one symbol + timeframe, ordered by (at, ord)):
- first row of an item = full payload, later rows = only the changed fields;
- synthetic rows `state:<name>` = engine state that is not an item ({"value": ...});
- `state:baseline` marks a baseline: replay restarts there, followed by the full payload of
  every object the engine still holds (so warm-up items are covered).

Each agent subclasses `TransitionRecorder` (which objects form a baseline, which synthetic
states to record) and writes its own `reconstruct_state` on top of `replay_items`.
Storage: `agent_item_transitions` (compressed TimescaleDB hypertable) via `PgTransitions`.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

STATE_BASELINE = "state:baseline"
BATCH_TRANSITIONS = 1000


@dataclass
class TransitionRecorder:
    rows: list[dict[str, Any]] = field(default_factory=list)
    _last: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    _ord: dict[int, int] = field(default_factory=dict)
    started: bool = False

    # ---- agent hooks
    def objects(self, engine: Any) -> list[Any]:
        """Objects (with .item()) the engine holds, in creation order, for a baseline."""
        raise NotImplementedError

    def record_states(self, at: int, tf: str, engine: Any) -> None:
        """Record the engine's non-item state via record_value()."""
        raise NotImplementedError

    # ---- core
    def _emit(self, at: int, tf: str, item_id: str, kind: str, payload: dict[str, Any]) -> None:
        o = self._ord.get(at, 0)
        self._ord[at] = o + 1
        self.rows.append(
            {"tf": tf, "item_id": item_id, "kind": kind, "at": at, "ord": o, "payload": payload}
        )

    def record(self, at: int, tf: str, item: dict[str, Any]) -> None:
        key = (tf, item["id"])
        prev = self._last.get(key)
        if prev is None:
            self._emit(at, tf, item["id"], item["kind"], copy.deepcopy(item))
        else:
            diff = {k: v for k, v in item.items() if prev.get(k) != v}
            if not diff:
                return
            self._emit(at, tf, item["id"], item["kind"], copy.deepcopy(diff))
        self._last[key] = copy.deepcopy(item)

    def record_value(
        self,
        at: int,
        tf: str,
        key: str,
        value: Any,
        same: Callable[[Any, Any], bool] | None = None,
    ) -> None:
        prev = self._last.get((tf, key), {"value": None})["value"]
        if prev is None and value is None:
            return
        if prev is not None and value is not None and (same or (lambda a, b: a == b))(prev, value):
            return
        self._emit(at, tf, key, key, {"value": copy.deepcopy(value)})
        self._last[(tf, key)] = {"value": copy.deepcopy(value)}

    def baseline(self, at: int, agent: Any) -> None:
        self.started = True
        self._last.clear()
        for tf, eng in agent.engines.items():
            self._emit(at, tf, STATE_BASELINE, STATE_BASELINE, {"value": None})
            for obj in self.objects(eng):
                item = obj.item() if hasattr(obj, "item") else obj
                self.record(at, tf, item)
            self.record_states(at, tf, eng)

    def boundary(self, at: int, agent: Any, changes: list[tuple[str, dict]]) -> None:
        for tf, item in changes:
            self.record(at, tf, item)
        for tf, eng in agent.engines.items():
            self.record_states(at, tf, eng)

    def take(self) -> list[dict[str, Any]]:
        rows, self.rows = self.rows, []
        return rows


def replay_items(
    rows: list[dict[str, Any]], at_most: int
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, Any]]:
    """Apply rows (one symbol+tf, sorted by (at, ord)) up to `at_most`.

    Returns (items by id, creation rank by id, state values by state id)."""
    items: dict[str, dict[str, Any]] = {}
    rank: dict[str, int] = {}
    states: dict[str, Any] = {}
    for n, r in enumerate(rows):
        if r["at"] > at_most:
            break
        if r["item_id"] == STATE_BASELINE:  # everything before it is superseded
            items, rank, states = {}, {}, {}
            continue
        if r["item_id"].startswith("state:"):
            states[r["item_id"]] = r["payload"]["value"]
            continue
        cur = items.get(r["item_id"])
        if cur is None:
            items[r["item_id"]] = copy.deepcopy(r["payload"])
            rank[r["item_id"]] = n
        else:
            cur.update(copy.deepcopy(r["payload"]))
    return items, rank, states


def rows_by_tf(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: (r["at"], r["ord"])):
        out.setdefault(r["tf"], []).append(r)
    return out


# ---------------------------------------------------------------- storage


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


class MemoryTransitions:
    """In-memory transitions + report pruning (tests)."""

    def __init__(self) -> None:
        self.trans: dict[tuple[str, str], list[dict[str, Any]]] = {}

    async def replace_transitions(self, symbol, version, from_ms, rows) -> int:
        kept = [r for r in self.trans.get((symbol, version), []) if r["at"] < from_ms]
        self.trans[(symbol, version)] = sorted([*kept, *rows], key=lambda r: (r["at"], r["ord"]))
        return len(rows)

    async def append_transitions(self, symbol, version, rows) -> int:
        cur = self.trans.setdefault((symbol, version), [])
        seen = {(r["tf"], r["at"], r["ord"]) for r in cur}
        new = [r for r in rows if (r["tf"], r["at"], r["ord"]) not in seen]
        self.trans[(symbol, version)] = sorted([*cur, *new], key=lambda r: (r["at"], r["ord"]))
        return len(new)

    async def transitions(self, symbol, version, tf, upto_ms) -> list[dict[str, Any]]:
        rows = [
            r for r in self.trans.get((symbol, version), []) if r["tf"] == tf and r["at"] <= upto_ms
        ]
        starts = [i for i, r in enumerate(rows) if r["item_id"] == STATE_BASELINE]
        return rows[starts[-1] :] if starts else rows


class PgTransitions:
    """agent_item_transitions access for one agent (Postgres)."""

    def __init__(self, sessions: Any, agent: str) -> None:
        self._sessions = sessions
        self.agent = agent

    def _values(self, symbol: str, version: str, rows: list[dict[str, Any]]):
        return [
            {"agent": self.agent, "symbol": symbol, "agent_version": version,
             "timeframe": r["tf"], "at": _dt(r["at"]), "ord": r["ord"],
             "item_id": r["item_id"], "kind": r["kind"], "payload": r["payload"]}
            for r in rows
        ]  # fmt: skip

    async def _insert(self, session: Any, values: list[dict[str, Any]]) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from deskmind.db.models import AgentItemTransition

        for i in range(0, len(values), BATCH_TRANSITIONS):
            stmt = insert(AgentItemTransition).values(values[i : i + BATCH_TRANSITIONS])
            await session.execute(
                stmt.on_conflict_do_nothing(
                    index_elements=["agent", "symbol", "agent_version", "timeframe", "at", "ord"]
                )
            )

    async def replace(self, symbol: str, version: str, from_ms: int, rows) -> int:
        from sqlalchemy import delete

        from deskmind.db.models import AgentItemTransition as T

        async with self._sessions() as session:
            await session.execute(
                delete(T).where(
                    T.agent == self.agent,
                    T.symbol == symbol,
                    T.agent_version == version,
                    T.at >= _dt(from_ms),
                )
            )
            await self._insert(session, self._values(symbol, version, rows))
            await session.commit()
        return len(rows)

    async def append(self, symbol: str, version: str, rows) -> int:
        if not rows:
            return 0
        async with self._sessions() as session:
            await self._insert(session, self._values(symbol, version, rows))
            await session.commit()
        return len(rows)

    async def load(self, symbol: str, version: str, tf: str, upto_ms: int) -> list[dict]:
        """Rows from the last baseline <= upto_ms up to upto_ms, ordered."""
        from sqlalchemy import select

        from deskmind.db.models import AgentItemTransition as T

        scope = (
            T.agent == self.agent,
            T.symbol == symbol,
            T.agent_version == version,
            T.timeframe == tf,
        )
        last_baseline = (
            select(T.at)
            .where(*scope, T.item_id == STATE_BASELINE, T.at <= _dt(upto_ms))
            .order_by(T.at.desc())
            .limit(1)
            .scalar_subquery()
        )
        q = (
            select(T.at, T.ord, T.item_id, T.kind, T.payload)
            .where(*scope, T.at <= _dt(upto_ms), T.at >= last_baseline)
            .order_by(T.at, T.ord)
        )
        async with self._sessions() as session:
            res = (await session.execute(q)).all()
        return [
            {"tf": tf, "at": int(at.timestamp() * 1000), "ord": o, "item_id": i, "kind": k,
             "payload": p}
            for at, o, i, k, p in res
        ]  # fmt: skip

    async def prune_reports(self, symbol: str, version: str, before_ms: int) -> int:
        """Live report retention for the CURRENT version only (older versions untouched)."""
        from sqlalchemy import delete

        from deskmind.db.models import AgentReport

        async with self._sessions() as session:
            res = await session.execute(
                delete(AgentReport).where(
                    AgentReport.agent_name == self.agent,
                    AgentReport.symbol == symbol,
                    AgentReport.version == version,
                    AgentReport.as_of < _dt(before_ms),
                )
            )
            await session.commit()
        return res.rowcount or 0
