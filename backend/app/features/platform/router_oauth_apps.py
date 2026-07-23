# app/features/platform/router_oauth_apps.py
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_owner, require_platform_admin, SessionUser
from app.data.db import get_db
from app.services.oauth_ttb import upsert_provider_app, list_provider_apps, delete_provider_app

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/oauth/provider-apps",
    tags=["Platform / OAuth Provider Apps"],
)

class ProviderAppUpsertReq(BaseModel):
    provider: str = Field(description="tiktok_business or tiktok_shop")
    name: str = Field(min_length=2, max_length=128)
    client_id: str = Field(min_length=4, max_length=128)   # 统一对外叫 client_id
    client_secret: str | None = Field(default=None, max_length=512, description="创建时必填；更新时可空表示不变")
    service_id: str | None = Field(default=None, max_length=128)
    redirect_uri: str = Field(min_length=8, max_length=512)
    is_enabled: bool = True

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = (value or "").strip().replace("-", "_")
        if normalized not in {"tiktok_business", "tiktok_shop"}:
            raise ValueError("provider must be tiktok_business or tiktok_shop")
        return normalized

    @field_validator("service_id")
    @classmethod
    def normalize_service_id(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

class ProviderAppOut(BaseModel):
    id: int
    provider: str
    name: str
    client_id: str                      # 对外输出 client_id
    service_id: str | None = None
    redirect_uri: str
    is_enabled: bool
    client_secret_key_version: int
    updated_at: str | None


class ProviderAppDeleteOut(BaseModel):
    deleted_apps: int
    deleted_redirects: int
    deleted_auth_sessions: int
    deleted_tiktok_account_sessions: int
    deleted_tiktok_shop_sessions: int = 0

@router.get("", response_model=List[ProviderAppOut])
def list_apps(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台侧：列出已配置的 Provider Apps。
    服务层 list_provider_apps 返回的键为：
      id / provider / name / app_id / redirect_uri / is_enabled / app_secret_key_version / updated_at
    这里做一次字段名映射 -> ProviderAppOut。
    """
    items = list_provider_apps(db)
    out: list[ProviderAppOut] = []
    for it in items:
        out.append(
            ProviderAppOut(
                id=int(it["id"]),
                provider=str(it["provider"]),
                name=str(it["name"]),
                client_id=str(it["app_id"]),  # 映射 app_id -> client_id
                service_id=str(it["service_id"]) if it.get("service_id") else None,
                redirect_uri=str(it["redirect_uri"]),
                is_enabled=bool(it["is_enabled"]),
                client_secret_key_version=int(it["client_secret_key_version"]),  # 同名
                updated_at=str(it["updated_at"]) if it.get("updated_at") else None,
            )
        )
    return out

@router.post("", response_model=ProviderAppOut)
def upsert_app(
    req: ProviderAppUpsertReq,
    me: SessionUser = Depends(require_platform_owner),
    db: Session = Depends(get_db),
):
    """
    平台侧：创建/更新 Provider App。
    注意 upsert_provider_app 的入参名是 app_id / app_secret（不是 client_id / client_secret）。
    """
    row = upsert_provider_app(
        db=db,
        provider=req.provider,
        name=req.name,
        app_id=req.client_id,                 # 映射 client_id -> app_id
        app_secret=req.client_secret,         # 映射 client_secret -> app_secret
        redirect_uri=req.redirect_uri,
        is_enabled=bool(req.is_enabled),
        actor_user_id=int(me.id),
        service_id=req.service_id,
    )
    return ProviderAppOut(
        id=int(row.id),
        provider=row.provider,
        name=row.name,
        client_id=row.client_id,                                         # 直接输出 client_id
        service_id=row.service_id,
        redirect_uri=row.redirect_uri,
        is_enabled=bool(row.is_enabled),
        client_secret_key_version=int(row.client_secret_key_version),
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.delete("/{provider_app_id}", response_model=ProviderAppDeleteOut)
def delete_app(
    provider_app_id: int,
    _: SessionUser = Depends(require_platform_owner),
    db: Session = Depends(get_db),
):
    return delete_provider_app(db, provider_app_id=int(provider_app_id))
