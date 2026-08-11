from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin
from app.core.errors import APIError
from app.core.security import client_ip
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthProviderApp
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount, OAuthTikTokShopShop
from app.services.audit import log_event
from app.services.oauth_tiktok_shop import (
    create_authorization_session,
    disconnect_account,
    refresh_account_token,
    sync_authorized_shops,
)


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/oauth/tiktok-shop",
    tags=["Tenant / TikTok Shop OAuth"],
)


class ReadinessOut(BaseModel):
    configured: bool
    provider_app_id: int | None = None
    app_name: str | None = None
    app_key: str | None = None
    service_id: str | None = None
    redirect_uri: str | None = None
    authorization_region: str = "US"


@router.get("/readiness", response_model=ReadinessOut)
def readiness(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    app = db.scalar(
        select(OAuthProviderApp)
        .where(
            OAuthProviderApp.provider == "tiktok_shop",
            OAuthProviderApp.is_enabled.is_(True),
        )
        .order_by(OAuthProviderApp.id.asc())
    )
    if not app or not app.service_id:
        return ReadinessOut(configured=False)
    return ReadinessOut(
        configured=True,
        provider_app_id=int(app.id),
        app_name=app.name,
        app_key=app.client_id,
        service_id=app.service_id,
        redirect_uri=app.redirect_uri,
    )


class AuthzCreateIn(BaseModel):
    provider_app_id: int | None = Field(default=None, gt=0)
    return_to: str | None = Field(default=None, max_length=512)
    alias: str | None = Field(default=None, max_length=128)
    authorization_type: Literal["seller", "creator"] = "seller"


class AuthzCreateOut(BaseModel):
    state: str
    auth_url: str
    expires_at: str


@router.post("/authz", response_model=AuthzCreateOut)
def create_authz(
    workspace_id: int,
    payload: AuthzCreateIn,
    request: Request,
    response: Response,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    session, auth_url = create_authorization_session(
        db,
        workspace_id=int(workspace_id),
        provider_app_id=payload.provider_app_id,
        created_by_user_id=int(me.id),
        client_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        return_to=payload.return_to,
        alias=payload.alias,
        authorization_type=payload.authorization_type,
    )
    response.set_cookie(
        "gmv_tiktok_shop_oauth_state",
        session.state,
        max_age=min(int(getattr(settings, "OAUTH_SESSION_TTL_SECONDS", 3600)), 3600),
        path=str(getattr(settings, "TT_SHOP_CALLBACK_PATH", "/api/oauth/tiktok-shop/callback")),
        secure=bool(getattr(settings, "COOKIE_SECURE", True)),
        httponly=True,
        samesite=str(getattr(settings, "COOKIE_SAMESITE", "lax") or "lax"),
    )
    log_event(
        db,
        action="tiktok_shop.oauth_started",
        resource_type="oauth_tiktok_shop_session",
        resource_id=int(session.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        workspace_id=int(workspace_id),
        details={
            "provider_app_id": int(session.provider_app_id),
            "authorization_type": session.authorization_type,
        },
    )
    return AuthzCreateOut(
        state=session.state,
        auth_url=auth_url,
        expires_at=session.expires_at.isoformat(),
    )


class ShopOut(BaseModel):
    id: int
    shop_id: str
    shop_code: str | None = None
    shop_name: str | None = None
    region: str | None = None
    timezone_name: str
    seller_type: str | None = None
    status: str
    is_active: bool
    last_seen_at: str | None = None


class AccountOut(BaseModel):
    id: int
    provider_app_id: int
    alias: str | None = None
    seller_name: str | None = None
    user_type: int | None = None
    account_type: str
    content_posting_ready: bool
    open_id_masked: str
    status: str
    granted_scopes: list[str]
    expires_at: str | None = None
    refresh_expires_at: str | None = None
    last_synced_at: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: str
    shops: list[ShopOut]


class AccountListOut(BaseModel):
    items: list[AccountOut]


def _mask(value: str) -> str:
    value = str(value or "")
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def _shop_out(shop: OAuthTikTokShopShop) -> ShopOut:
    return ShopOut(
        id=int(shop.id),
        shop_id=shop.shop_id,
        shop_code=shop.shop_code,
        shop_name=shop.shop_name,
        region=shop.region,
        timezone_name=shop.timezone_name,
        seller_type=shop.seller_type,
        status=shop.status,
        is_active=bool(shop.is_active),
        last_seen_at=shop.last_seen_at.isoformat() if shop.last_seen_at else None,
    )


@router.get("/accounts", response_model=AccountListOut)
def list_accounts(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    accounts = db.execute(
        select(OAuthTikTokShopAccount)
        .where(OAuthTikTokShopAccount.workspace_id == int(workspace_id))
        .order_by(OAuthTikTokShopAccount.created_at.desc())
    ).scalars().all()
    account_ids = [int(account.id) for account in accounts]
    shops_by_account: dict[int, list[OAuthTikTokShopShop]] = {account_id: [] for account_id in account_ids}
    if account_ids:
        shops = db.execute(
            select(OAuthTikTokShopShop)
            .where(OAuthTikTokShopShop.account_id.in_(account_ids))
            .order_by(OAuthTikTokShopShop.is_active.desc(), OAuthTikTokShopShop.shop_name.asc())
        ).scalars().all()
        for shop in shops:
            shops_by_account.setdefault(int(shop.account_id), []).append(shop)
    return AccountListOut(
        items=[
            AccountOut(
                id=int(account.id),
                provider_app_id=int(account.provider_app_id),
                alias=account.alias,
                seller_name=account.seller_name,
                user_type=account.user_type,
                account_type="creator" if account.user_type == 1 else "seller",
                content_posting_ready=(
                    account.status == "active"
                    and account.user_type == 1
                    and "creator.video.write" in set(account.granted_scopes_json or [])
                ),
                open_id_masked=_mask(account.open_id),
                status=account.status,
                granted_scopes=list(account.granted_scopes_json or []),
                expires_at=account.expires_at.isoformat() if account.expires_at else None,
                refresh_expires_at=(
                    account.refresh_expires_at.isoformat() if account.refresh_expires_at else None
                ),
                last_synced_at=account.last_synced_at.isoformat() if account.last_synced_at else None,
                last_error_code=account.last_error_code,
                last_error_message=account.last_error_message,
                created_at=account.created_at.isoformat(),
                shops=[_shop_out(shop) for shop in shops_by_account.get(int(account.id), [])],
            )
            for account in accounts
        ]
    )


class AccountActionOut(BaseModel):
    account_id: int
    status: str
    expires_at: str | None = None
    shops: list[ShopOut] = []


@router.post("/accounts/{account_id}/refresh", response_model=AccountActionOut)
async def refresh_token(
    workspace_id: int,
    account_id: int,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    account = await refresh_account_token(
        db,
        workspace_id=int(workspace_id),
        account_id=int(account_id),
        force=True,
    )
    log_event(
        db,
        action="tiktok_shop.token_refreshed",
        resource_type="oauth_tiktok_shop_account",
        resource_id=int(account.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
    )
    return AccountActionOut(
        account_id=int(account.id),
        status=account.status,
        expires_at=account.expires_at.isoformat() if account.expires_at else None,
    )


@router.post("/accounts/{account_id}/sync", response_model=AccountActionOut)
async def sync_shops(
    workspace_id: int,
    account_id: int,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    account = db.get(OAuthTikTokShopAccount, int(account_id))
    if not account or int(account.workspace_id) != int(workspace_id):
        raise APIError("NOT_FOUND", "TikTok Shop authorization not found.", 404)
    if account.user_type == 1:
        raise APIError(
            "CREATOR_ACCOUNT_NO_SHOP_SYNC",
            "Creator authorization does not expose seller shop synchronization.",
            409,
        )
    shops = await sync_authorized_shops(
        db,
        workspace_id=int(workspace_id),
        account_id=int(account_id),
    )
    log_event(
        db,
        action="tiktok_shop.shops_synced",
        resource_type="oauth_tiktok_shop_account",
        resource_id=int(account.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
        details={"shop_count": len(shops)},
    )
    return AccountActionOut(
        account_id=int(account.id),
        status=account.status,
        expires_at=account.expires_at.isoformat() if account.expires_at else None,
        shops=[_shop_out(shop) for shop in shops],
    )


@router.post("/accounts/{account_id}/disconnect", response_model=AccountActionOut)
def disconnect(
    workspace_id: int,
    account_id: int,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    account = disconnect_account(
        db,
        workspace_id=int(workspace_id),
        account_id=int(account_id),
    )
    log_event(
        db,
        action="tiktok_shop.authorization_disconnected",
        resource_type="oauth_tiktok_shop_account",
        resource_id=int(account.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
    )
    return AccountActionOut(account_id=int(account.id), status=account.status)


@router.delete("/accounts/{account_id}", response_model=AccountActionOut)
def delete_account(
    workspace_id: int,
    account_id: int,
    request: Request,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    account = db.get(OAuthTikTokShopAccount, int(account_id))
    if not account or int(account.workspace_id) != int(workspace_id):
        raise APIError("NOT_FOUND", "TikTok Shop authorization not found.", 404)
    if account.status == "active":
        raise APIError("DISCONNECT_REQUIRED", "Disconnect the authorization before deleting it.", 409)
    db.execute(delete(OAuthTikTokShopShop).where(OAuthTikTokShopShop.account_id == int(account.id)))
    db.delete(account)
    log_event(
        db,
        action="tiktok_shop.authorization_deleted",
        resource_type="oauth_tiktok_shop_account",
        resource_id=int(account_id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        workspace_id=int(workspace_id),
    )
    return AccountActionOut(account_id=int(account_id), status="deleted")
