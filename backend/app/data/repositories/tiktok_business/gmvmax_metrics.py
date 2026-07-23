"""Database helpers for querying GMV Max campaign metrics."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


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
__all__ = ["GMVMaxMetricDTO"]
