from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping

import httpx

from app.core.config import settings


class BandianwaApiError(Exception):
    """Raised when the Bandianwa API call cannot be completed."""


MAX_OMNI_REFERENCE_FILES = 7
_TASK_ID_KEYS = ("task_id", "taskId", "id")
_STATUS_KEYS = ("status", "state", "task_status", "taskStatus")
_ERROR_CODE_KEYS = ("error_code", "errorCode", "fail_code", "failCode", "code")
_ERROR_MESSAGE_KEYS = (
    "error_message",
    "errorMessage",
    "fail_msg",
    "failMsg",
    "message",
    "msg",
)
_URL_KEYS = (
    "video_url",
    "videoUrl",
    "content_url",
    "contentUrl",
    "download_url",
    "downloadUrl",
    "url",
)
_URL_LIST_KEYS = (
    "video_urls",
    "videoUrls",
    "urls",
    "result_urls",
    "resultUrls",
    "outputs",
    "output",
)

_IMAGE_URL_KEYS = (
    "image_url",
    "imageUrl",
    "content_url",
    "contentUrl",
    "download_url",
    "downloadUrl",
    "url",
)
_IMAGE_LIST_KEYS = (
    "images",
    "image_urls",
    "imageUrls",
    "urls",
    "outputs",
    "output",
    "data",
)
_IMAGE_BASE64_KEYS = ("b64_json", "b64", "base64", "image_base64", "imageBase64")


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _nested_mappings(resp: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = [resp]
    for key in ("data", "result", "task", "job"):
        child = _as_mapping(resp.get(key))
        if child is not None:
            items.append(child)
    return items


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk(child)


def extract_task_id(resp: Mapping[str, Any]) -> str | None:
    for obj in _nested_mappings(resp):
        for key in _TASK_ID_KEYS:
            value = obj.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    status = raw.lower()
    if status in {"queued", "queue", "pending", "waiting", "created"}:
        return "queued"
    if status in {"in_progress", "progress", "processing", "running", "generating"}:
        return "in_progress"
    if status in {"completed", "complete", "success", "succeeded", "ok"}:
        return "completed"
    if status in {"failed", "fail", "error", "errored", "cancelled", "canceled"}:
        return "failed"
    return status


def extract_status(resp: Mapping[str, Any]) -> str | None:
    for obj in _nested_mappings(resp):
        for key in _STATUS_KEYS:
            status = normalize_status(obj.get(key))
            if status:
                return status
    return None


def extract_error(resp: Mapping[str, Any]) -> tuple[str | None, str | None]:
    code: str | None = None
    message: str | None = None

    for obj in _nested_mappings(resp):
        if code is None:
            for key in _ERROR_CODE_KEYS:
                value = obj.get(key)
                if value not in (None, "", 0, "0", 200, "200"):
                    code = str(value)
                    break
        if message is None:
            for key in _ERROR_MESSAGE_KEYS:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    message = value.strip()
                    break

    error_obj = _as_mapping(resp.get("error"))
    if error_obj is not None:
        if code is None:
            raw_code = error_obj.get("code")
            if raw_code is not None:
                code = str(raw_code)
        if message is None:
            raw_msg = error_obj.get("message") or error_obj.get("msg")
            if raw_msg:
                message = str(raw_msg)

    return code, message


def _append_url(urls: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    url = value.strip()
    if not url.lower().startswith(("http://", "https://")):
        return
    if url not in urls:
        urls.append(url)


def extract_video_urls(resp: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []

    for obj in _nested_mappings(resp):
        for key in _URL_KEYS:
            _append_url(urls, obj.get(key))
        for key in _URL_LIST_KEYS:
            value = obj.get(key)
            if isinstance(value, str):
                _append_url(urls, value)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    if isinstance(item, Mapping):
                        for url_key in _URL_KEYS:
                            _append_url(urls, item.get(url_key))
                    else:
                        _append_url(urls, item)

    for value in _walk(resp):
        if isinstance(value, Mapping):
            for key in _URL_KEYS:
                _append_url(urls, value.get(key))

    return urls


def extract_image_outputs(resp: Mapping[str, Any]) -> list[dict[str, str]]:
    """Normalize immediate and asynchronous image response payloads."""
    outputs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def append(kind: str, value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        clean = value.strip()
        if kind == "url" and not clean.lower().startswith(("http://", "https://")):
            return
        key = (kind, clean)
        if key not in seen:
            seen.add(key)
            outputs.append({kind: clean})

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in _IMAGE_URL_KEYS:
                append("url", value.get(key))
            for key in _IMAGE_BASE64_KEYS:
                append("b64_json", value.get(key))
            for key in _IMAGE_LIST_KEYS:
                child = value.get(key)
                if isinstance(child, (list, tuple)):
                    for item in child:
                        inspect(item)
                elif isinstance(child, Mapping):
                    inspect(child)
            for key in ("result", "task", "job"):
                child = value.get(key)
                if isinstance(child, (Mapping, list, tuple)):
                    inspect(child)
        elif isinstance(value, (list, tuple)):
            for item in value:
                inspect(item)
        elif isinstance(value, str):
            append("url", value)

    inspect(resp)
    return outputs


def image_file_data_url(path: str | Path) -> str:
    file_path = Path(path)
    if not file_path.is_file():
        raise BandianwaApiError(f"Reference image not found: {file_path}")
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def normalize_submit_path(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"videos", "v1/videos", "/v1/videos"}:
        return "/v1/videos"
    if raw in {"generate", "api/v1/generate", "/api/v1/generate"}:
        return "/api/v1/generate"
    if raw.startswith("/"):
        return raw
    if raw:
        return f"/{raw}"
    return "/api/v1/generate"


class BandianwaVideoClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or settings.BANDIANWA_API_BASE_URL).rstrip("/")
        self.timeout = float(timeout or settings.BANDIANWA_HTTP_TIMEOUT_SECONDS)

    def _auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise BandianwaApiError("Bandianwa API key is empty")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise BandianwaApiError(
                    f"Bandianwa HTTP {response.status_code}: {response.text[:500]}",
                ) from exc
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                raise BandianwaApiError(
                    f"Invalid JSON response from Bandianwa: {response.text[:500]}",
                ) from exc
            if not isinstance(data, dict):
                raise BandianwaApiError("Bandianwa response is not a JSON object")
            return data

    async def _request_multipart(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, Any],
        reference_files: list[Mapping[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())

        handles: list[BinaryIO] = []
        files: list[tuple[str, tuple[str, BinaryIO, str]]] = []
        try:
            seen_paths: set[str] = set()
            for item in reference_files:
                raw_path = str(item.get("path") or "").strip()
                if not raw_path:
                    continue
                if raw_path in seen_paths:
                    continue
                seen_paths.add(raw_path)
                if len(files) >= MAX_OMNI_REFERENCE_FILES:
                    break
                path_obj = Path(raw_path)
                if not path_obj.exists() or not path_obj.is_file():
                    raise BandianwaApiError(f"Reference image not found: {raw_path}")
                handle = path_obj.open("rb")
                handles.append(handle)
                files.append(
                    (
                        "input_reference[]",
                        (
                            str(item.get("filename") or path_obj.name),
                            handle,
                            str(item.get("content_type") or "application/octet-stream"),
                        ),
                    ),
                )

            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    data={key: self._form_value(value) for key, value in data.items()},
                    files=files,
                    **kwargs,
                )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise BandianwaApiError(
                        f"Bandianwa HTTP {response.status_code}: {response.text[:500]}",
                    ) from exc
                try:
                    resp_data = response.json()
                except Exception as exc:  # noqa: BLE001
                    raise BandianwaApiError(
                        f"Invalid JSON response from Bandianwa: {response.text[:500]}",
                    ) from exc
                if not isinstance(resp_data, dict):
                    raise BandianwaApiError("Bandianwa response is not a JSON object")
                return resp_data
        finally:
            for handle in handles:
                handle.close()

    @staticmethod
    def _form_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    async def create_video_task(
        self,
        *,
        payload: Mapping[str, Any],
        submit_path: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        path = normalize_submit_path(submit_path or str(payload.get("submit_path") or ""))
        clean_payload = {
            key: value
            for key, value in dict(payload).items()
            if value is not None and key not in {"submit_path", "__submit_path"}
        }
        reference_files = clean_payload.pop("reference_file_paths", None) or []
        headers = {"Prefer": "respond-async"} if path == "/v1/videos" else {}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        params = {"async": "true"} if path == "/v1/videos" else None
        if path == "/v1/videos" and reference_files:
            clean_payload.pop("input_reference", None)
            return await self._request_multipart(
                "POST",
                path,
                data=clean_payload,
                reference_files=list(reference_files),
                headers=headers,
                params=params,
            )
        kwargs: dict[str, Any] = {"json": clean_payload}
        if headers:
            kwargs["headers"] = headers
        if params:
            kwargs["params"] = params
        return await self._request("POST", path, **kwargs)

    async def get_video_task(self, *, task_id: str) -> dict[str, Any]:
        task_id_clean = (task_id or "").strip()
        if not task_id_clean:
            raise BandianwaApiError("task_id is empty")
        return await self._request(
            "GET",
            f"/v1/videos/{task_id_clean}",
            params={"async": "true"},
        )

    def content_url(self, *, task_id: str) -> str:
        task_id_clean = (task_id or "").strip()
        return f"{self.base_url}/v1/videos/{task_id_clean}/content"


class BandianwaImageClient:
    """Client for Bandianwa's unified asynchronous image API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or settings.BANDIANWA_API_BASE_URL).rstrip("/")
        self.timeout = float(timeout or max(settings.BANDIANWA_HTTP_TIMEOUT_SECONDS, 120.0))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise BandianwaApiError("Bandianwa API key is empty")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._headers())
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # httpx.ReadTimeout has an empty string representation.  Letting it
            # escape made the Content Factory record a blank terminal stage
            # failure, even though retrying the same idempotent image request is
            # safe and expected.
            raise BandianwaApiError(
                f"Bandianwa image transport error during {method} {path}: "
                f"{exc.__class__.__name__}"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BandianwaApiError(
                f"Bandianwa image HTTP {response.status_code}: {response.text[:2000]}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BandianwaApiError(
                f"Bandianwa image API returned invalid JSON: {response.text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise BandianwaApiError("Bandianwa image response is not a JSON object")
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
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": max(1, int(n)),
            "response_format": "b64_json",
            "quality": str(quality or "high"),
            "input_fidelity": str(input_fidelity or "high"),
            "output_format": "png",
            "extra": {},
        }
        # The provider documents images as optional.  gpt-image-2 may accept
        # an explicit empty array and then fail the asynchronous job; omit the
        # field entirely for text-only visual references.
        if images:
            payload["images"] = list(images)
        headers = {"Prefer": "respond-async"}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        return await self._json_request(
            "POST",
            "/v1/images/generations",
            headers=headers,
            params={"async": "true"},
            json=payload,
        )

    async def get_image_task(self, *, task_id: str) -> dict[str, Any]:
        clean = str(task_id or "").strip()
        if not clean:
            raise BandianwaApiError("image task_id is empty")
        return await self._json_request(
            "GET", f"/v1/images/{clean}", params={"async": "true"}
        )

    async def get_image_content(self, *, task_id: str) -> tuple[bytes, str]:
        clean = str(task_id or "").strip()
        if not clean:
            raise BandianwaApiError("image task_id is empty")
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.get(
                    f"/v1/images/{clean}/content", headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise BandianwaApiError(
                "Bandianwa image content transport error: "
                f"{exc.__class__.__name__}"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BandianwaApiError(
                f"Bandianwa image content HTTP {response.status_code}: {response.text[:1000]}"
            ) from exc
        return response.content, str(response.headers.get("content-type") or "application/octet-stream")

    async def download(self, url: str) -> tuple[bytes, str]:
        headers = {"Accept": "image/*"}
        if str(url).startswith(self.base_url + "/"):
            headers.update(self._headers())
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(str(url), headers=headers)
        except httpx.HTTPError as exc:
            raise BandianwaApiError(
                "Bandianwa image download transport error: "
                f"{exc.__class__.__name__}"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BandianwaApiError(
                f"Bandianwa image download HTTP {response.status_code}: {response.text[:1000]}"
            ) from exc
        return response.content, str(response.headers.get("content-type") or "application/octet-stream")


__all__ = [
    "BandianwaApiError",
    "BandianwaImageClient",
    "BandianwaVideoClient",
    "extract_error",
    "extract_image_outputs",
    "extract_status",
    "extract_task_id",
    "extract_video_urls",
    "image_file_data_url",
    "normalize_status",
    "normalize_submit_path",
]
