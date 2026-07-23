import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

from app.services.website_ads_conversion_guard import (
    _hourly_probe_evidence,
    _json_safe,
    _next_probe_at,
    derive_cross_channel_policy,
)
from app.services.website_ads_daily_report import (
    _derived_metrics,
    _has_delivery_activity,
    _parse_json_object,
)
from app.services.website_ads_hermes_planner import (
    _audience_size_summary,
    review_website_campaign_conversion_guard_action,
)
from app.services.website_ads_targeting_catalog import (
    catalog_path,
    rank_general_interest_categories,
)


def test_cross_channel_policy_uses_hourly_dynamic_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.settings.WEBSITE_ADS_CONVERSION_GUARD_DEFAULT_OBSERVATION_MINUTES",
        240,
    )
    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.settings.WEBSITE_ADS_CONVERSION_GUARD_MIN_OBSERVATION_MINUTES",
        180,
    )
    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.settings.WEBSITE_ADS_CONVERSION_GUARD_MAX_OBSERVATION_MINUTES",
        480,
    )
    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.settings.WEBSITE_ADS_CONVERSION_GUARD_PROBE_INTERVAL_MINUTES",
        60,
    )
    monkeypatch.setattr(
        "app.services.website_ads_conversion_guard.settings.WEBSITE_ADS_CONVERSION_GUARD_EARLY_RESUME_MINUTES",
        30,
    )
    hours = [datetime(2026, 7, 1, 8), datetime(2026, 7, 1, 12), datetime(2026, 7, 1, 18)]

    first = derive_cross_channel_policy(
        reference_price=Decimal("13"), event_hours=hours, pause_count=0
    )
    repeated = derive_cross_channel_policy(
        reference_price=Decimal("13"), event_hours=hours, pause_count=3
    )

    assert first["minimum_incremental_spend"] == 7.8
    assert first["minimum_incremental_clicks"] == 13
    assert 180 <= first["observation_minutes"] <= 480
    assert first["cooldown_minutes"] == 60
    assert repeated["cooldown_minutes"] == 60
    assert first["probe_target_clicks"] == 13
    assert first["probe_target_spend"] == 5.85
    assert repeated["probe_target_spend"] < first["probe_target_spend"]


def test_hourly_probe_waits_for_runtime_and_evidence() -> None:
    policy = {
        "probe_min_runtime_minutes": 15,
        "probe_max_runtime_minutes": 45,
        "probe_target_spend": 3.6,
        "probe_target_clicks": 8,
    }

    too_early = _hourly_probe_evidence(
        elapsed_minutes=10,
        incremental_spend=Decimal("4.00"),
        incremental_clicks=20,
        policy=policy,
    )
    ready = _hourly_probe_evidence(
        elapsed_minutes=15,
        incremental_spend=Decimal("3.60"),
        incremental_clicks=8,
        policy=policy,
    )
    timeout = _hourly_probe_evidence(
        elapsed_minutes=45,
        incremental_spend=Decimal("0.20"),
        incremental_clicks=1,
        policy=policy,
    )

    assert too_early["spend_cap_reached"] and too_early["should_pause"]
    assert ready["sample_ready"] and ready["should_pause"]
    assert timeout["timed_out"] and timeout["should_pause"]


def test_hourly_probe_never_spends_past_cap_while_waiting_for_clicks() -> None:
    policy = {
        "probe_min_runtime_minutes": 15,
        "probe_max_runtime_minutes": 45,
        "probe_target_spend": 3.6,
        "probe_target_clicks": 8,
    }

    evidence = _hourly_probe_evidence(
        elapsed_minutes=3,
        incremental_spend=Decimal("3.61"),
        incremental_clicks=1,
        policy=policy,
    )

    assert evidence["spend_cap_reached"]
    assert not evidence["click_sample_ready"]
    assert evidence["should_pause"]


def test_next_probe_is_anchored_to_hourly_start() -> None:
    started = datetime(2026, 7, 16, 4, 0)

    assert _next_probe_at(
        probe_started_at=started,
        now=started + timedelta(minutes=15),
        interval_minutes=60,
    ) == started + timedelta(minutes=60)
    assert _next_probe_at(
        probe_started_at=started,
        now=started + timedelta(minutes=70),
        interval_minutes=60,
    ) == started + timedelta(minutes=71)


def test_cross_channel_policy_shortens_observation_for_fast_failed_spend() -> None:
    policy = derive_cross_channel_policy(
        reference_price=Decimal("10"),
        event_hours=[],
        pause_count=0,
        incremental_spend=Decimal("15"),
        incremental_clicks=24,
    )

    assert policy["observation_minutes"] >= 180
    assert policy["observation_minutes"] < 240


def test_cross_channel_evidence_is_json_serializable() -> None:
    value = _json_safe(
        {
            "source": {"latest_hour": datetime(2026, 7, 15, 12)},
            "last_order_at": datetime(2026, 7, 15, 4),
            "spend": Decimal("12.34"),
            "event_hours": [datetime(2026, 7, 14, 11)],
        }
    )

    assert json.loads(json.dumps(value)) == {
        "source": {"latest_hour": "2026-07-15T12:00:00"},
        "last_order_at": "2026-07-15T04:00:00",
        "spend": 12.34,
        "event_hours": ["2026-07-14T11:00:00"],
    }


def test_audience_estimate_stage_is_normalized() -> None:
    result = _audience_size_summary(
        {
            "request_id": "req-1",
            "data": {
                "user_count_stage": 3,
                "user_count": {"lower_end": 125000, "upper_end": 180000},
            },
        }
    )

    assert result == {
        "stage": 3,
        "label": "BALANCED",
        "lower_end": 125000,
        "upper_end": 180000,
        "request_id": "req-1",
    }


def test_daily_report_uses_weighted_rates() -> None:
    metrics = _derived_metrics(
        spend=Decimal("15"),
        impressions=1000,
        clicks=50,
        conversions=Decimal("12"),
        conversion_value=Decimal("0"),
        video_watched_2s=300,
        video_watched_6s=100,
        video_views_p100=40,
    )

    assert metrics["ctr"] == 0.05
    assert metrics["cpc"] == 0.3
    assert metrics["video_2s_rate"] == 0.3
    assert metrics["video_6s_rate"] == 0.1
    assert metrics["video_completion_rate"] == 0.04


def test_daily_report_parses_fenced_hermes_json() -> None:
    assert _parse_json_object('```json\n{"summary":"ok","next_audience_tests":[]}\n```') == {
        "summary": "ok",
        "next_audience_tests": [],
    }


def test_daily_report_skips_model_when_delivery_and_actions_are_empty() -> None:
    assert not _has_delivery_activity(
        {"spend": 0, "impressions": 0, "clicks": 0, "view_content_events": 0},
        {"total": 0},
    )
    assert _has_delivery_activity({"spend": 0.01}, {"total": 0})
    assert _has_delivery_activity({}, {"total": 1})


def test_official_catalog_challenger_excludes_previously_tested_interests(tmp_path) -> None:
    path = catalog_path("adv-1", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "categories": [
                    {"id": "used", "name": "Body care", "targeting_type": "GENERAL_INTEREST", "level": 2},
                    {"id": "new-1", "name": "Personal care", "targeting_type": "GENERAL_INTEREST", "level": 2},
                    {"id": "new-2", "name": "Wellness lifestyle", "targeting_type": "GENERAL_INTEREST", "level": 3},
                ]
            }
        ),
        encoding="utf-8",
    )

    ranked = rank_general_interest_categories(
        "adv-1",
        ["personal care", "wellness"],
        exclude_ids={"used"},
        root=tmp_path,
    )

    assert {item["id"] for item in ranked} == {"new-1", "new-2"}


def test_official_catalog_does_not_match_car_or_medical_care_from_self_care(tmp_path) -> None:
    path = catalog_path("adv-2", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "categories": [
                    {"id": "car", "name": "Car", "targeting_type": "GENERAL_INTEREST", "level": 2},
                    {
                        "id": "medical",
                        "name": "Medical Care",
                        "targeting_type": "GENERAL_INTEREST",
                        "level": 2,
                    },
                    {
                        "id": "self",
                        "name": "Self Care",
                        "targeting_type": "GENERAL_INTEREST",
                        "level": 2,
                    },
                    {"id": "sleep", "name": "Sleep", "targeting_type": "GENERAL_INTEREST", "level": 2},
                ]
            }
        ),
        encoding="utf-8",
    )

    ranked = rank_general_interest_categories(
        "adv-2",
        ["self care", "sleep routine"],
        root=tmp_path,
    )

    assert [item["id"] for item in ranked] == ["self", "sleep"]


def test_official_catalog_rejects_appliances_and_generic_personal_overlap(tmp_path) -> None:
    path = catalog_path("adv-3", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "id": "appliance",
                        "name": "Personal Care Appliances",
                        "targeting_type": "GENERAL_INTEREST",
                        "level": 2,
                    },
                    {
                        "id": "beauty",
                        "name": "Beauty & Personal Care",
                        "targeting_type": "GENERAL_INTEREST",
                        "level": 2,
                    },
                    {
                        "id": "healthy",
                        "name": "Healthy Lifestyle",
                        "targeting_type": "GENERAL_INTEREST",
                        "level": 2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    sleep_ranked = rank_general_interest_categories(
        "adv-3",
        ["self care", "personal development", "healthy habits"],
        root=tmp_path,
    )
    body_care_ranked = rank_general_interest_categories(
        "adv-3",
        ["personal care", "body balm"],
        root=tmp_path,
    )

    assert [item["id"] for item in sleep_ranked] == ["healthy"]
    assert [item["id"] for item in body_care_ranked] == ["beauty"]


def _cross_channel_subjects() -> tuple[SimpleNamespace, SimpleNamespace, dict[str, object]]:
    campaign = SimpleNamespace(id=10, campaign_id="remote-10", name="Campaign 10")
    product = SimpleNamespace(
        id=20,
        product_id="shop-product-20",
        content_name="Product 20",
        title="Product 20",
        reference_price=Decimal("10"),
    )
    evidence = {
        "hard_gates": {
            "source_fresh": True,
            "observation_complete": True,
            "minimum_spend_met": True,
            "minimum_clicks_met": True,
            "no_new_order_pulse": True,
        }
    }
    return campaign, product, evidence


def test_campaign_wide_pause_fails_closed_when_realtime_hermes_is_unavailable(monkeypatch) -> None:
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "app.services.website_ads_hermes_planner.HermesAdsRealtimeClient.create_response",
        unavailable,
    )
    campaign, product, evidence = _cross_channel_subjects()

    result = asyncio.run(
        review_website_campaign_conversion_guard_action(
            campaign=campaign,
            product=product,
            evidence=evidence,
        )
    )

    assert result["decision"] == "HOLD"
    assert result["source"] == "HERMES_UNAVAILABLE_FAIL_CLOSED"


def test_campaign_wide_pause_respects_hermes_hold(monkeypatch) -> None:
    async def hold(*_args, **_kwargs):
        return ({"output_text": json.dumps({
            "decision": "HOLD",
            "confidence": "high",
            "reason": "Contradictory evidence requires another observation.",
            "risk_flags": ["contradictory_evidence"],
        })}, 42)

    monkeypatch.setattr(
        "app.services.website_ads_hermes_planner.HermesAdsRealtimeClient.create_response",
        hold,
    )
    campaign, product, evidence = _cross_channel_subjects()

    result = asyncio.run(
        review_website_campaign_conversion_guard_action(
            campaign=campaign,
            product=product,
            evidence=evidence,
        )
    )

    assert result["decision"] == "HOLD"
    assert result["source"] == "HERMES"
