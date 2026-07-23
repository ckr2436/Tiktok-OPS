from __future__ import annotations

import contextlib
import logging
from dataclasses import replace
from datetime import date, datetime

from sqlalchemy import func, select

from app.data.models.gmv_restructured import GmvCampaign, PromotionTypeEnum
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.gmvmax.services.sync_service import GmvMaxSyncService


def _next_id(db_session, model) -> int:
    value = db_session.execute(select(func.coalesce(func.max(model.id), 0))).scalar_one()
    return int(value) + 1


def _ensure_account(db_session) -> tuple[int, int]:
    workspace = db_session.query(Workspace).first()
    if workspace is None:
        workspace = Workspace(id=_next_id(db_session, Workspace), name="Demo", company_code="0001")
        db_session.add(workspace)
        db_session.flush()

    provider = db_session.query(OAuthProviderApp).first()
    if provider is None:
        provider = OAuthProviderApp(
            id=_next_id(db_session, OAuthProviderApp),
            provider="tiktok-business",
            name="Provider",
            client_id="client-id",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.com/callback",
        )
        db_session.add(provider)
        db_session.flush()

    account = db_session.query(OAuthAccountTTB).first()
    if account is None:
        account = OAuthAccountTTB(
            id=_next_id(db_session, OAuthAccountTTB),
            workspace_id=workspace.id,
            provider_app_id=provider.id,
            alias="Account",
            access_token_cipher=b"cipher",
            token_fingerprint=b"f" * 32,
        )
        db_session.add(account)
        db_session.flush()

    return workspace.id, account.id


def _create_campaign(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    store_id: str,
    lifecycle_status: str,
    is_deleted: bool,
    ext_updated_time: datetime,
) -> GmvCampaign:
    campaign = GmvCampaign(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        store_id=store_id,
        promotion_type=PromotionTypeEnum.PRODUCT,
        status=lifecycle_status,
        lifecycle_status=lifecycle_status,
        is_deleted=is_deleted,
        ext_updated_time=ext_updated_time,
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


def _build_strategy(workspace_id: int, auth_id: int | None = None) -> MonitoringStrategy:
    return MonitoringStrategy(
        id=1,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=None,
        store_id=None,
        level="CREATIVE_10MIN",
        interval_minutes=10,
        enabled=True,
        promotion_type=None,
        max_campaigns_per_run=None,
        category="GMVMAX",
        task_name="gmvmax.creative_10min",
    )


def test_select_active_campaigns_filters_by_lifecycle_status(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    active = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="active-1",
        store_id="store-1",
        lifecycle_status="ACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 3),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="inactive-1",
        store_id="store-1",
        lifecycle_status="INACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 2),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="deleted-1",
        store_id="store-1",
        lifecycle_status="DELETED",
        is_deleted=True,
        ext_updated_time=datetime(2024, 1, 1),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="corrupted-1",
        store_id="store-1",
        lifecycle_status="ACTIVE",
        is_deleted=True,
        ext_updated_time=datetime(2024, 1, 4),
    )

    strategy = _build_strategy(workspace_id, auth_id)
    service = GmvMaxSyncService()

    results = service._select_active_campaigns(db_session, strategy)

    assert [campaign.campaign_id for campaign in results] == [active.campaign_id]


def test_select_campaigns_by_status_isolates_same_store_by_advertiser(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    target = _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-target",
        campaign_id="target-campaign",
        store_id="shared-store",
        lifecycle_status="ACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 1),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-other",
        campaign_id="other-campaign",
        store_id="shared-store",
        lifecycle_status="ACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 2),
    )
    strategy = replace(
        _build_strategy(workspace_id, auth_id),
        advertiser_id="adv-target",
        store_id="shared-store",
        level="LIVESTREAM",
    )

    results = GmvMaxSyncService()._select_campaigns_by_status(
        db_session,
        strategy,
        lifecycle_statuses={"ACTIVE"},
    )

    assert [campaign.id for campaign in results] == [target.id]


def test_select_active_campaigns_has_no_hidden_thirty_campaign_limit(db_session):
    workspace_id, auth_id = _ensure_account(db_session)
    for index in range(35):
        _create_campaign(
            db_session,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-many",
            campaign_id=f"active-{index:02d}",
            store_id="store-many",
            lifecycle_status="ACTIVE",
            is_deleted=False,
            ext_updated_time=datetime(2024, 1, 1, 0, index),
        )

    strategy = _build_strategy(workspace_id, auth_id)
    service = GmvMaxSyncService()

    unbounded = service._select_active_campaigns(db_session, strategy)
    explicitly_limited = service._select_active_campaigns(
        db_session,
        replace(strategy, max_campaigns_per_run=7),
    )

    assert len(unbounded) == 35
    assert [campaign.campaign_id for campaign in explicitly_limited] == [
        f"active-{index:02d}" for index in range(34, 27, -1)
    ]


def test_sync_creative_logs_workspace_when_no_binding(db_session, caplog):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="inactive-2",
        store_id="store-1",
        lifecycle_status="INACTIVE",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 5),
    )

    strategy = _build_strategy(workspace_id, auth_id)
    service = GmvMaxSyncService(session_factory=lambda: contextlib.nullcontext(db_session))

    caplog.set_level(logging.INFO, logger="gmv.services.gmvmax.sync")
    service._sync_creative_10min(strategy, datetime(2024, 1, 6))

    assert any("no advertiser/store bound" in record.message for record in caplog.records)
    last = caplog.records[-1]
    assert getattr(last, "workspace_id", None) == workspace_id


def test_manual_sync_uses_historical_campaigns(db_session, monkeypatch, caplog):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="gmv-disabled",
        store_id="store-1",
        lifecycle_status="DISABLED",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 5),
    )

    service = GmvMaxSyncService(session_factory=lambda: contextlib.nullcontext(db_session))
    monkeypatch.setattr(
        service,
        "_sync_overview_metrics",
        lambda *_, **__: {"synced_rows": 0},
    )

    class _FakeClient:
        async def aclose(self) -> None:  # pragma: no cover - trivial
            return None

    monkeypatch.setattr(
        "app.gmvmax.services.sync_service.build_ttb_gmvmax_client", lambda *_, **__: _FakeClient()
    )

    async def _fake_fetch_overview_summary_rows(*_, **__):
        return []

    monkeypatch.setattr(
        "app.gmvmax.services.sync_service.fetch_overview_summary_rows",
        _fake_fetch_overview_summary_rows,
    )

    caplog.set_level(logging.INFO, logger="gmv.services.gmvmax.sync")
    results = service.sync_levels_for_account(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
        levels=["OVERVIEW"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        require_active_campaigns=False,
    )

    assert results["OVERVIEW"]["synced_rows"] == 1
    assert not any("no active campaigns" in record.message for record in caplog.records)


def test_manual_campaign_selection_keeps_history_when_active_campaign_exists(
    db_session,
):
    workspace_id, auth_id = _ensure_account(db_session)
    for campaign_id, lifecycle_status in (
        ("gmv-active", "ACTIVE"),
        ("gmv-disabled", "DISABLED"),
        ("gmv-ended", "ENDED"),
    ):
        _create_campaign(
            db_session,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id="adv-1",
            campaign_id=campaign_id,
            store_id="store-1",
            lifecycle_status=lifecycle_status,
            is_deleted=False,
            ext_updated_time=datetime(2024, 1, 5),
        )

    service = GmvMaxSyncService(
        session_factory=lambda: contextlib.nullcontext(db_session)
    )
    strategy = MonitoringStrategy(
        id=0,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
        level="LIVESTREAM_DAILY",
        category="GMVMAX",
        task_name="gmvmax.manual_sync",
        interval_minutes=0,
        enabled=True,
        promotion_type=None,
        max_campaigns_per_run=None,
        params_json={},
        input_schema_json=None,
    )

    selected = service._select_campaigns_for_sync(
        db_session,
        strategy,
        require_active_campaigns=False,
    )

    assert {row.campaign_id for row in selected} == {
        "gmv-active",
        "gmv-disabled",
        "gmv-ended",
    }


def test_overview_sync_does_not_depend_on_campaign_lifecycle(
    db_session, monkeypatch, caplog
):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="gmv-ended",
        store_id="store-1",
        lifecycle_status="ENDED",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 4),
    )

    service = GmvMaxSyncService(session_factory=lambda: contextlib.nullcontext(db_session))
    monkeypatch.setattr(
        service,
        "_sync_overview_metrics",
        lambda *_, **__: {"synced_rows": 0},
    )

    class _FakeClient:
        async def aclose(self) -> None:  # pragma: no cover - trivial
            return None

    monkeypatch.setattr(
        "app.gmvmax.services.sync_service.build_ttb_gmvmax_client",
        lambda *_, **__: _FakeClient(),
    )

    async def _fake_fetch_overview_summary_rows(*_, **__):
        return []

    monkeypatch.setattr(
        "app.gmvmax.services.sync_service.fetch_overview_summary_rows",
        _fake_fetch_overview_summary_rows,
    )

    caplog.set_level(logging.INFO, logger="gmv.services.gmvmax.sync")
    results = service.sync_levels_for_account(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
        levels=["OVERVIEW"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
    )

    assert results["OVERVIEW"]["synced_rows"] == 1
    assert not any("no active campaigns" in record.message for record in caplog.records)


def test_manual_sync_filters_by_store_binding(db_session, monkeypatch):
    workspace_id, auth_id = _ensure_account(db_session)
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="gmv-store-a",
        store_id="store-a",
        lifecycle_status="DISABLED",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 6),
    )
    _create_campaign(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        campaign_id="gmv-store-b",
        store_id="store-b",
        lifecycle_status="DISABLED",
        is_deleted=False,
        ext_updated_time=datetime(2024, 1, 6),
    )

    observed_store_ids: list[str] = []

    async def _fake_fetch_overview_summary_rows(*_, store_id: str, **__):
        observed_store_ids.append(store_id)
        return []

    monkeypatch.setattr(
        "app.gmvmax.services.sync_service.fetch_overview_summary_rows",
        _fake_fetch_overview_summary_rows,
    )

    service = GmvMaxSyncService(session_factory=lambda: contextlib.nullcontext(db_session))
    monkeypatch.setattr(
        service,
        "_sync_overview_metrics",
        lambda *_, **__: {"synced_rows": 0},
    )

    class _FakeClient:
        async def aclose(self) -> None:  # pragma: no cover - trivial
            return None

    monkeypatch.setattr(
        "app.gmvmax.services.sync_service.build_ttb_gmvmax_client", lambda *_, **__: _FakeClient()
    )
    results = service.sync_levels_for_account(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-a",
        levels=["OVERVIEW"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        require_active_campaigns=False,
    )

    assert results["OVERVIEW"]["synced_rows"] == 1
    assert observed_store_ids == ["store-a"]


def test_account_level_sync_runs_daily_and_hourly_for_each_base_level(
    monkeypatch,
):
    service = GmvMaxSyncService()
    observed: dict[str, list[str]] = {
        "OVERVIEW": [],
        "CAMPAIGN": [],
        "PRODUCT": [],
        "LIVESTREAM": [],
        "DURATION": [],
    }
    observed_campaign_filters: list[list[str] | None] = []

    def _recorder(level: str):
        def _sync(*_, granularity: str, **__):
            observed[level].append(granularity)
            if level == "CAMPAIGN":
                observed_campaign_filters.append(__.get("campaign_ids"))
            return {"synced_rows": 1}

        return _sync

    monkeypatch.setattr(
        service,
        "_sync_overview_manual",
        lambda *_, **__: {"synced_rows": 1},
    )
    monkeypatch.setattr(
        service,
        "_sync_overview_metrics",
        _recorder("OVERVIEW"),
    )
    monkeypatch.setattr(
        service,
        "_sync_campaign_metrics",
        _recorder("CAMPAIGN"),
    )
    monkeypatch.setattr(
        service,
        "_sync_product_metrics",
        _recorder("PRODUCT"),
    )
    monkeypatch.setattr(
        service,
        "_sync_livestream_metrics",
        _recorder("LIVESTREAM"),
    )
    monkeypatch.setattr(
        service,
        "_sync_duration_metrics",
        _recorder("DURATION"),
    )

    results = service.sync_levels_for_account(
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        levels=[
            "OVERVIEW",
            "CAMPAIGN",
            "PRODUCT",
            "LIVESTREAM",
            "DURATION",
        ],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
        campaign_ids=["campaign-1"],
    )

    assert all(value == ["DAY", "HOUR"] for value in observed.values())
    assert observed_campaign_filters == [["campaign-1"], ["campaign-1"]]
    assert results["OVERVIEW"] == {
        "synced_rows": 3,
        "daily_rows": 1,
        "hourly_rows": 1,
        "snapshot_rows": 1,
    }
    for level in ("CAMPAIGN", "PRODUCT", "LIVESTREAM", "DURATION"):
        assert results[level] == {
            "synced_rows": 2,
            "daily_rows": 1,
            "hourly_rows": 1,
        }


def test_account_creative_report_sync_does_not_refresh_video_inventory_by_default(
    monkeypatch,
):
    service = GmvMaxSyncService()
    observed: list[bool] = []

    def _creative(*_args, **kwargs):
        observed.append(bool(kwargs.get("refresh_creative_assets")))
        return {"synced_rows": 1}

    monkeypatch.setattr(service, "_sync_creative_10min", _creative)

    result = service.sync_levels_for_account(
        workspace_id=3,
        auth_id=3,
        advertiser_id="advertiser-1",
        store_id="store-1",
        levels=["CREATIVE"],
        start_date=date(2026, 7, 21),
        end_date=date(2026, 7, 21),
        campaign_ids=["campaign-1"],
    )

    assert result["CREATIVE"] == {"synced_rows": 1}
    assert observed == [False]
