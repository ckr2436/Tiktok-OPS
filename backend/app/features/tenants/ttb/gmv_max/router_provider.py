from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_session, require_tenant_admin, require_tenant_member
from app.data.db import get_db
from app.services import audit as audit_svc

from .router_metrics import _parse_date, _parse_datetime
from .router_strategy import _serialize_strategy
from .schemas import (
    GmvMaxActionLogListResponse,
    GmvMaxActionLogOut,
    GmvMaxCampaignActionIn,
    GmvMaxCampaignActionOut,
    GmvMaxCampaignActionType,
    GmvMaxCampaignDetailResponse,
    GmvMaxCampaignListResponse,
    GmvMaxCampaignOut,
    GmvMaxMetricsPoint,
    GmvMaxMetricsResponse,
    GmvMaxMetricsSyncRequest,
    GmvMaxStrategyConfigIn,
    GmvMaxStrategyConfigOut,
    GmvMaxStrategyPreviewResponse,
    GmvMaxSyncResponse,
)
from .service import (
    apply_campaign_action,
    get_campaign,
    get_strategy,
    list_action_logs,
    list_campaigns,
    preview_strategy,
    query_metrics,
    sync_campaigns,
    sync_metrics,
    update_strategy,
)

router = APIRouter()


def _audit_adapter(actor_user: SessionUser | None) -> Callable[..., Any] | None:
    log_event = getattr(audit_svc, "log_event", None)
    if log_event is None:
        return None

    actor_user_id = int(actor_user.id) if actor_user else None

    def _hook(
        *,
        db: Session,
        workspace_id: int,
        actor: str,
        domain: str,
        event: str,
        target: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        result: str,
        error: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "domain": domain,
            "event": event,
            "actor": actor,
            "target": target or {},
            "before": before or {},
            "after": after or {},
            "result": result,
        }
        if error:
            details["error"] = error
        log_event(
            db,
            action=event,
            resource_type="gmv_max.campaign",
            resource_id=None,
            actor_user_id=actor_user_id,
            workspace_id=workspace_id,
            details=details,
        )

    return _hook


@router.post(
    "/campaigns/sync",
    response_model=GmvMaxSyncResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_campaigns_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> GmvMaxSyncResponse:
    synced = await sync_campaigns(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        status_filter=status_filter,
    )
    return GmvMaxSyncResponse(synced=synced)


@router.get(
    "/",
    response_model=GmvMaxCampaignListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_campaigns_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sync: bool = Query(False),
    db: Session = Depends(get_db),
) -> GmvMaxCampaignListResponse:
    payload = await list_campaigns(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        status_filter=status_filter,
        q=q,
        page=page,
        page_size=page_size,
        sync=sync,
    )
    return GmvMaxCampaignListResponse(
        total=payload["total"],
        page=payload["page"],
        page_size=payload["page_size"],
        items=[GmvMaxCampaignOut.from_orm(item) for item in payload["items"]],
        synced=payload.get("synced"),
    )


@router.get(
    "/{campaign_id}",
    response_model=GmvMaxCampaignDetailResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_campaign_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str = Path(...),
    advertiser_id: str | None = Query(None),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
) -> GmvMaxCampaignDetailResponse:
    campaign = await get_campaign(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
        refresh=refresh,
    )
    return GmvMaxCampaignDetailResponse(campaign=GmvMaxCampaignOut.from_orm(campaign))


@router.post(
    "/{campaign_id}/metrics/sync",
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_metrics_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxMetricsSyncRequest,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    synced = await sync_metrics(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=payload.advertiser_id,
        granularity=payload.granularity,
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    return {"synced_rows": synced}


@router.get(
    "/{campaign_id}/metrics",
    response_model=GmvMaxMetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def query_gmvmax_metrics_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    granularity: str = Query("hour", regex=r"^(?i)(hour|day)$"),
    start: Optional[str] = Query(None, description="ISO date or datetime"),
    end: Optional[str] = Query(None, description="ISO date or datetime"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> GmvMaxMetricsResponse:
    gran = granularity.lower()
    if gran == "hour":
        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        result = query_metrics(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id,
            granularity="HOUR",
            start=start_dt,
            end=end_dt,
            limit=limit,
            offset=offset,
        )
        points = [
            GmvMaxMetricsPoint(
                ts=row.interval_start,
                impressions=row.impressions,
                clicks=row.clicks,
                cost_cents=row.cost_cents,
                gross_revenue_cents=row.gross_revenue_cents,
                orders=row.orders,
                roi=row.roi,
            )
            for row in result["items"]
        ]
        return GmvMaxMetricsResponse(granularity="hour", points=points)

    start_date = _parse_date(start)
    end_date = _parse_date(end)
    result = query_metrics(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        granularity="DAY",
        start=start_date,
        end=end_date,
        limit=limit,
        offset=offset,
    )
    points = [
        GmvMaxMetricsPoint(
            ts=row.date,
            impressions=row.impressions,
            clicks=row.clicks,
            cost_cents=row.cost_cents,
            gross_revenue_cents=row.gross_revenue_cents,
            orders=row.orders,
            roi=row.roi,
        )
        for row in result["items"]
    ]
    return GmvMaxMetricsResponse(granularity="day", points=points)


@router.post(
    "/campaigns/{campaign_id}/actions",
    response_model=GmvMaxCampaignActionOut,
    dependencies=[Depends(require_tenant_admin)],
)
async def apply_gmvmax_campaign_action_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxCampaignActionIn,
    db: Session = Depends(get_db),
    current_user: SessionUser = Depends(require_session),
) -> GmvMaxCampaignActionOut:
    if payload.action == GmvMaxCampaignActionType.SET_BUDGET and payload.daily_budget_cents is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="daily_budget_cents is required for SET_BUDGET",
        )
    if payload.action == GmvMaxCampaignActionType.SET_ROAS and payload.roas_bid is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="roas_bid is required for SET_ROAS",
        )

    performed_by = current_user.email or str(current_user.id)
    payload_dict = {
        key: value
        for key, value in {
            "daily_budget_cents": payload.daily_budget_cents,
            "roas_bid": payload.roas_bid,
        }.items()
        if value is not None
    }

    audit_hook = _audit_adapter(current_user)

    campaign, log_entry = await apply_campaign_action(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        action=payload.action.value,
        payload=payload_dict,
        reason=payload.reason,
        performed_by=performed_by,
        audit_hook=audit_hook,
    )

    return GmvMaxCampaignActionOut(
        action=payload.action,
        result=log_entry.result or "",
        campaign=GmvMaxCampaignOut.from_orm(campaign),
    )


@router.get(
    "/{campaign_id}/actions",
    response_model=GmvMaxActionLogListResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def list_gmvmax_action_logs_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> GmvMaxActionLogListResponse:
    campaign, rows = list_action_logs(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        limit=limit,
        offset=offset,
    )
    items = [
        GmvMaxActionLogOut(
            id=row.id,
            action=row.action,
            reason=row.reason,
            performed_by=row.performed_by,
            result=row.result,
            error_message=row.error_message,
            before=row.before_json,
            after=row.after_json,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return GmvMaxActionLogListResponse(
        campaign_id=campaign.campaign_id,
        count=len(items),
        items=items,
    )


@router.get(
    "/{campaign_id}/strategy",
    response_model=GmvMaxStrategyConfigOut,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    db: Session = Depends(get_db),
) -> GmvMaxStrategyConfigOut:
    cfg = get_strategy(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )
    return _serialize_strategy(cfg)


@router.put(
    "/{campaign_id}/strategy",
    response_model=GmvMaxStrategyConfigOut,
    dependencies=[Depends(require_tenant_admin)],
)
async def update_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxStrategyConfigIn,
    db: Session = Depends(get_db),
) -> Response | GmvMaxStrategyConfigOut:
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    cfg = update_strategy(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        payload=data,
    )
    if cfg is None:
        return Response(status_code=204)
    return _serialize_strategy(cfg)


@router.get(
    "/{campaign_id}/strategy/preview",
    response_model=GmvMaxStrategyPreviewResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def preview_gmvmax_strategy_provider(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    db: Session = Depends(get_db),
) -> GmvMaxStrategyPreviewResponse:
    result = preview_strategy(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )
    return GmvMaxStrategyPreviewResponse(**result)
