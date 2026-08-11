from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.data.models.website_ads import WebsiteAdsActionLog
from app.features.tenants.ttb.website_ads.schemas import TargetingConfig
from app.services.website_ads_delivery_optimizer import (
    _group_race_evidence,
    _is_creative_specific_rejection,
    _is_platform_rejected,
    _recent_race_exists,
)
from app.services.website_ads_hermes_planner import _guard_trigger_samples_satisfied
from app.services.website_ads_monitor import _click_quality_guard_evidence


DEFAULT_CONFIG = {
    "min_ctr": 0.04,
    "max_cpc": 0.30,
    "min_impressions_before_action": 100,
    "min_clicks_for_cpc": 3,
    "min_spend_before_action": 0.90,
    "min_video_2s_rate": 0.20,
    "min_video_6s_rate": 0.06,
    "min_video_impressions_before_action": 150,
    "min_video_spend_before_action": 0.75,
    "qualified_click_override_ctr": 0.04,
    "qualified_click_override_cpc": 0.30,
}


def test_group_race_cooldown_is_not_hidden_by_other_campaign_logs(
    db_session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_COOLDOWN_MINUTES",
        60,
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(
        WebsiteAdsActionLog(
            workspace_id=7,
            auth_id=11,
            actor_type="HERMES_GROUP_RACING",
            action="SCALE_WINNER_TARGETING",
            result="SUCCESS",
            request_json={"campaign_local_id": 42},
            created_at=now - timedelta(minutes=30),
        )
    )
    db_session.add_all(
        [
            WebsiteAdsActionLog(
                workspace_id=7,
                auth_id=11,
                actor_type="HERMES_GROUP_RACING",
                action="SCALE_WINNER_TARGETING",
                result="SUCCESS",
                request_json={"campaign_local_id": 1000 + index},
                created_at=now - timedelta(seconds=index),
            )
            for index in range(25)
        ]
    )
    db_session.commit()

    campaign = SimpleNamespace(id=42, workspace_id=7, auth_id=11)

    assert _recent_race_exists(db_session, campaign) is True


def evidence(
    *,
    spend: str,
    impressions: int,
    clicks: int,
    video_play_actions: int = 0,
    video_watched_2s: int = 0,
    video_watched_6s: int = 0,
) -> dict:
    return _click_quality_guard_evidence(
        spend=Decimal(spend),
        impressions=impressions,
        clicks=clicks,
        config=DEFAULT_CONFIG,
        emergency_spend_threshold=Decimal("5"),
        video_play_actions=video_play_actions,
        video_watched_2s=video_watched_2s,
        video_watched_6s=video_watched_6s,
    )


def test_guard_waits_for_a_real_sample() -> None:
    result = evidence(spend="0.60", impressions=80, clicks=1)

    assert result["triggered"] is False
    assert result["reasons"] == []


def test_guard_rejects_ctr_below_four_percent() -> None:
    result = evidence(spend="0.90", impressions=100, clicks=3)

    assert result["triggered"] is True
    assert "LOW_CTR" in result["reasons"]


def test_guard_rejects_cpc_above_thirty_cents() -> None:
    result = evidence(spend="1.20", impressions=100, clicks=4)

    assert result["triggered"] is False
    assert "HIGH_CPC" not in result["reasons"]

    result = evidence(spend="1.21", impressions=100, clicks=4)
    assert "HIGH_CPC" in result["reasons"]


def test_guard_keeps_qualified_click_traffic() -> None:
    result = evidence(spend="1.20", impressions=100, clicks=5)

    assert result["triggered"] is False
    assert result["ctr"] == Decimal("0.05")
    assert result["cpc"] == Decimal("0.24")


def test_guard_replaces_creative_when_both_retention_rates_are_low() -> None:
    result = evidence(
        spend="0.85",
        impressions=160,
        clicks=4,
        video_play_actions=160,
        video_watched_2s=14,
        video_watched_6s=2,
    )

    assert result["triggered"] is True
    assert "LOW_VIDEO_RETENTION" in result["reasons"]
    assert result["sample"]["video_ready"] is True
    assert result["sample"]["qualified_click_override"] is False


def test_guard_waits_for_video_sample_before_replacing() -> None:
    result = evidence(
        spend="1.50",
        impressions=80,
        clicks=4,
        video_play_actions=80,
        video_watched_2s=4,
        video_watched_6s=1,
    )

    assert "LOW_VIDEO_RETENTION" not in result["reasons"]
    assert result["sample"]["video_ready"] is False


def test_guard_keeps_low_retention_creative_with_qualified_clicks() -> None:
    result = evidence(
        spend="2.00",
        impressions=160,
        clicks=20,
        video_play_actions=160,
        video_watched_2s=16,
        video_watched_6s=2,
    )

    assert result["triggered"] is False
    assert result["sample"]["qualified_click_override"] is True


def test_guard_requires_both_retention_rates_to_fail() -> None:
    result = evidence(
        spend="1.00",
        impressions=160,
        clicks=4,
        video_play_actions=160,
        video_watched_2s=40,
        video_watched_6s=4,
    )

    assert "LOW_VIDEO_RETENTION" not in result["reasons"]


def test_high_cpc_review_requires_only_its_own_sample_gate() -> None:
    assert _guard_trigger_samples_satisfied(
        {
            "trigger_reasons": ["HIGH_CPC"],
            "sample": {"cpc_ready": True, "ctr_ready": False, "video_ready": False},
        }
    ) is True


def test_retention_review_requires_video_sample_and_no_click_override() -> None:
    assert _guard_trigger_samples_satisfied(
        {
            "trigger_reasons": ["LOW_VIDEO_RETENTION"],
            "sample": {"video_ready": True, "qualified_click_override": False},
        }
    ) is True
    assert _guard_trigger_samples_satisfied(
        {
            "trigger_reasons": ["LOW_VIDEO_RETENTION"],
            "sample": {"video_ready": True, "qualified_click_override": True},
        }
    ) is False


def test_targeting_requires_verified_audience_and_tiktok_only_placement() -> None:
    targeting = TargetingConfig(
        location_ids=["6252001"],
        interest_category_ids=["123"],
    )

    assert targeting.placement_type == "PLACEMENT_TYPE_NORMAL"
    assert targeting.placements == ["PLACEMENT_TIKTOK"]


def test_group_race_only_scales_a_clear_winner(monkeypatch) -> None:
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_WIN_CTR", 0.04)
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_WIN_MAX_CPC", 0.30)
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_LOSE_CTR", 0.03)
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_LOSE_CPC", 0.45)

    winner = _group_race_evidence(
        spend=Decimal("2.40"), impressions=300, clicks=12,
        min_spend=Decimal("2.40"), min_impressions=300, min_clicks=8,
    )
    loser = _group_race_evidence(
        spend=Decimal("4.80"), impressions=300, clicks=2,
        min_spend=Decimal("2.40"), min_impressions=300, min_clicks=8,
    )

    assert winner["sample_ready"] is True
    assert winner["winner"] is True
    assert winner["loser"] is False
    assert loser["sample_ready"] is True
    assert loser["winner"] is False
    assert loser["loser"] is True


def test_group_race_does_not_wait_for_spend_when_click_sample_is_conclusive(monkeypatch) -> None:
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_WIN_CTR", 0.04)
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_WIN_MAX_CPC", 0.30)
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_LOSE_CTR", 0.03)
    monkeypatch.setattr("app.services.website_ads_delivery_optimizer.settings.WEBSITE_ADS_GROUP_RACING_LOSE_CPC", 0.45)

    winner = _group_race_evidence(
        spend=Decimal("1.01"), impressions=100, clicks=8,
        min_spend=Decimal("2.40"), winner_min_impressions=100,
        min_impressions=300, min_clicks=8,
    )
    loser = _group_race_evidence(
        spend=Decimal("1.63"), impressions=466, clicks=10,
        min_spend=Decimal("2.40"), winner_min_impressions=100,
        min_impressions=300, min_clicks=8,
    )

    assert winner["sample_ready"] is True
    assert winner["sample"]["winner_ready"] is True
    assert winner["winner"] is True
    assert loser["sample_ready"] is True
    assert loser["sample"]["low_ctr_ready"] is True
    assert loser["loser_reasons"] == ["LOW_CTR"]
    assert loser["loser"] is True


def test_group_race_keeps_an_immature_low_ctr_group_observing() -> None:
    evidence = _group_race_evidence(
        spend=Decimal("0.70"), impressions=180, clicks=3,
        min_spend=Decimal("2.40"), winner_min_impressions=100,
        min_impressions=300, min_clicks=8,
    )

    assert evidence["sample_ready"] is False
    assert evidence["winner"] is False
    assert evidence["loser"] is False


def test_only_tiktok_platform_rejection_becomes_a_hard_exclusion() -> None:
    assert _is_platform_rejected({"secondary_status": "AD_STATUS_AUDIT"}) is False
    assert _is_platform_rejected({"secondary_status": "AD_STATUS_DELIVERY_OK"}) is False
    assert _is_platform_rejected({"secondary_status": "AD_STATUS_AUDIT_DENIED"}) is True
    assert _is_platform_rejected({"secondary_status": "AD_STATUS_REJECT"}) is True
    assert _is_creative_specific_rejection({"secondary_status": "AD_STATUS_REJECT"}) is False
    assert _is_creative_specific_rejection({"reject_reason": "VIDEO_CONTENT_REJECTED"}) is True
