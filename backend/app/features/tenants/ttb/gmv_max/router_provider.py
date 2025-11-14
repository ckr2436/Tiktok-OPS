from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendRequest,
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignInfoRequest,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
    GMVMaxResponse,
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
from .schemas import (
    ActionLogEntry,
    CampaignActionRequest,
    CampaignActionResponse,
    CampaignDetailResponse,
    CampaignFilter,
    CampaignListOptions,
    CampaignListResponse,
    DEFAULT_PROMOTION_TYPES,
    DEFAULT_DIMENSIONS,
    DEFAULT_METRICS,
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


async def _call_tiktok(
    func: Callable[..., Awaitable[GMVMaxResponse[Any]]],
    *args: Any,
    **kwargs: Any,
) -> GMVMaxResponse[Any]:
    try:
        return await func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        await _handle_tiktok_error(exc)


def _build_campaign_request(
    advertiser_id: str,
    filtering: Optional[CampaignFilter],
    options: Optional[CampaignListOptions],
) -> GMVMaxCampaignGetRequest:
    filter_obj = filtering or CampaignFilter()
    filtering_model = GMVMaxCampaignFiltering(
        gmv_max_promotion_types=list(filter_obj.gmv_max_promotion_types),
        store_ids=filter_obj.store_ids,
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
    metrics = report.metrics or list(DEFAULT_METRICS)
    dimensions = report.dimensions or list(DEFAULT_DIMENSIONS)
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
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> SyncResponse:
    """Trigger a GMV Max campaign + report sync by proxying to TikTok."""

    advertiser_id = payload.advertiser_id or context.advertiser_id
    campaign_req = _build_campaign_request(
        advertiser_id,
        payload.campaign_filter,
        payload.campaign_options,
    )
    campaign_resp = await _call_tiktok(
        context.client.gmv_max_campaign_get,
        campaign_req,
    )
    report_req = _build_report_request(
        advertiser_id,
        payload.report,
        default_store_id=context.store_id,
    )
    report_resp = await _call_tiktok(
        context.client.gmv_max_report_get,
        report_req,
    )
    return SyncResponse(
        campaigns=campaign_resp.data.list,
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
    return CampaignListResponse(
        items=response.data.list,
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
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> MetricsResponse:
    """Return GMV Max performance metrics for the requested campaign."""

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=6))
    request_body = MetricsRequest(
        store_ids=None,
        start_date=start,
        end_date=end,
        metrics=None,
        dimensions=None,
        enable_total_metrics=None,
        filtering=ReportFiltering(),
    )
    adv = advertiser_id or context.advertiser_id
    report_req = _build_report_request(
        adv,
        request_body,
        default_store_id=context.store_id,
        campaign_id=campaign_id,
    )
    response = await _call_tiktok(context.client.gmv_max_report_get, report_req)
    return MetricsResponse(report=response.data, request_id=response.request_id)


@router.post(
    "/{campaign_id}/actions",
    response_model=CampaignActionResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def apply_gmvmax_campaign_action_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: CampaignActionRequest,
    advertiser_id: Optional[str] = Query(None),
    context: GMVMaxRouteContext = Depends(get_route_context),
) -> CampaignActionResponse:
    """Apply a GMV Max campaign action and return the TikTok response."""

    adv = advertiser_id or context.advertiser_id
    if payload.type == "update_strategy" and payload.payload.get("session_id"):
        body = _build_session_update_body(campaign_id, payload.payload, context.store_id)
        request = GMVMaxSessionUpdateRequest(advertiser_id=adv, body=body)
        response = await _call_tiktok(context.client.gmv_max_session_update, request)
        return CampaignActionResponse(
            type=payload.type,
            status="success",
            response={
                "sessions": [item.model_dump() for item in response.data.list],
            },
            request_id=response.request_id,
        )

    body = _build_campaign_update_body(campaign_id, payload.type, payload.payload)
    request = GMVMaxCampaignUpdateRequest(advertiser_id=adv, body=body)
    response = await _call_tiktok(context.client.gmv_max_campaign_update, request)
    return CampaignActionResponse(
        type=payload.type,
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

