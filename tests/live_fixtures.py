"""Fakes for live-service tests: WebSocket messages, a fake Binance REST, scripted sockets."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx

from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.http_client import RateLimitedClient
from tests.data_fixtures import FakeClock, kline_row


def ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp() * 1000)


def row(symbol: str, tf: str, open_time: int) -> list[Any]:
    base = {"BTCUSDT": 42000.0, "ETHUSDT": 2300.0}.get(symbol, 100.0)
    r = kline_row(open_time, TIMEFRAME_MS[tf], base)
    return [int(r[0]), *r[1:6], int(r[6]), r[7], int(r[8]), *r[9:]]


def ws_kline(symbol: str, tf: str, open_time: int, closed: bool, event_time: int = 0) -> dict:
    r = row(symbol, tf, open_time)
    k = {
        "t": r[0], "T": r[6], "s": symbol, "i": tf, "o": r[1], "c": r[4], "h": r[2], "l": r[3],
        "v": r[5], "n": r[8], "x": closed, "q": r[7], "V": r[9], "Q": r[10], "B": "0",
    }  # fmt: skip
    return {"e": "kline", "E": event_time or r[6], "s": symbol, "k": k}


def combined(stream: str, data: dict) -> str:
    return json.dumps({"stream": stream, "data": data})


class FakeRest:
    """Serves /fapi/v1/klines from `truth`; candles absent from truth do not exist."""

    def __init__(self) -> None:
        self.truth: dict[tuple[str, str], dict[int, list[Any]]] = {}
        self.calls: list[dict[str, str]] = []
        self.fail = False

    def add(self, symbol: str, tf: str, open_times: list[int]) -> None:
        d = self.truth.setdefault((symbol, tf), {})
        for t in open_times:
            d[t] = row(symbol, tf, t)

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.calls.append({"path": request.url.path, **q})
        if self.fail:
            return httpx.Response(503)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 0})
        if request.url.path == "/fapi/v1/openInterest":
            return httpx.Response(
                200, json={"openInterest": "1.0", "time": 1, "symbol": q["symbol"]}
            )
        rows = sorted(self.truth.get((q["symbol"], q["interval"]), {}).items())
        limit = int(q.get("limit", 500))
        if "startTime" in q:
            lo, hi = int(q["startTime"]), int(q.get("endTime", 2**62))
            sel = [r for t, r in rows if lo <= t <= hi][:limit]
        else:
            sel = [r for _, r in rows][-limit:]
        return httpx.Response(200, json=sel)

    def client(self) -> RateLimitedClient:
        clock = FakeClock()
        return RateLimitedClient(
            "https://fapi.test",
            transport=httpx.MockTransport(self.handler),
            sleep=clock.sleep,
            clock=clock,
            max_retries=0,
        )


class ScriptedSocket:
    """recv() pops messages; each item may be a str or a callable run before continuing."""

    def __init__(self, script: list[str | Callable[[], Any]], on_empty: Callable[[], Any]) -> None:
        self.script = list(script)
        self.on_empty = on_empty
        self.closed = False

    async def recv(self) -> str:
        while self.script:
            item = self.script.pop(0)
            if callable(item):
                res = item()
                if hasattr(res, "__await__"):
                    await res
                continue
            return item
        res = self.on_empty()
        if hasattr(res, "__await__"):
            await res
        raise ConnectionError("scripted disconnect")

    async def close(self) -> None:
        self.closed = True


def scripted_connector(sockets: list[ScriptedSocket]):
    queue = list(sockets)

    @asynccontextmanager
    async def connect(url: str):
        if not queue:
            raise ConnectionError("no more scripted connections")
        yield queue.pop(0)

    return connect
