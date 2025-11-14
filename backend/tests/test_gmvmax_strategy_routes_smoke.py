import base64
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import install_exception_handlers
from app.data.models import OAuthAccountTTB, OAuthProviderApp, Workspace
from app.features.tenants.ttb.router import router as ttb_router
from app.features.tenants.ttb.gmv_max import router_provider
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignInfoData,
    GMVMaxResponse,
    GMVMaxSession,
    GMVMaxSessionListData,
)
from app.services.crypto import encrypt_text_to_blob


@pytest.fixture()
def gmv_strategy_app(db_session, monkeypatch):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    dummy_user = SessionUser(
        id=1,
        email="member@example.com",
        username="member",
        display_name="Member",
        usercode="U100",
        is_platform_admin=False,
        workspace_id=1,
        role="member",
        is_active=True,
    )

    def _override(workspace_id: int):  # noqa: ANN001
        return dummy_user

    app.dependency_overrides[require_tenant_member] = _override
    app.dependency_overrides[require_tenant_admin] = _override

    if not getattr(settings, "CRYPTO_MASTER_KEY_B64", ""):
        settings.CRYPTO_MASTER_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode()

    workspace = Workspace(id=1, name="Acme", company_code="ACME")
    db_session.add(workspace)

    provider = OAuthProviderApp(
        id=1,
        provider="tiktok_business",
        name="Default",
        client_id="client",
        client_secret_cipher=encrypt_text_to_blob(
            "secret",
            key_version=1,
            aad_text="tiktok_business|client|https://example.com/callback",
        ),
        client_secret_key_version=1,
        redirect_uri="https://example.com/callback",
        is_enabled=True,
    )
    db_session.add(provider)

    account = OAuthAccountTTB(
        id=1,
        workspace_id=1,
        provider_app_id=1,
        alias="binding",
        access_token_cipher=encrypt_text_to_blob("token", key_version=1, aad_text="token"),
        key_version=1,
        token_fingerprint=b"0" * 32,
        status="active",
    )
    db_session.add(account)

    db_session.commit()

    calls: list[str] = []

    async def _campaign_update_stub(*args, **kwargs):  # noqa: ANN001
        return None

    async def _session_update_stub(*args, **kwargs):  # noqa: ANN001
        return None

    dummy_client = SimpleNamespace(
        gmv_max_campaign_update=_campaign_update_stub,
        gmv_max_session_update=_session_update_stub,
    )

    context = router_provider.GMVMaxRouteContext(
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        advertiser_id="7492997033645637633",
        store_id="store-1",
        binding=router_provider.GMVMaxAccountBinding(
            account=account,
            advertiser_id="7492997033645637633",
            store_id="store-1",
        ),
        client=dummy_client,
    )

    async def fake_call(func, *args, **kwargs):  # noqa: ANN001
        if func is _campaign_update_stub:
            calls.append("gmv_max_campaign_update")
            return GMVMaxResponse(
                code=0,
                message="ok",
                request_id="campaign-update",
                data=GMVMaxCampaignInfoData(
                    campaign_id="cmp-partial",
                    campaign_name="Partial Update",
                    advertiser_id="7492997033645637633",
                ),
            )
        if func is _session_update_stub:
            calls.append("gmv_max_session_update")
            session = GMVMaxSession(session_id="session-1", campaign_id="cmp-partial")
            return GMVMaxResponse(
                code=0,
                message="ok",
                request_id="session-update",
                data=GMVMaxSessionListData(list=[session]),
            )
        raise AssertionError("unexpected TikTok call")

    monkeypatch.setattr(router_provider, "_call_tiktok", fake_call)
    app.dependency_overrides[router_provider.get_route_context] = (
        lambda workspace_id, provider, auth_id, db=None: context  # noqa: ARG005
    )

    with TestClient(app) as client:
        yield client, db_session, calls

    app.dependency_overrides.clear()


def test_strategy_routes_registered():
    mod = import_module("app.features.tenants.ttb.gmv_max.router_provider")
    router = getattr(mod, "router", None)
    assert isinstance(router, APIRouter)
    paths = {route.path for route in router.routes}
    assert any(path.endswith("/strategy") for path in paths)
    assert any(path.endswith("/strategies/preview") for path in paths)


def test_update_strategy_partial_patch_returns_remote_payload(gmv_strategy_app):
    client, _db, calls = gmv_strategy_app

    routes = {route.path for route in client.app.routes}
    assert any('gmvmax' in path for path in routes)

    response = client.put(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-partial/strategy",
        json={"campaign": {"budget": 150.0}},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] == "success"
    assert body["campaign"]["campaign_id"] == "cmp-partial"
    assert body["campaign_request_id"] == "campaign-update"
    assert body["session_request_id"] is None
    assert calls == ["gmv_max_campaign_update"]


def test_update_strategy_noop_returns_noop_status(gmv_strategy_app):
    client, _db, calls = gmv_strategy_app

    response = client.put(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/cmp-noop/strategy",
        json={},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "noop", "campaign": None, "sessions": None, "campaign_request_id": None, "session_request_id": None}
    assert calls == []
