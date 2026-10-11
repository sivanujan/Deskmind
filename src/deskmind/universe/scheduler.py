"""Daily scheduling for the universe job: `deskmind universe run` (a supervised service).

The job runs at `run_hour_utc`:00 UTC every day. One run per UTC date: a completed run is never
repeated (restarts are safe). If the service restarts while a run was in progress (status
`running`), that run is resumed at once instead of waiting for the next day.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime, timedelta

from deskmind.config import Settings
from deskmind.logging import get_logger
from deskmind.universe.job import UniverseJob
from deskmind.universe.store import RUN_RUNNING

log = get_logger(__name__)


def next_run_at(now: datetime, hour_utc: int) -> datetime:
    """The next hour_utc:00:00 UTC strictly after `now`."""
    now = now.astimezone(UTC)
    at = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


async def run_scheduler(job: UniverseJob, stop: asyncio.Event) -> None:
    today = datetime.now(UTC).date()
    if await job.store.run_status(today) == RUN_RUNNING:
        log.info("universe_resume_interrupted_run", run_date=today.isoformat())
        await job.run()
    while not stop.is_set():
        at = next_run_at(datetime.now(UTC), job.cfg.run_hour_utc)
        log.info("universe_next_run", at=at.isoformat())
        delay = (at - datetime.now(UTC)).total_seconds()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(delay, 0))
        if stop.is_set():
            break
        try:
            await job.run()
        except Exception:  # the job records its own failures; keep the schedule alive
            log.exception("universe_scheduler_run_failed")


async def build_and_run(settings: Settings, *, once: bool = False, force: bool = False) -> dict:
    """Wire the real dependencies. once=True runs the job now (manual) instead of scheduling."""
    from deskmind.data.pipeline import make_clients
    from deskmind.db.session import create_engine
    from deskmind.universe.config import UniverseConfig
    from deskmind.universe.data_ops import ParquetDataOps, trend_warm_up
    from deskmind.universe.ranking import fetch_market_snapshot
    from deskmind.universe.store import PostgresUniverseStore

    engine = create_engine(settings)
    fapi, vision = make_clients(settings.binance_market_data_url)
    try:
        async with fapi, vision:
            job = UniverseJob(
                UniverseConfig.from_settings(settings),
                PostgresUniverseStore(engine),
                lambda: fetch_market_snapshot(fapi),
                ParquetDataOps(settings, fapi, vision),
                trend_warm_up(settings, engine),
            )
            if once:
                return await job.run(force=force)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, stop.set)
            await run_scheduler(job, stop)
            return {}
    finally:
        await engine.dispose()
