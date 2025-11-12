from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.deps import require_tenant_member
from app.data.db import get_db
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
)

from .schemas import GmvMaxMetricsPoint, GmvMaxMetricsResponse

router = APIRouter()


@router.get(
    "/campaigns/{campaign_id}/metrics",
    response_model=GmvMaxMetricsResponse,
    dependencies=[Depends(require_tenant_member)],
)
async def get_gmvmax_metrics_handler(
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    granularity: Literal["hour", "day"] = Query("hour"),
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: Session = Depends(get_db),
) -> GmvMaxMetricsResponse:
    campaign = (
        db.query(TTBGmvMaxCampaign)
        .filter(
            TTBGmvMaxCampaign.workspace_id == workspace_id,
            TTBGmvMaxCampaign.auth_id == auth_id,
            TTBGmvMaxCampaign.campaign_id == campaign_id,
        )
        .one_or_none()
    )
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    points: list[GmvMaxMetricsPoint] = []

    if granularity == "hour":
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time())
        rows = (
            db.query(TTBGmvMaxMetricsHourly)
            .filter(
                TTBGmvMaxMetricsHourly.campaign_id == campaign.id,
                TTBGmvMaxMetricsHourly.interval_start >= start_dt,
                TTBGmvMaxMetricsHourly.interval_start < end_dt,
            )
            .order_by(TTBGmvMaxMetricsHourly.interval_start.asc())
            .all()
        )
        for row in rows:
            points.append(
                GmvMaxMetricsPoint(
                    ts=row.interval_start,
                    impressions=row.impressions,
                    clicks=row.clicks,
                    cost_cents=row.cost_cents,
                    gross_revenue_cents=row.gross_revenue_cents,
                    orders=row.orders,
                    roi=row.roi,
                )
            )
    else:
        rows = (
            db.query(TTBGmvMaxMetricsDaily)
            .filter(
                TTBGmvMaxMetricsDaily.campaign_id == campaign.id,
                TTBGmvMaxMetricsDaily.date >= start_date,
                TTBGmvMaxMetricsDaily.date <= end_date,
            )
            .order_by(TTBGmvMaxMetricsDaily.date.asc())
            .all()
        )
        for row in rows:
            points.append(
                GmvMaxMetricsPoint(
                    ts=row.date,
                    impressions=row.impressions,
                    clicks=row.clicks,
                    cost_cents=row.cost_cents,
                    gross_revenue_cents=row.gross_revenue_cents,
                    orders=row.orders,
                    roi=row.roi,
                )
            )

    return GmvMaxMetricsResponse(granularity=granularity, points=points)
