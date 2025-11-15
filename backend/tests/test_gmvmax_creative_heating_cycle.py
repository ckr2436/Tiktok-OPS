import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxCreativeHeating
from app.data.repositories.tiktok_business.gmvmax_creative_metrics import upsert_creative_metrics
from app.services.gmvmax_heating import run_creative_heating_cycle


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
