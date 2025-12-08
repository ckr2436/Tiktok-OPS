from app.features.tenants.ttb.gmv_max.router_provider import (
    SUPPORTED_GMVMAX_METRIC_LEVELS,
)
from app.services.gmvmax_spec import GMVMaxReportLevel


def test_supported_levels_include_overview_and_dimensions():
    assert GMVMaxReportLevel.OVERVIEW in SUPPORTED_GMVMAX_METRIC_LEVELS
    assert GMVMaxReportLevel.CAMPAIGN in SUPPORTED_GMVMAX_METRIC_LEVELS
    assert GMVMaxReportLevel.PRODUCT in SUPPORTED_GMVMAX_METRIC_LEVELS
    assert GMVMaxReportLevel.CREATIVE in SUPPORTED_GMVMAX_METRIC_LEVELS
