"""Canonical GMV Max metric and dimension definitions.

This module centralizes the official metric and dimension names supported by
TikTok's GMV Max reporting API so that both service and feature layers use the
same configuration when talking to the upstream endpoint.
"""

from __future__ import annotations

from typing import Final

# Metrics documented under "Metrics in GMV Max Campaign reports". Keeping the
# list explicit helps us gate-keep unsupported/typoed names before calling the
# upstream API, which otherwise responds with 40002 errors.
GMVMAX_SUPPORTED_METRICS: Final[set[str]] = {
    "cost",
    "net_cost",
    "orders",
    "cost_per_order",
    "gross_revenue",
    "roi",
    "product_impressions",
    "product_clicks",
    "product_click_rate",
    "ad_click_rate",
    "ad_conversion_rate",
    "video_views_2s",
    "video_views_6s",
    "video_views_p25",
    "video_views_p50",
    "video_views_p75",
    "video_views_p100",
    "live_views",
    "live_follows",
    "cost_per_live_view",
    "10_second_live_views",
    "cost_per_10_second_live_view",
}

# Some tenants may still send deprecated field names (for example "spend").
# Explicit aliases allow us to normalize them into supported names without
# leaking unsupported values to TikTok.
GMVMAX_METRIC_ALIASES: Final[dict[str, str]] = {
    "spend": "cost",
}

# Defaults used by both background sync jobs and tenant facing APIs.
GMVMAX_DEFAULT_METRICS: Final[tuple[str, ...]] = (
    # Campaign-level reports only support aggregate commerce metrics. Official
    # API rejects product/video/live specific metrics when dimensions only
    # contain campaign_id/stat_time_day, so keep defaults limited to the
    # supported subset to avoid 40002 errors.
    "cost",
    "net_cost",
    "orders",
    "cost_per_order",
    "gross_revenue",
    "roi",
)

# Creative level monitoring needs a wider set because auto-heating relies on
# click-through/conversion signals.
GMVMAX_CREATIVE_METRICS: Final[tuple[str, ...]] = (
    "cost",
    "net_cost",
    "orders",
    "gross_revenue",
    "roi",
    "product_impressions",
    "product_clicks",
    "product_click_rate",
    "ad_click_rate",
    "ad_conversion_rate",
    "video_views_2s",
    "video_views_6s",
    "video_views_p25",
    "video_views_p50",
    "video_views_p75",
    "video_views_p100",
    "live_views",
    "live_follows",
)

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
    "GMVMAX_SUPPORTED_METRICS",
    "GMVMAX_METRIC_ALIASES",
    "GMVMAX_DEFAULT_METRICS",
    "GMVMAX_CREATIVE_METRICS",
    "GMVMAX_SUPPORTED_DIMENSIONS",
    "GMVMAX_DEFAULT_DIMENSIONS",
]
