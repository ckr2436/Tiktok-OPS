from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.features.platform import router_webshell


def _build_app():
    app = FastAPI()
    app.include_router(router_webshell.router)
    app.include_router(router_webshell.legacy_router)
    return app


def test_webshell_requires_platform_admin(monkeypatch):
    monkeypatch.setattr(router_webshell, "_load_platform_admin", lambda _db, _ws: None)

    app = _build_app()

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            payload = json.loads(ws.receive_text())
            assert payload["type"] == "error"
            assert "平台管理员" in payload["message"]


def test_webshell_can_connect_for_platform_admin(monkeypatch):
    monkeypatch.setattr(router_webshell, "_load_platform_admin", lambda _db, _ws: object())

    app = _build_app()

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            ws.send_text(json.dumps({"shell": "/bin/sh -i"}))
            connected = json.loads(ws.receive_text())
            assert connected["type"] == "connected"

            ws.send_text(json.dumps({"type": "input", "data": "echo connected\n"}))


def test_webshell_http_get_returns_upgrade_hint():
    app = _build_app()

    with TestClient(app) as client:
        response = client.get(f"{settings.API_PREFIX}/platform/webshell/ws")

    assert response.status_code == 426
    data = response.json()
    detail = data.get("detail", {})
    assert "仅支持 WebSocket" in detail.get("message", "")
    assert detail.get("expected_path") == f"{settings.API_PREFIX}/platform/webshell/ws"


def test_legacy_webshell_http_get_returns_upgrade_hint():
    app = _build_app()

    with TestClient(app) as client:
        response = client.get("/api/platform/webshell/ws")

    assert response.status_code == 426
    data = response.json()
    detail = data.get("detail", {})
    assert detail.get("expected_path") == "/api/platform/webshell/ws"
