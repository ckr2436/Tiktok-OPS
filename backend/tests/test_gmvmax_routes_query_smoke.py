import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import install_exception_handlers
from app.features.tenants.ttb.router import router as ttb_router
from app.features.tenants.ttb.gmv_max import router_campaigns


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    member = SessionUser(
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

    def _member_override(workspace_id: int, auth_id: int | None = None):  # noqa: ANN001
        return member

    app.dependency_overrides[require_tenant_member] = _member_override
    app.dependency_overrides[require_tenant_admin] = _member_override

    async def fake_list_campaigns(db, *, workspace_id, provider, auth_id, **kwargs):
        return {"items": [], "total": 0, "page": 1, "page_size": 20}

    monkeypatch.setattr(router_campaigns, "list_campaigns", fake_list_campaigns)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture()
def tenant_headers():
    return {"X-Workspace-Id": "1"}


def test_list_campaigns_only_requires_advertiser_id(client, tenant_headers):
    response = client.get(
        "/api/v1/tenants/1/ttb/accounts/1/gmvmax/",
        params={"advertiser_id": "123"},
        headers=tenant_headers,
    )
    assert response.status_code == 200, response.json()


def test_list_campaigns_missing_advertiser_id_422(client, tenant_headers):
    response = client.get(
        "/api/v1/tenants/1/ttb/accounts/1/gmvmax/",
        headers=tenant_headers,
    )
    assert response.status_code == 422
