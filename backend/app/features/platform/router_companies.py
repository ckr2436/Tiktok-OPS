# app/features/platform/router_companies.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.core.config import settings
from app.core.errors import APIError
from app.core.deps import require_platform_admin, require_platform_owner, SessionUser
from app.data.db import get_db
from app.data.models.workspaces import Workspace
from app.data.models.users import User
from app.services.accounts import alloc_company_code, create_user, normalize_username_from_email, ensure_unique_username
from app.services.audit import log_event

router = APIRouter(prefix=f"{settings.API_PREFIX}/platform/companies", tags=["Platform / Companies"])

class CreateCompanyOwner(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=128)
    username: Optional[str] = Field(default=None, min_length=2, max_length=64)

class CreateCompanyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    company_code: Optional[str] = Field(default=None)  # 留空自动分配
    owner: CreateCompanyOwner

class CreateCompanyResponse(BaseModel):
    workspace_id: int
    company_code: str
    owner_user_id: int
    owner_usercode: str

@router.post("", response_model=CreateCompanyResponse)
def create_company(req: CreateCompanyRequest, http: Request,
                   me: SessionUser = Depends(require_platform_admin),
                   db: Session = Depends(get_db)):
    if req.company_code:
        if req.company_code == "0000" or not (len(req.company_code) == 4 and req.company_code.isdigit()):
            raise APIError("INVALID_COMPANY_CODE", "company_code must be 4 digits and not '0000'.", 400)
        # 检查唯一
        exists = db.scalar(select(func.count()).select_from(Workspace).where(Workspace.company_code == req.company_code))
        if exists:
            raise APIError("COMPANY_CODE_EXISTS", "company_code already exists.", 409)
        code = req.company_code
    else:
        code = alloc_company_code(db)

    ws = Workspace(name=req.name, company_code=code)
    db.add(ws)
    db.flush()

    # owner email 全局唯一
    from sqlalchemy import select as S, func as F
    if db.scalar(S(F.count()).select_from(User).where(User.email == req.owner.email, User.deleted_at.is_(None))):
        raise APIError("EMAIL_EXISTS", "Owner email already exists.", 409)

    pref = req.owner.username or normalize_username_from_email(req.owner.email)
    uname = ensure_unique_username(db, int(ws.id), pref)

    u = create_user(
        db,
        workspace_id=int(ws.id),
        email=req.owner.email,
        password=req.owner.password,
        role="owner",
        is_platform_admin=False,
        display_name=req.owner.display_name,
        username=uname,
        created_by_user_id=int(me.id),
        company_code=ws.company_code,
    )

    log_event(
        db,
        action="platform.create_company",
        resource_type="workspace",
        resource_id=int(ws.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        target_user_id=int(u.id),
        workspace_id=int(ws.id),
        details={"name": ws.name, "company_code": ws.company_code, "owner_email": u.email},
    )

    return CreateCompanyResponse(
        workspace_id=int(ws.id),
        company_code=ws.company_code,
        owner_user_id=int(u.id),
        owner_usercode=u.usercode,
    )

class CompanyItem(BaseModel):
    id: int
    name: str
    company_code: str
    members: int
    owner_email: str | None

class CompanyListResponse(BaseModel):
    items: List[CompanyItem]
    total: int

@router.get("", response_model=CompanyListResponse)
def list_companies(me: SessionUser = Depends(require_platform_admin),
                   db: Session = Depends(get_db), q: str | None = None, page: int = 1, size: int = 20):
    stmt = select(Workspace)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Workspace.name.like(like) | (Workspace.company_code.like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(Workspace.id.asc()).offset((page-1)*size).limit(size)).scalars().all()

    items: list[CompanyItem] = []
    for ws in rows:
        members = db.scalar(select(func.count()).select_from(User).where(User.workspace_id == ws.id, User.deleted_at.is_(None))) or 0
        owner = db.scalar(select(User).where(User.workspace_id == ws.id, User.role == "owner", User.deleted_at.is_(None)))
        items.append(CompanyItem(
            id=int(ws.id), name=ws.name, company_code=ws.company_code, members=int(members), owner_email=owner.email if owner else None
        ))

    return CompanyListResponse(items=items, total=int(total or 0))

@router.delete("/{workspace_id}")
def delete_company(workspace_id: int, http: Request,
                   me: SessionUser = Depends(require_platform_owner),
                   db: Session = Depends(get_db)):
    ws = db.get(Workspace, int(workspace_id))
    if not ws:
        raise APIError("NOT_FOUND", "Workspace not found.", 404)
    if ws.company_code == "0000":
        raise APIError("FORBIDDEN", "Cannot delete platform workspace.", 403)

    # 软删所有用户（deleted_at），再删除工作区
    from datetime import datetime, timezone
    users = db.execute(select(User).where(User.workspace_id == ws.id, User.deleted_at.is_(None))).scalars().all()
    for u in users:
        u.deleted_at = datetime.now(timezone.utc)
        db.add(u)
    db.flush()
    db.delete(ws)

    log_event(
        db,
        action="platform.delete_company",
        resource_type="workspace",
        resource_id=int(workspace_id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(workspace_id),
        details={"company_code": ws.company_code, "name": ws.name},
    )
    return {"ok": True}

