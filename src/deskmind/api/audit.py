"""Audit logging service for tracking all control dashboard actions in PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from deskmind.db.models import AuditLog
from deskmind.db.session import session_scope
from deskmind.logging import get_logger

log = get_logger(__name__)

# In-memory buffer for testing and DB offline fallback
_MEMORY_AUDIT_LOGS: list[dict[str, Any]] = []


async def record_audit_action(
    action: str,
    user: str,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    status: str = "success",
) -> dict[str, Any]:
    """Record a control action to audit_log table and memory buffer."""
    now_dt = datetime.now(UTC)
    payload = details or {}

    entry_dict = {
        "created_at": now_dt.isoformat(),
        "action": action,
        "user": user,
        "details": payload,
        "ip_address": ip_address,
        "status": status,
    }
    _MEMORY_AUDIT_LOGS.insert(0, entry_dict)
    if len(_MEMORY_AUDIT_LOGS) > 1000:
        _MEMORY_AUDIT_LOGS.pop()

    try:
        async with session_scope() as session:
            db_entry = AuditLog(
                created_at=now_dt,
                action=action,
                user=user,
                details=payload,
                ip_address=ip_address,
                status=status,
            )
            session.add(db_entry)
        log.info("audit_action_recorded", action=action, user=user, status=status)
    except Exception as e:
        log.warning("audit_db_persistence_failed", error=str(e), action=action)

    return entry_dict


async def get_audit_logs(
    limit: int = 100,
    action: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve audit logs from PostgreSQL with memory fallback."""
    try:
        async with session_scope() as session:
            stmt = select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            res = await session.execute(stmt)
            rows = res.scalars().all()
            if rows:
                return [
                    {
                        "id": r.id,
                        "created_at": r.created_at.isoformat(),
                        "action": r.action,
                        "user": r.user,
                        "details": r.details,
                        "ip_address": r.ip_address,
                        "status": r.status,
                    }
                    for r in rows
                ]
    except Exception as e:
        log.debug("audit_db_fetch_fallback", error=str(e))

    # Fallback to in-memory logs
    out = _MEMORY_AUDIT_LOGS
    if action:
        out = [log_entry for log_entry in out if log_entry["action"] == action]
    return out[:limit]
