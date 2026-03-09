from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import shlex
import struct
import termios
from contextlib import suppress

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import read_session_from_cookie
from app.data.db import SessionLocal
from app.data.models.users import User

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/webshell",
    tags=["Platform / WebShell"],
)

legacy_router = APIRouter(
    prefix="/api/platform/webshell",
    tags=["Platform / WebShell"],
)

logger = logging.getLogger(__name__)


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


async def _webshell_proxy_impl(websocket: WebSocket):
    logger.info(
        "WebShell WS incoming connection: path=%s query=%s client=%s headers=%s",
        websocket.url.path,
        websocket.url.query,
        websocket.client,
        {
            "origin": websocket.headers.get("origin"),
            "host": websocket.headers.get("host"),
            "upgrade": websocket.headers.get("upgrade"),
            "connection": websocket.headers.get("connection"),
            "user-agent": websocket.headers.get("user-agent"),
        },
    )
    await websocket.accept()
    logger.info("WebShell WS handshake accepted: path=%s", websocket.url.path)

    db = SessionLocal()
    user = _load_platform_admin(db, websocket)
    db.close()

    if not user:
        await _send_json(websocket, {"type": "error", "message": "仅平台管理员可访问 WebShell。"})
        logger.warning("WebShell WS rejected by auth: path=%s", websocket.url.path)
        await websocket.close(code=1008)
        return

    try:
        message = await websocket.receive_text()
        payload = json.loads(message)
        shell = str(payload.get("shell") or settings.WEBSHELL_DEFAULT_SHELL).strip()
    except Exception:
        shell = settings.WEBSHELL_DEFAULT_SHELL

    if not shell:
        shell = "/bin/bash -li"

    master_fd: int | None = None
    process: asyncio.subprocess.Process | None = None

    async def stream_output() -> None:
        assert master_fd is not None
        while True:
            try:
                chunk = os.read(master_fd, 2048)
            except BlockingIOError:
                await asyncio.sleep(0.03)
                continue
            if not chunk:
                break
            await _send_json(websocket, {"type": "data", "data": chunk.decode(errors="ignore")})

    async def read_client_input() -> None:
        assert master_fd is not None
        while True:
            message = await websocket.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue

            msg_type = payload.get("type")
            if msg_type == "input":
                data = str(payload.get("data") or "")
                if data:
                    await asyncio.to_thread(os.write, master_fd, data.encode())
            elif msg_type == "resize":
                cols = int(payload.get("cols") or 120)
                rows = int(payload.get("rows") or 30)
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                with suppress(Exception):
                    await asyncio.to_thread(fcntl.ioctl, master_fd, termios.TIOCSWINSZ, winsize)

    try:
        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        process = await asyncio.create_subprocess_exec(
            *shlex.split(shell),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)

        await _send_json(websocket, {"type": "connected"})
        logger.info("WebShell session started for user_id=%s", getattr(user, "id", None))

        output_task = asyncio.create_task(stream_output())
        input_task = asyncio.create_task(read_client_input())
        wait_task = asyncio.create_task(process.wait())

        done, pending = await asyncio.wait([input_task, wait_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with suppress(Exception):
                task.result()

        for task in pending:
            task.cancel()
        output_task.cancel()
        with suppress(Exception):
            await asyncio.gather(*pending, output_task, return_exceptions=True)

    except (OSError, ValueError, RuntimeError) as exc:
        logger.exception("WebShell startup failed: %s", exc)
        await _send_json(websocket, {"type": "error", "message": f"WebShell 启动失败：{exc}"})
    except WebSocketDisconnect:
        logger.info("WebShell WS disconnected by client: path=%s", websocket.url.path)
    finally:
        if process is not None and process.returncode is None:
            with suppress(Exception):
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2)
        if master_fd is not None:
            with suppress(Exception):
                os.close(master_fd)
        with suppress(Exception):
            await websocket.close()
        logger.info(
            "WebShell WS closed: path=%s process_returncode=%s",
            websocket.url.path,
            None if process is None else process.returncode,
        )


def _upgrade_hint_detail(path: str) -> dict:
    return {
        "message": "该接口仅支持 WebSocket 握手，请检查反向代理是否正确转发 Upgrade 头。",
        "expected_path": path,
        "required_headers": ["Upgrade: websocket", "Connection: upgrade"],
    }


@router.websocket("/ws")
async def webshell_proxy(websocket: WebSocket):
    await _webshell_proxy_impl(websocket)


@legacy_router.websocket("/ws")
async def legacy_webshell_proxy(websocket: WebSocket):
    logger.warning("WebShell WS legacy path hit: %s", websocket.url.path)
    await _webshell_proxy_impl(websocket)


@router.get("/ws")
async def webshell_ws_upgrade_hint() -> JSONResponse:
    raise HTTPException(status_code=426, detail=_upgrade_hint_detail(f"{settings.API_PREFIX}/platform/webshell/ws"))


@legacy_router.get("/ws")
async def legacy_webshell_ws_upgrade_hint() -> JSONResponse:
    raise HTTPException(status_code=426, detail=_upgrade_hint_detail("/api/platform/webshell/ws"))
