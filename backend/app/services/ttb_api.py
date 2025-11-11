# app/services/ttb_api.py
from __future__ import annotations

"""
TikTok Business API 客户端：
- 只暴露 5 个读取器（异步）：
    * iter_business_centers()  -> /bc/get/（data.list + 可能的 page_info.cursor 或 page/page_size）
    * iter_advertisers()       -> /oauth2/advertiser/get/（data.list；cursor 分页；本文件里会自动附加 app_id/secret）
    * fetch_advertiser_info()  -> /advertiser/info/（GET，query: advertiser_ids, fields 均为 JSON 数组字符串）
    * iter_stores()            -> /store/list/  （data.stores，页码分页）
    * iter_products()          -> /store/product/get/（data.store_products，页码分页）
- URL 统一通过 app.services.ttb_http.build_url 构造，不重复 open_api/v1.3。
- 令牌桶限速（默认 10 QPS），429/5xx 指数退避，page_size 强制上限 50（支持分页的接口才生效）。
"""

import asyncio
import json
import math
import time
import logging
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


logger = logging.getLogger("gmv.ttb.http")


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


def _remove_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _remove_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple, set)):
        return [_remove_none(v) for v in value if v is not None]
    return value


def _clean_params_map(data: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        cleaned[key] = _remove_none(value)
    return cleaned


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
      - TTB_BC_GET             (默认 "bc/get/")
      - TTB_ADVERTISERS_GET    (默认 "oauth2/advertiser/get/")
      - TTB_ADVERTISER_INFO    (默认 "advertiser/info/")
      - TTB_STORES_LIST        (默认 "store/list/")
      - TTB_PRODUCTS_LIST      (默认 "store/product/get/")
    """

    bc_get: str
    advertisers_get: str
    advertiser_info: str
    stores_list: str
    products_list: str

    @classmethod
    def from_settings(cls) -> "TTBPaths":
        def g(name: str, default_rel: str) -> str:
            val = getattr(settings, name, None)
            return str(val).strip() if val else default_rel

        return cls(
            bc_get=g("TTB_BC_GET", "bc/get/"),
            advertisers_get=g("TTB_ADVERTISERS_GET", "oauth2/advertiser/get/"),
            advertiser_info=g("TTB_ADVERTISER_INFO", "advertiser/info/"),
            stores_list=g("TTB_STORES_LIST", "store/list/"),
            products_list=g("TTB_PRODUCTS_LIST", "store/product/get/"),
        )


# --------------------------- 客户端主体 ---------------------------


class TTBApiClient:
    """
    读取器：
      - iter_business_centers()
      - iter_advertisers()
      - fetch_advertiser_info()
      - iter_stores()
      - iter_products()
    """

    def __init__(
        self,
        *,
        access_token: str,
        app_id: str | None = None,
        app_secret: str | None = None,
        qps: float | None = None,
        timeout: float | None = None,
        headers: Optional[Dict[str, str]] = None,
        **_: Any,  # 吃掉将来多传的 keyword（比如 limits 等），避免 unexpected keyword argument
    ) -> None:
        if not access_token:
            raise TTBApiError("missing access token")

        self._paths = TTBPaths.from_settings()
        self._app_id = app_id
        self._app_secret = app_secret

        default_qps = float(getattr(settings, "TTB_API_DEFAULT_QPS", 5.0))
        self._bucket = TokenBucket(rate_per_sec=float(qps or default_qps))
        self._timeout = timeout or float(
            getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15.0)
        )

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
        json_body: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        await self._bucket.acquire()

        params = dict(params or {})

        # 只有支持分页的接口才会校正 page_size
        if "page_size" in params:
            params["page_size"] = _clamp_page_size(params["page_size"])

        # 对 /oauth2/advertiser/get/ 自动附加 app_id / secret（若提供）
        needs_app_credentials = path.rstrip("/") == self._paths.advertisers_get.rstrip("/")
        if needs_app_credentials and self._app_id and self._app_secret:
            params.setdefault("app_id", self._app_id)
            params.setdefault("secret", self._app_secret)

        url = build_url(path)
        resp = await self._client.request(method, url, params=params, json=json_body)

        status = resp.status_code
        text = resp.text

        if status in (429, 500, 502, 503, 504):
            # 这些是可重试错误，tenacity 会自动重试
            raise TTBHttpError(status, "retryable", payload=text)

        if status >= 400:
            # 不可重试的 HTTP 错误：打详细日志，再抛出
            logger.error(
                "TTB HTTP non-retryable error method=%s url=%s status=%s body=%s",
                method,
                url,
                status,
                text[:1000],
            )
            raise TTBHttpError(status, "client/server error", payload=text)

        try:
            data = resp.json()
        except Exception:
            logger.error(
                "TTB HTTP invalid json method=%s url=%s status=%s body=%s",
                method,
                url,
                status,
                text[:1000],
            )
            raise TTBApiError("invalid json response", payload=text, status=status)

        code = data.get("code")
        if code not in (0, "0", None):
            logger.error(
                "TTB API business error method=%s url=%s status=%s code=%s body=%s",
                method,
                url,
                status,
                code,
                json.dumps(data, ensure_ascii=False)[:1000],
            )
            raise TTBApiError(
                data.get("message") or "api error",
                code=code,
                payload=data,
                status=status,
            )
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
    def _extract_stores(payload: Dict[str, Any]) -> Tuple[Iterable[dict], bool]:
        data = payload.get("data") or {}
        items = data.get("stores") or []
        if not isinstance(items, list):
            items = []
        return items, bool(items)

    @staticmethod
    def _extract_products(payload: Dict[str, Any]) -> Tuple[Iterable[dict], bool]:
        data = payload.get("data") or {}
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
        extractor: Literal["stores", "products"],
    ) -> AsyncIterator[dict]:
        page = 1
        size = _clamp_page_size(page_size)

        while True:
            params = dict(base_params or {})
            params[page_param] = page
            params["page_size"] = size

            payload = await self._request_json(method, path, params=params)
            if extractor == "stores":
                items, _ = self._extract_stores(payload)
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

    async def iter_business_centers(
        self,
        *,
        page_size: int = _MAX_PAGE_SIZE,
    ) -> AsyncIterator[dict]:
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
        page_size: int = _MAX_PAGE_SIZE,
    ) -> AsyncIterator[dict]:
        """
        /oauth2/advertiser/get/ 返回 data.list；当存在 page_info.cursor 时按游标分页。
        本方法内部会自动附加 app_id / secret（如果在构造 TTBApiClient 时提供）。
        """
        async for item in self._paged_cursor(
            method="GET",
            path=self._paths.advertisers_get,
            base_params={},
            page_size=page_size,
        ):
            yield item

    async def fetch_advertiser_info(
        self,
        *,
        advertiser_ids: Iterable[str],
        fields: Iterable[str] | None = None,
    ) -> list[dict]:
        """
        GET /advertiser/info/
        - Header: Access-Token
        - Query:
            advertiser_ids: JSON list string, e.g. ["123","456"]
            fields: JSON list string, e.g. ["advertiser_id","name",...]
        """
        ids: list[str] = []
        for value in advertiser_ids:
            if value is None:
                continue
            s = str(value).strip()
            if not s:
                continue
            ids.append(s)
        if not ids:
            return []

        params: Dict[str, Any] = {
            "advertiser_ids": json.dumps(ids, ensure_ascii=False),
        }

        if fields:
            unique_fields: list[str] = []
            seen: set[str] = set()
            for field in fields:
                if not field:
                    continue
                key = str(field).strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                unique_fields.append(key)
            if unique_fields:
                params["fields"] = json.dumps(unique_fields, ensure_ascii=False)

        response = await self._request_json(
            "GET",
            self._paths.advertiser_info,
            params=params,
        )
        data = response.get("data") or {}
        candidates = []
        for key in ("list", "advertiser_list", "advertiser_infos", "advertisers"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if not isinstance(candidates, list):
            return []
        return [item for item in candidates if isinstance(item, dict)]

    async def iter_stores(
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
            path=self._paths.stores_list,
            base_params=params,
            page_param="page",
            page_size=page_size,
            extractor="stores",
        ):
            yield it

    async def iter_products(
        self,
        *,
        store_id: str,
        bc_id: Optional[str] = None,
        advertiser_id: Optional[str] = None,
        page_size: int = _MAX_PAGE_SIZE,
        eligibility: Optional[Literal["GMV_MAX", "CUSTOM_STORE_ADS"]] = None,
        product_name: Optional[str] = None,
        item_group_ids: Optional[list[str]] = None,
    ) -> AsyncIterator[dict]:
        params: Dict[str, Any] = {
            "store_id": str(store_id),
        }
        if bc_id:
            params["bc_id"] = str(bc_id)
        if product_name:
            params["product_name"] = product_name
        if item_group_ids:
            params["item_group_ids"] = item_group_ids[:10]
        if eligibility:
            if not advertiser_id:
                raise TTBApiError(
                    "advertiser_id is required when filtering by eligibility",
                    code="MISSING_ADVERTISER",
                )
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

    # ---------- GMV Max ----------

    async def list_gmvmax_stores(self, advertiser_id: str, **kwargs: Any) -> dict:
        params: Dict[str, Any] = {"advertiser_id": str(advertiser_id)}
        params.update(kwargs)
        payload = await self._request_json(
            "GET",
            "/gmv_max/store/list/",
            params=_clean_params_map(params),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def get_gmvmax_exclusive_auth(
        self,
        advertiser_id: str,
        store_id: str,
        store_authorized_bc_id: str,
    ) -> dict:
        params = _clean_params_map(
            {
                "advertiser_id": str(advertiser_id),
                "store_id": str(store_id),
                "store_authorized_bc_id": str(store_authorized_bc_id),
            }
        )
        payload = await self._request_json(
            "GET",
            "/gmv_max/exclusive_authorization/get/",
            params=params,
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def create_gmvmax_exclusive_auth(
        self,
        advertiser_id: str,
        store_id: str,
        store_authorized_bc_id: str,
    ) -> dict:
        params = _clean_params_map({"advertiser_id": str(advertiser_id)})
        body = _remove_none(
            {
                "store_id": str(store_id),
                "store_authorized_bc_id": str(store_authorized_bc_id),
            }
        )
        payload = await self._request_json(
            "POST",
            "/gmv_max/exclusive_authorization/create/",
            params=params,
            json_body=body,
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def list_gmvmax_identities(
        self,
        advertiser_id: str,
        store_id: str,
        store_authorized_bc_id: str,
    ) -> dict:
        params = _clean_params_map(
            {
                "advertiser_id": str(advertiser_id),
                "store_id": str(store_id),
                "store_authorized_bc_id": str(store_authorized_bc_id),
            }
        )
        payload = await self._request_json(
            "GET",
            "/gmv_max/identity/get/",
            params=params,
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def list_gmvmax_videos(
        self,
        advertiser_id: str,
        store_id: str,
        spu_id_list: list[str] | None = None,
        **filters: Any,
    ) -> dict:
        params: Dict[str, Any] = {
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
        }
        if spu_id_list is not None:
            params["spu_id_list"] = [str(item) for item in spu_id_list if item is not None]
        params.update(filters)
        payload = await self._request_json(
            "GET",
            "/gmv_max/video/get/",
            params=_clean_params_map(params),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def recommend_gmvmax_bid(
        self,
        advertiser_id: str,
        store_id: str,
        shopping_ads_type: str,
        optimization_goal: str,
        item_group_ids: list[str],
        identity_id: str | None = None,
    ) -> dict:
        params: Dict[str, Any] = {
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
            "shopping_ads_type": shopping_ads_type,
            "optimization_goal": optimization_goal,
            "item_group_ids": [str(item) for item in item_group_ids if item is not None],
            "identity_id": str(identity_id) if identity_id is not None else None,
        }
        payload = await self._request_json(
            "GET",
            "/gmv_max/bid/recommend/",
            params=_clean_params_map(params),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def create_gmvmax_campaign(self, advertiser_id: str, body: dict) -> dict:
        params = _clean_params_map({"advertiser_id": str(advertiser_id)})
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/create/",
            params=params,
            json_body=_remove_none(dict(body or {})),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def update_gmvmax_campaign(self, advertiser_id: str, body: dict) -> dict:
        params = _clean_params_map({"advertiser_id": str(advertiser_id)})
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/update/",
            params=params,
            json_body=_remove_none(dict(body or {})),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def fetch_gmvmax_campaigns(self, advertiser_id: str, **filters: Any) -> dict:
        params: Dict[str, Any] = {"advertiser_id": str(advertiser_id)}
        params.update(filters)
        payload = await self._request_json(
            "GET",
            "/gmv_max/campaign/get/",
            params=_clean_params_map(params),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def iter_gmvmax_campaigns(
        self,
        advertiser_id: str,
        **filters: Any,
    ) -> AsyncIterator[dict]:
        base_filters = dict(filters)
        page_size_value = base_filters.pop("page_size", None)
        cursor_value = base_filters.pop("cursor", None)
        page_token_value = base_filters.pop("page_token", None)
        page_value_raw = base_filters.pop("page", None)
        if cursor_value is None and page_token_value is None:
            if page_value_raw is None:
                page_value = 1
            else:
                try:
                    page_value = int(page_value_raw)
                except (TypeError, ValueError):
                    page_value = 1
        else:
            try:
                page_value = int(page_value_raw) if page_value_raw is not None else None
            except (TypeError, ValueError):
                page_value = None
        base_filters_clean = _clean_params_map(base_filters)

        while True:
            query: Dict[str, Any] = dict(base_filters_clean)
            if page_size_value is not None:
                query["page_size"] = page_size_value
            if cursor_value is not None:
                query["cursor"] = cursor_value
            if page_token_value is not None:
                query["page_token"] = page_token_value
            if page_value is not None:
                query["page"] = page_value

            data = await self.fetch_gmvmax_campaigns(advertiser_id, **query)

            items_list: list[dict] = []
            page_info: Dict[str, Any] | None = None
            if isinstance(data, dict):
                for key in ("list", "campaign_list", "items", "campaigns"):
                    value = data.get(key)
                    if isinstance(value, list):
                        items_list = [item for item in value if isinstance(item, dict)]
                        if items_list:
                            break
                raw_page_info = data.get("page_info")
                if isinstance(raw_page_info, dict):
                    page_info = raw_page_info

            for item in items_list:
                yield item

            if not items_list and not cursor_value and not page_token_value:
                break

            advanced = False
            info_dict = page_info or {}
            next_cursor = info_dict.get("cursor")
            next_page_token = info_dict.get("page_token")
            has_more_flags = info_dict.get("has_more") or info_dict.get("has_next") or info_dict.get("has_next_page")

            if next_cursor:
                if next_cursor != cursor_value:
                    cursor_value = next_cursor
                    page_token_value = None
                    page_value = None
                    advanced = True
            elif next_page_token:
                if next_page_token != page_token_value:
                    page_token_value = next_page_token
                    cursor_value = None
                    page_value = None
                    advanced = True
            else:
                current_page = info_dict.get("page")
                total_page = info_dict.get("total_page")
                try:
                    current_page_int = int(current_page) if current_page is not None else None
                except (TypeError, ValueError):
                    current_page_int = None
                try:
                    total_page_int = int(total_page) if total_page is not None else None
                except (TypeError, ValueError):
                    total_page_int = None

                if current_page_int is not None and total_page_int is not None and current_page_int < total_page_int:
                    page_value = current_page_int + 1
                    cursor_value = None
                    page_token_value = None
                    advanced = True
                elif has_more_flags in (True, 1):
                    next_page_number = (current_page_int or page_value or 1) + 1
                    page_value = next_page_number
                    cursor_value = None
                    page_token_value = None
                    advanced = True

            if not advanced:
                expected_size = None
                if page_size_value is not None:
                    try:
                        expected_size = int(page_size_value)
                    except (TypeError, ValueError):
                        expected_size = None
                if expected_size and len(items_list) == expected_size:
                    page_value = (page_value or 1) + 1
                    cursor_value = None
                    page_token_value = None
                    advanced = True

            if not advanced:
                break

    async def get_gmvmax_campaign_info(
        self,
        advertiser_id: str,
        campaign_id: str,
    ) -> dict:
        params = _clean_params_map(
            {
                "advertiser_id": str(advertiser_id),
                "campaign_id": str(campaign_id),
            }
        )
        payload = await self._request_json(
            "GET",
            "/campaign/gmv_max/info/",
            params=params,
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def report_gmvmax(
        self,
        advertiser_id: str,
        start_date: str,
        end_date: str,
        time_granularity: str = "HOUR",
        metrics: list[str] | None = None,
        **filters: Any,
    ) -> dict:
        params: Dict[str, Any] = {
            "advertiser_id": str(advertiser_id),
            "start_date": start_date,
            "end_date": end_date,
            "time_granularity": time_granularity,
        }
        if metrics is not None:
            params["metrics"] = [str(metric) for metric in metrics if metric is not None]
        params.update(filters)
        payload = await self._request_json(
            "GET",
            "/gmv_max/report/get/",
            params=_clean_params_map(params),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def create_gmvmax_session(self, advertiser_id: str, body: dict) -> dict:
        params = _clean_params_map({"advertiser_id": str(advertiser_id)})
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/create/",
            params=params,
            json_body=_remove_none(dict(body or {})),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def update_gmvmax_session(self, advertiser_id: str, body: dict) -> dict:
        params = _clean_params_map({"advertiser_id": str(advertiser_id)})
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/update/",
            params=params,
            json_body=_remove_none(dict(body or {})),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def delete_gmvmax_session(self, advertiser_id: str, session_id: str) -> dict:
        params = _clean_params_map({"advertiser_id": str(advertiser_id)})
        body = _remove_none({"session_id": str(session_id)})
        payload = await self._request_json(
            "POST",
            "/campaign/gmv_max/session/delete/",
            params=params,
            json_body=body,
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def get_gmvmax_session(
        self,
        advertiser_id: str,
        session_id: str,
    ) -> dict:
        params = _clean_params_map(
            {
                "advertiser_id": str(advertiser_id),
                "session_ids": [str(session_id)],
            }
        )
        payload = await self._request_json(
            "GET",
            "/campaign/gmv_max/session/get/",
            params=params,
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def list_gmvmax_sessions(
        self,
        advertiser_id: str,
        campaign_id: str,
        **paging: Any,
    ) -> dict:
        params: Dict[str, Any] = {
            "advertiser_id": str(advertiser_id),
            "campaign_id": str(campaign_id),
        }
        params.update(paging)
        payload = await self._request_json(
            "GET",
            "/campaign/gmv_max/session/list/",
            params=_clean_params_map(params),
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}


__all__ = ["TTBApiClient", "TTBApiError", "TTBHttpError", "TTBPaths"]

