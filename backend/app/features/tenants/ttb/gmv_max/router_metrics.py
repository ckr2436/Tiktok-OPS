from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_admin, require_tenant_member
from app.data.db import get_db

from .schemas import GmvMaxMetricsPoint, GmvMaxMetricsResponse, GmvMaxMetricsSyncRequest
from .service import query_metrics, sync_metrics

PROVIDER_ALIAS = "tiktok_business"

router = APIRouter()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid datetime") from exc


def _parse_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid date") from exc


@router.post(
    "/{campaign_id}/metrics/sync",
    dependencies=[Depends(require_tenant_admin)],
)
async def sync_gmvmax_metrics_handler(
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    payload: GmvMaxMetricsSyncRequest,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    synced = await sync_metrics(
        db,
        workspace_id=workspace_id,
        provider=PROVIDER_ALIAS,
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
async def query_gmvmax_metrics_handler(
    workspace_id: int,
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
            provider=PROVIDER_ALIAS,
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
        provider=PROVIDER_ALIAS,
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
