from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import asyncssh
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import read_session_from_cookie
from app.data.db import SessionLocal
from app.data.models.users import User

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/webssh",
    tags=["Platform / WebSSH"],
)


async def _send_json(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))


def _load_platform_admin(db: Session, websocket: WebSocket) -> User | None:
    raw_cookie = websocket.cookies.get(settings.COOKIE_NAME)
    session_data = read_session_from_cookie(raw_cookie)
    if not session_data or not session_data.get("id"):
        return None

    user = db.get(User, int(session_data["id"]))
    if not user or user.deleted_at is not None or not user.is_active:
        return None

    if not bool(user.is_platform_admin):
        return None

    return user


@router.websocket("/ws")
async def webssh_proxy(websocket: WebSocket):
    await websocket.accept()

    db = SessionLocal()
    user = _load_platform_admin(db, websocket)
    db.close()

    if not user:
        await _send_json(websocket, {"type": "error", "message": "仅平台管理员可访问 WebSSH。"})
        await websocket.close(code=1008)
        return

    try:
        init_raw = await websocket.receive_text()
    except WebSocketDisconnect:
        return

    try:
        init = json.loads(init_raw)
        host = str(init.get("host") or "").strip()
        username = str(init.get("username") or "").strip()
        password = init.get("password")
        private_key = init.get("privateKey")
        passphrase = init.get("passphrase")
        port = int(init.get("port") or 22)
    except Exception:
        await _send_json(websocket, {"type": "error", "message": "连接参数格式错误。"})
        await websocket.close(code=1003)
        return

    if not host or not username:
        await _send_json(websocket, {"type": "error", "message": "主机和用户名不能为空。"})
        await websocket.close(code=1003)
        return

    if (not password) and (not private_key):
        await _send_json(websocket, {"type": "error", "message": "请至少提供密码或私钥。"})
        await websocket.close(code=1003)
        return

    client: asyncssh.SSHClientConnection | None = None
    process: asyncssh.SSHClientProcess | None = None

    async def stream_stdout() -> None:
        assert process is not None
        while True:
            chunk = await process.stdout.read(2048)
            if not chunk:
                break
            await _send_json(websocket, {"type": "data", "data": chunk})

    async def stream_stderr() -> None:
        assert process is not None
        while True:
            chunk = await process.stderr.read(512)
            if not chunk:
                break
            await _send_json(websocket, {"type": "data", "data": chunk})

    async def read_client_input() -> None:
        assert process is not None
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            msg_type = payload.get("type")
            if msg_type == "input":
                process.stdin.write(str(payload.get("data") or ""))
            elif msg_type == "resize":
                cols = int(payload.get("cols") or 120)
                rows = int(payload.get("rows") or 30)
                process.change_terminal_size(cols, rows)

    async def create_interactive_process() -> asyncssh.SSHClientProcess:
        assert client is not None
        launch_candidates: list[dict] = [
            {"term_type": "xterm-256color", "term_size": (120, 30)},
            {"command": "/bin/bash -li", "term_type": "xterm-256color", "term_size": (120, 30)},
            {"command": "/bin/sh -i", "term_type": "xterm-256color", "term_size": (120, 30)},
        ]

        for options in launch_candidates:
            candidate = await client.create_process(**options)
            try:
                await asyncio.wait_for(candidate.wait_closed(), timeout=0.25)
            except asyncio.TimeoutError:
                return candidate

            with suppress(Exception):
                candidate.close()

        raise RuntimeError("远端会话启动后立即结束，请确认账号有可用的交互式 shell。")

    try:
        known_hosts_path = str(Path(settings.WEBSSH_KNOWN_HOSTS_FILE).expanduser())
        if not Path(known_hosts_path).is_file():
            await _send_json(
                websocket,
                {
                    "type": "error",
                    "message": "WebSSH 服务器配置错误：未找到 SSH known_hosts 文件，请联系管理员。",
                },
            )
            await websocket.close(code=1011)
            return

        connect_kwargs: dict = {
            "host": host,
            "port": port,
            "username": username,
            "known_hosts": known_hosts_path,
        }
        if password:
            connect_kwargs["password"] = str(password)
        if private_key:
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(str(private_key), passphrase=passphrase or None)]

        client = await asyncssh.connect(**connect_kwargs)
        process = await create_interactive_process()

        await _send_json(websocket, {"type": "connected"})

        stdout_task = asyncio.create_task(stream_stdout())
        stderr_task = asyncio.create_task(stream_stderr())
        input_task = asyncio.create_task(read_client_input())
        process_wait_task = asyncio.create_task(process.wait_closed())

        done, pending = await asyncio.wait(
            [input_task, process_wait_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            with suppress(Exception):
                task.result()

        for task in pending:
            task.cancel()
        for task in (stdout_task, stderr_task):
            task.cancel()

        with suppress(Exception):
            await asyncio.gather(*pending, stdout_task, stderr_task, return_exceptions=True)

    except (asyncssh.Error, OSError, ValueError, RuntimeError) as exc:
        await _send_json(websocket, {"type": "error", "message": f"SSH 连接失败：{exc}"})
    except WebSocketDisconnect:
        pass
    finally:
        if process is not None:
            with suppress(Exception):
                process.stdin.write("exit\n")
            with suppress(Exception):
                process.close()
        if client is not None:
            client.close()
            with suppress(Exception):
                await client.wait_closed()
        with suppress(Exception):
            await websocket.close()
