"""Compatibility aliases to route legacy GMV Max code to the restructured schema."""

from app.data.models.gmv_restructured import (
    GmvActionLog,
    GmvCampaign,
    GmvCampaignProduct,
    GmvCampaignMetricsDaily,
    GmvCampaignMetricsHourly,
    GmvCreativeHeating,
    GmvCreativeMetrics10Min,
    GmvCreativeMetricsDaily,
    GmvMonitoringStrategy,
    GmvStrategyConfig,
)

# Expose legacy class names so the rest of the codebase can continue importing
# ``app.data.models.ttb_gmvmax`` while operating on the new ``gmv_*`` tables.
TTBGmvMaxCampaign = GmvCampaign
TTBGmvMaxCampaignProduct = GmvCampaignProduct
TTBGmvMaxMetricsHourly = GmvCampaignMetricsHourly
TTBGmvMaxMetricsDaily = GmvCampaignMetricsDaily
TTBGmvMaxCreativeMetric = GmvCreativeMetricsDaily
TTBGmvMaxCreativeMetrics10Min = GmvCreativeMetrics10Min
TTBGmvMaxCreativeHeating = GmvCreativeHeating
TTBGmvMaxActionLog = GmvActionLog
TTBGmvMaxStrategyConfig = GmvStrategyConfig
TTBGmvMaxMonitoringStrategy = GmvMonitoringStrategy

# Provide backwards-compatible attribute aliases for renamed columns.
TTBGmvMaxMetricsHourly.interval_start = TTBGmvMaxMetricsHourly.stat_time_hour
TTBGmvMaxMetricsDaily.date = TTBGmvMaxMetricsDaily.stat_time_day

__all__ = [
    "TTBGmvMaxCampaign",
    "TTBGmvMaxCampaignProduct",
    "TTBGmvMaxMetricsHourly",
    "TTBGmvMaxMetricsDaily",
    "TTBGmvMaxCreativeMetric",
    "TTBGmvMaxCreativeMetrics10Min",
    "TTBGmvMaxCreativeHeating",
    "TTBGmvMaxActionLog",
    "TTBGmvMaxStrategyConfig",
    "TTBGmvMaxMonitoringStrategy",
]
