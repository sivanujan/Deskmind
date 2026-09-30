"""Live runner and history backfill for the Price Action agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from deskmind.agents.candles import load_bars, redis_bars
from deskmind.agents.price_action.agent import PriceActionAgent, run_batch
from deskmind.agents.price_action.config import AGENT_NAME, DEFAULT_CONFIG, PriceActionConfig
from deskmind.agents.price_action.history import PriceActionTransitionRecorder as TransitionRecorder
from deskmind.agents.price_action.store import PriceActionStore
from deskmind.agents.trend.models import Bar
from deskmind.api.candles import RecentCandleSource
from deskmind.config import Settings
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B
from deskmind.logging import get_logger

log = get_logger(__name__)

HEALTH_KEY = f"{B.PREFIX}:agents:{AGENT_NAME}:health"
ERRORS_KEY = f"{B.PREFIX}:agents:{AGENT_NAME}:errors"


async def record_error(redis: Redis, error: str) -> None:
    now = B.now_ms()
    pipe = redis.pipeline(transaction=False)
    pipe.zadd(ERRORS_KEY, {f"{now}:{error[:300]}": now})
    pipe.zremrangebyscore(ERRORS_KEY, 0, now - 7 * 86_400_000)
    pipe.hset(HEALTH_KEY, mapping={"last_error": error[:300], "last_error_at": now})
    await pipe.execute()


# ---------------------------------------------------------------- backfill

async def backfill(
    symbol: str,
    timeframes: list[str],
    start_ms: int,
    end_ms: int,
    *,
    data_dir: Any,
    recent: RecentCandleSource | None,
    store: PriceActionStore,
    cfg: PriceActionConfig = DEFAULT_CONFIG,
    debug_report_days: float = 0.0,
    trend_store: Any = None,  # for fetching trend swings
) -> dict[str, Any]:
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    bars: dict[str, list[Bar]] = {}
    for tf in timeframes:
        warm = start_ms - cfg.warmup_bars * TIMEFRAME_MS[tf]
        bars[tf] = await load_bars(data_dir, recent, symbol, tf, start_ms=warm, end_ms=end_ms)
    timings["load_s"] = time.perf_counter() - t0

    # Fetch trend swings if store provided
    swings_by_tf: dict[str, list[dict[str, Any]]] = {}
    if trend_store is not None:
        from deskmind.agents.trend.config import AGENT_VERSION as TREND_VERSION
        for tf in timeframes:
            try:
                swings_by_tf[tf] = await trend_store.items(
                    symbol, tf, start_ms - cfg.warmup_bars * TIMEFRAME_MS[tf], end_ms, None
                )
            except Exception:
                swings_by_tf[tf] = []

    agent = PriceActionAgent(symbol, cfg)
    t0 = time.perf_counter()
    report_from = end_ms - int(debug_report_days * 86_400_000) if debug_report_days else None
    result = await asyncio.to_thread(
        run_batch,
        agent,
        bars,
        swings_by_tf,
        report_from=report_from,
        store_from=start_ms,
        reports=bool(debug_report_days),
        recorder=TransitionRecorder(),
    )
    timings["replay_s"] = time.perf_counter() - t0

    rows = result.recorder.rows if result.recorder else []
    t0 = time.perf_counter()
    for tf, items in result.items.items():
        await store.upsert_items(symbol, tf, items, cfg.version)
    timings["write_items_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    # Transitions are stored per-tf via the rows (grouped by tf field)
    from deskmind.agents.transitions import rows_by_tf
    tf_rows = rows_by_tf(rows)
    for tf, tr in tf_rows.items():
        await store.replace_transitions(symbol, cfg.version, tf, start_ms, tr)
    timings["write_transitions_s"] = time.perf_counter() - t0

    if result.reports:
        for tf, rpts in result.reports.items():
            await store.upsert_reports(symbol, tf, rpts)

    counts: dict[str, dict[str, int]] = {}
    for tf, items in result.items.items():
        cnts: dict[str, int] = {}
        for item in items:
            cnts[item["kind"]] = cnts.get(item["kind"], 0) + 1
        counts[tf] = cnts

    total_items = sum(len(v) for v in result.items.values())
    return {
        "symbol": symbol,
        "candles": {tf: len(b) for tf, b in bars.items()},
        "reports": sum(len(r) for r in result.reports.values()),
        "items": total_items,
        "transitions": len(rows),
        "counts": counts,
        "timings": {k: round(v, 1) for k, v in timings.items()},
    }


# ---------------------------------------------------------------- live


class LivePriceActionRunner:
    def __init__(
        self,
        redis: Redis,
        store: PriceActionStore,
        recent: RecentCandleSource | None,
        settings: Settings,
        cfg: PriceActionConfig = DEFAULT_CONFIG,
        symbol_source: Callable[[], Awaitable[list[str]]] | None = None,
        refresh_s: float = 60.0,
        trend_store: Any = None,
    ) -> None:
        self.redis = redis
        self.symbol_source = symbol_source
        self.refresh_s = refresh_s
        self.store = store
        self.recent = recent
        self.settings = settings
        self.cfg = cfg
        self.trend_store = trend_store
        self.timeframes = list(settings.live_timeframes)
        self.agents: dict[str, PriceActionAgent] = {}
        self.recorders: dict[str, TransitionRecorder] = {}
        self.reports = 0
        self._last_prune = 0.0

    async def _symbols(self) -> list[str]:
        if self.symbol_source is None:
            return list(self.settings.data_symbols)
        return await self.symbol_source()

    async def warm_symbol(self, symbol: str, now_ms: int) -> None:
        bars = {
            tf: await load_bars(
                self.settings.data_dir,
                self.recent,
                symbol,
                tf,
                end_ms=now_ms,
                last_n=self.cfg.warmup_bars,
            )
            for tf in self.timeframes
        }
        agent = PriceActionAgent(symbol, self.cfg)
        store_from = now_ms - self.cfg.warmup_bars * min(TIMEFRAME_MS[tf] for tf in self.timeframes) // 2
        recorder = TransitionRecorder()
        result = await asyncio.to_thread(
            run_batch, agent, bars, {}, store_from=store_from, reports=False, recorder=recorder
        )
        for tf, items in result.items.items():
            await self.store.upsert_items(symbol, tf, items, self.cfg.version)
        from deskmind.agents.transitions import rows_by_tf
        for tf, tr in rows_by_tf(recorder.rows).items():
            await self.store.replace_transitions(symbol, self.cfg.version, tf, store_from, tr)
        recorder.rows = []
        self.agents[symbol] = agent
        self.recorders[symbol] = recorder
        log.info("price_action_warm", symbol=symbol, items=sum(len(v) for v in result.items.values()))

    async def sync_symbols(self, desired: list[str], now_ms: int) -> None:
        for symbol in [s for s in self.agents if s not in desired]:
            del self.agents[symbol]
            self.recorders.pop(symbol, None)
        for symbol in [s for s in desired if s not in self.agents]:
            try:
                await self.warm_symbol(symbol, now_ms)
            except Exception as exc:
                log.error("price_action_warm_failed", symbol=symbol, error=str(exc))
                await record_error(self.redis, f"{symbol}: {exc}")

    async def on_bar_close(self, event: dict[str, Any]) -> None:
        agent = self.agents.get(event["symbol"])
        if agent is None:
            return
        close_time = int(event["close_time"])
        bars: dict[str, list[Bar]] = {}
        for tf in event["closed_timeframes"]:
            raw = await redis_bars(self.redis, event["symbol"], tf, 50)
            bars[tf] = [b for b in raw if b.close_time <= close_time]

        out = agent.on_bar_close(close_time, bars, with_report=True)
        recorder = self.recorders.get(agent.symbol)
        for tf, chs in out.changes.items():
            if chs:
                await self.store.upsert_items(agent.symbol, tf, chs, self.cfg.version)
        if recorder is not None:
            flat = [(tf, ch) for tf, chs in out.changes.items() for ch in chs]
            recorder.boundary(close_time, agent, flat)
            from deskmind.agents.transitions import rows_by_tf
            for tf, tr in rows_by_tf(recorder.rows).items():
                await self.store.append_transitions(agent.symbol, self.cfg.version, tf, tr)
            recorder.rows = []
        for tf, rpt in out.reports.items():
            await self.store.upsert_reports(agent.symbol, tf, [rpt])
        await self._prune_reports()
        self.reports += len(out.reports)
        await self.redis.hset(HEALTH_KEY, mapping={"state": "running", "heartbeat": B.now_ms(), "reports": self.reports})

    async def _prune_reports(self) -> None:
        if time.monotonic() - self._last_prune < 3600:
            return
        self._last_prune = time.monotonic()
        retention = getattr(self.settings, "price_action_report_retention_days", 3.0)
        cutoff = B.now_ms() - int(retention * 86_400_000)
        for symbol in list(self.agents):
            try:
                await self.store.prune_reports(symbol, cutoff)
            except Exception as exc:
                log.warning("price_action_prune_failed", symbol=symbol, error=str(exc))

    async def run(self, stop: asyncio.Event) -> None:
        await self.sync_symbols(await self._symbols(), B.now_ms())
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(B.BAR_CLOSE_CHANNEL)
        log.info("price_action_agent_listening", channel=B.BAR_CLOSE_CHANNEL)
        last_refresh = time.monotonic()
        try:
            while not stop.is_set():
                if self.symbol_source is not None and time.monotonic() - last_refresh >= self.refresh_s:
                    last_refresh = time.monotonic()
                    with contextlib.suppress(Exception):
                        await self.sync_symbols(await self.symbol_source(), B.now_ms())
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        await self.on_bar_close(json.loads(msg["data"]))
                    except Exception as exc:
                        log.error("price_action_event_failed", error=str(exc))
                        await record_error(self.redis, str(exc))
                await self.redis.hset(HEALTH_KEY, mapping={"state": "running", "heartbeat": B.now_ms()})
        finally:
            await pubsub.unsubscribe(B.BAR_CLOSE_CHANNEL)
            await pubsub.aclose()


async def run_live(settings: Settings, duration_s: float | None = None) -> None:
    from deskmind.agents.price_action.store import PostgresPriceActionStore
    from deskmind.api.candles import PostgresRecentCandles
    from deskmind.cache.redis_client import create_redis
    from deskmind.db.session import create_engine

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    if duration_s:
        loop.call_later(duration_s, stop.set)

    redis = create_redis(settings)
    engine = create_engine(settings)
    from deskmind.agents.trend.store import PostgresTrendStore
    from deskmind.universe.store import PostgresUniverseStore
    from deskmind.universe.symbols import active_symbols

    ustore = PostgresUniverseStore()

    async def symbol_source() -> list[str]:
        return await active_symbols(list(settings.data_symbols), ustore)

    runner = LivePriceActionRunner(
        redis,
        PostgresPriceActionStore(engine),
        PostgresRecentCandles(engine),
        settings,
        symbol_source=symbol_source,
        trend_store=PostgresTrendStore(engine),
    )
    try:
        await runner.run(stop)
    finally:
        with contextlib.suppress(Exception):
            await redis.hset(HEALTH_KEY, mapping={"state": "stopped", "heartbeat": B.now_ms()})
        await engine.dispose()
        await redis.aclose()


def parse_day(s: str) -> int:
    dt = datetime.fromisoformat(s)
    dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
