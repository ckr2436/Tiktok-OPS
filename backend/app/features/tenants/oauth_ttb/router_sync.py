# app/features/tenants/oauth_ttb/router_sync.py
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Annotated, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, validator

from app.data.db import get_db
from sqlalchemy.orm import Session

from app.core.errors import APIError, RateLimitExceeded
from app.core.metrics import get_counter, get_histogram
from app.celery_app import celery_app  # 你现有的入口

from app.features.platform.router_tasks import _RATE_LIMIT_COUNTER


router = APIRouter(tags=["tenant.tiktok-business.sync"])

_SYNC_COUNTER = get_counter(
    "tenant_sync_jobs_total",
    "Count of tenant initiated sync jobs",
    labelnames=("kind", "status"),
)
_SYNC_DURATION = get_histogram(
    "tenant_sync_job_duration_seconds",
    "Latency of tenant sync trigger endpoints",
    labelnames=("kind",),
)

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
        raise APIError("INVALID_ARGUMENT", "Idempotency-Key header is required", status.HTTP_400_BAD_REQUEST)
    if len(idem) > 256:
        raise APIError("INVALID_ARGUMENT", "Idempotency-Key too long", status.HTTP_400_BAD_REQUEST)
    return idem


def _redis_client():
    backend = getattr(celery_app, "backend", None)
    return getattr(backend, "client", None)


def _load_cached_job(cache_key: str, payload_hash: str) -> Optional[Dict[str, str]]:
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = client.get(cache_key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        stored_hash = data.get("payload_hash")
        if stored_hash and stored_hash != payload_hash:
            raise APIError(
                "IDEMPOTENCY_CONFLICT",
                "Payload differs for the same Idempotency-Key",
                status.HTTP_409_CONFLICT,
                data={"job_id": data.get("job_id"), "payload_hash": stored_hash},
            )
        return data
    except APIError:
        raise
    except Exception:
        return None


def _store_cached_job(cache_key: str, payload_hash: str, job_id: str) -> Dict[str, str]:
    client = _redis_client()
    record = {"job_id": job_id, "payload_hash": payload_hash}
    if client is None:
        return record
    try:
        value = json.dumps(record, separators=(",", ":"))
        ok = client.set(cache_key, value, ex=24 * 3600, nx=True)
        if ok:
            return record
        existing = _load_cached_job(cache_key, payload_hash)
        return existing or record
    except APIError:
        raise
    except Exception:
        return record


def _binding_cache_key(workspace_id: int, auth_id: int, action: str, idem: str) -> str:
    return f"idempotency:ttb:{workspace_id}:{auth_id}:{action}:{idem}"


def _rate_limit_binding(workspace_id: int, auth_id: int, kind: str, *, limit: int = 10, window: int = 30) -> tuple[int, int, int]:
    client = _redis_client()
    if client is None:
        reset_ts = int(time.time()) + window
        return limit, limit, reset_ts
    key = f"ratelimit:ttb:{workspace_id}:{auth_id}:{kind}"
    try:
        current = client.incr(key)
        client.expire(key, window)
        ttl = client.ttl(key)
        ttl = int(ttl) if ttl and int(ttl) > 0 else window
        reset_ts = int(time.time()) + ttl
        remaining = max(0, limit - int(current))
        if int(current) > limit:
            _RATE_LIMIT_COUNTER.labels(scope="auth").inc()
            next_allowed = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            raise RateLimitExceeded(
                "Too many requests.",
                next_allowed_at=next_allowed,
                limit=limit,
                remaining=remaining,
                reset_ts=reset_ts,
            )
        return limit, remaining, reset_ts
    except RateLimitExceeded:
        raise
    except Exception:
        reset_ts = int(time.time()) + window
        return limit, limit, reset_ts


def _hash_payload(payload: Dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------- 触发器 ----------
@router.post(f"{BASE_PREFIX}/bc")
def trigger_bc_sync(
    response: Response,
    workspace_id: int,
    provider: str,
    auth_id: int,
    params: SyncCommonParams = Depends(),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _norm_provider(provider)
    key = _idempotency_key_required(idempotency_key)
    cache_key = _binding_cache_key(workspace_id, auth_id, "bc", key)
    payload_hash = _hash_payload({
        "workspace_id": workspace_id,
        "auth_id": auth_id,
        "params": params.dict(),
    })

    start = time.perf_counter()
    status_label = "error"

    try:
        limit, remaining, reset_ts = _rate_limit_binding(workspace_id, auth_id, "bc")
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)

        cached = _load_cached_job(cache_key, payload_hash)
        if cached:
            status_label = "idempotent"
            return {
                "job_id": cached.get("job_id"),
                "accepted": True,
                "action": "bc",
                "idempotent": True,
                "plan": {"mode": params.mode, "limit": params.limit or None},
                "hints": {"rate_limit": "10/s"},
            }

        task = celery_app.send_task(
            "tenant.ttb.sync.bc",
            kwargs={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "params": params.dict(),
            },
            queue="gmv.tasks.events",
        )
        _store_cached_job(cache_key, payload_hash, task.id)

        status_label = "accepted"
        return {
            "job_id": task.id,
            "accepted": True,
            "action": "bc",
            "plan": {"mode": params.mode, "limit": params.limit or None},
            "hints": {"rate_limit": "10/s"},
        }
    except RateLimitExceeded:
        status_label = "rate_limited"
        raise
    except APIError as exc:
        status_label = exc.code.lower()
        raise
    finally:
        duration = time.perf_counter() - start
        _SYNC_COUNTER.labels(kind="bc", status=status_label).inc()
        _SYNC_DURATION.labels(kind="bc").observe(duration)


@router.post(f"{BASE_PREFIX}/advertisers")
def trigger_advertisers_sync(
    response: Response,
    workspace_id: int,
    provider: str,
    auth_id: int,
    params: SyncCommonParams = Depends(),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _norm_provider(provider)
    key = _idempotency_key_required(idempotency_key)
    cache_key = _binding_cache_key(workspace_id, auth_id, "advertisers", key)
    payload_hash = _hash_payload({
        "workspace_id": workspace_id,
        "auth_id": auth_id,
        "params": params.dict(),
    })

    start = time.perf_counter()
    status_label = "error"

    try:
        limit, remaining, reset_ts = _rate_limit_binding(workspace_id, auth_id, "advertisers")
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)

        cached = _load_cached_job(cache_key, payload_hash)
        if cached:
            status_label = "idempotent"
            return {
                "job_id": cached.get("job_id"),
                "accepted": True,
                "action": "advertisers",
                "idempotent": True,
                "plan": {"mode": params.mode},
            }

        task = celery_app.send_task(
            "tenant.ttb.sync.advertisers",
            kwargs={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "params": params.dict(),
            },
            queue="gmv.tasks.events",
        )
        _store_cached_job(cache_key, payload_hash, task.id)

        status_label = "accepted"
        return {"job_id": task.id, "accepted": True, "action": "advertisers", "plan": {"mode": params.mode}}
    except RateLimitExceeded:
        status_label = "rate_limited"
        raise
    except APIError as exc:
        status_label = exc.code.lower()
        raise
    finally:
        duration = time.perf_counter() - start
        _SYNC_COUNTER.labels(kind="advertisers", status=status_label).inc()
        _SYNC_DURATION.labels(kind="advertisers").observe(duration)


@router.post(f"{BASE_PREFIX}/shops")
def trigger_shops_sync(
    response: Response,
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

    filt: Dict[str, object] = {}
    if advertiser_id:
        filt["advertiser_id"] = advertiser_id
    if advertiser_ids:
        filt["advertiser_ids"] = [x.strip() for x in advertiser_ids.split(",") if x.strip()]

    payload_hash = _hash_payload({
        "workspace_id": workspace_id,
        "auth_id": auth_id,
        "params": {**params.dict(), **filt},
    })

    start = time.perf_counter()
    status_label = "error"

    try:
        limit, remaining, reset_ts = _rate_limit_binding(workspace_id, auth_id, "shops")
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)

        cached = _load_cached_job(cache_key, payload_hash)
        if cached:
            status_label = "idempotent"
            return {
                "job_id": cached.get("job_id"),
                "accepted": True,
                "action": "shops",
                "idempotent": True,
                "plan": {"mode": params.mode},
            }

        task = celery_app.send_task(
            "tenant.ttb.sync.shops",
            kwargs={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "params": {**params.dict(), **filt},
            },
            queue="gmv.tasks.events",
        )
        _store_cached_job(cache_key, payload_hash, task.id)

        status_label = "accepted"
        return {"job_id": task.id, "accepted": True, "action": "shops", "plan": {"mode": params.mode}}
    except RateLimitExceeded:
        status_label = "rate_limited"
        raise
    except APIError as exc:
        status_label = exc.code.lower()
        raise
    finally:
        duration = time.perf_counter() - start
        _SYNC_COUNTER.labels(kind="shops", status=status_label).inc()
        _SYNC_DURATION.labels(kind="shops").observe(duration)


@router.post(f"{BASE_PREFIX}/products")
def trigger_products_sync(
    response: Response,
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

    filt: Dict[str, object] = {}
    if shop_id:
        filt["shop_id"] = shop_id
    if shop_ids:
        filt["shop_ids"] = [x.strip() for x in shop_ids.split(",") if x.strip()]

    payload_hash = _hash_payload({
        "workspace_id": workspace_id,
        "auth_id": auth_id,
        "params": {**params.dict(), **filt},
    })

    start = time.perf_counter()
    status_label = "error"

    try:
        limit, remaining, reset_ts = _rate_limit_binding(workspace_id, auth_id, "products")
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)

        cached = _load_cached_job(cache_key, payload_hash)
        if cached:
            status_label = "idempotent"
            return {
                "job_id": cached.get("job_id"),
                "accepted": True,
                "action": "products",
                "idempotent": True,
                "plan": {"mode": params.mode},
            }

        task = celery_app.send_task(
            "tenant.ttb.sync.products",
            kwargs={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "params": {**params.dict(), **filt},
            },
            queue="gmv.tasks.events",
        )
        _store_cached_job(cache_key, payload_hash, task.id)

        status_label = "accepted"
        return {"job_id": task.id, "accepted": True, "action": "products", "plan": {"mode": params.mode}}
    except RateLimitExceeded:
        status_label = "rate_limited"
        raise
    except APIError as exc:
        status_label = exc.code.lower()
        raise
    finally:
        duration = time.perf_counter() - start
        _SYNC_COUNTER.labels(kind="products", status=status_label).inc()
        _SYNC_DURATION.labels(kind="products").observe(duration)

