"""Authentication, CSRF validation, session management, and live trading rejection.

Part of Dashboard Control API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from deskmind.config import Settings, get_settings
from deskmind.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE_NAME = "deskmind_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60.0
SESSION_MAX_AGE_SECONDS = 86400  # 24 hours


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool
    username: str
    csrf_token: str
    message: str = "Login successful"


class UserInfoResponse(BaseModel):
    username: str
    authenticated: bool
    mode: str = "testnet"


@dataclass
class SessionData:
    session_id: str
    username: str
    csrf_token: str
    created_at: float
    last_active: float


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 rounds."""
    if salt is None:
        salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    key_b64 = base64.b64encode(key).decode("ascii")
    return f"pbkdf2_sha256$100000${salt_b64}${key_b64}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Verify plain password against stored PBKDF2 hash, or fallback to dev password."""
    if not plain_password:
        # Never accept an empty password, even if a hash of "" was stored by mistake
        # (e.g. a password prompt that did not capture input on a Windows terminal).
        return False
    if not stored_hash:
        # Dev default if not configured in .env: allow 'admin' / 'admin123'
        return plain_password in ("admin", "admin123", "change-me")

    if stored_hash.startswith("pbkdf2_sha256$"):
        parts = stored_hash.split("$")
        if len(parts) == 4:
            _algo, rounds_str, salt_b64, key_b64 = parts
            rounds = int(rounds_str)
            salt = base64.b64decode(salt_b64)
            expected_key = base64.b64decode(key_b64)
            actual_key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, rounds)
            return hmac.compare_digest(actual_key, expected_key)

    # Direct comparison if plain string specified in dev config
    return hmac.compare_digest(plain_password, stored_hash)


class AuthManager:
    """Manages sessions, CSRF tokens, and login rate limiting."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._sessions: dict[str, SessionData] = {}
        self._failed_attempts: dict[str, list[float]] = defaultdict(list)

    def check_rate_limit(self, ip: str) -> bool:
        """Returns True if IP is within login rate limits, False if blocked."""
        now = time.time()
        cutoff = now - LOGIN_WINDOW_SECONDS
        self._failed_attempts[ip] = [t for t in self._failed_attempts[ip] if t > cutoff]
        return len(self._failed_attempts[ip]) < MAX_LOGIN_ATTEMPTS

    def record_failed_attempt(self, ip: str) -> None:
        self._failed_attempts[ip].append(time.time())

    def reset_rate_limit(self, ip: str) -> None:
        self._failed_attempts.pop(ip, None)

    def create_session(self, username: str) -> SessionData:
        now = time.time()
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session = SessionData(
            session_id=session_id,
            username=username,
            csrf_token=csrf_token,
            created_at=now,
            last_active=now,
        )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str | None) -> SessionData | None:
        if not session_id or session_id not in self._sessions:
            return None
        session = self._sessions[session_id]
        now = time.time()
        if now - session.created_at > SESSION_MAX_AGE_SECONDS:
            del self._sessions[session_id]
            return None
        session.last_active = now
        return session

    def delete_session(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def verify_csrf(self, session: SessionData, csrf_token: str | None) -> bool:
        if not csrf_token:
            return False
        return hmac.compare_digest(session.csrf_token, csrf_token)


_auth_manager: AuthManager | None = None


def get_auth_manager() -> AuthManager:
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = AuthManager()
    return _auth_manager


def reset_auth_manager() -> None:
    global _auth_manager
    _auth_manager = AuthManager()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


async def get_current_session(
    request: Request,
    auth_mgr: Annotated[AuthManager, Depends(get_auth_manager)],
) -> SessionData:
    """Dependency verifying session from cookie or Authorization header."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            session_id = auth_header[7:].strip()

    session = auth_mgr.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return session


async def require_csrf(
    request: Request,
    session: Annotated[SessionData, Depends(get_current_session)],
    auth_mgr: Annotated[AuthManager, Depends(get_auth_manager)],
    x_csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
) -> None:
    """Dependency verifying CSRF token for control endpoints."""
    if not auth_mgr.verify_csrf(session, x_csrf_token):
        log.warning(
            "csrf_verification_failed",
            username=session.username,
            path=request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )


def forbid_live_trading_inspection(payload: dict | str | None) -> None:
    """Hard check rejecting any intent or configuration implying live/mainnet trading."""
    if payload is None:
        return
    text = str(payload).lower()
    if "live" in text or "mainnet" in text or "production" in text:
        log.critical("SECURITY_ALERT_LIVE_TRADING_REJECTED", payload=payload)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "LIVE/MAINNET TRADING IS STRICTLY FORBIDDEN. Only 'paper' and 'testnet' permitted."
            ),
        )
