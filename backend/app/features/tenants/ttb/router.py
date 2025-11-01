# app/features/tenants/ttb/router.py
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthAccountTTB
from app.data.models.scheduling import Schedule, ScheduleRun
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBBindingConfig,
)
from app.services.ttb_sync_dispatch import DispatchResult, SYNC_TASKS, dispatch_sync
from app.services.provider_registry import provider_registry, load_builtin_providers
from app.services.ttb_binding_config import (
    get_binding_config,
    upsert_binding_config,
)

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business"],
)

load_builtin_providers()

SUPPORTED_PROVIDERS = {"tiktok-business", "tiktok_business"}


# -------------------------- 请求/响应模型 --------------------------
class SyncRequest(BaseModel):
    scope: Literal["meta", "products"] = "meta"
    mode: Literal["incremental", "full"] = "full"
    advertiser_id: Optional[str] = Field(default=None, max_length=128)
    store_id: Optional[str] = Field(default=None, max_length=128)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    product_eligibility: Literal["gmv_max"] = "gmv_max"


class MetaSummaryItem(BaseModel):
    added: int
    removed: int
    unchanged: int


class MetaSummary(BaseModel):
    bc: MetaSummaryItem
    advertisers: MetaSummaryItem
    stores: MetaSummaryItem


class SyncResponse(BaseModel):
    run_id: Optional[int] = None
    schedule_id: Optional[int] = None
    task_name: Optional[str] = None
    task_id: Optional[str] = None
    status: str
    idempotent: bool = False
    summary: Optional[MetaSummary] = None


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


class PagedResult(BaseModel):
    items: list[Any]
    page: int
    page_size: int
    total: int


class BusinessCenterItem(BaseModel):
    bc_id: str
    name: Optional[str]
    status: Optional[str]
    timezone: Optional[str]
    country_code: Optional[str]
    owner_user_id: Optional[str]
    ext_created_time: Optional[str]
    ext_updated_time: Optional[str]
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    raw: Optional[Dict[str, Any]]


class BusinessCenterList(BaseModel):
    items: list[BusinessCenterItem]


class AdvertiserItem(BaseModel):
    advertiser_id: str
    bc_id: Optional[str]
    name: Optional[str]
    display_name: Optional[str]
    status: Optional[str]
    industry: Optional[str]
    currency: Optional[str]
    timezone: Optional[str]
    country_code: Optional[str]
    ext_created_time: Optional[str]
    ext_updated_time: Optional[str]
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    raw: Optional[Dict[str, Any]]


class AdvertiserList(BaseModel):
    items: list[AdvertiserItem]


class StoreItem(BaseModel):
    store_id: str
    advertiser_id: Optional[str]
    bc_id: Optional[str]
    name: Optional[str]
    status: Optional[str]
    region_code: Optional[str]
    ext_created_time: Optional[str]
    ext_updated_time: Optional[str]
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    raw: Optional[Dict[str, Any]]


class StoreList(BaseModel):
    items: list[StoreItem]


class GMVMaxBindingConfig(BaseModel):
    bc_id: Optional[str]
    advertiser_id: Optional[str]
    store_id: Optional[str]
    auto_sync_products: bool
    last_manual_synced_at: Optional[str]
    last_manual_sync_summary: Optional[Dict[str, Any]]
    last_auto_synced_at: Optional[str]
    last_auto_sync_summary: Optional[Dict[str, Any]]


class GMVMaxBindingUpdateRequest(BaseModel):
    bc_id: str = Field(max_length=64)
    advertiser_id: str = Field(max_length=64)
    store_id: str = Field(max_length=64)
    auto_sync_products: bool = False


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


def _serialize_binding_config(row: TTBBindingConfig | None) -> GMVMaxBindingConfig:
    if not row:
        return GMVMaxBindingConfig(
            bc_id=None,
            advertiser_id=None,
            store_id=None,
            auto_sync_products=False,
            last_manual_synced_at=None,
            last_manual_sync_summary=None,
            last_auto_synced_at=None,
            last_auto_sync_summary=None,
        )

    def _iso(dt: Optional[datetime]) -> Optional[str]:
        if not dt:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc).isoformat()
        return dt.astimezone(timezone.utc).isoformat()

    return GMVMaxBindingConfig(
        bc_id=row.bc_id,
        advertiser_id=row.advertiser_id,
        store_id=row.store_id,
        auto_sync_products=bool(row.auto_sync_products),
        last_manual_synced_at=_iso(row.last_manual_synced_at),
        last_manual_sync_summary=row.last_manual_sync_summary_json or None,
        last_auto_synced_at=_iso(row.last_auto_synced_at),
        last_auto_sync_summary=row.last_auto_sync_summary_json or None,
    )


def _legacy_disabled() -> None:
    raise APIError(
        "TTB_LEGACY_DISABLED",
        "TikTok Business legacy data endpoints have been disabled.",
        status.HTTP_410_GONE,
    )


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

    if body.scope == "meta":
        try:
            summary_payload = _perform_meta_sync(db, workspace_id=workspace_id, auth_id=auth_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        response_payload = SyncResponse(status="success", summary=_meta_summary_from_dict(summary_payload))
        return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(response_payload))

    if body.scope != "products":
        raise APIError("UNSUPPORTED_SCOPE", f"Scope {body.scope} is not supported.", status.HTTP_400_BAD_REQUEST)

    if not body.advertiser_id:
        raise APIError(
            "ADVERTISER_REQUIRED_FOR_GMV_MAX",
            "advertiser_id is required for GMV Max product sync.",
            status.HTTP_400_BAD_REQUEST,
        )
    if not body.store_id:
        raise APIError(
            "STORE_ID_REQUIRED_FOR_GMV_MAX",
            "store_id is required for GMV Max product sync.",
            status.HTTP_400_BAD_REQUEST,
        )

    advertiser = _get_advertiser(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(body.advertiser_id),
    )
    store = _get_store(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=str(body.store_id),
    )

    bc_id = store.bc_id or advertiser.bc_id
    _validate_bc_alignment(expected_bc_id=bc_id, advertiser=advertiser, store=store)

    _enforce_products_limits(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(body.advertiser_id),
        store_id=str(body.store_id),
    )

    params = {
        "mode": body.mode,
        "advertiser_id": str(body.advertiser_id),
        "store_id": str(body.store_id),
        "product_eligibility": body.product_eligibility,
        "bc_id": bc_id,
    }

    try:
        result: DispatchResult = dispatch_sync(
            db,
            workspace_id=int(workspace_id),
            provider=normalized_provider,
            auth_id=int(auth_id),
            scope="products",
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
        task_name=SYNC_TASKS["products"],
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
def _normalize_nullable_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        if trimmed.lower() == "none":
            return None
        return trimmed
    # For numeric IDs or other primitives, coerce to string then normalize once.
    return _normalize_nullable_str(str(value))


def _serialize_bc(row: TTBBusinessCenter) -> Dict[str, Any]:
    raw = row.raw_json or {}
    bc_info = raw.get("bc_info") or {}

    bc_id = _normalize_nullable_str(row.bc_id) or _normalize_nullable_str(
        bc_info.get("bc_id") or raw.get("business_center_id")
    )
    name = _normalize_nullable_str(row.name) or _normalize_nullable_str(
        bc_info.get("name") or bc_info.get("display_name")
    )
    status = _normalize_nullable_str(row.status) or _normalize_nullable_str(bc_info.get("status"))
    timezone = _normalize_nullable_str(row.timezone) or _normalize_nullable_str(bc_info.get("timezone"))
    country_code = _normalize_nullable_str(row.country_code) or _normalize_nullable_str(
        bc_info.get("registered_area") or bc_info.get("country_code")
    )

    return {
        "bc_id": bc_id,
        "name": name,
        "status": status,
        "timezone": timezone,
        "country_code": country_code,
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


def _serialize_store(row: TTBStore) -> Dict[str, Any]:
    return {
        "store_id": row.store_id,
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


def _coerce_utc(value: Optional[datetime]) -> Optional[datetime]:
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _options_match(params: Dict[str, Any], *, auth_id: int, advertiser_id: str, store_id: str) -> bool:
    raw_auth = params.get("auth_id") or params.get("authId")
    if raw_auth is None or int(raw_auth) != int(auth_id):
        return False
    options = params.get("options") or {}
    eligibility = str(options.get("product_eligibility") or "").strip().lower()
    if eligibility not in ("", "gmv_max"):
        return False
    if str(options.get("advertiser_id")) != str(advertiser_id):
        return False
    if str(options.get("store_id")) != str(store_id):
        return False
    return True


def _enforce_products_limits(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
) -> None:
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=1)
    rows = (
        db.query(ScheduleRun, Schedule)
        .join(Schedule, Schedule.id == ScheduleRun.schedule_id)
        .filter(
            ScheduleRun.workspace_id == int(workspace_id),
            Schedule.task_name == SYNC_TASKS["products"],
            ScheduleRun.created_at >= lookback,
        )
        .order_by(ScheduleRun.created_at.desc())
        .limit(50)
        .all()
    )

    for run, schedule in rows:
        params = schedule.params_json or {}
        if not _options_match(params, auth_id=auth_id, advertiser_id=advertiser_id, store_id=store_id):
            continue
        if run.status in {"enqueued", "running"}:
            raise APIError(
                "SYNC_IN_PROGRESS",
                "A GMV Max product sync is already running for this combination.",
                status.HTTP_409_CONFLICT,
            )

        ts = _coerce_utc(run.enqueued_at or run.scheduled_for or run.created_at)
        if ts and (now - ts) < timedelta(minutes=15):
            raise APIError(
                "SYNC_RATE_LIMITED",
                "GMV Max product sync was triggered too recently.",
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
        break


def _get_business_center(
    db: Session, *, workspace_id: int, auth_id: int, bc_id: str
) -> TTBBusinessCenter:
    row = (
        db.query(TTBBusinessCenter)
        .filter(
            TTBBusinessCenter.workspace_id == int(workspace_id),
            TTBBusinessCenter.auth_id == int(auth_id),
            TTBBusinessCenter.bc_id == bc_id,
        )
        .one_or_none()
    )
    if not row:
        raise APIError("BUSINESS_CENTER_NOT_FOUND", "Business center not found.", status.HTTP_404_NOT_FOUND)
    return row


def _get_advertiser(
    db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str
) -> TTBAdvertiser:
    row = (
        db.query(TTBAdvertiser)
        .filter(
            TTBAdvertiser.workspace_id == int(workspace_id),
            TTBAdvertiser.auth_id == int(auth_id),
            TTBAdvertiser.advertiser_id == advertiser_id,
        )
        .one_or_none()
    )
    if not row:
        raise APIError("ADVERTISER_NOT_FOUND", "Advertiser not found.", status.HTTP_404_NOT_FOUND)
    return row


def _get_store(
    db: Session, *, workspace_id: int, auth_id: int, store_id: str
) -> TTBStore:
    row = (
        db.query(TTBStore)
        .filter(
            TTBStore.workspace_id == int(workspace_id),
            TTBStore.auth_id == int(auth_id),
            TTBStore.store_id == store_id,
        )
        .one_or_none()
    )
    if not row:
        raise APIError("STORE_NOT_FOUND", "Store not found.", status.HTTP_404_NOT_FOUND)
    return row


def _validate_bc_alignment(
    *,
    expected_bc_id: Optional[str],
    advertiser: TTBAdvertiser,
    store: TTBStore,
) -> None:
    normalized_expected = _normalize_nullable_str(expected_bc_id)
    advertiser_bc = _normalize_nullable_str(advertiser.bc_id)
    store_bc = _normalize_nullable_str(store.bc_id)

    if normalized_expected and advertiser_bc and normalized_expected != advertiser_bc:
        raise APIError(
            "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
            "Advertiser belongs to a different business center.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if normalized_expected and store_bc and normalized_expected != store_bc:
        raise APIError(
            "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
            "Store belongs to a different business center.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if advertiser_bc and store_bc and advertiser_bc != store_bc:
        raise APIError(
            "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
            "Advertiser and store are not linked to the same business center.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


def _meta_summary_from_dict(payload: Dict[str, Dict[str, int]]) -> MetaSummary:
    def _section(key: str) -> MetaSummaryItem:
        data = payload.get(key) or {}
        return MetaSummaryItem(
            added=int(data.get("added") or 0),
            removed=int(data.get("removed") or 0),
            unchanged=int(data.get("unchanged") or 0),
        )

    return MetaSummary(
        bc=_section("bc"),
        advertisers=_section("advertisers"),
        stores=_section("stores"),
    )


def _perform_meta_sync(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    page_size: int = 200,
) -> Dict[str, Dict[str, int]]:
    handler = provider_registry.get("tiktok-business")
    envelope = {
        "envelope_version": 1,
        "provider": "tiktok-business",
        "scope": "meta",
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "options": {"page_size": page_size},
    }
    logger_adapter = logging.getLogger("gmv.ttb.meta")
    result = asyncio.run(
        handler.run_scope(
            db=db,
            envelope=envelope,
            scope="meta",
            logger=logger_adapter,
        )
    )
    return result.get("summary") or {}


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
    _legacy_disabled()


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
    _legacy_disabled()


@router.get(
    "/{workspace_id}/providers/{provider}/stores",
    response_model=PagedResult,
)
def list_stores(
    workspace_id: int,
    provider: str,
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _legacy_disabled()


@router.get(
    "/{workspace_id}/providers/{provider}/products",
    response_model=PagedResult,
)
def list_products(
    workspace_id: int,
    provider: str,
    store_id: Optional[str] = Query(default=None, max_length=64),
    # NEW: 基于 JSON 的过滤
    eligibility: Optional[Literal["gmv_max", "ads", "all"]] = Query(default=None),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _legacy_disabled()


# -------------------------- 新增：account-scoped 别名路由（修复前端 404） --------------------------
@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/business-centers",
    response_model=BusinessCenterList,
)
def list_account_business_centers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    rows = (
        db.query(TTBBusinessCenter)
        .filter(TTBBusinessCenter.workspace_id == int(workspace_id))
        .filter(TTBBusinessCenter.auth_id == int(auth_id))
        .order_by(TTBBusinessCenter.name.asc(), TTBBusinessCenter.bc_id.asc())
        .all()
    )
    return BusinessCenterList(items=[BusinessCenterItem(**_serialize_bc(r)) for r in rows])


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/advertisers",
    response_model=AdvertiserList,
)
def list_account_advertisers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    bc_id: Optional[str] = Query(default=None, max_length=64),
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
    rows = query.order_by(TTBAdvertiser.display_name.asc(), TTBAdvertiser.advertiser_id.asc()).all()
    return AdvertiserList(items=[AdvertiserItem(**_serialize_adv(r)) for r in rows])


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/stores",
    response_model=StoreList,
)
def list_account_stores(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBStore)
        .filter(TTBStore.workspace_id == int(workspace_id))
        .filter(TTBStore.auth_id == int(auth_id))
    )
    if advertiser_id:
        query = query.filter(TTBStore.advertiser_id == advertiser_id)
    rows = query.order_by(TTBStore.name.asc(), TTBStore.store_id.asc()).all()
    return StoreList(items=[StoreItem(**_serialize_store(r)) for r in rows])


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/products",
)
def list_account_products(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: Optional[str] = Query(default=None, max_length=64),
    eligibility: Optional[Literal["gmv_max", "ads", "all"]] = Query(default=None),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    _legacy_disabled()


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmv-max/config",
    response_model=GMVMaxBindingConfig,
)
def get_gmv_max_config(
    workspace_id: int,
    provider: str,
    auth_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    row = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    return _serialize_binding_config(row)


@router.put(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmv-max/config",
    response_model=GMVMaxBindingConfig,
)
def update_gmv_max_config(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: GMVMaxBindingUpdateRequest,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)

    bc = _get_business_center(db, workspace_id=workspace_id, auth_id=auth_id, bc_id=payload.bc_id)
    advertiser = _get_advertiser(db, workspace_id=workspace_id, auth_id=auth_id, advertiser_id=payload.advertiser_id)
    store = _get_store(db, workspace_id=workspace_id, auth_id=auth_id, store_id=payload.store_id)
    _validate_bc_alignment(expected_bc_id=bc.bc_id, advertiser=advertiser, store=store)

    try:
        row = upsert_binding_config(
            db,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            bc_id=payload.bc_id,
            advertiser_id=payload.advertiser_id,
            store_id=payload.store_id,
            auto_sync_products=payload.auto_sync_products,
            actor_user_id=int(me.id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return _serialize_binding_config(row)


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

