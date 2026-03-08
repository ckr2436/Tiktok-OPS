from __future__ import annotations

import asyncio
import json
from contextlib import suppress

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
            payload = json.loads(message)
            msg_type = payload.get("type")
            if msg_type == "input":
                process.stdin.write(str(payload.get("data") or ""))
            elif msg_type == "resize":
                cols = int(payload.get("cols") or 120)
                rows = int(payload.get("rows") or 30)
                process.change_terminal_size(cols, rows)

    try:
        connect_kwargs: dict = {
            "host": host,
            "port": port,
            "username": username,
            "known_hosts": None,
        }
        if password:
            connect_kwargs["password"] = str(password)
        if private_key:
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(str(private_key), passphrase=passphrase or None)]

        client = await asyncssh.connect(**connect_kwargs)
        process = await client.create_process(term_type="xterm-256color", term_size=(120, 30))

        await _send_json(websocket, {"type": "connected"})

        tasks = [
            asyncio.create_task(stream_stdout()),
            asyncio.create_task(stream_stderr()),
            asyncio.create_task(read_client_input()),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with suppress(Exception):
                task.result()
        for task in pending:
            task.cancel()

    except (asyncssh.Error, OSError, ValueError) as exc:
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
