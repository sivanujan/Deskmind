"""Daily universe job (00:01 UTC, just after the daily candle closes; runs under the supervisor).

1. Recompute the liquidity universe (ranking.py).
2. Exited symbols -> `exited`: no new entries, data kept (never deleted).
3. Entered symbols (and members whose earlier bring-up did not finish) -> bring-up:
   download history if missing (needed period + warm-up; else incremental update)
   -> verify (the existing `data verify` checks) -> warm up the analyst agents -> `tradeable`.
   Listed < min_listing_age_days -> `watch_only` after verification (data + chart, never traded).
   Verification failure -> `blocked` + alert; retried by the next run.
4. Existing members -> incremental update only; `watch_only` members that reach the listing age
   are verified + warmed up and promoted to `tradeable`.
5. Daily summary (entered, exited, new watch_only listings, downloads done/failed, disk usage,
   alerts) stored in universe_runs and shown on the dashboard (Telegram later).

Safety: at most `download_concurrency` symbols are downloaded at once (each download also goes
through the weight-limited Binance client); every symbol runs in its own error boundary so one
failure never blocks the rest; a disk guard stops all downloads above `disk_max_used_pct`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from deskmind.logging import get_logger
from deskmind.universe.history import MembershipRow, ms_day
from deskmind.universe.models import STATES, SymbolRecord, SymbolStat
from deskmind.universe.ranking import DAY_MS, UniverseConfig, rank_universe
from deskmind.universe.store import UniverseStore

log = get_logger(__name__)
HISTORY_KEEP = 20


class DataOps(Protocol):
    """Side effects of the job; the real implementation is LiveDataOps, tests use fakes."""

    async def fetch_stats(self, include: set[str], now_ms: int) -> list[SymbolStat]: ...
    def has_data(self, symbol: str) -> bool: ...
    async def download(self, symbol: str, start: date) -> None: ...
    async def update(self, symbol: str) -> None: ...
    async def verify(self, symbol: str) -> tuple[bool, str]: ...
    async def warm_up(self, symbol: str, start_ms: int, end_ms: int) -> None: ...
    def disk_usage(self) -> dict[str, Any]: ...


class DailyJob:
    def __init__(
        self,
        store: UniverseStore,
        ops: DataOps,
        cfg: UniverseConfig,
        now_ms: Any = None,
    ) -> None:
        self.store = store
        self.ops = ops
        self.cfg = cfg
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._sem = asyncio.Semaphore(cfg.download_concurrency)
        self._disk_guard_tripped = False
        self.summary: dict[str, Any] = {}

    # ------------------------------------------------------------ helpers

    async def _set(self, rec: SymbolRecord, state: str, reason: str | None = None) -> None:
        assert state in STATES, state
        if rec.state != state or rec.reason != reason:
            change = {
                "at_ms": self._now(),
                "from": rec.state or None,
                "to": state,
                "reason": reason,
            }
            rec.history = [*rec.history, change][-HISTORY_KEEP:]
            rec.state_changed_at_ms = self._now()
        rec.state = state
        rec.reason = reason
        await self.store.save(rec)

    def _alert(self, msg: str) -> None:
        log.warning("universe_alert", alert=msg)
        self.summary["alerts"].append(msg)

    def _age_days(self, rec: SymbolRecord) -> float:
        if not rec.onboard_ms:
            return float("inf")  # unknown listing time: not treated as a new listing
        return (self._now() - rec.onboard_ms) / DAY_MS

    def _disk_ok(self) -> bool:
        if self._disk_guard_tripped:
            return False
        usage = self.ops.disk_usage()
        if usage["used_pct"] > self.cfg.disk_max_used_pct:
            self._disk_guard_tripped = True
            self._alert(
                f"disk guard: {usage['used_pct']:.1f}% used > {self.cfg.disk_max_used_pct}% "
                "limit; downloads stopped for this run"
            )
            return False
        return True

    # ------------------------------------------------------------ steps

    async def _bring_up(self, rec: SymbolRecord, entered: bool) -> None:
        """Entered (or unfinished) symbol: data -> verify -> warm up -> tradeable/watch_only."""
        sym = rec.symbol
        async with self._sem:
            if not self._disk_ok():
                await self._set(rec, "blocked", "disk guard: download postponed")
                return
            try:
                if not self.ops.has_data(sym):
                    await self._set(rec, "downloading")
                    await self.ops.download(sym, self._download_start(rec))
                    self.summary["downloads_done"].append(sym)
                else:
                    await self.ops.update(sym)
            except Exception as exc:  # one symbol never blocks the others
                self.summary["downloads_failed"].append({"symbol": sym, "error": str(exc)[:300]})
                self._alert(f"{sym}: download failed: {str(exc)[:200]}")
                await self._set(rec, "blocked", f"download failed: {str(exc)[:200]}")
                return
        await self._verify_and_warm(rec, entered)

    async def _verify_and_warm(self, rec: SymbolRecord, entered: bool) -> None:
        sym = rec.symbol
        await self._set(rec, "verifying")
        try:
            passed, detail = await self.ops.verify(sym)
        except Exception as exc:
            passed, detail = False, f"verify error: {exc}"
        if not passed:
            rec.data_verified = False
            rec.warmed_up = False
            self.summary["verify_failed"].append({"symbol": sym, "detail": detail[:300]})
            self._alert(f"{sym}: data verification failed, symbol blocked: {detail[:200]}")
            await self._set(rec, "blocked", f"verification failed: {detail[:200]}")
            return
        rec.data_verified = True
        if self._age_days(rec) < self.cfg.min_listing_age_days:
            if entered:
                self.summary["new_watch_only"].append(sym)
            await self._set(
                rec,
                "watch_only",
                f"listed {self._age_days(rec):.0f} days ago "
                f"(< {self.cfg.min_listing_age_days}): chart only, no trading",
            )
            return
        await self._set(rec, "warming_up")
        now = self._now()
        try:
            await self.ops.warm_up(sym, now - self.cfg.warmup_days * DAY_MS, now)
        except Exception as exc:
            rec.warmed_up = False
            self._alert(f"{sym}: agent warm-up failed: {str(exc)[:200]}")
            await self._set(rec, "blocked", f"agent warm-up failed: {str(exc)[:200]}")
            return
        rec.warmed_up = True
        await self._set(rec, "tradeable")

    async def _update_member(self, rec: SymbolRecord) -> None:
        """Existing member: incremental update only (plus watch_only promotion when due)."""
        sym = rec.symbol
        async with self._sem:
            if not self._disk_ok():
                self.summary["update_failed"].append({"symbol": sym, "error": "disk guard"})
                return
            try:
                await self.ops.update(sym)
                self.summary["updated"].append(sym)
            except Exception as exc:
                self.summary["update_failed"].append({"symbol": sym, "error": str(exc)[:300]})
                self._alert(f"{sym}: incremental update failed: {str(exc)[:200]}")
                return
        if rec.state == "watch_only" and self._age_days(rec) >= self.cfg.min_listing_age_days:
            await self._verify_and_warm(rec, entered=False)
            if rec.state == "tradeable":
                self.summary["promoted"].append(sym)

    def _download_start(self, rec: SymbolRecord) -> date:
        now = datetime.fromtimestamp(self._now() / 1000, UTC)
        start = (now - timedelta(days=self.cfg.history_days + self.cfg.warmup_days)).date()
        if rec.onboard_ms:
            start = max(start, datetime.fromtimestamp(rec.onboard_ms / 1000, UTC).date())
        return start

    # ------------------------------------------------------------ run

    async def run(self) -> dict[str, Any]:
        started = datetime.now(UTC)
        now = self._now()
        self.summary = {
            "run_date": datetime.fromtimestamp(now / 1000, UTC).date().isoformat(),
            "entered": [],
            "exited": [],
            "new_watch_only": [],
            "downloads_done": [],
            "downloads_failed": [],
            "verify_failed": [],
            "updated": [],
            "update_failed": [],
            "promoted": [],
            "alerts": [],
        }
        records = await self.store.load()
        prev = {s for s, r in records.items() if r.member}
        try:
            stats = await self.ops.fetch_stats(prev, now)
        except Exception as exc:  # no ranking today: keep every state as it is
            self._alert(f"universe ranking failed, nothing changed: {str(exc)[:200]}")
            self.summary.update(members=len(prev), disk=self.ops.disk_usage())
            await self.store.save_run(started, datetime.now(UTC), self.summary)
            return self.summary
        result = rank_universe(stats, prev, self.cfg)
        by_sym = {s.symbol: s for s in stats}
        self.summary["entered"] = list(result.entered)
        self.summary["exited"] = list(result.exited)

        # Point-in-time history: today's membership, decided from candles closed before today.
        today = ms_day(now)
        await self.store.save_membership(
            [
                MembershipRow(
                    today,
                    sym,
                    result.ranks[sym],
                    by_sym[sym].median_quote_volume,
                    (now - by_sym[sym].onboard_ms) / DAY_MS >= self.cfg.min_listing_age_days
                    if by_sym[sym].onboard_ms
                    else True,
                )
                for sym in result.members
            ],
            source="live",
        )
        # A previous member that is no longer listed as TRADING has been delisted.
        delisted = {s: now for s in result.exited if s not in by_sym}
        if delisted:
            await self.store.save_delistings(delisted, source="live")
            self.summary["delisted"] = sorted(delisted)

        for sym in result.exited:  # data is kept; the symbol only stops taking new entries
            rec = records[sym]
            rec.member = False
            rec.exited_at_ms = now
            rec.rank = result.ranks.get(sym)
            await self._set(
                rec,
                "exited",
                "left the universe" + (f" (rank {rec.rank})" if rec.rank else " (not listed)"),
            )

        tasks = []
        for sym in result.members:
            st = by_sym[sym]
            rec = records.get(sym) or SymbolRecord(symbol=sym, state="")  # new: first _set names it
            rec.rank = result.ranks[sym]
            rec.median_quote_volume = st.median_quote_volume
            rec.onboard_ms = st.onboard_ms or rec.onboard_ms
            if sym in result.entered:
                rec.member = True
                rec.entered_at_ms = now
                rec.exited_at_ms = None
                rec.data_verified = False
                rec.warmed_up = False
                tasks.append(self._bring_up(rec, entered=True))
            elif rec.state in ("tradeable", "watch_only"):
                tasks.append(self._update_member(rec))
            else:  # blocked / interrupted bring-up: retry it
                tasks.append(self._bring_up(rec, entered=False))
            records[sym] = rec
        await asyncio.gather(*tasks)

        final = await self.store.load()
        by_state: dict[str, int] = {}
        for r in final.values():
            by_state[r.state] = by_state.get(r.state, 0) + 1
        self.summary.update(
            members=len(result.members),
            tradeable=sorted(s for s, r in final.items() if r.state == "tradeable"),
            by_state=by_state,
            disk=self.ops.disk_usage(),
            config={
                "top_n": self.cfg.top_n,
                "exit_rank": self.cfg.exit_rank,
                "lookback_days": self.cfg.lookback_days,
                "min_listing_age_days": self.cfg.min_listing_age_days,
                "disk_max_used_pct": self.cfg.disk_max_used_pct,
            },
        )
        await self.store.save_run(started, datetime.now(UTC), self.summary)
        log.info(
            "universe_daily_done",
            entered=len(self.summary["entered"]),
            exited=len(self.summary["exited"]),
            alerts=len(self.summary["alerts"]),
        )
        return self.summary


# ---------------------------------------------------------------- trading gate


async def can_open_new_entry(
    symbol: str, store: UniverseStore, trade_symbols: list[str]
) -> tuple[bool, str]:
    """New entries only for symbols in TRADE_SYMBOLS whose universe state is `tradeable`
    (data verified AND agents warmed up). Exits/stops of open positions are never gated."""
    if symbol not in trade_symbols:
        return False, f"{symbol} is not in TRADE_SYMBOLS"
    rec = (await store.load()).get(symbol)
    if rec is None:
        return False, f"{symbol} has no universe state yet (run `deskmind universe run`)"
    if rec.state != "tradeable":
        return False, f"{symbol} universe state is {rec.state}: {rec.reason or 'not tradeable'}"
    return True, "tradeable"


# ---------------------------------------------------------------- scheduler


def seconds_until_next_run(now: datetime, minute_after_midnight: int = 1) -> float:
    """Seconds to the next 00:01 UTC (the daily candle is closed by then)."""
    target = now.replace(hour=0, minute=minute_after_midnight, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_scheduler(make_job: Any, stop: asyncio.Event) -> None:
    """Run the daily job every day at 00:01 UTC until `stop` is set."""
    while not stop.is_set():
        wait = seconds_until_next_run(datetime.now(UTC))
        log.info("universe_next_run", in_s=round(wait))
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
            return
        except TimeoutError:
            pass
        try:
            await make_job().run()
        except Exception as exc:  # keep the scheduler alive; the alert is in the logs
            log.error("universe_daily_failed", error=str(exc))
