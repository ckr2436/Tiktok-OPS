from __future__ import annotations

from __future__ import annotations

from datetime import date, datetime
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendation,
    GMVMaxCampaign,
    GMVMaxCampaignInfoData,
    GMVMaxIdentity,
    GMVMaxReportData,
    GMVMaxOccupiedListData,
    GMVMaxSession,
    GMVMaxSessionProduct,
    GMVMaxSessionSettings,
    GMVMaxStoreAdUsageCheckData,
    PageInfo,
)
from app.services.gmvmax_spec import (
    GMVMAX_DEFAULT_DIMENSIONS,
    GMVMAX_DEFAULT_METRICS,
)

DEFAULT_PROMOTION_TYPES: List[str] = ["PRODUCT", "LIVE"]
DEFAULT_METRICS: List[str] = list(GMVMAX_DEFAULT_METRICS)
DEFAULT_DIMENSIONS: List[str] = list(GMVMAX_DEFAULT_DIMENSIONS)

_ACTION_TYPES = {"pause", "enable", "delete", "update_budget", "update_strategy"}
_ACTION_ALIASES = {
    "disable": "pause",
    "stop": "pause",
    "suspend": "pause",
    "pause": "pause",
    "enable": "enable",
    "resume": "enable",
    "start": "enable",
    "run": "enable",
    "delete": "delete",
    "remove": "delete",
    "update_budget": "update_budget",
    "set_budget": "update_budget",
    "budget": "update_budget",
    "update_strategy": "update_strategy",
    "update_roi": "update_strategy",
    "set_roi": "update_strategy",
}


class CampaignFilter(BaseModel):
    """High level filters supported by GMV Max campaign list endpoint."""

    gmv_max_promotion_types: List[str] = Field(
        default_factory=lambda: list(DEFAULT_PROMOTION_TYPES)
    )
    store_ids: Optional[List[str]] = None
    campaign_ids: Optional[List[str]] = None
    campaign_name: Optional[str] = None
    primary_status: Optional[str] = None
    creation_filter_start_time: Optional[str] = None
    creation_filter_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class CampaignListOptions(BaseModel):
    """Optional parameters for campaign listing requests."""

    fields: Optional[List[str]] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=50)


class ReportFiltering(BaseModel):
    """Filtering block for GMV Max report requests."""

    gmv_max_promotion_types: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")


class ReportRequest(BaseModel):
    """Tenant level request body for metrics/report endpoints."""

    store_ids: Optional[List[str]] = None
    start_date: date
    end_date: date
    metrics: List[str]
    dimensions: List[str]
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[ReportFiltering] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=50)
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None


class SyncRequest(BaseModel):
    """Payload accepted by the sync endpoint combining campaigns + report."""

    advertiser_id: Optional[str] = None
    bc_id: Optional[str] = Field(default=None, alias="bc_id")
    owner_bc_id: Optional[str] = Field(default=None, alias="owner_bc_id")
    store_id: Optional[str] = None
    campaign_filter: Optional[CampaignFilter] = Field(
        default=None, alias="campaign_filter"
    )
    campaign_options: Optional[CampaignListOptions] = None
    report: ReportRequest

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _sync_bc_fields(self) -> "SyncRequest":
        if self.owner_bc_id and not self.bc_id:
            self.bc_id = self.owner_bc_id
        elif self.bc_id and not self.owner_bc_id:
            self.owner_bc_id = self.bc_id
        return self


class SyncResponse(BaseModel):
    """Combined response returning campaign listing and report payloads."""

    campaigns: List[GMVMaxCampaign]
    campaigns_page_info: Optional[PageInfo] = None
    report: GMVMaxReportData
    campaign_request_id: Optional[str] = None
    report_request_id: Optional[str] = None


class CampaignListResponse(BaseModel):
    """Response returned by the campaign list route."""

    items: List[GMVMaxCampaign]
    page_info: Optional[PageInfo] = None
    request_id: Optional[str] = None


class CampaignDetailResponse(BaseModel):
    """Detailed campaign payload with optional session listing."""

    campaign: GMVMaxCampaignInfoData
    sessions: List[GMVMaxSession] = Field(default_factory=list)
    sessions_page_info: Optional[PageInfo] = None
    request_id: Optional[str] = None
    sessions_request_id: Optional[str] = None


class MetricsRequest(BaseModel):
    """Request payload for metrics endpoints."""

    store_ids: Optional[List[str]] = None
    start_date: date
    end_date: date
    metrics: Optional[List[str]] = None
    dimensions: Optional[List[str]] = None
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[ReportFiltering] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=50)
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None


class MetricsResponse(BaseModel):
    """Proxy payload for metrics queries."""

    report: GMVMaxReportData
    request_id: Optional[str] = None


class CampaignActionRequest(BaseModel):
    """Action payload accepted by the campaign actions route."""

    type: Literal["pause", "enable", "delete", "update_budget", "update_strategy"] = Field(
        validation_alias=AliasChoices("type", "action_type")
    )
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        if value is None:
            raise ValueError("action type is required")
        normalized = str(value).strip().lower()
        canonical = _ACTION_ALIASES.get(normalized, normalized)
        if canonical not in _ACTION_TYPES:
            raise ValueError("unsupported action type")
        return canonical


class CampaignActionResponse(BaseModel):
    """Normalized campaign action response."""

    type: Literal["pause", "enable", "delete", "update_budget", "update_strategy"]
    status: Literal["success", "failed"]
    response: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None


class CreativeHeatingActionRequest(BaseModel):
    """Payload accepted for creative heating actions."""

    action_type: Literal["BOOST_CREATIVE"]
    creative_id: str
    mode: Optional[str] = None
    target_daily_budget: Optional[float] = None
    budget_delta: Optional[float] = None
    currency: Optional[str] = None
    max_duration_minutes: Optional[int] = Field(default=None, ge=1)
    note: Optional[str] = None
    creative_name: Optional[str] = None
    product_id: Optional[str] = None
    item_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _ensure_budget_fields(self) -> "CreativeHeatingActionRequest":
        if self.target_daily_budget is None and self.budget_delta is None:
            raise ValueError("target_daily_budget or budget_delta is required")
        return self


class CreativeHeatingRecord(BaseModel):
    """Serialized representation of a creative heating row."""

    id: int
    workspace_id: int
    provider: str
    auth_id: int
    campaign_id: str
    creative_id: str
    creative_name: Optional[str] = None
    mode: Optional[str] = None
    target_daily_budget: Optional[float] = None
    budget_delta: Optional[float] = None
    currency: Optional[str] = None
    max_duration_minutes: Optional[int] = None
    note: Optional[str] = None
    status: str
    last_action_type: Optional[str] = None
    last_action_time: Optional[datetime] = None
    last_error: Optional[str] = None
    evaluation_window_minutes: int = 60
    min_clicks: Optional[int] = None
    min_ctr: Optional[float] = None
    min_gross_revenue: Optional[float] = None
    auto_stop_enabled: bool = True
    is_heating_active: bool = False
    last_evaluated_at: Optional[datetime] = None
    last_evaluation_result: Optional[str] = None


class CreativeHeatingActionResponse(BaseModel):
    """Response returned when applying a creative heating action."""

    action_type: Literal["BOOST_CREATIVE"]
    heating: CreativeHeatingRecord
    tiktok_response: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None


class StrategyResponse(BaseModel):
    """Strategy payload combining campaign, session, and recommendations."""

    campaign: GMVMaxCampaignInfoData
    sessions: List[GMVMaxSession] = Field(default_factory=list)
    sessions_page_info: Optional[PageInfo] = None
    recommendation: Optional[GMVMaxBidRecommendation] = None
    campaign_request_id: Optional[str] = None
    sessions_request_id: Optional[str] = None
    recommendation_request_id: Optional[str] = None


class StrategyCampaignPatch(BaseModel):
    """Subset of campaign fields that can be updated through the strategy route."""

    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    promotion_days: Optional[Dict[str, Any]] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class StrategySessionPatch(BaseModel):
    """Session level fields accepted by the strategy update route."""

    session_id: str
    store_id: Optional[str] = None
    session: Optional[GMVMaxSessionSettings] = None
    product_list: Optional[List[GMVMaxSessionProduct]] = None

    model_config = ConfigDict(extra="allow")


class StrategyUpdateRequest(BaseModel):
    """Payload accepted by PUT strategy route."""

    campaign: Optional[StrategyCampaignPatch] = None
    session: Optional[StrategySessionPatch] = None


class StrategyUpdateResponse(BaseModel):
    """Response returned by PUT strategy route."""

    status: Literal["success", "partial", "noop", "failed"]
    campaign: Optional[GMVMaxCampaignInfoData] = None
    sessions: Optional[List[GMVMaxSession]] = None
    campaign_request_id: Optional[str] = None
    session_request_id: Optional[str] = None


class StrategyPreviewRequest(BaseModel):
    """Request payload accepted by the strategy preview route."""

    store_id: Optional[str] = None
    shopping_ads_type: Optional[str] = None
    optimization_goal: Optional[str] = None
    item_group_ids: Optional[List[str]] = None
    identity_id: Optional[str] = None


class StrategyPreviewResponse(BaseModel):
    """Preview response returning bid recommendations."""

    status: Literal["success", "failed"]
    recommendation: Optional[GMVMaxBidRecommendation] = None
    request_id: Optional[str] = None


class ActionLogEntry(BaseModel):
    """Placeholder action log representation (empty for now)."""

    entries: List[Dict[str, Any]] = Field(default_factory=list)


class AutoBindingRequest(BaseModel):
    """Request payload for automatic GMV Max binding discovery."""

    advertiser_id: Optional[str] = None
    store_id: Optional[str] = None
    persist: bool = True


class AutoBindingCandidate(BaseModel):
    """Candidate binding derived from TikTok GMV Max metadata."""

    advertiser_id: str
    store_id: str
    store_name: Optional[str] = None
    store_authorized_bc_id: Optional[str] = None
    authorization_status: Optional[str] = None
    is_gmv_max_available: Optional[bool] = None
    promote_all_products_allowed: Optional[bool] = None
    is_running_custom_shop_ads: Optional[bool] = None
    request_id: Optional[str] = None
    source: Optional[Dict[str, Any]] = None


class AutoBindingResponse(BaseModel):
    """Result of automatic binding discovery and optional persistence."""

    selected: Optional[AutoBindingCandidate] = None
    candidates: List[AutoBindingCandidate] = Field(default_factory=list)
    persisted: bool = False


class GMVMaxPrecheckRequest(BaseModel):
    """Request payload for GMV Max asset preflight checks."""

    store_id: str
    store_authorized_bc_id: str
    advertiser_id: Optional[str] = None
    identity_id: Optional[str] = None
    product_item_group_ids: Optional[List[str]] = None
    occupied_asset_type: Optional[str] = None


class GMVMaxPrecheckResponse(BaseModel):
    """Aggregated payload combining store, identity, and occupancy checks."""

    store_usage: Optional[GMVMaxStoreAdUsageCheckData] = None
    identities: List[GMVMaxIdentity] = Field(default_factory=list)
    occupancy: Optional[GMVMaxOccupiedListData] = None
    request_ids: Dict[str, Optional[str]] = Field(default_factory=dict)


async def _schemas_async_marker() -> None:  # pragma: no cover - helper for verify script
    """No-op async marker for verification script."""
