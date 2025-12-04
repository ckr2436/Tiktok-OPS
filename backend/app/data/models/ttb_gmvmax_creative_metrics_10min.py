"""Compatibility alias mapping legacy 10-min creative metrics to the new schema."""

from app.data.models.gmv_restructured import GmvCreativeMetrics10Min

TTBGmvMaxCreativeMetrics10Min = GmvCreativeMetrics10Min

__all__ = ["TTBGmvMaxCreativeMetrics10Min"]
