"""Redis-backed backtest job queue with concurrency limits and progress streaming."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from deskmind.config import DEFAULT_DATA_SYMBOLS
from deskmind.logging import get_logger

log = get_logger(__name__)

JobState = Literal["queued", "running", "completed", "failed", "cancelled"]


class BacktestSubmitRequest(BaseModel):
    strategy: str = "trend_follow_v1"
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_DATA_SYMBOLS))
    split: Literal["train", "validation"] = "train"  # 'test' split strictly forbidden from UI
    start_date: str | None = None
    end_date: str | None = None
    initial_cash: Decimal = Decimal("10000.0")
    warmup_days: int = 30


@dataclass
class JobRecord:
    job_id: str
    request: dict[str, Any]
    status: JobState = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    progress_pct: float = 0.0
    current_date: str | None = None
    trades_count: int = 0
    run_id: str | None = None
    error: str | None = None
    metrics: dict[str, Any] | None = None


class BacktestQueueManager:
    """Manages queueing, concurrency limits (1-2), and background execution of backtests."""

    def __init__(self, max_concurrency: int = 2) -> None:
        self.max_concurrency = max_concurrency
        self.jobs: dict[str, JobRecord] = {}
        self.queue: list[str] = []
        self.active_tasks: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._worker_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._worker_task = asyncio.create_task(self._process_queue_loop())
        log.info("backtest_queue_started", max_concurrency=self.max_concurrency)

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        for task in self.active_tasks.values():
            task.cancel()
        log.info("backtest_queue_stopped")

    async def submit_job(self, req: BacktestSubmitRequest) -> JobRecord:
        """Submit a new backtest job. Validates that 'test' split cannot be run from UI."""
        # Non-negotiable safety invariant: test split is CLI only
        if str(req.split).lower() == "test":
            raise ValueError(
                "HARD SAFETY RULE: The 'test' holdout split is locked and cannot be run from "
                "the web dashboard. Use 'uv run deskmind backtest run --split test' via CLI only."
            )

        now_str = datetime.now(UTC).isoformat()
        job_id = f"bt_job_{uuid.uuid4().hex[:12]}"
        record = JobRecord(
            job_id=job_id,
            request=req.model_dump(mode="json"),
            status="queued",
            created_at=now_str,
        )
        self.jobs[job_id] = record
        self.queue.append(job_id)
        log.info("backtest_job_queued", job_id=job_id, strategy=req.strategy, split=req.split)
        return record

    async def cancel_job(self, job_id: str) -> bool:
        if job_id not in self.jobs:
            return False
        rec = self.jobs[job_id]
        if rec.status in ("completed", "failed", "cancelled"):
            return False

        rec.status = "cancelled"
        rec.finished_at = datetime.now(UTC).isoformat()
        if job_id in self.queue:
            self.queue.remove(job_id)
        if job_id in self.active_tasks:
            self.active_tasks[job_id].cancel()
            del self.active_tasks[job_id]

        await self._broadcast_progress(
            job_id, {"status": "cancelled", "progress_pct": rec.progress_pct}
        )
        log.info("backtest_job_cancelled", job_id=job_id)
        return True

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        return list(self.jobs.values())[-limit:]

    async def subscribe_progress(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        if job_id not in self._subscribers:
            self._subscribers[job_id] = []
        self._subscribers[job_id].append(q)
        return q

    def unsubscribe_progress(self, job_id: str, q: asyncio.Queue) -> None:
        if job_id in self._subscribers and q in self._subscribers[job_id]:
            self._subscribers[job_id].remove(q)

    async def _broadcast_progress(self, job_id: str, data: dict[str, Any]) -> None:
        if job_id in self._subscribers:
            for q in list(self._subscribers[job_id]):
                with asyncio.suppress(Exception):
                    q.put_nowait(data)

    async def _process_queue_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(0.5)
                # Clean up finished tasks
                finished = [jid for jid, t in self.active_tasks.items() if t.done()]
                for jid in finished:
                    del self.active_tasks[jid]

                # Check if we can start a new job
                if len(self.active_tasks) < self.max_concurrency and self.queue:
                    next_job_id = self.queue.pop(0)
                    rec = self.jobs[next_job_id]
                    if rec.status == "queued":
                        task = asyncio.create_task(self._run_job(rec))
                        self.active_tasks[next_job_id] = task
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("queue_worker_loop_error", error=str(e))

    async def _run_job(self, rec: JobRecord) -> None:
        rec.status = "running"
        rec.started_at = datetime.now(UTC).isoformat()
        log.info("starting_backtest_job", job_id=rec.job_id)

        try:
            # The real event-driven engine (same call as `deskmind backtest run`). It reports no
            # intermediate progress, so progress_pct stays 0 until the run finishes.
            from deskmind.backtest.runner import run_backtest

            req = rec.request
            # CPU-bound: run on its own event loop in a worker thread so the API stays responsive.
            res = await asyncio.to_thread(
                asyncio.run,
                run_backtest(
                    strategy_name=req.get("strategy", "trend_follow_v1"),
                    split=req.get("split", "train"),
                    symbols=req.get("symbols") or None,
                    start_date=req.get("start_date"),
                    end_date=req.get("end_date"),
                    initial_cash=Decimal(str(req.get("initial_cash", "10000"))),
                    warmup_days=int(req.get("warmup_days", 30)),
                    notes=f"dashboard job {rec.job_id}",
                ),
            )
            m = res["metrics"]
            metrics = dataclasses.asdict(m) if dataclasses.is_dataclass(m) else dict(m)
            rec.status = "completed"
            rec.run_id = res["run_id"]
            rec.trades_count = res["trades_count"]
            rec.progress_pct = 100.0
            rec.finished_at = datetime.now(UTC).isoformat()
            rec.metrics = {k: v for k, v in metrics.items() if not isinstance(v, dict | list)}
            await self._broadcast_progress(
                rec.job_id,
                {
                    "job_id": rec.job_id,
                    "status": "completed",
                    "progress_pct": 100.0,
                    "run_id": rec.run_id,
                    "metrics": rec.metrics,
                },
            )
            log.info("backtest_job_completed", job_id=rec.job_id, run_id=rec.run_id)
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.finished_at = datetime.now(UTC).isoformat()
        except Exception as e:
            rec.status = "failed"
            rec.error = str(e)
            rec.finished_at = datetime.now(UTC).isoformat()
            log.error("backtest_job_failed", job_id=rec.job_id, error=str(e))
            await self._broadcast_progress(
                rec.job_id,
                {"job_id": rec.job_id, "status": "failed", "error": str(e)},
            )


_queue_manager: BacktestQueueManager | None = None


def get_queue_manager() -> BacktestQueueManager:
    global _queue_manager
    if _queue_manager is None:
        _queue_manager = BacktestQueueManager()
    return _queue_manager
