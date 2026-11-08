"""One-shot local su authentication. No credential persistence or command interpolation."""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import stat
import struct
import termios
import fcntl
import logging
from pathlib import Path

HANDSHAKE_TIMEOUT = 30.0
logger = logging.getLogger(__name__)
_UNREAPED_ROOT: dict[int, asyncio.Task] = {}


def track_unreaped(pid: int, session_id: str) -> None:
    if pid in _UNREAPED_ROOT:
        return
    logger.error("Root su cleanup unconfirmed: pid=%s session_id=%s", pid, session_id)
    async def reap():
        while True:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done:
                break
            await asyncio.sleep(1)
        logger.info("Root su delayed cleanup complete: pid=%s session_id=%s", pid, session_id)
    task = asyncio.create_task(reap())
    _UNREAPED_ROOT[pid] = task
    task.add_done_callback(lambda _: _UNREAPED_ROOT.pop(pid, None))


class WebSocketFrameRedactor(logging.Filter):
    """Redact transport frame bodies BEFORE Frame.__str__ can expose secrets.

    Uvicorn 0.38 / websockets 15 uses this exact uvicorn.error DEBUG contract.
    Keep connection, authentication, warning and error records unchanged.
    """
    _gmv_secret_safe_frames = True

    def filter(self, record):
        if (record.name == "uvicorn.error" and record.levelno <= logging.DEBUG
            and record.msg in ("< %s", "> %s") and isinstance(record.args, tuple)
            and len(record.args) == 1):
            frame = record.args[0]
            opcode = getattr(frame, "opcode", None)
            data = getattr(frame, "data", None)
            if opcode is not None and isinstance(data, (bytes, bytearray, memoryview)):
                direction = record.msg[0]
                # Do not stringify the frame or decode its data, even temporarily.
                record.msg = "%s WS_FRAME opcode=%s bytes=%s [payload redacted]"
                record.args = (direction, int(opcode), len(data))
        return True


def install_frame_log_redaction():
    target = logging.getLogger("uvicorn.error")
    if not any(getattr(f, "_gmv_secret_safe_frames", False) for f in target.filters):
        target.addFilter(WebSocketFrameRedactor())


SU = Path('/usr/bin/su')


def su_argv(marker: str) -> list[str]:
    if len(marker) != 64 or any(c not in '0123456789abcdef' for c in marker):
        raise ValueError('Invalid handshake marker')
    st = SU.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or not st.st_mode & stat.S_ISUID or st.st_mode & 0o022:
        raise ValueError('Invalid system su executable')
    # Only server-generated marker is interpolated; NEVER a password or command supplied by the browser.
    command = f"test \"$(/usr/bin/id -u)\" = 0 || exit 126; printf '\\036GMVROOT:{marker}\\037'; exec /bin/bash -li"
    return [str(SU), '--pty', '--login', '--shell', '/bin/bash', '--command', command, 'root']


class RootSu:
    def __init__(self, spawn, env, cwd, on_ready, on_data, on_exit, *, session_id="", on_auth=None):
        self.session_id = session_id
        self.on_auth = on_auth
        self.auth_attempt = secrets.token_hex(32)
        self.auth_requested = False
        self.auth_submitted = False
        self.auth_deadline = 0.0
        self.spawn, self.env, self.cwd = spawn, env, cwd
        self.on_ready, self.on_data, self.on_exit = on_ready, on_data, on_exit
        self.pid = self.fd = None
        self.task = None
        self.ready = False
        self.closed = False
        self.close_requested = False
        self.finish_task = None
        self.queue = asyncio.Queue(maxsize=32)
        self.reader_paused = False

    def start(self, cols: int, rows: int):
        marker = secrets.token_hex(32)
        argv = su_argv(marker)
        self.auth_deadline = asyncio.get_running_loop().time() + HANDSHAKE_TIMEOUT
        env = {**self.env, 'LANG': 'C', 'LC_ALL': 'C'}
        self.pid, self.fd = self.spawn(argv, cwd=self.cwd, env=env)
        self.resize(cols, rows)
        asyncio.get_running_loop().add_reader(self.fd, self._readable)
        self.task = asyncio.create_task(self._run(f'\x1eGMVROOT:{marker}\x1f'.encode()))

    def _readable(self):
        if self.fd is None:
            return
        if self.queue.full():
            asyncio.get_running_loop().remove_reader(self.fd)
            self.reader_paused = True
            return
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            data = b''
        self.queue.put_nowait(data)
        if not data:
            asyncio.get_running_loop().remove_reader(self.fd)

    async def _run(self, marker: bytes):
        reason = 'root_exit'
        before = b''
        deadline = self.auth_deadline
        try:
            while True:
                if self.close_requested:
                    reason = "root_closed"
                    break
                if not self.ready and asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError
                timeout = max(0.01, deadline - asyncio.get_running_loop().time()) if not self.ready else None
                data = await asyncio.wait_for(self.queue.get(), timeout)
                if self.reader_paused and self.fd is not None:
                    self.reader_paused = False
                    asyncio.get_running_loop().add_reader(self.fd, self._readable)
                if not data:
                    if self.close_requested:
                        reason = "root_closed"
                    elif not self.ready:
                        reason = 'root_auth_failed'
                    break
                if not self.ready:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise asyncio.TimeoutError
                    before = (before + data)[-16384:]
                    pos = before.find(marker)
                    if pos < 0:
                        if self.auth_submitted and b'authentication failure' in before.lower():
                            reason = 'root_auth_failed'
                            break
                        if not self.auth_requested and b'Password:' in before and self._echo_disabled():
                            self.auth_requested = True
                            if self.on_auth:
                                await self.on_auth(self.auth_attempt, max(1, int(deadline - asyncio.get_running_loop().time())))
                        continue
                    data = before[pos + len(marker):]
                    before = b''
                    self.ready = True
                    await self.on_ready()
                if data:
                    await self.on_data(data)
        except asyncio.TimeoutError:
            reason = 'root_auth_failed'
        except asyncio.CancelledError:
            reason = 'root_closed'
        except Exception:
            reason = 'root_failed'
        finally:
            # A single shielded finalizer owns PID/fd cleanup and the mode callback.
            async def finish():
                final_reason = reason
                try:
                    await self._cleanup()
                except Exception:
                    final_reason = 'root_cleanup_failed'
                await self.on_exit(final_reason)
            self.finish_task = asyncio.create_task(finish())
            await asyncio.shield(self.finish_task)

    def _echo_disabled(self) -> bool:
        if self.fd is None:
            return False
        try:
            return not bool(termios.tcgetattr(self.fd)[3] & termios.ECHO)
        except (OSError, termios.error):
            return False

    async def submit_secret(self, attempt_id: str, secret: str) -> None:
        if (not self.auth_requested or self.auth_submitted or self.ready or self.closed
            or self.close_requested or attempt_id != self.auth_attempt
            or asyncio.get_running_loop().time() >= self.auth_deadline
            or not self._echo_disabled()):
            raise ValueError('Authentication attempt unavailable')
        if not isinstance(secret, str) or not secret or any(c in secret for c in ('\r', '\n', '\x00')):
            raise ValueError('Invalid authentication input')
        buf = bytearray(secret, 'utf-8')
        try:
            if len(buf) > 1024:
                raise ValueError('Authentication input too long')
            buf.append(10)
            self.auth_submitted = True  # A failed/partial write is never retried.
            written = os.write(self.fd, buf)
            if written != len(buf):
                raise ValueError('Incomplete authentication input')
        finally:
            buf[:] = b'\x00' * len(buf)
            secret = None

    def resize(self, cols: int, rows: int):
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    async def write(self, data: bytes):
        if not self.ready or self.closed or self.fd is None:
            raise ValueError('Root su not ready')
        # Small nonblocking writes; no retries across a mode change.
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.fd, view)
                view = view[written:]
            except BlockingIOError:
                await asyncio.sleep(0.01)
                if self.closed or self.fd is None:
                    raise ValueError('Root su closed')

    async def _cleanup(self):
        self.ready = False
        self.auth_attempt = None
        self.closed = True
        if self.fd is not None:
            asyncio.get_running_loop().remove_reader(self.fd)
        pid = self.pid
        signal_denied = False
        def close_master():
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        async def reap_for(seconds):
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    self.pid = None
                    return True
                if done:
                    self.pid = None
                    return True
                await asyncio.sleep(.02)
            return False
        try:
            if pid is None:
                return
            # util-linux su forwards TERM to its child and waits up to 2s before
            # KILL. Keep its PTY alive and allow 3s so we do not kill the supervisor
            # before it can clean up the privileged child.
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                signal_denied = True
                close_master()
            done = await reap_for(3)
            if not done:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    signal_denied = True
                close_master()
                done = await reap_for(1)
            if not done:
                track_unreaped(pid, self.session_id)
                raise RuntimeError('Root su process cleanup unconfirmed')
            if signal_denied:
                logger.error("Root su cleanup signal denied: pid=%s session_id=%s", pid, self.session_id)
                raise RuntimeError('Root su cleanup signal was denied')
        finally:
            close_master()

    async def close(self):
        self.close_requested = True
        self.ready = False
        if self.task and self.task is not asyncio.current_task():
            if not self.task.done():
                # Wake the consumer, but never cancel it during cleanup/on_exit.
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.queue.put_nowait(b'')
            await asyncio.shield(self.task)
            if self.finish_task:
                await asyncio.shield(self.finish_task)
        elif not self.task:
            if not self.finish_task:
                self.finish_task = asyncio.create_task(self._cleanup())
            await asyncio.shield(self.finish_task)
