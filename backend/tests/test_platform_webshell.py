from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.features.platform import router_webshell


class _Admin:
    id = 1
    is_platform_admin = True


def _build_app():
    app = FastAPI()
    app.include_router(router_webshell.router)
    app.include_router(router_webshell.legacy_router)
    return app


def _enable_webshell(monkeypatch):
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_ENABLED", True, raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_MAX_SESSIONS", 20, raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_MAX_SESSIONS_PER_USER", 20, raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_INIT_TIMEOUT_SECONDS", 1, raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_IDLE_TIMEOUT_SECONDS", 30, raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_SESSION_TIMEOUT_SECONDS", 60, raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_ALLOWED_SHELLS", ["/bin/sh", "/bin/bash"], raising=False)
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_DEFAULT_SHELL", "/bin/sh -i", raising=False)
    router_webshell._ACTIVE_SESSIONS.clear()


def test_webshell_disabled_by_default(monkeypatch):
    monkeypatch.setattr(router_webshell.settings, "WEBSHELL_ENABLED", False, raising=False)

    app = _build_app()

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            payload = json.loads(ws.receive_text())
            assert payload["type"] == "error"
            assert "未启用" in payload["message"]


def test_webshell_requires_platform_admin(monkeypatch):
    _enable_webshell(monkeypatch)
    monkeypatch.setattr(router_webshell, "_load_platform_admin", lambda _db, _ws: None)

    app = _build_app()

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            payload = json.loads(ws.receive_text())
            assert payload["type"] == "error"
            assert "平台管理员" in payload["message"]


def test_webshell_can_connect_for_platform_admin(monkeypatch):
    _enable_webshell(monkeypatch)
    monkeypatch.setattr(router_webshell, "_load_platform_admin", lambda _db, _ws: _Admin())

    async def _noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(router_webshell, "_audit", _noop_audit)

    app = _build_app()

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webshell/ws") as ws:
            ws.send_text(json.dumps({"type": "init", "shell": "/bin/sh -i", "cols": 100, "rows": 24}))
            connected = json.loads(ws.receive_text())
            assert connected["type"] == "connected"
            assert connected["cols"] == 100
            assert connected["rows"] == 24

            ws.send_text(json.dumps({"type": "input", "data": "echo connected\r"}))
            ws.send_text(json.dumps({"type": "close"}))


def test_enter_input_is_normalized_to_lf():
    assert router_webshell._normalise_terminal_input("\r") == "\n"
    assert router_webshell._normalise_terminal_input("echo ok\r") == "echo ok\n"
    assert router_webshell._normalise_terminal_input("a\r\nb\rc") == "a\nb\nc"


def test_shell_rejects_command_arguments(monkeypatch):
    _enable_webshell(monkeypatch)

    try:
        router_webshell._resolve_shell("/bin/sh -c id")
    except ValueError as exc:
        assert "禁止 -c" in str(exc)
    else:
        raise AssertionError("shell -c must be rejected")


def test_resize_is_clamped():
    assert router_webshell._clamp_resize(999, 999) == (300, 120)
    assert router_webshell._clamp_resize(1, 1) == (20, 5)


def test_webshell_http_get_returns_upgrade_hint():
    app = _build_app()

    with TestClient(app) as client:
        response = client.get(f"{settings.API_PREFIX}/platform/webshell/ws")

    assert response.status_code == 426
    data = response.json()
    detail = data.get("detail", {})
    assert "仅支持 WebSocket" in detail.get("message", "")
    assert detail.get("expected_path") == f"{settings.API_PREFIX}/platform/webshell/ws"
    assert "production_notes" in detail


def test_legacy_webshell_http_get_returns_upgrade_hint():
    app = _build_app()

    with TestClient(app) as client:
        response = client.get("/api/platform/webshell/ws")

    assert response.status_code == 426
    data = response.json()
    detail = data.get("detail", {})
    assert detail.get("expected_path") == "/api/platform/webshell/ws"
