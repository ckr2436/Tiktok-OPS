from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import (
    CreativeMetricsAggregate,
)
from app.services.gmvmax_heating import evaluate_heating_rule


def _make_heating(**overrides):
    base = {
        "workspace_id": 1,
        "auth_id": 1,
        "advertiser_id": "adv",
        "campaign_id": "cmp",
        "creative_id": "cr",
        "promotion_type": "PRODUCT",
        "evaluation_window_minutes": overrides.get("evaluation_window_minutes", 60),
        "auto_stop_enabled": overrides.get("auto_stop_enabled", True),
        "is_heating_active": overrides.get("is_heating_active", True),
        "min_clicks": overrides.get("min_clicks"),
        "min_ctr": overrides.get("min_ctr"),
        "min_gross_revenue": overrides.get("min_gross_revenue"),
    }
    return TTBGmvMaxCreativeHeating(**base)


def _metrics(clicks=0, ctr=None, revenue=None):
    return CreativeMetricsAggregate(
        creative_id="cr",
        item_group_id="spu",
        clicks=clicks,
        ad_click_rate=ctr,
        gross_revenue=revenue,
        cost=None,
        orders=None,
        roi=None,
        creative_status=None,
    )


def test_evaluate_heating_rule_metrics_missing():
    heating = _make_heating()
    result = evaluate_heating_rule(heating, None)
    assert result.result == "metrics_missing"
    assert result.should_stop is False


def test_evaluate_heating_rule_thresholds_pass():
    heating = _make_heating(min_clicks=10, min_ctr=0.02, min_gross_revenue=50)
    metrics = _metrics(clicks=12, ctr=0.03, revenue=60)
    result = evaluate_heating_rule(heating, metrics)
    assert result.result == "ready_to_heat"
    assert result.should_stop is False
    assert result.ready_to_heat is True


def test_evaluate_heating_rule_ready_to_heat_with_roi_and_orders():
    heating = _make_heating(min_clicks=1, min_ctr=0.01, min_gross_revenue=1)
    metrics = CreativeMetricsAggregate(
        creative_id="cr",
        item_group_id="spu",
        clicks=5,
        ad_click_rate=0.05,
        gross_revenue=100,
        cost=20,
        orders=3,
        roi=5.0,
        creative_status="DELIVERING",
    )
    result = evaluate_heating_rule(heating, metrics)
    assert result.result == "ready_to_heat"
    assert result.ready_to_heat is True
    assert result.should_stop is False


def test_evaluate_heating_rule_auto_stop_clicks():
    heating = _make_heating(min_clicks=10, auto_stop_enabled=True, is_heating_active=True)
    metrics = _metrics(clicks=5)
    result = evaluate_heating_rule(heating, metrics)
    assert result.result == "auto_stopped_low_clicks"
    assert result.should_stop is True


def test_evaluate_heating_rule_threshold_without_auto_stop():
    heating = _make_heating(min_ctr=0.05, auto_stop_enabled=False, is_heating_active=True)
    metrics = _metrics(clicks=50, ctr=0.03)
    result = evaluate_heating_rule(heating, metrics)
    assert result.result == "threshold_failed_low_ctr"
    assert result.should_stop is False


def test_evaluate_heating_rule_revenue_stop_only_when_active():
    heating = _make_heating(min_gross_revenue=200, auto_stop_enabled=True, is_heating_active=False)
    metrics = _metrics(clicks=100, ctr=0.1, revenue=150)
    result = evaluate_heating_rule(heating, metrics)
    assert result.result == "threshold_failed_low_revenue"
    assert result.should_stop is False
