from __future__ import annotations

import asyncio

from app.services.ttb_api import TTBApiClient
from app.services.ttb_client_factory import build_ttb_client


def test_build_ttb_client_uses_credentials(monkeypatch, db_session):
    token = "token-123"
    app_id = "app-id"
    app_secret = "app-secret"
    captured = {"token": False, "credentials": False}
    auth_id = 42

    def fake_get_access_token_plain(db, auth):
        assert db is db_session
        assert auth == auth_id
        captured["token"] = True
        return token, object()

    def fake_get_credentials_for_auth_id(db, auth):
        assert db is db_session
        assert auth == auth_id
        captured["credentials"] = True
        return app_id, app_secret, "https://example.com/callback"

    monkeypatch.setattr(
        "app.services.ttb_client_factory.get_access_token_plain",
        fake_get_access_token_plain,
    )
    monkeypatch.setattr(
        "app.services.ttb_client_factory.get_credentials_for_auth_id",
        fake_get_credentials_for_auth_id,
    )

    client = build_ttb_client(db_session, auth_id, qps=2.5)

    assert isinstance(client, TTBApiClient)
    assert captured["token"] and captured["credentials"]
    assert client._client.headers["Access-Token"] == token
    assert client._app_id == app_id
    assert client._app_secret == app_secret

    asyncio.run(client.aclose())
