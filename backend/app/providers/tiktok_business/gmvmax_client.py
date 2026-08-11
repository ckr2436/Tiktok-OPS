"""Typed TikTok Business GMV Max client built on top of :mod:`app.services.ttb_api`.

This module acts as the TikTok Business GMV Max **client** layer, keeping
request/response models aligned with the official API surface.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import (
    Any,
    Dict,
    Generic,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Type,
    TypeVar,
)
from datetime import date, datetime

import json

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enum import Enum

from app.services import ttb_api as _ttb_api
from app.services.ttb_api import TTBApiClient


_TIKTOK_ROAS_QUANT = Decimal("0.1")


def _normalize_tiktok_roas_bid(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        normalized = Decimal(str(value)).quantize(_TIKTOK_ROAS_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("roas_bid must be a positive number with at most one decimal place") from exc
    if normalized <= 0:
        raise ValueError("roas_bid must be greater than zero")
    return float(normalized)


class PageInfo(BaseModel):
    """Common pagination block returned by GMV Max endpoints."""

    page: Optional[int] = None
    page_size: Optional[int] = None
    total_number: Optional[int] = None
    total_page: Optional[int] = None
    cursor: Optional[str] = None
    has_more: Optional[bool] = None
    has_next: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaign(BaseModel):
    """Minimal view of a GMV Max campaign entry."""

    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    advertiser_id: Optional[str] = None
    operation_status: Optional[str] = None
    secondary_status: Optional[str] = None
    objective_type: Optional[str] = None
    gmv_max_promotion_type: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    create_time: Optional[str] = None
    modify_time: Optional[str] = None
    is_deleted: Optional[bool] = None
    deleted_at: Optional[datetime | str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignListData(BaseModel):
    """Envelope describing the payload of :func:`gmv_max_campaign_get`."""

    list: List[GMVMaxCampaign] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None
    links: Optional[Dict[str, Any]] = None
    stores: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="allow")


class CampaignStatusEntry(BaseModel):
    """Individual campaign status returned by /campaign/status/update/."""

    campaign_id: Optional[str] = None
    status: Optional[str] = None
    postback_window_mode: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class CampaignStatusUpdateData(BaseModel):
    """Response payload returned by /campaign/status/update/."""

    campaign_ids: Optional[List[str]] = None
    status: Optional[str] = None
    campaign_list: Optional[List[CampaignStatusEntry]] = None

    model_config = ConfigDict(extra="allow")


class PromotionDaysSetting(BaseModel):
    """Promotion days configuration summary."""

    is_enabled: Optional[bool] = None
    auto_schedule_enabled: Optional[bool] = None
    custom_schedule_list: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        max_length=50,
    )
    budget_increase_percentage: Optional[int] = Field(
        default=None,
        ge=50,
        le=300,
    )
    increase_limit: Optional[int] = Field(default=None, ge=1, le=10)

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentityInfo(BaseModel):
    """Identity descriptor returned by GMV Max endpoints."""

    identity_id: Optional[str] = None
    identity_type: Optional[str] = None
    identity_authorized_bc_id: Optional[str] = None
    identity_authorized_shop_id: Optional[str] = None
    store_id: Optional[str] = None
    user_name: Optional[str] = None
    profile_image: Optional[str] = None
    product_gmv_max_available: Optional[bool] = None
    live_gmv_max_available: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxVideoInfo(BaseModel):
    """Video metadata used in video/custom anchor listings."""

    video_id: Optional[str] = None
    preview_url: Optional[str] = None
    video_cover_url: Optional[str] = None
    duration: Optional[float] = None
    size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignInfoData(BaseModel):
    """Detailed campaign information returned by ``campaign/gmv_max/info``."""

    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    advertiser_id: Optional[str] = None
    store_id: Optional[str] = None
    store_authorized_bc_id: Optional[str] = None
    shopping_ads_type: Optional[str] = None
    optimization_goal: Optional[str] = None
    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    promotion_days: Optional[PromotionDaysSetting] = None
    auto_budget_enabled: Optional[bool] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    identity_list: Optional[List[GMVMaxIdentityInfo]] = None
    custom_anchor_video_list: Optional[List[Dict[str, Any]]] = None
    is_deleted: Optional[bool] = None
    deleted_at: Optional[datetime | str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionSettings(BaseModel):
    """Core session level settings for creative boost / max delivery."""

    bid_type: Optional[str] = None
    product_list: Optional[List[Dict[str, Any]]] = None
    item_id: Optional[str] = None
    budget: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionProduct(BaseModel):
    """Product level session settings."""

    spu_id: Optional[str] = None
    item_id: Optional[str] = None
    budget: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSession(BaseModel):
    """Session summary returned by list/get endpoints."""

    session_id: Optional[str] = None
    campaign_id: Optional[str] = None
    store_id: Optional[str] = None
    bid_type: Optional[str] = None
    status: Optional[str] = None
    session: Optional[GMVMaxSessionSettings] = None
    product_list: Optional[List[GMVMaxSessionProduct]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionListData(BaseModel):
    """Payload returned by ``campaign/gmv_max/session/list``."""

    session_list: List[GMVMaxSession] = Field(default_factory=list)
    list: List[GMVMaxSession] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_session_list(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        official_sessions = payload.get("session_list")
        legacy_sessions = payload.get("list")
        if official_sessions is None and legacy_sessions is not None:
            payload["session_list"] = legacy_sessions
        elif legacy_sessions is None and official_sessions is not None:
            payload["list"] = official_sessions
        return payload

    model_config = ConfigDict(extra="allow")


def gmv_max_session_entries(data: Any) -> list[GMVMaxSession]:
    """Read official ``session_list`` while remaining compatible with old payloads."""

    if data is None:
        return []
    values = getattr(data, "session_list", None)
    if values is None and isinstance(data, Mapping):
        values = data.get("session_list")
    if values is None:
        values = getattr(data, "list", None)
    if values is None and isinstance(data, Mapping):
        values = data.get("list")
    return [GMVMaxSession.model_validate(item) for item in (values or [])]


class GMVMaxSessionMutationData(BaseModel):
    """Payload returned by create/update/delete session endpoints."""

    session_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentity(BaseModel):
    """Identity result returned by ``gmv_max/identity/get``."""

    identity_info: Optional[GMVMaxIdentityInfo] = None
    identity_authorized_bc_id: Optional[str] = None
    identity_authorized_bc_name: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentityListData(BaseModel):
    """Payload for identity listing."""

    identity_list: List[GMVMaxIdentity] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class GMVMaxStore(BaseModel):
    """Store entry returned by ``gmv_max/store/list``."""

    store_id: Optional[str] = None
    store_name: Optional[str] = None
    store_region: Optional[str] = None
    store_authorized_bc_id: Optional[str] = None
    advertiser_id: Optional[str] = None
    advertiser_name: Optional[str] = None
    is_gmv_max_available: Optional[bool] = None
    gmv_max_authorization_status: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxStoreListData(BaseModel):
    """Payload for ``gmv_max/store/list``."""

    store_list: List[GMVMaxStore] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")

# 官方确认支持的基础指标集合
GMVMAX_BASE_METRICS = [
    "cost",
    "net_cost",
    "orders",
    "cost_per_order",
    "gross_revenue",
    "roi",
]

GMVMAX_CAMPAIGN_ATTRIBUTE_METRICS = [
    "campaign_id",
    "operation_status",
    "campaign_name",
    "schedule_type",
    "schedule_start_time",
    "schedule_end_time",
    "target_roi_budget",
    "bid_type",
    "max_delivery_budget",
    "roas_bid",
]

GMVMAX_PERFORMANCE_METRICS = [
    "product_impressions",
    "product_clicks",
    "product_click_rate",
    "ad_click_rate",
    "ad_conversion_rate",
    "ad_video_view_rate_2s",
    "ad_video_view_rate_6s",
    "ad_video_view_rate_p25",
    "ad_video_view_rate_p50",
    "ad_video_view_rate_p75",
    "ad_video_view_rate_p100",
]

# 注意：campaign 级使用官方允许的属性+基础指标集合
GMVMAX_CAMPAIGN_METRICS = list(
    dict.fromkeys(list(GMVMAX_CAMPAIGN_ATTRIBUTE_METRICS) + list(GMVMAX_BASE_METRICS))
)

# product 级别按照官方示例仅请求收益/花费等基础指标
GMVMAX_PRODUCT_METRICS = [
    "cost",
    "orders",
    "cost_per_order",
    "gross_revenue",
    "roi",
]

# creative 级别带上状态 + 曝光/点击/完播率指标
GMVMAX_CREATIVE_METRICS = list(
    dict.fromkeys(
        [
            "creative_delivery_status",
            "cost",
            "orders",
            "cost_per_order",
            "gross_revenue",
            "roi",
        ]
        + list(GMVMAX_PERFORMANCE_METRICS)
    )
)

GMVMAX_DIMENSIONS_BY_LEVEL = {
    "campaign": ["campaign_id", "stat_time_day"],
    "product": ["item_group_id", "stat_time_day"],
    "creative": ["campaign_id", "item_group_id", "item_id", "stat_time_day"],
}

GMVMAX_METRICS_BY_LEVEL = {
    "campaign": GMVMAX_CAMPAIGN_METRICS,
    "product": GMVMAX_PRODUCT_METRICS,
    "creative": GMVMAX_CREATIVE_METRICS,
}


class GMVMaxMetricsLevel(str, Enum):
    """Supported GMV Max report levels."""

    CAMPAIGN = "campaign"
    PRODUCT = "product"
    CREATIVE = "creative"


class GMVMaxStoreAdUsageCheckData(BaseModel):
    """Result of ``gmv_max/store/shop_ad_usage_check``."""

    store_id: Optional[str] = None
    promote_all_products_allowed: Optional[bool] = None
    is_running_custom_shop_ads: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxOccupiedAd(BaseModel):
    """Represents an occupied ad entity returned by occupancy checks."""

    advertiser_id: Optional[str] = None
    campaign_id: Optional[str] = None
    adgroup_id: Optional[str] = None
    ad_id: Optional[str] = None
    create_time: Optional[str] = None
    # Local reconciliation context. The official occupancy response does not
    # echo the queried asset, so the batching helper attaches it explicitly.
    asset_id: Optional[str] = None
    item_group_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxOccupiedListData(BaseModel):
    """Payload for ``gmv_max/occupied_custom_shop_ads/list``."""

    occupied_custom_shop_ads: List[GMVMaxOccupiedAd] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class GMVMaxVideo(BaseModel):
    """Entry returned by ``gmv_max/video/get``."""

    item_id: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None
    spu_id_list: Optional[List[str]] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None
    video_info: Optional[GMVMaxVideoInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxVideoListData(BaseModel):
    """Payload for video listing endpoints."""

    video_list: List[GMVMaxVideo] = Field(default_factory=list)
    item_list: List[GMVMaxVideo] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


class TikTokAdVideoUploadRequest(BaseModel):
    """Request for ``file/video/ad/upload`` using a publicly reachable URL."""

    advertiser_id: str
    file_name: Optional[str] = None
    video_url: str
    upload_type: str = "UPLOAD_BY_URL"
    flaw_detect: Optional[bool] = None
    auto_fix_enabled: Optional[bool] = None
    auto_bind_enabled: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class TikTokAdVideoMaterial(BaseModel):
    """Video material returned by TikTok Ads Manager video APIs."""

    video_id: Optional[str] = None
    material_id: Optional[str] = None
    file_name: Optional[str] = None
    preview_url: Optional[str] = None
    video_cover_url: Optional[str] = None
    cover_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    format: Optional[str] = None
    size: Optional[int] = None
    create_time: Optional[str] = None
    modify_time: Optional[str] = None
    source: Optional[str] = None
    material_source: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class TikTokAdVideoUploadData(BaseModel):
    """Response data for ``file/video/ad/upload``.

    The official endpoint may return ``data`` as a one-element array, so the
    client normalizes that array into ``list`` for typed downstream handling.
    """

    list: List[TikTokAdVideoMaterial] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class TikTokAdVideoSearchRequest(BaseModel):
    advertiser_id: str
    filtering: Optional[Dict[str, Any]] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class TikTokAdVideoSearchData(BaseModel):
    list: List[TikTokAdVideoMaterial] = Field(default_factory=list)
    # ``list`` is now a class-local FieldInfo, so it cannot be reused as the
    # default factory for the following field.
    videos: List[TikTokAdVideoMaterial] = Field(default_factory=lambda: [])
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


class TikTokAccountVideoPublishRequest(BaseModel):
    """Publish a video post through an authorized TikTok account token."""

    business_id: str
    video_url: str
    custom_thumbnail_url: Optional[str] = None
    post_info: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class TikTokAccountVideoPublishData(BaseModel):
    share_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class TikTokAccountPublishStatusRequest(BaseModel):
    business_id: str
    publish_id: str


class TikTokAccountPublishStatusData(BaseModel):
    status: Optional[str] = None
    post_ids: List[str] = Field(default_factory=list)
    reason: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxShopCustomAnchorCreateRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str
    custom_anchor_video_list: List[Dict[str, Any]]

    model_config = ConfigDict(extra="allow")


class GMVMaxShopCustomAnchorCreateData(BaseModel):
    failure_list: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class GMVMaxCreationCustomAnchorVideoListGetRequest(BaseModel):
    """Official request for listing customized TikTok posts for a shop."""

    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str
    creative_source: Literal["CUSTOMIZED"] = "CUSTOMIZED"
    spu_id_list: Optional[List[str]] = Field(default=None, max_length=50)
    identity_list: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        max_length=20,
    )
    need_auth_code_video: Optional[bool] = None
    keyword: Optional[str] = None
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None
    campaign_id: Optional[str] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=50)

    model_config = ConfigDict(extra="forbid")


class GMVMaxCreationCustomAnchorVideoListGetData(GMVMaxVideoListData):
    """Official ``item_list`` plus ``page_info`` response payload."""


class GMVMaxExclusiveAuthorizationData(BaseModel):
    """Authorization state for a TikTok Shop store."""

    store_id: Optional[str] = None
    store_authorized_bc_id: Optional[str] = None
    authorization_status: Optional[str] = None
    is_authorized: Optional[bool] = None
    authorized_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxBidRecommendation(BaseModel):
    """Recommended bid target and budget."""

    roas_bid: Optional[float] = None
    budget: Optional[float] = None
    recommendation: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxReportEntry(BaseModel):
    """Single row returned from the GMV Max report endpoint."""

    metrics: Optional[Dict[str, Any]] = None
    dimensions: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxReportData(BaseModel):
    """Report payload including data rows and pagination info."""

    list: List[GMVMaxReportEntry] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None
    total_metrics: Optional[Dict[str, Any]] = None
    summary: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_total_metrics(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        if payload.get("total_metrics") is None and payload.get("summary") is not None:
            payload["total_metrics"] = payload["summary"]
        elif payload.get("summary") is None and payload.get("total_metrics") is not None:
            payload["summary"] = payload["total_metrics"]
        return payload

    model_config = ConfigDict(extra="allow")


# ------------------------- Request models -------------------------


class GMVMaxCampaignFiltering(BaseModel):
    """Filtering block accepted by campaign list/report endpoints."""

    gmv_max_promotion_types: List[str]
    store_ids: Optional[List[str]] = Field(default=None, max_length=10)
    campaign_ids: Optional[List[str]] = Field(default=None, max_length=100)
    campaign_name: Optional[str] = None
    primary_status: Optional[str] = None
    creation_filter_start_time: Optional[str] = None
    creation_filter_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignGetRequest(BaseModel):
    advertiser_id: str
    filtering: GMVMaxCampaignFiltering
    fields: Optional[List[str]] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=100)

    model_config = ConfigDict(extra="forbid")


class GMVMaxCampaignInfoRequest(BaseModel):
    advertiser_id: str
    campaign_id: str


class GMVMaxCampaignCreateBody(BaseModel):
    request_id: Optional[str] = None
    store_id: str
    store_authorized_bc_id: Optional[str] = None
    shopping_ads_type: str
    optimization_goal: str
    deep_bid_type: Optional[str] = None
    campaign_name: str
    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    promotion_days: Optional[PromotionDaysSetting] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    product_specific_type: Optional[str] = None
    # Official /campaign/gmv_max/create/ contract: at most 50 product SPUs.
    item_group_ids: Optional[List[str]] = Field(default=None, max_length=50)
    # Official Product GMV Max create contract allows 0-20 identities.
    identity_list: Optional[List[GMVMaxIdentityInfo]] = Field(
        default=None,
        max_length=20,
    )
    product_video_specific_type: Optional[str] = None
    affiliate_posts_enabled: Optional[bool] = None
    custom_anchor_video_list: Optional[List[Dict[str, Any]]] = None
    item_list: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=50)

    _normalize_roas = field_validator("roas_bid", mode="before")(_normalize_tiktok_roas_bid)

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignCreateWireBody(GMVMaxCampaignCreateBody):
    """Official create payload after local defaults/authorization are resolved."""

    request_id: str
    store_authorized_bc_id: str
    shopping_ads_type: Literal["PRODUCT", "LIVE"]
    optimization_goal: Literal["VALUE"]
    deep_bid_type: Literal["VO_MIN_ROAS"]
    budget: float = Field(gt=0)
    roas_bid: float
    schedule_type: Literal["SCHEDULE_FROM_NOW", "SCHEDULE_START_END"]
    schedule_start_time: str

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized.isdigit() or int(normalized) > 2**63 - 1:
            raise ValueError("request_id must be a string representation of a 64-bit integer")
        return normalized

    @model_validator(mode="after")
    def _validate_official_conditionals(self) -> "GMVMaxCampaignCreateWireBody":
        if self.schedule_type == "SCHEDULE_START_END":
            if not str(self.schedule_end_time or "").strip():
                raise ValueError("schedule_end_time is required for SCHEDULE_START_END")
        elif self.schedule_end_time is not None:
            raise ValueError("schedule_end_time is invalid for SCHEDULE_FROM_NOW")

        if self.shopping_ads_type == "PRODUCT":
            if self.product_video_specific_type not in {
                "AUTO_SELECTION",
                "CUSTOM_SELECTION",
            }:
                raise ValueError("product_video_specific_type is required for PRODUCT")
            if (
                self.product_specific_type == "CUSTOMIZED_PRODUCTS"
                and not self.item_group_ids
            ):
                raise ValueError(
                    "item_group_ids is required for CUSTOMIZED_PRODUCTS"
                )
            if self.product_video_specific_type == "CUSTOM_SELECTION" and not self.item_list:
                raise ValueError("item_list is required for CUSTOM_SELECTION")
        else:
            if not self.identity_list or len(self.identity_list) != 1:
                raise ValueError("LIVE campaigns require exactly one identity")
            if self.product_video_specific_type is not None:
                raise ValueError("product_video_specific_type is invalid for LIVE")
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxCampaignCreateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignCreateBody


class GMVMaxCampaignUpdateBody(BaseModel):
    campaign_id: str
    campaign_name: Optional[str] = None
    budget: Optional[float] = Field(default=None, gt=0)
    roas_bid: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_end_time: Optional[str] = None
    promotion_days: Optional[PromotionDaysSetting] = None
    auto_budget: Optional[Dict[str, Any]] = None
    auto_budget_enabled: Optional[bool] = None
    item_group_ids: Optional[List[str]] = Field(default=None, max_length=50)
    affiliate_posts_enabled: Optional[bool] = None
    item_list: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=50)
    custom_anchor_video_list: Optional[List[Dict[str, Any]]] = None

    _normalize_roas = field_validator("roas_bid", mode="before")(_normalize_tiktok_roas_bid)

    @model_validator(mode="after")
    def _validate_update_fields(self) -> "GMVMaxCampaignUpdateBody":
        payload = self.model_dump(exclude_none=True)
        if set(payload) == {"campaign_id"}:
            raise ValueError("at least one campaign update field is required")
        if (
            self.schedule_type == "SCHEDULE_START_END"
            and not str(self.schedule_end_time or "").strip()
        ):
            raise ValueError("schedule_end_time is required for SCHEDULE_START_END")
        if self.schedule_type == "SCHEDULE_FROM_NOW" and self.schedule_end_time is not None:
            raise ValueError("schedule_end_time is invalid for SCHEDULE_FROM_NOW")
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxCampaignUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignUpdateBody

    model_config = ConfigDict(extra="forbid")


class CampaignStatusUpdateRequest(BaseModel):
    """Request payload for /campaign/status/update/."""

    advertiser_id: str
    campaign_ids: List[str]
    operation_status: str
    postback_window_mode: Optional[str] = None


class GMVMaxCreativeStatusUpdateItem(BaseModel):
    item_id: str
    spu_id_list: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCreativeStatusUpdateBody(BaseModel):
    """Body for TikTok's GMV Max creative remove/add-back endpoint."""

    campaign_id: str
    # Official endpoint accepts 400 posts per request (10,000 per campaign).
    item_list: List[GMVMaxCreativeStatusUpdateItem] = Field(
        default_factory=list,
        min_length=1,
        max_length=400,
    )
    action: str

    model_config = ConfigDict(extra="allow")


class GMVMaxCreativeStatusUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCreativeStatusUpdateBody


class GMVMaxCreativeStatusUpdateData(BaseModel):
    model_config = ConfigDict(extra="allow")


class GMVMaxSessionCreateBody(BaseModel):
    campaign_id: str
    store_id: str
    session: GMVMaxSessionSettings

    @model_validator(mode="after")
    def _validate_create_session(self) -> "GMVMaxSessionCreateBody":
        if not str(self.session.bid_type or "").strip():
            raise ValueError("session.bid_type is required")
        if not self.session.product_list:
            raise ValueError("session.product_list is required")
        if self.session.budget is None or float(self.session.budget) <= 0:
            raise ValueError("session.budget must be greater than zero")
        if (
            self.session.schedule_type == "SCHEDULE_START_END"
            and not str(self.session.schedule_end_time or "").strip()
        ):
            raise ValueError("session.schedule_end_time is required for SCHEDULE_START_END")
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxSessionCreateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxSessionCreateBody

    model_config = ConfigDict(extra="forbid")


class GMVMaxSessionUpdateBody(BaseModel):
    campaign_id: str
    session_id: str
    store_id: str
    session: GMVMaxSessionSettings

    @model_validator(mode="after")
    def _validate_update_session(self) -> "GMVMaxSessionUpdateBody":
        payload = self.session.model_dump(exclude_none=True)
        if not payload:
            raise ValueError("session must contain at least one update")
        if (
            self.session.schedule_type == "SCHEDULE_START_END"
            and not str(self.session.schedule_end_time or "").strip()
        ):
            raise ValueError("session.schedule_end_time is required for SCHEDULE_START_END")
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxSessionUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxSessionUpdateBody

    model_config = ConfigDict(extra="forbid")


class GMVMaxSessionListRequest(BaseModel):
    advertiser_id: str
    campaign_id: str

    model_config = ConfigDict(extra="forbid")


class GMVMaxIdentityGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str


class GMVMaxStoreListRequest(BaseModel):
    advertiser_id: str

    model_config = ConfigDict(extra="forbid")


class GMVMaxStoreAdUsageCheckRequest(BaseModel):
    advertiser_id: str
    store_id: str

    model_config = ConfigDict(extra="forbid")


class GMVMaxOccupiedCustomShopAdsListRequest(BaseModel):
    advertiser_id: str
    store_id: str
    occupied_asset_type: Literal[
        "IDENTITY_TT_USER",
        "IDENTITY_BC_AUTH_TT",
        "IDENTITY_TTS_TT",
        "SPU",
    ]
    asset_ids: List[str] = Field(min_length=1, max_length=1)

    model_config = ConfigDict(extra="forbid")


class GMVMaxVideoGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str
    spu_id_list: Optional[List[str]] = Field(default=None, max_length=50)
    identity_list: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        max_length=20,
    )
    need_auth_code_video: Optional[bool] = None
    custom_posts_eligible: Optional[bool] = None
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None
    keyword: Optional[str] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def _validate_custom_post_spu_limit(self) -> "GMVMaxVideoGetRequest":
        if self.custom_posts_eligible is True and len(self.spu_id_list or []) > 1:
            raise ValueError(
                "spu_id_list supports at most one ID when custom_posts_eligible is true"
            )
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxExclusiveAuthorizationGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str


class GMVMaxExclusiveAuthorizationCreateRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str


class GMVMaxBidRecommendRequest(BaseModel):
    advertiser_id: str
    store_id: str
    shopping_ads_type: Literal["PRODUCT", "LIVE"]
    optimization_goal: Literal["VALUE"]
    item_group_ids: Optional[Sequence[str]] = Field(default=None, max_length=50)
    identity_id: Optional[str] = None

    @model_validator(mode="after")
    def _validate_scope(self) -> "GMVMaxBidRecommendRequest":
        if self.shopping_ads_type == "LIVE":
            if not str(self.identity_id or "").strip():
                raise ValueError("identity_id is required when shopping_ads_type is LIVE")
            if self.item_group_ids:
                raise ValueError("item_group_ids is only valid for PRODUCT")
        elif self.identity_id is not None:
            raise ValueError("identity_id is only valid for LIVE")
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxReportFiltering(BaseModel):
    """Filtering block for GMV Max reports (campaign/product/creative/room/session)."""

    gmv_max_promotion_types: Optional[List[str]] = None
    store_ids: Optional[List[str]] = Field(default=None, max_length=1)
    campaign_ids: Optional[List[str]] = Field(default=None, max_length=100)
    item_group_ids: Optional[List[str]] = Field(default=None, max_length=100)
    creative_types: Optional[List[str]] = None
    creative_delivery_statuses: Optional[List[str]] = None
    room_ids: Optional[List[str]] = Field(default=None, max_length=100)
    search_word: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class GMVMaxReportTimeRange(BaseModel):
    """Time range payload accepted by GMV Max report endpoints."""

    start_time: str
    end_time: str


class GMVMaxReportGetRequest(BaseModel):
    """Request model for TikTok GET /gmv_max/report/get/."""

    advertiser_id: str
    store_ids: Sequence[str] = Field(min_length=1, max_length=1)
    start_date: str
    end_date: str
    metrics: Sequence[str] = Field(min_length=1)
    dimensions: Sequence[str] = Field(min_length=1)
    gmv_max_promotion_types: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)
    campaign_name: Optional[str] = None
    campaign_statuses: Optional[Sequence[str]] = None
    item_group_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)
    creative_types: Optional[Sequence[str]] = None
    creative_delivery_statuses: Optional[Sequence[str]] = None
    room_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)
    search_word: Optional[str] = None
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[GMVMaxReportFiltering] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=1000)
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None

    @model_validator(mode="after")
    def _validate_official_date_window(self) -> "GMVMaxReportGetRequest":
        try:
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
        except ValueError as exc:
            raise ValueError("start_date and end_date must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("start_date must not be after end_date")
        inclusive_days = (end - start).days + 1
        dimension_set = {str(value) for value in self.dimensions}
        if "stat_time_hour" in dimension_set and inclusive_days > 1:
            raise ValueError("hourly GMV Max reports support one advertiser day")
        if "stat_time_day" in dimension_set and inclusive_days > 30:
            raise ValueError("daily GMV Max reports support at most 30 days")
        if not dimension_set.intersection({"stat_time_day", "stat_time_hour"}) and inclusive_days > 365:
            raise ValueError("aggregate GMV Max reports support at most 365 days")
        return self

    model_config = ConfigDict(extra="forbid")


class GMVMaxBaseReportRequest(BaseModel):
    """Shared fields across GMV Max report endpoints."""

    advertiser_id: str
    metrics: Sequence[str]
    dimensions: Sequence[str]
    time_range: Optional[GMVMaxReportTimeRange] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    time_granularity: Optional[str] = None
    time_dimension: Optional[str] = None
    filtering: Optional[GMVMaxReportFiltering] = None
    page: Optional[int] = Field(default=None, ge=1)
    page_size: Optional[int] = Field(default=None, ge=1, le=1000)
    cursor: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignReportRequest(GMVMaxBaseReportRequest):
    campaign_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)


class GMVMaxProductReportRequest(GMVMaxBaseReportRequest):
    product_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)


class GMVMaxCreativeReportRequest(GMVMaxBaseReportRequest):
    creative_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)
    product_ids: Optional[Sequence[str]] = None


class GMVMaxRoomReportRequest(GMVMaxBaseReportRequest):
    room_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)
    campaign_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)


class GMVMaxSessionReportRequest(GMVMaxBaseReportRequest):
    session_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = Field(default=None, max_length=100)


# ------------------------- Dataset typing & builder for report -------------------------


class GMVMaxDataset(str, Enum):
    """High-level GMV Max dataset type = promotion type + metric level."""

    # Overview – ad account level, overall (PRODUCT + LIVE)
    OVERVIEW = "overview"

    # Product GMV Max
    PRODUCT_CAMPAIGN = "product_campaign"      # Campaign-level
    PRODUCT_PRODUCT = "product_product"        # Product-level
    CREATIVE = "creative"                      # Creative-level
    PRODUCT_DURATION = "product_duration"      # Duration-level

    # LIVE GMV Max
    LIVE_CAMPAIGN = "live_campaign"            # Campaign-level
    LIVE_LIVESTREAM = "live_livestream"        # Livestream(room)-level
    LIVE_DURATION = "live_duration"            # Duration-level


# 每种 dataset 对应的固定维度和必填过滤字段，根据官方 Metrics 文档整理
_GMV_MAX_DATASET_CONFIG: Dict[GMVMaxDataset, Dict[str, Any]] = {
    # Overview metrics：All GMV Max Campaigns, Ad account-level
    # 示例 curl 中是 ["advertiser_id", "stat_time_day"]
    GMVMaxDataset.OVERVIEW: {
        "promotion_types": None,  # 不限制 PRODUCT/LIVE，一起看整体
        "dimensions": ["advertiser_id", "stat_time_day"],
        "require_all_of": (),
        "require_any_of": (),
    },
    # Product GMV Max, campaign-level
    GMVMaxDataset.PRODUCT_CAMPAIGN: {
        "promotion_types": ["PRODUCT"],
        "dimensions": ["campaign_id", "stat_time_day"],
        "require_all_of": (),  # campaign_ids 可选
        "require_any_of": (),
    },
    # Product GMV Max, product-level（官方：campaign_ids Required）
    GMVMaxDataset.PRODUCT_PRODUCT: {
        # Official product-level examples accept campaign_ids only.
        # gmv_max_promotion_types is a campaign-level report filter and the
        # endpoint rejects it here with code 40002.
        "promotion_types": None,
        "dimensions": ["item_group_id", "stat_time_day"],
        "require_all_of": ("campaign_ids",),
        "require_any_of": (),
    },
    # Product GMV Max, creative-level（campaign_ids + item_group_ids Required）
    GMVMaxDataset.CREATIVE: {
        "promotion_types": None,  # 创意层禁止 gmv_max_promotion_types 过滤
        "dimensions": ["campaign_id", "item_group_id", "item_id", "stat_time_day"],
        "require_all_of": ("campaign_ids", "item_group_ids"),
        "require_any_of": (),
    },
    # Product GMV Max, duration-level（campaign_ids + item_group_ids Required）
    GMVMaxDataset.PRODUCT_DURATION: {
        "promotion_types": None,
        "dimensions": ["duration"],
        "require_all_of": ("campaign_ids", "item_group_ids"),
        "require_any_of": (),
    },
    # LIVE GMV Max, campaign-level
    GMVMaxDataset.LIVE_CAMPAIGN: {
        "promotion_types": ["LIVE"],
        "dimensions": ["campaign_id", "stat_time_day"],
        "require_all_of": (),
        "require_any_of": (),
    },
    # LIVE GMV Max, livestream-level（campaign_ids Required）
    GMVMaxDataset.LIVE_LIVESTREAM: {
        "promotion_types": None,
        "dimensions": ["room_id", "stat_time_day"],
        "require_all_of": ("campaign_ids",),
        "require_any_of": (),
    },
    # LIVE GMV Max, duration-level（campaign_ids + room_ids Required）
    GMVMaxDataset.LIVE_DURATION: {
        "promotion_types": None,
        "dimensions": ["duration"],
        "require_all_of": ("campaign_ids", "room_ids"),
        "require_any_of": (),
    },
}


def _sanitize_id_list(values: Optional[Sequence[str] | Sequence[int]]) -> list[str] | None:
    """Normalize an ID list by dropping sentinels like ``"all"`` and blanks."""

    if not values:
        return None

    cleaned: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() == "all":
            continue
        cleaned.append(text)
    return cleaned or None


def build_gmv_max_report_request(
    *,
    dataset: GMVMaxDataset,
    advertiser_id: str,
    store_ids: Sequence[str],
    start_date: str,
    end_date: str,
    metrics: Sequence[str],
    enable_total_metrics: Optional[bool] = None,
    campaign_ids: Optional[Sequence[str]] = None,
    campaign_name: Optional[str] = None,
    campaign_statuses: Optional[Sequence[str]] = None,
    item_group_ids: Optional[Sequence[str]] = None,
    room_ids: Optional[Sequence[str]] = None,
    search_word: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    sort_field: Optional[str] = None,
    sort_type: Optional[str] = None,
) -> GMVMaxReportGetRequest:
    """
    Build a GMVMaxReportGetRequest that respects the official dataset-level
    constraints (promotion type + metric level).

    This will:
    - Fix `dimensions` according to dataset.
    - Add appropriate gmv_max_promotion_types filter (PRODUCT / LIVE).
    - Validate required filters (campaign_ids / item_group_ids / room_ids).
    """
    if not store_ids:
        raise ValueError("store_ids must contain at least one store id")

    if not metrics:
        raise ValueError("metrics must contain at least one metric field")

    campaign_ids = _sanitize_id_list(campaign_ids)
    item_group_ids = _sanitize_id_list(item_group_ids)
    room_ids = _sanitize_id_list(room_ids)

    cfg = _GMV_MAX_DATASET_CONFIG[dataset]
    require_all_of = cfg.get("require_all_of") or ()
    require_any_of = cfg.get("require_any_of") or ()

    def _is_empty_sequence(value: Any) -> bool:
        return (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) == 0
        )

    # 校验 require_all_of：字段必须非空
    missing_all: List[str] = []
    for field_name in require_all_of:
        value = locals().get(field_name)
        if value is None or _is_empty_sequence(value):
            missing_all.append(field_name)

    # 校验 require_any_of：这些字段中至少一个非空
    missing_any_group = False
    if require_any_of:
        has_any = False
        for field_name in require_any_of:
            value = locals().get(field_name)
            if value is None or _is_empty_sequence(value):
                continue
            has_any = True
            break
        if not has_any:
            missing_any_group = True

    if missing_all or missing_any_group:
        msg_parts: List[str] = []
        if missing_all:
            msg_parts.append("missing required filters: %s" % ", ".join(missing_all))
        if missing_any_group:
            msg_parts.append(
                "at least one of the following filters must be provided: %s"
                % ", ".join(require_any_of)
            )
        raise ValueError(
            "Invalid GMV Max report request for dataset %r: %s"
            % (dataset.value, "; ".join(msg_parts))
        )

    # 推广类型过滤：PRODUCT / LIVE
    promotion_types = cfg.get("promotion_types")
    if promotion_types:
        promotion_types = list(promotion_types)

    report_filtering = None
    if promotion_types or campaign_ids or item_group_ids or room_ids:
        report_filtering = GMVMaxReportFiltering(
            gmv_max_promotion_types=list(promotion_types)
            if promotion_types
            else None,
            campaign_ids=list(campaign_ids) if campaign_ids else None,
            item_group_ids=list(item_group_ids) if item_group_ids else None,
            room_ids=list(room_ids) if room_ids else None,
        )

    request = GMVMaxReportGetRequest(
        advertiser_id=advertiser_id,
        store_ids=list(store_ids),
        start_date=start_date,
        end_date=end_date,
        metrics=list(metrics),
        dimensions=list(cfg["dimensions"]),
        gmv_max_promotion_types=promotion_types,
        campaign_ids=list(campaign_ids) if campaign_ids else None,
        campaign_name=campaign_name,
        campaign_statuses=list(campaign_statuses) if campaign_statuses else None,
        item_group_ids=list(item_group_ids) if item_group_ids else None,
        room_ids=list(room_ids) if room_ids else None,
        search_word=search_word,
        enable_total_metrics=enable_total_metrics,
        filtering=report_filtering,
        page=page,
        page_size=page_size,
        sort_field=sort_field,
        sort_type=sort_type,
    )
    return request


# ------------------------- Response envelope -------------------------


T = TypeVar("T", bound=BaseModel)


class GMVMaxResponse(BaseModel, Generic[T]):
    """Standard TikTok Business response wrapper."""

    code: int
    message: str
    request_id: Optional[str] = None
    data: T

    model_config = ConfigDict(extra="allow")


def _parse_report_data(payload: Mapping[str, Any]) -> GMVMaxReportData:
    data_block = payload.get("data") if isinstance(payload, Mapping) else None
    report_block: Mapping[str, Any] = {}
    if isinstance(data_block, Mapping):
        report_block = data_block.get("report") or data_block
    return GMVMaxReportData.model_validate(report_block)


def _coerce_store_ids(value: Any) -> List[str]:
    """Normalize store identifiers into a list of non-empty strings."""

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Sequence):
        results: List[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                results.append(text)
        return results
    text = str(value).strip()
    return [text] if text else []


# ------------------------- Client implementation -------------------------


class TikTokBusinessGMVMaxClient(TTBApiClient):
    """High level typed wrappers for TikTok Business GMV Max endpoints."""

    def __init__(
        self,
        *,
        access_token: str,
        app_id: str | None = None,
        app_secret: str | None = None,
        qps: float | None = None,
        timeout: float | None = None,
        headers: Optional[Dict[str, str]] = None,
        http_client: httpx.AsyncClient | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            access_token=access_token,
            app_id=app_id,
            app_secret=app_secret,
            qps=qps,
            timeout=timeout,
            headers=headers,
            **kwargs,
        )
        if http_client is not None:
            self._client = http_client

    def _parse_response(self, payload: Mapping[str, Any], data_type: Type[T]) -> GMVMaxResponse[T]:
        try:
            raw_code = payload.get("code", 0)
            code = int(raw_code) if isinstance(raw_code, (int, float, str)) else 0
        except ValueError:
            code = 0
        message = payload.get("message") or ""
        request_id = payload.get("request_id")
        data_payload = payload.get("data") or {}
        data_model = data_type.model_validate(data_payload)
        return GMVMaxResponse[T](code=code, message=str(message), request_id=request_id, data=data_model)

    def _parse_response_normalized(
        self,
        payload: Mapping[str, Any],
        data_type: Type[T],
        *,
        list_key: str = "list",
    ) -> GMVMaxResponse[T]:
        data_payload = payload.get("data") or {}
        if isinstance(data_payload, list):
            normalized_payload: Mapping[str, Any] = {list_key: data_payload}
        elif isinstance(data_payload, Mapping):
            normalized_payload = data_payload
        else:
            normalized_payload = {}
        normalized = dict(payload)
        normalized["data"] = normalized_payload
        return self._parse_response(normalized, data_type)

    async def gmv_max_campaign_get(
        self, request: GMVMaxCampaignGetRequest
    ) -> GMVMaxResponse[GMVMaxCampaignListData]:
        """Wrapper for TikTok GET /gmv_max/campaign/get/ to list campaigns."""
        params = request.model_dump(exclude_none=True)
        filtering_payload = params.get("filtering")
        if isinstance(filtering_payload, dict):
            normalized_store_ids: List[str] = []
            normalized_store_ids.extend(
                _coerce_store_ids(filtering_payload.pop("store_ids", None))
            )
            normalized_store_ids.extend(
                _coerce_store_ids(filtering_payload.pop("store_id", None))
            )
        else:
            filtering_payload = None
            normalized_store_ids = []

        # Campaign/get keeps store_ids inside its sole filtering JSON object.
        normalized_store_ids.extend(_coerce_store_ids(params.pop("store_ids", None)))
        if normalized_store_ids:
            unique_ids = list(dict.fromkeys(normalized_store_ids))
            if filtering_payload is None:
                filtering_payload = {}
                params["filtering"] = filtering_payload
            filtering_payload["store_ids"] = unique_ids

        if isinstance(params.get("fields"), list):
            params["fields"] = json.dumps(
                [str(field) for field in params["fields"]],
                ensure_ascii=False,
            )

        _ttb_api._ensure_gmvmax_campaign_filters(params, promotion_type_format="campaign")
        cleaned = _ttb_api._clean_params_map(params)
        payload = await self._request_json("GET", "/gmv_max/campaign/get/", params=cleaned)
        return self._parse_response(payload, GMVMaxCampaignListData)

    async def gmv_max_campaign_info(
        self, request: GMVMaxCampaignInfoRequest
    ) -> GMVMaxResponse[GMVMaxCampaignInfoData]:
        """Wrapper for TikTok GET /campaign/gmv_max/info/ to fetch campaign detail."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json("GET", "/campaign/gmv_max/info/", params=params)
        return self._parse_response(payload, GMVMaxCampaignInfoData)

    async def gmv_max_campaign_create(
        self, request: GMVMaxCampaignCreateRequest
    ) -> GMVMaxResponse[GMVMaxCampaignInfoData]:
        """Wrapper for TikTok POST /campaign/gmv_max/create/ to create campaigns."""
        params = {"advertiser_id": request.advertiser_id}
        wire_body = GMVMaxCampaignCreateWireBody.model_validate(
            request.body.model_dump(exclude_none=True)
        )
        body = wire_body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        # Campaign creation is non-idempotent outside TikTok's short
        # request_id window. Never inherit the generic transport/5xx retry
        # policy here: an ambiguous result must be reconciled with read APIs.
        payload = await self._request_json_once(
            "POST",
            "/campaign/gmv_max/create/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCampaignInfoData)

    async def gmv_max_campaign_update(
        self, request: GMVMaxCampaignUpdateRequest
    ) -> GMVMaxResponse[GMVMaxCampaignInfoData]:
        """Wrapper for TikTok POST /campaign/gmv_max/update/ to mutate campaigns."""
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/update/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCampaignInfoData)

    async def campaign_status_update(
        self, request: CampaignStatusUpdateRequest
    ) -> GMVMaxResponse[CampaignStatusUpdateData]:
        """Wrapper for TikTok POST /campaign/status/update/ to change status."""
        params = {"advertiser_id": request.advertiser_id}
        body = {
            "advertiser_id": request.advertiser_id,
            "campaign_ids": [str(c) for c in request.campaign_ids if str(c).strip()],
            "operation_status": str(request.operation_status),
            "postback_window_mode": request.postback_window_mode,
        }
        payload = await self._request_json(
            "POST",
            "/campaign/status/update/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, CampaignStatusUpdateData)

    async def gmv_max_creative_status_update(
        self, request: GMVMaxCreativeStatusUpdateRequest
    ) -> GMVMaxResponse[GMVMaxCreativeStatusUpdateData]:
        """Wrapper for TikTok's GMV Max creative remove/add-back endpoint.

        The official docs page is "Remove or add back GMV Max creatives".
        Keep the path centralized and configurable because TikTok has been
        rolling GMV Max docs faster than their public SDK generation.
        """
        endpoint = os.getenv(
            "TTB_GMVMAX_CREATIVE_STATUS_UPDATE_PATH",
            "/campaign/gmv_max/creative/update/",
        )
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            endpoint,
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCreativeStatusUpdateData)

    async def gmv_max_session_create(
        self, request: GMVMaxSessionCreateRequest
    ) -> GMVMaxResponse[GMVMaxSessionMutationData]:
        """Wrapper for TikTok POST /campaign/gmv_max/session/create/."""
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/create/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxSessionMutationData)

    async def gmv_max_session_update(
        self, request: GMVMaxSessionUpdateRequest
    ) -> GMVMaxResponse[GMVMaxSessionMutationData]:
        """Wrapper for TikTok POST /campaign/gmv_max/session/update/."""
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/update/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxSessionMutationData)

    async def gmv_max_session_delete(
        self, *, advertiser_id: str, session_id: str
    ) -> GMVMaxResponse[GMVMaxSessionMutationData]:
        """Wrapper for TikTok POST /campaign/gmv_max/session/delete/."""
        body = {
            "advertiser_id": str(advertiser_id),
            "session_id": str(session_id),
        }
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/delete/",
            params=_ttb_api._clean_params_map({"advertiser_id": str(advertiser_id)}),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxSessionMutationData)

    async def gmv_max_session_list(
        self, request: GMVMaxSessionListRequest
    ) -> GMVMaxResponse[GMVMaxSessionListData]:
        """Wrapper for TikTok GET /campaign/gmv_max/session/list/."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/campaign/gmv_max/session/list/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxSessionListData)

    async def gmv_max_identity_get(
        self, request: GMVMaxIdentityGetRequest
    ) -> GMVMaxResponse[GMVMaxIdentityListData]:
        """Wrapper for TikTok GET /gmv_max/identity/get/ to list eligible identities."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/identity/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxIdentityListData)

    async def gmv_max_store_list(
        self, request: GMVMaxStoreListRequest
    ) -> GMVMaxResponse[GMVMaxStoreListData]:
        """Wrapper for TikTok GET /gmv_max/store/list/ to enumerate stores."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/store/list/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxStoreListData)

    async def gmv_max_store_shop_ad_usage_check(
        self, request: GMVMaxStoreAdUsageCheckRequest
    ) -> GMVMaxResponse[GMVMaxStoreAdUsageCheckData]:
        """Wrapper for TikTok GET /gmv_max/store/shop_ad_usage_check/ to verify eligibility."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/store/shop_ad_usage_check/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxStoreAdUsageCheckData)

    async def gmv_max_occupied_custom_shop_ads_list(
        self, request: GMVMaxOccupiedCustomShopAdsListRequest
    ) -> GMVMaxResponse[GMVMaxOccupiedListData]:
        """Wrapper for the one-asset official occupancy endpoint."""

        params = request.model_dump(exclude_none=True)
        params["asset_ids"] = json.dumps(
            [str(request.asset_ids[0])],
            ensure_ascii=False,
        )
        payload = await self._request_json(
            "GET",
            "/gmv_max/occupied_custom_shop_ads/list/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxOccupiedListData)

    async def gmv_max_video_get(
        self, request: GMVMaxVideoGetRequest
    ) -> GMVMaxResponse[GMVMaxVideoListData]:
        """Wrapper for TikTok GET /gmv_max/video/get/."""
        params = request.model_dump(exclude_none=True)
        if isinstance(params.get("spu_id_list"), list):
            params["spu_id_list"] = json.dumps([str(item) for item in params["spu_id_list"]], ensure_ascii=False)
        if isinstance(params.get("identity_list"), list):
            params["identity_list"] = json.dumps(params["identity_list"], ensure_ascii=False)
        payload = await self._request_json(
            "GET",
            "/gmv_max/video/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxVideoListData)

    async def file_video_ad_upload_by_url(
        self, request: TikTokAdVideoUploadRequest
    ) -> GMVMaxResponse[TikTokAdVideoUploadData]:
        """Wrapper for TikTok POST /file/video/ad/upload/ with UPLOAD_BY_URL.

        This uploads a video to the ad account's video material library and
        returns a ``video_id``. Product GMV Max still requires a TikTok post
        ``item_id`` from GMV Max video/customized-post endpoints before the
        video can be manually selected in ``/campaign/gmv_max/create/``.
        """
        body = request.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        body["upload_type"] = request.upload_type or "UPLOAD_BY_URL"
        payload = await self._request_json(
            "POST",
            "/file/video/ad/upload/",
            params=_ttb_api._clean_params_map({"advertiser_id": request.advertiser_id}),
            multipart_body=_ttb_api._remove_none(body),
        )
        return self._parse_response_normalized(payload, TikTokAdVideoUploadData)

    async def business_video_publish(
        self, request: TikTokAccountVideoPublishRequest
    ) -> GMVMaxResponse[TikTokAccountVideoPublishData]:
        """Publish an owned-account TikTok post via ``/business/video/publish/``."""

        body = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "POST",
            "/business/video/publish/",
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, TikTokAccountVideoPublishData)

    async def business_publish_status(
        self, request: TikTokAccountPublishStatusRequest
    ) -> GMVMaxResponse[TikTokAccountPublishStatusData]:
        """Poll an owned-account publish task via ``/business/publish/status/``."""

        payload = await self._request_json(
            "GET",
            "/business/publish/status/",
            params=_ttb_api._clean_params_map(request.model_dump(exclude_none=True)),
        )
        return self._parse_response(payload, TikTokAccountPublishStatusData)

    async def file_video_ad_search(
        self, request: TikTokAdVideoSearchRequest
    ) -> GMVMaxResponse[TikTokAdVideoSearchData]:
        """Wrapper for TikTok GET /file/video/ad/search/."""
        params = request.model_dump(exclude_none=True)
        filtering = params.get("filtering")
        if isinstance(filtering, Mapping):
            params["filtering"] = json.dumps(filtering, ensure_ascii=False)
        for key in ("video_ids", "material_ids", "video_material_sources"):
            if isinstance(params.get(key), list):
                params[key] = json.dumps(
                    [str(item) for item in params[key]],
                    ensure_ascii=False,
                )
        payload = await self._request_json(
            "GET",
            "/file/video/ad/search/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, TikTokAdVideoSearchData)

    async def gmv_max_shop_custom_anchor_create(
        self, request: GMVMaxShopCustomAnchorCreateRequest
    ) -> GMVMaxResponse[GMVMaxShopCustomAnchorCreateData]:
        """Wrapper for POST /gmv_max/creation/custom_anchor_video_list/create/."""
        body = request.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/gmv_max/creation/custom_anchor_video_list/create/",
            params=_ttb_api._clean_params_map({"advertiser_id": request.advertiser_id}),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxShopCustomAnchorCreateData)

    async def gmv_max_creation_custom_anchor_video_list_get(
        self, request: GMVMaxCreationCustomAnchorVideoListGetRequest
    ) -> GMVMaxResponse[GMVMaxCreationCustomAnchorVideoListGetData]:
        """Wrapper for POST /gmv_max/creation/custom_anchor_video_list/get/."""
        body = request.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/gmv_max/creation/custom_anchor_video_list/get/",
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(
            payload,
            GMVMaxCreationCustomAnchorVideoListGetData,
        )

    async def gmv_max_exclusive_authorization_get(
        self, request: GMVMaxExclusiveAuthorizationGetRequest
    ) -> GMVMaxResponse[GMVMaxExclusiveAuthorizationData]:
        """Wrapper for TikTok GET /gmv_max/exclusive_authorization/get/."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/exclusive_authorization/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxExclusiveAuthorizationData)

    async def gmv_max_exclusive_authorization_create(
        self, request: GMVMaxExclusiveAuthorizationCreateRequest
    ) -> GMVMaxResponse[GMVMaxExclusiveAuthorizationData]:
        """Wrapper for TikTok POST /gmv_max/exclusive_authorization/create/."""
        params = {"advertiser_id": request.advertiser_id}
        body = {
            "advertiser_id": request.advertiser_id,
            "store_id": request.store_id,
            "store_authorized_bc_id": request.store_authorized_bc_id,
        }
        payload = await self._request_json(
            "POST",
            "/gmv_max/exclusive_authorization/create/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxExclusiveAuthorizationData)

    async def gmv_max_bid_recommend(
        self, request: GMVMaxBidRecommendRequest
    ) -> GMVMaxResponse[GMVMaxBidRecommendation]:
        """Wrapper for TikTok GET /gmv_max/bid/recommend/ to preview strategy suggestions."""
        params = request.model_dump(exclude_none=True)
        # TikTok v1.3 expects query-array fields as JSON text. Sending repeated
        # query keys is parsed as a scalar and returns code 40002.
        if request.item_group_ids is not None:
            params["item_group_ids"] = json.dumps(
                [str(item) for item in request.item_group_ids],
                ensure_ascii=False,
            )
        payload = await self._request_json(
            "GET",
            "/gmv_max/bid/recommend/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxBidRecommendation)

    async def _gmv_max_report_get(
        self, params: Mapping[str, Any]
    ) -> GMVMaxResponse[GMVMaxReportData]:
        payload = await self._request_json(
            "GET",
            "/gmv_max/report/get/",
            params=_ttb_api._clean_params_map(dict(params)),
        )
        report_data = _parse_report_data(payload)
        return GMVMaxResponse[GMVMaxReportData](  # type: ignore[call-arg]
            code=int(payload.get("code", -1)),
            message=str(payload.get("message") or ""),
            request_id=payload.get("request_id"),
            data=report_data,
        )

    def _build_report_get_params(
        self, request: GMVMaxReportGetRequest, *, inject_promotion_types: bool = True
    ) -> Dict[str, Any]:
        store_ids = [str(store) for store in request.store_ids]
        params: Dict[str, Any] = {
            "advertiser_id": request.advertiser_id,
            "store_ids": json.dumps(store_ids, ensure_ascii=False),
            "start_date": request.start_date,
            "end_date": request.end_date,
            "metrics": json.dumps(list(request.metrics), ensure_ascii=False),
            "dimensions": json.dumps(list(request.dimensions), ensure_ascii=False),
        }

        filtering_payload = (
            request.filtering.model_dump(exclude_none=True)
            if request.filtering is not None
            else {}
        )
        filtering_payload.pop("store_ids", None)
        filtering_payload.pop("store_id", None)
        optional_arrays = {
            "gmv_max_promotion_types": request.gmv_max_promotion_types,
            "campaign_ids": request.campaign_ids,
            "campaign_statuses": request.campaign_statuses,
            "item_group_ids": request.item_group_ids,
            "creative_types": request.creative_types,
            "creative_delivery_statuses": request.creative_delivery_statuses,
            "room_ids": request.room_ids,
        }
        for key, values in optional_arrays.items():
            if values is not None:
                filtering_payload[key] = [str(item) for item in values]

        if request.campaign_name is not None:
            filtering_payload["campaign_name"] = request.campaign_name
        if request.search_word is not None:
            filtering_payload["search_word"] = request.search_word
        if request.enable_total_metrics is not None:
            params["enable_total_metrics"] = bool(request.enable_total_metrics)
        if filtering_payload:
            params["filtering"] = filtering_payload
        for key in ("page", "page_size", "sort_field", "sort_type"):
            value = getattr(request, key)
            if value is not None:
                params[key] = value

        if inject_promotion_types:
            _ttb_api._ensure_gmvmax_campaign_filters(
                params, promotion_type_format="report"
            )
        elif isinstance(params.get("filtering"), Mapping):
            params["filtering"] = json.dumps(
                params["filtering"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return params

    async def gmv_max_campaign_report(
        self, request: GMVMaxCampaignReportRequest
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Run a GMV Max campaign report via GET /gmv_max/report/get/."""

        legacy_filtering = request.filtering.model_dump(exclude_none=True) if request.filtering else {}
        if "item_group_ids" not in legacy_filtering and legacy_filtering.get("product_ids"):
            legacy_filtering["item_group_ids"] = legacy_filtering.pop("product_ids")
        store_ids = _coerce_store_ids(legacy_filtering.pop("store_ids", None))
        start_date = None
        end_date = None
        if request.time_range is not None:
            start_date = request.time_range.start_time
            end_date = request.time_range.end_time
        start_date = start_date or request.start_time
        end_date = end_date or request.end_time
        if not start_date or not end_date:
            raise ValueError("start_date and end_date are required for GMV Max report")

        campaign_ids = _sanitize_id_list(
            getattr(request, "campaign_ids", None) or legacy_filtering.get("campaign_ids")
        )
        item_group_ids = _sanitize_id_list(
            getattr(request, "item_group_ids", None)
            or legacy_filtering.get("item_group_ids")
        )
        room_ids = _sanitize_id_list(
            getattr(request, "room_ids", None) or legacy_filtering.get("room_ids")
        )

        filtering_payload = dict(legacy_filtering)
        for key, value in (
            ("campaign_ids", campaign_ids),
            ("item_group_ids", item_group_ids),
            ("room_ids", room_ids),
        ):
            if value is None:
                filtering_payload.pop(key, None)
            else:
                filtering_payload[key] = value

        filtering_model = (
            GMVMaxReportFiltering.model_validate(filtering_payload)
            if filtering_payload
            else None
        )
        get_request = GMVMaxReportGetRequest(
            advertiser_id=request.advertiser_id,
            store_ids=store_ids,
            start_date=start_date,
            end_date=end_date,
            metrics=list(request.metrics),
            dimensions=list(request.dimensions),
            gmv_max_promotion_types=getattr(request, "gmv_max_promotion_types", None),
            campaign_ids=campaign_ids,
            item_group_ids=item_group_ids,
            room_ids=room_ids,
            filtering=filtering_model,
            page=request.page,
            page_size=request.page_size,
        )
        params = self._build_report_get_params(get_request)
        return await self._gmv_max_report_get(params)

    async def gmv_max_product_report(
        self, request: GMVMaxProductReportRequest
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Run a GMV Max product report via GET /gmv_max/report/get/."""

        return await self.gmv_max_campaign_report(request)

    async def gmv_max_creative_report(
        self, request: GMVMaxCreativeReportRequest
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Run a GMV Max creative report via GET /gmv_max/report/get/."""

        legacy_filtering = request.filtering.model_dump(exclude_none=True) if request.filtering else {}
        store_ids = _coerce_store_ids(legacy_filtering.pop("store_ids", None))
        if not store_ids:
            store_ids = _coerce_store_ids(legacy_filtering.pop("store_id", None))

        start_date = None
        end_date = None
        if request.time_range is not None:
            start_date = request.time_range.start_time
            end_date = request.time_range.end_time
        start_date = start_date or request.start_time
        end_date = end_date or request.end_time
        if not start_date or not end_date:
            raise ValueError("start_date and end_date are required for GMV Max creative report")

        campaign_ids = _sanitize_id_list(getattr(request, "campaign_ids", None))
        item_group_ids = _sanitize_id_list(
            getattr(request, "item_group_ids", None)
            or legacy_filtering.pop("item_group_ids", None)
            or legacy_filtering.pop("product_ids", None)
        )

        filtering_model = GMVMaxReportFiltering(
            campaign_ids=campaign_ids,
            item_group_ids=item_group_ids,
            creative_types=legacy_filtering.pop("creative_types", None),
            creative_delivery_statuses=legacy_filtering.pop("creative_delivery_statuses", None),
            search_word=legacy_filtering.pop("search_word", None),
        )

        get_request = GMVMaxReportGetRequest(
            advertiser_id=request.advertiser_id,
            store_ids=store_ids,
            start_date=start_date,
            end_date=end_date,
            metrics=list(
                request.metrics
                or GMVMAX_METRICS_BY_LEVEL[GMVMaxMetricsLevel.CREATIVE.value]
            ),
            dimensions=list(
                request.dimensions
                or GMVMAX_DIMENSIONS_BY_LEVEL[GMVMaxMetricsLevel.CREATIVE.value]
            ),
            gmv_max_promotion_types=filtering_model.gmv_max_promotion_types,
            campaign_ids=filtering_model.campaign_ids,
            item_group_ids=filtering_model.item_group_ids,
            creative_types=filtering_model.creative_types,
            creative_delivery_statuses=filtering_model.creative_delivery_statuses,
            search_word=filtering_model.search_word,
            filtering=filtering_model,
            page=request.page,
            page_size=request.page_size,
        )
        params = self._build_report_get_params(
            get_request, inject_promotion_types=False
        )
        return await self._gmv_max_report_get(params)

    async def gmv_max_room_report(
        self, request: GMVMaxRoomReportRequest
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Run a GMV Max room report via GET /gmv_max/report/get/."""

        return await self.gmv_max_campaign_report(request)

    async def gmv_max_session_report(
        self, request: GMVMaxSessionReportRequest
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Run a GMV Max session report via GET /gmv_max/report/get/."""

        return await self.gmv_max_campaign_report(request)

    async def gmv_max_report_get(
        self, request: GMVMaxReportGetRequest, *, inject_promotion_types: bool = True
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Fetch exactly one report page.

        Pagination belongs to the report synchronization services. Keeping the
        client page-scoped prevents nested pagination (N + N-1 + ... requests)
        when a caller already iterates over ``page_info``.
        """

        params = self._build_report_get_params(
            request, inject_promotion_types=inject_promotion_types
        )
        return await self._gmv_max_report_get(params)

    async def gmv_max_report_get_dataset(
        self,
        *,
        dataset: GMVMaxDataset,
        advertiser_id: str,
        store_ids: Sequence[str],
        start_date: str,
        end_date: str,
        metrics: Sequence[str],
        enable_total_metrics: Optional[bool] = None,
        campaign_ids: Optional[Sequence[str]] = None,
        campaign_name: Optional[str] = None,
        campaign_statuses: Optional[Sequence[str]] = None,
        item_group_ids: Optional[Sequence[str]] = None,
        room_ids: Optional[Sequence[str]] = None,
        search_word: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        sort_field: Optional[str] = None,
        sort_type: Optional[str] = None,
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """
        High-level wrapper around /gmv_max/report/get/ that takes a GMVMaxDataset
        (promotion type + metric level) and automatically builds a valid request.
        """
        request = build_gmv_max_report_request(
            dataset=dataset,
            advertiser_id=advertiser_id,
            store_ids=store_ids,
            start_date=start_date,
            end_date=end_date,
            metrics=metrics,
            enable_total_metrics=enable_total_metrics,
            campaign_ids=campaign_ids,
            campaign_name=campaign_name,
            campaign_statuses=campaign_statuses,
            item_group_ids=item_group_ids,
            room_ids=room_ids,
            search_word=search_word,
            page=page,
            page_size=page_size,
            sort_field=sort_field,
            sort_type=sort_type,
        )
        cfg = _GMV_MAX_DATASET_CONFIG[dataset]
        inject_promotion_types = cfg.get("promotion_types") is not None
        return await self.gmv_max_report_get(
            request, inject_promotion_types=inject_promotion_types
        )

    async def fetch_gmvmax_report(
        self,
        *,
        advertiser_id: str,
        store_id: str,
        campaign_id: str,
        campaign_ids: Sequence[str] | None = None,
        level: GMVMaxMetricsLevel,
        start_date: str,
        end_date: str,
        item_group_ids: Sequence[str] | None = None,
    ) -> GMVMaxResponse[GMVMaxReportData]:
        """Build a GMV Max report request for the given level and execute it."""

        level_value = GMVMaxMetricsLevel(level)
        try:
            metrics = GMVMAX_METRICS_BY_LEVEL[level_value.value]
            dimensions = GMVMAX_DIMENSIONS_BY_LEVEL[level_value.value]
        except KeyError as exc:
            raise ValueError(f"unsupported GMV Max metrics level: {level}") from exc
        campaign_id_list = _sanitize_id_list(campaign_ids) or _sanitize_id_list(
            [campaign_id]
        )
        item_group_list = _sanitize_id_list(item_group_ids)
        report_item_group_list = (
            item_group_list
            if level_value is GMVMaxMetricsLevel.CREATIVE
            else None
        )

        filters = GMVMaxReportFiltering(
            campaign_ids=campaign_id_list,
            item_group_ids=report_item_group_list,
        )

        base_kwargs = dict(
            advertiser_id=advertiser_id,
            store_ids=[store_id],
            campaign_ids=campaign_id_list,
            start_date=start_date,
            end_date=end_date,
            enable_total_metrics=False,
        )

        inject_promotion_types = level_value is GMVMaxMetricsLevel.CAMPAIGN
        if inject_promotion_types:
            base_kwargs["gmv_max_promotion_types"] = ["PRODUCT"]

        request_kwargs = dict(
            metrics=metrics,
            dimensions=dimensions,
            item_group_ids=report_item_group_list,
            **base_kwargs,
            filtering=filters,
        )
        request = GMVMaxReportGetRequest(**request_kwargs)

        return await self.gmv_max_report_get(
            request, inject_promotion_types=inject_promotion_types
        )


async def fetch_all_occupied_custom_shop_ads(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    occupied_asset_type: str,
    asset_ids: Sequence[str],
) -> GMVMaxResponse[GMVMaxOccupiedListData]:
    """Aggregate the official one-asset occupancy lookup for multiple IDs."""

    clean_ids = list(dict.fromkeys(str(item) for item in asset_ids if str(item)))
    if not clean_ids:
        return GMVMaxResponse[GMVMaxOccupiedListData](
            code=0,
            message="",
            request_id=None,
            data=GMVMaxOccupiedListData(),
        )

    aggregated: list[GMVMaxOccupiedAd] = []
    seen: set[tuple[str, str, str, str]] = set()
    request_ids: list[str] = []
    for asset_id in clean_ids:
        response = await client.gmv_max_occupied_custom_shop_ads_list(
            GMVMaxOccupiedCustomShopAdsListRequest(
                advertiser_id=str(advertiser_id),
                store_id=str(store_id),
                occupied_asset_type=str(occupied_asset_type),
                asset_ids=[asset_id],
            )
        )
        if response.request_id:
            request_ids.append(str(response.request_id))
        for item in (
            getattr(response.data, "occupied_custom_shop_ads", None) or []
        ):
            item_payload = item.model_dump(exclude_none=True)
            item_payload.setdefault("asset_id", asset_id)
            if str(occupied_asset_type).upper() == "SPU":
                item_payload.setdefault("item_group_id", asset_id)
            item = GMVMaxOccupiedAd.model_validate(item_payload)
            key = (
                str(getattr(item, "campaign_id", None) or ""),
                str(getattr(item, "adgroup_id", None) or ""),
                str(getattr(item, "ad_id", None) or ""),
                str(getattr(item, "item_group_id", None) or asset_id),
            )
            if key in seen:
                continue
            aggregated.append(item)
            seen.add(key)
    return GMVMaxResponse[GMVMaxOccupiedListData](
        code=0,
        message="",
        request_id=",".join(request_ids) or None,
        data=GMVMaxOccupiedListData(occupied_custom_shop_ads=aggregated),
    )


__all__ = [
    "TikTokBusinessGMVMaxClient",
    "GMVMaxResponse",
    "GMVMaxCampaignGetRequest",
    "GMVMaxCampaignInfoRequest",
    "GMVMaxCampaignCreateRequest",
    "GMVMaxCampaignUpdateRequest",
    "GMVMaxSessionCreateRequest",
    "GMVMaxSessionUpdateRequest",
    "GMVMaxSessionListRequest",
    "GMVMaxSessionListData",
    "GMVMaxSessionMutationData",
    "gmv_max_session_entries",
    "GMVMaxIdentityGetRequest",
    "GMVMaxOccupiedCustomShopAdsListRequest",
    "GMVMaxStoreListRequest",
    "GMVMaxStoreAdUsageCheckRequest",
    "GMVMaxStoreListData",
    "GMVMaxStoreAdUsageCheckData",
    "GMVMaxVideoGetRequest",
    "GMVMaxCreationCustomAnchorVideoListGetRequest",
    "GMVMaxCreationCustomAnchorVideoListGetData",
    "GMVMaxExclusiveAuthorizationGetRequest",
    "GMVMaxExclusiveAuthorizationCreateRequest",
    "GMVMaxBidRecommendRequest",
    "GMVMaxReportData",
    "GMVMaxReportFiltering",
    "GMVMaxReportGetRequest",
    "GMVMaxReportTimeRange",
    "GMVMaxBaseReportRequest",
    "GMVMaxCampaignReportRequest",
    "GMVMaxProductReportRequest",
    "GMVMaxCreativeReportRequest",
    "GMVMaxRoomReportRequest",
    "GMVMaxSessionReportRequest",
    "fetch_all_occupied_custom_shop_ads",
]
