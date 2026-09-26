"""Live service: replayed WebSocket messages, fake REST, fakeredis (no network)."""

import asyncio
import json
from decimal import Decimal

import fakeredis
import pytest

from deskmind.data.live import buffers as B
from deskmind.data.live.bar_close import BarCloseAggregator
from deskmind.data.live.buffers import Kline
from deskmind.data.live.kline_handler import KlineHandler
from deskmind.data.live.mark_handler import MarkHandler
from deskmind.data.live.service import LiveService, stream_names
from deskmind.data.live.ws_client import CombinedStreamClient
from deskmind.db.candles import MemoryCandleSink
from tests.live_fixtures import (
    FakeRest,
    ScriptedSocket,
    combined,
    ms,
    row,
    scripted_connector,
    ws_kline,
)

SYM = "BTCUSDT"
M5, M15, H1, H2 = 300_000, 900_000, 3_600_000, 7_200_000
T10 = ms(2024, 1, 1, 10)  # 10:00 closes 5m, 15m, 1h, 2h


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


async def events(redis) -> list[dict]:
    return [json.loads(f["event"]) for _, f in await redis.xrange(B.EVENT_LOG)]


async def make(redis, clock, rest: FakeRest, tfs=("5m", "15m", "1h", "2h"), symbols=(SYM,)):
    agg = BarCloseAggregator(redis, symbols, tfs, resolver=None, clock=clock)  # type: ignore[arg-type]
    handler = KlineHandler(redis, MemoryCandleSink(), agg, rest.client())
    agg.resolver = handler.resolve_missing
    await agg.load_state()
    agg.paused = False
    return agg, handler


# ---------------------------------------------------------------- buffers / handlers


async def test_partial_vs_closed(redis) -> None:
    agg, h = await make(redis, Clock(T10 - 60_000), FakeRest())
    await h.on_message(ws_kline(SYM, "5m", T10 - M5, closed=False, event_time=T10 - 1000))
    assert await B.buffer_size(redis, SYM, "5m") == 0
    oc = await B.get_open_candle(redis, SYM, "5m")
    assert oc and oc["is_open"] is True and oc["k"][0] == T10 - M5
    assert h.sink.rows == {}  # type: ignore[attr-defined]
    assert agg.seen[(SYM, "5m")] == set()

    await h.on_message(ws_kline(SYM, "5m", T10 - M5, closed=True))
    [k] = await B.get_candles(redis, SYM, "5m")
    assert k.open_time == T10 - M5 and isinstance(k.close, Decimal)
    assert (SYM, "5m", T10 - M5) in h.sink.rows  # type: ignore[attr-defined]
    assert agg.seen[(SYM, "5m")] == {T10 - M5}


async def test_older_partial_update_is_ignored(redis) -> None:
    newer = Kline.from_ws(ws_kline(SYM, "5m", T10, False)["k"])
    older = Kline.from_ws(ws_kline(SYM, "5m", T10 - M5, False)["k"])
    assert await B.put_open(redis, newer, T10 + 5)
    assert not await B.put_open(redis, older, T10 - 5)
    assert (await B.get_open_candle(redis, SYM, "5m"))["k"][0] == T10


async def test_buffer_capped_at_500(redis) -> None:
    ks = [Kline.from_rest(row(SYM, "5m", T10 + i * M5), SYM, "5m") for i in range(600)]
    await B.put_closed(redis, ks[:300])
    await B.put_closed(redis, ks[300:])
    got = await B.get_candles(redis, SYM, "5m")
    assert len(got) == 500
    assert got[0].open_time == T10 + 100 * M5 and got[-1].open_time == T10 + 599 * M5


async def test_duplicates_and_out_of_order_closed_candles(redis) -> None:
    _agg, h = await make(redis, Clock(T10 + 3 * M5 + 1000), FakeRest(), tfs=("5m",))
    a, b, c = (ws_kline(SYM, "5m", T10 + i * M5, True) for i in range(3))
    for msg in (b, a, b, c, a, c):  # out of order + duplicates
        await h.on_message(msg)
    got = await B.get_candles(redis, SYM, "5m")
    assert [k.open_time for k in got] == [T10, T10 + M5, T10 + 2 * M5]
    assert len(h.sink.rows) == 3  # type: ignore[attr-defined]


async def test_mark_price_out_of_order(redis) -> None:
    m = MarkHandler(redis)
    base = {"e": "markPriceUpdate", "s": SYM, "i": "1", "P": "1", "r": "0.0001", "T": 5}
    assert await m.on_message({**base, "E": 2000, "p": "100.5"})
    assert not await m.on_message({**base, "E": 1000, "p": "99.0"})
    assert (await B.get_mark(redis, SYM))["mark_price"] == "100.5"


# ---------------------------------------------------------------- bar close


async def test_multi_timeframe_boundary_emits_one_event(redis) -> None:
    clock = Clock(T10 - 30_000)
    agg, h = await make(redis, clock, FakeRest())
    assert agg.cursor[SYM] == T10
    clock.t = T10 + 400
    msgs = [ws_kline(SYM, "2h", T10 - H2, True), ws_kline(SYM, "5m", T10 - M5, True),
            ws_kline(SYM, "1h", T10 - H1, True), ws_kline(SYM, "15m", T10 - M15, True)]  # fmt: skip
    for i, msg in enumerate(msgs):
        await h.on_message(msg)
        assert len(await events(redis)) == (1 if i == 3 else 0)  # only when ALL have arrived
    [ev] = await events(redis)
    assert ev["close_time"] == T10 and sorted(ev["closed_timeframes"]) == ["15m", "1h", "2h", "5m"]
    assert ev["degraded"] is False and ev["schema_version"] == 1

    clock.t = T10 + M5 + 300  # 10:05 closes only 5m
    await h.on_message(ws_kline(SYM, "5m", T10, True))
    assert [e["closed_timeframes"] for e in await events(redis)][-1] == ["5m"]


async def test_timeout_backfills_from_rest(redis) -> None:
    rest = FakeRest()
    rest.add(SYM, "15m", [T10 - M15])
    rest.add(SYM, "1h", [T10 - H1])
    rest.add(SYM, "2h", [T10 - H2])
    clock = Clock(T10 - 30_000)
    agg, h = await make(redis, clock, rest)
    clock.t = T10 + 500
    await h.on_message(ws_kline(SYM, "5m", T10 - M5, True))
    assert await events(redis) == []  # waiting for the others
    clock.t = T10 + 9_000
    await agg.tick()
    assert await events(redis) == []  # timeout not reached
    clock.t = T10 + 10_500
    await agg.tick()
    [ev] = await events(redis)
    assert ev["degraded"] is False and len(ev["closed_timeframes"]) == 4
    assert await B.get_last_closed(redis, SYM, "1h")  # backfilled into the buffer


async def test_timeout_still_missing_is_degraded(redis) -> None:
    rest = FakeRest()
    rest.add(SYM, "15m", [T10 - M15])
    rest.add(SYM, "2h", [T10 - H2])  # 1h does not exist anywhere
    clock = Clock(T10 - 30_000)
    agg, h = await make(redis, clock, rest)
    clock.t = T10 + 500
    await h.on_message(ws_kline(SYM, "5m", T10 - M5, True))
    clock.t = T10 + 11_000
    await agg.tick()
    [ev] = await events(redis)
    assert ev["degraded"] is True and ev["missing"] == ["1h"]
    assert sorted(ev["closed_timeframes"]) == ["15m", "2h", "5m"]


async def test_rest_error_waits_instead_of_degrading(redis) -> None:
    rest = FakeRest()
    rest.fail = True
    clock = Clock(T10 - 30_000)
    agg, h = await make(redis, clock, rest, tfs=("5m", "15m"))
    clock.t = T10 + 500
    await h.on_message(ws_kline(SYM, "5m", T10 - M5, True))
    clock.t = T10 + 20_000
    await agg.tick()
    assert await events(redis) == []  # network trouble is not "missing data"
    rest.fail = False
    rest.add(SYM, "15m", [T10 - M15])
    await agg.tick()
    [ev] = await events(redis)
    assert ev["degraded"] is False


async def test_never_emits_twice_even_after_restart(redis) -> None:
    clock = Clock(T10 - 30_000)
    _agg, h = await make(redis, clock, FakeRest(), tfs=("5m",))
    clock.t = T10 + 200
    await h.on_message(ws_kline(SYM, "5m", T10 - M5, True))
    assert len(await events(redis)) == 1
    # A second aggregator (e.g. restarted service) resumes and sees the same candle again.
    agg2, h2 = await make(redis, clock, FakeRest(), tfs=("5m",))
    agg2.cursor[SYM] = T10  # force it to reconsider the same boundary
    await h2.on_message(ws_kline(SYM, "5m", T10 - M5, True))
    assert len(await events(redis)) == 1
    assert agg2.duplicates_suppressed == 1


# ---------------------------------------------------------------- reconnect end to end


async def test_reconnect_gap_is_backfilled_in_order_without_duplicates(redis) -> None:
    """10:05 arrives live; the socket drops; 10:10 and 10:15 are backfilled from REST after
    reconnect and emitted in order before the next live candle (10:20)."""
    tfs = ("5m", "15m")
    clock = Clock(T10 + 30_000)  # service starts at 10:00:30
    rest = FakeRest()
    rest.add(SYM, "5m", [T10 - i * M5 for i in range(1, 20)])  # history up to open 09:55
    rest.add(SYM, "15m", [T10 - i * M15 for i in range(1, 10)])
    stop = asyncio.Event()

    def during_outage() -> None:  # time passes, the exchange keeps producing candles
        clock.t = T10 + 17 * 60_000
        rest.add(SYM, "5m", [T10, T10 + M5, T10 + 2 * M5])
        rest.add(SYM, "15m", [T10])

    def at(t: int):
        def f() -> None:
            clock.t = t

        return f

    conn1 = ScriptedSocket(
        [at(T10 + M5 + 400), combined("btcusdt@kline_5m", ws_kline(SYM, "5m", T10, True))],
        on_empty=during_outage,
    )
    conn2 = ScriptedSocket(
        [
            at(T10 + 4 * M5 + 300),
            combined("btcusdt@kline_5m", ws_kline(SYM, "5m", T10 + 2 * M5, True)),  # duplicate
            combined("btcusdt@kline_5m", ws_kline(SYM, "5m", T10 + 3 * M5, True)),
        ],
        on_empty=stop.set,
    )
    service = LiveService(
        redis,
        MemoryCandleSink(),
        rest.client(),
        symbols=[SYM],
        timeframes=list(tfs),
        ws_url="wss://test/market",
        connector=scripted_connector([conn1, conn2]),
        clock=clock,
    )

    async def no_sleep(_s: float) -> None:
        await service.aggregator.tick()  # paused while disconnected: must not emit or degrade

    service.ws.sleep = no_sleep
    await service.start()
    await asyncio.wait_for(service.ws.run(stop), timeout=10)

    evs = await events(redis)
    times = [(e["close_time"] - T10) // 60_000 for e in evs]
    assert times == [5, 10, 15, 20]  # in order, nothing skipped, nothing twice
    assert [e["closed_timeframes"] for e in evs] == [["5m"], ["5m"], ["5m", "15m"], ["5m"]]
    assert not any(e["degraded"] for e in evs)
    assert service.ws.reconnects == 1
    closed = [k.open_time for k in await B.get_candles(redis, SYM, "5m")]
    assert closed == sorted(set(closed)) and closed[-1] == T10 + 3 * M5


# ---------------------------------------------------------------- ws client


async def test_ws_client_rotates_before_24h_and_reconnects_on_silence() -> None:
    t = [0.0]
    got: list[str] = []
    stop = asyncio.Event()

    def advance(dt: float):
        def f() -> None:
            t[0] += dt

        return f

    msg = combined("btcusdt@kline_5m", ws_kline(SYM, "5m", T10, False))
    conn1 = ScriptedSocket([msg, advance(23 * 3600 + 1), msg], on_empty=stop.set)
    conn2 = ScriptedSocket([msg], on_empty=stop.set)

    async def on_msg(stream: str, data: dict) -> None:
        got.append(stream)

    reasons: list[str] = []

    async def on_disc(reason: str) -> None:
        reasons.append(reason)

    client = CombinedStreamClient(
        "wss://test/market",
        ["btcusdt@kline_5m"],
        on_msg,
        on_disconnect=on_disc,
        connector=scripted_connector([conn1, conn2]),
        clock=lambda: t[0],
    )
    stop2 = asyncio.Event()

    async def fake_sleep(_s: float) -> None:
        stop2.set()

    client.sleep = fake_sleep
    await asyncio.wait_for(client.run(stop), timeout=5)
    assert reasons[0] == "rotate"  # proactive rotation, no backoff sleep
    assert client.reconnects == 1 and len(got) >= 2


def test_stream_names_keep_case_after_symbol() -> None:
    names = stream_names(["BTCUSDT", "ETHUSDT"], ["5m", "1h"])
    assert "btcusdt@kline_5m" in names and "ethusdt@markPrice@1s" in names
    assert len(names) == 2 * 2 + 2
    client = CombinedStreamClient("wss://x/market", names, on_message=None)  # type: ignore[arg-type]
    assert client.url.startswith("wss://x/market/stream?streams=btcusdt@kline_5m/")
    assert "@markPrice@1s" in client.url


def test_staleness_measured_from_reconnect_not_from_before_outage(redis) -> None:
    service = LiveService(
        redis, MemoryCandleSink(), FakeRest().client(), symbols=[SYM], timeframes=["5m"],
        ws_url="wss://test/market",
    )  # fmt: skip
    t = [1000.0]
    service.ws.clock = lambda: t[0]
    service.ws.state = "connected"
    service.ws.last_message_at = {s: 100.0 for s in service.streams}  # before a long outage
    service.ws.connected_since = 1000.0  # just reconnected
    assert service._stale_streams() == []
    t[0] = 1000.0 + 31  # 31s after reconnect with no new messages: now genuinely stale
    assert set(service._stale_streams()) == set(service.streams)
