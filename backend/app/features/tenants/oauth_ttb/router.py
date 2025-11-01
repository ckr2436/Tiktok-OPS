# app/features/tenants/oauth_ttb/router.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Query, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_tenant_admin, SessionUser  # 仅租户管理员可用
from app.core.errors import APIError
from app.core.security import client_ip
from app.data.db import get_db
from app.data.models.oauth_ttb import (
    OAuthProviderApp,
    OAuthAccountTTB,
)
from app.services.oauth_ttb import (
    create_authz_session,
    update_oauth_account_alias,
    revoke_oauth_account,
)
from app.services.ttb_sync_dispatch import SYNC_TASKS, dispatch_sync

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/oauth/tiktok-business",
    tags=["Tenant / TikTok Business OAuth"],
)

# ----------- 租户可读 Provider Apps（仅租户管理员；精简字段）-----------
class ProviderAppItem(BaseModel):
    id: int
    name: str
    client_id: str
    redirect_uri: str
    is_enabled: bool


class ProviderAppsResp(BaseModel):
    items: list[ProviderAppItem]


@router.get("/provider-apps", response_model=ProviderAppsResp)
def list_provider_apps_for_tenant(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),  # 仅管理员
    db: Session = Depends(get_db),
):
    rows = (
        db.execute(
            select(OAuthProviderApp)
            .where(
                OAuthProviderApp.provider == "tiktok_business",
                OAuthProviderApp.is_enabled.is_(True),
            )
            .order_by(OAuthProviderApp.id.asc())
        )
        .scalars()
        .all()
    )
    items = [
        ProviderAppItem(
            id=int(r.id),
            name=r.name,
            client_id=r.client_id,
            redirect_uri=r.redirect_uri,
            is_enabled=bool(r.is_enabled),
        )
        for r in rows
    ]
    return ProviderAppsResp(items=items)


# ----------- 发起授权（仅租户管理员）-----------
class AuthzCreateReq(BaseModel):
    provider_app_id: int = Field(gt=0)
    return_to: str | None = Field(default=None, max_length=512)
    alias: str | None = Field(default=None, max_length=128, description="为此次绑定起一个别名（可选）")


class AuthzCreateResp(BaseModel):
    state: str
    auth_url: str
    expires_at: str


@router.post("/authz", response_model=AuthzCreateResp)
def create_authz(
    workspace_id: int,
    req: AuthzCreateReq,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),  # 仅管理员
    db: Session = Depends(get_db),
):
    # 注意：第一个参数使用关键字方式传入，避免“positional after keyword”错误
    sess, url = create_authz_session(
        db=db,
        workspace_id=int(workspace_id),
        provider_app_id=int(req.provider_app_id),
        created_by_user_id=int(me.id),
        client_ip=client_ip(http),
        user_agent=http.headers.get("user-agent"),
        return_to=req.return_to,
        alias=req.alias,  # ★ 会话暂存别名
    )
    return AuthzCreateResp(
        state=sess.state,
        auth_url=url,
        expires_at=sess.expires_at.isoformat() if sess.expires_at else "",
    )


# ----------- 绑定列表（仅租户管理员）-----------
class BindingItem(BaseModel):
    auth_id: int
    provider_app_id: int
    alias: str | None
    status: str
    created_at: str


class BindingListResp(BaseModel):
    items: list[BindingItem]


@router.get("/bindings", response_model=BindingListResp)
def list_bindings(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),  # 仅管理员
    db: Session = Depends(get_db),
):
    accounts = (
        db.execute(
            select(OAuthAccountTTB)
            .where(
                OAuthAccountTTB.workspace_id == int(workspace_id),
                OAuthAccountTTB.status == "active",
            )
            .order_by(OAuthAccountTTB.id.asc())
        )
        .scalars()
        .all()
    )

    results: list[BindingItem] = []
    for acc in accounts:
        results.append(
            BindingItem(
                auth_id=int(acc.id),
                provider_app_id=int(acc.provider_app_id),
                alias=acc.alias,
                status=acc.status,
                created_at=acc.created_at.isoformat() if acc.created_at else "",
            )
        )
    return BindingListResp(items=results)


# ----------- 修改绑定别名（仅租户管理员）-----------
class AliasUpdateReq(BaseModel):
    alias: str | None = Field(default=None, max_length=128, description="为空或空白表示清空别名")


class AliasUpdateResp(BaseModel):
    auth_id: int
    alias: str | None


@router.patch("/bindings/{auth_id}/alias", response_model=AliasUpdateResp)
def update_alias(
    workspace_id: int,
    auth_id: int,
    req: AliasUpdateReq,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    acc = update_oauth_account_alias(
        db=db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        alias=req.alias,
        actor_user_id=int(me.id),
    )
    return AliasUpdateResp(auth_id=int(acc.id), alias=acc.alias)


# ----------- 绑定后触发同步（仅租户管理员）-----------
class BindingSyncReq(BaseModel):
    auth_id: int = Field(gt=0)
    scope: str | None = Field(default=None, description="bc|advertisers|stores|products|all")
    mode: str | None = Field(default=None, description="incremental|full")
    idempotency_key: str | None = Field(default=None, max_length=128)


class BindingSyncResp(BaseModel):
    run_id: int
    schedule_id: int
    task_name: str
    task_id: str | None
    status: str
    idempotent: bool = False


@router.post("/bind", response_model=BindingSyncResp, status_code=status.HTTP_202_ACCEPTED)
def trigger_binding_sync(
    workspace_id: int,
    req: BindingSyncReq,
    http: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    account = db.get(OAuthAccountTTB, int(req.auth_id))
    if not account or account.workspace_id != int(workspace_id):
        raise HTTPException(status_code=404, detail="binding not found")
    if account.status not in {"active", "invalid"}:
        raise HTTPException(status_code=400, detail=f"binding status {account.status} cannot be synced")

    scope = (req.scope or "all").lower()
    if scope not in SYNC_TASKS:
        raise HTTPException(status_code=400, detail="invalid scope")

    params = {
        "mode": (req.mode or "full"),
    }
    result = dispatch_sync(
        db,
        workspace_id=int(workspace_id),
        provider="tiktok-business",
        auth_id=int(account.id),
        scope=scope,
        params=params,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(http),
        idempotency_key=req.idempotency_key,
    )
    return BindingSyncResp(
        run_id=int(result.run.id),
        schedule_id=int(result.run.schedule_id),
        task_name=SYNC_TASKS[scope],
        task_id=result.task_id,
        status=result.status,
        idempotent=result.idempotent,
    )


# ----------- 取消授权（撤销长期令牌；仅租户管理员）-----------
class RevokeResp(BaseModel):
    removed_advertisers: int


@router.post("/bindings/{auth_id}/revoke", response_model=RevokeResp)
async def revoke_binding(
    workspace_id: int,
    auth_id: int,
    remote: bool = Query(True, description="是否同步撤销远端长期令牌；默认 True"),
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    # 保留此接口以便需要远端 revoke；本地仅做软撤销，不涉及广告主表
    result = await revoke_oauth_account(
        db=db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        remote=bool(remote),
    )
    return RevokeResp(**result)


# ----------- 硬删除（直接清库；仅租户管理员）-----------
class HardDeleteResp(BaseModel):
    removed_advertisers: int
    removed_accounts: int


@router.delete("/bindings/{auth_id}", response_model=HardDeleteResp)
def hard_delete_binding(
    workspace_id: int,
    auth_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc or acc.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "oauth account not found", 404)

    # 已移除广告主表
    del_advs = 0

    del_acc = db.execute(
        delete(OAuthAccountTTB).where(
            OAuthAccountTTB.id == int(auth_id),
            OAuthAccountTTB.workspace_id == int(workspace_id),
        )
    ).rowcount or 0

    return HardDeleteResp(removed_advertisers=int(del_advs), removed_accounts=int(del_acc))


@router.delete("/bindings", response_model=HardDeleteResp)
def hard_delete_all_bindings(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    # 已移除广告主表
    del_advs = 0

    del_acc = db.execute(
        delete(OAuthAccountTTB).where(OAuthAccountTTB.workspace_id == int(workspace_id))
    ).rowcount or 0

    return HardDeleteResp(removed_advertisers=int(del_advs), removed_accounts=int(del_acc))

