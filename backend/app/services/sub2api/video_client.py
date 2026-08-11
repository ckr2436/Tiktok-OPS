from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.services.sub2api.client import Sub2ApiApiError


_VIDEO_URL_RE = re.compile(r"https?://[^\s<>'\"\]\)]+", re.IGNORECASE)
_FLOW_VIDEO_HOSTS = {"flow-content.google"}
_NESTED_HTTP_STATUS_RE = re.compile(r"\bHTTP(?:\s+Error)?\s+(4\d\d|5\d\d)\b", re.IGNORECASE)


def _looks_like_video_url(value: str) -> bool:
    """Recognize durable provider video outputs without trusting arbitrary URLs.

    Google Flow serves generated videos from extensionless signed ``/video/``
    URLs.  Keep that exception host-bound so a URL echoed from user prompt text
    cannot turn an unrelated endpoint into a downloadable result.
    """

    parsed = urlparse(value)
    path = str(parsed.path or "").lower()
    if path.endswith((".mp4", ".mov", ".webm", ".m4v")) or "/tmp/" in path:
        return True
    return (
        str(parsed.scheme or "").lower() == "https"
        and str(parsed.hostname or "").lower() in _FLOW_VIDEO_HOSTS
        and path.startswith("/video/")
        and len(path) > len("/video/")
    )


class Sub2ApiVideoClient:
    """OpenAI-compatible adapter for the private Flow2API video pool.

    The call is intentionally blocking only inside the provider-neutral Celery
    worker.  The HTTP frontend remains asynchronous through the durable
    ``KieTask`` row and never waits for Flow generation.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or settings.SUB2API_API_BASE_URL).rstrip("/")
        self.timeout = float(timeout or 25 * 60)

    def _headers(self, idempotency_key: str) -> dict[str, str]:
        if not self.api_key:
            raise Sub2ApiApiError("Sub2API API key is empty")
        digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": f"gmv-video-{digest}",
        }

    @staticmethod
    def _image_part(path: str, mime_type: str | None) -> dict[str, Any]:
        source = Path(str(path or ""))
        if not source.is_file():
            raise Sub2ApiApiError("Sub2API Flow reference image is unavailable")
        content = source.read_bytes()
        if not content:
            raise Sub2ApiApiError("Sub2API Flow reference image is empty")
        if len(content) > 10 * 1024 * 1024:
            raise Sub2ApiApiError("Sub2API Flow reference image exceeds 10 MiB")
        mime = str(mime_type or mimetypes.guess_type(source.name)[0] or "image/png")
        if not mime.lower().startswith("image/"):
            raise Sub2ApiApiError("Sub2API Flow reference is not an image")
        encoded = base64.b64encode(content).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        }

    @classmethod
    def _video_urls(cls, payload: Any) -> list[str]:
        found: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                for item in value.values():
                    visit(item)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, str):
                return
            for match in _VIDEO_URL_RE.findall(value):
                clean = match.rstrip(".,;:")
                if clean not in found and _looks_like_video_url(clean):
                    found.append(clean)

        visit(payload)
        return found

    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        references: list[tuple[str, str | None]],
        idempotency_key: str,
        heartbeat: Callable[[], Awaitable[None]] | None = None,
        heartbeat_interval_seconds: float = 30.0,
    ) -> tuple[dict[str, Any], list[str]]:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": str(prompt or "").strip()}
        ]
        content.extend(self._image_part(path, mime) for path, mime in references)
        payload = {
            "model": str(model),
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            # This survives raw OpenAI passthrough and provides a stable
            # business identity for Flow2API-side replay protection.
            "gmv_idempotency_key": hashlib.sha256(
                str(idempotency_key).encode("utf-8")
            ).hexdigest(),
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=20.0),
            ) as client:
                request = asyncio.create_task(
                    client.post(
                        "/chat/completions",
                        headers=self._headers(idempotency_key),
                        json=payload,
                    )
                )
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {request},
                            timeout=max(0.01, float(heartbeat_interval_seconds)),
                        )
                        if request in done:
                            response = request.result()
                            break
                        if heartbeat is not None:
                            await heartbeat()
                finally:
                    if not request.done():
                        request.cancel()
                        await asyncio.gather(request, return_exceptions=True)
        except httpx.HTTPError as exc:
            raise Sub2ApiApiError(
                f"Sub2API Flow transport error: {exc.__class__.__name__}"
            ) from exc
        if not response.is_success:
            message = ""
            error_code = ""
            upstream_status_code: int | None = None
            try:
                body = response.json()
                if isinstance(body, Mapping):
                    error = body.get("error")
                    if isinstance(error, Mapping):
                        message = str(error.get("message") or "")
                        error_code = str(error.get("code") or "")
                        try:
                            upstream_status_code = int(
                                error.get("status_code") or 0
                            ) or None
                        except (TypeError, ValueError):
                            upstream_status_code = None
                    message = message or str(body.get("message") or "")
            except ValueError:
                message = ""
            if upstream_status_code is None:
                match = _NESTED_HTTP_STATUS_RE.search(message)
                if match:
                    upstream_status_code = int(match.group(1))
            effective_status = upstream_status_code or int(response.status_code)
            conflict_text = f"{error_code} {message}".lower()
            idempotency_conflict = (
                int(response.status_code) == 409
                and "idempot" in conflict_text
                and any(
                    token in conflict_text
                    for token in ("different request", "reused", "mismatch", "conflict")
                )
            )
            terminal_request_rejection = (
                idempotency_conflict
                or (
                    400 <= effective_status < 500
                and effective_status not in {408, 409, 425, 429}
                )
            )
            normalized_code = (
                "flow_idempotency_conflict"
                if idempotency_conflict
                else "flow_request_rejected"
                if terminal_request_rejection
                else (error_code or "sub2api_flow_error")
            )
            retry_after_seconds: int | None = None
            try:
                retry_after_seconds = max(
                    1,
                    min(3600, int(float(response.headers.get("retry-after", "")))),
                )
            except (TypeError, ValueError):
                retry_after_seconds = None
            raise Sub2ApiApiError(
                f"Sub2API Flow HTTP {response.status_code}: "
                f"{message[:600] or 'upstream request failed'}",
                status_code=int(response.status_code),
                upstream_status_code=upstream_status_code,
                retry_after_seconds=retry_after_seconds,
                code=normalized_code,
                retryable=not terminal_request_rejection,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise Sub2ApiApiError("Sub2API Flow returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise Sub2ApiApiError("Sub2API Flow response is not a JSON object")
        urls = self._video_urls(body)
        if not urls:
            raise Sub2ApiApiError("Sub2API Flow completed without a video URL")
        return body, urls


__all__ = ["Sub2ApiVideoClient"]
