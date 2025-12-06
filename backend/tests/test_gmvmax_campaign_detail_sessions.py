import logging
from typing import Any, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import require_tenant_admin, require_tenant_member
from app.core.errors import install_exception_handlers
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.router import router as ttb_router
from app.data.models.gmv_restructured import GmvCampaign, PromotionTypeEnum
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxResponse,
    GMVMaxSession,
    GMVMaxSessionListData,
    GMVMaxSessionListRequest,
    PageInfo,
)


class StubSessionClient:
    def __init__(self) -> None:
        self.session_requests: List[Any] = []
        self.should_fail = False

    async def gmv_max_session_list(self, request: GMVMaxSessionListRequest) -> GMVMaxResponse[GMVMaxSessionListData]:
        if self.should_fail:
            raise RuntimeError("session fetch failed")
        self.session_requests.append(request)
        session = GMVMaxSession(session_id="sess-1", campaign_id=request.campaign_id)
        return GMVMaxResponse(
            code=0,
            message="ok",
            request_id="session-request",
            data=GMVMaxSessionListData(
                list=[session],
                page_info=PageInfo(page=1, page_size=20, total_number=1),
            ),
        )


@pytest.fixture()
def campaign_detail_client(db_session):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    class _StubUser:
        email = "tester@example.com"
        display_name = "tester"
        username = "tester"

    def _member_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001, ARG001
        return _StubUser()

    def _admin_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001, ARG001
        return _StubUser()

    app.dependency_overrides[require_tenant_member] = _member_override
    app.dependency_overrides[require_tenant_admin] = _admin_override

    workspace = Workspace(id=1, name="Demo", company_code="0001")
    provider_app = OAuthProviderApp(
        id=1,
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    account = OAuthAccountTTB(
        id=1,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    campaign = GmvCampaign(
        id=1,
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-1",
        campaign_id="cmp-1",
        store_id="store-1",
        name="Primary",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.add_all([workspace, provider_app, account, campaign])
    db_session.flush()

    stub_client = StubSessionClient()
    context = router_provider.GMVMaxRouteContext(
        workspace_id=workspace.id,
        provider="tiktok-business",
        auth_id=account.id,
        advertiser_id="adv-1",
        store_id="store-1",
        binding=router_provider.GMVMaxAccountBinding(
            account=account, advertiser_id="adv-1", store_id="store-1"
        ),
        client=stub_client,
        db=db_session,
    )

    def _override_context(workspace_id: int, provider: str, auth_id: int, db=None):  # noqa: ANN001, ARG001
        return context

    app.dependency_overrides[router_provider.get_route_context] = _override_context

    with TestClient(app) as client:
        yield {"client": client, "stub": stub_client}

    app.dependency_overrides.clear()


def test_campaign_detail_fetches_sessions_on_demand(campaign_detail_client):
    client: TestClient = campaign_detail_client["client"]
    stub: StubSessionClient = campaign_detail_client["stub"]

    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1",
        params={"include_sessions": "true"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sessions"][0]["session_id"] == "sess-1"
    assert body["sessions_page_info"]["total_number"] == 1
    assert body["sessions_request_id"] == "session-request"
    assert len(stub.session_requests) == 1


def test_campaign_detail_skips_sessions_when_flag_false(campaign_detail_client):
    client: TestClient = campaign_detail_client["client"]
    stub: StubSessionClient = campaign_detail_client["stub"]

    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1",
        params={"include_sessions": "false"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sessions"] == []
    assert body["sessions_page_info"] is None
    assert body["sessions_request_id"] is None
    assert stub.session_requests == []


def test_campaign_detail_continues_on_session_error(campaign_detail_client, caplog):
    client: TestClient = campaign_detail_client["client"]
    stub: StubSessionClient = campaign_detail_client["stub"]
    stub.should_fail = True

    with caplog.at_level(logging.WARNING):
        response = client.get(
            "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-1",
            params={"include_sessions": "true"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sessions"] == []
    assert body["sessions_page_info"] is None
    assert body["sessions_request_id"] is None
    assert any("session list fetch failed" in record.message for record in caplog.records)
