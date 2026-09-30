"""Read-only API: every endpoint against fixtures (fakeredis, temp Parquet, in-memory Postgres)."""

import itertools
import json
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from deskmind.agents.trend.agent import SymbolTrendAgent, run_batch
from deskmind.agents.trend.models import Bar
from deskmind.agents.trend.store import MemoryTrendStore
from deskmind.api import overlays as ov
from deskmind.api.app import create_app
from deskmind.api.candles import CandleService
from deskmind.api.schemas import CandleOut
from deskmind.config import Settings
from deskmind.data.datasets import Dataset
from deskmind.data.live import buffers as B
from deskmind.data.live.buffers import Kline
from deskmind.data.storage import Store, candles_table, write_parquet
from deskmind.universe.store import MemoryUniverseStore
from tests.live_fixtures import ms, row

H1 = 3_600_000
T0 = ms(2024, 1, 1)
SYMBOLS = "BTCUSDT,ETHUSDT"


class MemoryRecent:
    def __init__(self, rows: dict[tuple[str, str], list[CandleOut]]) -> None:
        self.rows = rows

    async def fetch(self, symbol, tf, start_ms, end_ms, limit):
        out = [
            c
            for c in self.rows.get((symbol, tf), [])
            if c.time * 1000 < end_ms and (start_ms is None or c.time * 1000 >= start_ms)
        ]
        return out[-limit:]


def recent_candle(t_ms: int, close: float) -> CandleOut:
    return CandleOut(time=t_ms // 1000, open=1, high=close + 1, low=0.5, close=close, volume=2,
                     source="postgres")  # fmt: skip


@pytest.fixture
def env(tmp_path: Path):
    settings = Settings(_env_file=None, postgres_password="x", data_symbols=SYMBOLS,
                        live_timeframes="5m,15m,1h,2h", data_dir=tmp_path)  # fmt: skip
    # Parquet history: 48 x 1h for BTCUSDT (2024-01-01 00:00 .. 2024-01-02 23:00).
    store = Store(tmp_path, "BTCUSDT")
    ds = Dataset("klines", "1h")
    rows = [[str(x) for x in row("BTCUSDT", "1h", T0 + i * H1)] for i in range(48)]
    write_parquet(store.partition_path(ds, 2024, 1), candles_table(ds, rows))
    # Postgres: overlaps the last 2 Parquet hours (different close -> must win) + 3 newer.
    recent = MemoryRecent(
        {("BTCUSDT", "1h"): [recent_candle(T0 + i * H1, 99_000 + i) for i in range(46, 51)]}
    )
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    trend = MemoryTrendStore()
    from deskmind.agents.ict_smc.store import MemoryIctSmcStore

    ict_smc = MemoryIctSmcStore()
    universe = MemoryUniverseStore()
    app = create_app(
        settings,
        redis=redis,
        candles=CandleService(tmp_path, recent),
        trend_store=trend,
        ict_smc_store=ict_smc,
        health_checks=False,
        universe_store=universe,
    )
    app.state.universe_seed = universe
    app.state.trend_seed = trend
    app.state.ict_smc_seed = ict_smc
    return app, redis


def test_symbols(env) -> None:
    app, _ = env
    with TestClient(app) as c:
        r = c.get("/api/symbols")
    assert r.status_code == 200
    body = r.json()
    assert body["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert body["timeframes"] == ["5m", "15m", "1h", "2h"]
    assert body["entries"] == [
        {"symbol": "BTCUSDT", "state": None, "reason": None, "configured": True, "has_data": True},
        {"symbol": "ETHUSDT", "state": None, "reason": None, "configured": True, "has_data": False},
    ]


def test_symbols_include_universe_members_with_state(env) -> None:
    import asyncio

    from deskmind.universe.models import SymbolRecord

    app, _ = env
    store = app.state.universe_seed
    recs = [
        SymbolRecord("SOLUSDT", "tradeable", rank=3),
        SymbolRecord("NEWUSDT", "watch_only", rank=9),
        SymbolRecord("DOGEUSDT", "downloading", rank=5),
        SymbolRecord("OLDUSDT", "exited", rank=None, member=False),
    ]
    for rec in recs:
        asyncio.run(store.save(rec))
    with TestClient(app) as c:
        body = c.get("/api/symbols").json()
        candles = c.get("/api/candles", params={"symbol": "NEWUSDT", "tf": "1h"})
        gone = c.get("/api/candles", params={"symbol": "OLDUSDT", "tf": "1h"})
    by = {e["symbol"]: e for e in body["entries"]}
    assert body["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "NEWUSDT"]
    assert by["NEWUSDT"] == {
        "symbol": "NEWUSDT", "state": "watch_only", "reason": None, "configured": False, "has_data": False
    }  # fmt: skip
    assert by["DOGEUSDT"]["state"] == "downloading"
    assert "OLDUSDT" not in by  # exited: no longer offered in the selector
    assert candles.status_code == 200 and candles.json()["count"] == 0  # known, no data yet
    assert gone.status_code == 404


def test_candles_merge_parquet_and_postgres(env) -> None:
    app, _ = env
    with TestClient(app) as c:
        r = c.get("/api/candles", params={"symbol": "btcusdt", "tf": "1h", "limit": 5})
    body = r.json()
    assert r.status_code == 200 and body["count"] == 5
    times = [x["time"] for x in body["candles"]]
    assert times == [(T0 + i * H1) // 1000 for i in range(46, 51)]  # sorted, unique, latest 5
    assert {x["source"] for x in body["candles"]} == {"postgres"}  # overlap -> Postgres wins
    assert body["candles"][0]["close"] == 99_046


def test_candles_window_and_lazy_loading(env) -> None:
    app, _ = env
    with TestClient(app) as c:
        older = c.get(
            "/api/candles",
            params={"symbol": "BTCUSDT", "tf": "1h", "end": (T0 + 10 * H1) // 1000, "limit": 3},
        ).json()
        window = c.get(
            "/api/candles",
            params={"symbol": "BTCUSDT", "tf": "1h", "start": (T0 + 20 * H1) // 1000,
                    "end": (T0 + 22 * H1) // 1000},
        ).json()  # fmt: skip
    assert [x["time"] for x in older["candles"]] == [(T0 + i * H1) // 1000 for i in (7, 8, 9)]
    assert {x["source"] for x in older["candles"]} == {"parquet"}
    assert [x["time"] for x in window["candles"]] == [(T0 + i * H1) // 1000 for i in (20, 21)]


def test_candles_rejects_unknown_symbol_and_timeframe(env) -> None:
    app, _ = env
    with TestClient(app) as c:
        assert c.get("/api/candles", params={"symbol": "DOGEUSDT", "tf": "1h"}).status_code == 404
        assert c.get("/api/candles", params={"symbol": "BTCUSDT", "tf": "4h"}).status_code == 404
        assert (
            c.get("/api/candles", params={"symbol": "BTCUSDT", "tf": "1h", "limit": 0}).status_code
            == 422
        )
        assert c.get("/api/candles", params={"symbol": "ETHUSDT", "tf": "1h"}).json()["count"] == 0


async def _seed_live(redis, now: int) -> None:
    await redis.hset(
        B.HEALTH_SERVICE,
        mapping={"state": "running", "ws_state": "connected", "heartbeat": now - 2000,
                 "reconnects": 0, "clock_drift_ms": 120, "stale_streams": "[]"},
    )  # fmt: skip
    for s in ("BTCUSDT", "ETHUSDT"):
        for tf, iv in (("5m", 300_000), ("15m", 900_000), ("1h", H1), ("2h", 2 * H1)):
            last_open = (now // iv) * iv - iv
            await B.put_closed(redis, [Kline.from_rest(row(s, tf, last_open), s, tf)])
            await redis.hset(B.HEALTH_STREAMS, f"{s.lower()}@kline_{tf}", now - 1000)
        await redis.set(B.mark_key(s), json.dumps({"mark_price": "1.0", "event_time": now - 500}))


def test_live_status_green_then_red_when_service_stops(env) -> None:
    import asyncio

    app, redis = env
    now = B.now_ms()
    asyncio.run(_seed_live(redis, now))
    with TestClient(app) as c:
        body = c.get("/api/live/status").json()
        assert body["level"] == "green", body["reasons"]
        assert body["running"] is True and body["clock_drift_ms"] == 120
        btc = body["symbols"][0]
        assert btc["symbol"] == "BTCUSDT" and len(btc["timeframes"]) == 4
        assert all(not t["stale"] and t["behind_candles"] == 0 for t in btc["timeframes"])

        asyncio.run(redis.hset(B.HEALTH_SERVICE, mapping={"state": "stopped"}))
        body = c.get("/api/live/status").json()
        assert body["level"] == "red" and "not running" in body["reasons"][0]


def test_live_status_amber_on_stale_stream(env) -> None:
    import asyncio

    app, redis = env
    now = B.now_ms()
    asyncio.run(_seed_live(redis, now))
    asyncio.run(redis.hset(B.HEALTH_STREAMS, "ethusdt@kline_15m", now - 120_000))
    with TestClient(app) as c:
        body = c.get("/api/live/status").json()
    assert body["level"] == "amber" and "ETHUSDT 15m stale" in body["reasons"]


def test_health(env) -> None:
    app, _ = env
    with TestClient(app) as c:
        body = c.get("/api/health").json()
    assert body["ok"] is True and body["live"]["level"] == "red"  # no live service in the fixture


def test_overlays_empty_without_agent_results(env, monkeypatch) -> None:
    import deskmind.api.overlays as overlays_mod

    async def none(*_a, **_k):  # journal / imported fills live in the dev Postgres, not here
        return []

    monkeypatch.setattr(overlays_mod, "load_execution_overlays", none)
    monkeypatch.setattr(overlays_mod, "load_external_trade_overlays", none)
    app, _ = env
    with TestClient(app) as c:
        body = c.get("/api/overlays", params={"symbol": "BTCUSDT", "tf": "15m"}).json()
    assert body == {"symbol": "BTCUSDT", "timeframe": "15m", "schema_version": 1, "overlays": []}


def _seed_trend(store: MemoryTrendStore) -> tuple[list[Bar], dict]:
    """Run the real agent over a zig-zag so the store holds genuine items + reports."""
    import asyncio

    iv = H1
    pts = [105, 100, 110, 104, 116, 108, 122, 115, 100, 104, 90, 96]
    vals = [pts[0]]
    for a, b in itertools.pairwise(pts):
        vals += [a + (b - a) * k / 4 for k in range(1, 5)]
    bars, prev = [], vals[0]
    for i, v in enumerate(vals):
        t = T0 + i * iv
        bars.append(Bar(t, t + iv, prev, max(prev, v) + 0.5, min(prev, v) - 0.5, v))
        prev = v
    from deskmind.agents.trend.config import TrendConfig

    cfg = TrendConfig(left_bars=2, right_bars=2, atr_period=3)
    res = run_batch(SymbolTrendAgent("BTCUSDT", ["1h"], cfg), {"1h": bars})
    asyncio.run(
        store.upsert_items("BTCUSDT", [(tf, it) for (tf, _), it in res.items.items()], "1.0.0")
    )
    asyncio.run(store.upsert_reports("BTCUSDT", res.reports))
    return bars, res.items


def test_overlays_from_trend_items_and_replay_as_of(env) -> None:
    app, _ = env
    bars, _items = _seed_trend(app.state.trend_seed)
    end = bars[-1].close_time // 1000 + 3600
    with TestClient(app) as c:
        full = c.get("/api/overlays", params={"symbol": "BTCUSDT", "tf": "1h", "as_of": end}).json()
        kinds = {o["kind"] for o in full["overlays"]}
        assert {"swing_high", "swing_low", "bos"} <= kinds
        assert all(o["source_agent"] == "trend" and o["confirmed_at"] for o in full["overlays"])
        marker = next(o for o in full["overlays"] if o["kind"] == "swing_high")
        assert {"rule", "pivot_time", "confirmed_at", "level"} <= set(marker["meta"])
        # Replay at a mid point: exactly the items confirmed by then.
        mid = bars[len(bars) // 2].close_time // 1000
        part = c.get("/api/overlays", params={"symbol": "BTCUSDT", "tf": "1h", "as_of": mid}).json()
        assert 0 < len(part["overlays"]) < len(full["overlays"])
        assert all(o["confirmed_at"] <= mid for o in part["overlays"])
        hidden = {o["id"] for o in full["overlays"]} - {o["id"] for o in part["overlays"]}
        assert all(o["confirmed_at"] > mid for o in full["overlays"] if o["id"] in hidden)


def test_trend_report_endpoint(env) -> None:
    app, _ = env
    bars, _ = _seed_trend(app.state.trend_seed)
    mid = bars[len(bars) // 2].close_time // 1000
    with TestClient(app) as c:
        r = c.get("/api/agents/trend/report", params={"symbol": "BTCUSDT", "as_of": mid})
        assert r.status_code == 200
        body = r.json()
        assert body["agent"] == "trend" and body["as_of_ms"] <= mid * 1000
        assert body["timeframes"]["1h"]["trend"] in {"bullish", "bearish", "ranging"}
        assert (
            c.get("/api/agents/trend/report", params={"symbol": "BTCUSDT", "as_of": 1}).status_code
            == 404
        )
        assert c.get("/api/agents/trend/report", params={"symbol": "NOPEUSDT"}).status_code == 404


def test_overlay_contract_roundtrip() -> None:
    # The contract round-trips all three primitives.
    sample = ov.OverlayResponse(
        symbol="BTCUSDT",
        timeframe="15m",
        overlays=[
            {"type": "marker", "id": "m1", "kind": "swing_high", "source_agent": "trend",
             "agent_version": "1.0", "time": 1, "position": "above", "shape": "arrowDown"},
            {"type": "line", "id": "l1", "kind": "trendline", "source_agent": "trend",
             "agent_version": "1.0", "start_time": 1, "start_price": 1.0, "end_time": 2,
             "end_price": 2.0, "style": "dashed"},
            {"type": "zone", "id": "z1", "kind": "order_block", "source_agent": "trend",
             "agent_version": "1.0", "start_time": 1, "end_time": 2, "top": 2.0, "bottom": 1.0},
        ],  # type: ignore[list-item]
    )  # fmt: skip
    assert [o.type for o in sample.overlays] == ["marker", "line", "zone"]
    with pytest.raises(ValueError):
        ov.MarkerOverlay(id="x", kind="k", source_agent="a", agent_version="1", time=1,
                         position="left")  # type: ignore[arg-type]  # fmt: skip


def test_overlay_json_schema_file_is_up_to_date() -> None:
    path = Path(__file__).parents[1] / "docs" / "overlay.schema.json"
    assert json.loads(path.read_text(encoding="utf-8")) == ov.json_schema(), (
        "regenerate: uv run python -m deskmind.api.overlays > docs/overlay.schema.json"
    )


def test_no_write_or_order_endpoints(env) -> None:
    app, _ = env
    methods = {m for r in app.routes for m in getattr(r, "methods", set()) or set()}
    assert methods <= {"GET", "HEAD"}
    paths = " ".join(getattr(r, "path", "") for r in app.routes).lower()
    assert not any(w in paths for w in ("order", "trade", "position", "cancel"))


def test_ws_live_pushes_partial_and_closed(env) -> None:
    import asyncio

    app, redis = env
    iv = 900_000
    now = B.now_ms()
    last_open = (now // iv) * iv - iv
    k_closed = Kline.from_rest(row("BTCUSDT", "15m", last_open), "BTCUSDT", "15m")
    k_open = Kline.from_rest(row("BTCUSDT", "15m", last_open + iv), "BTCUSDT", "15m", closed=False)
    asyncio.run(B.put_closed(redis, [k_closed]))
    asyncio.run(B.put_open(redis, k_open, now))
    with TestClient(app) as c, c.websocket_connect("/ws/live?symbol=BTCUSDT&tf=15m") as ws:
        first, second = ws.receive_json(), ws.receive_json()
        assert first["type"] == "snapshot" and first["candle"]["time"] == last_open // 1000
        assert second["type"] == "partial" and second["is_open"] is True
        assert second["candle"]["time"] == (last_open + iv) // 1000
        # The forming candle closes: the stream announces it as closed.
        closed_now = Kline.from_rest(row("BTCUSDT", "15m", last_open + iv), "BTCUSDT", "15m")
        asyncio.run(B.put_closed(redis, [closed_now]))
        msg = ws.receive_json()
        assert msg["type"] == "closed" and msg["candle"]["time"] == (last_open + iv) // 1000


def test_ws_rejects_foreign_origin_and_unknown_symbol(env) -> None:
    from starlette.websockets import WebSocketDisconnect

    app, _ = env
    with TestClient(app) as c:
        with (
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect(
                "/ws/live?symbol=BTCUSDT&tf=15m", headers={"origin": "http://evil.example"}
            ) as ws,
        ):
            ws.receive_json()
        with (
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect("/ws/live?symbol=NOPEUSDT&tf=15m") as ws,
        ):
            ws.receive_json()


def test_cors_only_for_web_origin(env) -> None:
    app, _ = env
    with TestClient(app) as c:
        ok = c.get("/api/symbols", headers={"origin": "http://localhost:3010"})
        bad = c.get("/api/symbols", headers={"origin": "http://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3010"
    assert "access-control-allow-origin" not in bad.headers


def test_overlays_and_report_from_ict_smc(env) -> None:
    import asyncio

    from deskmind.agents.ict_smc.agent import SymbolIctSmcAgent, run_batch
    from deskmind.agents.ict_smc.config import DEFAULT_CONFIG as ICT_CFG

    app, _ = env
    bars, _ = _seed_trend(app.state.trend_seed)
    ict_agent = SymbolIctSmcAgent("BTCUSDT", ["1h"], ICT_CFG)
    res = run_batch(ict_agent, {"1h": bars})
    store = app.state.ict_smc_store
    asyncio.run(
        store.upsert_items(
            "BTCUSDT", [(tf, it) for (tf, _), it in res.items.items()], ICT_CFG.version
        )
    )
    asyncio.run(store.upsert_reports("BTCUSDT", res.reports))

    end = bars[-1].close_time // 1000 + 3600
    with TestClient(app) as c:
        full = c.get("/api/overlays", params={"symbol": "BTCUSDT", "tf": "1h", "as_of": end}).json()
        agents = {o["source_agent"] for o in full["overlays"]}
        assert "ict_smc" in agents

        rep = c.get("/api/agents/ict_smc/report", params={"symbol": "BTCUSDT", "as_of": end}).json()
        assert rep["agent"] == "ict_smc"
        assert rep["symbol"] == "BTCUSDT"
        assert "1h" in rep["timeframes"]
