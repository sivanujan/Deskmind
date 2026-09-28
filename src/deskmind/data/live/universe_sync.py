"""Live data for a changing symbol set: several combined-stream connections, re-planned when the
universe changes.

- Each connection is an ordinary LiveService (same reconnect, rotation, REST gap-fill and
  bar-close logic). A symbol's kline + markPrice streams always share one connection, so its
  bar_close aggregation is complete on that connection.
- Streams per connection stay <= `max_streams` (Binance allows 1024; the default is lower to keep
  URLs short and message rates modest). More symbols -> more connections.
- Assignment is stable: on a change, symbols stay on their connection; removed symbols are
  dropped, new ones fill the first connection with room. Only connections whose symbol list
  changed are restarted (= unsubscribe/subscribe); a restarted connection warms its buffers and
  backfills gaps from REST on connect, exactly like a reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from deskmind.data.live import buffers as B
from deskmind.logging import get_logger

log = get_logger(__name__)

DEFAULT_MAX_STREAMS = 200
REFRESH_S = 60.0


class Service(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


ServiceFactory = Callable[[list[str], int], Service]  # (symbols, connection id) -> service


def streams_per_symbol(timeframes: list[str]) -> int:
    return len(timeframes) + 1  # klines per timeframe + markPrice


def plan_groups(
    current: dict[int, list[str]], desired: list[str], per_symbol: int, max_streams: int
) -> dict[int, list[str]]:
    """New symbol list per connection id (stable). Empty list = connection to close."""
    cap = max_streams // per_symbol
    if cap < 1:
        raise ValueError(f"max_streams={max_streams} < {per_symbol} streams of one symbol")
    wanted = set(desired)
    plan = {cid: [s for s in syms if s in wanted] for cid, syms in current.items()}
    placed = {s for syms in plan.values() for s in syms}
    next_id = max(current, default=-1) + 1
    for sym in desired:
        if sym in placed:
            continue
        target = next((cid for cid in sorted(plan) if 0 < len(plan[cid]) < cap), None)
        if target is None:
            target = next((cid for cid in sorted(plan) if not plan[cid]), None)
        if target is None:
            target, next_id = next_id, next_id + 1
            plan[target] = []
        plan[target].append(sym)
        placed.add(sym)
    return plan


@dataclass
class _Conn:
    symbols: list[str]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class UniverseLiveManager:
    def __init__(
        self,
        make_service: ServiceFactory,
        symbol_source: Callable[[], Awaitable[list[str]]],
        timeframes: list[str],
        *,
        max_streams: int = DEFAULT_MAX_STREAMS,
        refresh_s: float = REFRESH_S,
        redis: Any = None,
    ) -> None:
        self.make_service = make_service
        self.symbol_source = symbol_source
        self.per_symbol = streams_per_symbol(timeframes)
        self.max_streams = max_streams
        self.refresh_s = refresh_s
        self.redis = redis
        self.conns: dict[int, _Conn] = {}
        self.changes: list[dict[str, Any]] = []

    @property
    def symbols(self) -> list[str]:
        return [s for cid in sorted(self.conns) for s in self.conns[cid].symbols]

    def _start(self, cid: int, symbols: list[str]) -> None:
        conn = _Conn(symbols=list(symbols))
        service = self.make_service(conn.symbols, cid)
        conn.task = asyncio.create_task(service.run(conn.stop), name=f"live_conn_{cid}")
        self.conns[cid] = conn

    async def _stop(self, cid: int) -> None:
        conn = self.conns.pop(cid)
        conn.stop.set()
        if conn.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(conn.task, timeout=30)

    async def apply(self, desired: list[str]) -> dict[str, Any]:
        before = set(self.symbols)
        plan = plan_groups(
            {cid: c.symbols for cid, c in self.conns.items()},
            desired,
            self.per_symbol,
            self.max_streams,
        )
        restarted = []
        for cid, symbols in sorted(plan.items()):
            existing = self.conns.get(cid)
            if existing is not None and existing.symbols == symbols:
                continue  # untouched connection keeps streaming
            if existing is not None:
                await self._stop(cid)
            if symbols:
                self._start(cid, symbols)
            restarted.append(cid)
        change = {
            "subscribed": sorted(set(desired) - before),
            "unsubscribed": sorted(before - set(desired)),
            "restarted_connections": restarted,
            "connections": {cid: list(c.symbols) for cid, c in sorted(self.conns.items())},
        }
        if change["subscribed"] or change["unsubscribed"]:
            log.info("live_universe_change", **change)
        self.changes.append(change)
        if self.redis is not None:
            with contextlib.suppress(Exception):
                await self.redis.hset(
                    B.HEALTH_SERVICE,
                    mapping={"connections": json.dumps(change["connections"])},
                )
        return change

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self.apply(await self.symbol_source())
            while not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.refresh_s)
                if stop.is_set():
                    break
                for cid, conn in list(self.conns.items()):  # a crashed connection is restarted
                    if conn.task is not None and conn.task.done():
                        log.error("live_connection_crashed", connection=cid)
                        self.conns.pop(cid)
                        self._start(cid, conn.symbols)
                try:
                    desired = await self.symbol_source()
                except Exception as exc:  # keep streaming the current set
                    log.warning("live_symbol_source_failed", error=str(exc))
                    continue
                if set(desired) != set(self.symbols):
                    await self.apply(desired)
        finally:
            for cid in list(self.conns):
                await self._stop(cid)
