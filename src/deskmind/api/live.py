"""Read-only views of the live service's Redis state (status + chart stream).

Only reads the documented Redis layout (deskmind.data.live.buffers); never writes to Redis.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from redis.asyncio import Redis
from starlette.websockets import WebSocket, WebSocketDisconnect

from deskmind.api.schemas import CandleOut, LiveStatusResponse, SymbolStatus, TimeframeStatus
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B

HEARTBEAT_FRESH_MS = 30_000
STALE_STREAM_S = 30.0
STALE_MARK_S = 10.0
WS_POLL_S = 0.5


def _age_s(ms: int | None, now: int) -> float | None:
    return None if not ms else round((now - int(ms)) / 1000, 1)


def kline_to_out(k: list[Any], source: str = "redis") -> CandleOut:
    return CandleOut(
        time=int(k[0]) // 1000,
        open=float(k[1]),
        high=float(k[2]),
        low=float(k[3]),
        close=float(k[4]),
        volume=float(k[5]),
        source=source,  # type: ignore[arg-type]
    )


async def live_status(
    redis: Redis, symbols: list[str], timeframes: list[str], now: int | None = None
) -> LiveStatusResponse:
    now = now if now is not None else B.now_ms()
    h = await redis.hgetall(B.HEALTH_SERVICE)
    streams = {k: int(v) for k, v in (await redis.hgetall(B.HEALTH_STREAMS)).items()}
    hb = int(h.get("heartbeat", 0) or 0)
    running = bool(hb) and now - hb < HEARTBEAT_FRESH_MS and h.get("state") != "stopped"
    reasons: list[str] = []
    red = amber = False
    if not running:
        red = True
        reasons.append(
            "live service not running" + (f" (heartbeat {_age_s(hb, now)}s ago)" if hb else "")
        )
    elif h.get("ws_state") != "connected":
        red = True
        reasons.append(f"websocket {h.get('ws_state')}")
    stale_streams = json.loads(h.get("stale_streams", "[]") or "[]") if running else []
    if stale_streams:
        amber = True
        reasons.append(f"{len(stale_streams)} stale streams")
    if running and h.get("state") == "degraded":
        amber = True

    out: list[SymbolStatus] = []
    for s in symbols:
        tfs: list[TimeframeStatus] = []
        for tf in timeframes:
            iv = TIMEFRAME_MS[tf]
            last = await B.get_last_closed(redis, s, tf)
            size = await B.buffer_size(redis, s, tf)
            oc = await B.get_open_candle(redis, s, tf)
            expected_last_open = (now // iv) * iv - iv
            behind = (expected_last_open - last.open_time) // iv if last else None
            stream_age = _age_s(streams.get(f"{s.lower()}@kline_{tf}"), now)
            # A closed candle may take a few seconds to arrive after the boundary.
            late = behind is not None and behind > 0 and (now - (expected_last_open + iv)) > 15_000
            stale = (stream_age is None or stream_age > STALE_STREAM_S) or behind is None or late
            if stale:
                amber = True
                reasons.append(f"{s} {tf} stale")
            tfs.append(
                TimeframeStatus(
                    timeframe=tf,
                    last_closed_open_time=last.open_time // 1000 if last else None,
                    buffer_size=size,
                    stream_age_s=stream_age,
                    open_candle_age_s=_age_s(oc["updated_at"], now) if oc else None,
                    behind_candles=max(behind, 0) if behind is not None else None,
                    stale=stale,
                )
            )
        m = await B.get_mark(redis, s) or {}
        oi = await B.get_open_interest(redis, s) or {}
        mark_age = _age_s(m.get("event_time"), now)
        if mark_age is None or mark_age > STALE_MARK_S:
            amber = True
            reasons.append(f"{s} mark price stale")
        last_ev = await redis.get(B.last_emitted_key(s))
        out.append(
            SymbolStatus(
                symbol=s,
                timeframes=tfs,
                mark_price=m.get("mark_price"),
                funding_rate=m.get("funding_rate"),
                next_funding_time=m["next_funding_time"] // 1000
                if m.get("next_funding_time")
                else None,
                mark_age_s=mark_age,
                open_interest=oi.get("open_interest"),
                open_interest_age_s=_age_s(oi.get("received_at"), now),
                last_bar_close=int(last_ev) // 1000 if last_ev else None,
            )
        )
    level = "red" if red else "amber" if amber else "green"
    if not running:  # everything is stale when the service is down; keep one clear reason
        reasons = reasons[:1]
    return LiveStatusResponse(
        level=level,
        reasons=reasons,
        running=running,
        state=h.get("state"),
        ws_state=h.get("ws_state"),
        heartbeat_age_s=_age_s(hb, now),
        reconnects=int(h["reconnects"]) if h.get("reconnects") else None,
        clock_drift_ms=int(h["clock_drift_ms"]) if h.get("clock_drift_ms") else None,
        stale_streams=stale_streams,
        symbols=out,
    )


async def stream_chart(ws: WebSocket, redis: Redis, symbol: str, tf: str) -> None:
    """Push the forming candle (in place) and each newly closed candle, read from Redis.

    Messages: {"type": "snapshot"|"partial"|"closed", "symbol", "timeframe", "candle", "is_open"}
    """
    last_partial: str | None = None
    last_closed_t: int | None = None

    async def send(kind: str, candle: CandleOut, is_open: bool) -> None:
        await ws.send_json(
            {
                "type": kind,
                "symbol": symbol,
                "timeframe": tf,
                "is_open": is_open,
                "candle": candle.model_dump(),
            }
        )

    try:
        first = True
        while True:
            closed = await B.get_last_closed(redis, symbol, tf)
            if closed and closed.open_time != last_closed_t:
                last_closed_t = closed.open_time
                await send("snapshot" if first else "closed", kline_to_out(closed.compact()), False)
            oc = await B.get_open_candle(redis, symbol, tf)
            if oc:
                key = json.dumps(oc["k"])
                is_current = last_closed_t is None or int(oc["k"][0]) > last_closed_t
                if key != last_partial and is_current:
                    last_partial = key
                    await send("partial", kline_to_out(oc["k"]), True)
            first = False
            await asyncio.sleep(WS_POLL_S)
    except (WebSocketDisconnect, RuntimeError):
        return
