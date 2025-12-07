"""Typed TikTok Business GMV Max client built on top of :mod:`app.services.ttb_api`.

This module acts as the TikTok Business GMV Max **client** layer, keeping
request/response models aligned with the official API surface.
"""

from __future__ import annotations

from typing import Any, Dict, Generic, Iterable, List, Mapping, Optional, Sequence, Type, TypeVar
from datetime import datetime

import json
from enum import Enum

import httpx
from pydantic import BaseModel, ConfigDict, Field

from enum import Enum

from app.services import ttb_api as _ttb_api
from app.services.ttb_api import TTBApiClient


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

    model_config = ConfigDict(extra="allow")


class GMVMaxIdentityInfo(BaseModel):
    """Identity descriptor returned by GMV Max endpoints."""

    identity_id: Optional[str] = None
    identity_type: Optional[str] = None
    user_name: Optional[str] = None
    profile_image: Optional[str] = None

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

    list: List[GMVMaxSession] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

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
    "product": ["campaign_id", "item_group_id", "stat_time_day"],
    "creative": ["campaign_id", "item_group_id", "item_id"],
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

    model_config = ConfigDict(extra="allow")


class GMVMaxOccupiedListData(BaseModel):
    """Payload for ``gmv_max/occupied_custom_shop_ads/list``."""

    occupied_custom_shop_ads: List[GMVMaxOccupiedAd] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class GMVMaxVideo(BaseModel):
    """Entry returned by ``gmv_max/video/get``."""

    item_id: Optional[str] = None
    spu_id_list: Optional[List[str]] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None
    video_info: Optional[GMVMaxVideoInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxVideoListData(BaseModel):
    """Payload for video listing endpoints."""

    video_list: List[GMVMaxVideo] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideo(BaseModel):
    """Customized anchor video record."""

    item_id: Optional[str] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None
    spu_id_list: Optional[List[str]] = None
    video_info: Optional[GMVMaxVideoInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideoListData(BaseModel):
    """Response body for ``gmv_max/custom_anchor_video_list/get``."""

    custom_anchor_video_list: List[GMVMaxCustomAnchorVideo] = Field(default_factory=list)
    page_info: Optional[PageInfo] = None

    model_config = ConfigDict(extra="allow")


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
    summary: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="allow")


# ------------------------- Request models -------------------------


class GMVMaxCampaignFiltering(BaseModel):
    """Filtering block accepted by campaign list/report endpoints."""

    gmv_max_promotion_types: List[str]
    store_ids: Optional[List[str]] = None
    campaign_ids: Optional[List[str]] = None
    campaign_name: Optional[str] = None
    primary_status: Optional[str] = None
    creation_filter_start_time: Optional[str] = None
    creation_filter_end_time: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignGetRequest(BaseModel):
    advertiser_id: str
    filtering: GMVMaxCampaignFiltering
    fields: Optional[List[str]] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

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
    item_group_ids: Optional[List[str]] = None
    identity_list: Optional[List[GMVMaxIdentityInfo]] = None
    product_video_specific_type: Optional[str] = None
    affiliate_posts_enabled: Optional[bool] = None
    custom_anchor_video_list: Optional[List[Dict[str, Any]]] = None
    item_list: Optional[List[Dict[str, Any]]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignCreateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignCreateBody


class GMVMaxCampaignUpdateBody(BaseModel):
    campaign_id: str
    campaign_name: Optional[str] = None
    budget: Optional[float] = None
    roas_bid: Optional[float] = None
    schedule_type: Optional[str] = None
    schedule_start_time: Optional[str] = None
    schedule_end_time: Optional[str] = None
    promotion_days: Optional[PromotionDaysSetting] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignUpdateBody


class CampaignStatusUpdateRequest(BaseModel):
    """Request payload for /campaign/status/update/."""

    advertiser_id: str
    campaign_ids: List[str]
    operation_status: str
    postback_window_mode: Optional[str] = None


class GMVMaxCampaignActionApplyBody(BaseModel):
    campaign_id: str
    action_type: str
    creative_id: Optional[str] = None
    mode: Optional[str] = None
    target_daily_budget: Optional[float] = None
    budget_delta: Optional[float] = None
    currency: Optional[str] = None
    max_duration_minutes: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignActionApplyRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxCampaignActionApplyBody


class GMVMaxCampaignActionApplyData(BaseModel):
    model_config = ConfigDict(extra="allow")


class GMVMaxSessionCreateBody(BaseModel):
    campaign_id: str
    store_id: str
    bid_type: Optional[str] = None
    session: GMVMaxSessionSettings
    product_list: List[GMVMaxSessionProduct]

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionCreateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxSessionCreateBody


class GMVMaxSessionUpdateBody(BaseModel):
    campaign_id: str
    session_id: str
    store_id: Optional[str] = None
    session: Optional[GMVMaxSessionSettings] = None
    product_list: Optional[List[GMVMaxSessionProduct]] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxSessionUpdateRequest(BaseModel):
    advertiser_id: str
    body: GMVMaxSessionUpdateBody


class GMVMaxSessionListRequest(BaseModel):
    advertiser_id: str
    campaign_id: str
    page: Optional[int] = None
    page_size: Optional[int] = None


class GMVMaxIdentityGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str


class GMVMaxStoreListRequest(BaseModel):
    advertiser_id: str
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxStoreAdUsageCheckRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxOccupiedCustomShopAdsListRequest(BaseModel):
    advertiser_id: str
    store_id: str
    occupied_asset_type: str
    asset_ids: List[str]


class GMVMaxVideoGetRequest(BaseModel):
    advertiser_id: str
    store_id: str
    store_authorized_bc_id: str
    spu_id_list: Optional[List[str]] = None
    custom_posts_eligible: Optional[bool] = None
    sort_field: Optional[str] = None
    sort_order: Optional[str] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideoQuery(BaseModel):
    item_id: Optional[str] = None
    spu_id_list: Optional[List[str]] = None
    identity_info: Optional[GMVMaxIdentityInfo] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCustomAnchorVideoListGetRequest(BaseModel):
    advertiser_id: str
    campaign_id: Optional[str] = None
    campaign_custom_anchor_video_id: Optional[str] = None
    custom_anchor_video_list: Optional[List[GMVMaxCustomAnchorVideoQuery]] = None
    page: Optional[int] = None
    page_size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


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
    shopping_ads_type: str
    optimization_goal: str
    item_group_ids: Sequence[str]
    identity_id: Optional[str] = None


class GMVMaxReportFiltering(BaseModel):
    """Filtering block for GMV Max reports (campaign/product/creative/room/session)."""

    gmv_max_promotion_types: Optional[List[str]] = None
    store_ids: Optional[List[str]] = None
    campaign_ids: Optional[List[str]] = None
    item_group_ids: Optional[List[str]] = None
    creative_types: Optional[List[str]] = None
    creative_delivery_statuses: Optional[List[str]] = None
    room_ids: Optional[List[str]] = None
    search_word: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxReportTimeRange(BaseModel):
    """Time range payload accepted by GMV Max report endpoints."""

    start_time: str
    end_time: str


class GMVMaxReportGetRequest(BaseModel):
    """Request model for TikTok GET /gmv_max/report/get/."""

    advertiser_id: str
    store_ids: Sequence[str]
    start_date: str
    end_date: str
    metrics: Sequence[str]
    dimensions: Sequence[str]
    gmv_max_promotion_types: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = None
    campaign_name: Optional[str] = None
    campaign_statuses: Optional[Sequence[str]] = None
    item_group_ids: Optional[Sequence[str]] = None
    creative_types: Optional[Sequence[str]] = None
    creative_delivery_statuses: Optional[Sequence[str]] = None
    room_ids: Optional[Sequence[str]] = None
    search_word: Optional[str] = None
    enable_total_metrics: Optional[bool] = None
    filtering: Optional[GMVMaxReportFiltering] = None
    page: Optional[int] = None
    page_size: Optional[int] = None
    sort_field: Optional[str] = None
    sort_type: Optional[str] = None

    model_config = ConfigDict(extra="allow")


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
    page: Optional[int] = None
    page_size: Optional[int] = None
    cursor: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class GMVMaxCampaignReportRequest(GMVMaxBaseReportRequest):
    campaign_ids: Optional[Sequence[str]] = None


class GMVMaxProductReportRequest(GMVMaxBaseReportRequest):
    product_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = None


class GMVMaxCreativeReportRequest(GMVMaxBaseReportRequest):
    creative_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = None
    product_ids: Optional[Sequence[str]] = None


class GMVMaxRoomReportRequest(GMVMaxBaseReportRequest):
    room_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = None


class GMVMaxSessionReportRequest(GMVMaxBaseReportRequest):
    session_ids: Optional[Sequence[str]] = None
    campaign_ids: Optional[Sequence[str]] = None


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
        "promotion_types": ["PRODUCT"],
        "dimensions": ["campaign_id", "item_group_id", "stat_time_day"],
        "require_all_of": ("campaign_ids",),
        "require_any_of": (),
    },
    # Product GMV Max, creative-level（campaign_ids + item_group_ids Required）
    GMVMaxDataset.CREATIVE: {
        "promotion_types": None,  # 创意层禁止 gmv_max_promotion_types 过滤
        "dimensions": ["campaign_id", "item_group_id", "item_id"],
        "require_all_of": ("campaign_ids", "item_group_ids"),
        "require_any_of": (),
    },
    # Product GMV Max, duration-level（campaign_ids + item_group_ids Required）
    GMVMaxDataset.PRODUCT_DURATION: {
        "promotion_types": ["PRODUCT"],
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
        "promotion_types": ["LIVE"],
        "dimensions": ["room_id", "stat_time_day"],
        "require_all_of": ("campaign_ids",),
        "require_any_of": (),
    },
    # LIVE GMV Max, duration-level（campaign_ids + room_ids Required）
    GMVMaxDataset.LIVE_DURATION: {
        "promotion_types": ["LIVE"],
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

        # TikTok expects store_ids as a top-level JSON array instead of inside the
        # filtering object. Preserve compatibility by accepting store_ids from
        # either location but always emit a single normalized payload.
        normalized_store_ids.extend(_coerce_store_ids(params.pop("store_ids", None)))
        if normalized_store_ids:
            unique_ids = list(dict.fromkeys(normalized_store_ids))
            params["store_ids"] = json.dumps(unique_ids, ensure_ascii=False)
        elif filtering_payload is not None:
            filtering_payload.pop("store_ids", None)

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
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
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

    async def gmv_max_campaign_action_apply(
        self, request: GMVMaxCampaignActionApplyRequest
    ) -> GMVMaxResponse[GMVMaxCampaignActionApplyData]:
        """Wrapper for TikTok POST /campaign/gmv_max/action/apply/ (e.g., BOOST_CREATIVE)."""
        params = {"advertiser_id": request.advertiser_id}
        body = request.body.model_dump(exclude_none=True)
        body["advertiser_id"] = request.advertiser_id
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/action/apply/",
            params=_ttb_api._clean_params_map(params),
            json_body=_ttb_api._remove_none(body),
        )
        return self._parse_response(payload, GMVMaxCampaignActionApplyData)

    async def gmv_max_session_create(
        self, request: GMVMaxSessionCreateRequest
    ) -> GMVMaxResponse[GMVMaxSessionListData]:
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
        return self._parse_response(payload, GMVMaxSessionListData)

    async def gmv_max_session_update(
        self, request: GMVMaxSessionUpdateRequest
    ) -> GMVMaxResponse[GMVMaxSessionListData]:
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
        return self._parse_response(payload, GMVMaxSessionListData)

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
        """Wrapper for TikTok GET /gmv_max/occupied_custom_shop_ads/list/."""
        params = request.model_dump(exclude_none=True)
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
        payload = await self._request_json(
            "GET",
            "/gmv_max/video/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxVideoListData)

    async def gmv_max_custom_anchor_video_list_get(
        self, request: GMVMaxCustomAnchorVideoListGetRequest
    ) -> GMVMaxResponse[GMVMaxCustomAnchorVideoListData]:
        """Wrapper for TikTok GET /gmv_max/custom_anchor_video_list/get/."""
        params = request.model_dump(exclude_none=True)
        payload = await self._request_json(
            "GET",
            "/gmv_max/custom_anchor_video_list/get/",
            params=_ttb_api._clean_params_map(params),
        )
        return self._parse_response(payload, GMVMaxCustomAnchorVideoListData)

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
        params["item_group_ids"] = [str(item) for item in request.item_group_ids]
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
        def _encode_seq(values: Sequence[Any] | None) -> str | None:
            if values is None:
                return None
            normalized = [str(item) for item in values if item is not None]
            return json.dumps(normalized, ensure_ascii=False)

        store_ids = [str(store) for store in request.store_ids]
        params: Dict[str, Any] = {
            "advertiser_id": request.advertiser_id,
            "store_ids": json.dumps(store_ids, ensure_ascii=False),
            "start_date": request.start_date,
            "end_date": request.end_date,
            "metrics": json.dumps(list(request.metrics), ensure_ascii=False),
            "dimensions": json.dumps(list(request.dimensions), ensure_ascii=False),
        }

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
            encoded = _encode_seq(values)
            if encoded is not None:
                params[key] = encoded

        if request.campaign_name is not None:
            params["campaign_name"] = request.campaign_name
        if request.search_word is not None:
            params["search_word"] = request.search_word
        if request.enable_total_metrics is not None:
            params["enable_total_metrics"] = bool(request.enable_total_metrics)
        if request.filtering is not None:
            params["filtering"] = json.dumps(
                request.filtering.model_dump(exclude_none=True), ensure_ascii=False
            )
        for key in ("page", "page_size", "sort_field", "sort_type"):
            value = getattr(request, key)
            if value is not None:
                params[key] = value

        if inject_promotion_types:
            _ttb_api._ensure_gmvmax_campaign_filters(
                params, promotion_type_format="report"
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
            store_ids=store_ids,
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
        """Wrapper for TikTok GET /gmv_max/report/get/ to fetch GMV Max metrics."""

        aggregated_entries: list[GMVMaxReportEntry] = []
        summary: Dict[str, Any] | None = None
        merged_page_info: PageInfo | None = None
        page_value = request.page or 1
        page_size_value = request.page_size or 200

        while True:
            request.page = page_value
            request.page_size = page_size_value
            params = self._build_report_get_params(
                request, inject_promotion_types=inject_promotion_types
            )
            response = await self._gmv_max_report_get(params)
            aggregated_entries.extend(response.data.list)
            if summary is None:
                summary = response.data.summary
            merged_page_info = response.data.page_info or merged_page_info

            page_info = response.data.page_info
            has_more = bool(
                getattr(page_info, "has_more", False) or getattr(page_info, "has_next", False)
            ) if page_info else False
            total_page = getattr(page_info, "total_page", None) if page_info else None
            if has_more:
                page_value += 1
                continue
            try:
                total_page_int = int(total_page) if total_page is not None else None
            except (TypeError, ValueError):
                total_page_int = None
            if total_page_int is not None and page_value < total_page_int:
                page_value += 1
                continue
            break

        merged_data = GMVMaxReportData(
            list=aggregated_entries,
            page_info=merged_page_info,
            summary=summary,
        )
        return GMVMaxResponse[GMVMaxReportData](  # type: ignore[call-arg]
            code=response.code,
            message=response.message,
            request_id=response.request_id,
            data=merged_data,
        )

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

        filters = GMVMaxReportFiltering(
            campaign_ids=campaign_id_list,
            item_group_ids=item_group_list,
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
            item_group_ids=item_group_list,
            **base_kwargs,
            filtering=filters,
        )
        request = GMVMaxReportGetRequest(**request_kwargs)

        return await self.gmv_max_report_get(
            request, inject_promotion_types=inject_promotion_types
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
    "GMVMaxIdentityGetRequest",
    "GMVMaxOccupiedCustomShopAdsListRequest",
    "GMVMaxVideoGetRequest",
    "GMVMaxCustomAnchorVideoListGetRequest",
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
]

