from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping

import httpx

from app.core.config import settings


class Sub2ApiApiError(RuntimeError):
    """A sanitized Sub2API transport or protocol failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        upstream_status_code: int | None = None,
        retry_after_seconds: int | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status_code = upstream_status_code
        self.retry_after_seconds = retry_after_seconds
        self.code = str(code or "sub2api_error")
        effective_status = upstream_status_code or status_code
        self.retryable = (
            bool(retryable)
            if retryable is not None
            else (
                effective_status is None
                or int(effective_status) in {408, 409, 425, 429}
                or int(effective_status) >= 500
            )
        )


class Sub2ApiImageClient:
    """Image adapter for the local Sub2API account scheduler.

    GPT Image uses Sub2API's durable asynchronous OpenAI-compatible endpoint.
    Flow/Gemini image generation is intentionally handled by the independent
    Flow2API adapter so account pools and route health never contaminate one
    another.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(
            base_url or settings.SUB2API_API_BASE_URL
        ).rstrip("/")
        self.timeout = float(
            timeout or settings.SUB2API_HTTP_TIMEOUT_SECONDS
        )

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise Sub2ApiApiError("Sub2API API key is empty")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    @staticmethod
    def _normalized_model(model: str | None) -> str:
        value = str(model or "gpt-image-2").strip().lower().replace("-", "_")
        aliases = {
            "gpt_image_2_0": "gpt-image-2",
            "gpt_image_2": "gpt-image-2",
        }
        return aliases.get(value, str(model or "gpt-image-2").strip().lower())

    @staticmethod
    def _canonical_json(value: Mapping[str, Any]) -> bytes:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _idempotency_headers(
        idempotency_key: str | None,
        fingerprint: str,
    ) -> dict[str, str]:
        if not str(idempotency_key or "").strip():
            return {}
        # Do not leak project/stage identities to gateway logs.  The digest is
        # still stable across Celery redelivery and short enough for proxies.
        key_digest = hashlib.sha256(
            str(idempotency_key).encode("utf-8")
        ).hexdigest()
        return {
            "Idempotency-Key": f"gmv-cf-{key_digest}",
            "X-Idempotency-Fingerprint": fingerprint,
        }

    @staticmethod
    def _decode_reference(value: str, index: int) -> tuple[str, bytes, str]:
        raw = str(value or "").strip()
        if raw.lower().startswith("data:"):
            header, separator, encoded = raw.partition(",")
            if not separator or ";base64" not in header.lower():
                raise Sub2ApiApiError("Sub2API reference data URL is invalid")
            mime_type = header[5:].split(";", 1)[0].strip() or "image/png"
            if not mime_type.lower().startswith("image/"):
                raise Sub2ApiApiError("Sub2API reference is not an image")
            try:
                content = base64.b64decode(encoded, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise Sub2ApiApiError(
                    "Sub2API reference data URL has invalid base64"
                ) from exc
            suffix = mimetypes.guess_extension(mime_type) or ".png"
            return f"reference-{index:02d}{suffix}", content, mime_type
        source = Path(raw)
        if source.is_file():
            mime_type = (
                mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            )
            if not mime_type.lower().startswith("image/"):
                raise Sub2ApiApiError("Sub2API reference file is not an image")
            return source.name, source.read_bytes(), mime_type
        raise Sub2ApiApiError(
            "Sub2API references must be local files or image data URLs"
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._headers())
        request_target = str(path)
        # The shared Sub2API base is OpenAI-compatible and therefore ends in
        # ``/v1``. httpx intentionally preserves that base path even when the
        # request argument starts with ``/``; passing Gemini's native
        # ``/v1beta`` route directly would become ``/v1/v1beta`` and return a
        # misleading 404. Use an absolute same-origin target for native Gemini
        # endpoints while retaining the configured origin and credentials.
        if request_target.startswith("/v1beta/") and self.base_url.endswith(
            "/v1"
        ):
            request_target = f"{self.base_url[:-3]}{request_target}"
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            ) as client:
                response = await client.request(
                    method, request_target, headers=headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise Sub2ApiApiError(
                f"Sub2API image transport error during {method} {path}: "
                f"{exc.__class__.__name__}"
            ) from exc
        if not response.is_success:
            message = ""
            code = "sub2api_http_error"
            upstream_status_code = None
            retry_after_seconds = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "")
                        code = str(error.get("status") or error.get("code") or code)
                        upstream_status_code = error.get("upstream_status_code")
                        retry_after_seconds = error.get("retry_after_seconds")
                    message = message or str(payload.get("message") or "")
            except ValueError:
                message = ""
            raise Sub2ApiApiError(
                f"Sub2API image HTTP {response.status_code}: "
                f"{message[:600] or 'upstream request failed'}",
                status_code=int(response.status_code),
                upstream_status_code=(
                    int(upstream_status_code)
                    if isinstance(upstream_status_code, int)
                    else None
                ),
                retry_after_seconds=(
                    int(retry_after_seconds)
                    if isinstance(retry_after_seconds, int)
                    else None
                ),
                code=code[:128],
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise Sub2ApiApiError(
                "Sub2API image API returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise Sub2ApiApiError(
                "Sub2API image response is not a JSON object"
            )
        return payload

    async def create_image_task(
        self,
        *,
        prompt: str,
        size: str,
        images: list[str] | None = None,
        model: str = "gpt-image-2",
        n: int = 1,
        quality: str = "high",
        input_fidelity: str = "high",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        references = [
            self._decode_reference(value, index)
            for index, value in enumerate(list(images or []), 1)
            if str(value or "").strip()
        ]
        normalized_model = self._normalized_model(model)
        if normalized_model != "gpt-image-2":
            raise Sub2ApiApiError(
                f"Sub2API image model is not supported: {normalized_model}",
                code="unsupported_image_model",
                retryable=False,
            )
        common: dict[str, Any] = {
            "model": normalized_model,
            "prompt": str(prompt or "").strip(),
            "size": str(size or "1024x1024"),
            "n": max(1, int(n)),
            "quality": str(quality or "high"),
            "output_format": "png",
        }
        if references:
            fields = {
                **common,
                "n": str(common["n"]),
                "input_fidelity": str(input_fidelity or "high"),
            }
            digest = hashlib.sha256(self._canonical_json(fields))
            files: list[tuple[str, tuple[str, bytes, str]]] = []
            for filename, content, mime_type in references:
                digest.update(hashlib.sha256(content).digest())
                files.append(("image", (filename, content, mime_type)))
            headers = self._idempotency_headers(
                idempotency_key, digest.hexdigest()
            )
            return await self._request(
                "POST",
                "/images/edits/async",
                headers=headers,
                data=fields,
                files=files,
            )
        fingerprint = hashlib.sha256(
            self._canonical_json(common)
        ).hexdigest()
        headers = self._idempotency_headers(idempotency_key, fingerprint)
        return await self._request(
            "POST",
            "/images/generations/async",
            headers=headers,
            json=common,
        )

    async def get_image_task(self, *, task_id: str) -> dict[str, Any]:
        clean = str(task_id or "").strip()
        if not clean:
            raise Sub2ApiApiError("Sub2API image task_id is empty")
        return await self._request("GET", f"/images/tasks/{clean}")

    async def download(self, url: str) -> tuple[bytes, str]:
        max_bytes = 64 * 1024 * 1024
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET", str(url), headers={"Accept": "image/*"}
                ) as response:
                    if not response.is_success:
                        raise Sub2ApiApiError(
                            f"Sub2API image download HTTP {response.status_code}"
                        )
                    content_type = str(
                        response.headers.get("content-type")
                        or "application/octet-stream"
                    )
                    if not content_type.lower().startswith("image/"):
                        raise Sub2ApiApiError(
                            "Sub2API image download returned non-image content"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise Sub2ApiApiError(
                                "Sub2API image download exceeded 64 MiB"
                            )
                    if not content:
                        raise Sub2ApiApiError(
                            "Sub2API image download returned an empty file"
                        )
                    return bytes(content), content_type
        except Sub2ApiApiError:
            raise
        except httpx.HTTPError as exc:
            raise Sub2ApiApiError(
                "Sub2API image download transport error: "
                f"{exc.__class__.__name__}"
            ) from exc


__all__ = ["Sub2ApiApiError", "Sub2ApiImageClient"]
