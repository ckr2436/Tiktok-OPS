from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from app.features.tenants.ttb.gmv_max import router_provider
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignActionApplyData,
    GMVMaxResponse,
)


class DummySession:
    def __init__(self) -> None:
        self.flush_calls = 0

    def flush(self) -> None:  # pragma: no cover - trivial
        self.flush_calls += 1


def test_apply_boost_creative_action(monkeypatch):
    workspace_id = 1
    provider = "tiktok-business"
    auth_id = 2
    campaign_id = "cmp-10"

    upsert_calls: list[dict[str, Any]] = []
    update_calls: list[dict[str, Any]] = []

    async def fake_upsert(*_, **kwargs):  # noqa: ANN001
        upsert_calls.append(kwargs)
        return SimpleNamespace(
            id=11,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id,
            creative_id=kwargs["creative_id"],
            creative_name=kwargs.get("creative_name"),
            mode=kwargs.get("mode"),
            target_daily_budget=kwargs.get("target_daily_budget"),
            budget_delta=kwargs.get("budget_delta"),
            currency=kwargs.get("currency"),
            max_duration_minutes=kwargs.get("max_duration_minutes"),
            note=kwargs.get("note"),
            status="PENDING",
            last_action_type=None,
            last_action_time=None,
            last_error=None,
        )

    async def fake_update(*_, **kwargs):  # noqa: ANN001
        update_calls.append(kwargs)
        return SimpleNamespace(
            id=kwargs["heating_id"],
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            campaign_id=campaign_id,
            creative_id="cr-55",
            creative_name="Hero",
            mode="BOOST",
            target_daily_budget=kwargs["request_payload"].get("target_daily_budget"),
            budget_delta=kwargs["request_payload"].get("budget_delta"),
            currency=kwargs["request_payload"].get("currency"),
            max_duration_minutes=kwargs["request_payload"].get("max_duration_minutes"),
            note=kwargs["request_payload"].get("note"),
            status=kwargs["status"],
            last_action_type=kwargs["action_type"],
            last_action_time=kwargs["action_time"],
            last_error=kwargs.get("error_message"),
        )

    monkeypatch.setattr(router_provider, "upsert_creative_heating", fake_upsert)
    monkeypatch.setattr(router_provider, "update_heating_action_result", fake_update)

    client_calls: list[Any] = []

    async def fake_action_apply(request):  # noqa: ANN001
        client_calls.append(request)
        data = GMVMaxCampaignActionApplyData.model_validate({"result": "ok"})
        return GMVMaxResponse(code=0, message="ok", request_id="req-1", data=data)

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
        client=SimpleNamespace(gmv_max_campaign_action_apply=fake_action_apply),
        db=DummySession(),
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
            context=context,
        )
    )

    assert isinstance(response, router_provider.CreativeHeatingActionResponse)
    assert response.action_type == "BOOST_CREATIVE"
    assert response.heating.creative_id == "cr-55"
    assert response.tiktok_response == {"result": "ok"}

    assert upsert_calls
    assert upsert_calls[0]["creative_id"] == "cr-55"
    assert update_calls and update_calls[0]["status"] == "APPLIED"
    assert client_calls and client_calls[0].body.action_type == "BOOST_CREATIVE"
    assert context.db.flush_calls >= 2
