# app/features/tenants/ttb/router.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthAccountTTB
from app.data.models.scheduling import Schedule, ScheduleRun
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBShop,
    TTBProduct,
)
from app.services.ttb_sync_dispatch import DispatchResult, SYNC_TASKS, dispatch_sync

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business"],
)

SUPPORTED_PROVIDERS = {"tiktok-business", "tiktok_business"}


# -------------------------- 请求/响应模型 --------------------------
class SyncRequest(BaseModel):
    scope: Literal["bc", "advertisers", "shops", "products", "all"] = "all"
    mode: Literal["incremental", "full"] = "incremental"
    limit: Optional[int] = Field(default=None, ge=1, le=2000)
    product_limit: Optional[int] = Field(default=None, ge=1, le=2000)
    shop_id: Optional[str] = Field(default=None, max_length=128)
    since: Optional[datetime] = Field(default=None)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    # NEW: 可投放类型（默认后台自动同步 GMV Max；ADS 保留给手动触发）
    # - gmv_max  => filtering.ad_creation_eligible = GMV_MAX
    # - ads      => filtering.ad_creation_eligible = CUSTOM_SHOP_ADS
    # - all      => 不加筛选
    product_eligibility: Literal["gmv_max", "ads", "all"] = "gmv_max"


class SyncResponse(BaseModel):
    run_id: int
    schedule_id: int
    task_name: str
    task_id: Optional[str]
    status: str
    idempotent: bool = False


class SyncRunResponse(BaseModel):
    id: int
    schedule_id: int
    task_name: str
    status: str
    scheduled_for: str
    enqueued_at: Optional[str]
    duration_ms: Optional[int]
    error_code: Optional[str]
    error_message: Optional[str]
    stats: Optional[Dict[str, Any]]


class PagedResult(BaseModel):
    items: list[Dict[str, Any]]
    page: int
    page_size: int
    total: int


class ProviderAccount(BaseModel):
    provider: str
    auth_id: int
    label: str
    status: Literal["active", "invalid"]


class ProviderAccountsResponse(BaseModel):
    items: list[ProviderAccount]
    page: int
    page_size: int
    total: int


class AccountSummary(BaseModel):
    auth_id: int
    label: str
    status: Literal["active", "invalid"]


class ProviderAccountListResponse(BaseModel):
    items: list[AccountSummary]
    page: int
    page_size: int
    total: int


# -------------------------- 工具函数 --------------------------
def _normalize_provider(provider: str) -> str:
    key = (provider or "").strip().lower()
    if key not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="provider not supported")
    return "tiktok-business"


def _ensure_account(db: Session, workspace_id: int, auth_id: int) -> OAuthAccountTTB:
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc or acc.workspace_id != int(workspace_id):
        raise HTTPException(status_code=404, detail="binding not found")
    if acc.status not in {"active", "invalid"}:
        raise HTTPException(status_code=400, detail=f"binding status {acc.status} cannot be synced")
    return acc


def _serialize_binding(row: OAuthAccountTTB) -> ProviderAccount:
    return ProviderAccount(
        provider="tiktok-business",
        auth_id=int(row.id),
        label=row.alias or row.created_at.isoformat(),
        status="active" if row.status == "active" else "invalid",
    )


def _serialize_account_summary(row: OAuthAccountTTB) -> AccountSummary:
    binding = _serialize_binding(row)
    return AccountSummary(auth_id=binding.auth_id, label=binding.label, status=binding.status)


# -------------------------- 账号列表 --------------------------
@router.get(
    "/{workspace_id}/providers",
    response_model=ProviderAccountsResponse,
)
def list_providers(
    workspace_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    query = db.query(OAuthAccountTTB).filter(OAuthAccountTTB.workspace_id == int(workspace_id))
    total = int(query.with_entities(func.count()).scalar() or 0)
    rows = (
        query.order_by(OAuthAccountTTB.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [_serialize_binding(row) for row in rows]
    return ProviderAccountsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts",
    response_model=ProviderAccountListResponse,
)
def list_provider_accounts(
    workspace_id: int,
    provider: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    query = db.query(OAuthAccountTTB).filter(OAuthAccountTTB.workspace_id == int(workspace_id))
    total = int(query.with_entities(func.count()).scalar() or 0)
    rows = (
        query.order_by(OAuthAccountTTB.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [_serialize_account_summary(row) for row in rows]
    return ProviderAccountListResponse(items=items, page=page, page_size=page_size, total=total)


# -------------------------- 触发同步 --------------------------
@router.post(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/sync",
    response_model=SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_sync(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    body: SyncRequest,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    normalized_provider = _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)

    params = {
        "mode": body.mode,
        "limit": body.limit,
        "product_limit": body.product_limit,
        "shop_id": body.shop_id,
        "since": body.since.isoformat() if body.since else None,
        # NEW: 将可投放类型传给调度层（由调度层决定是否仅后台自动跑 GMV Max）
        "product_eligibility": body.product_eligibility,
    }

    try:
        result: DispatchResult = dispatch_sync(
            db,
            workspace_id=int(workspace_id),
            provider=normalized_provider,
            auth_id=int(auth_id),
            scope=body.scope,
            params=params,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
            actor_ip=request.client.host if request.client else None,
            idempotency_key=body.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SyncResponse(
        run_id=int(result.run.id),
        schedule_id=int(result.run.schedule_id),
        task_name=SYNC_TASKS[body.scope],
        task_id=result.task_id,
        status=result.status,
        idempotent=result.idempotent,
    )


# -------------------------- 运行查询 --------------------------
def _run_matches_account(run: ScheduleRun, provider: str, auth_id: int) -> bool:
    stats = run.stats_json or {}
    requested = stats.get("requested") or {}
    if int(requested.get("auth_id") or 0) != int(auth_id):
        return False
    if (requested.get("provider") or "").strip() != provider:
        return False
    return True


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/sync-runs/{run_id}",
    response_model=SyncRunResponse,
)
def get_sync_run(
    workspace_id: int,
    provider: str,
    auth_id: int,
    run_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    normalized_provider = _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    run = db.get(ScheduleRun, int(run_id))
    if not run or run.workspace_id != int(workspace_id):
        raise HTTPException(status_code=404, detail="sync run not found")
    if not _run_matches_account(run, normalized_provider, auth_id):
        raise HTTPException(status_code=404, detail="sync run not found")
    schedule = db.get(Schedule, int(run.schedule_id)) if run.schedule_id else None
    return SyncRunResponse(
        id=int(run.id),
        schedule_id=int(run.schedule_id),
        task_name=schedule.task_name if schedule else "",
        status=run.status,
        scheduled_for=run.scheduled_for.isoformat() if run.scheduled_for else "",
        enqueued_at=run.enqueued_at.isoformat() if run.enqueued_at else None,
        duration_ms=run.duration_ms,
        error_code=run.error_code,
        error_message=run.error_message,
        stats=run.stats_json,
    )


# -------------------------- 基础分页 & 序列化 --------------------------
def _pagination(query, model, page: int, page_size: int):
    total = query.with_entities(func.count()).scalar() or 0
    rows = (
        query.order_by(model.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return int(total), rows


def _serialize_bc(row: TTBBusinessCenter) -> Dict[str, Any]:
    return {
        "bc_id": row.bc_id,
        "name": row.name,
        "status": row.status,
        "timezone": row.timezone,
        "country_code": row.country_code,
        "owner_user_id": row.owner_user_id,
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _serialize_adv(row: TTBAdvertiser) -> Dict[str, Any]:
    return {
        "advertiser_id": row.advertiser_id,
        "bc_id": row.bc_id,
        "name": row.name,
        "display_name": row.display_name,
        "status": row.status,
        "industry": row.industry,
        "currency": row.currency,
        "timezone": row.timezone,
        "country_code": row.country_code,
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _serialize_shop(row: TTBShop) -> Dict[str, Any]:
    return {
        "shop_id": row.shop_id,
        "advertiser_id": row.advertiser_id,
        "bc_id": row.bc_id,
        "name": row.name,
        "status": row.status,
        "region_code": row.region_code,
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _serialize_product(row: TTBProduct) -> Dict[str, Any]:
    price = float(row.price) if row.price is not None else None
    return {
        "product_id": row.product_id,
        "shop_id": row.shop_id,
        "title": row.title,
        "status": row.status,
        "currency": row.currency,
        "price": price,
        "stock": row.stock,
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _apply_product_eligibility_filter(query, eligibility: Optional[str]):
    """
    用 JSON 字段做过滤，不改表结构。
    - gmv_max => raw_json contains {"ad_creation_eligible": "GMV_MAX"}
    - ads     => raw_json contains {"ad_creation_eligible": "CUSTOM_SHOP_ADS"}
    - None/all => 不加过滤
    """
    if not eligibility or eligibility == "all":
        return query
    if eligibility == "gmv_max":
        return query.filter(TTBProduct.raw_json.contains({"ad_creation_eligible": "GMV_MAX"}))
    if eligibility == "ads":
        return query.filter(TTBProduct.raw_json.contains({"ad_creation_eligible": "CUSTOM_SHOP_ADS"}))
    return query


# -------------------------- 不带 auth_id 的列表（兼容旧前端） --------------------------
@router.get(
    "/{workspace_id}/providers/{provider}/business-centers",
    response_model=PagedResult,
)
def list_business_centers(
    workspace_id: int,
    provider: str,
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    auth_id: Optional[int] = Query(default=None, gt=0),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    query = db.query(TTBBusinessCenter).filter(TTBBusinessCenter.workspace_id == int(workspace_id))
    if auth_id:
        query = query.filter(TTBBusinessCenter.auth_id == int(auth_id))
    total, rows = _pagination(query, TTBBusinessCenter, page, page_size)
    return PagedResult(items=[_serialize_bc(r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/advertisers",
    response_model=PagedResult,
)
def list_advertisers(
    workspace_id: int,
    provider: str,
    bc_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    query = db.query(TTBAdvertiser).filter(TTBAdvertiser.workspace_id == int(workspace_id))
    if bc_id:
        query = query.filter(TTBAdvertiser.bc_id == bc_id)
    total, rows = _pagination(query, TTBAdvertiser, page, page_size)
    return PagedResult(items=[_serialize_adv(r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/shops",
    response_model=PagedResult,
)
def list_shops(
    workspace_id: int,
    provider: str,
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    query = db.query(TTBShop).filter(TTBShop.workspace_id == int(workspace_id))
    if advertiser_id:
        query = query.filter(TTBShop.advertiser_id == advertiser_id)
    total, rows = _pagination(query, TTBShop, page, page_size)
    return PagedResult(items=[_serialize_shop(r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/products",
    response_model=PagedResult,
)
def list_products(
    workspace_id: int,
    provider: str,
    shop_id: Optional[str] = Query(default=None, max_length=64),
    # NEW: 基于 JSON 的过滤
    eligibility: Optional[Literal["gmv_max", "ads", "all"]] = Query(default=None),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    query = db.query(TTBProduct).filter(TTBProduct.workspace_id == int(workspace_id))
    if shop_id:
        query = query.filter(TTBProduct.shop_id == shop_id)
    query = _apply_product_eligibility_filter(query, eligibility)
    total, rows = _pagination(query, TTBProduct, page, page_size)
    return PagedResult(items=[_serialize_product(r) for r in rows], page=page, page_size=page_size, total=total)


# -------------------------- 新增：account-scoped 别名路由（修复前端 404） --------------------------
@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/business-centers",
    response_model=PagedResult,
)
def list_account_business_centers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBBusinessCenter)
        .filter(TTBBusinessCenter.workspace_id == int(workspace_id))
        .filter(TTBBusinessCenter.auth_id == int(auth_id))
    )
    total, rows = _pagination(query, TTBBusinessCenter, page, page_size)
    return PagedResult(items=[_serialize_bc(r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/advertisers",
    response_model=PagedResult,
)
def list_account_advertisers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    bc_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
    )
    if bc_id:
        query = query.filter(TTBAdvertiser.bc_id == bc_id)
    total, rows = _pagination(query, TTBAdvertiser, page, page_size)
    return PagedResult(items=[_serialize_adv(r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/shops",
    response_model=PagedResult,
)
def list_account_shops(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBShop)
        .filter(TTBShop.workspace_id == int(workspace_id))
        .filter(TTBShop.auth_id == int(auth_id))
    )
    if advertiser_id:
        query = query.filter(TTBShop.advertiser_id == advertiser_id)
    total, rows = _pagination(query, TTBShop, page, page_size)
    return PagedResult(items=[_serialize_shop(r) for r in rows], page=page, page_size=page_size, total=total)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/products",
    response_model=PagedResult,
)
def list_account_products(
    workspace_id: int,
    provider: str,
    auth_id: int,
    shop_id: Optional[str] = Query(default=None, max_length=64),
    eligibility: Optional[Literal["gmv_max", "ads", "all"]] = Query(default=None),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBProduct)
        .filter(TTBProduct.workspace_id == int(workspace_id))
        .filter(TTBProduct.auth_id == int(auth_id))
    )
    if shop_id:
        query = query.filter(TTBProduct.shop_id == shop_id)
    query = _apply_product_eligibility_filter(query, eligibility)
    total, rows = _pagination(query, TTBProduct, page, page_size)
    return PagedResult(items=[_serialize_product(r) for r in rows], page=page, page_size=page_size, total=total)


# -------------------------- 旧路由废弃 --------------------------
_DEPRECATION_DETAIL = (
    "This endpoint was replaced by /api/v1/tenants/providers/tiktok-business/*. "
    "Legacy tenants/ttb routes will be removed after 2024-12-31."
)

@router.api_route(
    "/{workspace_id}/ttb/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
def deprecated_ttb_routes(**_: dict) -> None:
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_DEPRECATION_DETAIL)

