from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from fastapi import HTTPException, status
from typing import Any, Dict, List, Mapping, Optional, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendation,
    GMVMaxCampaign,
    GMVMaxCampaignCreateBody,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignUpdateBody,
    GMVMaxIdentity,
    GMVMaxIdentityInfo,
    GMVMaxReportData,
    GMVMaxOccupiedListData,
    GMVMaxSession,
    GMVMaxSessionProduct,
    GMVMaxSessionSettings,
    GMVMaxStoreAdUsageCheckData,
    GMVMaxVideoInfo,
    PageInfo,
)
from app.services.gmvmax_spec import (
    GMVMAX_DEFAULT_DIMENSIONS,
    GMVMAX_DEFAULT_METRICS,
    GMVMaxReportLevel,
)

DEFAULT_PROMOTION_TYPES: List[str] = ["PRODUCT", "LIVE"]
DEFAULT_METRICS: List[str] = list(GMVMAX_DEFAULT_METRICS)
DEFAULT_DIMENSIONS: List[str] = list(GMVMAX_DEFAULT_DIMENSIONS)
TIKTOK_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _advertiser_zone(advertiser_timezone: str | None) -> ZoneInfo | None:
    if not advertiser_timezone:
        return None
    try:
        return ZoneInfo(str(advertiser_timezone))
    except ZoneInfoNotFoundError:
        return None


def _format_tiktok_utc_datetime(
    value: datetime | None,
    advertiser_timezone: str | None = None,
) -> str | None:
    if value is None:
        return None
    tz = _advertiser_zone(advertiser_timezone)
    if value.tzinfo is not None:
        utc_value = value.astimezone(timezone.utc)
    elif tz is not None:
        utc_value = value.replace(tzinfo=tz).astimezone(timezone.utc)
    else:
        utc_value = value
    return utc_value.replace(tzinfo=None, microsecond=0).strftime(TIKTOK_DATETIME_FORMAT)


def _tiktok_now(advertiser_timezone: str | None = None) -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).strftime(TIKTOK_DATETIME_FORMAT)


class GmvMaxLevel(str, Enum):
    OVERVIEW = "OVERVIEW"
    CAMPAIGN = "CAMPAIGN"
    PRODUCT = "PRODUCT"
    LIVESTREAM = "LIVESTREAM"
    DURATION = "DURATION"
    CREATIVE = "CREATIVE"


def normalize_datetime_to_date(value: Any) -> Any:
    """Accept datetime-like values for date fields and coerce to ``date``.

    This keeps API compatibility when clients send ISO datetimes while the
    backend expects plain dates.
    """

    if value is None:
        return value

    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass

        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            return value

    return value

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
    page_size: Optional[int] = Field(default=None, ge=1, le=100)


class GMVMaxIdentityRequest(BaseModel):
    identity_id: str
    identity_type: Literal["AUTH_CODE", "TT_USER", "TTS_TT", "BC_AUTH_TT"]
    identity_authorized_bc_id: Optional[str] = None
    store_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideo(BaseModel):
    identity_info: GMVMaxIdentityRequest
    item_id: str
    spu_id_list: List[str]
    video_info: Optional[GMVMaxVideoInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxItemVideo(BaseModel):
    identity_info: GMVMaxIdentityRequest
    item_id: str
    spu_id_list: List[str]
    video_info: GMVMaxVideoInfo

    model_config = ConfigDict(extra="allow")


class CreateCampaignRequest(BaseModel):
    """Tenant payload for creating a *Product* GMV Max campaign.

    NOTE:
    - This schema currently targets Product GMV Max only.
    - Live GMV Max (LIVE shopping) is not supported here yet and should use a
      separate endpoint or an extended schema in the future (for example, a
      dedicated ``CreateLiveGmvMaxCampaignRequest`` or an explicit ``gmvmax_mode``
      flag that branches in the service layer).
    """

    # Legacy fields (kept for backward compatibility)
    name: Optional[str] = None
    objective_type: Optional[str] = None
    promotion_type: Optional[str] = None
    budget_mode: Optional[str] = None
    promotion_days: Optional[Dict[str, Any]] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    identity_ids: Optional[List[str]] = None

    # New fields aligned with TikTok Product GMV Max create
    advertiser_id: Optional[str] = None
    store_id: str
    store_authorized_bc_id: Optional[str] = None
    campaign_name: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=64)
    request_id: Optional[str] = Field(
        default=None,
        pattern=r"^[1-9]\d{0,18}$",
        max_length=19,
    )
    replacement_campaign_id: Optional[str] = Field(default=None, max_length=64)

    shopping_ads_type: Literal["PRODUCT"] = "PRODUCT"
    optimization_goal: Literal["VALUE"] = "VALUE"
    deep_bid_type: Literal["VO_MIN_ROAS"] = "VO_MIN_ROAS"

    product_specific_type: Literal["ALL", "CUSTOMIZED_PRODUCTS"] = "ALL"
    item_group_ids: Optional[List[str]] = Field(default=None, max_length=50)

    roas_bid: Optional[float] = None
    budget: Optional[int] = None

    schedule_type: Literal["SCHEDULE_FROM_NOW", "SCHEDULE_START_END", "SCHEDULE"] | None = None
    schedule_start_time: Optional[datetime] = None
    schedule_end_time: Optional[datetime] = None

    product_video_specific_type: Literal["AUTO_SELECTION", "CUSTOM_SELECTION"] = "AUTO_SELECTION"
    identity_list: Optional[List["GMVMaxIdentityRequest"]] = Field(
        default=None,
        max_length=20,
    )
    affiliate_posts_enabled: Optional[bool] = None
    custom_anchor_video_list: Optional[List["GMVMaxCustomAnchorVideo"]] = None
    item_list: Optional[List["GMVMaxItemVideo"]] = None
    automation: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _alias_campaign_name(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("campaign_name") is None and values.get("name") is not None:
            values["campaign_name"] = values.get("name")
        request_id = values.get("request_id")
        idempotency_key = values.get("idempotency_key")
        if idempotency_key is None and request_id is not None:
            values["idempotency_key"] = str(request_id)
        elif (
            request_id is not None
            and idempotency_key is not None
            and str(request_id) != str(idempotency_key)
        ):
            raise ValueError("request_id and idempotency_key must match")
        return values

    def _resolved_schedule_type(self) -> Optional[str]:
        schedule_type = self.schedule_type
        if schedule_type is None and (self.schedule_start_time or self.schedule_end_time):
            schedule_type = "SCHEDULE_START_END"
        if schedule_type is None and (self.start_time or self.end_time):
            schedule_type = "SCHEDULE"
        return schedule_type

    def to_client_body(
        self,
        *,
        store_authorized_bc_id: str | None = None,
        advertiser_timezone: str | None = None,
        official_request_id: str | None = None,
    ) -> GMVMaxCampaignCreateBody:
        campaign_name = self.campaign_name or self.name
        if not campaign_name:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "GMVMAX_INVALID_REQUEST", "message": "campaign_name is required"},
            )

        product_specific_type = self.product_specific_type or "ALL"
        item_group_ids = self.item_group_ids
        if product_specific_type == "CUSTOMIZED_PRODUCTS" and not item_group_ids:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "GMVMAX_INVALID_REQUEST",
                    "message": "item_group_ids are required for CUSTOMIZED_PRODUCTS",
                },
            )

        product_video_specific_type = self.product_video_specific_type or "AUTO_SELECTION"
        if product_video_specific_type == "CUSTOM_SELECTION" and not self.item_list:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "GMVMAX_INVALID_REQUEST",
                    "message": "item_list is required for CUSTOM_SELECTION",
                },
            )

        schedule_type = self._resolved_schedule_type()
        schedule_start = self.schedule_start_time or self.start_time
        schedule_end = self.schedule_end_time or self.end_time
        if schedule_type == "SCHEDULE_FROM_NOW":
            schedule_start_payload = _tiktok_now(advertiser_timezone)
        else:
            schedule_start_payload = _format_tiktok_utc_datetime(schedule_start, advertiser_timezone)
        schedule_end_payload = _format_tiktok_utc_datetime(schedule_end, advertiser_timezone)
        if schedule_type == "SCHEDULE_FROM_NOW" and schedule_start is None:
            schedule_start = datetime.now()
        if schedule_type == "SCHEDULE_START_END" and (schedule_start is None or schedule_end is None):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "GMVMAX_INVALID_REQUEST",
                    "message": "schedule_start_time and schedule_end_time are required",
                },
            )

        identity_list: list[dict[str, Any]] | None = None
        if self.identity_list is not None:
            identity_list = [
                identity.model_dump(exclude_none=True) for identity in self.identity_list
            ]
        elif self.identity_ids:
            identity_list = [{"identity_id": str(identity_id)} for identity_id in self.identity_ids]

        custom_anchor_video_list = (
            [anchor.model_dump(exclude_none=True) for anchor in self.custom_anchor_video_list]
            if self.custom_anchor_video_list
            else None
        )
        item_list = (
            [item.model_dump(exclude_none=True) for item in self.item_list]
            if self.item_list
            else None
        )

        payload: Dict[str, Any] = {
            "request_id": official_request_id or self.request_id,
            "store_id": str(self.store_id),
            # Product GMV Max only: shopping_ads_type is locked to PRODUCT.
            "store_authorized_bc_id": store_authorized_bc_id or self.store_authorized_bc_id,
            # Product GMV Max uses VALUE as optimization_goal and VO_MIN_ROAS as deep_bid_type.
            "shopping_ads_type": "PRODUCT",
            "optimization_goal": "VALUE",
            "deep_bid_type": "VO_MIN_ROAS",
            "campaign_name": campaign_name,
            "budget": float(self.budget) if self.budget is not None else None,
            "roas_bid": float(self.roas_bid) if self.roas_bid is not None else None,
            "promotion_days": self.promotion_days,
            "schedule_type": schedule_type,
            "schedule_start_time": schedule_start_payload,
            "schedule_end_time": schedule_end_payload,
            "product_specific_type": product_specific_type,
            "item_group_ids": [str(item) for item in item_group_ids] if item_group_ids else None,
            "product_video_specific_type": product_video_specific_type,
            "identity_list": identity_list,
            "affiliate_posts_enabled": self.affiliate_posts_enabled,
            "custom_anchor_video_list": custom_anchor_video_list,
            "item_list": item_list,
        }
        # TODO(live-gmvmax): When adding Live GMV Max support, either split this
        # method or branch on an explicit mode flag instead of assuming PRODUCT
        # defaults here.

        cleaned = {key: value for key, value in payload.items() if value is not None}
        return GMVMaxCampaignCreateBody(**cleaned)


class UpdateCampaignRequest(BaseModel):
    """Tenant payload for updating an existing GMV Max campaign."""

    name: Optional[str] = None
    item_group_ids: Optional[List[str]] = Field(default=None, max_length=50)
    daily_budget: Optional[float] = Field(default=None, gt=0)
    roas_bid: Optional[float] = Field(default=None, gt=0)
    promotion_days: Optional[Dict[str, Any]] = None
    schedule_type: Optional[Literal["SCHEDULE_FROM_NOW", "SCHEDULE_START_END"]] = None
    end_time: Optional[datetime] = None
    affiliate_posts_enabled: Optional[bool] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("roas_bid", mode="before")
    @classmethod
    def _normalize_roas_bid(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        # TikTok's GMV Max update contract accepts a positive ROI target with
        # at most one decimal place. Normalize at our public API boundary so
        # the persisted local fact and the official request cannot diverge.
        from decimal import Decimal, ROUND_HALF_UP

        parsed = Decimal(str(value))
        if parsed <= 0:
            raise ValueError("roas_bid must be greater than 0")
        return float(parsed.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

    @model_validator(mode="after")
    def _validate_update(self) -> "UpdateCampaignRequest":
        if not self.model_dump(exclude_none=True):
            raise ValueError("at least one campaign update field is required")
        if self.schedule_type == "SCHEDULE_START_END" and self.end_time is None:
            raise ValueError("end_time is required for SCHEDULE_START_END")
        if self.schedule_type == "SCHEDULE_FROM_NOW" and self.end_time is not None:
            raise ValueError("end_time is invalid for SCHEDULE_FROM_NOW")
        return self

    def to_client_body(
        self,
        *,
        campaign_id: str,
        advertiser_timezone: str | None = None,
    ) -> GMVMaxCampaignUpdateBody:
        schedule_type = self.schedule_type
        if schedule_type is None and self.end_time:
            schedule_type = "SCHEDULE_START_END"

        payload: Dict[str, Any] = {
            "campaign_id": str(campaign_id),
            "campaign_name": self.name,
            "budget": float(self.daily_budget) if self.daily_budget is not None else None,
            "roas_bid": float(self.roas_bid) if self.roas_bid is not None else None,
            "promotion_days": self.promotion_days,
            "schedule_type": schedule_type,
            "schedule_end_time": _format_tiktok_utc_datetime(self.end_time, advertiser_timezone),
            "affiliate_posts_enabled": self.affiliate_posts_enabled,
        }
        if self.item_group_ids is not None:
            payload["item_group_ids"] = [str(item) for item in self.item_group_ids]

        cleaned = {key: value for key, value in payload.items() if value is not None}
        return GMVMaxCampaignUpdateBody(**cleaned)


class ReportFiltering(BaseModel):
    """Filtering block for GMV Max report requests."""

    gmv_max_promotion_types: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")


class ReportRequest(BaseModel):
    """Tenant level request body for metrics/report endpoints."""

    store_ids: Optional[List[str]] = None
    start_date: date
    end_date: date
    level: GMVMaxReportLevel | None = Field(default=None, description="Aggregation level")
    metrics: List[str]
    dimensions: List[str]
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[ReportFiltering] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=1000)
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> Any:
        return normalize_datetime_to_date(value)


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
    report: Optional[ReportRequest] = None

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


class SyncTaskResponse(BaseModel):
    """Async sync trigger response with Celery task metadata."""

    task_id: str
    state: str
    status_url: Optional[str] = None


# Alias for async operations that fetch external data via Celery workers.
AsyncTaskResponse = SyncTaskResponse


class GmvMaxManualSyncRequest(BaseModel):
    """Manual sync payload scoped to a single GMV Max auth account."""

    start_date: date | None = None
    end_date: date | None = None
    levels: List[GmvMaxLevel]
    campaign_ids: Optional[List[str]] = None
    item_group_ids: Optional[List[str]] = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> Any:
        return normalize_datetime_to_date(value)


class GmvMaxManualSyncResult(BaseModel):
    results: Dict[GmvMaxLevel, Dict[str, int]]


class BalanceSyncRequest(BaseModel):
    """Request body for advertiser balance sync."""

    bc_id: Optional[str] = None
    advertiser_id: Optional[str] = None
    store_id: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class SyncTaskStateResponse(BaseModel):
    """Status payload for GMV Max sync Celery tasks."""

    task_id: str
    state: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None


class SyncIntervalUpdateRequest(BaseModel):
    interval: int = Field(..., description="Interval in minutes: 10/15/20/30")


class SyncIntervalResponse(BaseModel):
    interval: int
    available: List[int]
    message: Optional[str] = None


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
    creation_status: Optional[
        Literal["SUCCEEDED", "QUARANTINED", "QUARANTINE_PENDING"]
    ] = None
    idempotency_key: Optional[str] = None
    warnings: List[Dict[str, Any]] = Field(default_factory=list)


class MetricsRequest(BaseModel):
    """Request payload for metrics endpoints."""

    store_ids: Optional[List[str]] = None
    start_date: date
    end_date: date
    level: GMVMaxReportLevel | None = Field(default=None, description="Aggregation level")
    metrics: Optional[List[str]] = None
    dimensions: Optional[List[str]] = None
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[ReportFiltering] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=1000)
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> Any:
        return normalize_datetime_to_date(value)


class MetricsFreshness(BaseModel):
    """Describe the persisted report snapshot returned to the frontend."""

    state: Literal["fresh", "stale", "missing", "historical"]
    source: str
    last_synced_at: Optional[datetime] = None
    age_seconds: Optional[int] = None
    max_age_seconds: int
    row_count: int = 0
    advertiser_timezone: str = "UTC"
    start_date: date
    end_date: date
    is_realtime: bool = False
    message: Optional[str] = None


class MetricsResponse(BaseModel):
    """Proxy payload for metrics queries."""

    report: GMVMaxReportData
    request_id: Optional[str] = None
    freshness: Optional[MetricsFreshness] = None


class CampaignActionRequest(BaseModel):
    """Action payload accepted by the campaign actions route."""

    type: Literal["pause", "enable", "delete", "update_budget", "update_strategy"] = Field(
        validation_alias=AliasChoices("type", "action_type")
    )
    payload: Dict[str, Any] = Field(default_factory=dict)
    disable_strategy: bool = False

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
    status: Literal["success", "failed", "queued"]
    response: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None


class CreativeHeatingActionRequest(BaseModel):
    """Payload accepted for creative-level GMV Max actions."""

    action_type: Literal["BOOST_CREATIVE", "REMOVE_CREATIVE", "ADD_BACK_CREATIVE"]
    creative_id: str
    item_ids: Optional[List[str]] = None
    item_list: Optional[List[Dict[str, Any]]] = None
    spu_id_list: Optional[List[str]] = None
    action: Optional[Literal["REMOVE", "ADD"]] = None
    operation_type: Optional[str] = None
    mode: Optional[str] = None
    target_daily_budget: Optional[float] = Field(default=None, ge=10)
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
        if self.action_type != "BOOST_CREATIVE":
            return self
        if str(self.mode or "").upper() in {"STOP", "STOP_CREATIVE", "STOP_BOOST"}:
            return self
        if self.target_daily_budget is None:
            raise ValueError("target_daily_budget is required for creative boost")
        if self.budget_delta is not None:
            raise ValueError("budget_delta is not supported for creative boost")
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
    product_id: Optional[str] = None
    item_id: Optional[str] = None
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
    """Response returned when applying a creative-level GMV Max action."""

    action_type: Literal["BOOST_CREATIVE", "REMOVE_CREATIVE", "ADD_BACK_CREATIVE"]
    heating: CreativeHeatingRecord
    tiktok_response: Optional[Dict[str, Any]] = None
    request_id: Optional[str] = None


class CreativeHeatingListResponse(BaseModel):
    """List response for creative heating states."""

    items: List[CreativeHeatingRecord] = Field(default_factory=list)


class StrategyResponse(BaseModel):
    """Strategy payload combining campaign, session, and recommendations."""

    campaign: GMVMaxCampaignInfoData
    sessions: List[GMVMaxSession] = Field(default_factory=list)
    sessions_page_info: Optional[PageInfo] = None
    recommendation: Optional[GMVMaxBidRecommendation] = None
    enabled: Optional[bool] = None
    target_roi: Optional[float] = None
    min_roi: Optional[float] = None
    max_roi: Optional[float] = None
    min_impressions: Optional[int] = None
    min_clicks: Optional[int] = None
    max_budget_raise_pct_per_day: Optional[float] = None
    max_budget_cut_pct_per_day: Optional[float] = None
    max_roas_step_per_adjust: Optional[float] = None
    cooldown_minutes: Optional[int] = None
    min_runtime_minutes_before_first_change: Optional[int] = None
    config_json: Optional[Dict[str, Any]] = None
    campaign_request_id: Optional[str] = None
    sessions_request_id: Optional[str] = None
    recommendation_request_id: Optional[str] = None


class StrategyCampaignPatch(BaseModel):
    """Subset of campaign fields that can be updated through the strategy route."""

    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    promotion_days: Optional[Dict[str, Any]] = None
    schedule_type: Optional[Literal["SCHEDULE_FROM_NOW", "SCHEDULE_START_END"]] = None
    schedule_end_time: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class StrategySessionPatch(BaseModel):
    """Session level fields accepted by the strategy update route."""

    session_id: str
    store_id: Optional[str] = None
    session: Optional[GMVMaxSessionSettings] = None
    product_list: Optional[List[GMVMaxSessionProduct]] = None

    model_config = ConfigDict(extra="forbid")


class StrategyUpdateRequest(BaseModel):
    """Payload accepted by PUT strategy route."""

    campaign: Optional[StrategyCampaignPatch] = None
    session: Optional[StrategySessionPatch] = None
    enabled: Optional[bool] = None
    target_roi: Optional[float] = None
    min_roi: Optional[float] = None
    max_roi: Optional[float] = None
    min_impressions: Optional[int] = None
    min_clicks: Optional[int] = None
    max_budget_raise_pct_per_day: Optional[float] = None
    max_budget_cut_pct_per_day: Optional[float] = None
    max_roas_step_per_adjust: Optional[float] = None
    cooldown_minutes: Optional[int] = None
    min_runtime_minutes_before_first_change: Optional[int] = None
    auto_heating_enabled: Optional[bool] = None
    auto_pause_roi_threshold: Optional[float] = None
    config_json: Optional[Dict[str, Any]] = None


class StrategyUpdateResponse(BaseModel):
    """Response returned by PUT strategy route."""

    status: Literal["success", "partial", "noop", "failed"]
    campaign: Optional[GMVMaxCampaignInfoData] = None
    sessions: Optional[List[GMVMaxSession]] = None
    strategy: Optional[Dict[str, Any]] = None
    campaign_request_id: Optional[str] = None
    session_request_id: Optional[str] = None


class StrategyPreviewRequest(BaseModel):
    """Request payload accepted by the strategy preview route."""

    store_id: Optional[str] = None
    shopping_ads_type: Optional[Literal["PRODUCT", "LIVE"]] = None
    optimization_goal: Optional[Literal["VALUE"]] = None
    item_group_ids: Optional[List[str]] = Field(default=None, max_length=50)
    identity_id: Optional[str] = None

    @model_validator(mode="after")
    def _validate_bid_scope(self) -> "StrategyPreviewRequest":
        if self.shopping_ads_type == "LIVE":
            if not str(self.identity_id or "").strip():
                raise ValueError("identity_id is required for LIVE recommendations")
            if self.item_group_ids:
                raise ValueError("item_group_ids is only valid for PRODUCT")
        elif self.shopping_ads_type == "PRODUCT" and self.identity_id is not None:
            raise ValueError("identity_id is only valid for LIVE")
        return self


class StrategyPreviewResponse(BaseModel):
    """Preview response returning bid recommendations."""

    status: Literal["success", "failed"]
    recommendation: Optional[GMVMaxBidRecommendation] = None
    request_id: Optional[str] = None


class ActionLogEntry(BaseModel):
    """Stored campaign action logs returned by the actions list route."""

    entries: List[Dict[str, Any]] = Field(default_factory=list)
    total: Optional[int] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1)


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
    binding_changed: bool = False
    bootstrap_task_id: Optional[str] = None
    bootstrap_enqueued: bool = False


class BindingStatusResponse(BaseModel):
    """Summarized status for GMV Max advertiser-store binding."""

    has_binding: bool
    binding_ready: bool
    last_checked_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    advertiser_id: Optional[str] = None
    bc_id: Optional[str] = None
    store_id: Optional[str] = None


class OccupiedAdSummary(BaseModel):
    """Lightweight summary for VS/PS occupied ads."""

    ad_id: Optional[str] = None
    campaign_id: Optional[str] = None
    advertiser_id: Optional[str] = None
    item_group_id: Optional[str] = None
    create_time: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class IdentitySummary(BaseModel):
    """Identity entry usable for Product GMV Max."""

    identity_id: Optional[str] = None
    identity_type: Optional[str] = None
    user_name: Optional[str] = None
    identity_name: Optional[str] = None
    profile_image: Optional[str] = None
    product_gmv_max_available: Optional[bool] = None
    identity_authorized_bc_id: Optional[str] = None
    identity_authorized_bc_name: Optional[str] = None
    identity_authorized_shop_id: Optional[str] = None
    store_id: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class IdentityListResponse(BaseModel):
    """Stable frontend contract for eligible GMV Max identities."""

    identities: List[IdentitySummary] = Field(default_factory=list)
    identity_list: List[IdentitySummary] = Field(default_factory=list)
    items: List[IdentitySummary] = Field(default_factory=list)
    request_id: Optional[str] = None


class VideoSummary(BaseModel):
    """Video entry usable for Product GMV Max."""

    item_id: Optional[str] = None
    video_id: Optional[str] = None
    preview_url: Optional[str] = None
    video_cover_url: Optional[str] = None
    duration: Optional[float] = None

    model_config = ConfigDict(extra="ignore")


class CustomAnchorVideoSummary(BaseModel):
    """Custom anchor video entry."""

    custom_anchor_video_id: Optional[str] = None
    video_id: Optional[str] = None
    preview_url: Optional[str] = None
    video_cover_url: Optional[str] = None
    duration: Optional[float] = None

    model_config = ConfigDict(extra="ignore")


class GMVMaxPrecheckRequest(BaseModel):
    """Request payload for GMV Max asset preflight checks."""

    store_id: str
    store_authorized_bc_id: str
    advertiser_id: Optional[str] = None
    identity_id: Optional[str] = None
    product_item_group_ids: Optional[List[str]] = None
    occupied_asset_type: Optional[str] = None
    product_specific_type: Optional[Literal["ALL", "CUSTOMIZED_PRODUCTS"]] = None
    item_group_ids: Optional[List[str]] = None


class GMVMaxPrecheckResponse(BaseModel):
    """Aggregated payload combining store, identity, and occupancy checks."""

    # Store & auth
    is_gmv_max_available: bool
    needs_exclusive_auth: bool
    current_authorized_advertiser_id: Optional[str] = None

    # Shop level usage
    promote_all_products_allowed: bool
    has_running_custom_shop_ads: bool
    occupied_custom_shop_ads: List[OccupiedAdSummary] = Field(default_factory=list)

    # Product occupancy
    unoccupied_item_group_ids: List[str] = Field(default_factory=list)
    occupied_item_group_ids: List[str] = Field(default_factory=list)

    # Creative resources
    available_identities: List[IdentitySummary] = Field(default_factory=list)
    available_videos: List[VideoSummary] = Field(default_factory=list)
    available_custom_anchor_videos: List[CustomAnchorVideoSummary] = Field(
        default_factory=list
    )

    # Recommendations
    recommended_roas_bid: Optional[float] = None
    recommended_budget: Optional[int] = None

    # Legacy fields for backward compatibility
    store_usage: Optional[GMVMaxStoreAdUsageCheckData] = None
    identities: List[GMVMaxIdentity] = Field(default_factory=list)
    occupancy: Optional[GMVMaxOccupiedListData] = None
    request_ids: Dict[str, Optional[str]] = Field(default_factory=dict)


async def _schemas_async_marker() -> None:  # pragma: no cover - helper for verify script
    """No-op async marker for verification script."""
