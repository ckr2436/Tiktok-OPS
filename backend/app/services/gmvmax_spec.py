"""Canonical GMV Max metric and dimension definitions.

This module centralizes the official metric and dimension names supported by
TikTok's GMV Max reporting API so that both service and feature layers use the
same configuration when talking to the upstream endpoint.
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Final

# Metrics documented under "Metrics in GMV Max Campaign reports". Keeping the
# list explicit helps us gate-keep unsupported/typoed names before calling the
# upstream API, which otherwise responds with 40002 errors.
GMVMAX_BASE_METRICS: Final[tuple[str, ...]] = (
    "cost",
    "net_cost",
    "orders",
    "cost_per_order",
    "gross_revenue",
    "roi",
)

# Campaign-level attribute metrics that must be passed as metrics (not dimensions)
# when requesting Product GMV Max campaign reports.
GMVMAX_CAMPAIGN_ATTRIBUTE_METRICS: Final[tuple[str, ...]] = (
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
)

GMVMAX_PERFORMANCE_METRICS: Final[tuple[str, ...]] = (
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
)

GMVMAX_LIVE_METRICS: Final[tuple[str, ...]] = (
    "live_views",
    "live_follows",
    "cost_per_live_view",
    "10_second_live_views",
    "cost_per_10_second_live_view",
)

GMVMAX_SUPPORTED_METRICS: Final[set[str]] = set(
    GMVMAX_BASE_METRICS
    + GMVMAX_PERFORMANCE_METRICS
    + GMVMAX_LIVE_METRICS
    + GMVMAX_CAMPAIGN_ATTRIBUTE_METRICS
    + ("creative_delivery_status",)
)

# Some tenants may still send deprecated field names (for example "spend").
# Explicit aliases allow us to normalize them into supported names without
# leaking unsupported values to TikTok.
GMVMAX_METRIC_ALIASES: Final[dict[str, str]] = {
    "spend": "cost",
}

# Defaults used by both background sync jobs and tenant facing APIs:
# generic performance metrics that are valid at all levels.
GMVMAX_DEFAULT_METRICS: Final[tuple[str, ...]] = GMVMAX_BASE_METRICS

# Creative level monitoring currently uses full performance metrics + status
GMVMAX_CREATIVE_METRICS: Final[tuple[str, ...]] = (
    "creative_delivery_status",
    "cost",
    "orders",
    "cost_per_order",
    "gross_revenue",
    "roi",
    *GMVMAX_PERFORMANCE_METRICS,
)


class GMVMaxReportLevel(str, Enum):
    """Supported aggregation levels for GMV Max reports."""

    OVERVIEW = "overview"
    CAMPAIGN = "campaign"
    PRODUCT = "product"
    CREATIVE = "creative"
    ROOM = "room"
    SESSION = "session"


GMV_REPORT_CONFIG: Final[dict[GMVMaxReportLevel, dict[str, object]]] = {
    GMVMaxReportLevel.OVERVIEW: {
        "dimensions": ("advertiser_id", "stat_time_day"),
        "metrics": GMVMAX_BASE_METRICS,
        "max_range": timedelta(days=30),
    },
    GMVMaxReportLevel.CAMPAIGN: {
        "dimensions": ("campaign_id", "stat_time_day"),
        "metrics": (
            *GMVMAX_CAMPAIGN_ATTRIBUTE_METRICS,
            *GMVMAX_BASE_METRICS,
        ),
        "max_range": timedelta(days=30),
    },
    GMVMaxReportLevel.PRODUCT: {
        "dimensions": ("campaign_id", "item_group_id", "stat_time_day"),
        "metrics": (
            "cost",
            "orders",
            "cost_per_order",
            "gross_revenue",
            "roi",
        ),
        "max_range": timedelta(days=30),
    },
    GMVMaxReportLevel.CREATIVE: {
        "dimensions": ("campaign_id", "item_group_id", "item_id"),
        "metrics": GMVMAX_CREATIVE_METRICS,
        "max_range": timedelta(days=30),
    },
    GMVMaxReportLevel.ROOM: {
        "dimensions": ("campaign_id", "room_id", "stat_time_day"),
        "metrics": GMVMAX_DEFAULT_METRICS + GMVMAX_LIVE_METRICS,
        "max_range": timedelta(days=30),
    },
    GMVMaxReportLevel.SESSION: {
        "dimensions": ("campaign_id", "room_id", "duration", "stat_time_day"),
        "metrics": GMVMAX_DEFAULT_METRICS + GMVMAX_LIVE_METRICS,
        "max_range": timedelta(days=30),
    },
}

# Dimension set defined by https://business-api.tiktok.com/portal/docs?id=1824722485971009
GMVMAX_SUPPORTED_DIMENSIONS: Final[set[str]] = {
    "advertiser_id",
    "campaign_id",
    "stat_time_day",
    "stat_time_hour",
    "item_group_id",
    "item_id",
    "room_id",
    "duration",
}

GMVMAX_DEFAULT_DIMENSIONS: Final[tuple[str, ...]] = (
    "campaign_id",
    "stat_time_day",
)


__all__ = [
    "GMVMAX_BASE_METRICS",
    "GMVMAX_CAMPAIGN_ATTRIBUTE_METRICS",
    "GMVMAX_PERFORMANCE_METRICS",
    "GMVMAX_LIVE_METRICS",
    "GMVMAX_SUPPORTED_METRICS",
    "GMVMAX_METRIC_ALIASES",
    "GMVMAX_DEFAULT_METRICS",
    "GMVMAX_CREATIVE_METRICS",
    "GMVMAX_SUPPORTED_DIMENSIONS",
    "GMVMAX_DEFAULT_DIMENSIONS",
    "GMVMaxReportLevel",
    "GMV_REPORT_CONFIG",
]
