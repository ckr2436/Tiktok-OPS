# app/features/platform/router_oauth_apps.py
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_owner, require_platform_admin, SessionUser
from app.data.db import get_db
from app.services.oauth_ttb import upsert_provider_app, list_provider_apps

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/oauth/provider-apps",
    tags=["Platform / OAuth Provider Apps"],
)

class ProviderAppUpsertReq(BaseModel):
    provider: str = Field(pattern="^tiktok_business$", description="固定 tiktok_business")
    name: str = Field(min_length=2, max_length=128)
    client_id: str = Field(min_length=4, max_length=128)   # 统一对外叫 client_id
    client_secret: str | None = Field(default=None, max_length=512, description="创建时必填；更新时可空表示不变")
    redirect_uri: str = Field(min_length=8, max_length=512)
    is_enabled: bool = True

class ProviderAppOut(BaseModel):
    id: int
    provider: str
    name: str
    client_id: str                      # 对外输出 client_id
    redirect_uri: str
    is_enabled: bool
    client_secret_key_version: int
    updated_at: str | None

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
    items = list_provider_apps(db, provider="tiktok_business")
    out: list[ProviderAppOut] = []
    for it in items:
        out.append(
            ProviderAppOut(
                id=int(it["id"]),
                provider=str(it["provider"]),
                name=str(it["name"]),
                client_id=str(it["app_id"]),  # 映射 app_id -> client_id
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
    )
    return ProviderAppOut(
        id=int(row.id),
        provider=row.provider,
        name=row.name,
        client_id=row.client_id,                                         # 直接输出 client_id
        redirect_uri=row.redirect_uri,
        is_enabled=bool(row.is_enabled),
        client_secret_key_version=int(row.client_secret_key_version),
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )

