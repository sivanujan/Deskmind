"""Control dashboard API routes: services, trading modes, approvals.

Includes kill switch, backtests, and audit logs.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from deskmind.api.audit import get_audit_logs, record_audit_action
from deskmind.api.auth import (
    SESSION_COOKIE_NAME,
    AuthManager,
    LoginRequest,
    LoginResponse,
    SessionData,
    UserInfoResponse,
    forbid_live_trading_inspection,
    get_auth_manager,
    get_client_ip,
    get_current_session,
    require_csrf,
    verify_password,
)
from deskmind.backtest.queue import (
    BacktestQueueManager,
    BacktestSubmitRequest,
    get_queue_manager,
)
from deskmind.backtest.registry import get_run_local
from deskmind.config import get_settings
from deskmind.logging import get_logger
from deskmind.supervisor.manager import SupervisorManager, get_supervisor

log = get_logger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------------
# Request & Response Models
# -----------------------------------------------------------------------------


class ServiceControlRequest(BaseModel):
    service: Literal["live", "trend", "trade"]
    action: Literal["start", "stop", "restart"]


class StopAllServicesRequest(BaseModel):
    confirm: str = Field(description="Must match 'STOP_ALL'")


class ModeControlRequest(BaseModel):
    trading_mode: Literal["paper", "testnet"]
    approval_mode: Literal["auto", "manual"]


class ApprovalActionRequest(BaseModel):
    req_id: str


class KillSwitchRequest(BaseModel):
    confirm: str = Field(description="Must match 'KILL'")


class BreakerResetRequest(BaseModel):
    confirm: str = Field(description="Must match 'RESET'")


class BacktestCancelRequest(BaseModel):
    job_id: str


# -----------------------------------------------------------------------------
# Auth Endpoints (/api/auth/*)
# -----------------------------------------------------------------------------


@router.post("/api/auth/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    auth_mgr: Annotated[AuthManager, Depends(get_auth_manager)],
) -> LoginResponse:
    ip = get_client_ip(request)
    if not auth_mgr.check_rate_limit(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait 1 minute.",
        )

    settings = getattr(request.app.state, "settings", None) or get_settings()
    expected_user = settings.dashboard_username
    stored_hash = settings.dashboard_password_hash.get_secret_value()

    if req.username != expected_user or not verify_password(req.password, stored_hash):
        auth_mgr.record_failed_attempt(ip)
        await record_audit_action(
            action="login_failed",
            user=req.username,
            details={"ip": ip},
            ip_address=ip,
            status="failed",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    auth_mgr.reset_rate_limit(ip)
    session = auth_mgr.create_session(req.username)

    # Set secure cookie
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.session_id,
        httponly=True,
        samesite="lax",
        secure=False,  # Local/testing compatibility
        max_age=86400,
    )

    await record_audit_action(
        action="login_success",
        user=req.username,
        details={"ip": ip},
        ip_address=ip,
        status="success",
    )

    return LoginResponse(
        ok=True,
        username=session.username,
        csrf_token=session.csrf_token,
    )


@router.post("/api/auth/logout")
async def logout(
    request: Request,
    response: Response,
    auth_mgr: Annotated[AuthManager, Depends(get_auth_manager)],
) -> dict[str, bool]:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    auth_mgr.delete_session(session_id)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/api/auth/me", response_model=UserInfoResponse)
async def get_me(
    session: Annotated[SessionData, Depends(get_current_session)],
) -> UserInfoResponse:
    settings = get_settings()
    return UserInfoResponse(
        username=session.username,
        authenticated=True,
        mode=settings.trading_mode,
    )


@router.get("/api/auth/csrf")
async def get_csrf(
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, str]:
    return {"csrf_token": session.csrf_token}


# -----------------------------------------------------------------------------
# System Status & Read-only Dashboard Overview
# -----------------------------------------------------------------------------


@router.get("/api/system/status")
async def get_system_status(
    supervisor: Annotated[SupervisorManager, Depends(get_supervisor)],
) -> dict[str, Any]:
    """Real status only: trade-service status file + breaker state file, else explicit unknown."""
    from deskmind.api.dashboard import TRADE_STATUS_FRESH_S, read_trade_status

    settings = get_settings()
    services = supervisor.get_status()
    status_data, age = read_trade_status()
    fresh = status_data is not None and age is not None and age <= TRADE_STATUS_FRESH_S
    breaker = _breaker_file_state()
    if fresh and status_data:
        risk_state = status_data.get("breaker_state")
        source = "trade service status"
    elif breaker:
        risk_state = breaker["halt_state"]
        source = "risk state file"
    else:
        risk_state = None  # unknown: no trade status and no breaker state file
        source = None
    return {
        "mode_badge": "DEMO / TESTNET" if settings.trading_mode == "testnet" else "PAPER",
        "trading_mode": settings.trading_mode,
        "approval_mode": settings.approval_mode,
        "live_trading_allowed": False,  # Non-negotiable safety badge
        "services": services,
        "risk_state": risk_state,
        "risk_state_source": source,
        "circuit_breaker_tripped": None if risk_state is None else risk_state != "normal",
        "risk_details": breaker,
        "trade_status_age_s": age,
        "stats": status_data.get("stats") if fresh and status_data else None,
        "open_positions": status_data.get("positions", []) if fresh and status_data else None,
        "alerts": list(status_data.get("warnings", [])) if fresh and status_data else [],
    }


def _breaker_file_state() -> dict[str, Any] | None:
    """Circuit breaker state persisted by the Risk Manager (`deskmind risk status` reads it)."""
    from deskmind.risk.breakers import CircuitBreakerTracker

    path = get_settings().data_dir / "risk_state.json"
    if not path.exists():
        return None
    t = CircuitBreakerTracker.load_state(path)
    dd = (
        float((t.peak_equity - t.start_of_day_equity) / t.peak_equity) if t.peak_equity > 0 else 0.0
    )
    return {
        "halt_state": t.halt_state,
        "peak_equity": str(t.peak_equity),
        "start_of_day_equity": str(t.start_of_day_equity),
        "drawdown_pct_of_peak": dd,
        "consecutive_losses": t.consecutive_losses,
        "cooldown_until_ms": t.cooldown_until_ms,
        "transitions": [dict(vars(x)) for x in t.transitions[-50:]],
        "file": str(path),
    }


# -----------------------------------------------------------------------------
# Service Control Endpoints (POST /api/control/* with Auth + CSRF)
# -----------------------------------------------------------------------------


@router.post("/api/control/service", dependencies=[Depends(require_csrf)])
async def control_service(
    req: ServiceControlRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    supervisor: Annotated[SupervisorManager, Depends(get_supervisor)],
) -> dict[str, Any]:
    action = req.action
    svc = req.service
    ip = get_client_ip(request)

    ok = False
    if action == "start":
        ok = await supervisor.start_service(svc)
    elif action == "stop":
        ok = await supervisor.stop_service(svc)
    elif action == "restart":
        ok = await supervisor.restart_service(svc)

    await record_audit_action(
        action=f"service_{action}",
        user=session.username,
        details={"service": svc, "success": ok},
        ip_address=ip,
        status="success" if ok else "failed",
    )

    return {"service": svc, "action": action, "success": ok}


@router.post("/api/control/service/stop-all", dependencies=[Depends(require_csrf)])
async def stop_all_services(
    req: StopAllServicesRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    supervisor: Annotated[SupervisorManager, Depends(get_supervisor)],
) -> dict[str, Any]:
    if req.confirm != "STOP_ALL":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Confirmation text must match 'STOP_ALL' exactly.",
        )

    ip = get_client_ip(request)
    results = await supervisor.stop_all(confirm="STOP_ALL")
    await record_audit_action(
        action="stop_all_services",
        user=session.username,
        details={"results": results},
        ip_address=ip,
    )
    return {"success": True, "results": results}


@router.post("/api/control/mode", dependencies=[Depends(require_csrf)])
async def set_modes(
    req: ModeControlRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, Any]:
    forbid_live_trading_inspection(req.trading_mode)
    if req.trading_mode not in ("paper", "testnet"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only 'paper' or 'testnet' trading modes are permitted.",
        )

    ip = get_client_ip(request)
    settings = get_settings()
    await record_audit_action(
        action="set_trading_mode_refused",
        user=session.username,
        details={
            "requested": {"trading_mode": req.trading_mode, "approval_mode": req.approval_mode}
        },
        ip_address=ip,
        status="not_applied",
    )
    # Modes come from .env (TRADING_MODE / APPROVAL_MODE) and are read by the trade service at
    # start. Changing them at runtime is not implemented, so say so instead of faking success.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"Not applied: modes are set in .env (currently TRADING_MODE={settings.trading_mode}, "
            f"APPROVAL_MODE={settings.approval_mode}) and take effect when the trade service "
            "restarts."
        ),
    )


def _approval_gate() -> Any:
    from deskmind.execution.approval import ApprovalGate

    return ApprovalGate(mode="manual", trading_mode=get_settings().trading_mode)


@router.get("/api/control/pending")
async def get_pending_approvals() -> list[dict[str, Any]]:
    """Pending trade intents from the approval gate (same store as `deskmind trade pending`)."""
    return [r.to_dict() for r in _approval_gate().get_pending()]


@router.post("/api/control/approve", dependencies=[Depends(require_csrf)])
async def approve_trade(
    req: ApprovalActionRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, Any]:
    ip = get_client_ip(request)
    ok = _approval_gate().approve(req.req_id, note=f"Approved via dashboard by {session.username}")
    await record_audit_action(
        action="approve_trade",
        user=session.username,
        details={"req_id": req.req_id, "applied": ok},
        ip_address=ip,
        status="success" if ok else "failed",
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{req.req_id} is not pending (unknown, expired or already resolved)",
        )
    return {"req_id": req.req_id, "status": "approved"}


@router.post("/api/control/reject", dependencies=[Depends(require_csrf)])
async def reject_trade(
    req: ApprovalActionRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, Any]:
    ip = get_client_ip(request)
    ok = _approval_gate().reject(req.req_id, note=f"Rejected via dashboard by {session.username}")
    await record_audit_action(
        action="reject_trade",
        user=session.username,
        details={"req_id": req.req_id, "applied": ok},
        ip_address=ip,
        status="success" if ok else "failed",
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{req.req_id} is not pending (unknown, expired or already resolved)",
        )
    return {"req_id": req.req_id, "status": "rejected"}


@router.post("/api/control/kill", dependencies=[Depends(require_csrf)])
async def trigger_kill_switch(
    req: KillSwitchRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    supervisor: Annotated[SupervisorManager, Depends(get_supervisor)],
) -> dict[str, Any]:
    """Emergency stop: stops every supervised service (live, trend, trade).

    It does not close positions or cancel orders; open positions keep their exchange-side
    stop-loss and take-profit orders.
    """
    if req.confirm != "KILL":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Typed confirmation 'KILL' is required.",
        )

    ip = get_client_ip(request)
    await supervisor.stop_all(confirm="STOP_ALL")
    await record_audit_action(
        action="emergency_kill_switch",
        user=session.username,
        details={"action": "halted_and_stopped_services"},
        ip_address=ip,
        status="emergency_executed",
    )
    log.critical("EMERGENCY_KILL_SWITCH_EXECUTED_BY_DASHBOARD", user=session.username)
    return {"status": "killed", "message": "Emergency kill switch executed. All services stopped."}


@router.post("/api/control/breaker/reset", dependencies=[Depends(require_csrf)])
async def reset_circuit_breaker(
    req: BreakerResetRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, Any]:
    if req.confirm != "RESET":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Typed confirmation 'RESET' is required.",
        )

    import time as _time

    from deskmind.risk.breakers import CircuitBreakerTracker

    ip = get_client_ip(request)
    path = get_settings().data_dir / "risk_state.json"
    tracker = CircuitBreakerTracker.load_state(path) if path.exists() else None
    if tracker is None or tracker.halt_state != "full_stop":
        state = tracker.halt_state if tracker else "no state file"
        await record_audit_action(
            action="circuit_breaker_reset",
            user=session.username,
            details={"applied": False, "state": state},
            ip_address=ip,
            status="not_applied",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Not applied: breaker state is '{state}'. Manual reset only applies to "
                "'full_stop'; other states clear by their own hysteresis rules."
            ),
        )
    tracker.reset_full_stop(
        current_equity=tracker.start_of_day_equity, timestamp_ms=int(_time.time() * 1000)
    )
    tracker.save_state(path)
    await record_audit_action(
        action="circuit_breaker_reset",
        user=session.username,
        details={"applied": True, "from": "full_stop"},
        ip_address=ip,
    )
    return {"status": "reset", "circuit_breaker": tracker.halt_state}


# -----------------------------------------------------------------------------
# Backtest Lab Endpoints
# -----------------------------------------------------------------------------


@router.post("/api/control/backtest/submit", dependencies=[Depends(require_csrf)])
async def submit_backtest(
    req: BacktestSubmitRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    queue_mgr: Annotated[BacktestQueueManager, Depends(get_queue_manager)],
) -> dict[str, Any]:
    forbid_live_trading_inspection(str(req))
    if str(req.split).lower() == "test":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The 'test' split cannot be run from the UI (CLI only).",
        )

    try:
        job = await queue_mgr.submit_job(req)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    ip = get_client_ip(request)
    await record_audit_action(
        action="submit_backtest",
        user=session.username,
        details={"job_id": job.job_id, "strategy": req.strategy, "split": req.split},
        ip_address=ip,
    )
    return {"job_id": job.job_id, "status": job.status}


@router.post("/api/control/backtest/cancel", dependencies=[Depends(require_csrf)])
async def cancel_backtest(
    req: BacktestCancelRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    queue_mgr: Annotated[BacktestQueueManager, Depends(get_queue_manager)],
) -> dict[str, Any]:
    success = await queue_mgr.cancel_job(req.job_id)
    ip = get_client_ip(request)
    await record_audit_action(
        action="cancel_backtest",
        user=session.username,
        details={"job_id": req.job_id, "success": success},
        ip_address=ip,
    )
    return {"job_id": req.job_id, "cancelled": success}


@router.get("/api/backtest/jobs")
async def list_backtest_jobs(
    queue_mgr: Annotated[BacktestQueueManager, Depends(get_queue_manager)],
) -> list[dict[str, Any]]:
    jobs = queue_mgr.list_jobs()
    return [
        {
            "job_id": j.job_id,
            "status": j.status,
            "created_at": j.created_at,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "progress_pct": j.progress_pct,
            "run_id": j.run_id,
            "error": j.error,
            "metrics": j.metrics,
            "request": j.request,
        }
        for j in jobs
    ]


@router.get("/api/backtest/jobs/{job_id}")
async def get_backtest_job(
    job_id: str,
    queue_mgr: Annotated[BacktestQueueManager, Depends(get_queue_manager)],
) -> dict[str, Any]:
    job = queue_mgr.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "progress_pct": job.progress_pct,
        "run_id": job.run_id,
        "error": job.error,
        "metrics": job.metrics,
        "request": job.request,
    }


@router.get("/api/backtest/compare")
async def compare_backtests(
    run_a: str = Query(...),
    run_b: str = Query(...),
) -> dict[str, Any]:
    rec_a = get_run_local(run_a)
    rec_b = get_run_local(run_b)
    if not rec_a or not rec_b:
        raise HTTPException(status_code=404, detail="One or both runs not found")

    return {
        "run_a": rec_a,
        "run_b": rec_b,
    }


# -----------------------------------------------------------------------------
# Trades & Journal Endpoints
# -----------------------------------------------------------------------------


@router.get("/api/journal/trades")
async def list_journal_trades(
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from sqlalchemy import desc, select

    from deskmind.db.models import TradeJournal
    from deskmind.db.session import session_scope

    try:
        async with session_scope() as session:
            stmt = select(TradeJournal).order_by(desc(TradeJournal.created_at)).limit(limit)
            if symbol:
                stmt = stmt.where(TradeJournal.symbol == symbol)
            if status:
                stmt = stmt.where(TradeJournal.status == status)
            res = await session.execute(stmt)
            trades = res.scalars().all()
            return [
                {
                    "id": t.id,
                    "created_at": t.created_at.isoformat(),
                    "symbol": t.symbol,
                    "side": t.side,
                    "entry": float(t.entry),
                    "sl": float(t.sl),
                    "tp": float(t.tp),
                    "size": float(t.size),
                    "leverage": t.leverage,
                    "status": t.status,
                    "pnl": float(t.pnl) if t.pnl is not None else None,
                    "exit_price": float(t.exit_price) if t.exit_price is not None else None,
                    "exit_reason": t.exit_reason,
                    "trading_mode": t.trading_mode,
                    "client_order_id": t.client_order_id,
                }
                for t in trades
            ]
    except Exception as e:
        # Surface the failure: an empty list would look like "no trades".
        log.warning("fetch_journal_trades_error", error=str(e))
        raise HTTPException(status_code=503, detail=f"trade_journal unavailable: {e}") from e


# -----------------------------------------------------------------------------
# Audit Log & Strategy Library & Account Info
# -----------------------------------------------------------------------------


@router.get("/api/audit/logs")
async def list_audit_logs(
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return await get_audit_logs(limit=limit, action=action)


# -----------------------------------------------------------------------------
# WebSocket for Backtest Progress Streaming
# -----------------------------------------------------------------------------


@router.websocket("/ws/backtest/{job_id}")
async def ws_backtest_progress(
    websocket: WebSocket,
    job_id: str,
    queue_mgr: Annotated[BacktestQueueManager, Depends(get_queue_manager)],
) -> None:
    await websocket.accept()
    q = await queue_mgr.subscribe_progress(job_id)
    try:
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
            if msg.get("status") in ("completed", "failed", "cancelled"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        queue_mgr.unsubscribe_progress(job_id, q)
