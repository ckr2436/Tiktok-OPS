from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError, install_exception_handlers
from app.features.platform.router_sub2api import router
from app.services import sub2api_oidc


class FakeRedis:
    def __init__(self):
        self.values: dict[str, bytes] = {}

    def setex(self, key, ttl, value):
        del ttl
        self.values[str(key)] = value.encode() if isinstance(value, str) else value
        return True

    def get(self, key):
        return self.values.get(str(key))

    def getdel(self, key):
        return self.values.pop(str(key), None)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _admin() -> SessionUser:
    return SessionUser(
        id=7,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="0000-000007",
        is_platform_admin=True,
        workspace_id=1,
        role="owner",
        is_active=True,
    )


def _configure(monkeypatch, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "oidc.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    fake = FakeRedis()
    monkeypatch.setattr(settings, "SUB2API_SSO_ENABLED", True)
    monkeypatch.setattr(settings, "SUB2API_OIDC_ISSUER", "https://gmv.example/api/v1/platform/sub2api/oidc")
    monkeypatch.setattr(settings, "SUB2API_OIDC_CLIENT_ID", "sub2api-test")
    monkeypatch.setattr(settings, "SUB2API_OIDC_REDIRECT_URI", "https://gmv.example/sub2api/api/v1/auth/oauth/oidc/callback")
    monkeypatch.setattr(settings, "SUB2API_OIDC_PRIVATE_KEY_FILE", str(key_path))
    monkeypatch.setattr(sub2api_oidc, "get_redis_sync", lambda: fake)
    sub2api_oidc.load_signing_material.cache_clear()
    return fake


def _client(admin_dependency) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = admin_dependency
    return TestClient(app)


def test_oidc_pkce_flow_is_single_use(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    verifier = "v" * 64
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    client = _client(_admin)
    params = {
        "response_type": "code",
        "client_id": settings.SUB2API_OIDC_CLIENT_ID,
        "redirect_uri": settings.SUB2API_OIDC_REDIRECT_URI,
        "scope": "openid email profile",
        "state": "state-123",
        "nonce": "nonce-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = client.get("/api/v1/platform/sub2api/oidc/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]

    token = client.post(
        "/api/v1/platform/sub2api/oidc/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.SUB2API_OIDC_CLIENT_ID,
            "redirect_uri": settings.SUB2API_OIDC_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    body = token.json()
    assert body["token_type"] == "Bearer"
    header, payload, signature = body["id_token"].split(".")
    assert signature
    assert json.loads(base64.urlsafe_b64decode(header + "=="))["alg"] == "RS256"
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims["sub"] == sub2api_oidc.SHARED_ADMIN_SUBJECT
    assert claims["nonce"] == "nonce-123"

    userinfo = client.get(
        "/api/v1/platform/sub2api/oidc/userinfo",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert userinfo.status_code == 200
    assert userinfo.json()["email"] == sub2api_oidc.SHARED_ADMIN_EMAIL

    replay = client.post(
        "/api/v1/platform/sub2api/oidc/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.SUB2API_OIDC_CLIENT_ID,
            "redirect_uri": settings.SUB2API_OIDC_REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_authorize_rejects_wrong_redirect_and_requires_admin(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    verifier = "z" * 64
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code",
        "client_id": settings.SUB2API_OIDC_CLIENT_ID,
        "redirect_uri": "https://attacker.example/callback",
        "scope": "openid",
        "state": "state",
        "nonce": "nonce",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with _client(_admin) as client:
        response = client.get("/api/v1/platform/sub2api/oidc/authorize", params=params)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    def denied():
        raise APIError("FORBIDDEN", "Platform admin required.", 403)

    params["redirect_uri"] = settings.SUB2API_OIDC_REDIRECT_URI
    with _client(denied) as client:
        response = client.get("/api/v1/platform/sub2api/oidc/authorize", params=params)
        assert response.status_code == 403
