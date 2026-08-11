from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.deps import SessionUser
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.data.models.gmvmax_creative_metrics import (
    GmvmaxProductCreativeMetricsDaily,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBBindingConfig
from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating
from app.data.models.workspaces import Workspace
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.gmv_max.control import set_manual_pause_override
from app.features.tenants.ttb.gmv_max.schemas import CreativeHeatingActionRequest
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxResponse,
    GMVMaxSessionMutationData,
)
from app.services import gmvmax_heating_actions
from app.services.gmvmax_heating_actions import (
    CreativeHeatingActionBlocked,
    apply_boost_creative_action,
    stop_boost_creative_session,
)


class DummySession:
    def __init__(self) -> None:
        self.flush_calls = 0

    def flush(self) -> None:  # pragma: no cover - trivial
        self.flush_calls += 1


def test_creative_boost_budget_is_an_explicit_independent_daily_budget():
    campaign = SimpleNamespace(budget_cents=20000, daily_budget_cents=20000)

    assert (
        gmvmax_heating_actions._resolve_boost_budget(
            campaign,
            target_daily_budget=25,
            budget_delta=1,
        )
        == 25
    )
    with pytest.raises(ValueError, match="target_daily_budget is required"):
        gmvmax_heating_actions._resolve_boost_budget(
            campaign,
            target_daily_budget=None,
            budget_delta=1,
        )


def test_creative_boost_request_rejects_delta_only_and_subminimum_budget():
    base = {
        "action_type": "BOOST_CREATIVE",
        "creative_id": "creative-1",
        "mode": "MANUAL",
        "product_id": "product-1",
        "item_id": "creative-1",
    }

    with pytest.raises(ValueError, match="target_daily_budget is required"):
        CreativeHeatingActionRequest.model_validate({**base, "budget_delta": 10})
    with pytest.raises(ValueError):
        CreativeHeatingActionRequest.model_validate({**base, "target_daily_budget": 9.99})


class _FakeMutation:
    def assert_current(self, _db) -> None:
        return None

    def commit(self, db) -> None:
        flush = getattr(db, "flush", None)
        if callable(flush):
            flush()


@contextmanager
def _fake_manual_mutation(*_args, **_kwargs):
    yield _FakeMutation()


def test_apply_boost_creative_action_helper(monkeypatch):
    db = DummySession()
    workspace_id = 10
    auth_id = 20
    campaign = SimpleNamespace(
        id=111,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id="cmp-10",
        advertiser_id="adv-10",
        store_id="store-10",
        operation_status="ENABLE",
        budget_cents=5000,
    )
    heating = SimpleNamespace(
        id=211,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-10",
        campaign_id="cmp-10",
        creative_id="cr-10",
        item_group_id="spu-10",
        product_id="spu-10",
        item_id="cr-10",
        promotion_type="PRODUCT",
        mode="BOOST",
        target_daily_budget=50.0,
        budget_delta=None,
        currency="USD",
        max_duration_minutes=180,
        note="auto",
    )

    update_calls: list[dict[str, Any]] = []
    audit_calls: list[dict[str, Any]] = []

    async def fake_update(*_, **kwargs):  # noqa: ANN001
        update_calls.append(kwargs)
        return heating

    def fake_audit(*args, **kwargs):  # noqa: ANN001
        audit_calls.append(kwargs)
        return None

    @contextmanager
    def fake_fence(*args, **kwargs):  # noqa: ANN001
        yield 7

    client_calls: list[Any] = []

    class DummyClient:
        async def gmv_max_session_create(self, request):  # noqa: ANN001
            client_calls.append(request)
            data = GMVMaxSessionMutationData.model_validate({"result": "ok"})
            return GMVMaxResponse(
                code=0, message="ok", request_id="req-1", data=data
            )

    monkeypatch.setattr(
        gmvmax_heating_actions, "update_heating_action_result", fake_update
    )
    monkeypatch.setattr(gmvmax_heating_actions, "_record_heating_audit", fake_audit)
    monkeypatch.setattr(
        gmvmax_heating_actions,
        "_heating_mutation_fence",
        fake_fence,
    )
    monkeypatch.setattr(
        gmvmax_heating_actions,
        "_validate_mutation_scope",
        lambda *args, **kwargs: campaign,
    )

    updated_row, response = asyncio.run(
        apply_boost_creative_action(
            db,
            client=DummyClient(),
            campaign=campaign,
            heating=heating,
            mode=heating.mode,
            target_daily_budget=heating.target_daily_budget,
            currency=heating.currency,
            max_duration_minutes=heating.max_duration_minutes,
            note=heating.note,
            performed_by="tester",
        )
    )

    assert updated_row is heating
    assert response.data.model_dump(exclude_none=True) == {"result": "ok"}
    assert update_calls and update_calls[0]["action_type"] == "APPLY_BOOST"
    assert client_calls and client_calls[0].body.campaign_id == "cmp-10"
    assert client_calls[0].body.store_id == "store-10"
    serialized_body = client_calls[0].body.model_dump(exclude_none=True)
    assert "product_list" not in serialized_body
    assert serialized_body["session"]["product_list"] == [{"spu_id": "spu-10"}]
    assert serialized_body["session"]["bid_type"] == "CREATIVE_NO_BID"
    assert audit_calls and audit_calls[0]["performed_by"] == "tester"
    assert audit_calls[0]["fencing_token"] == 7
    assert db.flush_calls >= 1


def test_apply_boost_creative_action_route(monkeypatch):
    workspace_id = 1
    provider = "tiktok-business"
    auth_id = 2
    campaign_id = "cmp-10"

    upsert_calls: list[dict[str, Any]] = []
    apply_calls: list[dict[str, Any]] = []

    async def fake_upsert(*_, **kwargs):  # noqa: ANN001
        upsert_calls.append(kwargs)
        return SimpleNamespace(
            id=11,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id,
            creative_id=kwargs["creative_id"],
            product_id=kwargs.get("product_id"),
            item_id=kwargs.get("item_id"),
            mode=kwargs.get("mode"),
            target_daily_budget=kwargs.get("target_daily_budget"),
            budget_delta=kwargs.get("budget_delta"),
            currency=kwargs.get("currency"),
            max_duration_minutes=kwargs.get("max_duration_minutes"),
            note=kwargs.get("note"),
        )

    async def fake_apply_boost(*_, **kwargs):  # noqa: ANN001
        apply_calls.append(kwargs)
        data = GMVMaxSessionMutationData.model_validate({"result": "ok"})
        response = GMVMaxResponse(code=0, message="ok", request_id="req-1", data=data)
        return kwargs["heating"], response

    monkeypatch.setattr(router_provider, "upsert_creative_heating", fake_upsert)
    monkeypatch.setattr(
        router_provider,
        "_load_campaign_action_source",
        lambda *_: SimpleNamespace(
            id=99,
            campaign_id=campaign_id,
            advertiser_id="adv-1",
            store_id="store-1",
            promotion_type="PRODUCT",
        ),
    )
    monkeypatch.setattr(router_provider, "apply_boost_creative_action", fake_apply_boost)
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        _fake_manual_mutation,
    )

    binding = router_provider.GMVMaxAccountBinding(
        account=SimpleNamespace(), advertiser_id="adv-1", store_id="store-1"
    )
    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=binding,
        client=SimpleNamespace(),
        db=DummySession(),
    )

    me = SessionUser(
        id=123,
        email="tester@example.com",
        username="tester",
        display_name="Tester",
        usercode=None,
        is_platform_admin=False,
        workspace_id=workspace_id,
        role="owner",
        is_active=True,
    )

    payload = {
        "action_type": "BOOST_CREATIVE",
        "creative_id": "cr-55",
        "mode": "BOOST",
        "target_daily_budget": 45.0,
        "currency": "USD",
        "max_duration_minutes": 180,
        "note": "Boost this creative",
        "product_id": "spu-55",
        "item_id": "cr-55",
    }

    response = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id,
            payload=payload,
            advertiser_id=None,
            me=me,
            context=context,
        )
    )

    assert isinstance(response, router_provider.CreativeHeatingActionResponse)
    assert response.action_type == "BOOST_CREATIVE"
    assert response.heating.creative_id == "cr-55"
    assert response.heating.product_id == "spu-55"
    assert response.heating.item_id == "cr-55"
    assert response.tiktok_response == {"result": "ok"}

    assert upsert_calls
    assert upsert_calls[0]["creative_id"] == "cr-55"
    assert apply_calls and apply_calls[0]["performed_by"] == "tester@example.com"
    assert apply_calls[0]["note"] == "Boost this creative"
    assert context.db.flush_calls >= 1


def test_legacy_creative_action_aliases_normalize_to_canonical_contract(monkeypatch):
    campaign = SimpleNamespace(
        id=99,
        campaign_id="cmp-legacy",
        advertiser_id="adv-1",
        store_id="store-1",
        promotion_type="PRODUCT",
        budget_cents=5500,
    )
    monkeypatch.setattr(
        router_provider,
        "_load_campaign_action_source",
        lambda *_: campaign,
    )
    monkeypatch.setattr(
        router_provider,
        "_resolve_creative_item_group_id",
        lambda *_, **__: "spu-resolved",
    )

    captured: list[Any] = []

    async def fake_apply(*_, request, **__):  # noqa: ANN001
        captured.append(request)
        return request

    monkeypatch.setattr(
        router_provider,
        "_apply_creative_heating_action",
        fake_apply,
    )
    monkeypatch.setattr(
        router_provider,
        "_manual_guard_mutation_lease",
        _fake_manual_mutation,
    )
    context = router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=router_provider.GMVMaxAccountBinding(
            account=SimpleNamespace(),
            advertiser_id="adv-1",
            store_id="store-1",
        ),
        client=SimpleNamespace(),
        db=DummySession(),
    )
    me = SessionUser(
        id=123,
        email="tester@example.com",
        username="tester",
        display_name="Tester",
        usercode=None,
        is_platform_admin=False,
        workspace_id=1,
        role="owner",
        is_active=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_provider.apply_gmvmax_campaign_action_provider(
                workspace_id=1,
                provider="tiktok-business",
                auth_id=2,
                campaign_id="cmp-legacy",
                payload={"type": "boost", "creative_id": "creative-1"},
                advertiser_id=None,
                me=me,
                context=context,
            )
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "GMVMAX_CREATIVE_HEATING_INVALID"

    started = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            campaign_id="cmp-legacy",
            payload={
                "type": "boost",
                "creative_id": "creative-1",
                "target_daily_budget": 55,
            },
            advertiser_id=None,
            me=me,
            context=context,
        )
    )
    stopped = asyncio.run(
        router_provider.apply_gmvmax_campaign_action_provider(
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            campaign_id="cmp-legacy",
            payload={"type": "stop_heat", "creative_id": "creative-1"},
            advertiser_id=None,
            me=me,
            context=context,
        )
    )

    assert started.action_type == "BOOST_CREATIVE"
    assert started.target_daily_budget == 55.0
    assert started.product_id == "spu-resolved"
    assert stopped.action_type == "BOOST_CREATIVE"
    assert stopped.mode == "STOP"
    assert stopped.target_daily_budget is None
    assert [request.action_type for request in captured] == [
        "BOOST_CREATIVE",
        "BOOST_CREATIVE",
    ]


def _ensure_guard_event_table(db_session) -> None:
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
    db_session.execute(text("delete from gmv_campaign_guard_events"))


def _setup_real_scope(
    db_session,
    *,
    operation_status: str = "ENABLE",
    heating_item_group_id: str = "spu-1",
) -> tuple[GmvmaxProductCampaignCatalog, TTBGmvMaxCreativeHeating]:
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
        TTBBindingConfig(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv-1",
            store_id="store-1",
        )
    )
    campaign = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="cmp-1",
        campaign_name="Canonical",
        operation_status=operation_status,
        shopping_ads_type="PRODUCT",
        budget_cents=5000,
    )
    db_session.add(campaign)
    db_session.add(
        GmvmaxProductCreativeMetricsDaily(
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="cmp-1",
            item_group_id="spu-1",
            creative_id="creative-1",
            stat_time_day=date(2026, 7, 16),
            source_observed_at=datetime(2026, 7, 17, 1),
            ingested_at=datetime(2026, 7, 17, 1),
            creative_delivery_status="DELIVERING",
            is_final=False,
        )
    )
    heating = TTBGmvMaxCreativeHeating(
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        creative_id="creative-1",
        item_group_id=heating_item_group_id,
        product_id=heating_item_group_id,
        item_id="creative-1",
        promotion_type="PRODUCT",
        target_daily_budget=50,
        auto_stop_enabled=True,
        is_heating_active=False,
    )
    db_session.add(heating)
    _ensure_guard_event_table(db_session)
    db_session.commit()
    return campaign, heating


class _OfficialClient:
    def __init__(self) -> None:
        self.create_requests: list[Any] = []
        self.delete_requests: list[dict[str, str]] = []

    async def gmv_max_session_create(self, request):  # noqa: ANN001
        self.create_requests.append(request)
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="req-create",
            data=GMVMaxSessionMutationData.model_validate(
                {"session_id": "session-1"}
            ),
        )

    async def gmv_max_session_delete(self, *, advertiser_id, session_id):
        self.delete_requests.append(
            {"advertiser_id": advertiser_id, "session_id": session_id}
        )
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="req-delete",
            data=GMVMaxSessionMutationData.model_validate({"result": "ok"}),
        )


def test_successful_manual_heating_uses_catalog_scope_and_guard_event_audit(
    db_session,
):
    campaign, heating = _setup_real_scope(db_session)
    client = _OfficialClient()

    updated, _ = asyncio.run(
        apply_boost_creative_action(
            db_session,
            client=client,
            campaign=campaign,
            heating=heating,
            target_daily_budget=50,
            performed_by="operator@example.com",
        )
    )

    assert updated.status == "APPLIED"
    assert client.create_requests
    body = client.create_requests[0].body.model_dump(exclude_none=True)
    assert "product_list" not in body
    assert body["session"]["product_list"] == [{"spu_id": "spu-1"}]
    event = db_session.execute(
        text(
            """
            select event_type, action, result, advertiser_id, store_id, campaign_id,
                   request_json
            from gmv_campaign_guard_events
            """
        )
    ).mappings().one()
    assert event["event_type"] == "CREATIVE_HEATING"
    assert event["action"] == "APPLY_BOOST"
    assert event["result"] == "SUCCESS"
    assert event["advertiser_id"] == "adv-1"
    assert event["store_id"] == "store-1"
    assert event["campaign_id"] == "cmp-1"
    assert "fencing_token" in event["request_json"]
    # No gmv_campaigns row exists; the audit is deliberately FK-independent.
    assert (
        db_session.execute(text("select count(*) from gmv_campaigns")).scalar_one()
        == 0
    )


@pytest.mark.parametrize("blocker", ["manual_pause", "official_disable", "wrong_item"])
def test_creative_boost_fails_closed_before_official_request(db_session, blocker):
    campaign, heating = _setup_real_scope(
        db_session,
        operation_status="DISABLE" if blocker == "official_disable" else "ENABLE",
        heating_item_group_id="spu-other" if blocker == "wrong_item" else "spu-1",
    )
    if blocker == "manual_pause":
        set_manual_pause_override(
            db_session,
            workspace_id=1,
            auth_id=1,
            advertiser_id="adv-1",
            store_id="store-1",
            campaign_id="cmp-1",
            actor="operator",
            reason="manual pause",
        )
        db_session.commit()
    client = _OfficialClient()

    with pytest.raises(CreativeHeatingActionBlocked):
        asyncio.run(
            apply_boost_creative_action(
                db_session,
                client=client,
                campaign=campaign,
                heating=heating,
                target_daily_budget=50,
            )
        )

    assert client.create_requests == []
    event = db_session.execute(
        text(
            "select event_type, action, result from gmv_campaign_guard_events"
        )
    ).mappings().one()
    assert event == {
        "event_type": "CREATIVE_HEATING",
        "action": "APPLY_BOOST",
        "result": "HOLD",
    }


def test_stop_session_is_safe_while_campaign_is_manually_paused(db_session):
    campaign, heating = _setup_real_scope(
        db_session,
        operation_status="DISABLE",
    )
    heating.last_action_response = {"session_id": "session-1"}
    heating.is_heating_active = True
    set_manual_pause_override(
        db_session,
        workspace_id=1,
        auth_id=1,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="cmp-1",
        actor="operator",
        reason="manual pause",
    )
    db_session.commit()
    client = _OfficialClient()

    updated, _ = asyncio.run(
        stop_boost_creative_session(
            db_session,
            client=client,
            campaign=campaign,
            heating=heating,
        )
    )

    assert updated.is_heating_active is False
    assert updated.status == "CANCELLED"
    assert client.delete_requests == [
        {"advertiser_id": "adv-1", "session_id": "session-1"}
    ]
