# app/features/tenants/users/router.py
from __future__ import annotations
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.core.config import settings
from app.core.errors import APIError
from app.core.deps import (
    require_tenant_admin,
    require_tenant_member,
    SessionUser,
    ROLE_OWNER,
    ROLE_ADMIN,
    ROLE_MEMBER,
)
from app.data.db import get_db
from app.data.models.workspaces import Workspace
from app.data.models.users import User
from app.services.accounts import (
    create_user,
    normalize_username_from_email,
    ensure_unique_username,
)
from app.services.audit import log_event

router = APIRouter(prefix=f"{settings.API_PREFIX}/tenants", tags=["Tenant / Users"])

# ----------------------------------------------------------------------
# 输出模型
# ----------------------------------------------------------------------
class TenantUserItem(BaseModel):
    id: int
    email: str
    username: str
    display_name: str | None
    role: str
    usercode: str
    is_active: bool

class TenantUserListResponse(BaseModel):
    items: List[TenantUserItem]
    total: int

def _to_item(u: User) -> TenantUserItem:
    return TenantUserItem(
        id=int(u.id),
        email=u.email,
        username=u.username,
        display_name=u.display_name,
        role=str(u.role),
        usercode=u.usercode,
        is_active=bool(u.is_active),
    )

# ----------------------------------------------------------------------
# 新增：工作区元信息（用于前端显示公司名称）
# GET /api/v1/tenants/{workspace_id}/meta
# ----------------------------------------------------------------------
class WorkspaceMetaOut(BaseModel):
    id: int
    name: str
    company_code: str

@router.get("/{workspace_id}/meta", response_model=WorkspaceMetaOut)
def get_workspace_meta(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)
    ws = db.get(Workspace, int(workspace_id))
    if not ws:
        raise APIError("NOT_FOUND", "Workspace not found.", 404)
    return WorkspaceMetaOut(id=int(ws.id), name=ws.name, company_code=ws.company_code)

# ----------------------------------------------------------------------
# 列表
# ----------------------------------------------------------------------
@router.get("/{workspace_id}/users", response_model=TenantUserListResponse)
def list_users(
    workspace_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
    q: str | None = None,
    page: int = 1,
    size: int = 20,
):
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    stmt = select(User).where(User.workspace_id == workspace_id, User.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_
        stmt = stmt.where(
            or_(
                User.username.like(like),
                User.email.like(like),
                User.usercode.like(like),
            )
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(User.role.desc(), User.id.asc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()

    return TenantUserListResponse(
        items=[_to_item(x) for x in rows],
        total=int(total or 0),
    )

@router.get("/{workspace_id}/users/{user_id}", response_model=TenantUserItem)
def get_tenant_user(
    workspace_id: int,
    user_id: int,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    u = db.get(User, int(user_id))
    if not u or u.deleted_at is not None or u.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "User not found.", 404)
    return _to_item(u)

# ----------------------------------------------------------------------
# 创建（owner / admin）
# ----------------------------------------------------------------------
class CreateTenantUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(pattern="^(admin|member)$")  # 只能创建 admin/member
    display_name: Optional[str] = Field(default=None, max_length=128)
    username: Optional[str] = Field(default=None, min_length=2, max_length=64)

class CreateTenantUserResponse(BaseModel):
    id: int
    usercode: str
    role: str

@router.post("/{workspace_id}/users", response_model=CreateTenantUserResponse)
def create_tenant_user(
    workspace_id: int,
    req: CreateTenantUserRequest,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    ws = db.get(Workspace, int(workspace_id))
    if not ws:
        raise APIError("NOT_FOUND", "Workspace not found.", 404)

    pref = req.username or normalize_username_from_email(req.email)
    uname = ensure_unique_username(db, int(ws.id), pref)

    try:
        u = create_user(
            db,
            workspace_id=int(ws.id),
            email=req.email,
            password=req.password,
            role=req.role,
            is_platform_admin=False,
            display_name=req.display_name,
            username=uname,
            created_by_user_id=int(me.id),
            company_code=ws.company_code,
        )
    except ValueError as e:
        if str(e) == "EMAIL_EXISTS":
            raise APIError("EMAIL_EXISTS", "Email already exists.", 409)
        raise

    log_event(
        db,
        action="tenant.create_user",
        resource_type="user",
        resource_id=int(u.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(u.id),
        workspace_id=int(ws.id),
        details={"role": req.role, "email": u.email, "username": u.username},
    )

    return CreateTenantUserResponse(id=int(u.id), usercode=u.usercode, role=str(u.role))

# ----------------------------------------------------------------------
# 更新（owner/admin）
# ----------------------------------------------------------------------
class UpdateTenantUserRequest(BaseModel):
    display_name: Optional[str | None] = Field(default=None, max_length=128)
    username: Optional[str | None] = Field(default=None, min_length=2, max_length=64)
    is_active: Optional[bool] = None
    role: Optional[str] = Field(default=None, pattern="^(admin|member)$")

class UpdateTenantUserResponse(TenantUserItem):
    pass

def _guard_update_permissions(me: SessionUser, target: User, req: UpdateTenantUserRequest):
    # 任何人都不能操作 owner
    if target.role == ROLE_OWNER:
        raise APIError("FORBIDDEN", "Cannot modify owner.", 403)
    # admin 仅能操作 member，且不能改 role
    if me.role == ROLE_ADMIN:
        if target.role != ROLE_MEMBER:
            raise APIError("FORBIDDEN", "Admin can only modify members.", 403)
        if req.role is not None:
            raise APIError("FORBIDDEN", "Admin cannot change roles.", 403)

def _apply_updates(
    db: Session,
    workspace_id: int,
    target: User,
    req: UpdateTenantUserRequest,
) -> Dict[str, Any]:
    changed: Dict[str, Any] = {}

    if req.role is not None and req.role != target.role:
        if req.role not in (ROLE_ADMIN, ROLE_MEMBER):
            raise APIError("INVALID_ROLE", "role must be 'admin' or 'member'.", 400)
        target.role = req.role
        changed["role"] = req.role

    if req.display_name is not None and req.display_name != target.display_name:
        target.display_name = req.display_name
        changed["display_name"] = req.display_name

    if req.username is not None:
        new_uname = req.username
        if new_uname:
            new_uname = ensure_unique_username(db, int(workspace_id), str(new_uname))
        if new_uname != target.username:
            target.username = new_uname
            changed["username"] = new_uname

    if req.is_active is not None and bool(req.is_active) != bool(target.is_active):
        target.is_active = bool(req.is_active)
        changed["is_active"] = bool(req.is_active)

    return changed

def _update_user(
    workspace_id: int,
    user_id: int,
    req: UpdateTenantUserRequest,
    http: Request,
    me: SessionUser,
    db: Session,
) -> UpdateTenantUserResponse:
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    target = db.get(User, int(user_id))
    if not target or target.deleted_at is not None or target.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "User not found.", 404)

    _guard_update_permissions(me, target, req)
    changed = _apply_updates(db, workspace_id, target, req)

    if not changed:
        return _to_item(target)  # type: ignore[return-value]

    db.add(target)

    log_event(
        db,
        action="tenant.update_user",
        resource_type="user",
        resource_id=int(target.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(target.id),
        workspace_id=int(workspace_id),
        details=changed,
    )
    db.refresh(target)

    return _to_item(target)  # type: ignore[return-value]

@router.patch("/{workspace_id}/users/{user_id}", response_model=UpdateTenantUserResponse)
def patch_tenant_user(
    workspace_id: int,
    user_id: int,
    req: UpdateTenantUserRequest,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    return _update_user(workspace_id, user_id, req, http, me, db)

@router.put("/{workspace_id}/users/{user_id}", response_model=UpdateTenantUserResponse)
def put_tenant_user(
    workspace_id: int,
    user_id: int,
    req: UpdateTenantUserRequest,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    return _update_user(workspace_id, user_id, req, http, me, db)

# ----------------------------------------------------------------------
# 删除（owner 可删 admin/member；admin 只能删 member）
# ----------------------------------------------------------------------
@router.delete("/{workspace_id}/users/{user_id}")
def delete_tenant_user(
    workspace_id: int,
    user_id: int,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    target = db.get(User, int(user_id))
    if not target or target.deleted_at is not None or target.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "User not found.", 404)

    if target.role == ROLE_OWNER:
        raise APIError("FORBIDDEN", "Cannot delete owner.", 403)
    if me.role == ROLE_ADMIN and target.role != ROLE_MEMBER:
        raise APIError("FORBIDDEN", "Admin can only delete members.", 403)

    from datetime import datetime, timezone
    target.deleted_at = datetime.now(timezone.utc)
    db.add(target)

    log_event(
        db,
        action="tenant.delete_user",
        resource_type="user",
        resource_id=int(target.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(target.id),
        workspace_id=int(workspace_id),
        details={"target_role": target.role, "target_email": target.email},
    )

    return {"ok": True}

# ----------------------------------------------------------------------
# 重置密码（owner 可对 admin/member；admin 仅可对 member）
# ----------------------------------------------------------------------
class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)

class ResetPasswordResponse(BaseModel):
    ok: bool

def _hash_password_or_fail(raw: str) -> str:
    # 与现有安全模块对齐，仅保留统一命名
    from app.core import security as sec  # type: ignore
    if hasattr(sec, "hash_password"):
        return sec.hash_password(raw)  # type: ignore[attr-defined]
    # 没有可用的哈希函数时，明确报错（避免明文入库）
    raise APIError("SERVER_MISCONFIG", "Password hasher is not configured.", 500)

@router.post("/{workspace_id}/users/{user_id}/reset_password", response_model=ResetPasswordResponse)
def reset_password(
    workspace_id: int,
    user_id: int,
    req: ResetPasswordRequest,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    if me.workspace_id != int(workspace_id):
        raise APIError("FORBIDDEN", "Not a member of this workspace.", 403)

    target = db.get(User, int(user_id))
    if not target or target.deleted_at is not None or target.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "User not found.", 404)

    # 权限：owner 对 admin/member；admin 仅对 member；任何人不能对 owner
    if target.role == ROLE_OWNER:
        raise APIError("FORBIDDEN", "Cannot reset password for owner.", 403)
    if me.role == ROLE_ADMIN and target.role != ROLE_MEMBER:
        raise APIError("FORBIDDEN", "Admin can only reset password for members.", 403)

    # 执行重置
    target.password_hash = _hash_password_or_fail(req.new_password)  # type: ignore[attr-defined]
    db.add(target)

    log_event(
        db,
        action="tenant.reset_password",
        resource_type="user",
        resource_id=int(target.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(target.id),
        workspace_id=int(workspace_id),
        details={"target_role": target.role, "target_email": target.email},
    )
    return ResetPasswordResponse(ok=True)

