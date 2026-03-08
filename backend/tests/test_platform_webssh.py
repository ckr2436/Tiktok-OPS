from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.features.platform import router_webssh


class _FakeStream:
    async def read(self, _size: int) -> str:
        return ""


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.stdin = _FakeStdin()
        self._closed = asyncio.Event()

    def change_terminal_size(self, _cols: int, _rows: int) -> None:
        return None

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def close(self) -> None:
        self._closed.set()


class _FakeClient:
    def __init__(self, process: _FakeProcess) -> None:
        self._process = process

    async def create_process(self, **_kwargs):
        return self._process

    def close(self) -> None:
        self._process.close()

    async def wait_closed(self) -> None:
        return None


def test_webssh_does_not_close_immediately_when_output_streams_finish(monkeypatch, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey\n", encoding="utf-8")
    monkeypatch.setattr(settings, "WEBSSH_KNOWN_HOSTS_FILE", str(known_hosts))

    fake_process = _FakeProcess()

    async def _fake_connect(**_kwargs):
        return _FakeClient(fake_process)

    monkeypatch.setattr(router_webssh, "_load_platform_admin", lambda _db, _ws: object())
    monkeypatch.setattr(router_webssh.asyncssh, "connect", _fake_connect)

    app = FastAPI()
    app.include_router(router_webssh.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webssh/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 22,
                        "username": "root",
                        "password": "secret",
                    }
                )
            )

            payload = json.loads(ws.receive_text())
            assert payload["type"] == "connected"

            ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 40}))
            ws.send_text(json.dumps({"type": "input", "data": "echo ok\n"}))

    assert "echo ok\n" in fake_process.stdin.writes


class _FakeImmediateCloseProcess(_FakeProcess):
    async def wait_closed(self) -> None:
        return None


class _FakeFallbackClient:
    def __init__(self) -> None:
        self.options_history: list[dict] = []
        self._interactive = _FakeProcess()

    async def create_process(self, **kwargs):
        self.options_history.append(kwargs)
        if len(self.options_history) == 1:
            return _FakeImmediateCloseProcess()
        return self._interactive

    def close(self) -> None:
        self._interactive.close()

    async def wait_closed(self) -> None:
        return None


def test_webssh_retries_with_explicit_shell_when_default_process_exits_immediately(monkeypatch, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey\n", encoding="utf-8")
    monkeypatch.setattr(settings, "WEBSSH_KNOWN_HOSTS_FILE", str(known_hosts))

    fake_client = _FakeFallbackClient()

    async def _fake_connect(**_kwargs):
        return fake_client

    monkeypatch.setattr(router_webssh, "_load_platform_admin", lambda _db, _ws: object())
    monkeypatch.setattr(router_webssh.asyncssh, "connect", _fake_connect)

    app = FastAPI()
    app.include_router(router_webssh.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webssh/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 22,
                        "username": "root",
                        "password": "secret",
                    }
                )
            )

            payload = json.loads(ws.receive_text())
            assert payload["type"] == "connected"

            ws.send_text(json.dumps({"type": "input", "data": "whoami\n"}))

    assert fake_client.options_history[0] == {"term_type": "xterm-256color", "term_size": (120, 30)}
    assert fake_client.options_history[1]["command"] == "/bin/bash -li"
    assert "whoami\n" in fake_client._interactive.stdin.writes


def test_webssh_reports_error_when_all_shell_attempts_exit_immediately(monkeypatch, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey\n", encoding="utf-8")
    monkeypatch.setattr(settings, "WEBSSH_KNOWN_HOSTS_FILE", str(known_hosts))

    class _AlwaysCloseClient:
        async def create_process(self, **_kwargs):
            return _FakeImmediateCloseProcess()

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def _fake_connect(**_kwargs):
        return _AlwaysCloseClient()

    monkeypatch.setattr(router_webssh, "_load_platform_admin", lambda _db, _ws: object())
    monkeypatch.setattr(router_webssh.asyncssh, "connect", _fake_connect)

    app = FastAPI()
    app.include_router(router_webssh.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webssh/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 22,
                        "username": "root",
                        "password": "secret",
                    }
                )
            )

            payload = json.loads(ws.receive_text())
            assert payload["type"] == "error"
            assert "立即结束" in payload["message"]


def test_webssh_requires_password_when_password_auth_selected(monkeypatch, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey\n", encoding="utf-8")
    monkeypatch.setattr(settings, "WEBSSH_KNOWN_HOSTS_FILE", str(known_hosts))

    monkeypatch.setattr(router_webssh, "_load_platform_admin", lambda _db, _ws: object())

    app = FastAPI()
    app.include_router(router_webssh.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webssh/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 22,
                        "username": "root",
                        "authMethod": "password",
                    }
                )
            )

            payload = json.loads(ws.receive_text())
            assert payload["type"] == "error"
            assert "请输入密码" in payload["message"]


def test_webssh_uses_only_password_auth_when_requested(monkeypatch, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey\n", encoding="utf-8")
    monkeypatch.setattr(settings, "WEBSSH_KNOWN_HOSTS_FILE", str(known_hosts))

    fake_process = _FakeProcess()
    captured_kwargs: dict = {}

    async def _fake_connect(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeClient(fake_process)

    monkeypatch.setattr(router_webssh, "_load_platform_admin", lambda _db, _ws: object())
    monkeypatch.setattr(router_webssh.asyncssh, "connect", _fake_connect)

    app = FastAPI()
    app.include_router(router_webssh.router)

    with TestClient(app) as client:
        with client.websocket_connect(f"{settings.API_PREFIX}/platform/webssh/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": 22,
                        "username": "root",
                        "password": "secret",
                        "authMethod": "password",
                    }
                )
            )

            payload = json.loads(ws.receive_text())
            assert payload["type"] == "connected"

    assert captured_kwargs["preferred_auth"] == "password"
    assert captured_kwargs["client_keys"] == []
    assert captured_kwargs["password"] == "secret"
