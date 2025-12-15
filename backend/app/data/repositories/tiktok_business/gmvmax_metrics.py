"""Database helpers for querying GMV Max campaign metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvCampaign, GmvCampaignMetricsDaily


def _cents_to_amount(value: Optional[int]) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))


def _to_decimal(value: Optional[Decimal | float | int]) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except Exception:  # noqa: BLE001 - defensive conversion
        return None


class GMVMaxMetricDTO(BaseModel):
    """Serialized view of GMV Max metrics stored in MySQL."""

    stat_time_day: date
    campaign_id: str
    store_id: Optional[str] = None
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    cost: Optional[Decimal] = None
    net_cost: Optional[Decimal] = None
    orders: Optional[int] = None
    cost_per_order: Optional[Decimal] = None
    gross_revenue: Optional[Decimal] = None
    roi: Optional[Decimal] = None
    product_impressions: Optional[int] = None
    product_clicks: Optional[int] = None
    product_click_rate: Optional[float] = None
    ad_click_rate: Optional[float] = None
    ad_conversion_rate: Optional[float] = None
    live_views: Optional[int] = None
    live_follows: Optional[int] = None


@dataclass(slots=True)
class _MetricsRow:
    metric: GmvCampaignMetricsDaily
    campaign_id: str
    store_id: Optional[str]


def _serialize_row(row: _MetricsRow) -> GMVMaxMetricDTO:
    return GMVMaxMetricDTO(
        stat_time_day=row.metric.stat_time_day,
        campaign_id=row.campaign_id,
        store_id=row.store_id,
        impressions=row.metric.impressions,
        clicks=row.metric.clicks,
        cost=_cents_to_amount(row.metric.cost_cents),
        net_cost=_cents_to_amount(row.metric.net_cost_cents),
        orders=row.metric.orders,
        cost_per_order=_to_decimal(row.metric.cost_per_order),
        gross_revenue=_cents_to_amount(row.metric.gross_revenue_cents),
        roi=_to_decimal(row.metric.roi),
        product_impressions=row.metric.product_impressions,
        product_clicks=row.metric.product_clicks,
        product_click_rate=_to_decimal(row.metric.product_click_rate),
        ad_click_rate=_to_decimal(row.metric.ad_click_rate),
        ad_conversion_rate=_to_decimal(row.metric.ad_conversion_rate),
        live_views=row.metric.live_views,
        live_follows=row.metric.live_follows,
    )


def _base_query(
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: str,
    store_id: str,
    start_date: date,
    end_date: date,
) -> Select[tuple[GmvCampaignMetricsDaily, str, Optional[str]]]:
    query: Select[tuple[GmvCampaignMetricsDaily, str, Optional[str]]] = (
        select(
            GmvCampaignMetricsDaily,
            GmvCampaign.campaign_id,
            GmvCampaignMetricsDaily.store_id,
        )
        .join(
            GmvCampaign,
            GmvCampaign.campaign_id == GmvCampaignMetricsDaily.campaign_id,
        )
        .where(GmvCampaign.workspace_id == int(workspace_id))
        .where(GmvCampaign.advertiser_id == str(advertiser_id))
        .where(GmvCampaign.campaign_id == str(campaign_id))
        .where(GmvCampaignMetricsDaily.store_id == str(store_id))
        .where(GmvCampaignMetricsDaily.stat_time_day >= start_date)
        .where(GmvCampaignMetricsDaily.stat_time_day <= end_date)
    )

    return query


def query_gmvmax_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: str,
    store_id: str,
    start_date: date,
    end_date: date,
    limit: int = 50,
    offset: int = 0,
    order_desc: bool = False,
) -> tuple[list[GMVMaxMetricDTO], int]:
    """Return stored GMV Max metrics for the requested filters."""

    base = _base_query(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
    )

    order_columns = (
        GmvCampaignMetricsDaily.stat_time_day.desc(),
        GmvCampaignMetricsDaily.id.desc(),
    )
    if not order_desc:
        order_columns = (
            GmvCampaignMetricsDaily.stat_time_day.asc(),
            GmvCampaignMetricsDaily.id.asc(),
        )

    stmt = base.order_by(*order_columns).limit(limit).offset(offset)

    rows = [
        _MetricsRow(metric=metric, campaign_id=campaign_key, store_id=store_key)
        for metric, campaign_key, store_key in db.execute(stmt).all()
    ]

    count_stmt = select(func.count()).select_from(base.subquery())
    total = int(db.execute(count_stmt).scalar_one())

    return [_serialize_row(row) for row in rows], total


__all__ = ["GMVMaxMetricDTO", "query_gmvmax_metrics"]
