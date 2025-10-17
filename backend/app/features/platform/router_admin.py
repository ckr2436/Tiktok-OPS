# app/features/platform/router_admin.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.core.config import settings
from app.core.errors import APIError
from app.core.deps import require_platform_admin, require_platform_owner, SessionUser
from app.data.db import get_db
from app.data.models.workspaces import Workspace
from app.data.models.users import User
from app.services.accounts import (
    ensure_platform_workspace,
    create_user,
    normalize_username_from_email,
    ensure_unique_username,
)
from app.services.audit import log_event

router = APIRouter(prefix=f"{settings.API_PREFIX}/platform/admin", tags=["Platform / Admin"])

# ---- 存量检测 & 初始化 ----
class ExistsResponse(BaseModel):
    exists: bool

@router.get("/exists", response_model=ExistsResponse)
def platform_admin_exists(db: Session = Depends(get_db)):
    n = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.is_platform_admin.is_(True), User.deleted_at.is_(None))
    )
    return ExistsResponse(exists=bool(n and n > 0))


class InitAdminRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=128)
    username: Optional[str] = Field(default=None, min_length=2, max_length=64)

class InitAdminResponse(BaseModel):
    id: int
    workspace_id: int
    role: str
    is_platform_admin: bool
    usercode: str

@router.post("/init", response_model=InitAdminResponse)
def init_platform_owner(req: InitAdminRequest, http: Request, db: Session = Depends(get_db)):
    # 已存在平台管理员则拒绝
    exists = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.is_platform_admin.is_(True), User.deleted_at.is_(None))
    )
    if exists and exists > 0:
        raise APIError("ALREADY_INITIALIZED", "Platform admin already initialized.", 409)

    ws = ensure_platform_workspace(db)
    # 生成 username（可传入；缺省从 email 提取，本工作区内去重）
    pref = req.username or normalize_username_from_email(req.email)
    uname = ensure_unique_username(db, ws.id, pref)

    u = create_user(
        db,
        workspace_id=int(ws.id),
        email=req.email,
        password=req.password,
        role="owner",
        is_platform_admin=True,
        display_name=req.display_name,
        username=uname,
        created_by_user_id=None,
        company_code=ws.company_code,
    )

    log_event(
        db,
        action="platform.init_owner",
        resource_type="user",
        resource_id=int(u.id),
        actor_ip=None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(ws.id),
        target_user_id=int(u.id),
        details={"email": u.email, "username": u.username},
    )

    return InitAdminResponse(
        id=int(u.id),
        workspace_id=int(u.workspace_id),
        role=str(u.role),
        is_platform_admin=bool(u.is_platform_admin),
        usercode=u.usercode,
    )

# ---- 平台 Admin 管理（仅 owner 可操作）----
class CreatePlatformAdminRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=128)
    username: Optional[str] = Field(default=None, min_length=2, max_length=64)

class PlatformAdminOut(BaseModel):
    id: int
    email: str
    username: str
    display_name: str | None
    role: str
    is_platform_admin: bool
    workspace_id: int
    usercode: str

@router.post("/admins", response_model=PlatformAdminOut)
def create_platform_admin(
    req: CreatePlatformAdminRequest,
    http: Request,
    me: SessionUser = Depends(require_platform_owner),
    db: Session = Depends(get_db),
):
    ws = db.scalar(select(Workspace).where(Workspace.company_code == "0000"))
    if not ws:
        ws = ensure_platform_workspace(db)

    uname = (req.username or normalize_username_from_email(req.email))
    uname = ensure_unique_username(db, int(ws.id), uname)

    try:
        u = create_user(
            db,
            workspace_id=int(ws.id),
            email=req.email,
            password=req.password,
            role="admin",
            is_platform_admin=True,
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
        action="platform.create_admin",
        resource_type="user",
        resource_id=int(u.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(u.id),
        workspace_id=int(ws.id),
        details={"email": u.email, "username": u.username},
    )

    return PlatformAdminOut(
        id=int(u.id),
        email=u.email,
        username=u.username,
        display_name=u.display_name,
        role=str(u.role),
        is_platform_admin=bool(u.is_platform_admin),
        workspace_id=int(u.workspace_id),
        usercode=u.usercode,
    )

# ✅ 新增：更新平台管理员（目前支持修改 display_name；允许置空）
class UpdatePlatformAdminRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=128)

@router.patch("/admins/{user_id}", response_model=PlatformAdminOut)
@router.put("/admins/{user_id}", response_model=PlatformAdminOut)
def update_platform_admin(
    user_id: int,
    req: UpdatePlatformAdminRequest,
    http: Request,
    me: SessionUser = Depends(require_platform_owner),
    db: Session = Depends(get_db),
):
    u = db.get(User, int(user_id))
    if not u or u.deleted_at is not None:
        raise APIError("NOT_FOUND", "User not found.", 404)

    # 必须在平台工作区 & 是平台管理员（owner/admin 均可修改展示名）
    ws = db.scalar(select(Workspace).where(Workspace.id == u.workspace_id))
    if not ws or ws.company_code != "0000" or not u.is_platform_admin:
        raise APIError("FORBIDDEN", "Only platform admins in platform workspace can be updated.", 403)

    # 仅处理 display_name
    u.display_name = req.display_name
    db.add(u)

    log_event(
        db,
        action="platform.update_admin",
        resource_type="user",
        resource_id=int(u.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(u.id),
        workspace_id=int(ws.id),
        details={"display_name": u.display_name},
    )
    db.refresh(u)

    return PlatformAdminOut(
        id=int(u.id),
        email=u.email,
        username=u.username,
        display_name=u.display_name,
        role=str(u.role),
        is_platform_admin=bool(u.is_platform_admin),
        workspace_id=int(u.workspace_id),
        usercode=u.usercode,
    )

@router.delete("/admins/{user_id}")
def delete_platform_admin(
    user_id: int,
    http: Request,
    me: SessionUser = Depends(require_platform_owner),
    db: Session = Depends(get_db),
):
    u = db.get(User, int(user_id))
    if not u or u.deleted_at is not None:
        raise APIError("NOT_FOUND", "User not found.", 404)
    # 必须在平台工作区 & 是平台 admin；owner 不允许删
    ws = db.scalar(select(Workspace).where(Workspace.id == u.workspace_id))
    if not ws or ws.company_code != "0000" or not u.is_platform_admin or u.role != "admin":
        raise APIError("FORBIDDEN", "Only platform admin (non-owner) can be deleted.", 403)

    from datetime import datetime, timezone
    u.deleted_at = datetime.now(timezone.utc)
    db.add(u)

    log_event(
        db,
        action="platform.delete_admin",
        resource_type="user",
        resource_id=int(u.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(u.id),
        workspace_id=int(ws.id),
        details={"email": u.email, "username": u.username},
    )
    return {"ok": True}

class AdminListItem(BaseModel):
    id: int
    email: str
    username: str
    display_name: str | None
    role: str
    usercode: str
    is_platform_admin: bool

class AdminListResponse(BaseModel):
    items: List[AdminListItem]
    total: int

@router.get("/admins", response_model=AdminListResponse)
def list_platform_admins(
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
    q: str | None = None,
    page: int = 1,
    size: int = 20,
):
    ws = db.scalar(select(Workspace).where(Workspace.company_code == "0000"))
    if not ws:
        return AdminListResponse(items=[], total=0)

    stmt = select(User).where(
        User.workspace_id == ws.id,
        User.deleted_at.is_(None),
        User.is_platform_admin.is_(True),
    )
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_
        stmt = stmt.where(or_(User.username.like(like), User.email.like(like), User.usercode.like(like)))

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    stmt = stmt.order_by(User.role.desc(), User.id.asc()).offset((page - 1) * size).limit(size)
    rows = db.execute(stmt).scalars().all()

    items = [
        AdminListItem(
            id=int(x.id),
            email=x.email,
            username=x.username,
            display_name=x.display_name,
            role=str(x.role),
            usercode=x.usercode,
            is_platform_admin=bool(x.is_platform_admin),
        )
        for x in rows
    ]

    return AdminListResponse(items=items, total=int(total or 0))

