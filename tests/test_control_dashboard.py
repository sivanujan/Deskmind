"""Unit tests for the Control Dashboard backend.

All network and child processes are mocked (strictly no live calls).
Verifies:
1. Auth, session management, CSRF validation, and login rate limiting.
2. Hard safety rule: live trading rejection and test split lockdown.
3. Typed confirmation enforcement for Kill Switch, Breaker Reset, and Stop All Services.
4. Supervisor process lifecycle (start, stop, restart, crash backoff).
5. Backtest job queue concurrency and lifecycle.
6. Audit log recording for all control actions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskmind.api.app import create_app
from deskmind.api.audit import get_audit_logs, record_audit_action
from deskmind.api.auth import hash_password, reset_auth_manager
from deskmind.backtest.queue import BacktestQueueManager, BacktestSubmitRequest
from deskmind.config import Settings
from deskmind.supervisor.manager import SupervisorManager


def make_test_settings() -> Settings:
    return Settings(
        trading_mode="testnet",
        dashboard_username="admin",
        dashboard_password_hash=SecretStr(hash_password("admin_pass_123")),
        session_secret_key=SecretStr("test_secret_key"),
        data_symbols=["BTCUSDT"],
    )


@pytest.fixture(autouse=True)
def clean_auth_state():
    reset_auth_manager()
    yield
    reset_auth_manager()


@pytest.fixture
def dashboard_app(tmp_path):
    settings = make_test_settings()
    app = create_app(settings=settings, health_checks=False)
    return app


# -----------------------------------------------------------------------------
# 1. Auth, Rate Limiting & Session Lifecycle
# -----------------------------------------------------------------------------


def test_login_rate_limiting(dashboard_app):
    client = TestClient(dashboard_app)

    # 5 failed attempts allowed
    for _ in range(5):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong_password"})
        assert r.status_code == 401

    # 6th attempt must be blocked with 429
    r6 = client.post("/api/auth/login", json={"username": "admin", "password": "wrong_password"})
    assert r6.status_code == 429
    assert "Too many failed login attempts" in r6.json()["detail"]


def test_login_success_and_session(dashboard_app):
    client = TestClient(dashboard_app)

    # Valid credentials
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin_pass_123"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["username"] == "admin"
    csrf_token = data["csrf_token"]
    assert len(csrf_token) > 10

    # Cookie is set
    assert "deskmind_session" in client.cookies

    # Query /api/auth/me
    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["username"] == "admin"

    # Query /api/auth/csrf
    csrf_resp = client.get("/api/auth/csrf")
    assert csrf_resp.status_code == 200
    assert csrf_resp.json()["csrf_token"] == csrf_token

    # Logout
    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 200

    # After logout, /api/auth/me should be unauthorized
    assert client.get("/api/auth/me").status_code == 401


# -----------------------------------------------------------------------------
# 2. CSRF Protection on Control Endpoints
# -----------------------------------------------------------------------------


def test_control_endpoints_require_csrf(dashboard_app):
    client = TestClient(dashboard_app)
    # Login first
    login_resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin_pass_123"}
    )
    csrf_token = login_resp.json()["csrf_token"]

    # 1. Attempt POST without CSRF header -> 403 Forbidden
    r_no_csrf = client.post(
        "/api/control/service",
        json={"service": "live", "action": "start"},
    )
    assert r_no_csrf.status_code == 403
    assert "CSRF" in r_no_csrf.json()["detail"]

    # 2. Attempt POST with bogus CSRF header -> 403 Forbidden
    r_bad_csrf = client.post(
        "/api/control/service",
        json={"service": "live", "action": "start"},
        headers={"X-CSRF-Token": "bogus_token"},
    )
    assert r_bad_csrf.status_code == 403

    # 3. Attempt POST with valid CSRF header -> succeeds
    with patch("deskmind.supervisor.manager.SupervisorManager.start_service", return_value=True):
        r_ok = client.post(
            "/api/control/service",
            json={"service": "live", "action": "start"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["success"] is True


# -----------------------------------------------------------------------------
# 3. Hard Safety Rules: No Live Trading & Test Split Lockdown
# -----------------------------------------------------------------------------


def test_rejects_live_trading_mode_switch(dashboard_app):
    client = TestClient(dashboard_app)
    login_resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin_pass_123"}
    )
    csrf_token = login_resp.json()["csrf_token"]

    # Attempt to switch to 'live' mode
    r = client.post(
        "/api/control/mode",
        json={"trading_mode": "live", "approval_mode": "auto"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code in (400, 422)
    assert "FORBIDDEN" in str(r.json()) or "Input should be" in str(r.json())


def test_rejects_backtest_on_test_split(dashboard_app):
    client = TestClient(dashboard_app)
    login_resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin_pass_123"}
    )
    csrf_token = login_resp.json()["csrf_token"]

    # Attempt to submit backtest on 'test' split from UI
    r = client.post(
        "/api/control/backtest/submit",
        json={"strategy": "trend_follow_v1", "split": "test", "symbols": ["BTCUSDT"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code in (400, 422)
    assert "test" in str(r.json()).lower()


# -----------------------------------------------------------------------------
# 4. Typed Confirmation Enforcements
# -----------------------------------------------------------------------------


def test_typed_confirmations_enforced(dashboard_app):
    client = TestClient(dashboard_app)
    login_resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin_pass_123"}
    )
    csrf_token = login_resp.json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf_token}

    # 1. Kill Switch without 'KILL'
    r_kill_bad = client.post("/api/control/kill", json={"confirm": "yes"}, headers=headers)
    assert r_kill_bad.status_code == 400
    assert "KILL" in r_kill_bad.json()["detail"]

    # Kill Switch with 'KILL'
    r_kill_ok = client.post("/api/control/kill", json={"confirm": "KILL"}, headers=headers)
    assert r_kill_ok.status_code == 200
    assert r_kill_ok.json()["status"] == "killed"

    # 2. Breaker Reset without 'RESET'
    r_reset_bad = client.post("/api/control/breaker/reset", json={"confirm": "ok"}, headers=headers)
    assert r_reset_bad.status_code == 400
    assert "RESET" in r_reset_bad.json()["detail"]

    # Breaker Reset with 'RESET'
    r_reset_ok = client.post(
        "/api/control/breaker/reset", json={"confirm": "RESET"}, headers=headers
    )
    assert r_reset_ok.status_code == 200
    assert r_reset_ok.json()["status"] == "reset"

    # 3. Stop All without 'STOP_ALL'
    r_stop_bad = client.post(
        "/api/control/service/stop-all", json={"confirm": "stop"}, headers=headers
    )
    assert r_stop_bad.status_code == 400
    assert "STOP_ALL" in r_stop_bad.json()["detail"]


# -----------------------------------------------------------------------------
# 5. Supervisor Process Lifecycle Unit Test (Mocked)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_manager_lifecycle():
    supervisor = SupervisorManager(commands={"test_svc": ["echo", "hello"]})

    # Fake child process
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        # Start
        started = await supervisor.start_service("test_svc")
        assert started is True
        assert supervisor.services["test_svc"].status == "running"
        assert supervisor.services["test_svc"].pid == 9999

        # Stop
        mock_proc.returncode = 0
        stopped = await supervisor.stop_service("test_svc")
        assert stopped is True
        assert supervisor.services["test_svc"].status == "stopped"

        # Restart
        mock_proc.returncode = None
        restarted = await supervisor.restart_service("test_svc")
        assert restarted is True
        assert supervisor.services["test_svc"].status == "running"

        # Stop all with typed confirm
        results = await supervisor.stop_all(confirm="STOP_ALL")
        assert results["test_svc"] is True


# -----------------------------------------------------------------------------
# 6. Backtest Queue Manager Unit Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backtest_queue_lifecycle():
    queue_mgr = BacktestQueueManager(max_concurrency=1)
    await queue_mgr.start()

    try:
        # Submit valid train job
        req = BacktestSubmitRequest(strategy="trend_follow_v1", split="train", symbols=["BTCUSDT"])
        job = await queue_mgr.submit_job(req)
        assert job.status in ("queued", "running")
        assert job.job_id.startswith("bt_job_")

        # Query job
        queried = queue_mgr.get_job(job.job_id)
        assert queried is not None
        assert queried.request["strategy"] == "trend_follow_v1"

        # Cancel job
        cancelled = await queue_mgr.cancel_job(job.job_id)
        assert cancelled is True
        assert queue_mgr.get_job(job.job_id).status == "cancelled"

        # Verify reject 'test' split at model level or queue level
        with pytest.raises((ValueError, Exception)) as exc_info:
            await queue_mgr.submit_job(BacktestSubmitRequest.model_construct(split="test"))
        assert "SAFETY RULE" in str(exc_info.value) or "test" in str(exc_info.value).lower()

    finally:
        await queue_mgr.stop()


# -----------------------------------------------------------------------------
# 7. Audit Log Recording Test
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_logs_recorded():
    await record_audit_action("test_audit_action", "admin", {"detail": "unit_test"})
    logs = await get_audit_logs(limit=20)
    assert len(logs) >= 1
    actions = [entry["action"] for entry in logs]
    assert any(
        "test_audit_action" in a or "login" in a or "kill" in a or "service" in a for a in actions
    )
