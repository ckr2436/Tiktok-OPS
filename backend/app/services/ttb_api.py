# app/services/ttb_api.py
from __future__ import annotations

"""
TikTok Business API 客户端（严格版）：
- 只暴露 4 个读取器（异步）：
    * iter_business_centers()  -> /bc/get/      （data.list + 可能的 page_info.cursor 或 page/page_size）
    * iter_advertisers()       -> /oauth2/advertiser/get/（data.list；不分页；必须传 app_id/secret）
    * iter_shops()             -> /store/list/  （data.stores，页码分页）
    * iter_products()          -> /store/product/get/（data.store_products，页码分页）
- URL 统一通过 app.services.ttb_http.build_url 构造，不重复 open_api/v1.3。
- 令牌桶限速（默认 10 QPS），429/5xx 指数退避，page_size 强制上限 50（支持分页的接口才生效）。
"""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Iterable, Optional, Tuple, Literal

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

from app.core.config import settings
from app.services.ttb_http import build_url


# --------------------------- 错误类型 ---------------------------
class TTBApiError(Exception):
    """业务层错误（HTTP 2xx 但 code 非 0）"""

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
    """HTTP 层错误（4xx/5xx/429 触发重试或失败）"""

    def __init__(self, status: int, message: str, *, payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.payload = payload


# --------------------------- 常量/限流 ---------------------------
_MAX_PAGE_SIZE = 50  # 官方上限

def _clamp_page_size(x: Any, default: int = _MAX_PAGE_SIZE) -> int:
    try:
        n = int(x)
    except Exception:
        n = default
    return n if n <= _MAX_PAGE_SIZE else _MAX_PAGE_SIZE


class TokenBucket:
    """简单令牌桶，确保 QPS 上限"""

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


# --------------------------- 端点路径（来自 settings，可覆盖） ---------------------------
@dataclass(frozen=True, slots=True)
class TTBPaths:
    """
    仅支持以下固定 settings 覆盖项（可写相对/绝对路径）：
      - TTB_BC_GET            (默认 "bc/get/")
      - TTB_ADVERTISERS_GET   (默认 "oauth2/advertiser/get/")
      - TTB_SHOPS_LIST        (默认 "store/list/")
      - TTB_PRODUCTS_LIST     (默认 "store/product/get/")
    """

    bc_get: str
    advertisers_get: str
    shops_list: str
    products_list: str

    @classmethod
    def from_settings(cls) -> "TTBPaths":
        def g(name: str, default_rel: str) -> str:
            val = getattr(settings, name, None)
            return str(val).strip() if val else default_rel

        return cls(
            bc_get=g("TTB_BC_GET", "bc/get/"),
            advertisers_get=g("TTB_ADVERTISERS_GET", "oauth2/advertiser/get/"),
            shops_list=g("TTB_SHOPS_LIST", "store/list/"),
            products_list=g("TTB_PRODUCTS_LIST", "store/product/get/"),
        )


# --------------------------- 客户端主体 ---------------------------
class TTBApiClient:
    """
    读取器：
      - iter_business_centers()
      - iter_advertisers()
      - iter_shops()
      - iter_products()
    """

    def __init__(
        self,
        *,
        access_token: str,
        qps: float = 10.0,
        timeout: float | None = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        if not access_token:
            raise TTBApiError("missing access token")
        self._paths = TTBPaths.from_settings()
        self._bucket = TokenBucket(rate_per_sec=qps)
        self._timeout = timeout or float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15.0))

        # Access-Token 头是必须的
        default_headers = {
            "Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            default_headers.update(headers)

        self._client = httpx.AsyncClient(timeout=self._timeout, headers=default_headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- 请求基元 ----------
    @retry(
        retry=retry_if_exception_type((TTBHttpError, httpx.TransportError)),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        await self._bucket.acquire()

        # 只有支持分页的接口才会校正 page_size
        if params and "page_size" in params:
            params = dict(params)
            params["page_size"] = _clamp_page_size(params["page_size"])

        url = build_url(path)
        resp = await self._client.request(method, url, params=params)

        if resp.status_code in (429, 500, 502, 503, 504):
            raise TTBHttpError(resp.status_code, "retryable")
        if resp.status_code >= 400:
            raise TTBHttpError(resp.status_code, "client/server error", payload=resp.text)

        try:
            data = resp.json()
        except Exception:
            raise TTBApiError("invalid json response", payload=resp.text, status=resp.status_code)

        code = data.get("code")
        if code not in (0, "0", None):
            raise TTBApiError(data.get("message") or "api error", code=code, payload=data, status=resp.status_code)
        return data

    # ---------- 提取器 ----------
    @staticmethod
    def _extract_list_page(payload: Dict[str, Any]) -> Tuple[Iterable[dict], Optional[str]]:
        """
        用于 data.list + 可能的 page_info.cursor 的接口。
        """
        data = payload.get("data") or {}
        items = data.get("list") or []
        if not isinstance(items, list):
            items = []
        page_info = data.get("page_info") or {}
        cursor = page_info.get("cursor") if isinstance(page_info, dict) else None
        return items, cursor

    @staticmethod
    def _extract_shops(payload: Dict[str, Any]) -> Tuple[Iterable[dict], bool]:
        data = (payload.get("data") or {})
        items = data.get("stores") or []
        if not isinstance(items, list):
            items = []
        return items, bool(items)

    @staticmethod
    def _extract_products(payload: Dict[str, Any]) -> Tuple[Iterable[dict], bool]:
        data = (payload.get("data") or {})
        items = data.get("store_products") or []
        if not isinstance(items, list):
            items = []
        return items, bool(items)

    # ---------- 分页 ----------
    async def _paged_cursor(
        self,
        *,
        method: str,
        path: str,
        base_params: Dict[str, Any] | None = None,
        page_size: int = _MAX_PAGE_SIZE,
    ) -> AsyncIterator[dict]:
        params = dict(base_params or {})
        params["page_size"] = _clamp_page_size(page_size)
        cursor: Optional[str] = None

        while True:
            if cursor:
                params["cursor"] = cursor
            payload = await self._request_json(method, path, params=params)
            items, next_cursor = self._extract_list_page(payload)
            for it in items:
                if isinstance(it, dict):
                    yield it
            if not next_cursor:
                break
            cursor = next_cursor

    async def _paged_by_page(
        self,
        *,
        method: str,
        path: str,
        base_params: Dict[str, Any] | None = None,
        page_param: str = "page",
        page_size: int = _MAX_PAGE_SIZE,
        extractor: Literal["shops", "products"],
    ) -> AsyncIterator[dict]:
        page = 1
        size = _clamp_page_size(page_size)

        while True:
            params = dict(base_params or {})
            params[page_param] = page
            params["page_size"] = size

            payload = await self._request_json(method, path, params=params)
            if extractor == "shops":
                items, _ = self._extract_shops(payload)
            elif extractor == "products":
                items, _ = self._extract_products(payload)
            else:
                raise RuntimeError("unknown extractor")

            count = 0
            for it in items:
                count += 1
                if isinstance(it, dict):
                    yield it

            if count < size:
                break
            page += 1

    # ---------- 公共读取器 ----------
    async def iter_business_centers(self, *, page_size: int = _MAX_PAGE_SIZE) -> AsyncIterator[dict]:
        async for it in self._paged_cursor(
            method="GET",
            path=self._paths.bc_get,
            base_params={},
            page_size=page_size,
        ):
            yield it

    async def iter_advertisers(
        self,
        *,
        app_id: str,
        secret: str,
        page_size: int = _MAX_PAGE_SIZE,  # 保留形参以兼容调用方，但此接口不分页
    ) -> AsyncIterator[dict]:
        """
        /oauth2/advertiser/get/ 返回 data.list；无分页参数。
        需要 query: app_id, secret
        """
        payload = await self._request_json(
            "GET",
            self._paths.advertisers_get,
            params={"app_id": str(app_id), "secret": str(secret)},
        )
        data = payload.get("data") or {}
        items = data.get("list") or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    yield it

    async def iter_shops(
        self,
        *,
        advertiser_id: Optional[str] = None,
        bc_id: Optional[str] = None,
        page_size: int = _MAX_PAGE_SIZE,
    ) -> AsyncIterator[dict]:
        params: Dict[str, Any] = {}
        if advertiser_id:
            params["advertiser_id"] = str(advertiser_id)
        if bc_id:
            params["bc_id"] = str(bc_id)
        async for it in self._paged_by_page(
            method="GET",
            path=self._paths.shops_list,
            base_params=params,
            page_param="page",
            page_size=page_size,
            extractor="shops",
        ):
            yield it

    async def iter_products(
        self,
        *,
        bc_id: str,
        store_id: str,
        advertiser_id: Optional[str] = None,
        page_size: int = _MAX_PAGE_SIZE,
        eligibility: Optional[Literal["GMV_MAX", "CUSTOM_SHOP_ADS"]] = None,
        product_name: Optional[str] = None,
        item_group_ids: Optional[list[str]] = None,
    ) -> AsyncIterator[dict]:
        params: Dict[str, Any] = {
            "bc_id": str(bc_id),
            "store_id": str(store_id),
        }
        if product_name:
            params["product_name"] = product_name
        if item_group_ids:
            params["item_group_ids"] = item_group_ids[:10]
        if eligibility:
            if not advertiser_id:
                raise TTBApiError("advertiser_id is required when filtering by eligibility", code="MISSING_ADVERTISER")
            params["advertiser_id"] = str(advertiser_id)
            params["filtering"] = json.dumps({"ad_creation_eligible": eligibility})
            params["ad_creation_eligible"] = eligibility

        async for it in self._paged_by_page(
            method="GET",
            path=self._paths.products_list,
            base_params=params,
            page_param="page",
            page_size=page_size,
            extractor="products",
        ):
            yield it


__all__ = ["TTBApiClient", "TTBApiError", "TTBHttpError", "TTBPaths"]

