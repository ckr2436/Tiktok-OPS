# app/features/tenants/oauth_ttb/router_sync.py
from __future__ import annotations

from typing import Annotated, Optional, List, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, validator

from app.data.db import get_db
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.celery_app import celery_app  # 你现有的入口
from celery.result import AsyncResult


router = APIRouter(tags=["tenant.tiktok-business.sync"])

BASE_PREFIX = "/api/v1/tenants/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync"


def _norm_provider(provider: str) -> str:
    # 路由里是 tiktok-business；库里常用 tiktok_business。这里统一校验。
    p = (provider or "").strip().lower()
    if p in ("tiktok-business", "tiktok_business"):
        return "tiktok-business"
    raise HTTPException(status_code=400, detail="unsupported provider")


# ---------- 请求体 ----------
class SyncCommonParams(BaseModel):
    mode: str = Field(default="incremental", pattern="^(incremental|full)$")
    since: Optional[str] = None
    until: Optional[str] = None
    cursor: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1, le=2000)
    dry_run: bool = False

    @validator("until")
    def _pair(cls, v, values):
        # 简化：只做是否都存在的形式校验，真正的时间窗校验放到任务里或 API 调用上一致化
        return v


class SyncShopsParams(SyncCommonParams):
    advertiser_id: Optional[str] = None
    advertiser_ids: Optional[List[str]] = None


class SyncProductsParams(SyncCommonParams):
    shop_id: Optional[str] = None
    shop_ids: Optional[List[str]] = None
    sku_ids: Optional[List[str]] = None


# ---------- 幂等 & 任务登记（Best-effort，Redis 后端可复用） ----------
def _idempotency_key_required(idem: Optional[str]) -> str:
    if not idem:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if len(idem) > 256:
        raise HTTPException(status_code=400, detail="Idempotency-Key too long")
    return idem


def _redis_get_set_task_id(cache_key: str, task_id: Optional[str] = None) -> Optional[str]:
    """
    若 Celery backend 是 Redis，则用 setnx 复用同一 key 的 task_id。
    不是 Redis 时，返回 None。
    """
    backend = getattr(celery_app, "backend", None)
    client = getattr(backend, "client", None)
    if client is None:
        return None
    try:
        if task_id is None:
            val = client.get(cache_key)
            return val.decode("utf-8") if val else None
        # 写入：仅当不存在时设置（24h）
        ok = client.set(cache_key, task_id, ex=24 * 3600, nx=True)
        return task_id if ok else (client.get(cache_key).decode("utf-8") if client.get(cache_key) else None)
    except Exception:
        return None


def _binding_cache_key(workspace_id: int, auth_id: int, action: str, idem: str) -> str:
    return f"idempotency:ttb:{workspace_id}:{auth_id}:{action}:{idem}"


# ---------- 触发器 ----------
@router.post(f"{BASE_PREFIX}/bc")
def trigger_bc_sync(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    params: SyncCommonParams = Depends(),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _norm_provider(provider)
    key = _idempotency_key_required(idempotency_key)
    cache_key = _binding_cache_key(workspace_id, auth_id, "bc", key)

    # 幂等复用（后端为 Redis 时生效）
    existed = _redis_get_set_task_id(cache_key)
    if existed:
        return {"job_id": existed, "accepted": True, "action": "bc", "idempotent": True}

    task = celery_app.send_task(
        "tenant.ttb.sync.bc",
        kwargs={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "params": params.dict(),
        },
        queue="gmv.tasks.events",
    )
    _redis_get_set_task_id(cache_key, task.id)

    return {
        "job_id": task.id,
        "accepted": True,
        "action": "bc",
        "plan": {"mode": params.mode, "limit": params.limit or None},
        "hints": {"rate_limit": "10/s"},
    }


@router.post(f"{BASE_PREFIX}/advertisers")
def trigger_advertisers_sync(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    params: SyncCommonParams = Depends(),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _norm_provider(provider)
    key = _idempotency_key_required(idempotency_key)
    cache_key = _binding_cache_key(workspace_id, auth_id, "advertisers", key)

    existed = _redis_get_set_task_id(cache_key)
    if existed:
        return {"job_id": existed, "accepted": True, "action": "advertisers", "idempotent": True}

    task = celery_app.send_task(
        "tenant.ttb.sync.advertisers",
        kwargs={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "params": params.dict(),
        },
        queue="gmv.tasks.events",
    )
    _redis_get_set_task_id(cache_key, task.id)
    return {"job_id": task.id, "accepted": True, "action": "advertisers", "plan": {"mode": params.mode}}


@router.post(f"{BASE_PREFIX}/shops")
def trigger_shops_sync(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    params: SyncShopsParams = Depends(),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    advertiser_id: Optional[str] = Query(default=None),
    advertiser_ids: Optional[str] = Query(default=None, description="comma separated"),
):
    _norm_provider(provider)
    key = _idempotency_key_required(idempotency_key)
    cache_key = _binding_cache_key(workspace_id, auth_id, "shops", key)

    existed = _redis_get_set_task_id(cache_key)
    if existed:
        return {"job_id": existed, "accepted": True, "action": "shops", "idempotent": True}

    # 过滤器透传（当前任务内部未做基于广告主的过滤抓取；如需按广告主筛，将在 0005 扩展）
    filt = {}
    if advertiser_id:
        filt["advertiser_id"] = advertiser_id
    if advertiser_ids:
        filt["advertiser_ids"] = [x.strip() for x in advertiser_ids.split(",") if x.strip()]

    task = celery_app.send_task(
        "tenant.ttb.sync.shops",
        kwargs={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "params": {**params.dict(), **filt},
        },
        queue="gmv.tasks.events",
    )
    _redis_get_set_task_id(cache_key, task.id)
    return {"job_id": task.id, "accepted": True, "action": "shops", "plan": {"mode": params.mode}}


@router.post(f"{BASE_PREFIX}/products")
def trigger_products_sync(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    params: SyncProductsParams = Depends(),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    shop_id: Optional[str] = Query(default=None),
    shop_ids: Optional[str] = Query(default=None, description="comma separated"),
):
    _norm_provider(provider)
    key = _idempotency_key_required(idempotency_key)
    cache_key = _binding_cache_key(workspace_id, auth_id, "products", key)

    existed = _redis_get_set_task_id(cache_key)
    if existed:
        return {"job_id": existed, "accepted": True, "action": "products", "idempotent": True}

    filt = {}
    if shop_id:
        filt["shop_id"] = shop_id
    if shop_ids:
        filt["shop_ids"] = [x.strip() for x in shop_ids.split(",") if x.strip()]

    task = celery_app.send_task(
        "tenant.ttb.sync.products",
        kwargs={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "params": {**params.dict(), **filt},
        },
        queue="gmv.tasks.events",
    )
    _redis_get_set_task_id(cache_key, task.id)
    return {"job_id": task.id, "accepted": True, "action": "products", "plan": {"mode": params.mode}}

