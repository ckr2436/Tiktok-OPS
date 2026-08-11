from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import install_exception_handlers
from app.features.platform.router_flow2api import _safe_token, router
from app.services.flow2api_admin import Flow2ApiAdminClient
from app.services.flow_proxy_pool import (
    create_flow_proxy,
    decrypt_proxy_url,
    find_proxy_by_url,
    proxy_display_url,
    update_flow_proxy,
)


class _FakeClient:
    calls: list[dict] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, path, json=None):
        self.calls.append({"method": "POST", "path": path, "json": json})
        request = httpx.Request("POST", f"http://flow.test{path}")
        return httpx.Response(200, request=request, json={"token": "admin-session"})

    async def request(self, method, path, headers=None, json=None):
        self.calls.append(
            {"method": method, "path": path, "headers": headers, "json": json}
        )
        request = httpx.Request(method, f"http://flow.test{path}")
        return httpx.Response(
            200,
            request=request,
            json=[{"id": 1, "email": "one@example.com", "st": "must-not-leak"}],
        )


@pytest.mark.asyncio
async def test_flow_admin_session_stays_server_side(monkeypatch):
    _FakeClient.calls = []
    Flow2ApiAdminClient._session_token = None
    monkeypatch.setattr("app.services.flow2api_admin.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr("app.services.flow2api_admin._read_admin_password", lambda: "secret")

    result = await Flow2ApiAdminClient(base_url="http://127.0.0.1:19082").request(
        "GET", "/api/tokens"
    )

    assert result[0]["email"] == "one@example.com"
    assert _FakeClient.calls[0]["json"] == {"username": "admin", "password": "secret"}
    assert _FakeClient.calls[1]["headers"]["Authorization"] == "Bearer admin-session"


def test_flow_token_response_is_credential_free():
    safe = _safe_token(
        {
            "id": 1,
            "email": "one@example.com",
            "credits": 99,
            "is_active": True,
            "routable": False,
            "auth_state": "blocked",
            "ban_reason": "GRANT_EXPIRED",
            "last_keepalive_error": "grant expired",
            "keepalive_failure_count": 2,
            "st": "session-secret",
            "at": "access-secret",
            "captcha_proxy_url": "http://proxy-user:proxy-password@proxy.example:7893",
        }
    )
    assert safe == {
        "id": 1,
        "email": "one@example.com",
        "credits": 99,
        "is_active": True,
        "routable": False,
        "auth_state": "blocked",
        "ban_reason": "GRANT_EXPIRED",
        "last_keepalive_error": "grant expired",
        "keepalive_failure_count": 2,
        "captcha_proxy_url": "http://proxy.example:7893",
    }


def test_flow_account_pool_is_not_available_without_platform_admin():
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/v1/platform/flow2api/overview")
    assert response.status_code in {401, 403}


def test_flow_proxy_pool_encrypts_credentials_and_matches_account_binding(db_session):
    row = create_flow_proxy(
        db_session,
        name="US proxy 01",
        proxy_url="http://alice:secret@192.168.1.21:7893",
        actor_user_id=None,
    )
    db_session.commit()

    assert "alice" not in row.proxy_url_ciphertext
    assert "secret" not in row.proxy_url_ciphertext
    assert row.display_url == "http://192.168.1.21:7893"
    assert decrypt_proxy_url(row.proxy_url_ciphertext) == "http://alice:secret@192.168.1.21:7893"
    assert find_proxy_by_url(db_session, "http://alice:secret@192.168.1.21:7893").id == row.id


def test_flow_proxy_update_keeps_secret_when_only_status_changes(db_session):
    row = create_flow_proxy(
        db_session,
        name="US proxy 02",
        proxy_url="socks5h://192.168.1.22:7893",
    )
    before = row.proxy_url_ciphertext
    update_flow_proxy(db_session, row=row, is_active=False)

    assert row.proxy_url_ciphertext == before
    assert row.is_active is False
    assert proxy_display_url(decrypt_proxy_url(row.proxy_url_ciphertext)) == "socks5h://192.168.1.22:7893"
