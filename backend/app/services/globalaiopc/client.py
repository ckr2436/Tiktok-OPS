from __future__ import annotations

from typing import Any, Iterable, Mapping

import httpx

from app.core.config import settings


class GlobalAiOpcApiError(Exception):
    """Raised when the GlobalAiOpc API call cannot be completed."""


_TASK_ID_KEYS = ("task_id", "taskId", "id")
_STATUS_KEYS = ("status", "state", "task_status", "taskStatus")
_ERROR_CODE_KEYS = ("error_code", "errorCode", "fail_code", "failCode", "code")
_ERROR_MESSAGE_KEYS = (
    "error",
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


def normalize_submit_path(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {
        "",
        "videos",
        "v1/videos",
        "/v1/videos",
        "omni-flash",
        "omni_flash",
        "omni-flash/videos",
        "v1/omni-flash/videos",
        "/v1/omni-flash/videos",
    }:
        return "/v1/omni-flash/videos"
    if raw.startswith("/"):
        return raw
    return f"/{raw}"


class GlobalAiOpcVideoClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or settings.GLOBALAIOPC_OMNI_FLASH_API_BASE_URL).rstrip("/")
        self.timeout = float(timeout or settings.GLOBALAIOPC_OMNI_FLASH_HTTP_TIMEOUT_SECONDS)

    def _auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise GlobalAiOpcApiError("GlobalAiOpc API key is empty")
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
                raise GlobalAiOpcApiError(
                    f"GlobalAiOpc HTTP {response.status_code}: {response.text[:500]}",
                ) from exc
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                raise GlobalAiOpcApiError(
                    f"Invalid JSON response from GlobalAiOpc: {response.text[:500]}",
                ) from exc
            if not isinstance(data, dict):
                raise GlobalAiOpcApiError("GlobalAiOpc response is not a JSON object")
            return data

    async def create_video_task(
        self,
        *,
        payload: Mapping[str, Any],
        submit_path: str | None = None,
    ) -> dict[str, Any]:
        path = normalize_submit_path(submit_path or str(payload.get("submit_path") or ""))
        clean_payload = {
            key: value
            for key, value in dict(payload).items()
            if value is not None and key not in {"submit_path", "__submit_path"}
        }
        clean_payload.pop("reference_file_paths", None)
        return await self._request("POST", path, json=clean_payload)

    async def get_video_task(self, *, task_id: str) -> dict[str, Any]:
        task_id_clean = (task_id or "").strip()
        if not task_id_clean:
            raise GlobalAiOpcApiError("task_id is empty")
        return await self._request(
            "GET",
            f"/v1/result/{task_id_clean}",
        )

    def content_url(self, *, task_id: str) -> str:
        raise GlobalAiOpcApiError("GlobalAiOpc result response did not include video_url")


__all__ = [
    "GlobalAiOpcApiError",
    "GlobalAiOpcVideoClient",
    "extract_error",
    "extract_status",
    "extract_task_id",
    "extract_video_urls",
    "normalize_status",
    "normalize_submit_path",
]
