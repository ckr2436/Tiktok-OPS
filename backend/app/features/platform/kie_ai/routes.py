# app/features/platform/kie_ai/routes.py
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_admin, SessionUser
from app.data.db import get_db
from app.services.kie_api.accounts import (
    list_keys,
    get_key_by_id,
    get_default_key,
    create_kie_key,
    update_kie_key,
    deactivate_kie_key,
)
from app.services.kie_api.common import get_remaining_credits_for_key
from app.services.kie_api.sora2 import KieApiError

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/kie-ai",
    tags=["Platform / Kie AI"],
)

logger = logging.getLogger(__name__)


class KieKeyOut(BaseModel):
    id: int
    name: str
    provider_key: str
    is_active: bool
    is_default: bool

    class Config:
        from_attributes = True


class KieKeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field(..., min_length=1)
    is_default: bool = Field(False)


class KieKeyUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    api_key: Optional[str] = Field(None, min_length=1)
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


@router.get("/keys", response_model=List[KieKeyOut])
def list_kie_keys(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：列出所有 KIE API keys。
    """
    keys = list_keys(db)
    return keys


@router.post("/keys", response_model=KieKeyOut, status_code=status.HTTP_201_CREATED)
def create_key(
    payload: KieKeyCreateIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：创建 KIE API key。
    """
    key = create_kie_key(
        db,
        name=payload.name,
        api_key_plaintext=payload.api_key,
        is_default=payload.is_default,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )
    return key


@router.patch("/keys/{key_id}", response_model=KieKeyOut)
def update_key(
    key_id: int,
    payload: KieKeyUpdateIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：更新 KIE API key（名称 / 启用 / 默认 / key 本身）。
    """
    key = get_key_by_id(db, key_id=key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")

    key = update_kie_key(
        db,
        key=key,
        name=payload.name,
        api_key_plaintext=payload.api_key,
        is_active=payload.is_active,
        is_default=payload.is_default,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )
    return key


@router.delete("/keys/{key_id}", response_model=KieKeyOut)
def deactivate_key(
    key_id: int,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：停用某个 KIE API key（不物理删除）。
    """
    key = get_key_by_id(db, key_id=key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")

    key = deactivate_kie_key(
        db,
        key=key,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )
    return key


# ★ 先声明“默认 key”这条，让它优先匹配 /keys/default/credit
@router.get("/keys/default/credit", response_model=int)
async def get_default_key_credit(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：查询“默认 key”的当前余额。
    """
    key = get_default_key(db, require_active=True)
    if key is None:
        raise HTTPException(status_code=404, detail="Default key not found")

    try:
        credits = await get_remaining_credits_for_key(
            db,
            key_id=int(key.id),
            actor_user_id=None,
            actor_workspace_id=None,
        )
        return credits
    except (KieApiError, ValueError) as exc:
        logger.exception(
            "get_default_key_credit failed",
            extra={"key_id": int(key.id)},
        )
        # 前端 alert 里看到的就是这个 detail
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch credits for key: {exc}",
        ) from exc


@router.get("/keys/{key_id}/credit", response_model=int)
async def get_key_credit(
    key_id: int,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：查询指定 key 的当前余额。
    """
    key = get_key_by_id(db, key_id=key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")

    try:
        credits = await get_remaining_credits_for_key(
            db,
            key_id=int(key.id),
            actor_user_id=None,
            actor_workspace_id=None,
        )
        return credits
    except (KieApiError, ValueError) as exc:
        logger.exception(
            "get_key_credit failed",
            extra={"key_id": int(key.id)},
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch credits for key: {exc}",
        ) from exc

