from __future__ import annotations

from datetime import date
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import require_tenant_admin, require_tenant_member
from app.core.errors import install_exception_handlers
from app.features.tenants.ttb.router import router as ttb_router
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendation,
    GMVMaxCampaign,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignListData,
    GMVMaxReportData,
    GMVMaxReportEntry,
    GMVMaxResponse,
    GMVMaxSession,
    GMVMaxSessionListData,
    GMVMaxSessionProduct,
)


class StubGMVMaxClient:
    def __init__(self) -> None:
        self.campaign_requests: List[Any] = []
        self.report_requests: List[Any] = []
        self.action_calls: List[str] = []

    async def gmv_max_campaign_get(self, request):  # noqa: ANN001
        self.campaign_requests.append(request)
        data = GMVMaxCampaignListData(
            list=[
                GMVMaxCampaign(campaign_id="cmp-1", campaign_name="Primary"),
            ],
        )
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="campaign-list",
            data=data,
        )

    async def gmv_max_campaign_info(self, request):  # noqa: ANN001
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="campaign-info",
            data=GMVMaxCampaignInfoData(
                campaign_id=request.campaign_id,
                campaign_name="Primary",
                advertiser_id=request.advertiser_id,
                store_id="store-1",
                shopping_ads_type="PRODUCT",
                optimization_goal="GMV",
            ),
        )

    async def gmv_max_session_list(self, request):  # noqa: ANN001
        session = GMVMaxSession(
            session_id="session-1",
            campaign_id=request.campaign_id,
            product_list=[GMVMaxSessionProduct(spu_id="spu-1")],
        )
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="session-list",
            data=GMVMaxSessionListData(list=[session]),
        )

    async def gmv_max_report_get(self, request):  # noqa: ANN001
        self.report_requests.append(request)
        entry = GMVMaxReportEntry(metrics={"spend": "10"}, dimensions={})
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="report",
            data=GMVMaxReportData(list=[entry]),
        )

    async def gmv_max_campaign_update(self, request):  # noqa: ANN001
        self.action_calls.append("campaign_update")
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="campaign-update",
            data=GMVMaxCampaignInfoData(
                campaign_id=request.body.campaign_id,
                campaign_name="Primary",
                advertiser_id=request.advertiser_id,
                store_id="store-1",
            ),
        )

    async def gmv_max_session_update(self, request):  # noqa: ANN001
        self.action_calls.append("session_update")
        session = GMVMaxSession(session_id=request.body.session_id)
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="session-update",
            data=GMVMaxSessionListData(list=[session]),
        )

    async def gmv_max_bid_recommend(self, request):  # noqa: ANN001
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="bid-recommend",
            data=GMVMaxBidRecommendation(budget=123.0, roas_bid=2.5),
        )


@pytest.fixture()
def gmvmax_client_fixture(monkeypatch):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    # override auth dependencies
    def _member_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001, ARG001
        return True

    def _admin_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001, ARG001
        return True

    app.dependency_overrides[require_tenant_member] = _member_override
    app.dependency_overrides[require_tenant_admin] = _admin_override

    from app.features.tenants.ttb.gmv_max import router_provider

    stub_client = StubGMVMaxClient()

    context = router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=router_provider.GMVMaxAccountBinding(
            account=SimpleNamespace(),
            advertiser_id="adv-1",
            store_id="store-1",
        ),
        client=stub_client,
    )

    def _override_context(workspace_id: int, provider: str, auth_id: int, db=None):  # noqa: ANN001, ARG001
        return context

    app.dependency_overrides[router_provider.get_route_context] = _override_context

    with TestClient(app) as client:
        yield {
            "client": client,
            "stub": stub_client,
        }

    app.dependency_overrides.clear()


def test_sync_endpoint_returns_combined_payload(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    payload = {
        "report": {
            "start_date": date(2024, 1, 1).isoformat(),
            "end_date": date(2024, 1, 2).isoformat(),
            "metrics": ["spend"],
            "dimensions": ["campaign_id"],
        }
    }
    response = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/sync",
        json=payload,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaigns"][0]["campaign_id"] == "cmp-1"
    assert body["report"]["list"][0]["metrics"]["spend"] == "10"


def test_campaign_list_proxy(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"][0]["campaign_name"] == "Primary"


def test_campaign_detail_includes_sessions(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["campaign"]["campaign_id"] == "cmp-1"
    assert body["sessions"][0]["session_id"] == "session-1"


def test_metrics_sync_returns_report(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    payload = {
        "start_date": date(2024, 1, 1).isoformat(),
        "end_date": date(2024, 1, 7).isoformat(),
        "metrics": ["spend"],
        "dimensions": ["campaign_id"],
    }
    response = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/metrics/sync",
        json=payload,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["list"], body


def test_metrics_query_defaults(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/metrics",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["list"][0]["metrics"]["spend"] == "10"


def test_campaign_action_session_update(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    payload = {
        "type": "update_strategy",
        "payload": {
            "session_id": "session-1",
            "session": {"budget": 99.0},
        },
    }
    response = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/actions",
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert "session_update" in gmvmax_client_fixture["stub"].action_calls


def test_actions_placeholder_list(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/actions",
    )
    assert response.status_code == 200
    assert response.json()["entries"] == []


def test_strategy_payload(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/strategy",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["campaign"]["campaign_id"] == "cmp-1"
    assert body["sessions"][0]["session_id"] == "session-1"
    assert body["recommendation"]["budget"] == 123.0


def test_strategy_update_supports_dual_calls(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    payload = {
        "campaign": {"budget": 500.0},
        "session": {"session_id": "session-1"},
    }
    response = client.put(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/strategy",
        json=payload,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["campaign"]["campaign_id"] == "cmp-1"


def test_strategy_preview_returns_recommendation(gmvmax_client_fixture):
    client: TestClient = gmvmax_client_fixture["client"]
    payload = {
        "store_id": "store-1",
        "shopping_ads_type": "PRODUCT",
        "optimization_goal": "GMV",
        "item_group_ids": ["spu-1"],
    }
    response = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1/strategies/preview",
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["recommendation"]["budget"] == 123.0
