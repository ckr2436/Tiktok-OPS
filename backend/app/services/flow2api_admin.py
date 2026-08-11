from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from app.core.config import settings


class Flow2ApiAdminError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _credential_path() -> Path:
    directory = str(os.environ.get("CREDENTIALS_DIRECTORY") or "").strip()
    if directory:
        return Path(directory) / "flow2api_admin_password"
    # Explicit local fallback is useful for CLI diagnostics.  Production
    # Gunicorn receives the same secret through LoadCredential.
    return Path("/opt/apps/flow2api-omni/credentials/admin_password")


def _read_admin_password() -> str:
    path = _credential_path()
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise Flow2ApiAdminError(
            "Flow2API 管理凭据不可用，请检查服务部署配置。",
            status_code=503,
        ) from exc
    if not password:
        raise Flow2ApiAdminError("Flow2API 管理凭据为空。", status_code=503)
    return password


def _validated_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise Flow2ApiAdminError(
            "Flow2API 管理地址必须是本机回环地址。", status_code=503
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Flow2ApiAdminError("Flow2API 管理地址格式无效。", status_code=503)
    return url


def _safe_error_message(response: httpx.Response) -> str:
    message = "Flow2API 管理请求失败"
    try:
        payload = response.json()
    except ValueError:
        return message
    if isinstance(payload, Mapping):
        detail = payload.get("detail") or payload.get("message")
        if isinstance(detail, str) and detail.strip():
            message = detail.strip()[:500]
        elif isinstance(detail, Mapping):
            nested = detail.get("message") or detail.get("code")
            if nested:
                message = str(nested)[:500]
    return message


class Flow2ApiAdminClient:
    """Narrow, server-side administrator for the local Flow account pool."""

    _session_token: str | None = None
    _login_lock = asyncio.Lock()

    def __init__(self, *, base_url: str | None = None, timeout: float | None = None):
        self.base_url = _validated_base_url(
            base_url or settings.FLOW2API_ADMIN_BASE_URL
        )
        self.timeout = float(timeout or settings.FLOW2API_ADMIN_TIMEOUT_SECONDS)

    async def _login(self, client: httpx.AsyncClient, *, force: bool = False) -> str:
        if self.__class__._session_token and not force:
            return self.__class__._session_token
        async with self.__class__._login_lock:
            if self.__class__._session_token and not force:
                return self.__class__._session_token
            try:
                response = await client.post(
                    "/api/admin/login",
                    json={
                        "username": str(settings.FLOW2API_ADMIN_USERNAME),
                        "password": _read_admin_password(),
                    },
                )
            except httpx.HTTPError as exc:
                raise Flow2ApiAdminError(
                    "Flow2API 管理服务连接失败。", status_code=503
                ) from exc
            if not response.is_success:
                raise Flow2ApiAdminError(
                    "Flow2API 管理认证失败。", status_code=503
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise Flow2ApiAdminError("Flow2API 管理认证响应无效。") from exc
            token = str(payload.get("token") or "").strip()
            if not token:
                raise Flow2ApiAdminError("Flow2API 未返回管理会话。")
            self.__class__._session_token = token
            return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/api/") or ".." in path:
            raise Flow2ApiAdminError("Flow2API 管理路径不在允许范围。", status_code=400)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        ) as client:
            token = await self._login(client)
            for attempt in range(2):
                try:
                    response = await client.request(
                        method.upper(),
                        path,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "Cache-Control": "no-store",
                        },
                        json=dict(payload) if payload is not None else None,
                    )
                except httpx.HTTPError as exc:
                    raise Flow2ApiAdminError(
                        "Flow2API 管理服务连接失败。", status_code=503
                    ) from exc
                if response.status_code == 401 and attempt == 0:
                    self.__class__._session_token = None
                    token = await self._login(client, force=True)
                    continue
                if not response.is_success:
                    status = 400 if 400 <= response.status_code < 500 else 502
                    raise Flow2ApiAdminError(
                        _safe_error_message(response), status_code=status
                    )
                if response.status_code == 204 or not response.content:
                    return {"success": True}
                try:
                    return response.json()
                except ValueError as exc:
                    raise Flow2ApiAdminError("Flow2API 返回了无效响应。") from exc
        raise Flow2ApiAdminError("Flow2API 管理会话重试失败。")


__all__ = ["Flow2ApiAdminClient", "Flow2ApiAdminError"]
