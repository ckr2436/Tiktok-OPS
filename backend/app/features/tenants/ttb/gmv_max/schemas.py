from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel


class GmvMaxSyncResponse(BaseModel):
    synced: int


class GmvMaxCampaignOut(BaseModel):
    id: int
    campaign_id: str
    name: str
    status: str
    advertiser_id: str
    shopping_ads_type: Optional[str] = None
    optimization_goal: Optional[str] = None
    roas_bid: Optional[Decimal] = None
    daily_budget_cents: Optional[int] = None
    currency: Optional[str] = None
    ext_created_time: Optional[datetime] = None
    ext_updated_time: Optional[datetime] = None

    class Config:
        orm_mode = True


class GmvMaxCampaignListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[GmvMaxCampaignOut]


class GmvMaxMetricsPoint(BaseModel):
    ts: datetime | date
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    cost_cents: Optional[int] = None
    gross_revenue_cents: Optional[int] = None
    orders: Optional[int] = None
    roi: Optional[Decimal] = None


class GmvMaxMetricsResponse(BaseModel):
    granularity: Literal["hour", "day"]
    points: List[GmvMaxMetricsPoint]


class GmvMaxCampaignActionType(str, Enum):
    START = "START"
    PAUSE = "PAUSE"
    SET_BUDGET = "SET_BUDGET"
    SET_ROAS = "SET_ROAS"


class GmvMaxCampaignActionIn(BaseModel):
    action: GmvMaxCampaignActionType
    daily_budget_cents: Optional[int] = None
    roas_bid: Optional[Decimal] = None
    reason: Optional[str] = None


class GmvMaxCampaignActionOut(BaseModel):
    action: GmvMaxCampaignActionType
    result: str
    campaign: GmvMaxCampaignOut


async def _schemas_async_marker() -> None:  # pragma: no cover - helper for verify script
    """No-op async marker for verification script."""
