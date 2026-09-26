"""REST helpers for the live service (Decimal-exact), on top of the shared rate-limited client."""

from __future__ import annotations

from typing import Any

from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.live.buffers import Kline, now_ms
from deskmind.data.rest_backfill import kline_weight

MAX_LIMIT = 1500


async def fetch_closed_klines(
    client: RateLimitedClient,
    symbol: str,
    tf: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 500,
    now: int | None = None,
) -> list[Kline]:
    """Closed candles only (open_time + interval <= now), oldest first.

    With start_ms: every closed candle from start_ms (inclusive) up to end_ms/now (paginated).
    Without: the latest `limit` closed candles.
    """
    iv = TIMEFRAME_MS[tf]
    now = now if now is not None else now_ms()
    closed_before = (now // iv) * iv  # open_time must be < this to be closed
    end = min(end_ms, closed_before) if end_ms is not None else closed_before
    out: list[Kline] = []
    if start_ms is None:
        req = min(limit + 1, MAX_LIMIT)  # +1: the newest row may be the forming candle
        rows = await client.get_json(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": tf, "limit": req},
            weight=kline_weight(req),
        )
        out = [Kline.from_rest(r, symbol, tf) for r in rows if int(r[0]) < end]
        return out[-limit:]
    cursor = start_ms
    while cursor < end:
        req = int(min(MAX_LIMIT, max((end - cursor) // iv, 1)))
        rows = await client.get_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": tf,
                "startTime": cursor,
                "endTime": end - 1,
                "limit": req,
            },
            weight=kline_weight(req),
        )
        if not rows:
            break
        out.extend(Kline.from_rest(r, symbol, tf) for r in rows if cursor <= int(r[0]) < end)
        nxt = int(rows[-1][0]) + iv
        if nxt <= cursor:
            break
        cursor = nxt
    return out


async def fetch_open_interest(client: RateLimitedClient, symbol: str) -> dict[str, Any]:
    data = await client.get_json("/fapi/v1/openInterest", {"symbol": symbol}, weight=1)
    return {"open_interest": data["openInterest"], "time": int(data["time"])}


async def server_time_drift_ms(client: RateLimitedClient) -> tuple[int, int]:
    """(server - local clock in ms, round trip ms), measured at the request midpoint."""
    t0 = now_ms()
    data = await client.get_json("/fapi/v1/time", weight=1)
    t1 = now_ms()
    return int(data["serverTime"]) - (t0 + t1) // 2, t1 - t0
