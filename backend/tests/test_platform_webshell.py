from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.features.platform import router_webshell


def test_webshell_requires_platform_admin(monkeypatch):
    monkeypatch.setattr(router_webshell, "_load_platform_admin", lambda _db, _ws: None)

    app = FastAPI()
    app.include_router(router_webshell.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            payload = json.loads(ws.receive_text())
            assert payload["type"] == "error"
            assert "平台管理员" in payload["message"]


def test_webshell_can_connect_for_platform_admin(monkeypatch):
    monkeypatch.setattr(router_webshell, "_load_platform_admin", lambda _db, _ws: object())

    app = FastAPI()
    app.include_router(router_webshell.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            ws.send_text(json.dumps({"shell": "/bin/sh -i"}))
            connected = json.loads(ws.receive_text())
            assert connected["type"] == "connected"

            ws.send_text(json.dumps({"type": "input", "data": "echo connected\n"}))
