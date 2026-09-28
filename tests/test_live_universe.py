"""Live stream + trend agent follow the universe. Everything mocked (no WebSocket, no Redis I/O)."""

from __future__ import annotations

import asyncio

import pytest

from deskmind.data.live.service import stream_names
from deskmind.data.live.universe_sync import (
    UniverseLiveManager,
    plan_groups,
    streams_per_symbol,
)
from deskmind.data.live.ws_client import MAX_STREAMS_PER_CONNECTION
from deskmind.universe.models import SymbolRecord
from deskmind.universe.store import MemoryUniverseStore
from deskmind.universe.symbols import active_symbols, chart_entries, live_symbols

TFS = ["5m", "15m", "1h", "2h"]  # 5 streams per symbol with markPrice


def syms(n: int, prefix: str = "S") -> list[str]:
    return [f"{prefix}{i:03d}USDT" for i in range(n)]


# ---------------------------------------------------------------- stream-limit splitting


def test_split_respects_per_connection_stream_limit():
    per = streams_per_symbol(TFS)
    assert per == len(stream_names(["X"], TFS)) == 5
    plan = plan_groups({}, syms(45), per, max_streams=100)  # 20 symbols per connection
    assert [len(v) for v in plan.values()] == [20, 20, 5]
    for group in plan.values():
        assert len(stream_names(group, TFS)) <= 100
    # every symbol exactly once, kline + mark streams of a symbol on the same connection
    flat = [s for g in plan.values() for s in g]
    assert sorted(flat) == sorted(syms(45)) and len(flat) == len(set(flat))


def test_binance_hard_limit_is_1024_and_too_small_limit_rejected():
    assert MAX_STREAMS_PER_CONNECTION == 1024
    plan = plan_groups({}, syms(300), 5, max_streams=1024)
    assert [len(v) for v in plan.values()] == [204, 96]
    with pytest.raises(ValueError):
        plan_groups({}, ["AUSDT"], 5, max_streams=4)


def test_replan_is_stable_on_universe_change():
    current = {0: ["AUSDT", "BUSDT"], 1: ["CUSDT", "DUSDT"]}
    # B leaves, E joins: E fills the free slot on connection 0; connection 1 is untouched
    plan = plan_groups(current, ["AUSDT", "CUSDT", "DUSDT", "EUSDT"], 5, max_streams=10)
    assert plan == {0: ["AUSDT", "EUSDT"], 1: ["CUSDT", "DUSDT"]}
    # all of connection 1 leaves: it gets an empty list (= close it)
    plan = plan_groups(current, ["AUSDT", "BUSDT"], 5, max_streams=10)
    assert plan == {0: ["AUSDT", "BUSDT"], 1: []}


# ---------------------------------------------------------------- subscribe / unsubscribe


class FakeService:
    def __init__(self, log: list, symbols: list[str], cid: int) -> None:
        self.log, self.symbols, self.cid = log, list(symbols), cid

    async def run(self, stop: asyncio.Event) -> None:
        self.log.append(("start", self.cid, tuple(self.symbols)))
        await stop.wait()
        self.log.append(("stop", self.cid, tuple(self.symbols)))


def make_manager(max_streams: int = 10):
    log: list = []
    source = {"symbols": ["AUSDT", "BUSDT", "CUSDT"]}

    async def symbol_source() -> list[str]:
        return list(source["symbols"])

    mgr = UniverseLiveManager(
        lambda s, cid: FakeService(log, s, cid),
        symbol_source,
        TFS,
        max_streams=max_streams,
        refresh_s=0.01,
    )
    return mgr, log, source


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe_on_universe_change():
    mgr, log, _ = make_manager()
    first = await mgr.apply(["AUSDT", "BUSDT", "CUSDT"])
    await asyncio.sleep(0)
    assert first["connections"] == {0: ["AUSDT", "BUSDT"], 1: ["CUSDT"]}
    assert first["subscribed"] == ["AUSDT", "BUSDT", "CUSDT"]
    log.clear()

    # universe change: B exits, D enters -> only connection 0 is restarted
    change = await mgr.apply(["AUSDT", "CUSDT", "DUSDT"])
    await asyncio.sleep(0)
    assert change["subscribed"] == ["DUSDT"] and change["unsubscribed"] == ["BUSDT"]
    assert change["restarted_connections"] == [0]
    assert ("stop", 0, ("AUSDT", "BUSDT")) in log
    assert ("start", 0, ("AUSDT", "DUSDT")) in log
    assert not any(entry[1] == 1 for entry in log)  # connection 1 kept streaming

    # everything on connection 1 leaves: it is closed, not restarted
    log.clear()
    change = await mgr.apply(["AUSDT", "DUSDT"])
    await asyncio.sleep(0)
    assert change["unsubscribed"] == ["CUSDT"]
    assert log == [("stop", 1, ("CUSDT",))]
    assert set(mgr.conns) == {0}

    for cid in list(mgr.conns):
        await mgr._stop(cid)


@pytest.mark.asyncio
async def test_run_loop_polls_universe_and_stops_cleanly():
    mgr, log, source = make_manager(max_streams=100)
    stop = asyncio.Event()
    task = asyncio.create_task(mgr.run(stop))
    await asyncio.sleep(0.03)
    source["symbols"] = ["AUSDT", "BUSDT", "CUSDT", "NEWUSDT"]
    await asyncio.sleep(0.05)
    assert "NEWUSDT" in mgr.symbols
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert mgr.conns == {}
    assert log[-1][0] == "stop"


# ---------------------------------------------------------------- which symbols are live


@pytest.mark.asyncio
async def test_live_symbols_are_config_plus_tradeable_and_watch_only(tmp_path):
    store = MemoryUniverseStore()
    for rec in (
        SymbolRecord("SOLUSDT", "tradeable", rank=2),
        SymbolRecord("NEWUSDT", "watch_only", rank=5),
        SymbolRecord("DOGEUSDT", "downloading", rank=3),
        SymbolRecord("BADUSDT", "blocked", rank=4),
        SymbolRecord("OLDUSDT", "exited", member=False),
    ):
        await store.save(rec)
    configured = ["BTCUSDT"]
    assert await active_symbols(configured, store) == ["BTCUSDT", "SOLUSDT", "NEWUSDT"]
    entries = chart_entries(configured, await store.load(), tmp_path)
    assert [e["symbol"] for e in entries] == ["BTCUSDT", "SOLUSDT", "DOGEUSDT", "BADUSDT",
                                              "NEWUSDT"]  # fmt: skip
    assert all(e["has_data"] is False for e in entries)
    assert live_symbols(configured, {}) == configured


@pytest.mark.asyncio
async def test_active_symbols_falls_back_to_config_when_store_fails():
    class Broken(MemoryUniverseStore):
        async def load(self):
            raise RuntimeError("db down")

    assert await active_symbols(["BTCUSDT"], Broken()) == ["BTCUSDT"]


# ---------------------------------------------------------------- trend agent follows the universe


@pytest.mark.asyncio
async def test_trend_runner_warms_new_symbols_and_drops_removed(tmp_path):
    import fakeredis

    from deskmind.agents.trend.runner import LiveTrendRunner
    from deskmind.config import Settings

    settings = Settings(_env_file=None, postgres_password="x", data_dir=tmp_path)
    runner = LiveTrendRunner(
        fakeredis.FakeAsyncRedis(decode_responses=True),
        store=None,  # type: ignore[arg-type]
        recent=None,
        settings=settings,
    )
    warmed: list[str] = []

    async def fake_warm(symbol: str, now_ms: int) -> None:
        if symbol == "BADUSDT":
            raise RuntimeError("no history")
        warmed.append(symbol)
        runner.agents[symbol] = object()  # type: ignore[assignment]

    runner.warm_symbol = fake_warm  # type: ignore[method-assign]
    await runner.sync_symbols(["BTCUSDT", "SOLUSDT"], 0)
    assert warmed == ["BTCUSDT", "SOLUSDT"]
    change = await runner.sync_symbols(["BTCUSDT", "NEWUSDT", "BADUSDT"], 0)
    assert change == {"added": ["NEWUSDT", "BADUSDT"], "removed": ["SOLUSDT"]}
    assert set(runner.agents) == {"BTCUSDT", "NEWUSDT"}  # BAD failed alone, others fine
    assert warmed == ["BTCUSDT", "SOLUSDT", "NEWUSDT"]  # BTC not re-warmed
