from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import gmvmax_creative_guard as guard


def _scope(*, learning: dict | None = None) -> guard.CampaignScope:
    return guard.CampaignScope(
        strategy_id=1,
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=20_000,
        roas_bid=Decimal("1.2"),
        config={"learning_protection": learning or {}},
        monitor_state={},
        smart_guard_state={},
    )


def _metric(*, orders: int, cost_cents: int) -> guard.CreativeMetric:
    return guard.CreativeMetric(
        creative_id="creative-1",
        item_group_id="product-1",
        status="DELIVERING",
        cost_cents=cost_cents,
        gross_revenue_cents=cost_cents,
        orders=orders,
        product_impressions=1_000,
        product_clicks=25,
        ad_click_rate=Decimal("2.5"),
        product_click_rate=Decimal("2.5"),
        ad_conversion_rate=Decimal("4"),
        roi=Decimal("1"),
    )


def _activity(start_at: datetime) -> guard.CampaignActivity:
    return guard.CampaignActivity(
        campaign_start_at=start_at,
        latest_metric_at=start_at,
        today_cost_cents=0,
        recent_snapshot_count=0,
        low_spend_window_minutes=60,
        low_spend_delta_cents=0,
        low_spend_order_delta=0,
        low_spend_latest_cost_cents=0,
        low_spend_latest_orders=0,
    )


def _roi_remove_decision() -> dict:
    return {
        "action": "REMOVE",
        "reason": "creative_guard:roi_below_target",
        "context": {"no_order_threshold_cents": 1_600},
    }


def test_learning_protection_holds_one_order_roi_remove(monkeypatch) -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    scope = _scope(
        learning={
            "enabled": True,
            "min_campaign_age_hours": 72,
            "min_orders_for_roi_remove": 3,
            "min_roi_spend_multiplier": "2.0",
        }
    )
    monkeypatch.setattr(guard, "_load_campaign_activity", lambda *_args, **_kwargs: _activity(now - timedelta(days=7)))

    result = guard._apply_learning_protection(
        object(), scope, _metric(orders=1, cost_cents=1_600), _roi_remove_decision(), now=now
    )

    assert result["action"] == "HOLD"
    assert result["reason"] == "creative_guard:learning_protection:roi_sample_immature"


def test_learning_protection_allows_mature_repeatable_roi_remove(monkeypatch) -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    scope = _scope(
        learning={
            "enabled": True,
            "min_campaign_age_hours": 72,
            "min_orders_for_roi_remove": 3,
            "min_roi_spend_multiplier": "2.0",
        }
    )
    monkeypatch.setattr(guard, "_load_campaign_activity", lambda *_args, **_kwargs: _activity(now - timedelta(days=7)))

    result = guard._apply_learning_protection(
        object(), scope, _metric(orders=3, cost_cents=3_200), _roi_remove_decision(), now=now
    )

    assert result["action"] == "REMOVE"


def test_learning_protection_disables_automatic_campaign_rebuild() -> None:
    scope = _scope(learning={"enabled": True, "auto_rebuild_enabled": False})
    result = guard._apply_learning_protection(
        object(),
        scope,
        _metric(orders=0, cost_cents=10_000),
        {"action": "RESET_CAMPAIGN", "reason": "creative_guard:no_order_spend_threshold"},
        now=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    assert result["action"] == "HOLD"
    assert result["reason"] == "creative_guard:learning_protection:auto_rebuild_disabled"


def test_custom_selection_is_rejected_before_official_creative_mutation() -> None:
    class _Result:
        def mappings(self):
            return self

        def first(self):
            return {
                "product_specific_type": "CUSTOM_SELECTION",
                "detail_raw_json": {},
            }

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Result()

    with pytest.raises(guard.CreativeGuardAutomationHold, match="custom video selection"):
        guard._assert_campaign_supports_creative_status_update(_Db(), _scope())


def test_default_retest_window_is_not_sub_day() -> None:
    config = guard.default_creative_guard_config()["retest"]
    assert config["min_cooldown_minutes"] >= 24 * 60
    assert config["time_bucket_hours"] >= 24


def test_legacy_guard_config_inherits_learning_protection_defaults() -> None:
    strategy = SimpleNamespace(
        config_json={
            "creative_guard": {
                "enabled": True,
                "product_card_reset": {"enabled": True, "recreate": True},
            }
        }
    )

    config = guard._creative_guard_config(strategy)

    assert config["learning_protection"]["enabled"] is True
    assert config["learning_protection"]["auto_rebuild_enabled"] is False
