from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.core.config import settings


class Flow2ApiError(RuntimeError):
    """Sanitized Flow2API transport, policy or protocol failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.code = str(code or "flow2api_error")[:128]
        self.retryable = (
            bool(retryable)
            if retryable is not None
            else (
                status_code is None
                or int(status_code) in {408, 409, 425, 429}
                or int(status_code) >= 500
            )
        )


class Flow2ApiImageClient:
    """Nano Banana Pro adapter backed by the local Flow account pool.

    Flow2API's OpenAI-compatible endpoint owns durable idempotency when
    ``gmv_idempotency_key`` is supplied in the JSON body. References are sent
    as ordered multimodal parts so Content Factory anchors retain their exact
    semantic numbering.
    """

    PROVIDER_MODEL = "gemini-3.0-pro-image"
    MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
    _MARKDOWN_URL = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(
            base_url or settings.FLOW2API_API_BASE_URL
        ).rstrip("/")
        self.timeout = float(
            timeout or settings.FLOW2API_HTTP_TIMEOUT_SECONDS
        )

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise Flow2ApiError("Flow2API API key is empty", retryable=False)
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _aspect_ratio(size: str | None) -> str:
        value = str(size or "1024x1024").strip().lower()
        try:
            width, height = (int(part) for part in value.split("x", 1))
        except (TypeError, ValueError):
            return "1:1"
        if width <= 0 or height <= 0:
            return "1:1"
        ratio = width / height
        candidates = {
            "9:16": 9 / 16,
            "3:4": 3 / 4,
            "1:1": 1.0,
            "4:3": 4 / 3,
            "16:9": 16 / 9,
        }
        return min(candidates, key=lambda key: abs(candidates[key] - ratio))

    @staticmethod
    def _reference_data_url(value: str) -> tuple[str, str]:
        raw = str(value or "").strip()
        if raw.lower().startswith("data:"):
            header, separator, encoded = raw.partition(",")
            mime_type = header[5:].split(";", 1)[0].strip().lower()
            if (
                not separator
                or ";base64" not in header.lower()
                or not mime_type.startswith("image/")
            ):
                raise Flow2ApiError(
                    "Flow2API reference data URL is invalid",
                    retryable=False,
                )
            try:
                base64.b64decode(encoded, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise Flow2ApiError(
                    "Flow2API reference data URL has invalid base64",
                    retryable=False,
                ) from exc
            return raw, hashlib.sha256(encoded.encode("ascii")).hexdigest()
        source = Path(raw)
        if not source.is_file():
            raise Flow2ApiError(
                "Flow2API references must be local files or image data URLs",
                retryable=False,
            )
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        if not mime_type.lower().startswith("image/"):
            raise Flow2ApiError(
                "Flow2API reference file is not an image",
                retryable=False,
            )
        content = source.read_bytes()
        encoded = base64.b64encode(content).decode("ascii")
        return (
            f"data:{mime_type};base64,{encoded}",
            hashlib.sha256(content).hexdigest(),
        )

    @staticmethod
    def _stable_request_key(idempotency_key: str | None) -> str:
        raw = str(idempotency_key or "").strip()
        if not raw:
            raise Flow2ApiError(
                "Flow2API idempotency key is required",
                code="idempotency_key_required",
                retryable=False,
            )
        return f"gmv-cf-{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    @classmethod
    def _extract_url(cls, payload: Mapping[str, Any]) -> str:
        direct = str(payload.get("url") or "").strip()
        if direct:
            return direct
        for row in list(payload.get("data") or []):
            if isinstance(row, Mapping):
                candidate = str(row.get("url") or "").strip()
                if candidate:
                    return candidate
        for choice in list(payload.get("choices") or []):
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, str):
                match = cls._MARKDOWN_URL.search(content)
                if match:
                    return match.group(1)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    image_url = part.get("image_url")
                    if isinstance(image_url, Mapping):
                        nested_url = image_url.get("url")
                    elif isinstance(image_url, str):
                        nested_url = image_url
                    else:
                        nested_url = ""
                    candidate = str(part.get("url") or nested_url).strip()
                    if candidate:
                        return candidate
        raise Flow2ApiError(
            "Flow2API image response contains no downloadable image",
            code="image_missing",
            retryable=False,
        )

    async def create_image_task(
        self,
        *,
        prompt: str,
        size: str,
        images: list[str] | None = None,
        model: str = "nano_banana_pro",
        n: int = 1,
        quality: str = "high",
        input_fidelity: str = "high",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        del quality, input_fidelity
        logical_model = str(model or "").strip().lower().replace("-", "_")
        if logical_model != "nano_banana_pro":
            raise Flow2ApiError(
                f"Flow2API image model is not supported: {logical_model}",
                code="unsupported_image_model",
                retryable=False,
            )
        if int(n) != 1:
            raise Flow2ApiError(
                "Flow2API image adapter currently supports n=1",
                code="unsupported_candidate_count",
                retryable=False,
            )
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": str(prompt or "").strip(),
        }]
        reference_digests: list[str] = []
        for raw in list(images or []):
            if not str(raw or "").strip():
                continue
            data_url, digest = self._reference_data_url(str(raw))
            reference_digests.append(digest)
            content.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
        stable_key = self._stable_request_key(idempotency_key)
        request_payload: dict[str, Any] = {
            "model": self.PROVIDER_MODEL,
            "messages": [{"role": "user", "content": content}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": self._aspect_ratio(size),
                    "imageSize": "1K",
                },
            },
            "gmv_idempotency_key": stable_key,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            ) as client:
                response = await client.post(
                    "/chat/completions",
                    headers=self._headers(),
                    json=request_payload,
                )
        except httpx.HTTPError as exc:
            raise Flow2ApiError(
                f"Flow2API image transport error: {exc.__class__.__name__}"
            ) from exc
        if not response.is_success:
            retry_after = None
            try:
                retry_after = int(float(response.headers.get("retry-after", "")))
            except (TypeError, ValueError):
                retry_after = None
            message = "upstream request failed"
            code = "flow2api_http_error"
            try:
                payload = response.json()
                if isinstance(payload, Mapping):
                    error = payload.get("error")
                    if isinstance(error, Mapping):
                        message = str(error.get("message") or message)[:600]
                        code = str(error.get("code") or error.get("status") or code)
            except ValueError:
                pass
            raise Flow2ApiError(
                f"Flow2API image HTTP {response.status_code}: {message}",
                status_code=int(response.status_code),
                retry_after_seconds=retry_after,
                code=code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise Flow2ApiError("Flow2API image API returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise Flow2ApiError("Flow2API image response is not a JSON object")
        image_url = self._extract_url(payload)
        request_fingerprint = hashlib.sha256(
            (stable_key + "|" + "|".join(reference_digests)).encode("utf-8")
        ).hexdigest()
        return {
            "task_id": f"flow2api-{request_fingerprint[:24]}",
            "status": "completed",
            "model": "nano_banana_pro",
            "provider_model": self.PROVIDER_MODEL,
            "data": [{"url": image_url}],
        }

    async def get_image_task(self, *, task_id: str) -> dict[str, Any]:
        raise Flow2ApiError(
            f"Flow2API image task is synchronous: {str(task_id or '')[:32]}",
            code="synchronous_task",
            retryable=False,
        )

    def _download_url_allowed(self, url: str) -> bool:
        parsed = urlparse(str(url or ""))
        base = urlparse(self.base_url)
        hostname = str(parsed.hostname or "").lower()
        if parsed.scheme == "data":
            return True
        if parsed.scheme not in {"http", "https"}:
            return False
        if hostname == str(base.hostname or "").lower():
            return parsed.scheme == base.scheme
        return parsed.scheme == "https" and hostname == "flow-content.google"

    async def download(self, url: str) -> tuple[bytes, str]:
        raw = str(url or "").strip()
        if raw.lower().startswith("data:image/"):
            header, separator, encoded = raw.partition(",")
            if not separator or ";base64" not in header.lower():
                raise Flow2ApiError("Flow2API inline image is invalid")
            try:
                content = base64.b64decode(encoded, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise Flow2ApiError("Flow2API inline image base64 is invalid") from exc
            if not content or len(content) > self.MAX_DOWNLOAD_BYTES:
                raise Flow2ApiError("Flow2API inline image has an invalid size")
            return content, header[5:].split(";", 1)[0]
        if not self._download_url_allowed(raw):
            raise Flow2ApiError(
                "Flow2API image download host is not trusted",
                code="untrusted_download_host",
                retryable=False,
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET", raw, headers={"Accept": "image/*"}
                ) as response:
                    if not response.is_success:
                        raise Flow2ApiError(
                            f"Flow2API image download HTTP {response.status_code}",
                            status_code=int(response.status_code),
                        )
                    content_type = str(
                        response.headers.get("content-type")
                        or "application/octet-stream"
                    )
                    if not content_type.lower().startswith("image/"):
                        raise Flow2ApiError(
                            "Flow2API image download returned non-image content"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.MAX_DOWNLOAD_BYTES:
                            raise Flow2ApiError(
                                "Flow2API image download exceeded 64 MiB"
                            )
                    if not content:
                        raise Flow2ApiError(
                            "Flow2API image download returned an empty file"
                        )
                    return bytes(content), content_type
        except Flow2ApiError:
            raise
        except httpx.HTTPError as exc:
            raise Flow2ApiError(
                f"Flow2API image download transport error: {exc.__class__.__name__}"
            ) from exc


__all__ = ["Flow2ApiError", "Flow2ApiImageClient"]
