"""The daily universe job (00:00 UTC, run by `deskmind universe run` under the supervisor).

1. Recompute the universe (liquidity only, see ranking.py).
2. Symbols that LEFT: state `exited`, no new entries, data kept.
3. Symbols that ENTERED (or were blocked / interrupted before): if their history is missing,
   download it for the needed period + agent warm-up, otherwise update it; verify it with the
   same checks as `deskmind data verify`; warm up the agents; only then `tradeable`.
   Listings younger than `min_listing_age_days` are `watch_only`: data is collected, no trading,
   and they go through the same pipeline on the first run after they are old enough.
4. Existing tradeable members: incremental update only.
5. A daily summary is stored with the run (dashboard now, Telegram later).

Safety: downloads run with bounded concurrency on the shared rate-limited clients; every symbol is
isolated (one failure never blocks the rest); a disk guard stops all downloading above the
configured usage and raises an alert. Nothing here places, modifies or cancels orders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from deskmind.logging import get_logger
from deskmind.universe.config import UniverseConfig
from deskmind.universe.lifecycle import (
    PIPELINE_STATES,
    BlockedReason,
    SymbolRecord,
    SymbolState,
    is_tradeable,
)
from deskmind.universe.ranking import Candidate, MarketSnapshot, select_universe
from deskmind.universe.store import RUN_COMPLETED, RUN_FAILED, UniverseStore

log = get_logger(__name__)

Action = Literal["collect", "pipeline", "update"]


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    data_dir_bytes: int

    @property
    def used_pct(self) -> Decimal:
        if self.total_bytes <= 0:
            return Decimal(0)
        return (Decimal(self.used_bytes) * 100 / Decimal(self.total_bytes)).quantize(
            Decimal("0.01")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "data_dir_bytes": self.data_dir_bytes,
            "used_pct": str(self.used_pct),
        }


@dataclass(frozen=True)
class VerifyOutcome:
    status: str  # PASS | WARN | FAIL (as `deskmind data verify`)
    failures: list[str] = field(default_factory=list)
    report: str | None = None


class DataOps(Protocol):
    """Historical data operations the job needs (real implementation: data_ops.ParquetDataOps)."""

    def has_history(self, symbol: str, start: date) -> bool: ...

    async def download(self, symbol: str, start: date) -> None: ...

    async def update(self, symbol: str) -> None: ...

    async def verify(self, symbol: str) -> VerifyOutcome: ...

    def disk_usage(self) -> DiskUsage: ...


MarketSource = Callable[[], Awaitable[MarketSnapshot]]
WarmUp = Callable[[str], Awaitable[int]]  # symbol -> number of agent reports produced
Notifier = Callable[[dict[str, Any]], Awaitable[None]]


class DiskGuard:
    """Checked before every download. Once tripped it stays tripped for the rest of the run."""

    def __init__(self, ops: DataOps, max_used_pct: Decimal, alert: Callable[..., None]) -> None:
        self.ops = ops
        self.max_used_pct = max_used_pct
        self.alert = alert
        self.tripped = False
        self.last: DiskUsage | None = None

    def allows_download(self) -> bool:
        if self.tripped:
            return False
        self.last = self.ops.disk_usage()
        if self.last.used_pct >= self.max_used_pct:
            self.tripped = True
            self.alert(
                "disk_guard",
                None,
                f"disk usage {self.last.used_pct}% >= {self.max_used_pct}%: downloads stopped",
            )
        return not self.tripped


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


class UniverseJob:
    def __init__(
        self,
        cfg: UniverseConfig,
        store: UniverseStore,
        market: MarketSource,
        ops: DataOps,
        warm_up: WarmUp,
        *,
        notify: Notifier | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.market = market
        self.ops = ops
        self.warm_up = warm_up
        self.notify = notify

    # ------------------------------------------------------------------ helpers

    def needed_start(self, cand: Candidate, now: datetime) -> date:
        """History start for a symbol: needed period + agent warm-up, never before listing."""
        start = now.date() - timedelta(days=self.cfg.history_days + self.cfg.warmup_days)
        if cand.onboard_at is not None:
            # First full UTC day after listing: the listing day itself starts mid-day.
            start = max(start, cand.onboard_at.date() + timedelta(days=1))
        return start

    def is_young(self, cand: Candidate, now: datetime) -> bool:
        age = cand.listing_age_days(now)
        return age is None or age < self.cfg.min_listing_age_days

    # ------------------------------------------------------------------ run

    async def run(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        run_date = now.date()
        if not force and await self.store.run_status(run_date) == RUN_COMPLETED:
            log.info("universe_run_skipped", run_date=run_date.isoformat(), reason="completed")
            return {"run_date": run_date.isoformat(), "skipped": True}
        await self.store.start_run(run_date, now)
        summary = _Summary(run_date, now, self.cfg)
        try:
            await self._run(now, summary)
            status = RUN_COMPLETED
        except Exception as exc:
            log.exception("universe_run_failed", run_date=run_date.isoformat())
            summary.alert("run_failed", None, _err(exc))
            status = RUN_FAILED
        out = summary.finish(datetime.now(UTC), status)
        await self.store.finish_run(run_date, status, datetime.now(UTC), out)
        log.info("universe_run_finished", **{k: out[k] for k in ("run_date", "status")})
        if self.notify is not None:
            try:
                await self.notify(out)
            except Exception:
                log.exception("universe_notify_failed")
        return out

    async def _run(self, now: datetime, summary: _Summary) -> None:
        summary.disk_start = self.ops.disk_usage()
        snapshot = await self.market()
        records = await self.store.load_all()
        members = {s for s, r in records.items() if r.in_universe}
        selection = select_universe(snapshot, members, self.cfg)
        guard = DiskGuard(self.ops, self.cfg.disk_max_used_pct, summary.alert)

        # Exits: no new entries from now on; stored data is kept.
        for sym in sorted(members - set(selection.members)):
            rec = records[sym]
            ranked = selection.ranked.get(sym)
            rec = rec.transition(
                SymbolState.exited,
                now,
                in_universe=False,
                exited_at=now,
                rank=ranked.rank if ranked else None,
                quote_volume_24h=ranked.quote_volume_24h if ranked else None,
            )
            await self.store.save(rec)
            records[sym] = rec
            summary.exited.append(sym)

        # Entries and existing members: decide what each one needs.
        plan: list[tuple[SymbolRecord, Candidate, Action]] = []
        for sym, cand in sorted(selection.members.items(), key=lambda kv: kv[1].rank):
            rec = records.get(sym)
            young = self.is_young(cand, now)
            common = {
                "in_universe": True,
                "rank": cand.rank,
                "quote_volume_24h": cand.quote_volume_24h,
                "onboard_at": cand.onboard_at,
            }
            action: Action
            if rec is None or not rec.in_universe:
                base = rec or SymbolRecord(sym, SymbolState.watch_only)
                state = SymbolState.watch_only if young else SymbolState.downloading
                rec = base.transition(
                    state, now, entered_at=now, exited_at=None, last_error=None, **common
                )
                action = "collect" if young else "pipeline"
                summary.entered.append(
                    {
                        "symbol": sym,
                        "rank": cand.rank,
                        "quote_volume_24h": str(cand.quote_volume_24h),
                        "state": str(state),
                    }
                )
                if young:
                    summary.new_watch_only.append(sym)
            elif young:
                rec = rec.transition(SymbolState.watch_only, now, **common)
                action = "collect"
            elif rec.state == SymbolState.tradeable and is_tradeable(rec):
                rec = rec.transition(SymbolState.tradeable, now, **common)
                action = "update"
            else:
                if rec.state == SymbolState.watch_only:
                    summary.promoted.append(sym)
                elif rec.state in PIPELINE_STATES:
                    summary.retried.append(sym)
                rec = rec.transition(SymbolState.downloading, now, **common)
                action = "pipeline"
            await self.store.save(rec)
            plan.append((rec, cand, action))

        sem = asyncio.Semaphore(self.cfg.download_concurrency)

        async def one(rec: SymbolRecord, cand: Candidate, action: Action) -> None:
            async with sem:
                try:
                    await self._process(rec, cand, action, now, guard, summary)
                except Exception as exc:  # isolation: never let one symbol stop the others
                    log.exception("universe_symbol_failed", symbol=rec.symbol)
                    summary.alert("symbol_failed", rec.symbol, _err(exc))

        await asyncio.gather(*(one(r, c, a) for r, c, a in plan))

        final = await self.store.load_all()
        summary.universe_size = sum(1 for r in final.values() if r.in_universe)
        summary.tradeable = sorted(s for s, r in final.items() if is_tradeable(r))
        summary.blocked = [
            {"symbol": s, "reason": str(r.blocked_reason), "error": r.last_error}
            for s, r in sorted(final.items())
            if r.in_universe and r.state == SymbolState.blocked
        ]
        summary.disk_guard_tripped = guard.tripped
        summary.disk_end = self.ops.disk_usage()

    # ------------------------------------------------------------------ per symbol

    async def _process(
        self,
        rec: SymbolRecord,
        cand: Candidate,
        action: Action,
        now: datetime,
        guard: DiskGuard,
        summary: _Summary,
    ) -> None:
        sym = rec.symbol
        if action in ("collect", "update"):
            if not guard.allows_download():
                summary.skipped_disk_guard.append(sym)
                return
            kind = await self._fetch(rec, cand, now, summary, alert_kind="download_failed")
            if kind is None:
                await self.store.save(rec.transition(rec.state, now, last_error=summary.last(sym)))
            return

        # Full entry pipeline. Verification and warm-up timestamps are reset first, so the symbol
        # can never be tradeable on a previous run's checks.
        rec = rec.transition(SymbolState.downloading, now, verified_at=None, warmed_up_at=None)
        await self.store.save(rec)
        if not guard.allows_download():
            summary.skipped_disk_guard.append(sym)
            await self._block(rec, now, BlockedReason.disk_guard, "disk guard: download skipped")
            return
        if await self._fetch(rec, cand, now, summary, alert_kind="download_failed") is None:
            await self._block(rec, now, BlockedReason.download_failed, summary.last(sym))
            return

        rec = rec.transition(SymbolState.verifying, now)
        await self.store.save(rec)
        try:
            outcome = await self.ops.verify(sym)
        except Exception as exc:
            outcome = VerifyOutcome("FAIL", [_err(exc)])
        if outcome.status == "FAIL":
            detail = "; ".join(outcome.failures) or "verification FAIL"
            summary.verification_failed.append(
                {"symbol": sym, "failures": outcome.failures, "report": outcome.report}
            )
            summary.alert("verification_failed", sym, detail)
            await self._block(rec, now, BlockedReason.verification_failed, detail)
            return
        summary.verification_passed.append(
            {"symbol": sym, "status": outcome.status, "report": outcome.report}
        )
        rec = rec.transition(SymbolState.warming_up, now, verified_at=datetime.now(UTC))
        await self.store.save(rec)

        try:
            reports = await self.warm_up(sym)
            error = None if reports > 0 else "warm-up produced no agent reports"
        except Exception as exc:
            reports, error = 0, _err(exc)
        if error is not None:
            summary.warmup_failed.append({"symbol": sym, "error": error})
            summary.alert("warmup_failed", sym, error)
            await self._block(rec, now, BlockedReason.warmup_failed, error)
            return
        summary.warmup_done.append({"symbol": sym, "reports": reports})
        rec = rec.transition(
            SymbolState.tradeable, now, warmed_up_at=datetime.now(UTC), last_error=None
        )
        await self.store.save(rec)

    async def _fetch(
        self,
        rec: SymbolRecord,
        cand: Candidate,
        now: datetime,
        summary: _Summary,
        *,
        alert_kind: str,
    ) -> str | None:
        """Download missing history (needed period + warm-up), else update. Kind, or None."""
        start = self.needed_start(cand, now)
        kind = "update" if self.ops.has_history(rec.symbol, start) else "history"
        try:
            if kind == "history":
                await self.ops.download(rec.symbol, start)
            else:
                await self.ops.update(rec.symbol)
        except Exception as exc:
            summary.downloads_failed.append(
                {"symbol": rec.symbol, "kind": kind, "error": _err(exc)}
            )
            summary.alert(alert_kind, rec.symbol, _err(exc))
            return None
        summary.downloads_done.append(
            {
                "symbol": rec.symbol,
                "kind": kind,
                "start": start.isoformat() if kind == "history" else None,
            }
        )
        return kind

    async def _block(
        self, rec: SymbolRecord, now: datetime, reason: BlockedReason, error: str | None
    ) -> None:
        await self.store.save(
            rec.transition(
                SymbolState.blocked,
                now,
                blocked_reason=reason,
                last_error=error,
                verified_at=None,
                warmed_up_at=None,
            )
        )


# ---------------------------------------------------------------- summary


class _Summary:
    def __init__(self, run_date: date, started_at: datetime, cfg: UniverseConfig) -> None:
        self.run_date = run_date
        self.started_at = started_at
        self.cfg = cfg
        self.entered: list[dict[str, Any]] = []
        self.exited: list[str] = []
        self.new_watch_only: list[str] = []
        self.promoted: list[str] = []
        self.retried: list[str] = []
        self.downloads_done: list[dict[str, Any]] = []
        self.downloads_failed: list[dict[str, Any]] = []
        self.verification_passed: list[dict[str, Any]] = []
        self.verification_failed: list[dict[str, Any]] = []
        self.warmup_done: list[dict[str, Any]] = []
        self.warmup_failed: list[dict[str, Any]] = []
        self.skipped_disk_guard: list[str] = []
        self.blocked: list[dict[str, Any]] = []
        self.tradeable: list[str] = []
        self.universe_size: int | None = None
        self.disk_start: DiskUsage | None = None
        self.disk_end: DiskUsage | None = None
        self.disk_guard_tripped = False
        self.alerts: list[dict[str, Any]] = []

    def alert(self, kind: str, symbol: str | None, message: str) -> None:
        entry = {
            "kind": kind,
            "symbol": symbol,
            "message": message,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.alerts.append(entry)
        log.error("universe_alert", **entry)

    def last(self, symbol: str) -> str | None:
        for a in reversed(self.alerts):
            if a["symbol"] == symbol:
                return a["message"]
        return None

    def finish(self, finished_at: datetime, status: str) -> dict[str, Any]:
        disk = self.disk_end or self.disk_start
        return {
            "run_date": self.run_date.isoformat(),
            "status": status,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "config": self.cfg.as_dict(),
            "universe_size": self.universe_size,
            "tradeable": self.tradeable,
            "entered": self.entered,
            "exited": sorted(self.exited),
            "new_watch_only": sorted(self.new_watch_only),
            "promoted": sorted(self.promoted),
            "retried": sorted(self.retried),
            "downloads": {"done": self.downloads_done, "failed": self.downloads_failed},
            "verification": {
                "passed": self.verification_passed,
                "failed": self.verification_failed,
            },
            "warm_up": {"done": self.warmup_done, "failed": self.warmup_failed},
            "blocked": self.blocked,
            "skipped_disk_guard": sorted(self.skipped_disk_guard),
            "disk": {
                **(disk.as_dict() if disk else {}),
                "start": self.disk_start.as_dict() if self.disk_start else None,
                "threshold_pct": str(self.cfg.disk_max_used_pct),
                "guard_tripped": self.disk_guard_tripped,
            },
            "alerts": self.alerts,
        }
