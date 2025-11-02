# app/features/tenants/ttb/router.py
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
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
    TTBProduct,
    TTBBCAdvertiserLink,
    TTBAdvertiserStoreLink,
)
from app.services.ttb_sync_dispatch import DispatchResult, SYNC_TASKS, dispatch_sync
from app.services.provider_registry import provider_registry, load_builtin_providers
from app.services.ttb_binding_config import (
    BindingConfigStorageNotReady,
    get_binding_config,
    upsert_binding_config,
)
from app.services.ttb_meta import (
    MetaCursorState,
    build_gmvmax_options,
    compute_meta_etag,
    enqueue_meta_sync,
    get_meta_cursor_state,
)
from app.services.ttb_sync import _normalize_identifier

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants",
    tags=["Tenant / TikTok Business"],
)

load_builtin_providers()

SUPPORTED_PROVIDERS = {"tiktok-business", "tiktok_business"}


logger = logging.getLogger("gmv.ttb.meta")


_RELATION_PRIORITY = {"OWNER": 1, "AUTHORIZER": 2, "PARTNER": 3, "UNKNOWN": 4}


def _relation_rank(value: Optional[str]) -> int:
    return _RELATION_PRIORITY.get((value or "UNKNOWN").upper(), 5)


# -------------------------- 请求/响应模型 --------------------------
class SyncRequest(BaseModel):
    scope: Literal["meta", "products"] = "meta"
    mode: Optional[Literal["incremental", "full"]] = "full"
    advertiser_id: Optional[str] = Field(default=None, max_length=128)
    store_id: Optional[str] = Field(default=None, max_length=128)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    product_eligibility: Optional[Literal["gmv_max", "ads", "all"]] = None
    options: Optional[Dict[str, Any]] = None


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
    idempotency_key: Optional[str] = None
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
    display_timezone: Optional[str]
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
    store_type: Optional[str]
    store_code: Optional[str]
    store_authorized_bc_id: Optional[str]
    ext_created_time: Optional[str]
    ext_updated_time: Optional[str]
    first_seen_at: Optional[str]
    last_seen_at: Optional[str]
    raw: Optional[Dict[str, Any]]


class StoreList(BaseModel):
    items: list[StoreItem]


class ProductItem(BaseModel):
    product_id: str
    store_id: Optional[str]
    title: Optional[str]
    status: Optional[str]
    sku_count: Optional[int]
    price_range: Optional[str]
    updated_time: Optional[str]
    currency: Optional[str]
    price: Optional[float]
    stock: Optional[int]
    ext_created_time: Optional[str]
    ext_updated_time: Optional[str]
    raw: Optional[Dict[str, Any]]


class ProductList(BaseModel):
    items: list[ProductItem]
    total: int
    page: int
    page_size: int


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


def _normalize_if_none_match(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    token = value.split(",", 1)[0].strip()
    if token.startswith("W/"):
        token = token[2:].strip()
    if token.startswith("\"") and token.endswith("\"") and len(token) >= 2:
        token = token[1:-1]
    return token or None


async def _poll_for_meta_refresh(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    initial_state: MetaCursorState,
    initial_etag: str,
    timeout_seconds: float = settings.GMV_MAX_OPTIONS_POLL_TIMEOUT_SECONDS,
    interval_seconds: float = settings.GMV_MAX_OPTIONS_POLL_INTERVAL_SECONDS,
):
    deadline = time.monotonic() + timeout_seconds
    state = initial_state
    changed = False
    while time.monotonic() < deadline:
        await asyncio.sleep(interval_seconds)
        db.expire_all()
        state = get_meta_cursor_state(db, workspace_id=workspace_id, auth_id=auth_id)
        current_etag = compute_meta_etag(state.revisions)
        if current_etag != initial_etag:
            changed = True
            break
    if not changed:
        db.expire_all()
        state = get_meta_cursor_state(db, workspace_id=workspace_id, auth_id=auth_id)
    return state, changed


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

    options_payload = dict(body.options or {})
    payload_idempotency = options_payload.pop("idempotency_key", None)
    requested_idempotency = body.idempotency_key or payload_idempotency

    raw_mode = options_payload.pop("mode", None) or body.mode or "full"
    normalized_mode = str(raw_mode).strip().lower() if raw_mode else "full"
    if normalized_mode not in {"incremental", "full"}:
        raise APIError("INVALID_MODE", "mode must be incremental or full.", status.HTTP_400_BAD_REQUEST)

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

    advertiser_id = options_payload.get("advertiser_id") or body.advertiser_id
    store_id = options_payload.get("store_id") or body.store_id

    if not advertiser_id:
        raise APIError(
            "ADVERTISER_REQUIRED_FOR_GMV_MAX",
            "advertiser_id is required for GMV Max product sync.",
            status.HTTP_400_BAD_REQUEST,
        )
    if not store_id:
        raise APIError(
            "STORE_ID_REQUIRED_FOR_GMV_MAX",
            "store_id is required for GMV Max product sync.",
            status.HTTP_400_BAD_REQUEST,
        )

    advertiser = _get_advertiser(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
    )
    store = _get_store(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=str(store_id),
    )

    bc_hint = options_payload.get("bc_id")
    bc_id = _normalize_identifier(bc_hint) or store.bc_id or advertiser.bc_id
    _validate_bc_alignment(
        db=db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        expected_bc_id=bc_id,
        advertiser=advertiser,
        store=store,
    )

    _enforce_products_limits(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
    )

    raw_eligibility = (
        options_payload.pop("product_eligibility", None)
        or options_payload.pop("eligibility", None)
        or body.product_eligibility
        or "gmv_max"
    )
    product_eligibility = str(raw_eligibility).strip().lower()
    if product_eligibility not in {"gmv_max", "ads", "all"}:
        raise APIError(
            "INVALID_PRODUCT_ELIGIBILITY",
            "product_eligibility must be one of gmv_max, ads, all.",
            status.HTTP_400_BAD_REQUEST,
        )

    params = {
        "mode": normalized_mode,
        "advertiser_id": str(advertiser_id),
        "store_id": str(store_id),
        "product_eligibility": product_eligibility,
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
            idempotency_key=requested_idempotency,
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
        idempotency_key=result.run.idempotency_key,
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


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


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
        "name": _as_str(name),
        "status": _as_str(status),
        "timezone": _as_str(timezone),
        "country_code": _as_str(country_code),
        "owner_user_id": _as_str(row.owner_user_id),
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _serialize_adv(row: TTBAdvertiser) -> Dict[str, Any]:
    return {
        "advertiser_id": row.advertiser_id,
        "bc_id": _as_str(row.bc_id),
        "name": _as_str(row.name),
        "display_name": _as_str(row.display_name),
        "status": _as_str(row.status),
        "industry": _as_str(row.industry),
        "currency": _as_str(row.currency),
        "timezone": _as_str(row.timezone),
        "display_timezone": _as_str(row.display_timezone),
        "country_code": _as_str(row.country_code),
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _serialize_store(row: TTBStore) -> Dict[str, Any]:
    return {
        "store_id": row.store_id,
        "advertiser_id": _as_str(row.advertiser_id),
        "bc_id": _as_str(row.bc_id),
        "name": _as_str(row.name),
        "status": _as_str(row.status),
        "region_code": _as_str(row.region_code),
        "store_type": _as_str(getattr(row, "store_type", None) or ""),
        "store_code": _as_str(getattr(row, "store_code", None) or ""),
        "store_authorized_bc_id": _as_str(getattr(row, "store_authorized_bc_id", None) or ""),
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "raw": row.raw_json,
    }


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _format_price_range(currency: Optional[str], low: Optional[Decimal], high: Optional[Decimal]) -> Optional[str]:
    if low is None and high is None:
        return None

    def _fmt(val: Decimal) -> str:
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val)

    label_currency = (currency or "").strip()
    if low is not None and high is not None:
        if label_currency:
            return f"{label_currency} {_fmt(low)} - {_fmt(high)}"
        return f"{_fmt(low)} - {_fmt(high)}"
    chosen = low if low is not None else high
    if chosen is None:
        return None
    if label_currency:
        return f"{label_currency} {_fmt(chosen)}"
    return _fmt(chosen)


def _extract_product_sku_count(raw: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(raw, dict):
        return None
    for key in ("sku_count", "skuCount"):
        value = raw.get(key)
        if value is not None:
            try:
                count = int(value)
                if count >= 0:
                    return count
            except (TypeError, ValueError):
                continue
    for key in ("skus", "sku_list", "skuList", "sku_infos", "skuInfos"):
        value = raw.get(key)
        if isinstance(value, list):
            return len([item for item in value if item is not None])
    return None


def _extract_product_price_range(row: TTBProduct) -> Optional[str]:
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    currency = _as_str(raw.get("currency")) or _as_str(row.currency)
    candidates: list[Optional[Decimal]] = []

    price_range = raw.get("price_range") or raw.get("priceRange")
    if isinstance(price_range, dict):
        candidates.append(_to_decimal(price_range.get("min")))
        candidates.append(_to_decimal(price_range.get("min_price")))
        candidates.append(_to_decimal(price_range.get("minPrice")))
        high_candidates = [
            _to_decimal(price_range.get("max")),
            _to_decimal(price_range.get("max_price")),
            _to_decimal(price_range.get("maxPrice")),
        ]
        low_value = next((c for c in candidates if c is not None), None)
        high_value = next((c for c in high_candidates if c is not None), None)
        if low_value is not None or high_value is not None:
            return _format_price_range(currency, low_value, high_value)
    elif isinstance(price_range, (list, tuple)):
        values = [_to_decimal(v) for v in price_range]
        if values:
            low_value = next((v for v in values if v is not None), None)
            high_value = next((v for v in reversed(values) if v is not None), None)
            if low_value is not None or high_value is not None:
                return _format_price_range(currency, low_value, high_value)
    elif isinstance(price_range, str):
        cleaned = price_range.strip()
        if cleaned:
            return cleaned

    low = None
    high = None
    for key in ("min_price", "minPrice", "price", "sale_price", "salePrice"):
        val = _to_decimal(raw.get(key))
        if val is not None:
            low = val
            break
    for key in ("max_price", "maxPrice", "original_price", "originalPrice"):
        val = _to_decimal(raw.get(key))
        if val is not None:
            high = val
            break
    if low is None and high is None:
        low = _to_decimal(row.price)
    return _format_price_range(currency, low, high)


def _extract_product_updated_time(row: TTBProduct) -> Optional[str]:
    if row.ext_updated_time:
        return row.ext_updated_time.isoformat()
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    for key in ("updated_time", "update_time", "modify_time", "updateTime", "updatedTime"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _serialize_product(row: TTBProduct) -> Dict[str, Any]:
    price_range = _extract_product_price_range(row)
    sku_count = _extract_product_sku_count(row.raw_json if isinstance(row.raw_json, dict) else {})
    updated_time = _extract_product_updated_time(row)
    return {
        "product_id": row.product_id,
        "store_id": _as_str(row.store_id),
        "title": _as_str(row.title),
        "status": _as_str(row.status),
        "sku_count": sku_count,
        "price_range": price_range,
        "updated_time": updated_time,
        "currency": _as_str(row.currency),
        "price": float(row.price) if row.price is not None else None,
        "stock": int(row.stock) if row.stock is not None else None,
        "ext_created_time": row.ext_created_time.isoformat() if row.ext_created_time else None,
        "ext_updated_time": row.ext_updated_time.isoformat() if row.ext_updated_time else None,
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


def _collect_bc_candidates(*values: Any) -> set[str]:
    candidates: set[str] = set()
    for value in values:
        normalized = _normalize_nullable_str(value)
        if normalized:
            candidates.add(normalized)
    return candidates


def _resolve_advertiser_bc_candidates(
    db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str
) -> tuple[set[str], Optional[str]]:
    rows = (
        db.query(TTBBCAdvertiserLink.bc_id, TTBBCAdvertiserLink.relation_type)
        .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
        .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
        .filter(TTBBCAdvertiserLink.advertiser_id == str(advertiser_id))
        .all()
    )
    candidates: set[str] = set()
    best_rank: Optional[int] = None
    preferred: Optional[str] = None
    for bc_id, relation in rows:
        normalized = _normalize_nullable_str(bc_id)
        if not normalized:
            continue
        candidates.add(normalized)
        rank = _relation_rank(relation)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            preferred = normalized
    return candidates, preferred


def _resolve_store_bc_candidates(
    db: Session, *, workspace_id: int, auth_id: int, store_id: str
) -> tuple[set[str], Optional[str]]:
    rows = (
        db.query(
            TTBAdvertiserStoreLink.store_authorized_bc_id,
            TTBAdvertiserStoreLink.bc_id_hint,
        )
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.store_id == str(store_id))
        .all()
    )
    candidates: set[str] = set()
    best_priority: Optional[int] = None
    preferred: Optional[str] = None
    for authorized_bc, bc_hint in rows:
        for priority, value in enumerate((authorized_bc, bc_hint)):
            normalized = _normalize_nullable_str(value)
            if not normalized:
                continue
            candidates.add(normalized)
            if best_priority is None or priority < best_priority:
                best_priority = priority
                preferred = normalized
    return candidates, preferred


def _validate_bc_alignment(
    *,
    db: Session,
    workspace_id: int,
    auth_id: int,
    expected_bc_id: Optional[str],
    advertiser: TTBAdvertiser,
    store: TTBStore,
) -> None:
    normalized_expected = _normalize_nullable_str(expected_bc_id)

    advertiser_direct = _normalize_nullable_str(advertiser.bc_id)
    store_direct = _normalize_nullable_str(store.bc_id)
    store_authorized = _normalize_nullable_str(store.store_authorized_bc_id)

    adv_link_candidates, adv_preferred = _resolve_advertiser_bc_candidates(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser.advertiser_id),
    )
    store_link_candidates, store_preferred = _resolve_store_bc_candidates(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=str(store.store_id),
    )

    advertiser_candidates = _collect_bc_candidates(advertiser_direct, *adv_link_candidates)
    store_candidates = _collect_bc_candidates(store_direct, store_authorized, *store_link_candidates)

    advertiser_primary = advertiser_direct or adv_preferred
    store_primary = store_direct or store_authorized or store_preferred

    if normalized_expected:
        if advertiser_candidates and normalized_expected not in advertiser_candidates:
            raise APIError(
                "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
                "Advertiser belongs to a different business center.",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if store_candidates and normalized_expected not in store_candidates:
            raise APIError(
                "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
                "Store belongs to a different business center.",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    if advertiser_primary and store_direct and advertiser_primary != store_direct:
        raise APIError(
            "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
            "Advertiser and store are not linked to the same business center.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if advertiser_primary and store_authorized and advertiser_primary != store_authorized:
        raise APIError(
            "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
            "Advertiser and store are not linked to the same business center.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if normalized_expected and store_primary and normalized_expected != store_primary:
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
    request: Request,
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _legacy_disabled()
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )


@router.get(
    "/{workspace_id}/providers/{provider}/stores",
    response_model=PagedResult,
)
def list_stores(
    workspace_id: int,
    provider: str,
    request: Request,
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=200),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _legacy_disabled()
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )


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
    request: Request,
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    query = (
        db.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
    )
    normalized_owner = _normalize_identifier(owner_bc_id)
    if normalized_owner:
        link_subquery = (
            db.query(TTBBCAdvertiserLink.advertiser_id)
            .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
            .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
            .filter(TTBBCAdvertiserLink.bc_id == normalized_owner)
        )
        query = query.filter(
            or_(TTBAdvertiser.advertiser_id.in_(link_subquery), TTBAdvertiser.bc_id == normalized_owner)
        )
    rows = query.order_by(TTBAdvertiser.display_name.asc(), TTBAdvertiser.advertiser_id.asc()).all()

    advertiser_ids = [str(row.advertiser_id) for row in rows if row and row.advertiser_id]
    bc_hints: dict[str, tuple[int, str]] = {}
    if advertiser_ids:
        link_rows = (
            db.query(
                TTBBCAdvertiserLink.advertiser_id,
                TTBBCAdvertiserLink.bc_id,
                TTBBCAdvertiserLink.relation_type,
            )
            .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
            .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
            .filter(TTBBCAdvertiserLink.advertiser_id.in_(advertiser_ids))
            .all()
        )
        for adv_id, bc_id, relation_type in link_rows:
            if not adv_id or not bc_id:
                continue
            key = str(adv_id)
            rank = _relation_rank(relation_type)
            existing = bc_hints.get(key)
            if existing is None or rank < existing[0]:
                bc_hints[key] = (rank, str(bc_id))

    items: list[AdvertiserItem] = []
    for row in rows:
        payload = _serialize_adv(row)
        adv_id = payload.get("advertiser_id")
        if adv_id:
            hint = bc_hints.get(str(adv_id))
            if hint and not payload.get("bc_id"):
                payload["bc_id"] = hint[1]
        items.append(AdvertiserItem(**payload))
    return AdvertiserList(items=items)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/stores",
    response_model=StoreList,
)
def list_account_stores(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    advertiser_id: str = Query(..., max_length=64),
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    store_authorized_bc_id: Optional[str] = Query(default=None, max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)
    if request and "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    normalized_adv = _normalize_identifier(advertiser_id)
    query = (
        db.query(TTBStore)
        .filter(TTBStore.workspace_id == int(workspace_id))
        .filter(TTBStore.auth_id == int(auth_id))
        .filter(TTBStore.advertiser_id == normalized_adv)
    )
    normalized_owner_bc = _normalize_identifier(owner_bc_id)
    normalized_authorized_filter = _normalize_identifier(store_authorized_bc_id)
    if normalized_authorized_filter:
        query = query.filter(TTBStore.store_authorized_bc_id == normalized_authorized_filter)

    rows = query.order_by(TTBStore.name.asc(), TTBStore.store_id.asc()).all()

    link_rows = (
        db.query(
            TTBAdvertiserStoreLink.store_id,
            TTBAdvertiserStoreLink.relation_type,
            TTBAdvertiserStoreLink.store_authorized_bc_id,
            TTBAdvertiserStoreLink.bc_id_hint,
        )
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)
        .all()
    )

    link_map: dict[str, dict[str, Optional[str]]] = {}
    for store_id, relation_type, authorized_bc, bc_hint in link_rows:
        if not store_id:
            continue
        link_map[str(store_id)] = {
            "relation_type": relation_type,
            "store_authorized_bc_id": _normalize_identifier(authorized_bc),
            "bc_id_hint": _normalize_identifier(bc_hint),
        }

    items_with_priority: list[tuple[int, Dict[str, Any]]] = []
    for row in rows:
        payload = _serialize_store(row)
        store_id = payload.get("store_id")
        link_info = link_map.get(str(store_id)) if store_id else None
        if link_info:
            if link_info.get("store_authorized_bc_id") and not payload.get("store_authorized_bc_id"):
                payload["store_authorized_bc_id"] = link_info["store_authorized_bc_id"]
            if link_info.get("bc_id_hint") and not payload.get("bc_id"):
                payload["bc_id"] = link_info["bc_id_hint"]

        authorized_match = False
        if normalized_owner_bc:
            owner_value = normalized_owner_bc
            candidates = [
                _normalize_identifier(payload.get("store_authorized_bc_id")),
                _normalize_identifier(payload.get("bc_id")),
            ]
            if link_info:
                candidates.append(link_info.get("store_authorized_bc_id"))
                candidates.append(link_info.get("bc_id_hint"))
            authorized_match = any(
                candidate and str(candidate) == owner_value for candidate in candidates if candidate
            )

        priority = 0 if authorized_match else 1
        items_with_priority.append((priority, payload))

    items_with_priority.sort(
        key=lambda item: (
            item[0],
            (item[1].get("name") or "").lower(),
            item[1].get("store_id") or "",
        )
    )

    return StoreList(items=[StoreItem(**payload) for _, payload in items_with_priority])


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/products",
    response_model=ProductList,
)
def list_account_products(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: str = Query(..., max_length=64),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(200, ge=1, le=500),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)

    base_query = (
        db.query(TTBProduct)
        .filter(TTBProduct.workspace_id == int(workspace_id))
        .filter(TTBProduct.auth_id == int(auth_id))
        .filter(TTBProduct.store_id == str(store_id))
    )

    total = base_query.count()
    offset = (page - 1) * page_size
    rows = (
        base_query.order_by(TTBProduct.title.asc(), TTBProduct.product_id.asc())
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return ProductList(
        items=[ProductItem(**_serialize_product(r)) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmv-max/options",
)
async def get_gmv_max_options(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    refresh: int = Query(default=0, ge=0, le=1),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    """Return GMV Max binding options with optional refresh and ETag support."""
    _normalize_provider(provider)
    _ensure_account(db, workspace_id, auth_id)

    refresh_requested = bool(refresh)

    cursor_state = get_meta_cursor_state(db, workspace_id=workspace_id, auth_id=auth_id)
    etag = compute_meta_etag(cursor_state.revisions)
    request_etag = _normalize_if_none_match(request.headers.get("if-none-match"))

    if not refresh_requested and request_etag and request_etag == etag:
        response = Response(status_code=status.HTTP_304_NOT_MODIFIED)
        response.headers["ETag"] = f'"{etag}"'
        return response

    refresh_status: Optional[str] = None
    idempotency_key: Optional[str] = None
    task_name: Optional[str] = None
    if refresh_requested:
        try:
            result = enqueue_meta_sync(workspace_id=int(workspace_id), auth_id=int(auth_id))
            idempotency_key = result.idempotency_key
            task_name = result.task_name
            logger.info(
                "gmv max options refresh enqueued",
                extra={
                    "provider": "tiktok-business",
                    "workspace_id": int(workspace_id),
                    "auth_id": int(auth_id),
                    "idempotency_key": idempotency_key,
                    "task_name": task_name,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to enqueue meta refresh",
                extra={
                    "provider": "tiktok-business",
                    "workspace_id": int(workspace_id),
                    "auth_id": int(auth_id),
                    "idempotency_key": idempotency_key,
                    "task_name": task_name,
                },
            )

        cursor_state, changed = await _poll_for_meta_refresh(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            initial_state=cursor_state,
            initial_etag=etag,
        )
        new_etag = compute_meta_etag(cursor_state.revisions)
        if new_etag != etag:
            etag = new_etag
        if not changed:
            refresh_status = "timeout"
        logger.info(
            "gmv max options refresh polled",
            extra={
                "provider": "tiktok-business",
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "idempotency_key": idempotency_key,
                "task_name": task_name,
                "refresh_changed": changed,
            },
        )

    db.expire_all()
    payload = build_gmvmax_options(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        fallback_synced_at=cursor_state.updated_at,
    )
    if refresh_status:
        payload["refresh"] = refresh_status
        if refresh_status == "timeout" and idempotency_key:
            payload["idempotency_key"] = idempotency_key

    response = JSONResponse(payload)
    response.headers["ETag"] = f'"{etag}"'
    return response


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
    try:
        row = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    except BindingConfigStorageNotReady as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GMV Max binding configuration storage is not initialized; please run database migrations.",
        ) from exc
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
    _validate_bc_alignment(
        db=db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        expected_bc_id=bc.bc_id,
        advertiser=advertiser,
        store=store,
    )

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
    except BindingConfigStorageNotReady as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GMV Max binding configuration storage is not initialized; please run database migrations.",
        ) from exc
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

