# app/services/ttb_api.py
from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, ClassVar, Dict, Iterable, Optional, Tuple

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

from app.core.config import settings


# --------------------------- 错误 ---------------------------
class TTBApiError(Exception):
    """业务级错误（HTTP 2xx 但返回 code 非 0 / 非 OK）"""

    def __init__(
        self,
        message: str,
        *,
        code: str | int | None = None,
        payload: Any = None,
        status: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.status = status


class TTBHttpError(Exception):
    """HTTP 层错误（非 2xx；或 429/5xx 触发重试）"""

    def __init__(self, status: int, message: str, *, payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.payload = payload


# --------------------------- 端点路径 ---------------------------
@dataclass(frozen=True, slots=True)
class TTBPaths:
    """Container for TikTok Business endpoint paths (all plain strings)."""

    DEFAULTS: ClassVar[Dict[str, str]] = {
        "bc_get": "/open_api/v1.3/bc/get/",
        "advertiser_get": "/open_api/v1.3/oauth2/advertiser/get/",
        "shop_get": "/open_api/v1.3/store/list/",
        "product_get": "/open_api/v1.3/store/product/get/",
    }

    bc_get: str
    advertiser_get: str
    shop_get: str
    product_get: str

    @classmethod
    def from_settings(cls) -> "TTBPaths":
        def _get(attr: str) -> str:
            default = cls.DEFAULTS[attr]
            for name in (attr.upper(), f"TT_{attr.upper()}", f"TTB_{attr.upper()}"):
                val = getattr(settings, name, None)
                if val:
                    return str(val)
            return default

        return cls(
            bc_get=_get("bc_get"),
            advertiser_get=_get("advertiser_get"),
            shop_get=_get("shop_get"),
            product_get=_get("product_get"),
        )


# --------------------------- 令牌桶限速（默认 10 QPS） ---------------------------
class TokenBucket:
    def __init__(self, rate_per_sec: float = 10.0, capacity: int | None = None):
        self.rate = float(rate_per_sec)
        self.capacity = capacity or max(1, int(math.ceil(rate_per_sec)))
        self.tokens = self.capacity
        self.timestamp = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.timestamp
            self.timestamp = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens < 1:
                need = 1 - self.tokens
                await asyncio.sleep(need / self.rate)
                self.tokens = 0
                self.timestamp = time.monotonic()
            else:
                self.tokens -= 1


# --------------------------- 客户端 ---------------------------
class TTBApiClient:
    """
    TikTok Business API 客户端（异步）：
    - base_url：settings.TT_BIZ_TOKEN_URL 或默认 https://business-api.tiktok.com/open_api/v1.3
    - Bearer token 鉴权
    - 10 QPS 限速
    - 429/5xx 指数退避（抖动）
    - 分页抽象（兼容 cursor 与 page 模型）
    """

    def __init__(
        self,
        *,
        access_token: str,
        base_url: Optional[str] = None,
        timeout: float | None = None,
        qps: float = 10.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = (
            (base_url or settings.TT_BIZ_TOKEN_URL or "https://business-api.tiktok.com/open_api/v1.3")
            .rstrip("/")
        )
        self._timeout = timeout or float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15.0))
        self._token = access_token
        self._bucket = TokenBucket(rate_per_sec=qps)
        self._paths = TTBPaths.from_settings()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                **(headers or {}),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- 底层请求 ----
    @retry(
        retry=retry_if_exception_type((TTBHttpError, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Dict[str, Any] | None = None,
        json_body: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        await self._bucket.acquire()
        resp = await self._client.request(method, url, params=params, json=json_body)

        if resp.status_code >= 500:
            raise TTBHttpError(resp.status_code, "server error", payload=resp.text)
        if resp.status_code == 429:
            raise TTBHttpError(resp.status_code, "rate limited", payload=resp.text)
        if resp.status_code >= 400:
            raise TTBHttpError(resp.status_code, "client error", payload=resp.text)

        try:
            data = resp.json()
        except Exception:
            raise TTBApiError("invalid json response", payload=resp.text, status=resp.status_code)

        code = data.get("code")
        if code not in (0, "0", "OK", "ok", None):
            raise TTBApiError(data.get("message") or "api error", code=code, payload=data, status=resp.status_code)

        return data

    # ---- 提取器（cursor 风格）----
    @staticmethod
    def _extract_list_and_cursor(payload: Dict[str, Any]) -> Tuple[Iterable[dict], Optional[str]]:
        root = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
        items: Iterable[dict] = []
        next_cursor: Optional[str] = None

        if isinstance(root, dict):
            if isinstance(root.get("list"), list):
                items = root["list"]
                pi = root.get("page_info") or {}
                if isinstance(pi, dict):
                    next_cursor = pi.get("cursor") or pi.get("next_cursor")
                next_cursor = next_cursor or root.get("next_cursor")
            elif isinstance(root.get("items"), list):
                items = root["items"]
                next_cursor = root.get("next_cursor")
            elif isinstance(root.get("data"), list):
                items = root["data"]
            elif isinstance(root.get("data"), dict) and isinstance(root["data"].get("list"), list):
                items = root["data"]["list"]
                pi = root["data"].get("page_info") or {}
                if isinstance(pi, dict):
                    next_cursor = pi.get("cursor") or pi.get("next_cursor")
        elif isinstance(root, list):
            items = root

        if not next_cursor and isinstance(payload.get("page_info"), dict):
            next_cursor = payload["page_info"].get("cursor") or payload["page_info"].get("next_cursor")

        return items, next_cursor

    async def _paged_get(
        self,
        path: str,
        *,
        method: str = "GET",
        query: Dict[str, Any] | None = None,
        body: Dict[str, Any] | None = None,
        cursor_param: str = "cursor",
        limit_param: str = "page_size",
        limit: int | None = None,
    ) -> AsyncIterator[dict]:
        q = dict(query or {})
        b = dict(body or {})
        if limit:
            if method.upper() == "GET":
                q.setdefault(limit_param, limit)
            else:
                b.setdefault(limit_param, limit)

        cursor: Optional[str] = q.get(cursor_param) or b.get(cursor_param)

        while True:
            if cursor:
                if method.upper() == "GET":
                    q[cursor_param] = cursor
                else:
                    b[cursor_param] = cursor

            data = await self._request_json(
                method,
                path,
                params=q if method.upper() == "GET" else None,
                json_body=None if method.upper() == "GET" else b,
            )
            items, next_cursor = self._extract_list_and_cursor(data)
            for it in items or []:
                if isinstance(it, dict):
                    yield it

            if not next_cursor:
                break
            cursor = next_cursor

    # ---- 按页分页（page/page_size）----
    async def _paged_by_page(
        self,
        path: str,
        *,
        base_query: Dict[str, Any],
        page_param: str = "page",
        page_size_param: str = "page_size",
        page_size: int = 100,
    ) -> AsyncIterator[dict]:
        page = int(base_query.get(page_param, 1))
        while True:
            q = dict(base_query)
            q[page_param] = page
            q[page_size_param] = page_size
            data = await self._request_json("GET", path, params=q, json_body=None)

            root = data.get("data") if isinstance(data.get("data"), (dict, list)) else data
            items = []
            has_more = None
            if isinstance(root, dict):
                items = root.get("list") or root.get("items") or root.get("data") or []
                pi = root.get("page_info") or {}
                if isinstance(pi, dict) and "has_more" in pi:
                    has_more = bool(pi.get("has_more"))

            for it in items or []:
                if isinstance(it, dict):
                    yield it

            if has_more is None:
                has_more = bool(items) and len(items) >= page_size

            if not has_more:
                break
            page += 1

    # ---- 业务 API ----
    def paths(self) -> TTBPaths:
        return self._paths

    async def iter_business_centers(self, *, limit: int = 200) -> AsyncIterator[dict]:
        async for it in self._paged_get(self._paths.bc_get, method="GET", limit_param="page_size", limit=limit):
            yield it

    async def iter_advertisers(
        self, *, limit: int = 200, app_id: Optional[str] = None, secret: Optional[str] = None
    ) -> AsyncIterator[dict]:
        # 官方文档：需要 app_id/secret（或等价字段）+ Access-Token
        q: Dict[str, Any] = {}
        if app_id and secret:
            # 兼容参数名（不同文档版本有 app_id/secret 或 client_id/client_secret）
            q["app_id"] = app_id
            q["secret"] = secret
        async for it in self._paged_get(self._paths.advertiser_get, method="GET", query=q, limit_param="page_size", limit=limit):
            yield it

    async def iter_shops(self, *, advertiser_id: str, page_size: int = 100) -> AsyncIterator[dict]:
        base_q = {"advertiser_id": advertiser_id}
        async for it in self._paged_by_page(self._paths.shop_get, base_query=base_q, page_size=page_size):
            yield it

    async def iter_products(
        self,
        *,
        bc_id: str,
        store_id: str,
        advertiser_id: Optional[str] = None,
        page_size: int = 100,
    ) -> AsyncIterator[dict]:
        base_q: Dict[str, Any] = {"bc_id": bc_id, "store_id": store_id}
        if advertiser_id:
            base_q["advertiser_id"] = advertiser_id
        async for it in self._paged_by_page(self._paths.product_get, base_query=base_q, page_size=page_size):
            yield it

