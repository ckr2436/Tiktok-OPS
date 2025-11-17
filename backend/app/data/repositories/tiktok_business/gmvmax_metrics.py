"""Database helpers for querying GMV Max campaign metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import (
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
)


def _cents_to_amount(value: Optional[int]) -> float | None:
    if value is None:
        return None
    return float(Decimal(value) / Decimal(100))


def _to_float(value: Optional[Decimal | float | int]) -> float | None:
    if value is None:
        return None
    return float(value)


class GMVMaxMetricDTO(BaseModel):
    """Serialized view of GMV Max metrics stored in MySQL."""

    stat_time_day: date
    campaign_id: str
    store_id: Optional[str] = None
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    cost: Optional[float] = None
    net_cost: Optional[float] = None
    orders: Optional[int] = None
    cost_per_order: Optional[float] = None
    gross_revenue: Optional[float] = None
    roi: Optional[float] = None
    product_impressions: Optional[int] = None
    product_clicks: Optional[int] = None
    product_click_rate: Optional[float] = None
    ad_click_rate: Optional[float] = None
    ad_conversion_rate: Optional[float] = None
    live_views: Optional[int] = None
    live_follows: Optional[int] = None


@dataclass(slots=True)
class _MetricsRow:
    metric: TTBGmvMaxMetricsDaily
    campaign_id: str
    store_id: Optional[str]


def _serialize_row(row: _MetricsRow) -> GMVMaxMetricDTO:
    cost = _cents_to_amount(row.metric.cost_cents)
    orders = row.metric.orders or 0
    cost_per_order = None
    if cost is not None and orders > 0:
        cost_per_order = cost / orders

    return GMVMaxMetricDTO(
        stat_time_day=row.metric.date,
        campaign_id=row.campaign_id,
        store_id=row.store_id,
        impressions=row.metric.impressions,
        clicks=row.metric.clicks,
        cost=cost,
        net_cost=_cents_to_amount(row.metric.net_cost_cents),
        orders=row.metric.orders,
        cost_per_order=cost_per_order,
        gross_revenue=_cents_to_amount(row.metric.gross_revenue_cents),
        roi=_to_float(row.metric.roi),
        product_impressions=row.metric.product_impressions,
        product_clicks=row.metric.product_clicks,
        product_click_rate=_to_float(row.metric.product_click_rate),
        ad_click_rate=_to_float(row.metric.ad_click_rate),
        ad_conversion_rate=_to_float(row.metric.ad_conversion_rate),
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
) -> Select[tuple[TTBGmvMaxMetricsDaily, str, Optional[str]]]:
    query: Select[tuple[TTBGmvMaxMetricsDaily, str, Optional[str]]] = (
        select(
            TTBGmvMaxMetricsDaily,
            TTBGmvMaxCampaign.campaign_id,
            TTBGmvMaxCampaign.store_id,
        )
        .join(
            TTBGmvMaxCampaign,
            TTBGmvMaxCampaign.id == TTBGmvMaxMetricsDaily.campaign_id,
        )
        .where(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
        .where(TTBGmvMaxCampaign.auth_id == int(auth_id))
        .where(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
        .where(TTBGmvMaxCampaign.store_id == str(store_id))
        .where(TTBGmvMaxMetricsDaily.date >= start_date)
        .where(TTBGmvMaxMetricsDaily.date <= end_date)
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

    stmt = (
        base.order_by(
            TTBGmvMaxMetricsDaily.date.asc(),
            TTBGmvMaxMetricsDaily.id.asc(),
        )
        .limit(limit)
        .offset(offset)
    )

    rows = [
        _MetricsRow(metric=metric, campaign_id=campaign_key, store_id=store_key)
        for metric, campaign_key, store_key in db.execute(stmt).all()
    ]

    count_stmt = select(func.count()).select_from(base.subquery())
    total = int(db.execute(count_stmt).scalar_one())

    return [_serialize_row(row) for row in rows], total


__all__ = ["GMVMaxMetricDTO", "query_gmvmax_metrics"]
