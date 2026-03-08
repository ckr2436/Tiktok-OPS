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
