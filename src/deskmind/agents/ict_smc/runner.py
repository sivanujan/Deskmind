"""Live runner and history backfill for the ICT/SMC agent (both drive SymbolIctSmcAgent).

Live: warm up each symbol by replaying the last `warmup_bars` candles per timeframe through
`run_batch`, then react to `deskmind:bar_close` events: feed newly closed candles from Redis,
store items + transitions + report (reports kept ICT_SMC_REPORT_RETENTION_DAYS), publish
`deskmind:agent_report`.

Backfill: items + transitions only (the point-in-time record); full reports only for an optional
short debug window at the end.
"""

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
from deskmind.agents.ict_smc.agent import SymbolIctSmcAgent, check_smt_divergence, run_batch
from deskmind.agents.ict_smc.config import AGENT_NAME, DEFAULT_CONFIG, IctSmcConfig
from deskmind.agents.ict_smc.history import TransitionRecorder
from deskmind.agents.ict_smc.store import IctSmcStore
from deskmind.agents.trend.config import DEFAULT_CONFIG as DEFAULT_TREND_CONFIG
from deskmind.agents.trend.config import TrendConfig
from deskmind.agents.trend.models import Bar
from deskmind.api.candles import RecentCandleSource
from deskmind.config import Settings
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.live import buffers as B
from deskmind.logging import get_logger

log = get_logger(__name__)

AGENT_REPORT_CHANNEL = "deskmind:agent_report"
HEALTH_KEY = f"{B.PREFIX}:agents:{AGENT_NAME}:health"
ERRORS_KEY = f"{B.PREFIX}:agents:{AGENT_NAME}:errors"


async def record_error(redis: Redis, error: str) -> None:
    """Keep a timestamped error log so the dashboard can count real errors."""
    now = B.now_ms()
    pipe = redis.pipeline(transaction=False)
    pipe.zadd(ERRORS_KEY, {f"{now}:{error[:300]}": now})
    pipe.zremrangebyscore(ERRORS_KEY, 0, now - 7 * 86_400_000)
    pipe.hset(HEALTH_KEY, mapping={"last_error": error[:300], "last_error_at": now})
    await pipe.execute()


# ---------------------------------------------------------------- batch / backfill


async def backfill(
    symbol: str,
    timeframes: list[str],
    start_ms: int,
    end_ms: int,
    *,
    data_dir: Any,
    recent: RecentCandleSource | None,
    store: IctSmcStore,
    cfg: IctSmcConfig = DEFAULT_CONFIG,
    trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
    debug_report_days: float = 0.0,
) -> dict[str, Any]:
    """Replay [start - warm-up, end]; store items + transitions for [start, end].

    debug_report_days > 0 also stores full reports for the last N days of the window."""
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    bars: dict[str, list[Bar]] = {}
    for tf in timeframes:
        warm = start_ms - cfg.warmup_bars * TIMEFRAME_MS[tf]
        bars[tf] = await load_bars(data_dir, recent, symbol, tf, start_ms=warm, end_ms=end_ms)
    timings["load_s"] = time.perf_counter() - t0
    agent = SymbolIctSmcAgent(symbol, timeframes, cfg, trend_cfg)
    t0 = time.perf_counter()
    report_from = end_ms - int(debug_report_days * 86_400_000) if debug_report_days else None
    result = await asyncio.to_thread(
        run_batch,
        agent,
        bars,
        report_from=report_from,
        store_from=start_ms,
        reports=bool(debug_report_days),
        recorder=TransitionRecorder(),
    )
    timings["replay_s"] = time.perf_counter() - t0
    rows = result.recorder.rows if result.recorder else []
    items = [(tf, item) for (tf, _), item in result.items.items()]
    t0 = time.perf_counter()
    await store.upsert_items(symbol, items, cfg.version)
    timings["write_items_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    await store.replace_transitions(symbol, cfg.version, start_ms, rows)
    timings["write_transitions_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    if result.reports:
        await store.upsert_reports(symbol, result.reports)
    timings["write_reports_s"] = time.perf_counter() - t0
    counts: dict[str, dict[str, int]] = {}
    for tf, item in items:
        counts.setdefault(tf, {}).setdefault(item["kind"], 0)
        counts[tf][item["kind"]] += 1
    return {
        "symbol": symbol,
        "candles": {tf: len(b) for tf, b in bars.items()},
        "reports": len(result.reports),
        "items": len(items),
        "transitions": len(rows),
        "counts": counts,
        "timings": {k: round(v, 1) for k, v in timings.items()},
    }


# ---------------------------------------------------------------- live


class LiveIctSmcRunner:
    def __init__(
        self,
        redis: Redis,
        store: IctSmcStore,
        recent: RecentCandleSource | None,
        settings: Settings,
        cfg: IctSmcConfig = DEFAULT_CONFIG,
        trend_cfg: TrendConfig = DEFAULT_TREND_CONFIG,
        symbol_source: Callable[[], Awaitable[list[str]]] | None = None,
        refresh_s: float = 60.0,
    ) -> None:
        self.redis = redis
        self.symbol_source = symbol_source
        self.refresh_s = refresh_s
        self.store = store
        self.recent = recent
        self.settings = settings
        self.cfg = cfg
        self.trend_cfg = trend_cfg
        self.timeframes = list(settings.live_timeframes)
        self.agents: dict[str, SymbolIctSmcAgent] = {}
        self.recorders: dict[str, TransitionRecorder] = {}
        self.reports = 0
        self._last_prune = 0.0

    async def _symbols(self) -> list[str]:
        if self.symbol_source is None:
            return list(self.settings.data_symbols)
        return await self.symbol_source()

    async def sync_symbols(self, desired: list[str], now_ms: int) -> dict[str, list[str]]:
        added = [s for s in desired if s not in self.agents]
        removed = [s for s in self.agents if s not in desired]
        for symbol in removed:
            del self.agents[symbol]
            self.recorders.pop(symbol, None)
        for symbol in added:
            try:
                await self.warm_symbol(symbol, now_ms)
            except Exception as exc:
                log.error("ict_smc_warm_failed", symbol=symbol, error=str(exc))
                await record_error(self.redis, f"{symbol}: warm-up failed: {exc}")
        if added or removed:
            log.info("ict_smc_symbols_changed", added=added, removed=removed)
        return {"added": added, "removed": removed}

    async def warm_up(self, now_ms: int) -> None:
        await self.sync_symbols(await self._symbols(), now_ms)

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
        agent = SymbolIctSmcAgent(symbol, self.timeframes, self.cfg, self.trend_cfg)
        base = min(TIMEFRAME_MS[tf] for tf in self.timeframes)
        store_from = now_ms - (self.cfg.warmup_bars // 2) * base
        recorder = TransitionRecorder()
        result = await asyncio.to_thread(
            run_batch, agent, bars, store_from=store_from, reports=False, recorder=recorder
        )
        await self.store.upsert_items(
            symbol, [(tf, it) for (tf, _), it in result.items.items()], self.cfg.version
        )
        # rows from store_from on are rewritten: a fresh baseline supersedes older sessions
        await self.store.replace_transitions(symbol, self.cfg.version, store_from, recorder.rows)
        recorder.rows = []
        last_t = max((b.close_time for bs in bars.values() for b in bs), default=None)
        if last_t is not None:
            await self.store.upsert_reports(symbol, [agent.report(last_t)])
        self.agents[symbol] = agent
        self.recorders[symbol] = recorder
        log.info(
            "ict_smc_warm",
            symbol=symbol,
            candles={tf: len(b) for tf, b in bars.items()},
            items=len(result.items),
            transitions_from=store_from,
        )

    async def _new_bars(self, agent: SymbolIctSmcAgent, tf: str, close_time: int) -> list[Bar]:
        engine = agent.engines[tf]
        last = engine.bars[-1].open_time if engine.bars else None
        bars = [
            b
            for b in await redis_bars(self.redis, agent.symbol, tf, 50)
            if b.close_time <= close_time
        ]
        new = [b for b in bars if last is None or b.open_time > last]
        iv = TIMEFRAME_MS[tf]
        if last is not None and new and new[0].open_time != last + iv:
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

        # Check SMT divergence across configured pairs
        for pair in self.cfg.smt_pairs:
            sym_a, sym_b = pair
            if agent.symbol in (sym_a, sym_b):
                other_sym = sym_b if agent.symbol == sym_a else sym_a
                other_agent = self.agents.get(other_sym)
                if other_agent is not None:
                    for tf in event["closed_timeframes"]:
                        check_smt_divergence(agent, other_agent, tf, close_time)

        if out.changes:
            await self.store.upsert_items(agent.symbol, out.changes, self.cfg.version)
        recorder = self.recorders.get(agent.symbol)
        if recorder is not None:  # after the SMT check, so SMT changes are recorded too
            recorder.boundary(close_time, agent, out.changes)
            await self.store.append_transitions(agent.symbol, self.cfg.version, recorder.rows)
            recorder.rows = []
        assert out.report is not None
        await self.store.upsert_reports(agent.symbol, [out.report])
        await self._prune_reports()
        await self.redis.publish(
            AGENT_REPORT_CHANNEL, json.dumps(out.report, separators=(",", ":"))
        )
        self.reports += 1
        await self.redis.hset(
            HEALTH_KEY,
            mapping={
                "state": "running",
                "heartbeat": B.now_ms(),
                "last_report": out.report["as_of"],
                "reports": self.reports,
            },
        )
        log.info(
            "ict_smc_report",
            symbol=agent.symbol,
            as_of=out.report["as_of"],
            updated=[tf for tf in bars if bars[tf]],
            new_items=len(out.changes),
        )
        return out.report

    async def _prune_reports(self) -> None:
        """Keep full live reports for the retention window only (checked hourly)."""
        if time.monotonic() - self._last_prune < 3600:
            return
        self._last_prune = time.monotonic()
        cutoff = B.now_ms() - int(self.settings.ict_smc_report_retention_days * 86_400_000)
        for symbol in list(self.agents):
            try:
                n = await self.store.prune_reports(symbol, cutoff)
                if n:
                    log.info("ict_smc_reports_pruned", symbol=symbol, deleted=n)
            except Exception as exc:
                log.warning("ict_smc_prune_failed", symbol=symbol, error=str(exc))

    async def run(self, stop: asyncio.Event) -> None:
        await self.warm_up(B.now_ms())
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(B.BAR_CLOSE_CHANNEL)
        log.info("ict_smc_agent_listening", channel=B.BAR_CLOSE_CHANNEL)
        last_refresh = time.monotonic()
        try:
            while not stop.is_set():
                if self.symbol_source is not None and time.monotonic() - last_refresh >= (
                    self.refresh_s
                ):
                    last_refresh = time.monotonic()
                    try:
                        await self.sync_symbols(await self.symbol_source(), B.now_ms())
                    except Exception as exc:
                        log.warning("ict_smc_symbol_source_failed", error=str(exc))
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        await self.on_bar_close(json.loads(msg["data"]))
                    except Exception as exc:
                        log.error("ict_smc_event_failed", error=str(exc), event=msg["data"])
                        await record_error(self.redis, str(exc))
                await self.redis.hset(
                    HEALTH_KEY, mapping={"state": "running", "heartbeat": B.now_ms()}
                )
        finally:
            await pubsub.unsubscribe(B.BAR_CLOSE_CHANNEL)
            await pubsub.aclose()


async def run_live(settings: Settings, duration_s: float | None = None) -> None:
    from deskmind.agents.ict_smc.store import PostgresIctSmcStore
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
    from deskmind.universe.store import PostgresUniverseStore
    from deskmind.universe.symbols import active_symbols

    ustore = PostgresUniverseStore()

    async def symbol_source() -> list[str]:
        return await active_symbols(list(settings.data_symbols), ustore)

    runner = LiveIctSmcRunner(
        redis,
        PostgresIctSmcStore(engine),
        PostgresRecentCandles(engine),
        settings,
        symbol_source=symbol_source,
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
