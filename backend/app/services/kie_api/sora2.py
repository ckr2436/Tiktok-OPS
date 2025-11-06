# app/services/kie_api/sora2.py
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

import httpx


class KieApiError(Exception):
    """KIE API 调用异常。"""
    pass


class Sora2ImageToVideoService:
    """
    封装 KIE 官方 API：
    - /api/v1/jobs/createTask
    - /api/v1/jobs/recordInfo
    - /api/v1/chat/credit
    - /api/v1/common/download-url
    - https://kieai.redpandaai.co/api/file-stream-upload （文件上传）
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.kie.ai",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise KieApiError("KIE API key is empty")
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        调用 https://api.kie.ai 下的通用请求封装。
        """
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        ) as client:
            resp = await client.request(method, path, headers=headers, **kwargs)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise KieApiError(f"Invalid JSON response from KIE: {resp.text}") from exc

    # ----------------- 任务相关 -----------------
    async def create_image_to_video_task(
        self,
        *,
        model: str,
        input_data: Mapping[str, Any],
        callback_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        POST /api/v1/jobs/createTask
        """
        payload: dict[str, Any] = {
            "model": model,
            "input": dict(input_data),
        }
        if callback_url:
            payload["callBackUrl"] = callback_url

        return await self._request(
            "POST",
            "/api/v1/jobs/createTask",
            json=payload,
        )

    async def get_task_record(
        self,
        *,
        task_id: str,
    ) -> dict[str, Any]:
        """
        GET /api/v1/jobs/recordInfo
        """
        return await self._request(
            "GET",
            "/api/v1/jobs/recordInfo",
            params={"taskId": task_id},
        )

    # ----------------- 通用 API -----------------
    async def get_remaining_credits(self) -> dict[str, Any]:
        """
        GET /api/v1/chat/credit
        """
        return await self._request("GET", "/api/v1/chat/credit")

    async def get_download_url(self, *, file_url: str) -> dict[str, Any]:
        """
        POST /api/v1/common/download-url
        """
        payload = {"url": file_url}
        return await self._request(
            "POST",
            "/api/v1/common/download-url",
            json=payload,
        )

    # ----------------- 文件上传 -----------------
    async def upload_file_stream(
        self,
        *,
        filename: str,
        file_bytes: bytes,
        upload_path: Optional[str] = None,
        file_name: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        POST https://kieai.redpandaai.co/api/file-stream-upload

        用于把前端上传的图片传到 KIE，获得 fileUrl：
        - file: 二进制文件
        - uploadPath: 目录（可选）
        - fileName: 文件名（可选）
        - mime_type: Content-Type（可选，默认为 application/octet-stream）
        """
        if not file_bytes:
            raise KieApiError("upload_file_stream: file_bytes is empty")

        url = "https://kieai.redpandaai.co/api/file-stream-upload"

        data: dict[str, str] = {}
        if upload_path:
            data["uploadPath"] = upload_path
        if file_name:
            data["fileName"] = file_name

        files = {
            "file": (
                file_name or filename or "upload",
                file_bytes,
                mime_type or "application/octet-stream",
            ),
        }

        headers = self._auth_headers()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers=headers,
                data=data,
                files=files,
            )
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise KieApiError(f"Invalid JSON response from KIE upload: {resp.text}") from exc


# 方便脚本直接用环境变量测试
_default_api_key = os.getenv("KIE_API_KEY", "").strip()
sora = Sora2ImageToVideoService(api_key=_default_api_key) if _default_api_key else None

__all__ = ["KieApiError", "Sora2ImageToVideoService", "sora"]

