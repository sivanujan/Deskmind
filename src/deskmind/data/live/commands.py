"""Async implementations behind `deskmind live ...` commands."""

from __future__ import annotations

import asyncio
import itertools
import json
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from deskmind.cache.redis_client import create_redis
from deskmind.config import Settings
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.http_client import RateLimitedClient
from deskmind.data.live import buffers as B
from deskmind.data.live.rest import fetch_closed_klines
from deskmind.logging import get_logger

log = get_logger(__name__)
Echo = Callable[[str], None]

HEARTBEAT_FRESH_MS = 30_000


def _iso(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _age(ms: int | None, now: int) -> str:
    if not ms:
        return "-"
    s = (now - ms) / 1000
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f}m" if s < 7200 else f"{s / 3600:.1f}h"


# ---------------------------------------------------------------- run


async def run_service(settings: Settings, duration_s: float | None = None) -> None:
    from deskmind.data.live.service import LiveService
    from deskmind.db.candles import PostgresCandleSink
    from deskmind.db.session import create_engine

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    if duration_s:
        loop.call_later(duration_s, stop.set)

    redis = create_redis(settings)
    engine = create_engine(settings)
    rest = RateLimitedClient(settings.binance_market_data_url, max_concurrency=4)
    service = LiveService(
        redis,
        PostgresCandleSink(engine),
        rest,
        symbols=list(settings.data_symbols),
        timeframes=list(settings.live_timeframes),
        ws_url=settings.binance_ws_market_url,
    )
    log.info(
        "live_service_starting",
        symbols=settings.data_symbols,
        timeframes=settings.live_timeframes,
        duration_s=duration_s,
    )
    try:
        await service.run(stop)
    finally:
        await rest.aclose()
        await engine.dispose()
        await redis.aclose()


# ---------------------------------------------------------------- status


async def status(settings: Settings, echo: Echo) -> dict[str, Any]:
    redis = create_redis(settings)
    try:
        now = B.now_ms()
        h = await redis.hgetall(B.HEALTH_SERVICE)
        streams = {k: int(v) for k, v in (await redis.hgetall(B.HEALTH_STREAMS)).items()}
        hb = int(h.get("heartbeat", 0) or 0)
        running = hb and now - hb < HEARTBEAT_FRESH_MS and h.get("state") != "stopped"
        echo(
            f"service: {'RUNNING' if running else 'NOT RUNNING'}  state={h.get('state', '-')}  "
            f"ws={h.get('ws_state', '-')}  heartbeat={_age(hb, now)} ago  "
            f"up since {_iso(int(h.get('started_at', 0) or 0))}"
        )
        echo(
            f"connected since {_iso(int(h.get('connected_since', 0) or 0))}  "
            f"reconnects={h.get('reconnects', 0)}  last disconnect: "
            f"{h.get('last_disconnect_reason') or '-'}"
        )
        echo(
            f"clock drift={h.get('clock_drift_ms', '-')}ms (checked "
            f"{_age(int(h.get('clock_checked_at', 0) or 0), now)} ago)  "
            f"messages={h.get('messages')}  events={h.get('events_emitted')} "
            f"degraded={h.get('events_degraded')} "
            f"dup_suppressed={h.get('duplicates_suppressed')}  backfilled="
            f"{h.get('candles_backfilled')}  db_errors={h.get('db_errors')}"
        )
        stale = json.loads(h.get("stale_streams", "[]") or "[]")
        echo(f"stale streams: {', '.join(stale) if stale else 'none'}")
        echo("")
        echo(
            f"{'symbol':<9}{'tf':<5}{'last closed (UTC)':<21}{'buffer':>7}  "
            f"{'stream age':>10}  {'open candle':>11}"
        )
        for s in settings.data_symbols:
            for tf in settings.live_timeframes:
                last = await B.get_last_closed(redis, s, tf)
                size = await B.buffer_size(redis, s, tf)
                oc = await B.get_open_candle(redis, s, tf)
                st = streams.get(f"{s.lower()}@kline_{tf}")
                echo(
                    f"{s:<9}{tf:<5}{_iso(last.open_time) if last else '-':<21}{size:>7}  "
                    f"{_age(st, now):>10}  {_age(oc['updated_at'], now) if oc else '-':>11}"
                )
        echo("")
        echo(
            f"{'symbol':<9}{'mark price':>14}{'funding':>11}{'next funding (UTC)':>21}"
            f"{'mark age':>10}{'open interest':>16}{'OI age':>9}{'last bar_close (UTC)':>22}"
        )
        for s in settings.data_symbols:
            m = await B.get_mark(redis, s) or {}
            oi = await B.get_open_interest(redis, s) or {}
            last_ev = await redis.get(B.last_emitted_key(s))
            fr = f"{float(m['funding_rate']):.4%}" if m.get("funding_rate") else "-"
            echo(
                f"{s:<9}{m.get('mark_price', '-'):>14}{fr:>11}"
                f"{_iso(m.get('next_funding_time')):>21}{_age(m.get('event_time'), now):>10}"
                f"{oi.get('open_interest', '-'):>16}{_age(oi.get('received_at'), now):>9}"
                f"{_iso(int(last_ev) if last_ev else None):>22}"
            )
        return {"running": bool(running), "health": h, "stale": stale}
    finally:
        await redis.aclose()


# ---------------------------------------------------------------- tail


def format_event(ev: dict[str, Any]) -> str:
    flag = f"  DEGRADED missing={ev['missing']}" if ev.get("degraded") else ""
    lag = (ev.get("emitted_at", 0) - ev["close_time"]) / 1000
    return (
        f"{ev['close_time_iso']}  {ev['symbol']:<8} {','.join(ev['closed_timeframes']):<14} "
        f"(+{lag:.1f}s){flag}"
    )


async def tail(settings: Settings, echo: Echo, history: int = 0) -> None:
    redis = create_redis(settings)
    try:
        if history:
            for _id, fields in reversed(await redis.xrevrange(B.EVENT_LOG, count=history)):
                echo(format_event(json.loads(fields["event"])))
            echo("---- live ----")
        pubsub = redis.pubsub()
        await pubsub.subscribe(B.BAR_CLOSE_CHANNEL)
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                echo(format_event(json.loads(msg["data"])))
    finally:
        await redis.aclose()


# ---------------------------------------------------------------- disconnect test hook


async def block_ws(settings: Settings, seconds: int) -> int:
    redis = create_redis(settings)
    try:
        until = B.now_ms() + seconds * 1000
        await redis.set(B.WS_BLOCK_UNTIL, until, px=seconds * 1000 + 60_000)
        return until
    finally:
        await redis.aclose()


# ---------------------------------------------------------------- verify


async def events_between(redis: Any, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    out = []
    for _id, fields in await redis.xrange(B.EVENT_LOG):
        ev = json.loads(fields["event"])
        if start_ms <= ev["close_time"] <= end_ms:
            out.append(ev)
    return out


async def verify(settings: Settings, echo: Echo, since: datetime, until: datetime) -> bool:
    """Live-stored candles vs fresh REST (exact), and the bar_close event sequence."""
    from deskmind.db.candles import PostgresCandleSink
    from deskmind.db.session import create_engine

    redis = create_redis(settings)
    engine = create_engine(settings)
    rest = RateLimitedClient(settings.binance_market_data_url, max_concurrency=4)
    sink = PostgresCandleSink(engine)
    start_ms, end_ms = int(since.timestamp() * 1000), int(until.timestamp() * 1000)
    ok = True
    try:
        echo(f"window: {_iso(start_ms)} -> {_iso(end_ms)} UTC")
        echo(
            f"{'symbol':<9}{'tf':<5}{'REST':>6}{'redis':>7}{'redis=REST':>11}"
            f"{'postgres':>9}{'pg=REST':>9}"
        )
        totals = {"rest": 0, "redis_bad": 0, "pg_bad": 0}
        for s in settings.data_symbols:
            for tf in settings.live_timeframes:
                iv = TIMEFRAME_MS[tf]
                lo = -(-start_ms // iv) * iv
                fresh = await fetch_closed_klines(rest, s, tf, start_ms=lo, end_ms=end_ms)
                buf = {
                    k.open_time: k
                    for k in await B.get_candles(redis, s, tf)
                    if lo <= k.open_time < end_ms
                }
                pg = {
                    int(c.open_time.timestamp() * 1000): c
                    for c in await sink.fetch(s, tf, datetime.fromtimestamp(lo / 1000, UTC), until)
                }
                r_bad = sum(
                    1
                    for k in fresh
                    if k.open_time not in buf or not k.same_values(buf[k.open_time])
                ) + len(set(buf) - {k.open_time for k in fresh})
                p_bad = sum(1 for k in fresh if not _pg_equal(pg.get(k.open_time), k)) + len(
                    set(pg) - {k.open_time for k in fresh}
                )
                totals["rest"] += len(fresh)
                totals["redis_bad"] += r_bad
                totals["pg_bad"] += p_bad
                echo(
                    f"{s:<9}{tf:<5}{len(fresh):>6}{len(buf):>7}"
                    f"{'OK' if not r_bad else f'{r_bad} DIFF':>11}{len(pg):>9}"
                    f"{'OK' if not p_bad else f'{p_bad} DIFF':>9}"
                )
        echo(
            f"candles compared: {totals['rest']}  redis mismatches: {totals['redis_bad']}  "
            f"postgres mismatches: {totals['pg_bad']}"
        )
        ok = totals["redis_bad"] == 0 and totals["pg_bad"] == 0

        echo("")
        echo("bar_close events (from the event log):")
        events = await events_between(redis, start_ms, end_ms)
        base = min(TIMEFRAME_MS[tf] for tf in settings.live_timeframes)
        for s in settings.data_symbols:
            times = [e["close_time"] for e in events if e["symbol"] == s]
            dups = len(times) - len(set(times))
            order = sum(1 for a, b in itertools.pairwise(times) if b <= a)
            if times:
                expected = set(range(times[0], times[-1] + 1, base))
                skipped = sorted(expected - set(times))
            else:
                skipped = []
            degraded = sum(1 for e in events if e["symbol"] == s and e.get("degraded"))
            wrong_tfs = sum(
                1
                for e in events
                if e["symbol"] == s
                and not e.get("degraded")
                and sorted(e["closed_timeframes"])
                != sorted(
                    tf for tf in settings.live_timeframes if e["close_time"] % TIMEFRAME_MS[tf] == 0
                )
            )
            echo(
                f"  {s:<9} events={len(times):>4}  first={_iso(times[0]) if times else '-'}  "
                f"last={_iso(times[-1]) if times else '-'}  duplicates={dups}  "
                f"out_of_order={order}  skipped={len(skipped)}  degraded={degraded}  "
                f"wrong_timeframes={wrong_tfs}"
            )
            ok = ok and not (dups or order or skipped or wrong_tfs)
        return ok
    finally:
        await rest.aclose()
        await engine.dispose()
        await redis.aclose()


def _pg_equal(row: Any, k: B.Kline) -> bool:
    if row is None:
        return False
    return (row.open, row.high, row.low, row.close, row.volume, row.quote_volume) == (
        k.open,
        k.high,
        k.low,
        k.close,
        k.volume,
        k.quote_volume,
    )


async def live_health(settings: Settings) -> tuple[bool, str]:
    redis = create_redis(settings)
    try:
        h = await redis.hgetall(B.HEALTH_SERVICE)
    finally:
        await redis.aclose()
    now = B.now_ms()
    hb = int(h.get("heartbeat", 0) or 0)
    if not hb or now - hb > HEARTBEAT_FRESH_MS or h.get("state") == "stopped":
        return False, f"not running (last heartbeat {_age(hb, now)} ago)" if hb else "not running"
    stale = json.loads(h.get("stale_streams", "[]") or "[]")
    detail = (
        f"state={h.get('state')} ws={h.get('ws_state')} reconnects={h.get('reconnects')} "
        f"stale={len(stale)} clock_drift={h.get('clock_drift_ms')}ms"
    )
    return h.get("state") == "running" and not stale, detail


def parse_since(hours: float | None, since: str | None) -> datetime:
    if since:
        dt = datetime.fromisoformat(since)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return datetime.now(UTC) - timedelta(hours=hours or 1)
