# app/core/deps.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional
from fastapi import Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.errors import APIError
from app.core.security import read_session_from_request
from app.data.db import get_db
from app.data.models.users import User
from app.data.models.workspaces import Workspace

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ADMIN_ROLES = {ROLE_OWNER, ROLE_ADMIN}

@dataclass(frozen=True, slots=True)
class SessionUser:
    id: int
    email: Optional[str]
    username: str
    display_name: Optional[str]
    usercode: Optional[str]
    is_platform_admin: bool
    workspace_id: int
    role: str
    is_active: bool

    def as_dict(self) -> dict:
        return asdict(self)


def require_session(request: Request, db: Session = Depends(get_db)) -> SessionUser:
    data = read_session_from_request(request)
    if not data or not data.get("id"):
        raise APIError("AUTH_REQUIRED", "Authentication required.", 401)
    user = db.get(User, int(data["id"]))
    if not user or user.deleted_at is not None or not user.is_active:
        raise APIError("AUTH_REQUIRED", "Authentication required.", 401)
    return SessionUser(
        id=int(user.id),
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        usercode=user.usercode,
        is_platform_admin=bool(user.is_platform_admin),
        workspace_id=int(user.workspace_id),
        role=str(user.role),
        is_active=bool(user.is_active),
    )


def _is_platform_workspace(db: Session, workspace_id: int) -> bool:
    ws = db.scalar(select(Workspace).where(Workspace.id == workspace_id))
    return bool(ws and ws.company_code == "0000")


def require_platform_admin(me: SessionUser = Depends(require_session)) -> SessionUser:
    if not me.is_platform_admin:
        raise APIError("FORBIDDEN", "Platform admin required.", 403)
    return me


def require_platform_owner(me: SessionUser = Depends(require_session), db: Session = Depends(get_db)) -> SessionUser:
    if not me.is_platform_admin or me.role != ROLE_OWNER or not _is_platform_workspace(db, me.workspace_id):
        raise APIError("FORBIDDEN", "Platform owner required.", 403)
    return me


def require_tenant_member(
    workspace_id: int,
    me: SessionUser = Depends(require_session),
) -> SessionUser:
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)
    return me


def require_tenant_admin(
    workspace_id: int,
    me: SessionUser = Depends(require_session),
) -> SessionUser:
    if me.workspace_id != int(workspace_id) or me.role not in ADMIN_ROLES:
        raise APIError("FORBIDDEN", "Admin or owner role required.", 403)
    return me

