import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import (
    CreativeMetricsAggregate,
    upsert_creative_metrics,
)
from app.services.gmvmax_heating import HeatingEvaluationResult, run_creative_heating_cycle


class DummyClient:
    def __init__(self):
        self.requests = []

    async def gmv_max_campaign_action_apply(self, request):
        self.requests.append(request)
        return SimpleNamespace(data=SimpleNamespace(model_dump=lambda exclude_none=True: {"ok": True}))

    async def aclose(self):  # pragma: no cover - cleanup
        return None


async def _fake_sync(
    db,
    client,
    *,
    workspace_id,
    provider,
    auth_id,
    campaign,
    start_date,
    end_date,
):
    row = await upsert_creative_metrics(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        campaign_id=campaign.campaign_id,
        creative_id="creative-1",
        stat_time_day=datetime.now(timezone.utc),
        metrics={"clicks": 0, "ad_click_rate": 0.01, "gross_revenue": 10},
    )
    if getattr(row, "id", None) is None:
        row.id = 1
    return 1


def test_run_creative_heating_cycle_auto_stop(monkeypatch, db_session):
    campaign = TTBGmvMaxCampaign(
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv",
        campaign_id="cmp",
        store_id="store",
        name="Test",
    )
    campaign.id = 1
    db_session.add(campaign)
    db_session.flush()

    heating = TTBGmvMaxCreativeHeating(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        campaign_id="cmp",
        creative_id="creative-1",
        auto_stop_enabled=True,
        is_heating_active=True,
        min_clicks=5,
    )
    heating.id = 1
    db_session.add(heating)
    db_session.flush()

    dummy_client = DummyClient()
    monkeypatch.setattr(
        "app.services.gmvmax_heating.build_ttb_gmvmax_client",
        lambda db, auth_id: dummy_client,
    )
    monkeypatch.setattr(
        "app.services.gmvmax_heating._sync_creative_metrics_for_campaign",
        _fake_sync,
    )

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    summary = asyncio.run(run_creative_heating_cycle(db_session, now=now))

    assert summary["stopped"] == 1
    assert dummy_client.requests

    refreshed = db_session.get(TTBGmvMaxCreativeHeating, heating.id)
    assert refreshed is not None
    assert refreshed.is_heating_active is False
    assert refreshed.last_evaluation_result.startswith("auto_stopped")


def test_run_creative_heating_cycle_ready_to_heat_delivering(monkeypatch, db_session, caplog):
    campaign = TTBGmvMaxCampaign(
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv",
        campaign_id="cmp",
        store_id="store",
        name="Test",
    )
    campaign.id = 2
    db_session.add(campaign)
    db_session.flush()

    heating = TTBGmvMaxCreativeHeating(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        campaign_id="cmp",
        creative_id="creative-2",
        auto_stop_enabled=True,
        is_heating_active=True,
        min_clicks=0,
    )
    heating.id = 2
    db_session.add(heating)
    db_session.flush()

    dummy_client = DummyClient()
    monkeypatch.setattr(
        "app.services.gmvmax_heating.build_ttb_gmvmax_client",
        lambda db, auth_id: dummy_client,
    )
    monkeypatch.setattr(
        "app.services.gmvmax_heating._sync_creative_metrics_for_campaign",
        _fake_sync,
    )

    metrics_map = {
        "creative-2": CreativeMetricsAggregate(
            creative_id="creative-2",
            clicks=10,
            ad_click_rate=0.02,
            gross_revenue=100,
            cost=20,
            orders=5,
            roi=5.0,
            creative_status="DELIVERING",
        )
    }

    async def _fake_recent(*args, **kwargs):
        return metrics_map

    monkeypatch.setattr(
        "app.services.gmvmax_heating.get_recent_creative_metrics",
        _fake_recent,
    )

    monkeypatch.setattr(
        "app.services.gmvmax_heating.evaluate_heating_rule",
        lambda heating, metrics: HeatingEvaluationResult(
            result="ready_to_heat", should_stop=False, ready_to_heat=True
        ),
    )

    apply_calls: list[dict] = []

    async def _fake_apply(*args, **kwargs):  # noqa: ANN001
        apply_calls.append(kwargs)
        return SimpleNamespace(), SimpleNamespace(data=SimpleNamespace(model_dump=lambda exclude_none=True: {"ok": True}))

    monkeypatch.setattr(
        "app.services.gmvmax_heating.apply_boost_creative_action",
        _fake_apply,
    )

    caplog.set_level("DEBUG")
    now = datetime(2024, 1, 2, tzinfo=timezone.utc)
    summary = asyncio.run(run_creative_heating_cycle(db_session, now=now))

    assert summary["processed"] >= 1
    assert summary.get("boosted_creatives") == 1
    assert "creative ready to heat and delivering" in caplog.text

    refreshed = db_session.get(TTBGmvMaxCreativeHeating, heating.id)
    assert refreshed is not None
    assert refreshed.last_evaluation_result == "ready_to_heat"
    assert refreshed.is_heating_active is True
    assert apply_calls
