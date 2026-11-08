from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import fcntl
import json
import logging
import os
import select
import secrets
import shlex
import shutil
import signal
import struct
import termios
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import read_session_from_cookie
from app.data.db import SessionLocal
from app.data.models.users import User
from app.features.platform.webshell_su import RootSu, install_frame_log_redaction

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/webshell",
    tags=["Platform / WebShell"],
)

logger = logging.getLogger(__name__)
install_frame_log_redaction()

_ALLOWED_SHELL_ARGS = {"-i", "-l", "-li", "-il", "--login"}
_CLOSE_POLICY_VIOLATION = 1008
_CLOSE_INTERNAL_ERROR = 1011


@dataclass(slots=True)
class _ActiveSession:
    session_id: str
    user_id: int | None
    client_ip: str | None
    started_at: float = field(default_factory=time.monotonic)


_ACTIVE_SESSIONS: dict[str, _ActiveSession] = {}
_ACTIVE_LOCK = asyncio.Lock()
_ROOT_ATTEMPTS: dict[int, list[float]] = {}


async def _allow_root_attempt(user_id: int) -> bool:
    async with _ACTIVE_LOCK:
        now = time.monotonic()
        for key in list(_ROOT_ATTEMPTS):
            _ROOT_ATTEMPTS[key] = [t for t in _ROOT_ATTEMPTS[key] if now - t < 300]
            if not _ROOT_ATTEMPTS[key]:
                _ROOT_ATTEMPTS.pop(key, None)
        attempts = _ROOT_ATTEMPTS.setdefault(user_id, [])
        if len(attempts) >= 3 or (attempts and now - attempts[-1] < 30):
            return False
        attempts.append(now)
        return True


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def _safe_send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    with suppress(Exception):
        await asyncio.wait_for(_send_json(websocket, payload), timeout=5)


def _client_ip(websocket: WebSocket) -> str | None:
    forwarded_for = websocket.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    if websocket.client:
        return websocket.client.host
    return None


def _user_agent(websocket: WebSocket) -> str | None:
    value = websocket.headers.get("user-agent")
    if not value:
        return None
    return value[:255]


def _load_platform_admin(db: Session, websocket: WebSocket) -> User | None:
    raw_cookie = websocket.cookies.get(settings.COOKIE_NAME)
    session_data = read_session_from_cookie(raw_cookie)
    if not session_data or not session_data.get("id"):
        return None

    with suppress(Exception):
        user_id = int(session_data["id"])
        user = db.get(User, user_id)
        if not user or user.deleted_at is not None or not user.is_active:
            return None
        if not bool(user.is_platform_admin):
            return None
        return user
    return None


def _settings_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _raw_setting(name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    if value is not None:
        return value
    return os.getenv(name, default)


def _setting_str(name: str, default: str) -> str:
    return str(_raw_setting(name, default) or default)


def _setting_bool(name: str, default: bool) -> bool:
    value = _raw_setting(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(name: str, default: int) -> int:
    try:
        return int(_raw_setting(name, default))
    except Exception:
        return default


def _setting_float(name: str, default: float) -> float:
    try:
        return float(_raw_setting(name, default))
    except Exception:
        return default


def _setting_list(name: str, default: list[str]) -> list[str]:
    value = _raw_setting(name, default)
    parsed = _settings_list(value)
    return parsed or default


def _same_origin(origin: str, host: str | None) -> bool:
    if not origin or not host:
        return False
    parsed = urlparse(origin)
    return parsed.netloc.lower() == host.lower()


def _origin_allowed(websocket: WebSocket) -> bool:
    """Reject browser-origin CSWSH while still allowing same-origin admin UI traffic."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True

    host = websocket.headers.get("host")
    if _same_origin(origin, host):
        return True

    allowed = {item.rstrip("/") for item in _setting_list("CORS_ORIGINS", [])}
    issuer = (settings.ISSUER or "").strip().rstrip("/")
    if issuer:
        allowed.add(issuer)

    wildcard_allowed = "*" in allowed
    env = str(settings.ENV or "").lower()
    if wildcard_allowed and env not in {"prod", "production"}:
        return True

    return origin.rstrip("/") in {item for item in allowed if item != "*"}


def _normalise_terminal_input(data: str) -> str:
    """Normalize textarea/script command input only. Do not use for PTY raw input."""
    if not data:
        return ""
    return data.replace("\r\n", "\n").replace("\r", "\n")


def _coerce_raw_terminal_input(value: Any) -> str:
    """
    Backward-compatible legacy string input. New frontend uses input_b64 so
    underscores, control bytes and IME-sensitive characters bypass text-layer
    rewriting entirely.
    """
    if value is None:
        return ""
    return str(value)


def _decode_terminal_input_bytes(payload: dict[str, Any]) -> bytes:
    """
    Decode terminal input from the WebSocket payload.

    Prefer base64 byte transport. It prevents characters such as underscore and
    raw terminal control bytes from being changed by browser, IME, JSON/string,
    middleware, or logging layers before reaching the PTY. Keep legacy `input`
    support only for older cached frontend bundles.
    """
    if payload.get("type") == "input_b64":
        raw_b64 = str(payload.get("data_b64") or "")
        if not raw_b64:
            return b""
        try:
            return base64.b64decode(raw_b64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise ValueError("input_b64 payload is not valid base64") from exc

    data = _coerce_raw_terminal_input(payload.get("data"))
    if data == "":
        return b""
    return data.encode("utf-8", errors="ignore")


def _clamp_resize(cols: Any, rows: Any) -> tuple[int, int]:
    with suppress(Exception):
        c = int(cols)
        r = int(rows)
        return max(20, min(c, 300)), max(5, min(r, 120))
    return 120, 30


def _allowed_shell_realpaths() -> dict[str, str]:
    allowed: dict[str, str] = {}
    for item in _setting_list("WEBSHELL_ALLOWED_SHELLS", ["/bin/bash", "/bin/sh"]):
        parts = shlex.split(item)
        if not parts:
            continue
        executable = parts[0]
        resolved = executable if executable.startswith("/") else shutil.which(executable)
        if not resolved:
            continue
        real = os.path.realpath(resolved)
        allowed[real] = executable
    return allowed


def _resolve_shell(requested_shell: str | None) -> list[str]:
    requested = (requested_shell or _setting_str("WEBSHELL_DEFAULT_SHELL", "/bin/bash -li") or "").strip()
    if not requested:
        requested = "/bin/bash -li"

    try:
        parts = shlex.split(requested)
    except ValueError as exc:
        raise ValueError("shell 参数格式不正确。") from exc

    if not parts:
        raise ValueError("shell 参数不能为空。")

    executable = parts[0]
    resolved = executable if executable.startswith("/") else shutil.which(executable)
    if not resolved:
        raise ValueError("指定 shell 不存在。")

    real_executable = os.path.realpath(resolved)
    allowed = _allowed_shell_realpaths()
    if allowed and real_executable not in allowed:
        raise ValueError("指定 shell 不在生产允许列表中。")

    extra_args = parts[1:]
    if any(arg not in _ALLOWED_SHELL_ARGS for arg in extra_args):
        raise ValueError("shell 只允许交互/登录参数，禁止 -c 等命令参数。")

    if not extra_args:
        basename = os.path.basename(real_executable)
        extra_args = ["-li"] if basename in {"bash", "zsh"} else ["-i"]

    return [real_executable, *extra_args]


def _webshell_env(shell_path: str, session_id: str) -> dict[str, str]:
    path = os.environ.get(
        "PATH",
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    )
    home = os.environ.get("HOME") or "/tmp"
    lang = os.environ.get("LANG") or "C.UTF-8"
    return {
        "PATH": path,
        "HOME": home,
        "LANG": lang,
        "LC_ALL": os.environ.get("LC_ALL") or lang,
        "TERM": _setting_str("WEBSHELL_TERM", "xterm-256color"),
        "COLORTERM": "truecolor",
        "SHELL": shell_path,
        "GMV_WEBSHELL_SESSION_ID": session_id,
    }


def _webshell_cwd() -> str:
    configured = str(_setting_str("WEBSHELL_CWD", "") or "").strip()
    candidate = configured or os.getcwd()
    candidate = os.path.realpath(candidate)
    if os.path.isdir(candidate):
        return candidate
    return os.getcwd()


def _spawn_pty(argv: list[str], *, cwd: str, env: dict[str, str]) -> tuple[int, int]:
    """Spawn the shell in a real controlling PTY for interactive terminal apps."""
    pid, master_fd = os.forkpty()
    if pid == 0:  # child
        try:
            os.chdir(cwd)
            os.execve(argv[0], argv, env)
        except BaseException:
            os._exit(127)
    os.set_blocking(master_fd, False)
    return pid, master_fd


def _set_winsize(master_fd: int, cols: int, rows: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)


def _read_pty(master_fd: int, chunk_size: int) -> bytes:
    readable, _, _ = select.select([master_fd], [], [], 0.5)
    if not readable:
        return b""
    return os.read(master_fd, chunk_size)


def _waitpid_returncode(pid: int) -> int | None:
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        return None
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -int(os.WTERMSIG(status))
    return None


async def _poll_child_returncode(pid: int) -> int | None:
    # Cancellable polling avoids an executor thread blocked forever in waitpid.
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if done:
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            if os.WIFSIGNALED(status):
                return -int(os.WTERMSIG(status))
            return None
        await asyncio.sleep(0.05)


async def _register_session(session: _ActiveSession) -> bool:
    max_sessions = max(0, int(_setting_int("WEBSHELL_MAX_SESSIONS", 2)))
    max_per_user = max(0, int(_setting_int("WEBSHELL_MAX_SESSIONS_PER_USER", 1)))
    async with _ACTIVE_LOCK:
        if max_sessions and len(_ACTIVE_SESSIONS) >= max_sessions:
            return False
        if max_per_user and session.user_id is not None:
            active_for_user = sum(1 for item in _ACTIVE_SESSIONS.values() if item.user_id == session.user_id)
            if active_for_user >= max_per_user:
                return False
        _ACTIVE_SESSIONS[session.session_id] = session
        return True


async def _unregister_session(session_id: str) -> None:
    async with _ACTIVE_LOCK:
        _ACTIVE_SESSIONS.pop(session_id, None)


def _write_audit(action: str, websocket: WebSocket, user: User | None, details: dict[str, Any]) -> None:
    try:
        from app.data.models.audit_logs import AuditLog

        db = SessionLocal()
        try:
            db.add(
                AuditLog(
                    actor_user_id=getattr(user, "id", None),
                    actor_ip=_client_ip(websocket),
                    user_agent=_user_agent(websocket),
                    action=action[:64],
                    resource_type="webshell_session",
                    resource_id=None,
                    details=details,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to write WebShell audit event: action=%s", action)


async def _audit(action: str, websocket: WebSocket, user: User | None, details: dict[str, Any]) -> None:
    safe_details = dict(details)
    safe_details.pop("input", None)
    safe_details.pop("data", None)
    safe_details.pop("data_b64", None)
    safe_details.pop("secret", None)
    safe_details.pop("password", None)
    try:
        await asyncio.wait_for(asyncio.to_thread(_write_audit, action, websocket, user, safe_details), timeout=3)
    except asyncio.TimeoutError:
        logger.warning("WebShell audit timeout: action=%s", action)


async def _receive_init(websocket: WebSocket) -> dict[str, Any]:
    try:
        message = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=max(1.0, float(_setting_float("WEBSHELL_INIT_TIMEOUT_SECONDS", 5.0))),
        )
    except asyncio.TimeoutError:
        return {}
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return {}

    if not isinstance(payload, dict):
        return {}
    if payload.get("type") in {None, "init"} or "shell" in payload:
        return payload
    return {}


async def _webshell_proxy_impl(websocket: WebSocket, *, admin_loader=None, raw_output: bool = False, pty_spawner=None) -> None:
    load_admin = admin_loader or _load_platform_admin
    spawn_pty = pty_spawner or _spawn_pty
    session_id = uuid.uuid4().hex
    client_ip = _client_ip(websocket)

    logger.info(
        "WebShell WS incoming: session_id=%s path=%s client_ip=%s origin=%s host=%s",
        session_id,
        websocket.url.path,
        client_ip,
        websocket.headers.get("origin"),
        websocket.headers.get("host"),
    )

    await websocket.accept()

    user: User | None = None
    registered = False
    master_fd: int | None = None
    pid: int | None = None
    returncode: int | None = None
    started_at = time.monotonic()
    last_input_at = started_at
    bytes_in = 0
    bytes_out = 0
    close_reason = "disconnect"
    root_session: RootSu | None = None
    credential_intent: str | None = None
    credential_deadline = 0.0
    shutting_down = False
    mode = "gmv"
    mode_epoch = 0
    mode_token = uuid.uuid4().hex
    mode_v1 = False
    background_output = bytearray()
    background_truncated = False

    try:
        if not _setting_bool("WEBSHELL_ENABLED", False):
            close_reason = "disabled"
            await _safe_send_json(websocket, {"type": "error", "message": "WebShell 未启用。请在生产环境显式配置 WEBSHELL_ENABLED=true。"})
            await websocket.close(code=_CLOSE_POLICY_VIOLATION)
            return

        if not _origin_allowed(websocket):
            close_reason = "bad_origin"
            await _safe_send_json(websocket, {"type": "error", "message": "WebShell Origin 校验失败。"})
            await _audit("webshell.rejected", websocket, None, {"session_id": session_id, "reason": close_reason})
            await websocket.close(code=_CLOSE_POLICY_VIOLATION)
            return

        db = SessionLocal()
        try:
            user = load_admin(db, websocket)
        finally:
            db.close()

        if not user:
            close_reason = "auth_failed"
            await _safe_send_json(websocket, {"type": "error", "message": "仅平台管理员可访问 WebShell。"})
            await _audit("webshell.rejected", websocket, None, {"session_id": session_id, "reason": close_reason})
            await websocket.close(code=_CLOSE_POLICY_VIOLATION)
            return

        active_session = _ActiveSession(
            session_id=session_id,
            user_id=getattr(user, "id", None),
            client_ip=client_ip,
        )
        registered = await _register_session(active_session)
        if not registered:
            close_reason = "too_many_sessions"
            await _safe_send_json(websocket, {"type": "error", "message": "WebShell 会话数已达到上限，请关闭其他会话后重试。"})
            await _audit("webshell.rejected", websocket, user, {"session_id": session_id, "reason": close_reason})
            await websocket.close(code=_CLOSE_POLICY_VIOLATION)
            return

        init_payload = await _receive_init(websocket)
        mode_v1 = init_payload.get("control_version") in (1, 2)
        root_enabled = init_payload.get("control_version") == 2 and _setting_bool("WEBSHELL_ROOT_SU_ENABLED", False)
        cols, rows = _clamp_resize(init_payload.get("cols"), init_payload.get("rows"))
        argv = _resolve_shell(str(init_payload.get("shell") or _setting_str("WEBSHELL_DEFAULT_SHELL", "/bin/bash -li")))
        cwd = _webshell_cwd()
        env = _webshell_env(argv[0], session_id)

        pid, master_fd = spawn_pty(argv, cwd=cwd, env=env)
        with suppress(Exception):
            _set_winsize(master_fd, cols, rows)

        await _safe_send_json(
            websocket,
            {
                "type": "connected",
                "session_id": session_id,
                "shell": os.path.basename(argv[0]),
                "cols": cols,
                "rows": rows,
                "root_enabled": root_enabled,
                "output_encoding": "base64" if raw_output else "utf-8-replace",
                "runtime_uid": os.getuid(), "runtime_gid": os.getgid(),
                "execution_host": os.uname().nodename,
                "mode": "gmv", "mode_epoch": mode_epoch,
                "mode_token": mode_token if mode_v1 else None,
            },
        )
        await _audit(
            "webshell.started",
            websocket,
            user,
            {
                "session_id": session_id,
                "shell": argv[0],
                "cwd": cwd,
                "cols": cols,
                "rows": rows,
            },
        )

        close_event = asyncio.Event()
        child_done = asyncio.Event()
        output_drained = asyncio.Event()

        async def send_mode(state: str = "ready") -> None:
            await _safe_send_json(websocket, {"type": "mode", "mode": mode,
                "state": state, "mode_epoch": mode_epoch,
                "execution_uid": 0 if mode == "root" else os.getuid() if mode == "gmv" else None})

        async def root_ready() -> None:
            nonlocal mode, mode_epoch
            mode = "root"
            mode_epoch += 1
            await send_mode()
            await _audit("webshell.root.started", websocket, user, {"session_id": session_id})

        async def send_output(data: bytes) -> None:
            payload = ({"type": "data_b64", "data_b64": base64.b64encode(data).decode("ascii")}
                if raw_output else {"type": "data", "data": data.decode("utf-8", errors="replace")})
            await asyncio.wait_for(_send_json(websocket, payload), timeout=5)

        async def root_data(data: bytes) -> None:
            nonlocal bytes_out
            bytes_out += len(data)
            await send_output(data)

        async def root_exit(reason: str) -> None:
            nonlocal root_session, mode, mode_epoch, background_truncated, credential_intent, credential_deadline
            root_session = None
            credential_intent = None
            credential_deadline = 0.0
            mode = "gmv"
            mode_epoch += 1
            await _audit("webshell.root.closed", websocket, user, {"session_id": session_id, "reason": reason})
            if shutting_down:
                return
            if reason == "root_cleanup_failed":
                close_event.set()
                await websocket.close(code=_CLOSE_INTERNAL_ERROR)
                return
            await send_mode()
            if reason in {"root_auth_failed", "root_failed"}:
                await _safe_send_json(websocket, {"type": "mode_error", "code": "root_failed"})
            if background_truncated:
                await send_output("\r\n[gmv 后台输出超出缓存，已省略较早部分]\r\n".encode())
                background_truncated = False
            if background_output:
                data = bytes(background_output)
                background_output.clear()
                await send_output(data)

        async def switch_mode(payload: dict[str, Any]) -> None:
            nonlocal root_session, mode, mode_epoch, credential_intent, credential_deadline
            if not root_enabled or payload.get("mode_token") != mode_token or payload.get("mode_epoch") != mode_epoch:
                await _safe_send_json(websocket, {"type": "mode_error", "code": "mode_denied"})
                await send_mode("switching" if mode == "switching" else "ready")
                return
            target = payload.get("target")
            if target == "gmv":
                if root_session:
                    await root_session.close()
                elif credential_intent is not None or mode == "switching":
                    await root_exit("credential_entry_canceled")
                else:
                    await send_mode()
                return
            # A cookie alone is insufficient: require a valid browser Origin on elevation.
            origin = websocket.headers.get("origin") or ""
            if target != "root" or mode != "gmv" or root_session or urlparse(origin).scheme != "https" or not _origin_allowed(websocket):
                await _safe_send_json(websocket, {"type": "mode_error", "code": "mode_denied"})
                await send_mode("switching" if mode == "switching" else "ready")
                return
            if not await validate_admin():
                await _safe_send_json(websocket, {"type": "mode_error", "code": "mode_denied"})
                await send_mode()
                return
            # Human credential collection is a separate, non-secret phase.
            # No su/PAM process exists until Submit; the system-auth clock remains 30s.
            credential_intent = secrets.token_hex(32)
            credential_deadline = time.monotonic() + 600
            mode = "switching"
            mode_epoch += 1
            await send_mode("switching")
            await _audit("webshell.root.entry_requested", websocket, user, {"session_id": session_id})
            await _safe_send_json(websocket, {"type": "elevation_auth", "phase": "credential_entry",
                "attempt_id": credential_intent, "expires_in": 600, "mode_epoch": mode_epoch})

        async def submit_elevation_secret(payload: dict[str, Any]) -> None:
            nonlocal root_session, credential_intent, credential_deadline
            secret = payload.pop("secret", None)
            intent = credential_intent
            epoch = mode_epoch
            target = None
            waiters = []
            try:
                if (not root_enabled or mode != "switching" or intent is None or root_session
                    or payload.get("attempt_id") != intent
                    or payload.get("mode_token") != mode_token or payload.get("mode_epoch") != epoch
                    or not await validate_admin()
                    or credential_intent != intent or root_session or mode != "switching"
                    or mode_epoch != epoch or time.monotonic() >= credential_deadline):
                    await _safe_send_json(websocket, {"type": "mode_error", "code": "auth_unavailable"})
                    return
                # Consume this intent once. Nothing is reused in any future attempt.
                credential_intent = None
                credential_deadline = 0.0
                if (not isinstance(secret, str) or not secret or any(c in secret for c in ("\r", "\n", "\x00"))
                    or len(secret.encode("utf-8")) > 1024):
                    await root_exit("root_auth_failed")
                    return
                if not await _allow_root_attempt(user.id):
                    await root_exit("root_rate_limited")
                    await _safe_send_json(websocket, {"type": "mode_error", "code": "root_rate_limited"})
                    return
                await _audit("webshell.root.auth_requested", websocket, user, {"session_id": session_id})
                auth_ready = asyncio.Event()
                verified = asyncio.Event()
                async def on_auth(_attempt, _expires):
                    auth_ready.set()
                async def on_verified():
                    verified.set()  # UID nonce already verified; slow transport/audit is not authentication.
                    await root_ready()
                target = RootSu(spawn_pty, env, cwd, on_verified, root_data, root_exit,
                    session_id=session_id, on_auth=on_auth)
                root_session = target
                target.start(cols, rows)  # The 30-second su authentication clock starts HERE.
                waiters = [asyncio.create_task(auth_ready.wait()), asyncio.create_task(verified.wait())]
                await asyncio.wait([*waiters, target.task], timeout=5, return_when=asyncio.FIRST_COMPLETED)
                if verified.is_set():
                    return  # PAM may legitimately grant entry without needing this secret.
                if (not auth_ready.is_set() or root_session is not target or mode != "switching"
                    or mode_epoch != epoch or target.closed):
                    if root_session is target:
                        await target.close()
                        await _safe_send_json(websocket, {"type": "mode_error", "code": "root_failed"})
                    return
                # RootSu again checks its actual prompt, ECHO-off, deadline and one-shot state.
                await target.submit_secret(target.auth_attempt, secret)
            except (OSError, ValueError):
                if target is not None and root_session is target:
                    await target.close()
                elif mode == "switching":
                    await root_exit("root_auth_failed")
                await _safe_send_json(websocket, {"type": "mode_error", "code": "root_failed"})
            finally:
                secret = None
                payload.clear()
                for waiter in waiters:
                    waiter.cancel()
                if waiters:
                    await asyncio.gather(*waiters, return_exceptions=True)

        async def stream_output() -> None:
            nonlocal bytes_out, close_reason, background_truncated
            assert master_fd is not None
            chunk_size = max(512, min(int(_setting_int("WEBSHELL_READ_CHUNK_BYTES", 4096)), 65536))
            while not close_event.is_set():
                try:
                    chunk = await asyncio.to_thread(_read_pty, master_fd, chunk_size)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    raise
                if not chunk:
                    if child_done.is_set():
                        break
                    continue
                bytes_out += len(chunk)
                if mode != "gmv":
                    background_output.extend(chunk)
                    if len(background_output) > 262144:
                        del background_output[:-262144]
                        background_truncated = True
                    continue
                await send_output(chunk)
            output_drained.set()
            close_reason = close_reason or "output_closed"

        async def read_client_input() -> None:
            nonlocal bytes_in, last_input_at, close_reason, cols, rows
            assert master_fd is not None
            idle_timeout = max(30.0, float(_setting_float("WEBSHELL_IDLE_TIMEOUT_SECONDS", 600.0)))
            session_timeout = max(idle_timeout, float(_setting_float("WEBSHELL_SESSION_TIMEOUT_SECONDS", 1800.0)))
            max_input_bytes = max(1, int(_setting_int("WEBSHELL_MAX_INPUT_BYTES", 8192)))

            while not close_event.is_set():
                now = time.monotonic()
                if credential_intent is not None and now >= credential_deadline:
                    await root_exit("credential_entry_expired")
                    await _safe_send_json(websocket, {"type": "mode_error", "code": "auth_unavailable"})
                if now - started_at > session_timeout:
                    close_reason = "session_timeout"
                    await _safe_send_json(websocket, {"type": "exit", "reason": close_reason})
                    close_event.set()
                    break
                if now - last_input_at > idle_timeout:
                    close_reason = "idle_timeout"
                    await _safe_send_json(websocket, {"type": "exit", "reason": close_reason})
                    close_event.set()
                    break

                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                message = ""  # Drop the raw frame; credentials are never logged or retained.
                if not isinstance(payload, dict):
                    continue

                msg_type = payload.get("type")
                if msg_type == "elevation_secret":
                    await submit_elevation_secret(payload)
                elif msg_type == "switch_mode":
                    await switch_mode(payload)
                    last_input_at = time.monotonic()
                elif msg_type in {"input", "input_b64"}:
                    if mode == "switching" or (mode_v1 and payload.get("mode_epoch") != mode_epoch):
                        await _safe_send_json(websocket, {"type": "mode_error", "code": "stale_input"})
                        continue
                    try:
                        encoded = _decode_terminal_input_bytes(payload)
                    except ValueError:
                        logger.warning("Invalid WebShell input payload: session_id=%s type=%s", session_id, msg_type)
                        continue
                    if not encoded:
                        continue
                    if len(encoded) > max_input_bytes:
                        close_reason = "input_too_large"
                        await _safe_send_json(websocket, {"type": "error", "message": "单次输入过大，WebShell 已断开。"})
                        close_event.set()
                        break
                    bytes_in += len(encoded)
                    last_input_at = time.monotonic()
                    if mode == "root":
                        if root_session and root_session.ready:
                            try:
                                await asyncio.wait_for(root_session.write(encoded), timeout=2)
                            except (asyncio.TimeoutError, OSError, ValueError):
                                if root_session:
                                    await root_session.close()
                                await _safe_send_json(websocket, {"type": "mode_error", "code": "root_input_failed"})
                    else:
                        await asyncio.to_thread(os.write, master_fd, encoded)
                elif msg_type == "resize":
                    cols, rows = _clamp_resize(payload.get("cols"), payload.get("rows"))
                    with suppress(Exception):
                        await asyncio.to_thread(_set_winsize, master_fd, cols, rows)
                        if root_session:
                            root_session.resize(cols, rows)
                elif msg_type == "pong":
                    last_input_at = time.monotonic()
                elif msg_type == "close":
                    close_reason = "client_close"
                    close_event.set()
                    break

        async def wait_child() -> None:
            nonlocal returncode, close_reason
            assert pid is not None
            returncode = await _poll_child_returncode(pid)
            child_done.set()
            if not close_event.is_set():
                try:
                    await asyncio.wait_for(output_drained.wait(), timeout=3)
                    close_reason = "process_exit"
                except asyncio.TimeoutError:
                    close_reason = "process_exit_output_incomplete"
                await _safe_send_json(websocket, {"type": "exit", "reason": close_reason, "returncode": returncode})
                close_event.set()

        async def validate_admin() -> bool:
            def check() -> bool:
                db = SessionLocal()
                try:
                    current_user = load_admin(db, websocket)
                    return bool(current_user and current_user.id == user.id)
                finally:
                    db.close()
            try:
                return await asyncio.wait_for(asyncio.to_thread(check), timeout=3)
            except Exception:
                return False

        async def auth_watchdog() -> None:
            nonlocal close_reason
            # Revocation terminates ALL modes, including any active root auth attempt.
            interval = max(2.0, min(_setting_float("WEBSHELL_AUTH_RECHECK_SECONDS", 5.0), 30.0))
            while not close_event.is_set():
                await asyncio.sleep(interval)
                if not await validate_admin():
                    close_reason = "auth_revoked"
                    close_event.set()
                    await websocket.close(code=_CLOSE_POLICY_VIOLATION)
                    break

        async def ping_client() -> None:
            interval = max(10.0, float(_setting_float("WEBSHELL_PING_INTERVAL_SECONDS", 25.0)))
            while not close_event.is_set():
                await asyncio.sleep(interval)
                await _safe_send_json(websocket, {"type": "ping", "session_id": session_id, "ts": int(time.time())})

        tasks = [
            asyncio.create_task(stream_output()),
            asyncio.create_task(read_client_input()),
            asyncio.create_task(wait_child()),
            asyncio.create_task(ping_client()),
            asyncio.create_task(auth_watchdog()),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with suppress(WebSocketDisconnect):
                task.result()
        close_event.set()
        for task in pending:
            task.cancel()
        with suppress(Exception):
            await asyncio.gather(*pending, return_exceptions=True)

    except WebSocketDisconnect:
        close_reason = "disconnect"
        logger.info("WebShell WS disconnected by client: session_id=%s", session_id)
    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
        close_reason = "error"
        logger.exception("WebShell session failed: session_id=%s error=%s", session_id, exc)
        await _safe_send_json(websocket, {"type": "error", "message": f"WebShell 启动失败：{exc}"})
        with suppress(Exception):
            await websocket.close(code=_CLOSE_INTERNAL_ERROR)
    finally:
        shutting_down = True
        async def finish_session() -> None:
            if root_session:
                with suppress(Exception):
                    await root_session.close()
            if pid is not None:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGTERM)
                with suppress(Exception):
                    await asyncio.wait_for(_poll_child_returncode(pid), timeout=2)
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
                with suppress(Exception):
                    await asyncio.wait_for(_poll_child_returncode(pid), timeout=1)
            if master_fd is not None:
                with suppress(Exception):
                    os.close(master_fd)
            if registered:
                await _unregister_session(session_id)

            duration_ms = int((time.monotonic() - started_at) * 1000)
            await _audit(
                "webshell.closed",
                websocket,
                user,
                {
                    "session_id": session_id,
                    "reason": close_reason,
                    "returncode": returncode,
                    "duration_ms": duration_ms,
                    "bytes_in": bytes_in,
                    "bytes_out": bytes_out,
                },
            )
            with suppress(Exception):
                await websocket.close()
            logger.info(
                "WebShell WS closed: session_id=%s user_id=%s reason=%s returncode=%s duration_ms=%s bytes_in=%s bytes_out=%s",
                session_id,
                getattr(user, "id", None),
                close_reason,
                returncode,
                duration_ms,
                bytes_in,
                bytes_out,
            )
        # Ensure browser disconnect cannot interrupt cleanup halfway through.
        cleanup_task = asyncio.create_task(finish_session())
        with suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup_task)


def _upgrade_hint_detail(path: str) -> dict[str, Any]:
    return {
        "message": "该接口仅支持 WebSocket 握手，请检查反向代理是否正确转发 Upgrade 头。",
        "expected_path": path,
        "required_headers": ["Upgrade: websocket", "Connection: upgrade"],
        "production_notes": [
            "生产环境必须显式配置 WEBSHELL_ENABLED=true 才会启用。",
            "建议反向代理关闭响应缓冲，并设置 proxy_read_timeout 大于 WEBSHELL_SESSION_TIMEOUT_SECONDS。",
            "Origin 必须与后台同源，或加入 CORS_ORIGINS/ISSUER 白名单。",
        ],
    }


@router.websocket("/ws")
async def webshell_proxy(websocket: WebSocket) -> None:
    await _webshell_proxy_impl(websocket)


@router.get("/ws")
async def webshell_ws_upgrade_hint() -> JSONResponse:
    raise HTTPException(status_code=426, detail=_upgrade_hint_detail(f"{settings.API_PREFIX}/platform/webshell/ws"))

