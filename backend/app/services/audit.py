# app/services/audit.py
from __future__ import annotations
from typing import Any, Optional
from sqlalchemy.orm import Session
from app.data.models.audit_logs import AuditLog

def log_event(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
    target_user_id: int | None = None,
    workspace_id: int | None = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    db.add(AuditLog(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        actor_ip=actor_ip,
        user_agent=user_agent,
        target_user_id=target_user_id,
        workspace_id=workspace_id,
        details=details or None,
    ))

