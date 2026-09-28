"""Dashboard buttons for one-off jobs (login + CSRF, audit-logged, run in the background).

- POST /api/control/universe/run   the daily universe job once (same as `deskmind universe run`)
- POST /api/control/account/sync   read-only mainnet snapshot (same as `deskmind account sync`;
                                    the key's permissions are checked first, GET only)
- GET  /api/control/tasks          state of the last run of each job (the page polls this)

Only one run per job at a time (409 while running). Nothing here places, modifies or cancels an
order.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from deskmind.api.auth import SessionData, get_client_ip, get_current_session, require_csrf
from deskmind.config import get_settings
from deskmind.logging import get_logger

log = get_logger(__name__)
router = APIRouter()

JOBS = ("universe_run", "account_sync")
_tasks: dict[str, dict[str, Any]] = {}
_handles: dict[str, asyncio.Task[None]] = {}


def task_state(kind: str) -> dict[str, Any] | None:
    return _tasks.get(kind)


async def start_task(
    kind: str, fn: Callable[[], Awaitable[dict[str, Any]]], user: str
) -> dict[str, Any]:
    running = _handles.get(kind)
    if running is not None and not running.done():
        started = _tasks[kind]["started_at"]
        raise HTTPException(409, f"{kind} is already running (started {started})")
    state: dict[str, Any] = {
        "kind": kind,
        "state": "running",
        "started_at": int(time.time() * 1000),
        "finished_at": None,
        "user": user,
        "result": None,
        "error": None,
    }
    _tasks[kind] = state

    async def runner() -> None:
        try:
            state["result"] = await fn()
            state["state"] = "done"
        except Exception as exc:  # shown on the page; the job's own alerts are in its summary
            log.error("dashboard_task_failed", kind=kind, error=str(exc))
            state["state"] = "failed"
            state["error"] = str(exc)[:500]
        finally:
            state["finished_at"] = int(time.time() * 1000)

    _handles[kind] = asyncio.create_task(runner(), name=f"dashboard_{kind}")
    return dict(state)


async def _universe_run() -> dict[str, Any]:
    from deskmind.universe.daily import DailyJob
    from deskmind.universe.ops import LiveDataOps
    from deskmind.universe.ranking import UniverseConfig
    from deskmind.universe.store import PostgresUniverseStore

    settings = get_settings()
    cfg = UniverseConfig.from_settings(settings)
    summary = await DailyJob(PostgresUniverseStore(), LiveDataOps(settings, cfg), cfg).run()
    return {
        "entered": len(summary.get("entered", [])),
        "exited": len(summary.get("exited", [])),
        "tradeable": len(summary.get("tradeable", [])),
        "alerts": summary.get("alerts", []),
    }


async def _account_sync() -> dict[str, Any]:
    from deskmind.data.mainnet_readonly import MainnetReadOnlyClient

    settings = get_settings()
    if not settings.binance_mainnet_readonly_api_key.get_secret_value():
        raise RuntimeError(
            "no read-only mainnet key configured (BINANCE_MAINNET_READONLY_API_KEY in .env)"
        )
    client = MainnetReadOnlyClient(settings)
    try:
        report = await client.sync_account()
    finally:
        await client.close()
    return {"synced_at": report.get("synced_at"), "symbols": len(report.get("symbols", []))}


RUNNERS: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "universe_run": _universe_run,
    "account_sync": _account_sync,
}


async def _trigger(kind: str, request: Request, session: SessionData) -> dict[str, Any]:
    from deskmind.api.audit import record_audit_action

    state = await start_task(kind, RUNNERS[kind], session.username)
    await record_audit_action(
        action=f"start_{kind}",
        user=session.username,
        details={"kind": kind},
        ip_address=get_client_ip(request),
    )
    return state


@router.post("/api/control/universe/run", dependencies=[Depends(require_csrf)])
async def trigger_universe_run(
    request: Request, session: Annotated[SessionData, Depends(get_current_session)]
) -> dict[str, Any]:
    return await _trigger("universe_run", request, session)


@router.post("/api/control/account/sync", dependencies=[Depends(require_csrf)])
async def trigger_account_sync(
    request: Request, session: Annotated[SessionData, Depends(get_current_session)]
) -> dict[str, Any]:
    return await _trigger("account_sync", request, session)


@router.get("/api/control/tasks")
async def list_tasks() -> dict[str, Any]:
    return {kind: task_state(kind) for kind in JOBS}
