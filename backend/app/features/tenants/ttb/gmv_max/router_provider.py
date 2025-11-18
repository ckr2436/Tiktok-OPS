from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.data.repositories.tiktok_business.gmvmax_heating import (
    update_heating_action_result,
    upsert_creative_heating,
)
from app.data.repositories.tiktok_business.gmvmax_metrics import (
    GMVMaxMetricDTO,
    query_gmvmax_metrics,
)
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
    GMVMaxReportData,
    GMVMaxReportEntry,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxResponse,
    PageInfo,
    GMVMaxSessionListRequest,
    GMVMaxSession,
    GMVMaxSessionProduct,
    GMVMaxSessionSettings,
    GMVMaxSessionUpdateBody,
    GMVMaxSessionUpdateRequest,
    TikTokBusinessGMVMaxClient,
)
from app.services.ttb_api import TTBApiError, TTBHttpError

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
)
from app.services.ttb_gmvmax import (
    resolve_store_id_from_page_context,
    upsert_campaign_from_api,
)

from .schemas import (
    ActionLogEntry,
    CampaignActionRequest,
    CampaignActionResponse,
    CampaignDetailResponse,
    CampaignFilter,
    CampaignListOptions,
    CampaignListResponse,
    DEFAULT_PROMOTION_TYPES,
    CreativeHeatingActionRequest,
    CreativeHeatingActionResponse,
    CreativeHeatingRecord,
    MetricsRequest,
    MetricsResponse,
    ReportFiltering,
    ReportRequest,
    StrategyPreviewRequest,
    StrategyPreviewResponse,
    StrategyResponse,
    StrategyUpdateRequest,
    StrategyUpdateResponse,
    SyncRequest,
    SyncResponse,
)

router = APIRouter(prefix="/gmvmax")
logger = logging.getLogger("gmv.ttb.gmvmax.router")


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


@dataclass(slots=True)
class GMVMaxRouteContext:
    """Per-request context containing the TikTok client and binding metadata."""

    workspace_id: int
    provider: str
    auth_id: int
    advertiser_id: str
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


def _persist_campaign_relations(
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
            store_hint = store_scope
        try:
            upsert_campaign_from_api(
                db,
                workspace_id=context.workspace_id,
                auth_id=context.auth_id,
                advertiser_id=str(advertiser_id),
                payload=payload,
                store_id_hint=store_hint,
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
        upsert_campaign_from_api(
            db,
            workspace_id=context.workspace_id,
            auth_id=context.auth_id,
            advertiser_id=str(advertiser_id),
            payload=response.data.model_dump(exclude_none=False),
            store_id_hint=store_hint or context.store_id,
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


def _build_report_request(
    advertiser_id: str,
    report: ReportRequest | MetricsRequest,
    *,
    default_store_id: Optional[str],
    campaign_id: Optional[str] = None,
) -> GMVMaxReportGetRequest:
    store_ids = _normalize_store_ids(report.store_ids, default_store_id)
    metrics = _normalize_metrics_list(report.metrics)
    dimensions = _normalize_dimensions_list(report.dimensions)
    filtering_payload: Dict[str, Any] = {}
    if report.filtering is not None:
        filtering_payload.update(report.filtering.model_dump(exclude_none=True))
    if campaign_id:
        filtering_payload.setdefault("campaign_ids", [str(campaign_id)])
    filtering_model = (
        GMVMaxReportFiltering(**filtering_payload) if filtering_payload else None
    )
    return GMVMaxReportGetRequest(
        advertiser_id=str(advertiser_id),
        store_ids=store_ids,
        start_date=report.start_date.isoformat(),
        end_date=report.end_date.isoformat(),
        metrics=list(metrics),
        dimensions=list(dimensions),
        enable_total_metrics=report.enable_total_metrics,
        filtering=filtering_model,
        page=report.page,
        page_size=report.page_size,
        sort_field=report.sort_field,
        sort_type=report.sort_type,
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


def get_route_context(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
) -> GMVMaxRouteContext:
    normalized_provider = normalize_provider(provider)
    binding = resolve_account_binding(db, workspace_id, normalized_provider, auth_id)
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


@router.post(
    "/sync",
    response_model=SyncResponse,
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
) -> SyncResponse:
    """Trigger a GMV Max campaign + report sync by proxying to TikTok."""

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
    store_ids_override = [store_id] if store_id else None
    campaign_req = _build_campaign_request(
        advertiser_id,
        payload.campaign_filter,
        payload.campaign_options,
        store_ids_override=store_ids_override,
    )
    campaign_resp = await _call_tiktok(
        context.client.gmv_max_campaign_get,
        campaign_req,
        _log_context=log_context,
    )
    _persist_campaign_relations(
        context,
        advertiser_id=advertiser_id,
        response=campaign_resp,
        store_scope=store_id,
    )
    filtered_campaigns = _filter_campaign_entries(campaign_resp.data.list)
    report_req = _build_report_request(
        advertiser_id,
        payload.report,
        default_store_id=store_id,
    )
    report_resp = await _call_tiktok(
        context.client.gmv_max_report_get,
        report_req,
        _log_context=log_context,
    )
    return SyncResponse(
        campaigns=filtered_campaigns,
        campaigns_page_info=campaign_resp.data.page_info,
        report=report_resp.data,
        campaign_request_id=campaign_resp.request_id,
        report_request_id=report_resp.request_id,
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
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignListResponse:
    """List GMV Max campaigns for this advertiser account."""

    adv = advertiser_id or context.advertiser_id
    filter_obj = CampaignFilter(
        gmv_max_promotion_types=gmv_max_promotion_types or list(DEFAULT_PROMOTION_TYPES),
        store_ids=store_ids,
        campaign_ids=campaign_ids,
        campaign_name=campaign_name,
        primary_status=primary_status,
        creation_filter_start_time=creation_filter_start_time,
        creation_filter_end_time=creation_filter_end_time,
    )
    options = CampaignListOptions(fields=fields, page=page, page_size=page_size)
    request = _build_campaign_request(adv, filter_obj, options)
    response = await _call_tiktok(context.client.gmv_max_campaign_get, request)
    store_scope = store_ids[0] if store_ids else context.store_id
    _persist_campaign_relations(
        context,
        advertiser_id=adv,
        response=response,
        store_scope=store_scope,
    )
    filtered_items = _filter_campaign_entries(response.data.list)
    return CampaignListResponse(
        items=filtered_items,
        page_info=response.data.page_info,
        request_id=response.request_id,
    )


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
    """Retrieve a single GMV Max campaign for this advertiser account."""

    adv = advertiser_id or context.advertiser_id
    info_resp = await _call_tiktok(
        context.client.gmv_max_campaign_info,
        GMVMaxCampaignInfoRequest(advertiser_id=adv, campaign_id=str(campaign_id)),
    )
    session_resp = None
    sessions: List[GMVMaxSession] = []
    sessions_page_info = None
    if include_sessions:
        session_resp = await _call_tiktok(
            context.client.gmv_max_session_list,
            GMVMaxSessionListRequest(
                advertiser_id=adv,
                campaign_id=str(campaign_id),
            ),
        )
        sessions = session_resp.data.list
        sessions_page_info = session_resp.data.page_info
    return CampaignDetailResponse(
        campaign=info_resp.data,
        sessions=sessions,
        sessions_page_info=sessions_page_info,
        request_id=info_resp.request_id,
        sessions_request_id=session_resp.request_id if session_resp else None,
    )


@router.post(
    "/{campaign_id}/metrics/sync",
    response_model=MetricsResponse,
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
) -> MetricsResponse:
    """Trigger a metrics sync for the specified GMV Max campaign."""

    adv = advertiser_id or context.advertiser_id
    report_req = _build_report_request(
        adv,
        payload,
        default_store_id=context.store_id,
        campaign_id=campaign_id,
    )
    response = await _call_tiktok(context.client.gmv_max_report_get, report_req)
    return MetricsResponse(report=response.data, request_id=response.request_id)


@router.get(
    "/{campaign_id}/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def query_gmvmax_metrics_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    store_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> MetricsResponse:
    """Return stored GMV Max performance metrics for the requested campaign."""

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=6))
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_date_range",
                "message": "start_date must be earlier than or equal to end_date.",
            },
        )

    effective_store_id = store_id or context.store_id
    if not effective_store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )
    cache_key = (
        context.workspace_id,
        context.provider,
        context.auth_id,
        str(campaign_id),
        effective_store_id or "*",
        start.isoformat(),
        end.isoformat(),
        page,
        page_size,
    )
    cached = _metrics_cache.get(cache_key)
    if cached:
        return cached

    limit = page_size
    offset = (page - 1) * page_size
    items, total = query_gmvmax_metrics(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        advertiser_id=context.advertiser_id,
        store_id=effective_store_id,
        start_date=start,
        end_date=end,
        limit=limit,
        offset=offset,
    )
    response = _build_metrics_response(
        items,
        total=total,
        page=page,
        page_size=page_size,
    )
    _metrics_cache.set(cache_key, response)
    return response


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


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


async def _apply_creative_heating_action(
    *,
    context: GMVMaxRouteContext,
    campaign_id: str,
    request: CreativeHeatingActionRequest,
) -> CreativeHeatingActionResponse:
    heating_row = await upsert_creative_heating(
        context.db,
        workspace_id=context.workspace_id,
        provider=context.provider,
        auth_id=context.auth_id,
        campaign_id=str(campaign_id),
        creative_id=request.creative_id,
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

    action_body = {
        "campaign_id": str(campaign_id),
        "action_type": request.action_type,
        "creative_id": request.creative_id,
    }
    if request.mode:
        action_body["mode"] = request.mode
    if request.target_daily_budget is not None:
        action_body["target_daily_budget"] = request.target_daily_budget
    if request.budget_delta is not None:
        action_body["budget_delta"] = request.budget_delta
    if request.currency:
        action_body["currency"] = request.currency
    if request.max_duration_minutes is not None:
        action_body["max_duration_minutes"] = request.max_duration_minutes
    if request.note:
        action_body["note"] = request.note

    api_request = GMVMaxCampaignActionApplyRequest(
        advertiser_id=context.advertiser_id,
        body=GMVMaxCampaignActionApplyBody(**action_body),
    )

    action_time = datetime.now(tz=timezone.utc)
    try:
        response = await _call_tiktok(
            context.client.gmv_max_campaign_action_apply, api_request
        )
    except HTTPException as exc:
        detail = exc.detail
        await update_heating_action_result(
            context.db,
            heating_id=heating_row.id,
            status="FAILED",
            action_type="APPLY_BOOST",
            action_time=action_time,
            request_payload=action_body,
            response_payload=detail if isinstance(detail, dict) else None,
            error_message=_extract_error_message(detail),
        )
        context.db.flush()
        raise

    updated_row = await update_heating_action_result(
        context.db,
        heating_id=heating_row.id,
        status="APPLIED",
        action_type="APPLY_BOOST",
        action_time=action_time,
        request_payload=action_body,
        response_payload=response.data.model_dump(exclude_none=True),
        error_message=None,
    )
    context.db.flush()

    return CreativeHeatingActionResponse(
        action_type="BOOST_CREATIVE",
        heating=_serialize_heating_row(updated_row),
        tiktok_response=response.data.model_dump(exclude_none=True),
        request_id=response.request_id,
    )


@router.post(
    "/{campaign_id}/actions",
    response_model=Union[CampaignActionResponse, CreativeHeatingActionResponse],
    dependencies=[Depends(require_tenant_admin)],
)
async def apply_gmvmax_campaign_action_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: Dict[str, Any] = Body(...),
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> Union[CampaignActionResponse, CreativeHeatingActionResponse]:
    """Apply a GMV Max campaign action and return the TikTok response."""

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
            campaign_id=str(campaign_id),
            request=heating_request,
        )

    action_request = CampaignActionRequest.model_validate(payload)
    adv = advertiser_id or context.advertiser_id
    if action_request.type in {"pause", "enable", "delete"}:
        operation_status_map = {
            "pause": "DISABLE",
            "enable": "ENABLE",
            "delete": "DELETE",
        }
        operation_status = operation_status_map[action_request.type]
        status_request = CampaignStatusUpdateRequest(
            advertiser_id=adv,
            campaign_ids=[str(campaign_id)],
            operation_status=operation_status,
        )
        response = await _call_tiktok(
            context.client.campaign_status_update, status_request
        )
        await _refresh_campaign_snapshot(
            context,
            advertiser_id=adv,
            campaign_id=str(campaign_id),
        )
        return CampaignActionResponse(
            type=action_request.type,
            status="success",
            response=response.data.model_dump(exclude_none=True),
            request_id=response.request_id,
        )
    if action_request.type == "update_strategy" and action_request.payload.get("session_id"):
        body = _build_session_update_body(campaign_id, action_request.payload, context.store_id)
        request = GMVMaxSessionUpdateRequest(advertiser_id=adv, body=body)
        response = await _call_tiktok(context.client.gmv_max_session_update, request)
        await _refresh_campaign_snapshot(
            context,
            advertiser_id=adv,
            campaign_id=str(campaign_id),
        )
        return CampaignActionResponse(
            type=action_request.type,
            status="success",
            response={
                "sessions": [item.model_dump() for item in response.data.list],
            },
            request_id=response.request_id,
        )

    body = _build_campaign_update_body(campaign_id, action_request.type, action_request.payload)
    request = GMVMaxCampaignUpdateRequest(advertiser_id=adv, body=body)
    response = await _call_tiktok(context.client.gmv_max_campaign_update, request)
    await _refresh_campaign_snapshot(
        context,
        advertiser_id=adv,
        campaign_id=str(campaign_id),
    )
    return CampaignActionResponse(
        type=action_request.type,
        status="success",
        response=response.data.model_dump(exclude_none=True),
        request_id=response.request_id,
    )


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
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> ActionLogEntry:
    """Return placeholder action logs until storage is implemented."""

    # TODO: Wire this endpoint to persisted action logs in a future task.
    return ActionLogEntry(entries=[])


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
    """Fetch the GMV Max optimization strategy for the campaign."""

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
    """Update the strategy configuration for the campaign."""

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
    response_model=StrategyPreviewResponse,
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
) -> StrategyPreviewResponse:
    """Preview the GMV Max optimization strategy for the campaign."""

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
    response = await _call_tiktok(context.client.gmv_max_bid_recommend, request)
    return StrategyPreviewResponse(
        status="success",
        recommendation=response.data,
        request_id=response.request_id,
    )

