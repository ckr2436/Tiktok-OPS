from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, Mapping

import httpx

from app.core.config import settings


class ToApisApiError(RuntimeError):
    pass


class ToApisVideoClient:
    def __init__(self, *, api_key: str, base_url: str | None = None, timeout: float | None = None) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(
            base_url or getattr(settings, "TOAPIS_API_BASE_URL", "https://toapis.com")
        ).rstrip("/")
        self.timeout = float(timeout or getattr(settings, "TOAPIS_HTTP_TIMEOUT_SECONDS", 120))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ToApisApiError("ToAPIs API key is empty")
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._headers())
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # Keep transport failures inside the provider error contract.  The
            # content-factory image state machine handles ToApisApiError per
            # reference, persists already-submitted sibling tasks, and applies
            # its short bounded retry.  Letting RemoteProtocolError escape
            # bypassed that path and incorrectly imposed the generic five
            # minute stage backoff.
            raise ToApisApiError(
                f"ToAPIs transport error: {exc.__class__.__name__}"
            ) from exc
        if not response.is_success:
            raise ToApisApiError(f"ToAPIs HTTP {response.status_code}: {response.text[:800]}")
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ToApisApiError(f"ToAPIs returned invalid JSON: {response.text[:500]}") from exc
        if not isinstance(payload, dict):
            raise ToApisApiError("ToAPIs response is not a JSON object")
        return payload

    async def upload_image(self, path: str, mime_type: str | None = None) -> str:
        source = Path(path)
        if not source.is_file():
            raise ToApisApiError(f"ToAPIs reference image not found: {path}")
        mime = mime_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        with source.open("rb") as stream:
            payload = await self._request(
                "POST",
                "/v1/uploads/images",
                files={"file": (source.name, stream, mime)},
                data={"purpose": "generation"},
            )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        url = str(data.get("url") or "").strip()
        if not payload.get("success", True) or not url:
            raise ToApisApiError(str(payload.get("message") or "ToAPIs image upload returned no URL")[:800])
        return url

    async def _upload_image_bytes(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
    ) -> str:
        if not content:
            raise ToApisApiError("ToAPIs reference image is empty")
        payload = await self._request(
            "POST",
            "/v1/uploads/images",
            files={"file": (filename, content, mime_type)},
            data={"purpose": "generation"},
        )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        url = str(data.get("url") or "").strip()
        if not payload.get("success", True) or not url:
            raise ToApisApiError(
                str(payload.get("message") or "ToAPIs image upload returned no URL")[:800]
            )
        return url

    async def _reference_image_url(self, value: str) -> str:
        raw = str(value or "").strip()
        if raw.lower().startswith(("http://", "https://")):
            return raw
        if raw.lower().startswith("data:"):
            header, separator, encoded = raw.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ToApisApiError("ToAPIs reference data URL is invalid")
            mime_type = header[5:].split(";", 1)[0].strip() or "image/png"
            suffix = mimetypes.guess_extension(mime_type) or ".png"
            try:
                content = base64.b64decode(encoded, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ToApisApiError("ToAPIs reference data URL has invalid base64") from exc
            digest = hashlib.sha256(content).hexdigest()[:16]
            return await self._upload_image_bytes(
                content,
                filename=f"reference-{digest}{suffix}",
                mime_type=mime_type,
            )
        return await self.upload_image(raw)

    @staticmethod
    def _image_ratio(size: str) -> str:
        raw = str(size or "").strip().lower()
        if raw in {
            "1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5",
            "16:9", "9:16", "2:1", "1:2", "21:9", "9:21",
        }:
            return raw
        aliases = {
            "1024x1024": "1:1",
            "1792x1024": "16:9",
            "1024x1792": "9:16",
            "1536x864": "16:9",
            "864x1536": "9:16",
        }
        return aliases.get(raw, "9:16")

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
        del quality, input_fidelity
        business_id = ""
        if idempotency_key:
            business_id = (
                "cf_img_" + hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:40]
            )
            try:
                # A POST can be accepted even when its HTTP response is lost.
                # Recover by the caller-owned business id before uploading the
                # same anchors or creating another billable generation.
                return await self.get_image_task(task_id=business_id)
            except ToApisApiError as exc:
                if "HTTP 404" not in str(exc):
                    raise
        reference_images = [
            await self._reference_image_url(value)
            for value in list(images or [])
            if str(value or "").strip()
        ]
        payload: dict[str, Any] = {
            "model": "gpt-image-2" if str(model or "").strip() != "gpt-image-2" else model,
            "prompt": str(prompt or "").strip(),
            "size": self._image_ratio(size),
            "resolution": "1k",
            "n": max(1, int(n)),
            "response_format": "url",
        }
        if reference_images:
            payload["reference_images"] = reference_images
        if business_id:
            payload["client_business_id"] = business_id
        return await self._request("POST", "/v1/images/generations", json=payload)

    async def get_image_task(self, *, task_id: str) -> dict[str, Any]:
        value = str(task_id or "").strip()
        if not value:
            raise ToApisApiError("ToAPIs image task id is empty")
        return await self._request("GET", f"/v1/images/generations/{value}")

    async def download(self, url: str) -> tuple[bytes, str]:
        headers = {"Accept": "image/*"}
        if str(url).startswith(self.base_url + "/"):
            headers.update(self._headers())
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(str(url), headers=headers)
        except httpx.HTTPError as exc:
            raise ToApisApiError(
                f"ToAPIs image download transport error: {exc.__class__.__name__}"
            ) from exc
        if not response.is_success:
            raise ToApisApiError(
                f"ToAPIs image download HTTP {response.status_code}: {response.text[:800]}"
            )
        return response.content, str(
            response.headers.get("content-type") or "application/octet-stream"
        )

    async def create_video(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/videos/generations", json=dict(payload))

    async def get_video(self, task_id: str) -> dict[str, Any]:
        value = str(task_id or "").strip()
        if not value:
            raise ToApisApiError("ToAPIs task id is empty")
        return await self._request("GET", f"/v1/videos/generations/{value}")
