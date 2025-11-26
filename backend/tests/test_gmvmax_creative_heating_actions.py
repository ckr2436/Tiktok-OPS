from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from app.core.deps import SessionUser
from app.features.tenants.ttb.gmv_max import router_provider
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignActionApplyData,
    GMVMaxResponse,
)
from app.services import gmvmax_heating_actions
from app.services.gmvmax_heating_actions import apply_boost_creative_action


class DummySession:
    def __init__(self) -> None:
        self.flush_calls = 0

    def flush(self) -> None:  # pragma: no cover - trivial
        self.flush_calls += 1


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
    )
    heating = SimpleNamespace(
        id=211,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id="cmp-10",
        creative_id="cr-10",
        mode="BOOST",
        target_daily_budget=50.0,
        budget_delta=None,
        currency="USD",
        max_duration_minutes=180,
        note="auto",
    )

    update_calls: list[dict[str, Any]] = []
    log_calls: list[dict[str, Any]] = []

    async def fake_update(*_, **kwargs):  # noqa: ANN001
        update_calls.append(kwargs)
        return heating

    def fake_log(*args, **kwargs):  # noqa: ANN001
        log_calls.append(kwargs)
        return None

    client_calls: list[Any] = []

    class DummyClient:
        async def gmv_max_campaign_action_apply(self, request):  # noqa: ANN001
            client_calls.append(request)
            data = GMVMaxCampaignActionApplyData.model_validate({"result": "ok"})
            return GMVMaxResponse(
                code=0, message="ok", request_id="req-1", data=data
            )

    monkeypatch.setattr(
        gmvmax_heating_actions, "update_heating_action_result", fake_update
    )
    monkeypatch.setattr(gmvmax_heating_actions, "log_campaign_action", fake_log)

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
    assert client_calls and client_calls[0].body.action_type == "BOOST_CREATIVE"
    assert log_calls and log_calls[0]["performed_by"] == "tester"
    assert db.flush_calls >= 2


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
            mode=kwargs.get("mode"),
            target_daily_budget=kwargs.get("target_daily_budget"),
            budget_delta=kwargs.get("budget_delta"),
            currency=kwargs.get("currency"),
            max_duration_minutes=kwargs.get("max_duration_minutes"),
            note=kwargs.get("note"),
        )

    async def fake_apply_boost(*_, **kwargs):  # noqa: ANN001
        apply_calls.append(kwargs)
        data = GMVMaxCampaignActionApplyData.model_validate({"result": "ok"})
        response = GMVMaxResponse(code=0, message="ok", request_id="req-1", data=data)
        return kwargs["heating"], response

    monkeypatch.setattr(router_provider, "upsert_creative_heating", fake_upsert)
    monkeypatch.setattr(router_provider, "_load_campaign_row", lambda *_: SimpleNamespace(id=99, campaign_id=campaign_id, advertiser_id="adv-1"))
    monkeypatch.setattr(router_provider, "apply_boost_creative_action", fake_apply_boost)

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
    assert response.tiktok_response == {"result": "ok"}

    assert upsert_calls
    assert upsert_calls[0]["creative_id"] == "cr-55"
    assert apply_calls and apply_calls[0]["performed_by"] == "tester@example.com"
    assert apply_calls[0]["note"] == "Boost this creative"
    assert context.db.flush_calls >= 1
