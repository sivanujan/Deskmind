"""Live runner and history backfill for the trend agent (both drive SymbolTrendAgent).

Live: warm up each symbol by replaying the last `warmup_bars` candles per timeframe through
`run_batch` (same code path), then react to `deskmind:bar_close` events: feed the newly closed
candles from the Redis buffers, store items + the report, publish `deskmind:agent_report`.
The live data service is only read (Redis buffers, Postgres candles), never modified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from deskmind.agents.candles import load_bars, redis_bars
from deskmind.agents.trend.agent import SymbolTrendAgent, run_batch
from deskmind.agents.trend.config import AGENT_NAME, DEFAULT_CONFIG, TrendConfig
from deskmind.agents.trend.models import Bar
from deskmind.agents.trend.store import TrendStore
from deskmind.api.candles import RecentCandleSource
from deskmind.config import Settings
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B
from deskmind.logging import get_logger

log = get_logger(__name__)

AGENT_REPORT_CHANNEL = "deskmind:agent_report"
HEALTH_KEY = f"{B.PREFIX}:agents:{AGENT_NAME}:health"


# ---------------------------------------------------------------- batch / backfill


async def backfill(
    symbol: str,
    timeframes: list[str],
    start_ms: int,
    end_ms: int,
    *,
    data_dir: Any,
    recent: RecentCandleSource | None,
    store: TrendStore,
    cfg: TrendConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Replay [start - warm-up, end] and store reports + items for [start, end]."""
    bars: dict[str, list[Bar]] = {}
    for tf in timeframes:
        warm = start_ms - cfg.warmup_bars * TIMEFRAME_MS[tf]
        bars[tf] = await load_bars(data_dir, recent, symbol, tf, start_ms=warm, end_ms=end_ms)
    agent = SymbolTrendAgent(symbol, timeframes, cfg)
    result = await asyncio.to_thread(
        run_batch, agent, bars, report_from=start_ms, store_from=start_ms
    )
    items = [(tf, item) for (tf, _), item in result.items.items()]
    await store.upsert_items(symbol, items, cfg.version)
    await store.upsert_reports(symbol, result.reports)
    counts: dict[str, dict[str, int]] = {}
    for tf, item in items:
        counts.setdefault(tf, {}).setdefault(item["kind"], 0)
        counts[tf][item["kind"]] += 1
    return {
        "symbol": symbol,
        "candles": {tf: len(b) for tf, b in bars.items()},
        "reports": len(result.reports),
        "items": len(items),
        "counts": counts,
    }


# ---------------------------------------------------------------- live


class LiveTrendRunner:
    def __init__(
        self,
        redis: Redis,
        store: TrendStore,
        recent: RecentCandleSource | None,
        settings: Settings,
        cfg: TrendConfig = DEFAULT_CONFIG,
    ) -> None:
        self.redis = redis
        self.store = store
        self.recent = recent
        self.settings = settings
        self.cfg = cfg
        self.timeframes = list(settings.live_timeframes)
        self.agents: dict[str, SymbolTrendAgent] = {}
        self.reports = 0

    async def warm_up(self, now_ms: int) -> None:
        for symbol in self.settings.data_symbols:
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
            agent = SymbolTrendAgent(symbol, self.timeframes, self.cfg)
            # Items from the newer half of the warm-up are (re)written; older ones already exist.
            base = min(TIMEFRAME_MS[tf] for tf in self.timeframes)
            store_from = now_ms - (self.cfg.warmup_bars // 2) * base
            result = await asyncio.to_thread(run_batch, agent, bars, store_from=store_from)
            await self.store.upsert_items(
                symbol, [(tf, it) for (tf, _), it in result.items.items()], self.cfg.version
            )
            if result.reports:
                await self.store.upsert_reports(symbol, result.reports[-1:])
            self.agents[symbol] = agent
            log.info(
                "trend_warm",
                symbol=symbol,
                candles={tf: len(b) for tf, b in bars.items()},
                items=len(result.items),
                last=result.reports[-1]["as_of"] if result.reports else None,
            )

    async def _new_bars(self, agent: SymbolTrendAgent, tf: str, close_time: int) -> list[Bar]:
        engine = agent.engines[tf]
        last = engine.last_open_time
        bars = [
            b
            for b in await redis_bars(self.redis, agent.symbol, tf, 50)
            if b.close_time <= close_time
        ]
        new = [b for b in bars if last is None or b.open_time > last]
        iv = TIMEFRAME_MS[tf]
        if last is not None and new and new[0].open_time != last + iv:
            # gap larger than the buffer window: reload from storage
            new = await load_bars(
                self.settings.data_dir,
                self.recent,
                agent.symbol,
                tf,
                start_ms=last + iv,
                end_ms=close_time,
            )
        return new

    async def on_bar_close(self, event: dict[str, Any]) -> dict[str, Any] | None:
        agent = self.agents.get(event["symbol"])
        if agent is None:
            return None
        close_time = int(event["close_time"])
        bars = {
            tf: await self._new_bars(agent, tf, close_time)
            for tf in event["closed_timeframes"]
            if tf in agent.engines
        }
        out = agent.on_bar_close(close_time, bars)
        if out.changes:
            await self.store.upsert_items(agent.symbol, out.changes, self.cfg.version)
        await self.store.upsert_reports(agent.symbol, [out.report])
        await self.redis.publish(
            AGENT_REPORT_CHANNEL, json.dumps(out.report, separators=(",", ":"))
        )
        self.reports += 1
        await self.redis.hset(
            HEALTH_KEY,
            mapping={
                "heartbeat": B.now_ms(),
                "last_report": out.report["as_of"],
                "reports": self.reports,
            },
        )
        log.info(
            "trend_report",
            symbol=agent.symbol,
            as_of=out.report["as_of"],
            updated=[tf for tf in bars if bars[tf]],
            new_items=len(out.changes),
            trends={tf: s["trend"] for tf, s in out.report["timeframes"].items()},
        )
        return out.report

    async def run(self, stop: asyncio.Event) -> None:
        await self.warm_up(B.now_ms())
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(B.BAR_CLOSE_CHANNEL)
        log.info("trend_agent_listening", channel=B.BAR_CLOSE_CHANNEL)
        try:
            while not stop.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        await self.on_bar_close(json.loads(msg["data"]))
                    except Exception as exc:  # keep running; the next event catches up
                        log.error("trend_event_failed", error=str(exc), event=msg["data"])
                await self.redis.hset(HEALTH_KEY, mapping={"heartbeat": B.now_ms()})
        finally:
            await pubsub.unsubscribe(B.BAR_CLOSE_CHANNEL)
            await pubsub.aclose()


async def run_live(settings: Settings, duration_s: float | None = None) -> None:
    from deskmind.agents.trend.store import PostgresTrendStore
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
    runner = LiveTrendRunner(
        redis, PostgresTrendStore(engine), PostgresRecentCandles(engine), settings
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
