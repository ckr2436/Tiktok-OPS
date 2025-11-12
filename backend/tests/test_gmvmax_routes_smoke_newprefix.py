from __future__ import annotations


from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import (
    SessionUser,
    require_session,
    require_tenant_admin,
    require_tenant_member,
)
from app.core.errors import install_exception_handlers
from app.features.tenants.ttb.router import router as ttb_router


@pytest.fixture()
def gmvmax_smoke_client(monkeypatch):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    member = SessionUser(
        id=100,
        email="tester@example.com",
        username="tester",
        display_name="Tester",
        usercode="UTEST",
        is_platform_admin=False,
        workspace_id=1,
        role="member",
        is_active=True,
    )

    def _member_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001
        return member

    def _admin_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001
        return member

    def _session_override():  # noqa: ANN001
        return member

    app.dependency_overrides[require_tenant_member] = _member_override
    app.dependency_overrides[require_tenant_admin] = _admin_override
    app.dependency_overrides[require_session] = _session_override

    from app.features.tenants.ttb.gmv_max import (
        router_actions,
        router_campaigns,
        router_metrics,
        router_strategy,
    )
    from app.features.tenants.ttb.gmv_max import schemas as gmv_schemas

    gmv_schemas.GmvMaxCampaignOut.model_config = {
        **gmv_schemas.GmvMaxCampaignOut.model_config,
        "from_attributes": True,
    }

    campaign = gmv_schemas.GmvMaxCampaignOut(
        id=101,
        campaign_id="cmp-smoke",
        name="Smoke Test",
        status="ACTIVE",
        advertiser_id="adv-001",
        shopping_ads_type=None,
        optimization_goal=None,
        roas_bid=None,
        daily_budget_cents=5000,
        currency="CNY",
        ext_created_time=datetime(2024, 1, 1, 0, 0, 0),
        ext_updated_time=None,
    )

    base_strategy = {
        "workspace_id": 1,
        "auth_id": 99,
        "campaign_id": campaign.campaign_id,
        "enabled": True,
        "target_roi": Decimal("1.20"),
        "min_roi": Decimal("0.80"),
        "max_roi": Decimal("2.40"),
        "min_impressions": 100,
        "min_clicks": 5,
        "max_budget_raise_pct_per_day": Decimal("10.0"),
        "max_budget_cut_pct_per_day": Decimal("5.0"),
        "max_roas_step_per_adjust": Decimal("0.50"),
        "cooldown_minutes": 30,
        "min_runtime_minutes_before_first_change": 120,
    }

    sync_campaign_calls: list[dict] = []
    sync_metrics_calls: list[dict] = []
    update_calls: list[dict] = []
    action_calls: list[dict] = []
    preview_calls: list[dict] = []

    async def fake_list_campaigns(db, *, workspace_id, provider, auth_id, **_extra):
        sync_campaign_calls.append({
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "list": True,
        })
        return {
            "items": [campaign],
            "total": 1,
            "page": 1,
            "page_size": 20,
        }

    async def fake_sync_campaigns(db, *, workspace_id, provider, auth_id, **_extra):
        sync_campaign_calls.append({
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "sync": True,
        })
        return 2

    async def fake_sync_metrics(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        campaign_id,
        advertiser_id,
        granularity,
        start_date,
        end_date,
    ):
        sync_metrics_calls.append({
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "advertiser_id": advertiser_id,
            "granularity": granularity,
            "start_date": start_date,
            "end_date": end_date,
        })
        return 12

    def fake_preview_strategy(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        campaign_id,
    ):
        preview_calls.append({
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
        })
        return {"enabled": True, "decision": {"reason": "ok"}}

    def fake_update_strategy(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        campaign_id,
        payload,
    ):
        update_calls.append(payload)
        if not payload:
            return None

        normalized: dict[str, object] = {}
        for key, value in payload.items():
            if key in {
                "target_roi",
                "min_roi",
                "max_roi",
                "max_roas_step_per_adjust",
                "max_budget_raise_pct_per_day",
                "max_budget_cut_pct_per_day",
            }:
                normalized[key] = Decimal(str(value))
            else:
                normalized[key] = value

        data = base_strategy | normalized
        return SimpleNamespace(**data)

    async def fake_apply_campaign_action(
        db,
        *,
        workspace_id,
        provider,
        auth_id,
        campaign_id,
        action,
        payload,
        reason,
        performed_by,
        audit_hook=None,
    ):
        action_calls.append({
            "workspace_id": workspace_id,
            "provider": provider,
            "auth_id": auth_id,
            "campaign_id": campaign_id,
            "action": action,
            "payload": payload,
            "reason": reason,
            "performed_by": performed_by,
        })
        log_entry = SimpleNamespace(result="ok", action=action)
        return campaign, log_entry

    monkeypatch.setattr(router_campaigns, "list_campaigns", fake_list_campaigns)
    monkeypatch.setattr(router_campaigns, "sync_campaigns", fake_sync_campaigns)
    monkeypatch.setattr(router_metrics, "sync_metrics", fake_sync_metrics)
    monkeypatch.setattr(router_strategy, "preview_strategy", fake_preview_strategy)
    monkeypatch.setattr(router_strategy, "update_strategy", fake_update_strategy)
    monkeypatch.setattr(router_actions, "apply_campaign_action", fake_apply_campaign_action)

    with TestClient(app) as client:
        yield {
            "client": client,
            "campaign": campaign,
            "sync_campaign_calls": sync_campaign_calls,
            "sync_metrics_calls": sync_metrics_calls,
            "update_calls": update_calls,
            "action_calls": action_calls,
            "preview_calls": preview_calls,
        }

    app.dependency_overrides.clear()


def test_list_campaigns_uses_new_prefix(gmvmax_smoke_client):
    client = gmvmax_smoke_client["client"]
    response = client.get("/api/v1/tenants/1/ttb/accounts/99/gmvmax")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["campaign_id"] == "cmp-smoke"


def test_sync_campaigns_accepts_minimal_body(gmvmax_smoke_client):
    client = gmvmax_smoke_client["client"]
    response = client.post(
        "/api/v1/tenants/1/ttb/accounts/99/gmvmax/sync",
        json={"force": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["synced"] == 2


def test_sync_metrics_accepts_range_payload(gmvmax_smoke_client):
    client = gmvmax_smoke_client["client"]
    response = client.post(
        "/api/v1/tenants/1/ttb/accounts/99/gmvmax/cmp-smoke/metrics/sync",
        json={
            "advertiser_id": "adv-001",
            "granularity": "DAY",
            "start_date": date(2024, 1, 1).isoformat(),
            "end_date": date(2024, 1, 7).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["synced_rows"] == 12
    call = gmvmax_smoke_client["sync_metrics_calls"][0]
    assert call["granularity"] == "DAY"
    assert call["start_date"] == date(2024, 1, 1)
    assert call["end_date"] == date(2024, 1, 7)


def test_preview_strategy_allows_post_with_empty_body(gmvmax_smoke_client):
    client = gmvmax_smoke_client["client"]
    response = client.post(
        "/api/v1/tenants/1/ttb/accounts/99/gmvmax/cmp-smoke/strategies/preview",
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert gmvmax_smoke_client["preview_calls"]


def test_update_strategy_returns_partial_patch(gmvmax_smoke_client):
    client = gmvmax_smoke_client["client"]
    response = client.put(
        "/api/v1/tenants/1/ttb/accounts/99/gmvmax/cmp-smoke/strategy",
        json={"target_roi": "1.50"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_roi"] == "1.50"
    assert gmvmax_smoke_client["update_calls"][0] == {"target_roi": "1.50"}


def test_apply_action_accepts_minimal_payload(gmvmax_smoke_client):
    client = gmvmax_smoke_client["client"]
    response = client.post(
        "/api/v1/tenants/1/ttb/accounts/99/gmvmax/cmp-smoke/actions",
        json={"action": "PAUSE"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "PAUSE"
    call = gmvmax_smoke_client["action_calls"][0]
    assert call["payload"] == {}
