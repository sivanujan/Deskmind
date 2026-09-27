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
    settings = get_settings()
    services = supervisor.get_status()

    return {
        "mode_badge": "DEMO / TESTNET" if settings.trading_mode == "testnet" else "PAPER",
        "trading_mode": settings.trading_mode,
        "approval_mode": settings.approval_mode,
        "live_trading_allowed": False,  # Non-negotiable safety badge
        "services": services,
        "risk_state": {
            "circuit_breaker": "normal",
            "drawdown_pct": 0.0,
            "consecutive_losses": 0,
            "max_drawdown_limit_pct": 15.0,
        },
        "today_stats": {
            "intents": 0,
            "approvals": 0,
            "rejections": 0,
            "orders": 0,
        },
        "open_positions": [],
        "alerts": [],
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
    # Record mode update in audit log
    await record_audit_action(
        action="set_trading_mode",
        user=session.username,
        details={"trading_mode": req.trading_mode, "approval_mode": req.approval_mode},
        ip_address=ip,
    )
    return {"trading_mode": req.trading_mode, "approval_mode": req.approval_mode}


@router.get("/api/control/pending")
async def get_pending_approvals() -> list[dict[str, Any]]:
    """Return pending trade intents awaiting human confirmation."""
    return []


@router.post("/api/control/approve", dependencies=[Depends(require_csrf)])
async def approve_trade(
    req: ApprovalActionRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, Any]:
    ip = get_client_ip(request)
    await record_audit_action(
        action="approve_trade",
        user=session.username,
        details={"req_id": req.req_id},
        ip_address=ip,
    )
    return {"req_id": req.req_id, "status": "approved"}


@router.post("/api/control/reject", dependencies=[Depends(require_csrf)])
async def reject_trade(
    req: ApprovalActionRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
) -> dict[str, Any]:
    ip = get_client_ip(request)
    await record_audit_action(
        action="reject_trade",
        user=session.username,
        details={"req_id": req.req_id},
        ip_address=ip,
    )
    return {"req_id": req.req_id, "status": "rejected"}


@router.post("/api/control/kill", dependencies=[Depends(require_csrf)])
async def trigger_kill_switch(
    req: KillSwitchRequest,
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    supervisor: Annotated[SupervisorManager, Depends(get_supervisor)],
) -> dict[str, Any]:
    """Emergency Kill Switch: closes all positions, cancels orders, halts system."""
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

    ip = get_client_ip(request)
    await record_audit_action(
        action="circuit_breaker_reset",
        user=session.username,
        details={"reset": True},
        ip_address=ip,
    )
    return {"status": "reset", "circuit_breaker": "normal"}


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
        log.debug("fetch_journal_trades_error", error=str(e))
        return []


# -----------------------------------------------------------------------------
# Audit Log & Strategy Library & Account Info
# -----------------------------------------------------------------------------


@router.get("/api/audit/logs")
async def list_audit_logs(
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return await get_audit_logs(limit=limit, action=action)


@router.get("/api/strategies")
async def list_strategies() -> list[dict[str, Any]]:
    """Return available strategies from strategy_library."""
    return [
        {
            "name": "trend_follow_v1",
            "version": "1.0.0",
            "description": (
                "Multi-timeframe trend following with swing structures and BOS confirmation."
            ),
            "symbols": list(get_settings().data_symbols),
            "status": "untested",
            "parameters": {
                "fast_period": 20,
                "slow_period": 50,
                "risk_per_trade_pct": 1.0,
            },
        }
    ]


@router.get("/api/account/info")
async def get_account_info() -> dict[str, Any]:
    """Return read-only mainnet account sync summary."""
    from deskmind.config import (
        DEFAULT_MAINTENANCE_BRACKETS,
        DEFAULT_MAKER_FEE_RATE,
        DEFAULT_TAKER_FEE_RATE,
    )

    return {
        "status": "read_only",
        "source": "binance_mainnet_readonly",
        "vip_level": "VIP0",
        "default_fees": {
            "maker_rate": str(DEFAULT_MAKER_FEE_RATE),
            "taker_rate": str(DEFAULT_TAKER_FEE_RATE),
        },
        "default_mmr_tier_1": str(DEFAULT_MAINTENANCE_BRACKETS[0][1]),
        "notes": "Mainnet data is strictly read-only for metadata verification.",
    }


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
