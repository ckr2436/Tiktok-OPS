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

# ---- 角色常量 ----
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ADMIN_ROLES = {ROLE_OWNER, ROLE_ADMIN}


@dataclass(frozen=True, slots=True)
class SessionUser:
    """
    当前登录用户在应用层的会话视图。

    注意：
    - is_platform_admin=True 表示“平台账号”，只能访问 /platform 域接口；
    - 租户账号（workspace 下的 owner/admin/member）访问 /tenants/{workspace_id}/...。
    """
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


# ---- 基础会话 & 平台判定 ----
def require_session(request: Request, db: Session = Depends(get_db)) -> SessionUser:
    """
    从请求中读取会话信息，并加载对应 User。
    未登录、已删除、已停用 → 统一抛 AUTH_REQUIRED。
    """
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
    """
    判断 workspace 是否为平台 workspace（company_code == '0000'）。
    """
    ws = db.scalar(select(Workspace).where(Workspace.id == workspace_id))
    return bool(ws and ws.company_code == "0000")


# ---- 平台域权限 ----
def require_platform_admin(me: SessionUser = Depends(require_session)) -> SessionUser:
    """
    平台管理员（包括 owner / admin）。
    仅用于 /api/v1/platform/... 下的接口。
    """
    if not me.is_platform_admin:
        raise APIError("FORBIDDEN", "Platform admin required.", 403)
    return me


def require_platform_owner(
    me: SessionUser = Depends(require_session),
    db: Session = Depends(get_db),
) -> SessionUser:
    """
    平台 owner：is_platform_admin=True 且 当前 workspace 是平台 workspace('0000') 且角色为 owner。
    """
    if (
        not me.is_platform_admin
        or me.role != ROLE_OWNER
        or not _is_platform_workspace(db, me.workspace_id)
    ):
        raise APIError("FORBIDDEN", "Platform owner required.", 403)
    return me


# ---- 租户域权限 ----
def require_tenant_member(
    workspace_id: int,
    me: SessionUser = Depends(require_session),
) -> SessionUser:
    """
    租户成员（含 owner / admin / member）访问 /api/v1/tenants/{workspace_id}/...。

    规则：
    - 平台账号(is_platform_admin=True) 一律禁止访问租户域；
    - 必须是该 workspace 的成员。
    """
    # ★ 平台账号禁止访问任何 tenants 域接口
    if me.is_platform_admin:
        raise APIError(
            "FORBIDDEN",
            "Platform users cannot access tenant workspace APIs.",
            403,
        )

    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    return me


def require_tenant_admin(
    workspace_id: int,
    me: SessionUser = Depends(require_session),
) -> SessionUser:
    """
    租户管理员 / owner 访问 /api/v1/tenants/{workspace_id}/...。

    规则：
    - 平台账号(is_platform_admin=True) 一律禁止访问租户域；
    - 必须是该 workspace 的成员；
    - 角色必须在 {owner, admin}。
    """
    # ★ 平台账号禁止访问任何 tenants 域接口
    if me.is_platform_admin:
        raise APIError(
            "FORBIDDEN",
            "Platform users cannot access tenant workspace APIs.",
            403,
        )

    if me.workspace_id != int(workspace_id) or me.role not in ADMIN_ROLES:
        raise APIError("FORBIDDEN", "Admin or owner role required.", 403)

    return me

