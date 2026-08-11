import asyncio
import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.deps import SessionUser
from app.data.models.gmv_restructured import GmvStrategyConfig, PromotionTypeEnum
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.data.models.workspaces import Workspace
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max.control import (
    GMVMaxCampaignManualOverride,
)
from app.providers.tiktok_business.gmvmax_client import GMVMaxCampaignUpdateBody


class _DummyResponseData:
    def __init__(self, payload: dict, sessions: list | None = None) -> None:
        self._payload = dict(payload)
        self.list = sessions or []

    def model_dump(self, exclude_none: bool = True) -> dict:
        if not exclude_none:
            return dict(self._payload)
        return {k: v for k, v in self._payload.items() if v is not None}


def _response(payload: dict, *, sessions: list | None = None, request_id: str = "req-1"):
    return SimpleNamespace(request_id=request_id, data=_DummyResponseData(payload, sessions=sessions))


def test_accepted_campaign_update_is_applied_locally_without_info_readback(
    monkeypatch,
):
    observed_at = datetime(2026, 7, 17, 9, 30)
    campaign = SimpleNamespace(
        campaign_id="cmp-1",
        campaign_name="Old",
        budget_cents=1000,
        roas_bid=Decimal("2.0"),
        schedule_type="SCHEDULE_FROM_NOW",
        schedule_start_time_utc=None,
        schedule_end_time_utc=None,
        list_synced_at=None,
        detail_synced_at=None,
        modify_time_utc=None,
        updated_at=None,
        detail_raw_json={},
        list_raw_json={},
    )

    class _Db:
        def __init__(self):
            self.added = []
            self.flushes = 0

        def add(self, value):
            self.added.append(value)

        def flush(self):
            self.flushes += 1

    db = _Db()
    context = SimpleNamespace(
        db=db,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
    )
    monkeypatch.setattr(
        router_provider,
        "catalog_observation_now",
        lambda: observed_at,
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_advertiser_timezone_name",
        lambda *_args, **_kwargs: "Asia/Shanghai",
    )

    router_provider._apply_local_campaign_update_body(
        context,
        campaign=campaign,
        body=GMVMaxCampaignUpdateBody(
            campaign_id="cmp-1",
            campaign_name="New",
            budget=55.25,
            roas_bid=1.4,
            schedule_type="SCHEDULE_START_END",
            schedule_end_time="2026-07-19 08:00:00",
        ),
    )

    assert campaign.campaign_name == "New"
    assert campaign.budget_cents == 5525
    assert campaign.roas_bid == Decimal("1.4")
    assert campaign.list_synced_at == observed_at
    assert campaign.detail_synced_at == observed_at
    assert campaign.modify_time_utc == observed_at
    assert campaign.schedule_start_time_utc is None
    assert campaign.schedule_end_time_utc == datetime(2026, 7, 19, 0, 0)
    assert campaign.detail_raw_json["budget"] == 55.25
    assert db.added == [campaign]
    assert db.flushes == 1


def _setup_entities(db_session):
    workspace = Workspace(id=101, name="Tenant", company_code="0001")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=202,
        provider="tiktok-business",
        name="Provider",
        client_id="id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/cb",
    )
    db_session.add(provider_app)
    db_session.flush()

    account = OAuthAccountTTB(
        id=303,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.flush()

    campaign = TTBGmvMaxCampaign(
        id=404,
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        store_id="store-1",
        promotion_type=PromotionTypeEnum.PRODUCT,
        name="GMV",
        status="ACTIVE",
        daily_budget_cents=1000,
        roas_bid=Decimal("1.20"),
        currency="USD",
    )
    db_session.add(campaign)
    db_session.flush()
    return workspace, account, campaign


def _ensure_guard_event_table(db_session):
    db_session.execute(
        text(
            """
            create table if not exists gmv_campaign_guard_events (
                id integer primary key autoincrement,
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                campaign_id varchar(64) not null,
                strategy_id integer null,
                event_type varchar(64) not null,
                action varchar(32) not null,
                reason varchar(512) null,
                result varchar(32) not null,
                cost_cents integer null,
                gross_revenue_cents integer null,
                orders integer null,
                roi numeric null,
                request_json text null,
                response_json text null,
                error_message text null,
                created_at datetime not null
            )
            """
        )
    )


def _ensure_realtime_state_table(db_session):
    db_session.execute(
        text(
            """
            create table if not exists gmv_campaign_realtime_state (
                workspace_id integer not null,
                auth_id integer not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                campaign_id varchar(64) not null,
                operation_status varchar(32) null,
                secondary_status varchar(64) null,
                updated_at datetime null
            )
            """
        )
    )


def test_pause_override_commits_before_best_effort_refresh(monkeypatch, db_session):
    workspace, account, campaign = _setup_entities(db_session)
    strategy = GmvStrategyConfig(
        workspace_id=workspace.id,
        auth_id=account.id,
        campaign_id=campaign.campaign_id,
        enabled=True,
        config_json={"smart_guard": {"enabled": True}},
    )
    db_session.add(strategy)
    db_session.commit()
    actor = SessionUser(
        id=99,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=workspace.id,
        role="owner",
        is_active=True,
    )

    async def fake_status_update(request):  # noqa: ANN001
        assert request.operation_status == "DISABLE"
        return _response({"operation_status": "DISABLE"})

    async def failing_refresh(*_, **__):
        raise RuntimeError("campaign info temporarily unavailable")

    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        binding=router_provider.GMVMaxAccountBinding(
            account=account,
            advertiser_id=campaign.advertiser_id,
            store_id=campaign.store_id,
        ),
        client=SimpleNamespace(campaign_status_update=fake_status_update),
        db=db_session,
    )
    monkeypatch.setattr(
        router_provider,
        "_load_campaign_action_source",
        lambda *_: SimpleNamespace(
            campaign_id=campaign.campaign_id,
            advertiser_id=campaign.advertiser_id,
            store_id=campaign.store_id,
            operation_status="ENABLE",
            budget_cents=campaign.daily_budget_cents,
            roas_bid=campaign.roas_bid,
        ),
    )
    monkeypatch.setattr(
        router_provider,
        "_refresh_campaign_snapshot",
        failing_refresh,
    )

    response = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=workspace.id,
            provider="tiktok-business",
            auth_id=account.id,
            campaign_id=campaign.campaign_id,
            payload={"type": "pause", "disable_strategy": True},
            advertiser_id=None,
            me=actor,
            context=context,
        )
    )

    assert response.status == "success"
    db_session.expire_all()
    override = db_session.query(GMVMaxCampaignManualOverride).one()
    assert override.active is True
    assert override.override_type == "PAUSE"
    db_session.refresh(strategy)
    assert strategy.enabled is False
    assert strategy.config_json["smart_guard"]["enabled"] is False


def test_catalog_campaign_action_writes_guard_event(monkeypatch, db_session):
    workspace, account, _ = _setup_entities(db_session)
    _ensure_guard_event_table(db_session)
    _ensure_realtime_state_table(db_session)
    campaign = SimpleNamespace(
        campaign_id="catalog-cmp-1",
        advertiser_id="adv-1",
        store_id="store-1",
        operation_status="ENABLE",
        budget_cents=5000,
        roas_bid=Decimal("1.40"),
    )
    db_session.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                operation_status, secondary_status, updated_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                'ENABLE', 'CAMPAIGN_STATUS_ENABLE', current_timestamp
            )
            """
        ),
        {
            "workspace_id": workspace.id,
            "auth_id": account.id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    )
    actor = SessionUser(
        id=99,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=workspace.id,
        role="owner",
        is_active=True,
    )

    async def fake_status_update(request):  # noqa: ANN001
        campaign.operation_status = request.operation_status
        return _response({"operation_status": request.operation_status})

    async def fake_refresh(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    client = SimpleNamespace(campaign_status_update=fake_status_update)
    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        binding=SimpleNamespace(),
        client=client,
        db=db_session,
    )
    monkeypatch.setattr(router_provider, "_load_campaign_action_source", lambda *_: campaign)
    monkeypatch.setattr(router_provider, "_refresh_campaign_snapshot", fake_refresh)

    asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=workspace.id,
            provider="tiktok-business",
            auth_id=account.id,
            campaign_id=campaign.campaign_id,
            payload={"type": "pause"},
            advertiser_id=None,
            me=actor,
            context=context,
        )
    )

    row = db_session.execute(
        text(
            """
            select event_type, action, result, request_json, response_json
            from gmv_campaign_guard_events
            where campaign_id=:campaign_id
            """
        ),
        {"campaign_id": campaign.campaign_id},
    ).mappings().one()
    assert row["event_type"] == "MANUAL_ACTION"
    assert row["action"] == "PAUSE"
    assert row["result"] == "SUCCESS"
    assert json.loads(row["request_json"])["manual"]["performed_by"] == actor.email
    assert json.loads(row["response_json"])["after"]["status"] == "DISABLE"
    realtime_row = db_session.execute(
        text(
            """
            select operation_status, secondary_status
            from gmv_campaign_realtime_state
            where campaign_id=:campaign_id
            """
        ),
        {"campaign_id": campaign.campaign_id},
    ).mappings().one()
    assert realtime_row["operation_status"] == "DISABLE"
    assert realtime_row["secondary_status"] == "CAMPAIGN_STATUS_DISABLE"


def test_catalog_delete_marks_local_state_without_refresh(monkeypatch, db_session):
    workspace, account, _ = _setup_entities(db_session)
    _ensure_guard_event_table(db_session)
    campaign = SimpleNamespace(
        campaign_id="catalog-cmp-delete",
        advertiser_id="adv-1",
        store_id="store-1",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        detail_raw_json={"operation_status": "ENABLE"},
    )
    actor = SessionUser(
        id=99,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=workspace.id,
        role="owner",
        is_active=True,
    )

    async def fake_status_update(request):  # noqa: ANN001
        assert request.operation_status == "DELETE"
        return _response({"operation_status": request.operation_status})

    async def unexpected_refresh(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("deleted campaigns must not be refreshed through campaign/info")

    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        binding=SimpleNamespace(),
        client=SimpleNamespace(campaign_status_update=fake_status_update),
        db=db_session,
    )
    monkeypatch.setattr(router_provider, "_load_campaign_action_source", lambda *_: campaign)
    monkeypatch.setattr(router_provider, "_refresh_campaign_snapshot", unexpected_refresh)

    response = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=workspace.id,
            provider="tiktok-business",
            auth_id=account.id,
            campaign_id=campaign.campaign_id,
            payload={"type": "delete"},
            advertiser_id=None,
            me=actor,
            context=context,
        )
    )

    assert response.status == "success"
    assert campaign.operation_status == "DISABLE"
    assert campaign.secondary_status == "CAMPAIGN_STATUS_DELETE"
    assert campaign.detail_raw_json["secondary_status"] == "CAMPAIGN_STATUS_DELETE"
    row = db_session.execute(
        text(
            """
            select action, result, response_json
            from gmv_campaign_guard_events
            where campaign_id=:campaign_id
            """
        ),
        {"campaign_id": campaign.campaign_id},
    ).mappings().one()
    assert row["action"] == "DELETE"
    assert row["result"] == "SUCCESS"
    assert json.loads(row["response_json"])["after"]["secondary_status"] == "CAMPAIGN_STATUS_DELETE"


def test_delete_is_idempotent_when_local_campaign_is_already_deleted(monkeypatch, db_session):
    workspace, account, _ = _setup_entities(db_session)
    _ensure_guard_event_table(db_session)
    campaign = SimpleNamespace(
        campaign_id="catalog-cmp-already-deleted",
        advertiser_id="adv-1",
        store_id="store-1",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DELETE",
    )
    actor = SessionUser(
        id=99,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=workspace.id,
        role="owner",
        is_active=True,
    )

    async def unexpected_status_update(request):  # noqa: ANN001
        raise AssertionError("an already deleted campaign must not be deleted twice")

    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        binding=SimpleNamespace(),
        client=SimpleNamespace(campaign_status_update=unexpected_status_update),
        db=db_session,
    )
    monkeypatch.setattr(router_provider, "_load_campaign_action_source", lambda *_: campaign)

    response = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=workspace.id,
            provider="tiktok-business",
            auth_id=account.id,
            campaign_id=campaign.campaign_id,
            payload={"type": "delete"},
            advertiser_id=None,
            me=actor,
            context=context,
        )
    )

    assert response.status == "success"
    assert response.response == {"already_deleted": True}
    row = db_session.execute(
        text(
            """
            select action, result, reason
            from gmv_campaign_guard_events
            where campaign_id=:campaign_id
            """
        ),
        {"campaign_id": campaign.campaign_id},
    ).mappings().one()
    assert row["action"] == "DELETE"
    assert row["result"] == "SUCCESS"
    assert row["reason"] == "already_deleted"


def test_catalog_campaign_action_logs_read_guard_events(monkeypatch, db_session):
    workspace, account, _ = _setup_entities(db_session)
    _ensure_guard_event_table(db_session)
    campaign = SimpleNamespace(
        campaign_id="catalog-cmp-2",
        advertiser_id="adv-1",
        store_id="store-1",
        operation_status="ENABLE",
    )
    db_session.execute(
        text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, event_type, action, reason, result,
                cost_cents, gross_revenue_cents, orders, roi,
                request_json, response_json, error_message, created_at
            ) values (
                :workspace_id, :auth_id, 'adv-1', 'store-1', :campaign_id,
                1, 'CREATIVE_GUARD', 'REMOVE', 'creative_guard:roi_below_target', 'SUCCESS',
                847, 3064, 4, 3.6175,
                :request_json, :response_json, null, current_timestamp
            )
            """
        ),
        {
            "workspace_id": workspace.id,
            "auth_id": account.id,
            "campaign_id": campaign.campaign_id,
            "request_json": json.dumps(
                {"body": {"item_list": [{"item_id": "creative-1"}]}}
            ),
            "response_json": json.dumps({"code": 0}),
        },
    )
    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        binding=SimpleNamespace(),
        client=SimpleNamespace(),
        db=db_session,
    )
    monkeypatch.setattr(router_provider, "_load_campaign_action_source", lambda *_: campaign)

    response = asyncio.run(
        router_provider.list_gmvmax_action_logs_provider(
            workspace_id=workspace.id,
            provider="tiktok-business",
            auth_id=account.id,
            campaign_id=campaign.campaign_id,
            page=1,
            page_size=20,
            sort="-timestamp",
            context=context,
        )
    )

    assert response.total == 1
    assert response.page == 1
    assert response.page_size == 20
    assert response.entries[0]["action_type"] == "REMOVE"
    assert response.entries[0]["creative_id"] == "creative-1"
    assert response.entries[0]["before_value"]["orders"] == 4


def test_action_log_storage_error_is_a_5xx(monkeypatch, db_session):
    context = SimpleNamespace(
        workspace_id=101,
        auth_id=202,
        db=db_session,
    )
    monkeypatch.setattr(
        router_provider,
        "_load_campaign_action_source",
        lambda *_args, **_kwargs: SimpleNamespace(campaign_id="cmp-1"),
    )
    monkeypatch.setattr(
        router_provider,
        "_list_guard_action_logs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SQLAlchemyError("offline")),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_provider.list_gmvmax_action_logs_provider(
                workspace_id=101,
                provider="tiktok-business",
                auth_id=202,
                campaign_id="cmp-1",
                page=2,
                page_size=10,
                sort="-timestamp",
                context=context,
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "GMVMAX_ACTION_LOG_STORAGE_UNAVAILABLE"
