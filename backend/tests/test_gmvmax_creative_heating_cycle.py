from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from app.data.models.gmv_restructured import (
    GmvCreativeMetrics10Min,
    PromotionTypeEnum,
)
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBBindingConfig, TTBAdvertiser
from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating
from app.data.models.workspaces import Workspace
from app.services.gmvmax_heating import run_creative_heating_cycle


class DummyClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _setup_scope(db_session, *, include_binding: bool = True) -> None:
    db_session.add(Workspace(id=1, name="Tenant", company_code="0001"))
    db_session.add(
        OAuthProviderApp(
            id=1,
            provider="tiktok-business",
            name="Provider",
            client_id="client",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.com/callback",
        )
    )
    db_session.add(
        OAuthAccountTTB(
            id=1,
            workspace_id=1,
            provider_app_id=1,
            alias="Account",
            access_token_cipher=b"cipher",
            token_fingerprint=b"f" * 32,
        )
    )
    db_session.flush()
    db_session.add(
        TTBAdvertiser(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            timezone="UTC",
            display_timezone="UTC",
        )
    )
    if include_binding:
        db_session.add(
            TTBBindingConfig(
                workspace_id=1,
                auth_id=1,
                advertiser_id="adv",
                store_id="store",
            )
        )
    db_session.add(
        GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv",
            store_id="store",
            campaign_id="cmp",
            campaign_name="Canonical",
            operation_status="ENABLE",
            shopping_ads_type="PRODUCT",
            budget_cents=5000,
        )
    )
    db_session.flush()


def _add_heating(db_session, *, min_clicks: int) -> TTBGmvMaxCreativeHeating:
    heating = TTBGmvMaxCreativeHeating(
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv",
        campaign_id="cmp",
        creative_id="creative-1",
        item_group_id="spu-1",
        product_id="spu-1",
        item_id="creative-1",
        promotion_type=PromotionTypeEnum.PRODUCT,
        auto_stop_enabled=True,
        is_heating_active=True,
        evaluation_window_minutes=60,
        min_clicks=min_clicks,
        last_action_response={"session_id": "session-1"},
    )
    db_session.add(heating)
    db_session.flush()
    return heating


def _add_snapshots(db_session, *, latest_clicks: int) -> None:
    common = {
        "workspace_id": 1,
        "auth_id": 1,
        "advertiser_id": "adv",
        "store_id": "store",
        "campaign_id": "cmp",
        "item_group_id": "spu-1",
        "creative_id": "creative-1",
        "stat_time_day": date(2026, 7, 17),
        "creative_status": "DELIVERING",
        "source_observed_at": datetime(2026, 7, 17, 15, 50),
        "ingested_at": datetime(2026, 7, 17, 15, 50),
        "is_final": False,
    }
    db_session.add_all(
        [
            GmvCreativeMetrics10Min(
                **common,
                snapshot_at=datetime(2026, 7, 17, 14, 50),
                clicks=10,
                impressions=100,
                cost_cents=1000,
                gross_revenue_cents=2000,
                orders=1,
            ),
            GmvCreativeMetrics10Min(
                **common,
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                clicks=latest_clicks,
                impressions=200,
                cost_cents=2000,
                gross_revenue_cents=4000,
                orders=2,
            ),
            # Same creative ID under another product is intentionally noisy.
            GmvCreativeMetrics10Min(
                **{
                    **common,
                    "item_group_id": "spu-other",
                },
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                clicks=999,
                impressions=999,
                cost_cents=99999,
                gross_revenue_cents=0,
                orders=0,
            ),
        ]
    )
    db_session.add_all(
        [
            GmvCreative10MinBatchManifest(
                workspace_id=1,
                auth_id=1,
                advertiser_id="adv",
                store_id="store",
                campaign_id="cmp",
                stat_time_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 14, 50),
                complete=True,
                row_count=1,
                source_observed_at=datetime(2026, 7, 17, 14, 50),
            ),
            GmvCreative10MinBatchManifest(
                workspace_id=1,
                auth_id=1,
                advertiser_id="adv",
                store_id="store",
                campaign_id="cmp",
                stat_time_day=date(2026, 7, 17),
                snapshot_at=datetime(2026, 7, 17, 15, 50),
                complete=True,
                row_count=2,
                source_observed_at=datetime(2026, 7, 17, 15, 50),
            ),
        ]
    )
    db_session.flush()


def test_cycle_reads_canonical_scope_and_auto_stops_without_report_sync(
    monkeypatch,
    db_session,
):
    _setup_scope(db_session)
    heating = _add_heating(db_session, min_clicks=5)
    _add_snapshots(db_session, latest_clicks=11)
    dummy_client = DummyClient()
    monkeypatch.setattr(
        "app.services.gmvmax_heating.build_ttb_gmvmax_client",
        lambda db, auth_id: dummy_client,
    )

    stop_calls: list[dict] = []

    async def fake_stop(*args, **kwargs):  # noqa: ANN001
        stop_calls.append(kwargs)
        return heating, object()

    monkeypatch.setattr(
        "app.services.gmvmax_heating.stop_boost_creative_session",
        fake_stop,
    )

    summary = asyncio.run(
        run_creative_heating_cycle(
            db_session,
            now=datetime(2026, 7, 17, 16, tzinfo=timezone.utc),
        )
    )

    assert summary["stopped"] == 1
    assert len(stop_calls) == 1
    assert stop_calls[0]["campaign"].store_id == "store"
    assert stop_calls[0]["campaign"].advertiser_id == "adv"
    assert dummy_client.closed is True
    assert heating.is_heating_active is False
    assert heating.last_evaluation_result == "auto_stopped_low_clicks"


def test_cycle_never_recreates_session_for_an_already_active_heating_config(
    monkeypatch,
    db_session,
):
    _setup_scope(db_session)
    heating = _add_heating(db_session, min_clicks=0)
    _add_snapshots(db_session, latest_clicks=25)

    def fail_client(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("ready evaluation must not create an official client")

    monkeypatch.setattr(
        "app.services.gmvmax_heating.build_ttb_gmvmax_client",
        fail_client,
    )
    summary = asyncio.run(
        run_creative_heating_cycle(
            db_session,
            now=datetime(2026, 7, 17, 16, tzinfo=timezone.utc),
        )
    )

    assert summary == {"processed": 1, "stopped": 0, "campaigns": 1}
    assert heating.is_heating_active is True
    assert heating.last_evaluation_result == "ready_to_heat"


def test_cycle_fails_closed_when_current_binding_does_not_own_catalog_scope(
    db_session,
):
    _setup_scope(db_session, include_binding=False)
    heating = _add_heating(db_session, min_clicks=5)

    summary = asyncio.run(
        run_creative_heating_cycle(
            db_session,
            now=datetime(2026, 7, 17, 16, tzinfo=timezone.utc),
        )
    )

    assert summary["stopped"] == 0
    assert heating.is_heating_active is True
    assert heating.last_evaluation_result == "campaign_scope_missing"
