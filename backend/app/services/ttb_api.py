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
- 令牌桶限速（默认 10 QPS），429/5xx 指数退避，并按端点执行官方 page_size 上限。
"""

import asyncio
import hashlib
import json
import math
import time
import logging
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

from app.core.config import settings
from app.services.redis_client import get_redis_sync
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


class TTBBusinessError(TTBApiError):
    """Non-retryable business error returned by TikTok Business APIs."""


class TTBHttpError(Exception):
    """HTTP 层错误（4xx/5xx/429 触发重试或失败）"""

    def __init__(self, status: int, message: str, *, payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.payload = payload


class TTBRateLimitBudgetError(TTBApiError):
    """A shared app or endpoint quota cannot be acquired within the wait budget."""


class TTBPaginationError(TTBApiError):
    """TikTok pagination metadata is contradictory or cannot make progress."""


def ttb_retry_countdown(
    exc: BaseException,
    *,
    default_seconds: int = 60,
    maximum_seconds: int = 6 * 60 * 60,
) -> int:
    """Honor a shared quota reset without creating a one-minute retry storm."""

    retry_after_ms: int | None = None
    payload = getattr(exc, "payload", None)
    if isinstance(payload, Mapping):
        try:
            retry_after_ms = int(payload.get("retry_after_ms"))
        except (TypeError, ValueError):
            retry_after_ms = None
    if retry_after_ms is None or retry_after_ms <= 0:
        return max(1, min(int(default_seconds), int(maximum_seconds)))
    # Add a small drain margin so workers do not all wake on the exact Redis
    # window boundary.
    seconds = math.ceil(retry_after_ms / 1000) + 5
    return max(1, min(int(seconds), int(maximum_seconds)))


_RATE_LIMIT_SCRIPT = b"""
for index = 1, #KEYS do
    local current = tonumber(redis.call('GET', KEYS[index]) or '0')
    local limit = tonumber(ARGV[index])
    if current >= limit then
        return {0, index, redis.call('PTTL', KEYS[index])}
    end
end
for index = 1, #KEYS do
    local current = redis.call('INCR', KEYS[index])
    if current == 1 then
        redis.call('PEXPIRE', KEYS[index], ARGV[#KEYS + index])
    end
end
return {1, 0, 0}
"""

_COOLDOWN_SCRIPT = b"""
local current = redis.call('PTTL', KEYS[1])
local requested = tonumber(ARGV[1])
if current < requested then
    redis.call('SET', KEYS[1], '1', 'PX', requested)
    return requested
end
return current
"""


# --------------------------- 常量/限流 ---------------------------

_MAX_PAGE_SIZE = 50
_TIKTOK_LARGE_PAGE_SIZE = 1000
_MAX_PAGINATION_PAGES = 2000

_ALLOWED_GMV_MAX_PROMOTION_TYPES: tuple[str, str] = (
    "PRODUCT",
    "LIVE",
)

_DEFAULT_GMV_MAX_PROMOTION_TYPES = _ALLOWED_GMV_MAX_PROMOTION_TYPES

_PromotionTypeFormat = Literal["campaign", "report"]

_PROMOTION_TYPE_FORMATTERS: Dict[_PromotionTypeFormat, Dict[str, str]] = {
    "campaign": {
        "PRODUCT": "PRODUCT_GMV_MAX",
        "LIVE": "LIVE_GMV_MAX",
    },
    "report": {
        "PRODUCT": "PRODUCT",
        "LIVE": "LIVE",
    },
}

_GMV_MAX_PROMOTION_TYPE_ALIASES: Dict[str, str] = {
    "PRODUCT": "PRODUCT",
    "PRODUCT_GMV_MAX": "PRODUCT",
    "LIVE": "LIVE",
    "LIVE_GMV_MAX": "LIVE",
}


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


def _encode_query_arrays(params: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Encode TikTok GET array parameters using the v1.3 JSON-string contract."""
    for key in keys:
        value = params.get(key)
        if isinstance(value, (list, tuple, set)):
            params[key] = json.dumps(
                [_remove_none(item) for item in value if item is not None],
                ensure_ascii=False,
            )
    return params


def _normalize_promotion_types(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
        candidates = list(decoded) if isinstance(decoded, list) else [decoded]
    elif isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            canonical = _GMV_MAX_PROMOTION_TYPE_ALIASES.get(text.upper(), text)
            if canonical not in seen:
                normalized.append(canonical)
                seen.add(canonical)
    return normalized


def _ensure_gmvmax_campaign_filters(
    params: Dict[str, Any], *, promotion_type_format: _PromotionTypeFormat = "campaign"
) -> None:
    """确保 GMV Max Campaigns 查询包含必填的过滤项。"""

    top_level_raw = params.get("gmv_max_promotion_types")
    filtering_raw = params.get("filtering")
    if isinstance(filtering_raw, str):
        try:
            filtering_dict = json.loads(filtering_raw)
        except json.JSONDecodeError:
            filtering_dict = {}
    elif isinstance(filtering_raw, dict):
        filtering_dict = dict(filtering_raw)
    else:
        filtering_dict = {}

    formatter = _PROMOTION_TYPE_FORMATTERS.get(
        promotion_type_format, _PROMOTION_TYPE_FORMATTERS["campaign"]
    )
    allowed_canonical = tuple(formatter.keys())

    promotion_types = [
        item
        for item in _normalize_promotion_types(filtering_dict.get("gmv_max_promotion_types"))
        if item in allowed_canonical
    ]
    if not promotion_types:
        fallback = [
            item
            for item in _normalize_promotion_types(top_level_raw)
            if item in allowed_canonical
        ]
        if not fallback:
            fallback = [item for item in _DEFAULT_GMV_MAX_PROMOTION_TYPES if item in allowed_canonical]
        promotion_types = fallback

    formatted_types = [formatter[item] for item in promotion_types]
    filtering_dict["gmv_max_promotion_types"] = formatted_types

    if top_level_raw is not None:
        params["gmv_max_promotion_types"] = json.dumps(
            formatted_types,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        params.pop("gmv_max_promotion_types", None)

    cleaned_filtering = _remove_none(filtering_dict)
    params["filtering"] = json.dumps(
        cleaned_filtering,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _page_size_limit(path: str) -> int:
    """Return the documented limit for a paginated TikTok endpoint."""

    normalized = f"/{str(path or '').strip('/')}/"
    if normalized in {
        "/gmv_max/report/get/",
        "/report/integrated/get/",
        "/ad/get/",
    }:
        return _TIKTOK_LARGE_PAGE_SIZE
    if normalized in {
        "/gmv_max/campaign/get/",
        "/identity/get/",
    }:
        return 100
    if normalized == "/store/product/get/":
        return 100
    if normalized == "/file/video/ad/search/":
        return 100
    return _MAX_PAGE_SIZE


def _clamp_page_size(
    x: Any,
    default: int = _MAX_PAGE_SIZE,
    *,
    maximum: int = _MAX_PAGE_SIZE,
) -> int:
    try:
        n = int(x)
    except Exception:
        n = default
    return max(1, min(n, int(maximum)))


def _pagination_int(
    value: Any,
    *,
    field: str,
    minimum: int,
    page_info: Mapping[str, Any],
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TTBPaginationError(
            f"TikTok pagination field {field} must be an integer",
            code="PAGINATION_METADATA_INVALID",
            payload={"field": field, "value": value, "page_info": dict(page_info)},
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TTBPaginationError(
            f"TikTok pagination field {field} must be an integer",
            code="PAGINATION_METADATA_INVALID",
            payload={"field": field, "value": value, "page_info": dict(page_info)},
        ) from exc
    if isinstance(value, float) and value != parsed:
        raise TTBPaginationError(
            f"TikTok pagination field {field} must be an integer",
            code="PAGINATION_METADATA_INVALID",
            payload={"field": field, "value": value, "page_info": dict(page_info)},
        )
    if parsed < minimum:
        raise TTBPaginationError(
            f"TikTok pagination field {field} is below {minimum}",
            code="PAGINATION_METADATA_INVALID",
            payload={"field": field, "value": value, "page_info": dict(page_info)},
        )
    return parsed


def _pagination_bool(
    value: Any,
    *,
    field: str,
    page_info: Mapping[str, Any],
) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise TTBPaginationError(
        f"TikTok pagination field {field} must be boolean",
        code="PAGINATION_METADATA_INVALID",
        payload={"field": field, "value": value, "page_info": dict(page_info)},
    )


def _pagination_cursor(
    value: Any,
    *,
    page_info: Mapping[str, Any] | None = None,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise TTBPaginationError(
            "TikTok pagination cursor has an invalid type",
            code="PAGINATION_METADATA_INVALID",
            payload={
                "field": "cursor",
                "value": value,
                "page_info": dict(page_info or {}),
            },
        )
    normalized = str(value).strip()
    return normalized or None


def _pagination_page_info(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    page_info = data.get("page_info")
    if page_info is None:
        return None
    if not isinstance(page_info, Mapping):
        raise TTBPaginationError(
            "TikTok data.page_info must be an object",
            code="PAGINATION_METADATA_INVALID",
            payload={"page_info": page_info},
        )
    return page_info


def _pagination_continuation(
    page_info: Mapping[str, Any] | None,
    *,
    requested_page: int,
    items_seen: int,
) -> bool | None:
    if page_info is None:
        return None

    response_page = _pagination_int(
        page_info.get("page"),
        field="page",
        minimum=1,
        page_info=page_info,
    )
    if response_page is not None and response_page != requested_page:
        raise TTBPaginationError(
            "TikTok pagination returned an unexpected page "
            f"(requested {requested_page}, returned {response_page})",
            code="PAGINATION_PAGE_STALLED",
            payload={
                "requested_page": requested_page,
                "response_page": response_page,
                "page_info": dict(page_info),
            },
        )

    signals: dict[str, bool] = {}
    total_page = _pagination_int(
        page_info.get("total_page"),
        field="total_page",
        minimum=0,
        page_info=page_info,
    )
    if total_page is not None:
        if total_page == 0:
            if items_seen:
                raise TTBPaginationError(
                    "TikTok pagination returned rows with total_page=0",
                    code="PAGINATION_METADATA_CONFLICT",
                    payload={
                        "requested_page": requested_page,
                        "items_seen": items_seen,
                        "page_info": dict(page_info),
                    },
                )
            signals["total_page"] = False
        else:
            if requested_page > total_page:
                raise TTBPaginationError(
                    "TikTok pagination advanced beyond total_page",
                    code="PAGINATION_METADATA_CONFLICT",
                    payload={
                        "requested_page": requested_page,
                        "items_seen": items_seen,
                        "page_info": dict(page_info),
                    },
                )
            signals["total_page"] = requested_page < total_page

    total_number = _pagination_int(
        page_info.get("total_number"),
        field="total_number",
        minimum=0,
        page_info=page_info,
    )
    if total_number is not None:
        if items_seen > total_number:
            raise TTBPaginationError(
                "TikTok pagination returned more rows than total_number",
                code="PAGINATION_METADATA_CONFLICT",
                payload={
                    "requested_page": requested_page,
                    "items_seen": items_seen,
                    "page_info": dict(page_info),
                },
            )
        signals["total_number"] = items_seen < total_number

    for field in ("has_more", "has_next"):
        signal = _pagination_bool(
            page_info.get(field),
            field=field,
            page_info=page_info,
        )
        if signal is not None:
            signals[field] = signal

    if not signals:
        return None
    # TikTok occasionally returns a stale negative flag alongside an
    # authoritative positive total. A positive continuation signal must win:
    # stopping is safe only when every supplied signal says the page is final.
    return any(signals.values())


def _pagination_page_signature(items: Iterable[Any]) -> str:
    serialized = json.dumps(
        list(items),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _pagination_item_signature(
    item: Any,
    *,
    item_key: Callable[[Mapping[str, Any]], Any],
    requested_page: int,
    item_index: int,
) -> str:
    if not isinstance(item, Mapping):
        raise TTBPaginationError(
            "TikTok pagination returned a non-object item",
            code="PAGINATION_ITEM_KEY_INVALID",
            payload={
                "requested_page": requested_page,
                "item_index": item_index,
            },
        )
    try:
        value = item_key(item)
    except Exception as exc:  # noqa: BLE001 - normalize caller key extractors
        raise TTBPaginationError(
            "TikTok pagination could not extract a stable item key",
            code="PAGINATION_ITEM_KEY_INVALID",
            payload={
                "requested_page": requested_page,
                "item_index": item_index,
            },
        ) from exc
    if value is None or (isinstance(value, str) and not value.strip()):
        raise TTBPaginationError(
            "TikTok pagination returned an item without a stable key",
            code="PAGINATION_ITEM_KEY_INVALID",
            payload={
                "requested_page": requested_page,
                "item_index": item_index,
            },
        )
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _product_pagination_key(item: Mapping[str, Any]) -> str | None:
    for field in ("item_group_id", "product_id", "id"):
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _resource_pagination_key(
    item: Mapping[str, Any],
    fields: tuple[str, ...],
) -> str | None:
    for field in fields:
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _default_pagination_item_key(
    path: str,
) -> Callable[[Mapping[str, Any]], Any] | None:
    """Bind every persisted official resource to an endpoint-stable key."""

    normalized = f"/{str(path or '').strip('/')}/"
    fields_by_path: dict[str, tuple[str, ...]] = {
        "/bc/get/": ("bc_id", "business_center_id", "id"),
        "/oauth2/advertiser/get/": ("advertiser_id", "id"),
        "/store/list/": ("store_id", "id"),
        "/store/product/get/": ("item_group_id", "product_id", "id"),
    }
    fields = fields_by_path.get(normalized)
    if fields is None:
        return None
    return lambda item: _resource_pagination_key(item, fields)


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


class SharedTikTokRateLimiter:
    """Redis-backed quota gate shared by API and Celery worker processes."""

    def __init__(self, *, app_id: str | None, access_token: str) -> None:
        identity = str(app_id or access_token or "default")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        environment = str(getattr(settings, "LOCK_ENV", "prod") or "prod")
        self._prefix = f"gmv:ttb:quota:{environment}:{digest}"
        self._max_wait = max(
            0.25,
            float(getattr(settings, "TTB_API_RATE_LIMIT_MAX_WAIT_SECONDS", 8.0)),
        )

    @staticmethod
    def _limits(path: str) -> list[tuple[str, int, int]]:
        limits = [
            ("global:s", int(getattr(settings, "TTB_API_GLOBAL_QPS", 18)), 1_000),
            ("global:m", int(getattr(settings, "TTB_API_GLOBAL_QPM", 1000)), 60_000),
            ("global:d", int(getattr(settings, "TTB_API_GLOBAL_QPD", 1_600_000)), 86_400_000),
        ]
        normalized = "/" + str(path or "").strip("/") + "/"
        if normalized == "/gmv_max/report/get/":
            limits.extend(
                [
                    (
                        "gmv-report:s",
                        int(getattr(settings, "TTB_API_GMVMAX_REPORT_QPS", 6)),
                        1_000,
                    ),
                    (
                        "gmv-report:m",
                        int(getattr(settings, "TTB_API_GMVMAX_REPORT_QPM", 240)),
                        60_000,
                    ),
                    (
                        "gmv-report:d",
                        int(getattr(settings, "TTB_API_GMVMAX_REPORT_QPD", 28_000)),
                        86_400_000,
                    ),
                ]
            )
        if normalized in {"/store/list/", "/gmv_max/store/list/"}:
            limits.extend(
                [
                    ("store-list:s", int(getattr(settings, "TTB_API_STORE_LIST_QPS", 3)), 1_000),
                    ("store-list:m", int(getattr(settings, "TTB_API_STORE_LIST_QPM", 60)), 60_000),
                ]
            )
        return [(name, max(1, limit), period_ms) for name, limit, period_ms in limits]

    def _cooldown_key(self, path: str) -> str:
        normalized = "/" + str(path or "").strip("/") + "/"
        quota = "gmv-report" if normalized == "/gmv_max/report/get/" else "global"
        return f"{self._prefix}:cooldown:{quota}"

    async def penalize(
        self,
        path: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Publish an upstream quota cooldown shared by every process."""

        default_seconds = max(
            1.0,
            float(getattr(settings, "TTB_API_UPSTREAM_COOLDOWN_SECONDS", 30.0)),
        )
        maximum_seconds = max(
            default_seconds,
            float(getattr(settings, "TTB_API_UPSTREAM_COOLDOWN_MAX_SECONDS", 300.0)),
        )
        requested_seconds = (
            default_seconds
            if retry_after_seconds is None
            else max(default_seconds, float(retry_after_seconds))
        )
        requested_ms = int(min(requested_seconds, maximum_seconds) * 1000)
        try:
            redis_client = get_redis_sync()
            await asyncio.to_thread(
                redis_client.eval,
                _COOLDOWN_SCRIPT,
                1,
                self._cooldown_key(path),
                str(requested_ms),
            )
        except Exception as exc:
            # The per-process tenacity backoff remains active if Redis is down.
            logger.warning("TTB shared cooldown unavailable: %s", exc)

    async def acquire(self, path: str) -> None:
        deadline = time.monotonic() + self._max_wait
        limits = self._limits(path)
        while True:
            try:
                redis_client = get_redis_sync()
                cooldown_ms = int(
                    await asyncio.to_thread(
                        redis_client.pttl,
                        self._cooldown_key(path),
                    )
                    or -1
                )
            except Exception as exc:  # Redis outage must not disable ad control.
                logger.warning("TTB shared rate limiter unavailable; using local bucket: %s", exc)
                return
            if cooldown_ms > 0:
                remaining = deadline - time.monotonic()
                if cooldown_ms / 1000 > remaining:
                    raise TTBRateLimitBudgetError(
                        "TikTok upstream quota cooldown is active",
                        code="UPSTREAM_RATE_LIMIT",
                        payload={
                            "path": path,
                            "quota": "upstream-cooldown",
                            "retry_after_ms": cooldown_ms,
                        },
                        status=429,
                    )
                await asyncio.sleep(min(cooldown_ms / 1000, remaining))
                continue

            now_ms = int(time.time() * 1000)
            keys = [
                f"{self._prefix}:{name}:{now_ms // period_ms}"
                for name, _limit, period_ms in limits
            ]
            argv = [str(limit) for _name, limit, _period_ms in limits]
            argv.extend(str(period_ms + 2_000) for _name, _limit, period_ms in limits)
            try:
                result = await asyncio.to_thread(
                    redis_client.eval,
                    _RATE_LIMIT_SCRIPT,
                    len(keys),
                    *keys,
                    *argv,
                )
            except Exception as exc:  # Redis outage must not disable ad control.
                logger.warning("TTB shared rate limiter unavailable; using local bucket: %s", exc)
                return

            allowed = bool(result and int(result[0]) == 1)
            if allowed:
                return

            blocked_index = max(1, int(result[1] or 1)) - 1
            retry_ms = max(50, int(result[2] or 100))
            remaining = deadline - time.monotonic()
            if retry_ms / 1000 > remaining:
                quota_name = limits[min(blocked_index, len(limits) - 1)][0]
                raise TTBRateLimitBudgetError(
                    f"TikTok shared quota busy: {quota_name}",
                    code="LOCAL_RATE_LIMIT",
                    payload={"path": path, "quota": quota_name, "retry_after_ms": retry_ms},
                    status=429,
                )
            await asyncio.sleep(min(retry_ms / 1000, remaining))


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
    advertiser_balance_get: str

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
            advertiser_balance_get=g(
                "TTB_ADVERTISER_BALANCE_GET", "advertiser/balance/get/"
            ),
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
        self._shared_limiter = SharedTikTokRateLimiter(
            app_id=app_id,
            access_token=access_token,
        )
        self._timeout = timeout or float(
            getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15.0)
        )

        # Access-Token 头是必须的
        default_headers = {
            "Access-Token": access_token,
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
        multipart_body: Dict[str, Any] | None = None,
        multipart_files: Dict[str, tuple[str, Any, str]] | None = None,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> Dict[str, Any]:
        return await self._request_json_once(
            method,
            path,
            params=params,
            json_body=json_body,
            multipart_body=multipart_body,
            multipart_files=multipart_files,
            request_timeout=request_timeout,
        )

    async def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        json_body: Dict[str, Any] | None = None,
        multipart_body: Dict[str, Any] | None = None,
        multipart_files: Dict[str, tuple[str, Any, str]] | None = None,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> Dict[str, Any]:
        """Perform exactly one HTTP request.

        Non-idempotent create endpoints use this primitive so a transport
        timeout or a retryable TikTok error can never cause a blind second
        POST after the remote side may already have committed the mutation.
        """

        await self._bucket.acquire()
        await self._shared_limiter.acquire(path)

        params = dict(params or {})

        # GMV Max reports officially allow 1,000 rows, while most Business API
        # list endpoints cap pages at 50. A global 50-row clamp made the report
        # synchronizers believe they had requested 200 rows even though only 50
        # were sent, which could truncate responses lacking page_info.
        if "page_size" in params:
            page_limit = _page_size_limit(path)
            params["page_size"] = _clamp_page_size(
                params["page_size"],
                default=page_limit,
                maximum=page_limit,
            )

        # 对 /oauth2/advertiser/get/ 自动附加 app_id / secret（若提供）
        needs_app_credentials = path.rstrip("/") == self._paths.advertisers_get.rstrip("/")
        if needs_app_credentials and self._app_id and self._app_secret:
            params.setdefault("app_id", self._app_id)
            params.setdefault("secret", self._app_secret)

        url = build_url(path)
        request_kwargs: Dict[str, Any] = {"params": params}
        if request_timeout is not None:
            request_kwargs["timeout"] = request_timeout
        if multipart_body is not None or multipart_files is not None:
            for _, file_value in (multipart_files or {}).items():
                stream = file_value[1] if len(file_value) > 1 else None
                if hasattr(stream, "seek"):
                    stream.seek(0)
            files = {
                str(key): (None, str(value).lower() if isinstance(value, bool) else str(value))
                for key, value in (multipart_body or {}).items()
                if value is not None
            }
            files.update(multipart_files or {})
            request_kwargs["files"] = files
        elif json_body is not None:
            request_kwargs["json"] = json_body
        resp = await self._client.request(method, url, **request_kwargs)

        status = resp.status_code
        text = resp.text

        if status in (429, 500, 502, 503, 504):
            # 这些是可重试错误，tenacity 会自动重试
            if status == 429:
                retry_after_seconds: float | None = None
                try:
                    retry_after_seconds = float(resp.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    retry_after_seconds = None
                await self._shared_limiter.penalize(
                    path,
                    retry_after_seconds=retry_after_seconds,
                )
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
            message = data.get("message") or "api error"
            normalized_message = str(message).lower()
            if (
                str(code) == "40100"
                or (str(code) == "40000" and any(token in normalized_message for token in ("frequent", "too many", "rate limit")))
                or (str(code) == "40700" and any(token in normalized_message for token in ("timeout", "redis error", "server error", "internal")))
            ):
                retry_status = 429 if str(code) in {"40000", "40100"} else 503
                if retry_status == 429:
                    await self._shared_limiter.penalize(path)
                raise TTBHttpError(retry_status, message, payload=data)
            if str(code) == "40002":
                raise TTBBusinessError(
                    message,
                    code=code,
                    payload=data,
                    status=status,
                )
            raise TTBApiError(
                message,
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
        item_key: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> AsyncIterator[dict]:
        params = dict(base_params or {})
        effective_item_key = item_key or _default_pagination_item_key(path)
        page_limit = _page_size_limit(path)
        size = _clamp_page_size(
            page_size,
            default=page_limit,
            maximum=page_limit,
        )
        params["page_size"] = size
        cursor = _pagination_cursor(params.pop("cursor", None))
        seen_cursors = {cursor} if cursor is not None else set()
        seen_signatures: set[str] = set()
        seen_item_keys: set[str] = set()
        items_seen = 0
        expected_total_number: int | None = None

        for requested_page in range(1, _MAX_PAGINATION_PAGES + 1):
            if cursor is None:
                params.pop("cursor", None)
            else:
                params["cursor"] = cursor
            payload = await self._request_json(method, path, params=params)
            items_iterable, _ = self._extract_list_page(payload)
            items = list(items_iterable)
            page_info = _pagination_page_info(payload)
            if items:
                signature = _pagination_page_signature(items)
                if signature in seen_signatures:
                    raise TTBPaginationError(
                        "TikTok cursor pagination repeated a page",
                        code="PAGINATION_CURSOR_STALLED",
                        payload={
                            "requested_page": requested_page,
                            "cursor": cursor,
                            "page_info": dict(page_info or {}),
                        },
                    )
                seen_signatures.add(signature)
            if effective_item_key is None:
                items_seen += len(items)
            else:
                page_item_keys: set[str] = set()
                for item_index, item in enumerate(items):
                    signature = _pagination_item_signature(
                        item,
                        item_key=effective_item_key,
                        requested_page=requested_page,
                        item_index=item_index,
                    )
                    if (
                        signature in page_item_keys
                        or signature in seen_item_keys
                    ):
                        raise TTBPaginationError(
                            "TikTok cursor pagination repeated a stable item key",
                            code="PAGINATION_ITEM_DUPLICATE",
                            payload={
                                "requested_page": requested_page,
                                "item_index": item_index,
                            },
                        )
                    page_item_keys.add(signature)
                seen_item_keys.update(page_item_keys)
                items_seen = len(seen_item_keys)

            declared_total_number = _pagination_int(
                page_info.get("total_number") if page_info is not None else None,
                field="total_number",
                minimum=0,
                page_info=page_info or {},
            )
            if declared_total_number is not None:
                if expected_total_number is None:
                    expected_total_number = declared_total_number
                elif declared_total_number != expected_total_number:
                    raise TTBPaginationError(
                        "TikTok pagination changed total_number across pages",
                        code="PAGINATION_METADATA_CONFLICT",
                        payload={
                            "requested_page": requested_page,
                            "expected_total_number": expected_total_number,
                            "total_number": declared_total_number,
                        },
                    )
            explicit_continuation = _pagination_continuation(
                page_info,
                requested_page=requested_page,
                items_seen=items_seen,
            )
            next_cursor = _pagination_cursor(
                page_info.get("cursor") if page_info is not None else None,
                page_info=page_info,
            )

            if explicit_continuation is True and not items:
                raise TTBPaginationError(
                    "TikTok cursor pagination returned an empty non-terminal page",
                    code="PAGINATION_METADATA_CONFLICT",
                    payload={
                        "requested_page": requested_page,
                        "items_seen": items_seen,
                        "page_info": dict(page_info or {}),
                    },
                )

            if explicit_continuation is None:
                if next_cursor is not None:
                    has_more = True
                elif len(items) == size:
                    raise TTBPaginationError(
                        "TikTok cursor pagination returned a full page without "
                        "a continuation cursor",
                        code="PAGINATION_CURSOR_MISSING",
                        payload={
                            "requested_page": requested_page,
                            "items_seen": items_seen,
                            "page_size": size,
                            "page_info": dict(page_info or {}),
                        },
                    )
                else:
                    has_more = False
            else:
                has_more = explicit_continuation

            if expected_total_number is not None:
                if items_seen > expected_total_number:
                    raise TTBPaginationError(
                        "TikTok pagination returned more unique items than total_number",
                        code="PAGINATION_METADATA_CONFLICT",
                        payload={
                            "requested_page": requested_page,
                            "items_seen": items_seen,
                            "total_number": expected_total_number,
                        },
                    )
                if items_seen < expected_total_number:
                    if not items:
                        raise TTBPaginationError(
                            "TikTok pagination terminated before every unique item "
                            "was returned",
                            code="PAGINATION_METADATA_CONFLICT",
                            payload={
                                "requested_page": requested_page,
                                "items_seen": items_seen,
                                "total_number": expected_total_number,
                            },
                        )
                    has_more = True

            if has_more:
                if next_cursor is None:
                    raise TTBPaginationError(
                        "TikTok cursor pagination requires another page but "
                        "did not return a cursor",
                        code="PAGINATION_CURSOR_MISSING",
                        payload={
                            "requested_page": requested_page,
                            "items_seen": items_seen,
                            "page_info": dict(page_info or {}),
                        },
                    )
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise TTBPaginationError(
                        "TikTok cursor pagination did not advance",
                        code="PAGINATION_CURSOR_STALLED",
                        payload={
                            "requested_page": requested_page,
                            "cursor": cursor,
                            "next_cursor": next_cursor,
                            "page_info": dict(page_info or {}),
                        },
                    )

            for it in items:
                if isinstance(it, dict):
                    yield it
            if not has_more:
                if (
                    expected_total_number is not None
                    and items_seen != expected_total_number
                ):
                    raise TTBPaginationError(
                        "TikTok pagination unique item count does not match "
                        "total_number",
                        code="PAGINATION_METADATA_CONFLICT",
                        payload={
                            "requested_page": requested_page,
                            "items_seen": items_seen,
                            "total_number": expected_total_number,
                        },
                    )
                return

            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise TTBPaginationError(
            "TikTok cursor pagination exceeded the safety page limit",
            code="PAGINATION_LIMIT_EXCEEDED",
            payload={"max_pages": _MAX_PAGINATION_PAGES, "items_seen": items_seen},
        )

    async def _paged_by_page(
        self,
        *,
        method: str,
        path: str,
        base_params: Dict[str, Any] | None = None,
        page_param: str = "page",
        page_size: int = _MAX_PAGE_SIZE,
        extractor: Literal["stores", "products", "list"],
        item_key: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> AsyncIterator[dict]:
        page = 1
        effective_item_key = item_key or _default_pagination_item_key(path)
        page_limit = _page_size_limit(path)
        size = _clamp_page_size(
            page_size,
            default=page_limit,
            maximum=page_limit,
        )
        items_seen = 0
        seen_signatures: set[str] = set()
        seen_item_keys: set[str] = set()
        expected_total_number: int | None = None

        while page <= _MAX_PAGINATION_PAGES:
            params = dict(base_params or {})
            params[page_param] = page
            params["page_size"] = size

            payload = await self._request_json(method, path, params=params)
            if extractor == "stores":
                items, _ = self._extract_stores(payload)
            elif extractor == "products":
                items, _ = self._extract_products(payload)
            elif extractor == "list":
                items, _ = self._extract_list_page(payload)
            else:
                raise RuntimeError("unknown extractor")

            items = list(items)
            count = len(items)
            page_info = _pagination_page_info(payload)
            if items:
                signature = _pagination_page_signature(items)
                if signature in seen_signatures:
                    raise TTBPaginationError(
                        "TikTok numbered pagination repeated a page",
                        code="PAGINATION_PAGE_STALLED",
                        payload={
                            "requested_page": page,
                            "items_seen": items_seen,
                            "page_info": dict(page_info or {}),
                        },
                    )
                seen_signatures.add(signature)
            if effective_item_key is None:
                items_seen += count
            else:
                page_item_keys: set[str] = set()
                for item_index, item in enumerate(items):
                    signature = _pagination_item_signature(
                        item,
                        item_key=effective_item_key,
                        requested_page=page,
                        item_index=item_index,
                    )
                    if (
                        signature in page_item_keys
                        or signature in seen_item_keys
                    ):
                        raise TTBPaginationError(
                            "TikTok numbered pagination repeated a stable item key",
                            code="PAGINATION_ITEM_DUPLICATE",
                            payload={
                                "requested_page": page,
                                "item_index": item_index,
                            },
                        )
                    page_item_keys.add(signature)
                seen_item_keys.update(page_item_keys)
                items_seen = len(seen_item_keys)

            declared_total_number = _pagination_int(
                page_info.get("total_number") if page_info is not None else None,
                field="total_number",
                minimum=0,
                page_info=page_info or {},
            )
            if declared_total_number is not None:
                if expected_total_number is None:
                    expected_total_number = declared_total_number
                elif declared_total_number != expected_total_number:
                    raise TTBPaginationError(
                        "TikTok pagination changed total_number across pages",
                        code="PAGINATION_METADATA_CONFLICT",
                        payload={
                            "requested_page": page,
                            "expected_total_number": expected_total_number,
                            "total_number": declared_total_number,
                        },
                    )
            explicit_continuation = _pagination_continuation(
                page_info,
                requested_page=page,
                items_seen=items_seen,
            )

            if explicit_continuation is True and not items:
                raise TTBPaginationError(
                    "TikTok numbered pagination returned an empty non-terminal page",
                    code="PAGINATION_METADATA_CONFLICT",
                    payload={
                        "requested_page": page,
                        "items_seen": items_seen,
                        "page_info": dict(page_info or {}),
                    },
                )

            for it in items:
                if isinstance(it, dict):
                    yield it

            if explicit_continuation is None:
                # A short page is not a reliable terminal signal: TikTok can
                # return fewer rows than requested while more pages remain.
                # Metadata-free numbered endpoints therefore probe until an
                # empty page. If the server ignores ``page``, the repeated-page
                # signature check above fails closed instead of truncating.
                has_more = bool(items)
            else:
                has_more = explicit_continuation
            if expected_total_number is not None:
                if items_seen > expected_total_number:
                    raise TTBPaginationError(
                        "TikTok pagination returned more unique items than total_number",
                        code="PAGINATION_METADATA_CONFLICT",
                        payload={
                            "requested_page": page,
                            "items_seen": items_seen,
                            "total_number": expected_total_number,
                        },
                    )
                if items_seen < expected_total_number:
                    if not items:
                        raise TTBPaginationError(
                            "TikTok pagination terminated before every unique item "
                            "was returned",
                            code="PAGINATION_METADATA_CONFLICT",
                            payload={
                                "requested_page": page,
                                "items_seen": items_seen,
                                "total_number": expected_total_number,
                            },
                        )
                    has_more = True
            if not has_more:
                if (
                    expected_total_number is not None
                    and items_seen != expected_total_number
                ):
                    raise TTBPaginationError(
                        "TikTok pagination unique item count does not match "
                        "total_number",
                        code="PAGINATION_METADATA_CONFLICT",
                        payload={
                            "requested_page": page,
                            "items_seen": items_seen,
                            "total_number": expected_total_number,
                        },
                    )
                return
            page += 1

        raise TTBPaginationError(
            "TikTok numbered pagination exceeded the safety page limit",
            code="PAGINATION_LIMIT_EXCEEDED",
            payload={"max_pages": _MAX_PAGINATION_PAGES, "items_seen": items_seen},
        )

    # ---------- 公共读取器 ----------

    async def iter_business_centers(
        self,
        *,
        page_size: int = _MAX_PAGE_SIZE,
    ) -> AsyncIterator[dict]:
        async for it in self._paged_by_page(
            method="GET",
            path=self._paths.bc_get,
            base_params={},
            page_param="page",
            page_size=page_size,
            extractor="list",
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

    async def fetch_advertiser_balances(
        self,
        *,
        bc_id: str | None,
        advertiser_ids: Iterable[str] | None = None,
        page_size: int = _MAX_PAGE_SIZE,
        fields: Iterable[str] | None = None,
    ) -> list[dict]:
        """
        Fetch advertiser balance and budget via /advertiser/balance/get/.

        TikTok returns budget_remaining only when it is explicitly requested in
        fields, so keep this method on the official balance endpoint instead of
        advertiser/info.
        """

        if not bc_id:
            return []

        ids: list[str] = []
        if advertiser_ids:
            for adv in advertiser_ids:
                if adv is None:
                    continue
                s = str(adv).strip()
                if s:
                    ids.append(s)
        if not ids:
            return []

        default_fields = [
            "budget_remaining",
            "budget_amount_restriction",
            "balance_info",
        ]
        requested_fields: list[str] = []
        seen: set[str] = set()
        for field in fields or default_fields:
            if not field:
                continue
            key = str(field).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            requested_fields.append(key)

        params: Dict[str, Any] = {
            "bc_id": str(bc_id),
            "page": 1,
            "page_size": max(1, min(int(page_size or _MAX_PAGE_SIZE), _MAX_PAGE_SIZE)),
        }
        if len(ids) == 1:
            params["keyword"] = ids[0]
        if requested_fields:
            params["fields"] = json.dumps(requested_fields, ensure_ascii=False)

        response = await self._request_json(
            "GET",
            self._paths.advertiser_balance_get,
            params=params,
        )
        data = response.get("data") or {}
        candidates = []
        for key in ("advertiser_account_list", "list", "advertiser_list", "advertiser_infos", "advertisers"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if not isinstance(candidates, list):
            return []
        id_set = set(ids)
        matched = [
            item
            for item in candidates
            if isinstance(item, dict) and (not id_set or str(item.get("advertiser_id") or "") in id_set)
        ]
        if matched:
            return matched
        if len(ids) == 1:
            fallback = [item for item in candidates if isinstance(item, dict)]
            for item in fallback:
                item.setdefault("_requested_advertiser_id", ids[0])
                item.setdefault("_balance_advertiser_id", str(item.get("advertiser_id") or ""))
            return fallback[:1]
        return []

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
        eligibility: Optional[Literal["GMV_MAX", "CUSTOM_SHOP_ADS"]] = None,
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

        _encode_query_arrays(params, "item_group_ids")

        async for it in self._paged_by_page(
            method="GET",
            path=self._paths.products_list,
            base_params=params,
            page_param="page",
            page_size=page_size,
            extractor="products",
            item_key=_product_pagination_key,
        ):
            yield it

    async def get_store_products_for_gmvmax_item_group_ids(
        self,
        *,
        bc_id: str,
        store_id: str,
        advertiser_id: str,
        item_group_ids: list[str],
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Batch-fetch GMV Max eligible products for the given item_group_ids.

        TikTok's /store/product/get/ accepts up to 10 ``item_group_ids`` per
        request. This helper splits the list into batches, applies the
        ``ad_creation_eligible=GMV_MAX`` filter, and aggregates all returned
        ``store_products`` entries.
        """

        cleaned_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in item_group_ids
                if item is not None and str(item).strip()
            )
        )
        if not cleaned_ids:
            return []

        batches = [
            cleaned_ids[i : i + 10]
            for i in range(0, len(cleaned_ids), 10)
        ]

        results: list[dict[str, Any]] = []
        seen_product_ids: set[str] = set()
        for batch in batches:
            batch_ids = set(batch)
            params: Dict[str, Any] = {
                "store_id": str(store_id),
                "advertiser_id": str(advertiser_id),
                "filtering": json.dumps(
                    {
                        "ad_creation_eligible": "GMV_MAX",
                        "item_group_ids": batch,
                    },
                    ensure_ascii=False,
                ),
            }
            if bc_id:
                params["bc_id"] = str(bc_id)

            async for item in self._paged_by_page(
                method="GET",
                path=self._paths.products_list,
                base_params=params,
                page_param="page",
                page_size=page_size,
                extractor="products",
                item_key=_product_pagination_key,
            ):
                product_id = _product_pagination_key(item)
                item_group_id = str(item.get("item_group_id") or "").strip()
                if (
                    product_id is None
                    or not item_group_id
                    or item_group_id not in batch_ids
                ):
                    raise TTBPaginationError(
                        "TikTok product response escaped its item_group_id filter",
                        code="PAGINATION_ITEM_KEY_INVALID",
                        payload={
                            "product_id": product_id,
                            "item_group_ids": list(batch),
                        },
                    )
                if item_group_id in seen_product_ids:
                    raise TTBPaginationError(
                        "TikTok product response repeated an item across filter chunks",
                        code="PAGINATION_ITEM_DUPLICATE",
                        payload={"item_group_id": item_group_id},
                    )
                seen_product_ids.add(item_group_id)
                results.append(item)

        return results

__all__ = [
    "TTBApiClient",
    "TTBApiError",
    "TTBBusinessError",
    "TTBHttpError",
    "TTBPaginationError",
    "TTBRateLimitBudgetError",
    "TTBPaths",
]
