from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import app.celery_app  # noqa: F401 - establish production task import order
from app.services import gmvmax_creative_guard as creative_guard


def test_zero_spend_excluded_creative_is_loaded_for_retest(monkeypatch):
    scope = creative_guard.CampaignScope(
        strategy_id=71,
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=20_000,
        roas_bid=Decimal("1.4"),
        config={},
        monitor_state={},
        smart_guard_state={},
    )

    class FakeMappings:
        def all(self):
            return [
                {
                    "creative_id": "creative-1",
                    "item_group_id": "product-1",
                    "creative_delivery_status": "EXCLUDED",
                    "cost_cents": 0,
                    "gross_revenue_cents": 0,
                    "orders": 0,
                    "product_impressions": 0,
                    "product_clicks": 0,
                }
            ]

    class FakeResult:
        def mappings(self):
            return FakeMappings()

    class FakeDb:
        statement = ""

        def execute(self, statement, _params):
            self.statement = str(statement)
            return FakeResult()

    db = FakeDb()
    monkeypatch.setattr(
        creative_guard,
        "_campaign_report_start_day",
        lambda *_args: datetime(2026, 7, 13, tzinfo=timezone.utc).date(),
    )

    metrics = creative_guard._load_daily_creatives(db, scope)

    assert len(metrics) == 1
    assert metrics[0].status == "EXCLUDED"
    assert metrics[0].cost_cents == 0
    assert "%EXCLUD%" in db.statement


def test_inherited_exclusion_is_retested_when_not_permanently_blacklisted(monkeypatch):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    scope = creative_guard.CampaignScope(
        strategy_id=71,
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=20_000,
        roas_bid=Decimal("1.4"),
        # This case exercises inherited-exclusion retry eligibility.  Keep the
        # calendar-bucket rule out of scope; production defaults deliberately
        # require a new daily bucket before a creative can be reintroduced.
        config={"retest": {"enabled": True, "require_new_time_bucket": False}},
        monitor_state={},
        smart_guard_state={},
    )
    metric = creative_guard.CreativeMetric(
        creative_id="creative-1",
        item_group_id="product-1",
        status="EXCLUDED",
        cost_cents=1_500,
        gross_revenue_cents=0,
        orders=0,
        product_impressions=500,
        product_clicks=25,
        ad_click_rate=Decimal("0.05"),
        product_click_rate=Decimal("0.05"),
        ad_conversion_rate=Decimal("0"),
        roi=Decimal("0"),
    )
    activity = creative_guard.CampaignActivity(
        campaign_start_at=now - timedelta(hours=24),
        latest_metric_at=now,
        today_cost_cents=1_500,
        recent_snapshot_count=3,
        low_spend_window_minutes=30,
        low_spend_delta_cents=0,
        low_spend_order_delta=0,
        low_spend_latest_cost_cents=1_500,
        low_spend_latest_orders=0,
    )
    inserted = []

    monkeypatch.setattr(creative_guard, "_manual_pause_override_active", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(creative_guard, "_campaign_is_currently_enabled", lambda *_args: True)
    monkeypatch.setattr(creative_guard, "_load_campaign_activity", lambda *_args, **_kwargs: activity)
    monkeypatch.setattr(creative_guard, "_historical_removed_creatives_for_scope", lambda *_args: [])
    monkeypatch.setattr(
        creative_guard,
        "_creative_event_history",
        lambda *_args: [
            {
                "action": "REMOVE",
                "reason": "creative_guard:inherit_historical_exclusions",
                "created_at": now - timedelta(hours=3),
                "request_json": {},
            }
        ],
    )
    monkeypatch.setattr(
        creative_guard,
        "_creative_quality_context",
        lambda *_args: {"high_quality": True, "quality_score": 10},
    )
    monkeypatch.setattr(
        creative_guard,
        "_failed_retest_state",
        lambda *_args: (0, None),
    )
    monkeypatch.setattr(
        creative_guard,
        "gmvmax_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(commit=lambda *_: None)
        ),
    )
    monkeypatch.setattr(creative_guard, "_dynamic_retest_cooldown_minutes", lambda *_args, **_kwargs: 45)
    monkeypatch.setattr(creative_guard, "_creative_time_bucket", lambda *_args, **_kwargs: "bucket-2")

    async def fake_add_back(*_args, **_kwargs):
        return {"body": {"action": "ADD"}}, {"code": 0}

    monkeypatch.setattr(creative_guard, "_add_back_creative", fake_add_back)
    monkeypatch.setattr(
        creative_guard,
        "_insert_event",
        lambda *_args, **kwargs: inserted.append(kwargs),
    )

    result = asyncio.run(
        creative_guard._retest_removed_creatives(object(), scope, [metric], now=now)
    )

    assert result == {"added": 1, "creative_ids": ["creative-1"], "errors": 0}
    assert inserted[0]["decision"]["reason"] == "creative_guard:scheduled_retest"
    assert inserted[0]["decision"]["context"]["inherited_exclusion"] is True


def test_failed_retest_does_not_starve_later_candidate(monkeypatch):
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    scope = creative_guard.CampaignScope(
        strategy_id=71,
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=20_000,
        roas_bid=Decimal("1.4"),
        # This case exercises failure isolation between candidates, not the
        # production daily retest cadence.
        config={
            "retest": {
                "enabled": True,
                "max_add_back_per_cycle": 1,
                "require_new_time_bucket": False,
            }
        },
        monitor_state={},
        smart_guard_state={},
    )
    metrics = [
        creative_guard.CreativeMetric(
            creative_id=creative_id,
            item_group_id=f"product-{index}",
            status="EXCLUDED",
            cost_cents=2_000 - index,
            gross_revenue_cents=0,
            orders=0,
            product_impressions=500,
            product_clicks=25,
            ad_click_rate=None,
            product_click_rate=None,
            ad_conversion_rate=None,
            roi=None,
        )
        for index, creative_id in enumerate(("creative-bad", "creative-good"))
    ]
    activity = creative_guard.CampaignActivity(
        campaign_start_at=now - timedelta(hours=24),
        latest_metric_at=now,
        today_cost_cents=2_000,
        recent_snapshot_count=3,
        low_spend_window_minutes=30,
        low_spend_delta_cents=0,
        low_spend_order_delta=0,
        low_spend_latest_cost_cents=2_000,
        low_spend_latest_orders=0,
    )
    inserted = []
    attempted = []

    monkeypatch.setattr(
        creative_guard,
        "_manual_pause_override_active",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        creative_guard,
        "_campaign_is_currently_enabled",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        creative_guard,
        "_load_campaign_activity",
        lambda *_args, **_kwargs: activity,
    )
    monkeypatch.setattr(
        creative_guard,
        "_historical_removed_creatives_for_scope",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        creative_guard,
        "_creative_event_history",
        lambda *_args: [
            {
                "action": "REMOVE",
                "reason": "creative_guard:test",
                "created_at": now - timedelta(hours=6),
                "request_json": {},
            }
        ],
    )
    monkeypatch.setattr(
        creative_guard,
        "_failed_retest_state",
        lambda *_args: (0, None),
    )
    monkeypatch.setattr(
        creative_guard,
        "gmvmax_mutation_lease",
        lambda *_args, **_kwargs: nullcontext(
            SimpleNamespace(commit=lambda *_: None)
        ),
    )
    monkeypatch.setattr(
        creative_guard,
        "_creative_quality_context",
        lambda metric, *_args: {
            "high_quality": True,
            "quality_score": 10 if metric.creative_id == "creative-bad" else 9,
        },
    )
    monkeypatch.setattr(
        creative_guard,
        "_dynamic_retest_cooldown_minutes",
        lambda *_args, **_kwargs: 45,
    )
    monkeypatch.setattr(
        creative_guard,
        "_creative_time_bucket",
        lambda *_args, **_kwargs: "bucket-2",
    )

    async def fake_add_back(_db, _scope, metric, _decision):
        attempted.append(metric.creative_id)
        if metric.creative_id == "creative-bad":
            raise RuntimeError("permanent official rejection")
        return {"body": {"action": "ADD"}}, {"code": 0}

    monkeypatch.setattr(creative_guard, "_add_back_creative", fake_add_back)
    monkeypatch.setattr(
        creative_guard,
        "_insert_event",
        lambda *_args, **kwargs: inserted.append(kwargs),
    )

    result = asyncio.run(
        creative_guard._retest_removed_creatives(
            object(),
            scope,
            metrics,
            now=now,
        )
    )

    assert attempted == ["creative-bad", "creative-good"]
    assert result == {
        "added": 1,
        "creative_ids": ["creative-good"],
        "errors": 1,
    }
    assert inserted[0]["result"] == "FAILED"
    assert inserted[0]["request_json"]["creative_id"] == "creative-bad"
    assert inserted[1]["result"] == "SUCCESS"


def test_product_card_rebuild_is_deferred_for_manual_or_external_pause(monkeypatch):
    scope = creative_guard.CampaignScope(
        strategy_id=9,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
        budget_cents=10000,
        roas_bid=None,
        config={},
        monitor_state={},
        smart_guard_state={},
    )
    monkeypatch.setattr(creative_guard, "_load_scopes", lambda *_: [scope])
    monkeypatch.setattr(
        creative_guard,
        "_manual_pause_override_active",
        lambda *_, **__: True,
    )
    monkeypatch.setattr(
        creative_guard,
        "_campaign_is_currently_enabled",
        lambda *_, **__: False,
    )
    monkeypatch.setattr(
        creative_guard,
        "build_ttb_gmvmax_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("official client must not be created")
        ),
    )

    request, response = asyncio.run(
        creative_guard.rebuild_campaign_for_delivery_failure(
            object(),
            strategy_id=9,
            reason="creative_guard:no_spend_timeout",
        )
    )

    assert request["old_campaign_id"] == "campaign-1"
    assert response["rebuild_deferred"] is True
    assert response["manual_pause_override"] is True


def test_campaign_activity_accepts_current_day_downward_correction(monkeypatch):
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    scope = creative_guard.CampaignScope(
        strategy_id=9,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=10000,
        roas_bid=None,
        config={},
        monitor_state={},
        smart_guard_state={},
    )

    class _Result:
        def __init__(self, value):
            self.value = value

        def mappings(self):
            return self

        def first(self):
            return self.value if isinstance(self.value, dict) else None

        def all(self):
            return self.value if isinstance(self.value, list) else []

    class _Db:
        def __init__(self):
            self.values = [
                {"created_at": now.replace(tzinfo=None) - timedelta(hours=2)},
                {
                    "hourly_rows": 1,
                    "hourly_cost_cents": 2000,
                    "official_daily_rows": 1,
                    "official_daily_cost_cents": 10000,
                    "creative_rows": 1,
                    "creative_cost_cents": 10000,
                    "latest_metric_at": now.replace(tzinfo=None),
                    "recent_snapshot_count": 1,
                },
                [],
            ]
            self.statements = []

        def execute(self, statement, _params):
            self.statements.append(str(statement))
            return _Result(self.values.pop(0))

    db = _Db()
    monkeypatch.setattr(
        creative_guard,
        "_advertiser_today",
        lambda *_: now.date(),
    )

    activity = creative_guard._load_campaign_activity(db, scope, now=now)

    # The current hourly official correction (100 -> 20) must win as one
    # complete source row; MAX across sources would incorrectly retain 100.
    assert activity.today_cost_cents == 2000
    metrics_query = " ".join(db.statements[1].split())
    assert "date(stat_time_hour)=:today" in metrics_query
    assert "source_observed_at is not null" in metrics_query
    assert ":start_day" not in metrics_query


def test_realtime_creatives_keep_same_creative_separate_per_item_group(monkeypatch):
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    scope = creative_guard.CampaignScope(
        strategy_id=9,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=10000,
        roas_bid=None,
        config={},
        monitor_state={},
        smart_guard_state={},
    )
    rows = [
        {
            "creative_id": "shared-creative",
            "item_group_id": "product-1",
            "creative_delivery_status": "DELIVERING",
            "cost_cents": 100,
            "gross_revenue_cents": 200,
            "orders": 1,
            "product_impressions": 10,
            "product_clicks": 2,
        },
        {
            "creative_id": "shared-creative",
            "item_group_id": "product-2",
            "creative_delivery_status": "DELIVERING",
            "cost_cents": 900,
            "gross_revenue_cents": 1800,
            "orders": 9,
            "product_impressions": 90,
            "product_clicks": 18,
        },
    ]

    class _Result:
        def mappings(self):
            return self

        def all(self):
            return rows

    class _Db:
        statement = ""

        def execute(self, statement, _params):
            self.statement = str(statement)
            return _Result()

    db = _Db()
    monkeypatch.setattr(
        creative_guard,
        "_catalog_primary_item_group_id",
        lambda *_: "product-1",
    )
    monkeypatch.setattr(
        creative_guard,
        "_campaign_report_start_day",
        lambda *_: now.date(),
    )

    metrics = creative_guard._load_realtime_creatives(db, scope)

    assert [(item.item_group_id, item.cost_cents) for item in metrics] == [
        ("product-1", 100),
        ("product-2", 900),
    ]
    normalized = " ".join(db.statement.split())
    assert "from gmv_creative_10min_batch_manifests" in normalized
    assert (
        "group by workspace_id, auth_id, advertiser_id, store_id, "
        "campaign_id, stat_time_day"
    ) in normalized
    assert "latest.snapshot_at=m.snapshot_at" in normalized
    assert "latest.item_group_id=m.item_group_id" not in normalized
    assert "d.item_group_id=m.item_group_id" in normalized
    assert "group by daily.item_group_id, daily.creative_id" in normalized

    monkeypatch.setattr(creative_guard, "_load_realtime_creatives", lambda *_: metrics)
    monkeypatch.setattr(creative_guard, "_load_daily_creatives", lambda *_: metrics)
    merged = creative_guard._load_creatives(db, scope)
    assert {item.item_group_id for item in merged} == {"product-1", "product-2"}
