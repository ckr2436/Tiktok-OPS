# app/features/platform/router_auth.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_session
from app.core.errors import APIError
from app.core.security import (
    clear_session,
    client_ip,
    read_session_from_request,
    verify_password,
    write_session,
)
from app.data.db import get_db
from app.data.models.users import User
from app.data.models.workspaces import Workspace
from app.services.audit import log_event

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/auth",
    tags=["Platform / Auth"],
)

# =========================
# Schemas
# =========================

class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    remember: bool | None = None
    # 新增：可选 workspace_id（指定租户后直接在该租户下校验）
    workspace_id: int | None = Field(default=None, ge=1)

class SessionResponse(BaseModel):
    id: int
    email: str | None
    username: str
    display_name: str | None
    usercode: str | None
    is_platform_admin: bool
    workspace_id: int
    role: str
    is_active: bool

class DiscoverTenantsRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)

class TenantItem(BaseModel):
    workspace_id: int
    company_code: str
    company_name: str

class DiscoverTenantsResponse(BaseModel):
    items: list[TenantItem]

# =========================
# Helpers
# =========================

def _session_response_from_user(user: User) -> SessionResponse:
    return SessionResponse(
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

# =========================
# Endpoints
# =========================

@router.post("/login", response_model=SessionResponse)
def login(req: LoginRequest, resp: Response, http: Request, db: Session = Depends(get_db)):
    """
    登录策略：
    - 若提供 workspace_id：仅在该租户下按 username 精确查找并校验密码。
    - 若未提供 workspace_id：
        1) 找出所有租户下处于活跃态且用户名相同的用户；
        2) 若仅 1 个候选 → 直接校验该用户密码；
        3) 若 >=2 个候选：
           - 遍历校验密码，若“唯一命中” → 直接登录成功（体验最佳）；
           - 否则 → 返回 AUTH_FAILED（前端随后调用 /tenants/discover 引导选择租户再登录）。
    说明：返回 AUTH_FAILED 的语义保持稳定，不泄露“密码对错/是否多租户”的细节。
    """
    ip = client_ip(http)
    ua = http.headers.get("user-agent")

    # ----（A）指定 workspace 精确登录 ----
    if req.workspace_id:
        user = db.scalar(
            select(User).where(
                User.username == req.username,
                User.workspace_id == int(req.workspace_id),
                User.deleted_at.is_(None),
            )
        )
        if not user or not user.is_active or not verify_password(req.password, user.password_hash):
            # 统一返回，不暴露具体原因
            raise APIError("AUTH_FAILED", "Invalid credentials.", 401)

        write_session(resp, {"id": int(user.id)}, remember=req.remember)
        log_event(
            db,
            action="auth.login",
            resource_type="user",
            resource_id=int(user.id),
            actor_user_id=int(user.id),
            actor_workspace_id=int(user.workspace_id),
            actor_ip=ip,
            user_agent=ua,
            details={"username": user.username, "mode": "by_workspace"},
        )
        return _session_response_from_user(user)

    # ----（B）未指定 workspace → 同名跨租户处理 ----
    candidates: list[User] = list(
        db.execute(
            select(User)
            .where(
                User.username == req.username,
                User.deleted_at.is_(None),
            )
            .order_by(User.workspace_id.asc(), User.id.asc())
        ).scalars()
    )

    # 无此用户名
    if not candidates:
        raise APIError("AUTH_FAILED", "Invalid credentials.", 401)

    # 仅一个候选 → 直接校验
    if len(candidates) == 1:
        user = candidates[0]
        if not user.is_active or not verify_password(req.password, user.password_hash):
            raise APIError("AUTH_FAILED", "Invalid credentials.", 401)

        write_session(resp, {"id": int(user.id)}, remember=req.remember)
        log_event(
            db,
            action="auth.login",
            resource_type="user",
            resource_id=int(user.id),
            actor_user_id=int(user.id),
            actor_workspace_id=int(user.workspace_id),
            actor_ip=ip,
            user_agent=ua,
            details={"username": user.username, "mode": "single_candidate"},
        )
        return _session_response_from_user(user)

    # 多个候选：尝试“唯一密码命中”
    matched: list[User] = []
    for u in candidates:
        try:
            if u.is_active and verify_password(req.password, u.password_hash):
                matched.append(u)
                if len(matched) > 1:
                    break  # 超过 1 个即可提前结束
        except Exception:
            # 忽略单体异常，继续尝试下一个候选
            continue

    if len(matched) == 1:
        user = matched[0]
        write_session(resp, {"id": int(user.id)}, remember=req.remember)
        log_event(
            db,
            action="auth.login",
            resource_type="user",
            resource_id=int(user.id),
            actor_user_id=int(user.id),
            actor_workspace_id=int(user.workspace_id),
            actor_ip=ip,
            user_agent=ua,
            details={"username": user.username, "mode": "unique_password_match"},
        )
        return _session_response_from_user(user)

    # 0 个或多个命中 → 统一 AUTH_FAILED（前端再调 discover 引导选租户）
    log_event(
        db,
        action="auth.login_failed",
        resource_type="user",
        resource_id=None,
        actor_user_id=None,
        actor_workspace_id=None,
        actor_ip=ip,
        user_agent=ua,
        details={"username": req.username, "reason": "ambiguous_or_wrong_password"},
    )
    raise APIError("AUTH_FAILED", "Invalid credentials.", 401)


@router.post("/logout")
def logout(
    resp: Response,
    http: Request,
    me: SessionUser = Depends(require_session),
    db: Session = Depends(get_db),
):
    clear_session(resp)
    log_event(
        db,
        action="auth.logout",
        resource_type="user",
        resource_id=int(me.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(http),
        user_agent=http.headers.get("user-agent"),
    )
    return {"ok": True}


@router.get("/session", response_model=SessionResponse)
def session(
    _: dict | None = Depends(read_session_from_request),
    me: SessionUser = Depends(require_session),
):
    return _session_response_from_user(me)  # type: ignore[arg-type]


@router.post("/tenants/discover", response_model=DiscoverTenantsResponse)
def discover_tenants(req: DiscoverTenantsRequest, db: Session = Depends(get_db)):
    """
    按用户名发现其所在租户（不校验密码；仅返回最小必要信息）。
    - 永远返回固定结构，不暴露“是否存在/不存在”的差异细节（空数组表示无匹配）。
    """
    rows: list[tuple[int, str, str]] = []
    # 联表获取 workspace 基础信息（避免多次 round-trip）
    q = (
        select(User.workspace_id, Workspace.company_code, Workspace.name)
        .join(Workspace, Workspace.id == User.workspace_id)
        .where(User.username == req.username, User.deleted_at.is_(None))
        .order_by(User.workspace_id.asc())
    )
    for wid, code, name in db.execute(q).all():
        rows.append((int(wid), str(code), str(name)))

    items = [
        TenantItem(workspace_id=wid, company_code=code, company_name=name)
        for (wid, code, name) in rows
    ]
    return DiscoverTenantsResponse(items=items)

