from __future__ import annotations

import asyncio
import logging
"""Tenant GMV Max provider-scoped router definitions (router layer)."""

from collections import OrderedDict
from decimal import Decimal
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, status
from celery.result import AsyncResult
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.celery_app import (
    GMVMAX_SYNC_INTERVAL_OPTIONS,
    celery_app,
    get_gmvmax_sync_interval,
    set_gmvmax_sync_interval,
)
from app.data.db import get_db
from app.data.models.ttb_entities import (
    TTBAdvertiser,
    TTBAdvertiserStoreLink,
    TTBBCAdvertiserLink,
    TTBProduct,
)
from app.data.models.gmv_restructured import (
    GmvActionLog,
    GmvCampaign,
    GmvCampaignMetricsDaily,
    GmvCampaignProduct,
    GmvCreativeMetricsDaily,
)
from app.data.repositories.tiktok_business.gmvmax_heating import (
    list_heating_configs,
    update_heating_action_result,
    upsert_creative_heating,
)
from app.data.repositories.tiktok_business.gmvmax_metrics import GMVMaxMetricDTO
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMaxBidRecommendRequest,
    GMVMaxCampaign,
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignInfoRequest,
    GMVMaxCampaignListData,
    GMVMaxCampaignActionApplyBody,
    GMVMaxCampaignActionApplyRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxExclusiveAuthorizationGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxReportData,
    GMVMaxReportEntry,
    GMVMaxResponse,
    PageInfo,
    GMVMaxSessionListData,
    GMVMaxSessionListRequest,
    GMVMaxSession,
    GMVMaxSessionProduct,
    GMVMaxSessionSettings,
    GMVMaxSessionUpdateBody,
    GMVMaxSessionUpdateRequest,
    GMVMaxStoreAdUsageCheckRequest,
    GMVMaxStoreListRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_api import TTBApiError, TTBBusinessError, TTBHttpError
from app.services.ttb_binding_config import (
    BindingConfigStorageNotReady,
    get_binding_config,
    upsert_binding_config,
)
from app.services.provider_registry import provider_registry
from app.services.gmvmax_heating_actions import apply_boost_creative_action
from app.services.gmvmax_creative_metrics import latest_creative_metrics_snapshots
from app.services.ttb_sync import _normalize_identifier
from app.tasks.ttb_sync_tasks import task_sync_products

from ._helpers import (
    GMVMaxAccountBinding,
    get_gmvmax_client_for_account,
    normalize_provider,
    resolve_account_binding,
)
from app.services.gmvmax_spec import (
    GMVMAX_DEFAULT_DIMENSIONS,
    GMVMAX_DEFAULT_METRICS,
    GMVMAX_METRIC_ALIASES,
    GMVMAX_SUPPORTED_DIMENSIONS,
    GMVMAX_SUPPORTED_METRICS,
    GMV_REPORT_CONFIG,
    GMVMaxReportLevel,
)
from .service import _ensure_provider, list_action_logs
from app.services.ttb_gmvmax import (
    _extract_item_group_ids_from_payload,
    _sanitize_id_list,
    build_gmvmax_anchor_params,
    create_gmvmax_campaign,
    ensure_gmvmax_store_authorized,
    log_campaign_action,
    resolve_store_id_from_page_context,
    upsert_campaign_from_api,
    update_gmvmax_campaign,
)

from .schemas import (
    ActionLogEntry,
    AsyncTaskResponse,
    AutoBindingCandidate,
    AutoBindingRequest,
    AutoBindingResponse,
    BalanceSyncRequest,
    BindingStatusResponse,
    CampaignActionRequest,
    CampaignActionResponse,
    CampaignDetailResponse,
    CampaignFilter,
    CampaignListOptions,
    CampaignListResponse,
    CreateCampaignRequest,
    CreativeHeatingActionRequest,
    CreativeHeatingListResponse,
    CreativeHeatingActionResponse,
    CreativeHeatingRecord,
    DEFAULT_PROMOTION_TYPES,
    GMVMaxCampaignInfoData,
    GMVMaxPrecheckRequest,
    GMVMaxPrecheckResponse,
    MetricsRequest,
    MetricsResponse,
    ReportFiltering,
    ReportRequest,
    StrategyPreviewRequest,
    StrategyPreviewResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    StrategyUpdateResponse,
    SyncIntervalResponse,
    SyncIntervalUpdateRequest,
    SyncRequest,
    SyncTaskResponse,
    SyncTaskStateResponse,
    UpdateCampaignRequest,
    normalize_datetime_to_date,
)

router = APIRouter(prefix="/gmvmax")
logger = logging.getLogger("gmv.ttb.gmvmax.router")


# === GMV Max sync cadence & task monitoring ===
# - fetch/update sync interval configuration
# - enqueue full sync jobs and check Celery task status
def _build_task_response(
    async_res: AsyncResult,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
) -> AsyncTaskResponse:
    task_id = async_res.id or ""
    status_url = (
        f"/tenants/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/tasks/{task_id}"
        if task_id
        else None
    )
    return AsyncTaskResponse(task_id=task_id, state=str(async_res.state), status_url=status_url)

_ACTION_LOG_TYPES = {
    "pause": "PAUSE",
    "enable": "START",
    "delete": "DELETE",
    "update_budget": "SET_BUDGET",
    "update_strategy": "UPDATE_STRATEGY",
}


_DEFAULT_SEED_CONVERSIONS = 3
_DEFAULT_SEED_ROAS = 2.0
_DEFAULT_SEED_SPEND = 20.0


class _TTLCache:
    """Simple per-process TTL cache for metrics queries."""

    def __init__(self, *, ttl_seconds: float, maxsize: int) -> None:
        self._ttl = float(ttl_seconds)
        self._maxsize = maxsize
        self._store: OrderedDict[tuple[Any, ...], tuple[float, MetricsResponse]] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> MetricsResponse | None:
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, value = entry
        now = monotonic()
        if expires_at <= now:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: tuple[Any, ...], value: MetricsResponse) -> None:
        expires_at = monotonic() + self._ttl
        self._store[key] = (expires_at, value)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)


_metrics_cache = _TTLCache(ttl_seconds=60.0, maxsize=200)


def _campaign_row_to_schema(row: GmvCampaign) -> GMVMaxCampaign:
    if row.raw_json:
        try:
            payload = dict(row.raw_json)
            payload.update(
                {
                    "is_deleted": bool(row.is_deleted),
                    "deleted_at": row.deleted_at,
                }
            )
            return GMVMaxCampaign.model_validate(payload)
        except Exception:  # noqa: BLE001
            logger.warning(
                "gmvmax campaign raw_json parse failed",
                exc_info=True,
                extra={"campaign_id": row.campaign_id, "workspace_id": row.workspace_id},
            )

    payload: Dict[str, Any] = {
        "campaign_id": row.campaign_id,
        "campaign_name": row.name,
        "advertiser_id": row.advertiser_id,
        "store_id": row.store_id,
        "operation_status": row.operation_status,
        "secondary_status": row.secondary_status,
        "shopping_ads_type": row.shopping_ads_type,
        "optimization_goal": row.optimization_goal,
        "roas_bid": row.roas_bid,
        "currency": row.currency,
    }
    if row.ext_created_time:
        payload["create_time"] = row.ext_created_time
    if row.ext_updated_time:
        payload["update_time"] = row.ext_updated_time
    if row.daily_budget_cents is not None:
        payload["daily_budget"] = row.daily_budget_cents
    payload["is_deleted"] = bool(row.is_deleted)
    if row.deleted_at:
        payload["deleted_at"] = row.deleted_at
    return GMVMaxCampaign.model_validate(payload)


def _campaign_row_to_detail(row: GmvCampaign) -> GMVMaxCampaignInfoData:
    """Build a detailed campaign schema directly from the persisted row."""

    fallback_campaign = _campaign_row_to_schema(row)
    return GMVMaxCampaignInfoData.model_validate(
        fallback_campaign.model_dump(exclude_none=True)
    )

def _count_products(db: Session, *, workspace_id: int, auth_id: int, store_id: str) -> tuple[int, int]:
    base_query = (
        db.query(TTBProduct)
        .filter(TTBProduct.workspace_id == int(workspace_id))
        .filter(TTBProduct.auth_id == int(auth_id))
        .filter(TTBProduct.store_id == str(store_id))
    )
    total = int(base_query.count() or 0)
    missing = int(base_query.filter(TTBProduct.gmv_max_ads_status.is_(None)).count() or 0)
    return total, missing


async def _sync_products_now(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    store_id: str,
) -> None:
    options: Dict[str, Any] = {
        "mode": "full",
        "store_id": str(store_id),
        "product_eligibility": "gmv_max",
        "advertiser_id": str(advertiser_id),
    }
    envelope = {
        "envelope_version": 1,
        "provider": context.provider,
        "scope": "products",
        "workspace_id": int(context.workspace_id),
        "auth_id": int(context.auth_id),
        "options": options,
    }
    sync_logger = logger.getChild("products")
    try:
        task = task_sync_products.apply_async(
            kwargs={
                "workspace_id": int(context.workspace_id),
                "auth_id": int(context.auth_id),
                "scope": "products",
                "params": {"envelope": envelope},
            },
            queue="gmvmax",
        )
        await asyncio.to_thread(task.get, timeout=300)
    except Exception as exc:  # noqa: BLE001
        sync_logger.exception("product sync failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="GMV Max product sync failed; please retry later.",
        ) from exc


async def _ensure_products_ready(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    store_id: str,
) -> None:
    if not advertiser_id or not store_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="GMV Max product sync requires advertiser_id and store_id.",
        )

    db = context.db
    if db is None:
        return

    attempts = 0
    total, missing = _count_products(db, workspace_id=context.workspace_id, auth_id=context.auth_id, store_id=store_id)
    while attempts < 2 and (total == 0 or missing > 0):
        attempts += 1
        await _sync_products_now(
            context, advertiser_id=advertiser_id, store_id=store_id
        )
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            raise
        db.expire_all()
        total, missing = _count_products(
            db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            store_id=store_id,
        )

    if total == 0 or missing > 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "GMV Max products are missing eligibility data. "
                "Please run the product sync again after verifying store and advertiser binding."
            ),
        )


def _resolve_actor_label(me: SessionUser | None) -> str:
    if me is None:
        return "system"
    for candidate in (me.email, me.display_name, me.username):
        if candidate:
            return str(candidate)
    return f"user:{me.id}"


def _load_campaign_row(
    context: GMVMaxRouteContext,
    campaign_id: str,
) -> GmvCampaign | None:
    db = getattr(context, "db", None)
    if db is None or not hasattr(db, "execute"):
        return None
    stmt = (
        select(GmvCampaign)
        .where(GmvCampaign.workspace_id == int(context.workspace_id))
        .where(GmvCampaign.auth_id == int(context.auth_id))
        .where(GmvCampaign.campaign_id == str(campaign_id))
    )
    return db.execute(stmt).scalars().first()


def _ensure_campaign_not_deleted(row: GmvCampaign | None) -> None:
    if row is None:
        return
    if getattr(row, "is_deleted", False):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CAMPAIGN_DELETED",
                "message": "This campaign has been deleted on TikTok and can no longer be updated.",
            },
        )


def _snapshot_campaign_state(
    campaign: GmvCampaign | None,
) -> Dict[str, Any]:
    if campaign is None:
        return {}
    return {
        "status": getattr(campaign, "status", None),
        "daily_budget_cents": getattr(campaign, "daily_budget_cents", None),
        "roas_bid": getattr(campaign, "roas_bid", None),
    }


def _log_action_entry(
    context: GMVMaxRouteContext,
    *,
    campaign_id: str,
    campaign: GmvCampaign | None,
    action: str,
    actor: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    result: str,
    reason: str | None = None,
    error_message: str | None = None,
) -> None:
    db = getattr(context, "db", None)
    if db is None or campaign is None:
        return
    try:
        log_campaign_action(
            db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            campaign=campaign,
            action=action,
            reason=reason,
            before=dict(before or {}),
            after=dict(after or {}),
            performed_by=actor,
            result=result,
            error_message=error_message,
        )
        db.flush()
    except Exception:  # noqa: BLE001
        logger.exception(
            "gmvmax campaign action log failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": campaign_id,
                "action": action,
                "result": result,
            },
        )


def _dto_to_report_entry(row: GMVMaxMetricDTO) -> GMVMaxReportEntry:
    metrics = {
        "impressions": row.impressions,
        "clicks": row.clicks,
        "cost": row.cost,
        "net_cost": row.net_cost,
        "orders": row.orders,
        "cost_per_order": row.cost_per_order,
        "gross_revenue": row.gross_revenue,
        "roi": row.roi,
        "product_impressions": row.product_impressions,
        "product_clicks": row.product_clicks,
        "product_click_rate": row.product_click_rate,
        "ad_click_rate": row.ad_click_rate,
        "ad_conversion_rate": row.ad_conversion_rate,
        "live_views": row.live_views,
        "live_follows": row.live_follows,
    }
    serialized_metrics = {k: v for k, v in metrics.items() if v is not None}
    dimensions: Dict[str, Any] = {
        "campaign_id": row.campaign_id,
        "stat_time_day": row.stat_time_day.isoformat(),
    }
    if row.store_id:
        dimensions["store_id"] = row.store_id
    return GMVMaxReportEntry(metrics=serialized_metrics, dimensions=dimensions)


def _build_metrics_response(
    items: list[GMVMaxMetricDTO], *, total: int, page: int, page_size: int
) -> MetricsResponse:
    entries = [_dto_to_report_entry(item) for item in items]
    has_more = page_size > 0 and page * page_size < total
    total_page = ceil(total / page_size) if page_size else None
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_number=total,
        total_page=total_page,
        has_more=has_more,
        has_next=has_more,
    )
    report = GMVMaxReportData(list=entries, page_info=page_info, summary=None)
    return MetricsResponse(report=report, request_id=None)


def _build_creative_metrics_response(
    *,
    rows: Sequence[Any],
    start: date,
    end: date,
    page: int,
    page_size: int,
    total: int,
    seed_min_conversions: int,
    seed_min_roas: float,
    seed_min_spend: float,
) -> MetricsResponse:
    entries: list[GMVMaxReportEntry] = []
    for row in rows:
        raw = getattr(row, "raw_metrics", None) or {}
        creative_id = str(getattr(row, "creative_id", "") or raw.get("item_id") or "")
        if not creative_id:
            continue
        metrics_data: dict[str, Any] = dict(raw)
        metrics = {
            "title": metrics_data.get("title"),
            "item_id": metrics_data.get("item_id") or getattr(row, "item_id", None) or creative_id,
            "tt_account_authorization_type": metrics_data.get("tt_account_authorization_type"),
            "shop_content_type": metrics_data.get("shop_content_type"),
            "creative_delivery_status": metrics_data.get("creative_delivery_status")
            or getattr(row, "creative_status", None),
            "cost": metrics_data.get("cost") or getattr(row, "cost", None),
            "orders": metrics_data.get("orders") or getattr(row, "orders", None),
            "cost_per_order": metrics_data.get("cost_per_order"),
            "gross_revenue": metrics_data.get("gross_revenue")
            or getattr(row, "gross_revenue", None),
            "roi": metrics_data.get("roi") or getattr(row, "roi", None),
            "product_impressions": metrics_data.get("product_impressions"),
            "product_clicks": metrics_data.get("product_clicks"),
            "product_click_rate": metrics_data.get("product_click_rate"),
            "ad_click_rate": metrics_data.get("ad_click_rate") or getattr(row, "ad_click_rate", None),
            "ad_conversion_rate": metrics_data.get("ad_conversion_rate")
            or getattr(row, "ad_conversion_rate", None),
            "ad_video_view_rate_2s": metrics_data.get("ad_video_view_rate_2s")
            or getattr(row, "ad_video_view_rate_2s", None),
            "ad_video_view_rate_6s": metrics_data.get("ad_video_view_rate_6s")
            or getattr(row, "ad_video_view_rate_6s", None),
            "ad_video_view_rate_p25": metrics_data.get("ad_video_view_rate_p25")
            or getattr(row, "ad_video_view_rate_p25", None),
            "ad_video_view_rate_p50": metrics_data.get("ad_video_view_rate_p50")
            or getattr(row, "ad_video_view_rate_p50", None),
            "ad_video_view_rate_p75": metrics_data.get("ad_video_view_rate_p75")
            or getattr(row, "ad_video_view_rate_p75", None),
            "ad_video_view_rate_p100": metrics_data.get("ad_video_view_rate_p100")
            or getattr(row, "ad_video_view_rate_p100", None),
        }
        serialized_metrics = {k: v for k, v in metrics.items() if v is not None}
        dimensions = {
            "campaign_id": getattr(row, "campaign_id", None),
            "item_group_id": metrics_data.get("item_group_id")
            or getattr(row, "item_group_id", None),
            "item_id": metrics.get("item_id"),
            "stat_time_day": getattr(row, "stat_time_day", None) or end,
        }
        entries.append(GMVMaxReportEntry(metrics=serialized_metrics, dimensions=dimensions))

    has_more = page_size > 0 and page * page_size < total
    total_page = ceil(total / page_size) if page_size else None
    page_info = PageInfo(
        page=page,
        page_size=page_size,
        total_number=total,
        total_page=total_page,
        has_more=has_more,
        has_next=has_more,
    )
    report = GMVMaxReportData(list=entries, page_info=page_info, summary=None)
    return MetricsResponse(report=report, request_id=None)


def _normalize_metrics_list(metrics: Optional[Sequence[str]]) -> List[str]:
    """Return canonical metric names accepted by TikTok."""

    source = metrics or GMVMAX_DEFAULT_METRICS
    normalized: List[str] = []
    seen: set[str] = set()
    invalid: List[str] = []
    for metric in source:
        if not metric:
            continue
        canonical = GMVMAX_METRIC_ALIASES.get(metric, metric)
        if canonical not in GMVMAX_SUPPORTED_METRICS:
            invalid.append(metric)
            continue
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_metric",
                "message": "Unsupported GMV Max metrics provided.",
                "details": {
                    "invalid": invalid,
                    "allowed": sorted(GMVMAX_SUPPORTED_METRICS),
                },
            },
        )
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "empty_metrics",
                "message": "At least one GMV Max metric is required.",
            },
        )
    return normalized


def _normalize_dimensions_list(dimensions: Optional[Sequence[str]]) -> List[str]:
    """Return canonical dimension names accepted by TikTok."""

    source = dimensions or GMVMAX_DEFAULT_DIMENSIONS
    normalized: List[str] = []
    seen: set[str] = set()
    invalid: List[str] = []
    for dimension in source:
        if not dimension:
            continue
        canonical = dimension
        if canonical not in GMVMAX_SUPPORTED_DIMENSIONS:
            invalid.append(dimension)
            continue
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_dimension",
                "message": "Unsupported GMV Max dimensions provided.",
                "details": {
                    "invalid": invalid,
                    "allowed": sorted(GMVMAX_SUPPORTED_DIMENSIONS),
                },
            },
        )
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "empty_dimensions",
                "message": "At least one GMV Max dimension is required.",
            },
        )
    return normalized


def _resolve_report_level(level: Any) -> GMVMaxReportLevel:
    if level is None:
        return GMVMaxReportLevel.CAMPAIGN
    try:
        return GMVMaxReportLevel(str(level))
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unsupported_level", "message": "Unsupported GMV Max report level."},
        ) from exc


def _validate_date_range_for_level(
    *, start_date: date, end_date: date, level: GMVMaxReportLevel
) -> None:
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_date_range",
                "message": "end_date must not be earlier than start_date.",
            },
        )

    config = GMV_REPORT_CONFIG.get(level)
    if not config:
        return
    max_range = config.get("max_range")
    if max_range and (end_date - start_date) > max_range:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "range_too_large",
                "message": (
                    f"{level.value} reports support up to {max_range.days} days for the selected granularity"
                ),
            },
        )


@dataclass(slots=True)
class GMVMaxRouteContext:
    """Per-request context containing the TikTok client and binding metadata."""

    workspace_id: int
    provider: str
    auth_id: int
    advertiser_id: Optional[str]
    store_id: Optional[str]
    binding: GMVMaxAccountBinding
    client: TikTokBusinessGMVMaxClient
    db: Session


async def _handle_tiktok_error(exc: Exception) -> None:
    if isinstance(exc, TTBApiError):
        detail: Dict[str, Any] = {
            "code": "tiktok_error",
            "message": str(exc),
            "details": {},
        }
        if exc.code is not None:
            detail["details"]["code"] = exc.code
        if exc.payload is not None:
            detail["details"]["payload"] = exc.payload
        raise HTTPException(
            status_code=exc.status or status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        ) from exc
    if isinstance(exc, TTBHttpError):
        detail = {
            "code": "tiktok_http_error",
            "message": str(exc),
            "details": {"status": exc.status},
        }
        if exc.payload is not None:
            detail["details"]["payload"] = exc.payload
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "tiktok_error", "message": str(exc)},
    ) from exc


def _extract_request_payload(args: Sequence[Any], kwargs: Dict[str, Any]) -> Any:
    for candidate in list(args) + list(kwargs.values()):
        if hasattr(candidate, "model_dump"):
            try:
                return candidate.model_dump(exclude_none=True)
            except Exception:  # pragma: no cover - defensive
                return str(candidate)
        if isinstance(candidate, dict):
            return candidate
    return None


async def _call_tiktok(
    func: Callable[..., Awaitable[GMVMaxResponse[Any]]],
    *args: Any,
    _log_context: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> GMVMaxResponse[Any]:
    try:
        return await func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        payload = _extract_request_payload(args, kwargs)
        endpoint_name = getattr(func, "__name__", repr(func))
        extra: Dict[str, Any] = {
            "gmvmax_endpoint": endpoint_name,
            "gmvmax_request_payload": payload,
        }
        if _log_context is not None:
            extra["gmvmax_context"] = _log_context
        logger.warning(
            "tiktok gmv max request failed",
            exc_info=True,
            extra=extra,
        )
        await _handle_tiktok_error(exc)


def _normalize_status(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _should_include_campaign(entry: GMVMaxCampaign) -> bool:
    operation_status = _normalize_status(entry.operation_status)
    if operation_status == "DELETE":
        return False
    secondary_status = _normalize_status(entry.secondary_status)
    if secondary_status == "CAMPAIGN_STATUS_DELETE":
        return False
    return True


def _filter_campaign_entries(entries: Optional[Sequence[GMVMaxCampaign | Dict[str, Any]]]) -> List[GMVMaxCampaign]:
    if not entries:
        return []
    filtered: List[GMVMaxCampaign] = []
    for entry in entries:
        campaign: Optional[GMVMaxCampaign]
        if isinstance(entry, GMVMaxCampaign):
            campaign = entry
        elif isinstance(entry, dict):
            try:
                campaign = GMVMaxCampaign.model_validate(entry)
            except Exception:  # noqa: BLE001
                continue
        else:
            continue
        if _should_include_campaign(campaign):
            filtered.append(campaign)
    return filtered


async def _fetch_campaign_info_payload(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    campaign_id: str,
) -> Mapping[str, Any] | None:
    client = getattr(context, "client", None)
    if client is None:
        return None
    try:
        response = await _call_tiktok(
            client.gmv_max_campaign_info,
            GMVMaxCampaignInfoRequest(
                advertiser_id=str(advertiser_id), campaign_id=str(campaign_id)
            ),
        )
    except HTTPException:
        logger.warning(
            "gmvmax campaign info lookup failed",
            exc_info=True,
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
            },
        )
        return None
    payload = getattr(response, "data", None)
    if payload is None:
        return None
    if hasattr(payload, "model_dump"):
        try:
            return payload.model_dump(exclude_none=False)
        except Exception:  # pragma: no cover - defensive
            return None
    if isinstance(payload, Mapping):
        return dict(payload)
    return None


async def _persist_campaign_relations(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    response: GMVMaxResponse[GMVMaxCampaignListData],
    store_scope: Optional[str],
) -> None:
    db = getattr(context, "db", None)
    if db is None:
        return
    data = response.data
    if not data or not data.list:
        return
    page_context: Dict[str, Any] = {}
    if data.links:
        page_context["links"] = data.links
    if data.stores:
        page_context["stores"] = data.stores
    seen: set[str] = set()
    for entry in data.list:
        if isinstance(entry, GMVMaxCampaign):
            payload = entry.model_dump(exclude_none=False)
        elif isinstance(entry, dict):
            payload = dict(entry)
        else:
            continue
        campaign_identifier = payload.get("campaign_id") or payload.get("id")
        if not campaign_identifier:
            continue
        campaign_id = str(campaign_identifier)
        if campaign_id in seen:
            continue
        seen.add(campaign_id)
        campaign_details: Mapping[str, Any] | None = None
        try:
            store_hint = resolve_store_id_from_page_context(
                advertiser_id=str(advertiser_id),
                campaign_payload=payload,
                page_context=page_context,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to resolve store_id for campaign page entry",
                exc_info=True,
                extra={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                },
            )
            store_hint = None
        if not store_hint:
            campaign_details = await _fetch_campaign_info_payload(
                context,
                advertiser_id=str(advertiser_id),
                campaign_id=campaign_id,
            )
            if campaign_details:
                store_hint = (
                    campaign_details.get("store_id")
                    or campaign_details.get("shop_id")
                    or None
                )
        if not store_hint:
            store_hint = store_scope
        try:
            upsert_campaign_from_api(
                db,
                workspace_id=context.workspace_id,
                auth_id=context.auth_id,
                advertiser_id=str(advertiser_id),
                payload=payload,
                store_id_hint=store_hint,
                campaign_details=campaign_details,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to persist gmvmax campaign page entry",
                exc_info=True,
                extra={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "advertiser_id": advertiser_id,
                    "campaign_id": campaign_id,
                },
            )


async def _refresh_campaign_snapshot(
    context: GMVMaxRouteContext,
    *,
    advertiser_id: str,
    campaign_id: str,
    store_hint: Optional[str] = None,
) -> None:
    """Fetch the latest campaign info and persist it locally."""

    db = getattr(context, "db", None)
    if db is None:
        return
    try:
        response = await _call_tiktok(
            context.client.gmv_max_campaign_info,
            GMVMaxCampaignInfoRequest(
                advertiser_id=str(advertiser_id),
                campaign_id=str(campaign_id),
            ),
        )
    except HTTPException:
        logger.warning(
            "gmvmax campaign refresh failed",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": campaign_id,
            },
            exc_info=True,
        )
        return

    try:
        info_payload = response.data.model_dump(exclude_none=False)
        upsert_campaign_from_api(
            db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            advertiser_id=str(advertiser_id),
            payload=info_payload,
            store_id_hint=store_hint or context.store_id,
            campaign_details=info_payload,
        )
        db.flush()
    except Exception:  # noqa: BLE001
        logger.warning(
            "failed to persist refreshed gmvmax campaign",
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": campaign_id,
            },
            exc_info=True,
        )


def _build_campaign_request(
    advertiser_id: str,
    filtering: Optional[CampaignFilter],
    options: Optional[CampaignListOptions],
    *,
    store_ids_override: Optional[Sequence[str]] = None,
) -> GMVMaxCampaignGetRequest:
    filter_obj = filtering or CampaignFilter()
    store_ids = filter_obj.store_ids
    if (not store_ids or len(store_ids) == 0) and store_ids_override:
        store_ids = [str(item) for item in store_ids_override if item]
    filtering_model = GMVMaxCampaignFiltering(
        gmv_max_promotion_types=list(filter_obj.gmv_max_promotion_types),
        store_ids=store_ids,
        campaign_ids=filter_obj.campaign_ids,
        campaign_name=filter_obj.campaign_name,
        primary_status=filter_obj.primary_status,
        creation_filter_start_time=filter_obj.creation_filter_start_time,
        creation_filter_end_time=filter_obj.creation_filter_end_time,
    )
    return GMVMaxCampaignGetRequest(
        advertiser_id=str(advertiser_id),
        filtering=filtering_model,
        fields=options.fields if options else None,
        page=options.page if options else None,
        page_size=options.page_size if options else None,
    )


def _normalize_store_ids(
    candidate: Optional[Sequence[str]],
    fallback: Optional[str],
) -> List[str]:
    if candidate and len(candidate) > 0:
        return [str(item) for item in candidate if item]
    if fallback:
        return [str(fallback)]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "missing_store", "message": "store_id is required for this operation"},
    )


def _normalize_date_value(value: Any, *, field_name: str) -> Optional[date]:
    normalized = normalize_datetime_to_date(value)
    if normalized is None:
        return None
    if isinstance(normalized, date) and not isinstance(normalized, datetime):
        return normalized
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "invalid_date_format",
            "message": f"{field_name} must be a valid date (YYYY-MM-DD)",
        },
    )


def _parse_session_products(
    items: Optional[Sequence[Dict[str, Any]]]
) -> Optional[List[GMVMaxSessionProduct]]:
    if not items:
        return None
    return [GMVMaxSessionProduct.model_validate(item) for item in items]


def _parse_session_settings(
    settings: Optional[Dict[str, Any]]
) -> Optional[GMVMaxSessionSettings]:
    if settings is None:
        return None
    return GMVMaxSessionSettings.model_validate(settings)


def _build_campaign_update_body(
    campaign_id: str,
    action_type: str,
    payload: Dict[str, Any],
) -> GMVMaxCampaignUpdateBody:
    body_payload: Dict[str, Any] = {"campaign_id": str(campaign_id)}
    if action_type == "pause":
        body_payload["operation_status"] = "STATUS_DISABLE"
    elif action_type == "enable":
        body_payload["operation_status"] = "STATUS_DELIVERY_OK"
    elif action_type == "update_budget":
        budget = payload.get("budget")
        if budget is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="payload.budget is required for update_budget",
            )
        body_payload["budget"] = float(budget)
    elif action_type == "update_strategy":
        if "roas_bid" in payload:
            body_payload["roas_bid"] = float(payload["roas_bid"])
        if "promotion_days" in payload:
            body_payload["promotion_days"] = payload["promotion_days"]
    return GMVMaxCampaignUpdateBody(**body_payload)


def _build_session_update_body(
    campaign_id: str,
    payload: Dict[str, Any],
    default_store_id: Optional[str],
) -> GMVMaxSessionUpdateBody:
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="payload.session_id is required for update_strategy",
        )
    store_id = payload.get("store_id") or default_store_id
    if store_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required for session updates"},
        )
    session_settings = _parse_session_settings(payload.get("session"))
    product_list = _parse_session_products(payload.get("product_list"))
    return GMVMaxSessionUpdateBody(
        campaign_id=str(campaign_id),
        session_id=str(session_id),
        store_id=str(store_id),
        session=session_settings,
        product_list=product_list,
    )


def _extract_item_group_ids(sessions: Sequence[GMVMaxSession]) -> List[str]:
    results: List[str] = []
    for session in sessions:
        if not session.product_list:
            continue
        for product in session.product_list:
            candidate = product.spu_id or product.item_id
            if candidate:
                results.append(str(candidate))
    return list(dict.fromkeys(results))


async def _resolve_campaign_product_ids(
    context: GMVMaxRouteContext,
    campaign: GmvCampaign | None,
    advertiser_id: str,
) -> list[str]:
    """Load product bindings for a campaign, refreshing from TikTok if needed."""

    payload: Mapping[str, Any] | None = None
    if isinstance(getattr(campaign, "raw_json", None), Mapping):
        payload = campaign.raw_json

    product_ids = _extract_item_group_ids_from_payload(payload)
    if product_ids:
        return product_ids

    try:
        response = await _call_tiktok(
            context.client.gmv_max_campaign_info,
            GMVMaxCampaignInfoRequest(
                advertiser_id=str(advertiser_id),
                campaign_id=str(getattr(campaign, "campaign_id", "")),
            ),
        )
        payload = response.data.model_dump(exclude_none=False)
        product_ids = _extract_item_group_ids_from_payload(payload)
    except HTTPException:
        logger.warning(
            "failed to fetch campaign info for product bindings",
            exc_info=True,
            extra={
                "workspace_id": context.workspace_id,
                "auth_id": context.auth_id,
                "campaign_id": getattr(campaign, "campaign_id", None),
            },
        )
        return []

    db = getattr(context, "db", None)
    if db is not None and payload is not None:
        try:
            upsert_campaign_from_api(
                db,
                workspace_id=context.workspace_id,
                auth_id=context.auth_id,
                advertiser_id=str(advertiser_id),
                payload=payload,
                store_id_hint=context.store_id,
                campaign_details=payload,
            )
            db.flush()
        except Exception:  # noqa: BLE001
            logger.warning(
                "failed to persist refreshed campaign payload",
                exc_info=True,
                extra={
                    "workspace_id": context.workspace_id,
                    "auth_id": context.auth_id,
                    "campaign_id": getattr(campaign, "campaign_id", None),
                },
            )

    return product_ids


async def _ensure_campaign_products_available(
    context: GMVMaxRouteContext,
    *,
    campaign: GmvCampaign | None,
    advertiser_id: str,
) -> None:
    """Verify that campaign product bindings are not occupied by other campaigns."""

    if campaign is None:
        return

    product_ids = await _resolve_campaign_product_ids(
        context, campaign=campaign, advertiser_id=advertiser_id
    )
    if not product_ids:
        return

    store_id = getattr(campaign, "store_id", None) or context.store_id
    if not store_id:
        return

    db = getattr(context, "db", None)
    if db is None:
        return

    conflict_stmt = (
        select(
            GmvCampaignProduct.item_group_id,
            GmvCampaignProduct.campaign_id,
        )
        .join(
            GmvCampaign,
            GmvCampaign.id == GmvCampaignProduct.campaign_pk,
        )
        .where(GmvCampaignProduct.workspace_id == int(context.workspace_id))
        .where(GmvCampaignProduct.auth_id == int(context.auth_id))
        .where(GmvCampaignProduct.store_id == str(store_id))
        .where(GmvCampaignProduct.item_group_id.in_(product_ids))
        .where(func.lower(GmvCampaign.operation_status) == "enable")
    )
    if getattr(campaign, "id", None) is not None:
        conflict_stmt = conflict_stmt.where(
            GmvCampaignProduct.campaign_pk != int(campaign.id)
        )

    conflicts = db.execute(conflict_stmt).all()
    if conflicts:
        occupied_products = sorted(
            {
                str(getattr(row, "item_group_id", None))
                for row in conflicts
                if getattr(row, "item_group_id", None) is not None
            }
        )
        conflicting_campaigns = sorted(
            {
                str(getattr(row, "campaign_id", None))
                for row in conflicts
                if getattr(row, "campaign_id", None)
            }
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "gmvmax_products_occupied",
                "message": "Existing products are occupied by other GMV Max campaigns.",
                "occupied_products": occupied_products,
                "conflicting_campaigns": conflicting_campaigns,
            },
        )


def _build_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session,
    *,
    allow_missing_advertiser: bool,
) -> GMVMaxRouteContext:
    normalized_provider = normalize_provider(provider)
    binding = resolve_account_binding(
        db,
        workspace_id,
        normalized_provider,
        auth_id,
        allow_missing_advertiser=allow_missing_advertiser,
    )
    client = get_gmvmax_client_for_account(
        db,
        workspace_id,
        normalized_provider,
        auth_id,
    )
    return GMVMaxRouteContext(
        workspace_id=workspace_id,
        provider=normalized_provider,
        auth_id=auth_id,
        advertiser_id=binding.advertiser_id,
        store_id=binding.store_id,
        binding=binding,
        client=client,
        db=db,
    )


def get_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return _build_route_context(
        workspace_id,
        provider,
        auth_id,
        db,
        allow_missing_advertiser=False,
    )


def get_optional_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    return _build_route_context(
        workspace_id,
        provider,
        auth_id,
        db,
        allow_missing_advertiser=True,
    )


def _extract_store_metadata(store: Any) -> Dict[str, Any]:
    if hasattr(store, "model_dump"):
        try:
            return store.model_dump(exclude_none=False)
        except Exception:  # noqa: BLE001 - defensive
            return {}
    if isinstance(store, dict):
        return dict(store)
    return {}


def _build_auto_binding_candidate(
    store: Any,
    *,
    advertiser_id: str,
    authorization_data: Any,
    usage_data: Any,
    request_ids: Dict[str, Optional[str]],
) -> AutoBindingCandidate | None:
    payload = _extract_store_metadata(store)
    store_id = _normalize_identifier(
        payload.get("store_id") or getattr(store, "store_id", None)
    )
    advertiser_id = _normalize_identifier(
        payload.get("advertiser_id")
        or getattr(store, "advertiser_id", None)
        or advertiser_id
    )
    bc_id = _normalize_identifier(
        payload.get("store_authorized_bc_id")
        or getattr(store, "store_authorized_bc_id", None)
    )
    if not store_id or not advertiser_id:
        return None
    auth_status = None
    if authorization_data is not None:
        auth_status = (
            getattr(authorization_data, "authorization_status", None)
            or getattr(authorization_data, "status", None)
        )
        if not auth_status and getattr(authorization_data, "is_authorized", None):
            auth_status = "EFFECTIVE"

    usage_allowed = getattr(usage_data, "promote_all_products_allowed", None)
    is_running = getattr(usage_data, "is_running_custom_shop_ads", None)

    return AutoBindingCandidate(
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        store_name=payload.get("store_name") or getattr(store, "store_name", None),
        store_authorized_bc_id=bc_id,
        authorization_status=auth_status,
        is_gmv_max_available=payload.get("is_gmv_max_available")
        or getattr(store, "is_gmv_max_available", None),
        promote_all_products_allowed=usage_allowed,
        is_running_custom_shop_ads=is_running,
        request_id=request_ids.get("authorization") or request_ids.get("usage"),
        source=payload or None,
    )


def _is_binding_candidate_ready(candidate: AutoBindingCandidate) -> bool:
    auth_status = (candidate.authorization_status or "").upper()
    auth_ok = auth_status == "EFFECTIVE"
    return bool(candidate.store_authorized_bc_id) and auth_ok


@router.get(
    "/sync-interval",
    response_model=SyncIntervalResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def get_gmvmax_sync_interval_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncIntervalResponse:
    """Return the configured GMV Max sync interval (no upstream TikTok call)."""
    interval = get_gmvmax_sync_interval()
    logger.info(
        "gmvmax.sync_interval fetched",
        extra={"workspace_id": context.workspace_id, "auth_id": context.auth_id, "interval": interval},
    )
    return SyncIntervalResponse(interval=interval, available=list(GMVMAX_SYNC_INTERVAL_OPTIONS))


@router.put(
    "/sync-interval",
    response_model=SyncIntervalResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def update_gmvmax_sync_interval_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: SyncIntervalUpdateRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncIntervalResponse:
    """Update the per-provider GMV Max sync interval used by Celery beat."""
    interval = set_gmvmax_sync_interval(int(payload.interval))
    logger.info(
        "gmvmax.sync_interval updated",
        extra={"workspace_id": context.workspace_id, "auth_id": context.auth_id, "interval": interval},
    )
    return SyncIntervalResponse(
        interval=interval,
        available=list(GMVMAX_SYNC_INTERVAL_OPTIONS),
        message="同步间隔已更新，将在下一轮生效。",
    )


@router.post(
    "/sync",
    response_model=SyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_campaigns_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: SyncRequest,
    bc_id_query: Optional[str] = Query(None, alias="bc_id"),
    owner_bc_id_query: Optional[str] = Query(None, alias="owner_bc_id"),
    advertiser_id_query: Optional[str] = Query(None, alias="advertiser_id"),
    store_id_query: Optional[str] = Query(None, alias="store_id"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncTaskResponse:
    """Enqueue GMV Max campaign sync (TikTok GET /gmv_max/campaign/get/) via Celery."""

    advertiser_id = (
        payload.advertiser_id or advertiser_id_query or context.advertiser_id
    )
    store_id = payload.store_id or store_id_query or context.store_id
    resolved_bc_id = (
        payload.owner_bc_id
        or payload.bc_id
        or owner_bc_id_query
        or bc_id_query
    )
    scope_context = {
        "bc_id": resolved_bc_id,
        "advertiser_id": advertiser_id,
        "store_id": store_id,
    }
    log_context = {
        "workspace_id": context.workspace_id,
        "auth_id": context.auth_id,
        "scope": scope_context,
    }

    filters: Dict[str, Any] = {}
    if payload.campaign_filter:
        filters.update(
            payload.campaign_filter.model_dump(exclude_none=True, by_alias=True)
        )
    if payload.campaign_options:
        filters.update(
            payload.campaign_options.model_dump(exclude_none=True, by_alias=True)
        )
    if store_id:
        filters.setdefault("store_ids", [str(store_id)])

    task_kwargs: Dict[str, Any] = {
        "workspace_id": context.workspace_id,
        "auth_id": context.auth_id,
        "advertiser_id": str(advertiser_id),
    }
    if filters:
        task_kwargs["filters"] = filters

    params_payload = payload.model_dump(exclude_none=True, by_alias=True)
    if params_payload:
        task_kwargs["params"] = params_payload

    async_res = celery_app.send_task(
        "gmvmax.sync_campaigns",
        kwargs=task_kwargs,
        queue="gmvmax",
    )
    task_id = async_res.id or ""
    logger.info(
        "gmvmax.sync enqueued", extra={**log_context, "task_id": task_id}
    )
    status_url = (
        f"/tenants/{workspace_id}/providers/{provider}/accounts/{auth_id}/gmvmax/sync/{task_id}"
        if task_id
        else None
    )

    return SyncTaskResponse(task_id=task_id, state=str(async_res.state), status_url=status_url)


@router.get(
    "/sync/{task_id}",
    response_model=SyncTaskStateResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def get_sync_task_state(
    workspace_id: int,
    provider: str,
    auth_id: int,
    task_id: str = Path(..., description="Celery task identifier"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncTaskStateResponse:
    """Return Celery task status for GMV Max sync jobs (campaign/report sync)."""

    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    state = str(res.state)
    info = res.info if isinstance(res.info, dict) else {}
    error = info.get("error") if state in {"FAILURE", "RETRY"} else None
    result = None
    if state == "SUCCESS":
        result = info.get("result") if info else res.result

    logger.info(
        "gmvmax.sync polled",
        extra={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "task_id": task_id,
            "state": state,
        },
    )

    return SyncTaskStateResponse(task_id=task_id, state=state, result=result, error=error)


@router.get(
    "/tasks/{task_id}",
    response_model=SyncTaskStateResponse,
    dependencies=[Depends(require_tenant_member)],
)
def get_async_task_state(
    workspace_id: int,
    provider: str,
    auth_id: int,
    task_id: str = Path(..., description="Celery task identifier"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncTaskStateResponse:
    """Return Celery task status for GMV Max async API fetches (report/bid preview)."""

    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    state = str(res.state)
    info = res.info if isinstance(res.info, dict) else {}
    error = info.get("error") if state in {"FAILURE", "RETRY"} else None
    result = None
    if state == "SUCCESS":
        result = info.get("result") if info else res.result

    logger.info(
        "gmvmax.async_task polled",
        extra={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "task_id": task_id,
            "state": state,
        },
    )

    return SyncTaskStateResponse(task_id=task_id, state=state, result=result, error=error)


def _upsert_store_link(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: Optional[str],
) -> None:
    row = (
        db.query(TTBAdvertiserStoreLink)
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.advertiser_id == str(advertiser_id))
        .filter(TTBAdvertiserStoreLink.store_id == str(store_id))
        .first()
    )
    if row is None:
        row = TTBAdvertiserStoreLink(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            relation_type="AUTHORIZER",
            store_authorized_bc_id=_normalize_identifier(store_authorized_bc_id),
            bc_id_hint=_normalize_identifier(store_authorized_bc_id),
            source="gmvmax.auto_bind",
        )
    else:
        if not row.relation_type:
            row.relation_type = "AUTHORIZER"
        if not row.store_authorized_bc_id:
            row.store_authorized_bc_id = _normalize_identifier(store_authorized_bc_id)
        if not row.bc_id_hint:
            row.bc_id_hint = _normalize_identifier(store_authorized_bc_id)
        if not row.source:
            row.source = "gmvmax.auto_bind"
    db.add(row)


def _dedupe_append(target: list[str], value: Optional[str], seen: set[str]) -> None:
    normalized = _normalize_identifier(value)
    if normalized and normalized not in seen:
        seen.add(normalized)
        target.append(normalized)


def _build_binding_status(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    store_id: Optional[str],
    advertiser_id: Optional[str],
    bc_id: Optional[str],
) -> BindingStatusResponse:
    normalized_store = _normalize_identifier(store_id)
    normalized_adv = _normalize_identifier(advertiser_id)
    normalized_bc = _normalize_identifier(bc_id)
    binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))

    has_binding = bool(normalized_store and normalized_adv)
    status = BindingStatusResponse(
        has_binding=has_binding,
        binding_ready=False,
        advertiser_id=normalized_adv,
        store_id=normalized_store,
        bc_id=normalized_bc,
        last_checked_at=datetime.now(timezone.utc),
    )

    if not binding or not has_binding:
        status.error_code = "binding_missing"
        status.error_message = "尚未完成 GMV Max 店铺-广告主绑定。"
        return status

    candidate = _select_binding_from_links(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        target_store=normalized_store or _normalize_identifier(binding.store_id),
        target_advertiser=normalized_adv or _normalize_identifier(binding.advertiser_id),
        target_bc=normalized_bc or _normalize_identifier(binding.bc_id),
    )

    if candidate and _is_binding_candidate_ready(candidate):
        status.binding_ready = True
        return status

    if candidate is None:
        status.error_code = "binding_not_found"
        status.error_message = "未找到匹配的 GMV Max 授权记录，请重试绑定。"
    else:
        status.error_code = "binding_not_ready"
        status.error_message = "GMV Max 授权未生效，请重试绑定。"

    return status


def _select_binding_from_links(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    target_store: Optional[str],
    target_advertiser: Optional[str],
    target_bc: Optional[str],
) -> AutoBindingCandidate | None:
    """Try to derive a single binding tuple from cached link tables.

    Prefers the most recently seen advertiser-store link that is consistent with a
    business center relationship, and only returns a candidate when the triple is
    unique after normalization and optional targeting filters.
    """

    normalized_bc = _normalize_identifier(target_bc)
    normalized_store = _normalize_identifier(target_store)
    normalized_adv = _normalize_identifier(target_advertiser)

    bc_link_rows = (
        db.query(TTBBCAdvertiserLink.bc_id, TTBBCAdvertiserLink.advertiser_id)
        .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
        .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
        .order_by(TTBBCAdvertiserLink.last_seen_at.desc())
        .all()
    )
    bc_pairs = {
        (_normalize_identifier(bc_id), _normalize_identifier(adv_id))
        for bc_id, adv_id in bc_link_rows
        if _normalize_identifier(bc_id) and _normalize_identifier(adv_id)
    }

    query = (
        db.query(TTBAdvertiserStoreLink)
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
    )
    if normalized_store:
        query = query.filter(TTBAdvertiserStoreLink.store_id == normalized_store)
    if normalized_adv:
        query = query.filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)

    rows = (
        query.order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
        .filter(TTBAdvertiserStoreLink.store_authorized_bc_id.isnot(None))
        .all()
    )

    candidates: list[AutoBindingCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        store_id = _normalize_identifier(row.store_id)
        adv_id = _normalize_identifier(row.advertiser_id)
        bc_id = _normalize_identifier(row.store_authorized_bc_id or row.bc_id_hint)
        if not store_id or not adv_id or not bc_id:
            continue
        if normalized_bc and bc_id != normalized_bc:
            continue
        if bc_pairs and (bc_id, adv_id) not in bc_pairs:
            continue
        key = (store_id, adv_id, bc_id)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            AutoBindingCandidate(
                advertiser_id=adv_id,
                store_id=store_id,
                store_authorized_bc_id=bc_id,
                authorization_status="EFFECTIVE",
                request_id=None,
                source=row.raw_json if hasattr(row, "raw_json") else None,
            )
        )

    if len(candidates) != 1:
        return None
    return candidates[0]


# === GMV Max binding discovery & eligibility ===
# - check current binding status and store/advertiser readiness
# - auto-discover bindings via TikTok store/authorization APIs
# - run precheck for identity/asset occupancy and balance sync
@router.get(
    "/binding_status",
    response_model=BindingStatusResponse,
    dependencies=[Depends(require_tenant_member)],
)
def get_gmvmax_binding_status(
    workspace_id: int,
    provider: str,
    auth_id: int,
    store_id: Optional[str] = Query(default=None),
    advertiser_id: Optional[str] = Query(default=None),
    context: GMVMaxRouteContext = Depends(get_optional_route_context),
) -> BindingStatusResponse:
    """Report GMV Max binding readiness using stored links and cached configs."""
    provider = _ensure_provider(provider)
    status = _build_binding_status(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=store_id or (context.binding.store_id if context.binding else None),
        advertiser_id=advertiser_id or (context.binding.advertiser_id if context.binding else None),
        bc_id=context.binding.bc_id if context.binding else None,
    )
    logger.info(
        "gmvmax.binding_status fetched",
        extra={
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "store_id": status.store_id,
            "advertiser_id": status.advertiser_id,
            "binding_ready": status.binding_ready,
            "has_binding": status.has_binding,
            "error_code": status.error_code,
        },
    )
    return status


@router.post(
    "/rebind_auto",
    response_model=AutoBindingResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def rebind_gmvmax_binding(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: AutoBindingRequest = Body(default_factory=AutoBindingRequest),
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_optional_route_context),
) -> AutoBindingResponse:
    """Force auto-binding using TikTok store/authorization checks and persist when allowed."""
    provider = _ensure_provider(provider)
    normalized_payload = AutoBindingRequest(
        advertiser_id=payload.advertiser_id,
        store_id=payload.store_id,
        persist=True,
    )
    response = await auto_bind_gmvmax_account(
        workspace_id,
        provider,
        auth_id,
        normalized_payload,
        me=me,
        context=context,
    )
    logger.info(
        "gmvmax.rebind_auto completed",
        extra={
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "store_id": normalized_payload.store_id,
            "advertiser_id": normalized_payload.advertiser_id,
            "persisted": response.persisted,
        },
    )
    return response


@router.post(
    "/binding/auto",
    response_model=AutoBindingResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def auto_bind_gmvmax_account(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: AutoBindingRequest,
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_optional_route_context),
) -> AutoBindingResponse:
    """Discover GMV Max store bindings via TikTok store/list + authorization/usage APIs and optionally persist them."""

    target_store = _normalize_identifier(payload.store_id)
    target_bc = None
    advertiser_candidates: List[str] = []
    seen_advertisers: set[str] = set()

    _dedupe_append(advertiser_candidates, payload.advertiser_id, seen_advertisers)
    _dedupe_append(advertiser_candidates, context.advertiser_id, seen_advertisers)

    db_candidate = _select_binding_from_links(
        context.db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        target_store=target_store,
        target_advertiser=payload.advertiser_id or context.advertiser_id,
        target_bc=target_bc,
    )
    if db_candidate:
        request_ids: Dict[str, Optional[str]] = {}
        auth_resp = await _call_tiktok(
            context.client.gmv_max_exclusive_authorization_get,
            GMVMaxExclusiveAuthorizationGetRequest(
                advertiser_id=str(db_candidate.advertiser_id),
                store_id=str(db_candidate.store_id),
                store_authorized_bc_id=str(db_candidate.store_authorized_bc_id),
            ),
        )
        request_ids["authorization"] = auth_resp.request_id
        usage_resp = await _call_tiktok(
            context.client.gmv_max_store_shop_ad_usage_check,
            GMVMaxStoreAdUsageCheckRequest(
                advertiser_id=str(db_candidate.advertiser_id),
                store_id=str(db_candidate.store_id or ""),
                store_authorized_bc_id=str(db_candidate.store_authorized_bc_id),
            ),
        )
        request_ids["usage"] = usage_resp.request_id

        refreshed_candidate = _build_auto_binding_candidate(
            {
                "store_id": db_candidate.store_id,
                "store_authorized_bc_id": db_candidate.store_authorized_bc_id,
                "advertiser_id": db_candidate.advertiser_id,
            },
            advertiser_id=db_candidate.advertiser_id,
            authorization_data=auth_resp.data if auth_resp else None,
            usage_data=usage_resp.data if usage_resp else None,
            request_ids=request_ids,
        )
        candidate = refreshed_candidate or db_candidate
        if payload.persist and not _is_binding_candidate_ready(candidate):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cached GMV Max binding is not EFFECTIVE in exclusive authorization; cannot persist binding."
                ),
            )
        persisted = False
        if payload.persist:
            try:
                upsert_binding_config(
                    context.db,
                    workspace_id=int(workspace_id),
                    auth_id=int(auth_id),
                    bc_id=candidate.store_authorized_bc_id,
                    advertiser_id=candidate.advertiser_id,
                    store_id=candidate.store_id,
                    auto_sync_products=True,
                    actor_user_id=int(me.id),
                )
                _upsert_store_link(
                    context.db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=candidate.advertiser_id,
                    store_id=candidate.store_id,
                    store_authorized_bc_id=candidate.store_authorized_bc_id,
                )
                context.db.commit()
                persisted = True
            except BindingConfigStorageNotReady as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "GMV Max binding configuration storage is not initialized; "
                        "please run database migrations."
                    ),
                ) from exc
        else:
            try:
                _upsert_store_link(
                    context.db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=candidate.advertiser_id,
                    store_id=candidate.store_id,
                    store_authorized_bc_id=candidate.store_authorized_bc_id,
                )
                context.db.commit()
            except Exception:
                context.db.rollback()
                raise
        return AutoBindingResponse(
            selected=candidate,
            candidates=[candidate],
            persisted=persisted,
        )

    if target_store:
        link_rows = (
            context.db.query(TTBAdvertiserStoreLink.advertiser_id)
            .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
            .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
            .filter(TTBAdvertiserStoreLink.store_id == target_store)
            .order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
            .all()
        )
        for (adv_id,) in link_rows:
            _dedupe_append(advertiser_candidates, adv_id, seen_advertisers)

    advertiser_rows = (
        context.db.query(TTBAdvertiser.advertiser_id)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .all()
    )
    for (adv_id,) in advertiser_rows:
        _dedupe_append(advertiser_candidates, adv_id, seen_advertisers)

    if not advertiser_candidates:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="No advertisers available for GMV Max binding discovery",
        )

    candidates: List[AutoBindingCandidate] = []
    for advertiser_id in advertiser_candidates:
        store_resp = await _call_tiktok(
            context.client.gmv_max_store_list,
            GMVMaxStoreListRequest(advertiser_id=str(advertiser_id)),
        )

        for store in store_resp.data.store_list:
            store_meta = _extract_store_metadata(store)
            store_id = _normalize_identifier(store_meta.get("store_id") or getattr(store, "store_id", None))
            bc_id = _normalize_identifier(
                store_meta.get("store_authorized_bc_id")
                or getattr(store, "store_authorized_bc_id", None)
            )
            if target_store and store_id != target_store:
                continue
            if not store_id:
                continue
            if not bc_id:
                # Without a business center ID TikTok APIs reject usage checks, and the
                # binding cannot be persisted anyway.
                continue

            request_ids: Dict[str, Optional[str]] = {"store_list": store_resp.request_id}

            auth_resp = await _call_tiktok(
                context.client.gmv_max_exclusive_authorization_get,
                GMVMaxExclusiveAuthorizationGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    store_authorized_bc_id=str(bc_id),
                ),
            )
            request_ids["authorization"] = auth_resp.request_id
            usage_resp = await _call_tiktok(
                context.client.gmv_max_store_shop_ad_usage_check,
                GMVMaxStoreAdUsageCheckRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id or ""),
                    store_authorized_bc_id=bc_id,
                ),
            )
            request_ids["usage"] = usage_resp.request_id

            candidate = _build_auto_binding_candidate(
                store,
                advertiser_id=str(advertiser_id),
                authorization_data=auth_resp.data if auth_resp else None,
                usage_data=usage_resp.data,
                request_ids=request_ids,
            )
            if candidate:
                candidates.append(candidate)

    ready_candidates = [c for c in candidates if _is_binding_candidate_ready(c)]
    selected = ready_candidates[0] if ready_candidates else None
    if selected is None:
        authorized_candidates = [
            c
            for c in candidates
            if (c.authorization_status or "").upper() not in {"UNAUTHORIZED", "REJECTED"}
        ]
        selected = authorized_candidates[0] if authorized_candidates else None

    logger.info(
        "gmvmax.auto_bind selection",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "target_store": target_store,
            "target_advertiser": payload.advertiser_id or context.advertiser_id,
            "target_bc": target_bc,
            "candidate_count": len(candidates),
            "ready_candidates": len(ready_candidates),
            "selected": selected.model_dump(exclude_none=True) if selected else None,
        },
    )

    if selected is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="未找到授权的广告户，请在店铺后台进行绑定。",
        )

    persisted = False
    if payload.persist and selected:
        if not _is_binding_candidate_ready(selected):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No GMV Max binding candidates passed exclusive authorization checks; cannot persist binding."
                ),
            )
        if not selected.store_authorized_bc_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="store_authorized_bc_id is required to persist GMV Max binding",
            )
        try:
            upsert_binding_config(
                context.db,
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                bc_id=selected.store_authorized_bc_id,
                advertiser_id=selected.advertiser_id,
                store_id=selected.store_id,
                auto_sync_products=True,
                actor_user_id=int(me.id),
            )
            _upsert_store_link(
                context.db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=selected.advertiser_id,
                store_id=selected.store_id,
                store_authorized_bc_id=selected.store_authorized_bc_id,
            )
            context.db.commit()
            persisted = True
        except BindingConfigStorageNotReady as exc:
            context.db.rollback()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GMV Max binding configuration storage is not initialized; please run database migrations.",
            ) from exc
        except Exception:
            context.db.rollback()
            raise
    elif selected:
        try:
            _upsert_store_link(
                context.db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=selected.advertiser_id,
                store_id=selected.store_id,
                store_authorized_bc_id=selected.store_authorized_bc_id,
            )
            context.db.commit()
        except Exception:
            context.db.rollback()
            raise

    return AutoBindingResponse(
        selected=selected,
        candidates=candidates,
        persisted=persisted,
    )


@router.post(
    "/precheck",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def gmvmax_precheck(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: GMVMaxPrecheckRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Enqueue precheck (shop usage, identity list, occupancy) via TikTok GMV Max APIs."""

    advertiser_id = _normalize_identifier(payload.advertiser_id) or context.advertiser_id
    if not advertiser_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="advertiser_id is required for GMV Max precheck",
        )
    if not payload.store_authorized_bc_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="store_authorized_bc_id is required",
        )

    async_res = celery_app.send_task(
        "gmvmax.precheck",
        kwargs={
            "auth_id": context.auth_id,
            "advertiser_id": str(advertiser_id),
            "store_id": str(payload.store_id),
            "store_authorized_bc_id": payload.store_authorized_bc_id,
            "identity_id": payload.identity_id,
            "product_item_group_ids": payload.product_item_group_ids,
            "occupied_asset_type": payload.occupied_asset_type,
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.precheck enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": advertiser_id,
            "store_id": payload.store_id,
            "task_id": async_res.id,
        },
    )
    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )


# === GMV Max campaign lifecycle & details ===
# - create/update campaigns, list from cache, and fetch TikTok details/sessions
@router.post(
    "",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def create_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: CreateCampaignRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignDetailResponse:
    """Create a GMV Max campaign (POST /campaign/gmv_max/create/) then fetch detail."""
    shopping_ads_type = payload.shopping_ads_type or payload.promotion_type
    if not shopping_ads_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "shopping_ads_type_required", "message": "shopping_ads_type is required"},
        )

    try:
        store_authorized_bc_id = await ensure_gmvmax_store_authorized(
            context.client,
            advertiser_id=context.advertiser_id,
            target_store_id=payload.store_id,
        )
        anchor_params = await build_gmvmax_anchor_params(
            context.client,
            advertiser_id=context.advertiser_id,
            shopping_ads_type=shopping_ads_type,
            store_id=payload.store_id,
            store_authorized_bc_id=store_authorized_bc_id,
            product_specific_type=payload.product_specific_type,
            item_group_ids=payload.item_group_ids,
            product_video_specific_type=payload.product_video_specific_type,
            identity_ids=payload.identity_ids,
        )
        row = await create_gmvmax_campaign(
            context.db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=context.advertiser_id,
            client=context.client,
            body=payload.to_client_body(
                store_authorized_bc_id=store_authorized_bc_id,
                anchor_params=anchor_params,
                shopping_ads_type=shopping_ads_type,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        await _handle_tiktok_error(exc)

    info_request = GMVMaxCampaignInfoRequest(
        advertiser_id=context.advertiser_id, campaign_id=str(row.campaign_id)
    )
    info_response = await _call_tiktok(context.client.gmv_max_campaign_info, info_request)
    campaign_info = info_response.data

    return CampaignDetailResponse(
        campaign=campaign_info,
        sessions=[],
        request_id=info_response.request_id,
    )


@router.put(
    "/{campaign_id}",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def update_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: UpdateCampaignRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignDetailResponse:
    """Update a GMV Max campaign (POST /campaign/gmv_max/update/) and refresh detail."""
    existing_row = _load_campaign_row(context, campaign_id)
    _ensure_campaign_not_deleted(existing_row)

    row = await update_gmvmax_campaign(
        context.db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=context.advertiser_id,
        client=context.client,
        body=payload.to_client_body(campaign_id=campaign_id),
    )

    info_request = GMVMaxCampaignInfoRequest(
        advertiser_id=context.advertiser_id, campaign_id=str(row.campaign_id)
    )
    info_response = await _call_tiktok(context.client.gmv_max_campaign_info, info_request)
    campaign_info = info_response.data

    return CampaignDetailResponse(
        campaign=campaign_info,
        sessions=[],
        request_id=info_response.request_id,
    )


@router.get(
    "",
    response_model=CampaignListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_campaigns_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    gmv_max_promotion_types: Optional[List[str]] = Query(None),
    store_ids: Optional[List[str]] = Query(None),
    campaign_ids: Optional[List[str]] = Query(None),
    campaign_name: Optional[str] = Query(None),
    primary_status: Optional[str] = Query(None),
    creation_filter_start_time: Optional[str] = Query(None),
    creation_filter_end_time: Optional[str] = Query(None),
    fields: Optional[List[str]] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    page_size: Optional[int] = Query(None, ge=1, le=50),
    advertiser_id: Optional[str] = Query(None),
    include_deleted: bool = Query(False),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignListResponse:
    """List GMV Max campaigns for this advertiser account from local cache (synced from /gmv_max/campaign/get/)."""

    adv = advertiser_id or context.advertiser_id
    page_value = page or 1
    page_size_value = page_size or 20

    store_filters = [str(item) for item in store_ids] if store_ids else None
    if not store_filters and context.store_id:
        store_filters = [str(context.store_id)]

    query = (
        context.db.query(GmvCampaign)
        .filter(GmvCampaign.workspace_id == int(workspace_id))
        .filter(GmvCampaign.advertiser_id == str(adv))
        .filter(GmvCampaign.auth_id == int(auth_id))
    )
    if store_filters:
        query = query.filter(GmvCampaign.store_id.in_(store_filters))
    if campaign_ids:
        query = query.filter(GmvCampaign.campaign_id.in_([str(item) for item in campaign_ids]))
    if campaign_name:
        query = query.filter(GmvCampaign.name.ilike(f"%{campaign_name}%"))
    if primary_status:
        query = query.filter(GmvCampaign.status == str(primary_status))
    if not include_deleted:
        query = query.filter(GmvCampaign.is_deleted.is_(False)).filter(
            or_(
                GmvCampaign.operation_status.is_(None),
                GmvCampaign.operation_status != "DELETE",
            )
        ).filter(
            or_(
                GmvCampaign.secondary_status.is_(None),
                GmvCampaign.secondary_status != "CAMPAIGN_STATUS_DELETE",
            )
        )

    total = query.count()
    rows = (
        query.order_by(GmvCampaign.updated_at.desc())
        .offset((page_value - 1) * page_size_value)
        .limit(page_size_value)
        .all()
    )
    items = [_campaign_row_to_schema(row) for row in rows]
    page_info = PageInfo(page=page_value, page_size=page_size_value, total_number=total)
    return CampaignListResponse(items=items, page_info=page_info, request_id=None)


@router.get(
    "/{campaign_id}",
    response_model=CampaignDetailResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str = Path(...),
    advertiser_id: Optional[str] = Query(None),
    include_sessions: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignDetailResponse:
    """Return campaign detail from cache and fetch sessions on demand when requested."""

    if context.db is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="database unavailable")

    adv = advertiser_id or context.advertiser_id
    row = (
        context.db.query(GmvCampaign)
        .filter(GmvCampaign.workspace_id == int(workspace_id))
        .filter(GmvCampaign.auth_id == int(auth_id))
        .filter(GmvCampaign.advertiser_id == str(adv))
        .filter(GmvCampaign.campaign_id == str(campaign_id))
        .order_by(GmvCampaign.updated_at.desc())
        .first()
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="campaign not found in cache; trigger refresh first",
    )

    sessions: List[GMVMaxSession] = []
    sessions_page_info = None
    sessions_request_id: str | None = None
    if include_sessions:
        try:
            session_resp = await context.client.gmv_max_session_list(
                GMVMaxSessionListRequest(
                    advertiser_id=str(adv),
                    campaign_id=str(campaign_id),
                )
            )
            data = getattr(session_resp, "data", None)
            raw_sessions = getattr(data, "list", None) or []
            sessions = [GMVMaxSession.model_validate(item) for item in raw_sessions]
            if getattr(data, "page_info", None):
                sessions_page_info = PageInfo.model_validate(data.page_info)
            sessions_request_id = getattr(session_resp, "request_id", None)
        except Exception:  # noqa: BLE001
            logger.warning(
                "gmvmax session list fetch failed",
                exc_info=True,
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "campaign_id": str(campaign_id),
                    "advertiser_id": str(adv),
                },
            )

    campaign_info = _campaign_row_to_detail(row)

    return CampaignDetailResponse(
        campaign=campaign_info,
        sessions=sessions if include_sessions else [],
        sessions_page_info=sessions_page_info,
        request_id=None,
        sessions_request_id=sessions_request_id,
    )


@router.post(
    "/{campaign_id}/refresh",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def refresh_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str = Path(...),
    advertiser_id: Optional[str] = Query(None),
    include_sessions: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Enqueue TikTok campaign detail/session refresh via Celery."""

    adv = advertiser_id or context.advertiser_id
    async_res = celery_app.send_task(
        "gmvmax.fetch_campaign_detail",
        kwargs={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "advertiser_id": str(adv),
            "campaign_id": str(campaign_id),
            "include_sessions": include_sessions,
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.campaign_refresh enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": adv,
            "campaign_id": campaign_id,
            "task_id": async_res.id,
        },
    )

    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )


# === GMV Max metrics ingestion & queries ===
# - enqueue TikTok report pulls and read cached daily/hourly metrics
@router.post(
    "/balance/sync",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_advertiser_balance(
    workspace_id: int,
    provider: str,
    auth_id: int,
    payload: BalanceSyncRequest,
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Trigger advertiser balance sync via Celery (no direct TikTok GMV Max call here)."""

    normalized_bc = _normalize_identifier(payload.bc_id) or context.binding.bc_id
    normalized_adv = _normalize_identifier(payload.advertiser_id) or context.advertiser_id
    if payload.store_id and context.store_id:
        normalized_store = _normalize_identifier(payload.store_id)
        if normalized_store and normalized_store != _normalize_identifier(context.store_id):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="store_id does not match the bound GMV Max store",
            )

    if not normalized_bc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="bc_id is required for balance sync")
    if not normalized_adv:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="advertiser_id is required for balance sync",
        )

    async_res = celery_app.send_task(
        "gmvmax.sync_advertiser_balance",
        kwargs={
            "workspace_id": context.workspace_id,
            "auth_id": context.auth_id,
            "bc_id": str(normalized_bc),
            "advertiser_id": str(normalized_adv),
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.sync_advertiser_balance enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "bc_id": normalized_bc,
            "advertiser_id": normalized_adv,
            "task_id": async_res.id,
        },
    )

    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )


@router.post(
    "/{campaign_id}/metrics/sync",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_metrics_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: MetricsRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> AsyncTaskResponse:
    """Trigger metrics sync via TikTok GMV Max report endpoints for the campaign."""

    adv = advertiser_id or context.advertiser_id
    async_res = celery_app.send_task(
        "gmvmax.sync_metrics",
        kwargs={
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "advertiser_id": adv,
            "campaign_id": campaign_id,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "granularity": "DAY",
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.metrics report enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "task_id": async_res.id,
        },
    )
    logger.info(
        "gmvmax.creative metrics manual sync skipped for 10min table",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
        },
    )
    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )


@router.get(
    "/{campaign_id}/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def query_gmvmax_metrics_provider(
    request: Request,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    store_id: Optional[str] = Query(None),
    level: str = Query("campaign"),
    start_date: Optional[Union[date, datetime, str]] = Query(None),
    end_date: Optional[Union[date, datetime, str]] = Query(None),
    advertiser_id: Optional[str] = Query(None),
    campaign_ids: Optional[List[str]] = Query(None),
    item_group_ids: Optional[List[str]] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> MetricsResponse:
    """Return GMV Max performance metrics for the requested campaign and level."""

    end = _normalize_date_value(end_date, field_name="end_date") or date.today()
    start = _normalize_date_value(start_date, field_name="start_date") or (end - timedelta(days=6))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_date_range",
                "message": "start_date must be earlier than or equal to end_date.",
            },
        )

    clean_campaign_ids = _sanitize_id_list(campaign_ids)
    clean_item_group_ids = _sanitize_id_list(item_group_ids)

    effective_store_id = store_id or context.store_id
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )

    effective_advertiser_id = advertiser_id or context.advertiser_id
    if not effective_advertiser_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_advertiser", "message": "advertiser_id is required"},
        )

    level_param = (request.query_params.get("level") or level or "campaign").lower()
    if level_param not in {"campaign", "product", "creative"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid GMV Max metrics level: {level_param}",
        )

    if level_param == "product" and not (clean_campaign_ids and len(clean_campaign_ids) > 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Item level metrics requires at least 1 campaign_id filter.",
        )

    if level_param == "creative" and (
        not (clean_campaign_ids and len(clean_campaign_ids) > 0)
        or not (clean_item_group_ids and len(clean_item_group_ids) > 0)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Creative level metrics requires at least 1 campaign_id and 1 item_group_id filter.",
        )

    db = context.db

    def _serialize_campaign_rows(rows: list[Any]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for row in rows:
            spend_cents = int(row.spend_cents or 0)
            revenue_cents = int(row.gross_revenue_cents or 0)
            cost_value = float(Decimal(spend_cents) / Decimal(100)) if spend_cents else 0.0
            gross_value = float(Decimal(revenue_cents) / Decimal(100)) if revenue_cents else 0.0
            orders_value = int(row.orders or 0)
            impressions_value = int(row.impressions or 0)
            clicks_value = int(row.clicks or 0)
            roas_value: float | None = None
            if spend_cents > 0:
                roas_value = float(Decimal(revenue_cents) / Decimal(spend_cents))

            serialized.append(
                {
                    "metrics": {
                        "spend": cost_value,
                        "cost": cost_value,
                        "net_cost": cost_value,
                        "gross_revenue": gross_value,
                        "gmv": gross_value,
                        "orders": orders_value,
                        "impressions": impressions_value,
                        "clicks": clicks_value,
                        "roas": roas_value,
                        "roi": roas_value,
                    },
                    "dimensions": {
                        "campaign_id": row.campaign_id,
                        "stat_time_day": row.stat_time_day.isoformat(),
                    },
                }
            )
        return serialized

    def _serialize_creative_rows(rows: list[Any]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for row in rows:
            cost_cents = getattr(row, "net_cost_cents", None) or getattr(row, "cost_cents", None)
            gross_cents = getattr(row, "gross_revenue_cents", None)
            spend_value = float(Decimal(cost_cents or 0) / Decimal(100)) if cost_cents else float(getattr(row, "net_cost", 0) or getattr(row, "cost", 0) or 0)
            gross_value = (
                float(Decimal(gross_cents or 0) / Decimal(100))
                if gross_cents is not None
                else float(getattr(row, "gross_revenue", 0) or 0)
            )
            impressions_value = int(getattr(row, "impressions", 0) or 0)
            clicks_value = int(getattr(row, "clicks", 0) or 0)
            orders_value = int(getattr(row, "orders", 0) or 0)
            roas_value: float | None = None
            if spend_value > 0:
                roas_value = gross_value / spend_value

            def _coerce_rate(attr: str) -> float | None:
                val = getattr(row, attr, None)
                return float(val) if val is not None else None

            metrics_payload = {
                "spend": spend_value,
                "cost": float(spend_value),
                "net_cost": float(spend_value),
                "gross_revenue": gross_value,
                "gmv": gross_value,
                "orders": orders_value,
                "impressions": impressions_value,
                "clicks": clicks_value,
                "roas": roas_value,
                "roi": roas_value,
                "product_impressions": getattr(row, "product_impressions", None),
                "product_clicks": getattr(row, "product_clicks", None),
                "product_click_rate": _coerce_rate("product_click_rate"),
                "ad_click_rate": _coerce_rate("ad_click_rate"),
                "ad_conversion_rate": _coerce_rate("ad_conversion_rate"),
                "ad_video_view_rate_2s": _coerce_rate("ad_video_view_rate_2s"),
                "ad_video_view_rate_6s": _coerce_rate("ad_video_view_rate_6s"),
                "ad_video_view_rate_p25": _coerce_rate("ad_video_view_rate_p25"),
                "ad_video_view_rate_p50": _coerce_rate("ad_video_view_rate_p50"),
                "ad_video_view_rate_p75": _coerce_rate("ad_video_view_rate_p75"),
                "ad_video_view_rate_p100": _coerce_rate("ad_video_view_rate_p100"),
                "creative_delivery_status": getattr(row, "creative_delivery_status", None)
                or getattr(row, "creative_status", None),
            }

            serialized.append(
                {
                    "metrics": {k: v for k, v in metrics_payload.items() if v is not None},
                    "dimensions": {
                        "campaign_id": row.campaign_id,
                        "creative_id": getattr(row, "creative_id", None) or getattr(row, "item_id", None),
                        "shop_content_id": getattr(row, "item_id", None) or getattr(row, "creative_id", None),
                        "product_id": getattr(row, "item_group_id", None),
                        "stat_time_day": (
                            row.stat_time_day.isoformat()
                            if isinstance(getattr(row, "stat_time_day", None), date)
                            else getattr(row, "stat_time_day", None).date().isoformat()
                        ),
                    },
                }
            )
        return serialized

    rows: list[Any] = []
    summary: dict[str, Any] | None = None

    if level_param in {"campaign", "product"}:
        campaign_filter_ids = clean_campaign_ids or [str(campaign_id)]
        stmt = (
            select(
                GmvCampaign.campaign_id,
                GmvCampaignMetricsDaily.stat_time_day.label("stat_time_day"),
                func.sum(func.coalesce(GmvCampaignMetricsDaily.net_cost_cents, GmvCampaignMetricsDaily.cost_cents, 0)).label(
                    "spend_cents"
                ),
                func.sum(GmvCampaignMetricsDaily.gross_revenue_cents).label("gross_revenue_cents"),
                func.sum(GmvCampaignMetricsDaily.orders).label("orders"),
                func.sum(GmvCampaignMetricsDaily.impressions).label("impressions"),
                func.sum(GmvCampaignMetricsDaily.clicks).label("clicks"),
            )
            .join(GmvCampaign, GmvCampaign.campaign_id == GmvCampaignMetricsDaily.campaign_id)
            .where(GmvCampaign.workspace_id == workspace_id)
            .where(GmvCampaign.auth_id == auth_id)
            .where(GmvCampaign.advertiser_id == str(effective_advertiser_id))
            .where(GmvCampaign.campaign_id.in_(campaign_filter_ids))
            .where(GmvCampaign.store_id == str(effective_store_id))
            .where(GmvCampaign.is_deleted.is_(False))
            .where(GmvCampaignMetricsDaily.stat_time_day >= start)
            .where(GmvCampaignMetricsDaily.stat_time_day <= end)
            .group_by(GmvCampaign.campaign_id, GmvCampaignMetricsDaily.stat_time_day)
            .order_by(GmvCampaignMetricsDaily.stat_time_day.asc())
        )
        rows = db.execute(stmt).all()

        totals = {
            "spend_cents": 0,
            "gross_revenue_cents": 0,
            "orders": 0,
            "impressions": 0,
            "clicks": 0,
        }
        for row in rows:
            totals["spend_cents"] += int(row.spend_cents or 0)
            totals["gross_revenue_cents"] += int(row.gross_revenue_cents or 0)
            totals["orders"] += int(row.orders or 0)
            totals["impressions"] += int(row.impressions or 0)
            totals["clicks"] += int(row.clicks or 0)

        spend_total = Decimal(totals["spend_cents"]) / Decimal(100) if totals["spend_cents"] else Decimal(0)
        gmv_total = Decimal(totals["gross_revenue_cents"]) / Decimal(100) if totals["gross_revenue_cents"] else Decimal(0)
        summary = {
            "spend": float(spend_total),
            "cost": float(spend_total),
            "net_cost": float(spend_total),
            "gmv": float(gmv_total),
            "gross_revenue": float(gmv_total),
            "orders": totals["orders"],
            "impressions": totals["impressions"],
            "clicks": totals["clicks"],
            "roas": float(gmv_total / spend_total) if spend_total > 0 else None,
            "roi": float(gmv_total / spend_total) if spend_total > 0 else None,
        }
    else:
        campaign_filter_ids = clean_campaign_ids or [str(campaign_id)]
        snapshots = latest_creative_metrics_snapshots(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_ids=campaign_filter_ids,
            start_date=start,
            end_date=end,
            store_ids=[str(effective_store_id)],
            product_ids=clean_item_group_ids,
        )

        rows = list(snapshots)

        historical_end = min(end, date.today() - timedelta(days=1)) if start < date.today() else None
        if historical_end and historical_end >= start:
            creative_stmt = (
                select(GmvCreativeMetricsDaily)
                .where(GmvCreativeMetricsDaily.campaign_id.in_(campaign_filter_ids))
                .where(func.date(GmvCreativeMetricsDaily.stat_time_day) >= start)
                .where(func.date(GmvCreativeMetricsDaily.stat_time_day) <= historical_end)
                .order_by(GmvCreativeMetricsDaily.stat_time_day.asc())
            )
            if clean_item_group_ids:
                creative_stmt = creative_stmt.where(GmvCreativeMetricsDaily.item_group_id.in_(clean_item_group_ids))

            rows.extend(list(db.execute(creative_stmt).scalars().all()))

        totals = {
            "spend": Decimal("0"),
            "gmv": Decimal("0"),
            "orders": 0,
            "impressions": 0,
            "clicks": 0,
        }
        for row in rows:
            spend_cents = getattr(row, "net_cost_cents", None) or getattr(row, "cost_cents", None)
            spend_value = (
                Decimal(spend_cents) / Decimal(100)
                if spend_cents is not None
                else Decimal(getattr(row, "net_cost", 0) or getattr(row, "cost", 0) or 0)
            )
            gross_cents = getattr(row, "gross_revenue_cents", None)
            gross_value = (
                Decimal(gross_cents) / Decimal(100)
                if gross_cents is not None
                else Decimal(getattr(row, "gross_revenue", 0) or 0)
            )
            totals["spend"] += spend_value
            totals["gmv"] += gross_value
            totals["orders"] += int(getattr(row, "orders", 0) or 0)
            totals["impressions"] += int(getattr(row, "impressions", 0) or 0)
            totals["clicks"] += int(getattr(row, "clicks", 0) or 0)

        summary = {
            "spend": float(totals["spend"]),
            "cost": float(totals["spend"]),
            "net_cost": float(totals["spend"]),
            "gmv": float(totals["gmv"]),
            "gross_revenue": float(totals["gmv"]),
            "orders": totals["orders"],
            "impressions": totals["impressions"],
            "clicks": totals["clicks"],
            "roas": float(totals["gmv"] / totals["spend"]) if totals["spend"] > 0 else None,
            "roi": float(totals["gmv"] / totals["spend"]) if totals["spend"] > 0 else None,
        }

    serialized_rows = (
        _serialize_campaign_rows(rows)
        if level_param in {"campaign", "product"}
        else _serialize_creative_rows(rows)
    )

    return {
        "report": {
            "list": serialized_rows,
            "page_info": {
                "page": 1,
                "page_size": len(serialized_rows),
                "total_number": len(serialized_rows),
                "total_page": 1,
                "cursor": None,
                "has_more": False,
                "has_next": False,
            },
            "summary": summary,
        },
        "request_id": None,
    }


# === GMV Max actions, creative heating, and strategy ===
# - campaign status/strategy actions and logs
# - creative heating triggers and evaluation
# - bid recommendation previews and session strategy updates
def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _coerce_decimal(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0


def _is_seed_creative(
    *,
    orders: int | float | None,
    roas: float | None,
    spend: float | None,
    min_conversions: int,
    min_roas: float,
    min_spend: float,
) -> bool:
    conversions_value = int(orders or 0)
    spend_value = float(spend or 0)
    roas_value = float(roas or 0)
    return (
        conversions_value >= min_conversions
        and spend_value >= min_spend
        and roas_value >= min_roas
    )


def _serialize_heating_row(row: Any) -> CreativeHeatingRecord:
    payload: Dict[str, Any] = {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "provider": row.provider,
        "auth_id": row.auth_id,
        "campaign_id": row.campaign_id,
        "creative_id": row.creative_id,
        "creative_name": getattr(row, "creative_name", None),
        "mode": getattr(row, "mode", None),
        "target_daily_budget": _to_float(getattr(row, "target_daily_budget", None)),
        "budget_delta": _to_float(getattr(row, "budget_delta", None)),
        "currency": getattr(row, "currency", None),
        "max_duration_minutes": getattr(row, "max_duration_minutes", None),
        "note": getattr(row, "note", None),
        "status": getattr(row, "status", "PENDING"),
        "last_action_type": getattr(row, "last_action_type", None),
        "last_action_time": getattr(row, "last_action_time", None),
        "last_error": getattr(row, "last_error", None),
        "evaluation_window_minutes": getattr(row, "evaluation_window_minutes", 60),
        "min_clicks": getattr(row, "min_clicks", None),
        "min_ctr": _to_float(getattr(row, "min_ctr", None)),
        "min_gross_revenue": _to_float(getattr(row, "min_gross_revenue", None)),
        "auto_stop_enabled": bool(getattr(row, "auto_stop_enabled", True)),
        "is_heating_active": bool(getattr(row, "is_heating_active", False)),
        "last_evaluated_at": getattr(row, "last_evaluated_at", None),
        "last_evaluation_result": getattr(row, "last_evaluation_result", None),
    }
    return CreativeHeatingRecord.model_validate(payload)


def _extract_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail")
        if message:
            return str(message)
        return str(detail)
    return str(detail)


def _serialize_action_log_row(
    campaign: GmvCampaign, row: GmvActionLog
) -> Dict[str, Any]:
    campaign_identifier = getattr(campaign, "campaign_id", None) or getattr(
        campaign, "id", None
    )
    return {
        "id": row.id,
        "campaign_id": campaign_identifier,
        "action_type": row.action,
        "reason": row.reason,
        "before": row.before_json,
        "after": row.after_json,
        "before_value": row.before_json,
        "after_value": row.after_json,
        "operator": row.performed_by,
        "result": row.result,
        "error_message": row.error_message,
        "timestamp": row.created_at,
        "created_at": row.created_at,
    }


async def _apply_creative_heating_action(
    *,
    context: GMVMaxRouteContext,
    campaign_id: str,
    request: CreativeHeatingActionRequest,
    performed_by: str,
) -> CreativeHeatingActionResponse:
    campaign_row = _load_campaign_row(context, campaign_id)
    if campaign_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )
    _ensure_campaign_not_deleted(campaign_row)
    before_state = _snapshot_campaign_state(campaign_row)
    heating_row = await upsert_creative_heating(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        creative_id=request.creative_id,
        advertiser_id=str(campaign_row.advertiser_id),
        promotion_type=str(campaign_row.promotion_type),
        mode=request.mode,
        target_daily_budget=request.target_daily_budget,
        budget_delta=request.budget_delta,
        currency=request.currency,
        max_duration_minutes=request.max_duration_minutes,
        note=request.note,
        creative_name=request.creative_name,
        product_id=request.product_id,
        item_id=request.item_id,
    )
    context.db.flush()

    updated_row, response = await apply_boost_creative_action(
        context.db,
        client=context.client,
        campaign=campaign_row,
        heating=heating_row,
        mode=request.mode,
        target_daily_budget=request.target_daily_budget,
        budget_delta=request.budget_delta,
        currency=request.currency,
        max_duration_minutes=request.max_duration_minutes,
        note=request.note,
        performed_by=performed_by,
        before_state=before_state,
    )

    return CreativeHeatingActionResponse(
        action_type="BOOST_CREATIVE",
        heating=_serialize_heating_row(updated_row),
        tiktok_response=response.data.model_dump(exclude_none=True),
        request_id=response.request_id,
    )


@router.post(
    "/{campaign_id}/actions",
    response_model=Union[CampaignActionResponse, CreativeHeatingActionResponse],
)
async def apply_gmvmax_campaign_action_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: Dict[str, Any] = Body(...),
    advertiser_id: Optional[str] = Query(None),
    me: SessionUser = Depends(require_tenant_admin),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> Union[CampaignActionResponse, CreativeHeatingActionResponse]:
    """Apply campaign status/strategy actions or BOOST_CREATIVE (status/update or /campaign/gmv_max/action/apply/)."""

    actor_label = _resolve_actor_label(me)
    normalized_campaign_id = str(campaign_id)
    campaign_before = _load_campaign_row(context, normalized_campaign_id)
    before_state = _snapshot_campaign_state(campaign_before)
    _ensure_campaign_not_deleted(campaign_before)

    raw_type = payload.get("action_type")
    if raw_type is None and "type" in payload:
        raw_type = payload["type"]
    normalized_type = str(raw_type or "").upper()

    if normalized_type == "BOOST_CREATIVE":
        candidate = dict(payload)
        candidate["action_type"] = "BOOST_CREATIVE"
        heating_request = CreativeHeatingActionRequest.model_validate(candidate)
        return await _apply_creative_heating_action(
            context=context,
            campaign_id=normalized_campaign_id,
            request=heating_request,
            performed_by=actor_label,
        )

    action_request = CampaignActionRequest.model_validate(payload)
    action_label = _ACTION_LOG_TYPES.get(
        action_request.type, action_request.type.upper()
    )
    adv = advertiser_id or context.advertiser_id

    def _log_success() -> None:
        campaign_after = _load_campaign_row(context, normalized_campaign_id)
        after_state = _snapshot_campaign_state(campaign_after)
        _log_action_entry(
            context,
            campaign_id=normalized_campaign_id,
            campaign=campaign_after or campaign_before,
            action=action_label,
            actor=actor_label,
            before=before_state,
            after=after_state or before_state,
            result="SUCCESS",
        )

    def _log_failure(detail: Any) -> None:
        _log_action_entry(
            context,
            campaign_id=normalized_campaign_id,
            campaign=campaign_before,
            action=action_label,
            actor=actor_label,
            before=before_state,
            after=before_state,
            result="FAILED",
            error_message=_extract_error_message(detail),
        )

    try:
        if action_request.type in {"pause", "enable", "delete"}:
            if action_request.type == "enable":
                await _ensure_campaign_products_available(
                    context, campaign=campaign_before, advertiser_id=str(adv)
                )
            operation_status_map = {
                "pause": "DISABLE",
                "enable": "ENABLE",
                "delete": "DELETE",
            }
            operation_status = operation_status_map[action_request.type]
            status_request = CampaignStatusUpdateRequest(
                advertiser_id=adv,
                campaign_ids=[normalized_campaign_id],
                operation_status=operation_status,
            )
            response = await _call_tiktok(
                context.client.campaign_status_update, status_request
            )
            await _refresh_campaign_snapshot(
                context,
                advertiser_id=adv,
                campaign_id=normalized_campaign_id,
            )
            _log_success()
            return CampaignActionResponse(
                type=action_request.type,
                status="success",
                response=response.data.model_dump(exclude_none=True),
                request_id=response.request_id,
            )
        if (
            action_request.type == "update_strategy"
            and action_request.payload.get("session_id")
        ):
            body = _build_session_update_body(
                normalized_campaign_id, action_request.payload, context.store_id
            )
            request = GMVMaxSessionUpdateRequest(advertiser_id=adv, body=body)
            response = await _call_tiktok(context.client.gmv_max_session_update, request)
            await _refresh_campaign_snapshot(
                context,
                advertiser_id=adv,
                campaign_id=normalized_campaign_id,
            )
            _log_success()
            return CampaignActionResponse(
                type=action_request.type,
                status="success",
                response={
                    "sessions": [item.model_dump() for item in response.data.list],
                },
                request_id=response.request_id,
            )

        body = _build_campaign_update_body(
            normalized_campaign_id, action_request.type, action_request.payload
        )
        request = GMVMaxCampaignUpdateRequest(advertiser_id=adv, body=body)
        response = await _call_tiktok(context.client.gmv_max_campaign_update, request)
        await _refresh_campaign_snapshot(
            context,
            advertiser_id=adv,
            campaign_id=normalized_campaign_id,
        )
        _log_success()
        return CampaignActionResponse(
            type=action_request.type,
            status="success",
            response=response.data.model_dump(exclude_none=True),
            request_id=response.request_id,
        )
    except HTTPException as exc:
        _log_failure(exc.detail)
        raise


@router.get(
    "/{campaign_id}/actions",
    response_model=ActionLogEntry,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_action_logs_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=200, alias="page_size"),
    sort: str = Query("-timestamp", description="Sort by timestamp, use - for desc"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> ActionLogEntry:
    """Return stored campaign action logs (paginated)."""

    limit = page_size
    offset = (page - 1) * page_size

    campaign, rows, total = list_action_logs(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        limit=limit,
        offset=offset,
        sort=sort,
    )

    entries = [_serialize_action_log_row(campaign, row) for row in rows]
    return ActionLogEntry(entries=entries, total=total)


@router.get(
    "/{campaign_id}/creatives/heating",
    response_model=CreativeHeatingListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_creative_heating_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    status: Optional[str] = Query(None, description="Optional heating status filter"),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CreativeHeatingListResponse:
    """Return stored heating state rows for a campaign's creatives (no live API call)."""

    rows = await list_heating_configs(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        status=str(status) if status is not None else None,
    )
    return CreativeHeatingListResponse(items=[_serialize_heating_row(row) for row in rows])


@router.get(
    "/{campaign_id}/strategy",
    response_model=StrategyResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = Query(None),
    include_recommendation: bool = Query(True),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> StrategyResponse:
    """Fetch campaign + session details and optional bid recommendation from TikTok APIs."""

    adv = advertiser_id or context.advertiser_id
    campaign_resp = await _call_tiktok(
        context.client.gmv_max_campaign_info,
        GMVMaxCampaignInfoRequest(advertiser_id=adv, campaign_id=str(campaign_id)),
    )
    sessions_resp = await _call_tiktok(
        context.client.gmv_max_session_list,
        GMVMaxSessionListRequest(advertiser_id=adv, campaign_id=str(campaign_id)),
    )
    recommendation = None
    recommendation_request_id = None
    if include_recommendation:
        item_group_ids = _extract_item_group_ids(sessions_resp.data.list)
        store_id = campaign_resp.data.store_id or context.store_id
        shopping_ads_type = campaign_resp.data.shopping_ads_type
        optimization_goal = campaign_resp.data.optimization_goal
        if (
            store_id
            and shopping_ads_type
            and optimization_goal
            and item_group_ids
        ):
            bid_request = GMVMaxBidRecommendRequest(
                advertiser_id=adv,
                store_id=str(store_id),
                shopping_ads_type=str(shopping_ads_type),
                optimization_goal=str(optimization_goal),
                item_group_ids=item_group_ids,
            )
            recommendation_resp = await _call_tiktok(
                context.client.gmv_max_bid_recommend,
                bid_request,
            )
            recommendation = recommendation_resp.data
            recommendation_request_id = recommendation_resp.request_id
    return StrategyResponse(
        campaign=campaign_resp.data,
        sessions=sessions_resp.data.list,
        sessions_page_info=sessions_resp.data.page_info,
        recommendation=recommendation,
        campaign_request_id=campaign_resp.request_id,
        sessions_request_id=sessions_resp.request_id,
        recommendation_request_id=recommendation_request_id,
    )


@router.put(
    "/{campaign_id}/strategy",
    response_model=StrategyUpdateResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def update_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: StrategyUpdateRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> StrategyUpdateResponse:
    """Update campaign/session strategy via TikTok update endpoints then return status."""

    adv = advertiser_id or context.advertiser_id
    campaign_resp = None
    session_resp = None
    if payload.campaign:
        body = GMVMaxCampaignUpdateBody(
            campaign_id=str(campaign_id),
            budget=payload.campaign.budget,
            roas_bid=payload.campaign.roas_bid,
            promotion_days=payload.campaign.promotion_days,
            schedule_type=payload.campaign.schedule_type,
            schedule_start_time=payload.campaign.schedule_start_time,
            schedule_end_time=payload.campaign.schedule_end_time,
        )
        campaign_resp = await _call_tiktok(
            context.client.gmv_max_campaign_update,
            GMVMaxCampaignUpdateRequest(advertiser_id=adv, body=body),
        )
    if payload.session:
        body = _build_session_update_body(
            campaign_id,
            payload.session.model_dump(exclude_none=True),
            context.store_id,
        )
        session_resp = await _call_tiktok(
            context.client.gmv_max_session_update,
            GMVMaxSessionUpdateRequest(advertiser_id=adv, body=body),
        )
    if not payload.campaign and not payload.session:
        return StrategyUpdateResponse(status="noop")
    status_value = "success"
    return StrategyUpdateResponse(
        status=status_value,
        campaign=campaign_resp.data if campaign_resp else None,
        sessions=session_resp.data.list if session_resp else None,
        campaign_request_id=campaign_resp.request_id if campaign_resp else None,
        session_request_id=session_resp.request_id if session_resp else None,
    )


@router.post(
    "/{campaign_id}/strategies/preview",
    response_model=AsyncTaskResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def preview_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: StrategyPreviewRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),

) -> AsyncTaskResponse:
    """Preview bid recommendations via TikTok GET /gmv_max/bid/recommend/ through Celery."""

    adv = advertiser_id or context.advertiser_id
    store_id = payload.store_id or context.store_id
    if not store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )
    if not payload.shopping_ads_type or not payload.optimization_goal:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shopping_ads_type and optimization_goal are required",
        )
    if not payload.item_group_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="item_group_ids is required",
        )
    request = GMVMaxBidRecommendRequest(
        advertiser_id=adv,
        store_id=str(store_id),
        shopping_ads_type=str(payload.shopping_ads_type),
        optimization_goal=str(payload.optimization_goal),
        item_group_ids=[str(item) for item in payload.item_group_ids],
        identity_id=payload.identity_id,
    )
    async_res = celery_app.send_task(
        "gmvmax.strategy_preview",
        kwargs={
            "auth_id": context.auth_id,
            "bid_request": request.model_dump(exclude_none=True, by_alias=True),
        },
        queue="gmvmax",
    )
    logger.info(
        "gmvmax.strategy_preview enqueued",
        extra={
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "task_id": async_res.id,
        },
    )
    return _build_task_response(
        async_res,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
    )

