from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount, OAuthTikTokShopShop
from app.services.oauth_tiktok_shop import (
    _account_credentials,
    _decrypt_app_secret,
    refresh_account_token,
    sign_api_request,
)


logger = logging.getLogger("gmv.tiktok_shop.api")

_TRANSIENT_PROVIDER_CODES = {
    36009002,  # TikTok downstream service/rate-limit failure.
    36009003,
    36009004,
}


def _compact_json(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _query_value(value: Any) -> Any:
    """Return the exact scalar that is both signed and sent on the wire.

    TikTok documents query booleans as lower-case ``true``/``false``.  Python's
    string form is title-cased while httpx serializes booleans lower-case, so
    signing the original bool produces a signature that does not match the
    actual request.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _provider_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return None


def _request_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = str(payload.get("request_id") or payload.get("requestId") or "").strip()
    return value[:128] or None


def _provider_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "TikTok Shop returned an invalid response."
    value = str(payload.get("message") or payload.get("msg") or "TikTok Shop request failed.")
    return value.replace("\r", " ").replace("\n", " ")[:512]


@dataclass(slots=True)
class TikTokShopRequestResult:
    data: Any
    request_id: str | None
    provider_code: int


class TikTokShopAPIClient:
    """Signed TikTok Shop Open API client scoped to one authorized shop."""

    def __init__(
        self,
        *,
        db: Session,
        workspace_id: int,
        account: OAuthTikTokShopAccount,
        shop: OAuthTikTokShopShop,
        app_key: str,
        app_secret: str,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self.workspace_id = int(workspace_id)
        self.account = account
        self.shop = shop
        self.app_key = str(app_key)
        self.app_secret = str(app_secret)
        self.access_token = str(access_token)
        self.base_url = str(settings.TT_SHOP_API_BASE).rstrip("/")
        self._http_client = http_client
        self._owns_http_client = http_client is None

    @classmethod
    async def create(
        cls,
        db: Session,
        *,
        workspace_id: int,
        account_id: int,
        shop_row_id: int,
        http_client: httpx.AsyncClient | None = None,
    ) -> "TikTokShopAPIClient":
        account = await refresh_account_token(
            db,
            workspace_id=int(workspace_id),
            account_id=int(account_id),
            force=False,
        )
        account, app, access_token, _ = _account_credentials(
            db,
            workspace_id=int(workspace_id),
            account_id=int(account.id),
        )
        shop = db.get(OAuthTikTokShopShop, int(shop_row_id))
        if (
            not shop
            or int(shop.workspace_id) != int(workspace_id)
            or int(shop.account_id) != int(account.id)
            or not bool(shop.is_active)
        ):
            raise APIError("TIKTOK_SHOP_NOT_FOUND", "Active TikTok Shop not found.", 404)
        return cls(
            db=db,
            workspace_id=int(workspace_id),
            account=account,
            shop=shop,
            app_key=str(app.client_id),
            app_secret=_decrypt_app_secret(app),
            access_token=access_token,
            http_client=http_client,
        )

    async def __aenter__(self) -> "TikTokShopAPIClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        self._http_client = None

    def require_scope(self, scope: str) -> None:
        granted = {str(value) for value in (self.account.granted_scopes_json or [])}
        if scope not in granted:
            raise APIError(
                "TIKTOK_SHOP_SCOPE_REQUIRED",
                f"TikTok Shop authorization is missing required scope: {scope}.",
                409,
                data={"required_scope": scope},
            )

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            timeout = httpx.Timeout(
                float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 25.0)),
                connect=10.0,
            )
            self._http_client = httpx.AsyncClient(timeout=timeout, http2=True)
        return self._http_client

    async def _force_refresh(self) -> None:
        account = await refresh_account_token(
            self.db,
            workspace_id=self.workspace_id,
            account_id=int(self.account.id),
            force=True,
        )
        account, _app, access_token, _ = _account_credentials(
            self.db,
            workspace_id=self.workspace_id,
            account_id=int(account.id),
        )
        self.account = account
        self.access_token = access_token

    async def request(
        self,
        method: str,
        path: str,
        *,
        scope: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        include_shop_cipher: bool = True,
        max_attempts: int = 4,
    ) -> TikTokShopRequestResult:
        self.require_scope(scope)
        method = str(method).upper()
        body_text = _compact_json(body)
        refreshed_after_unauthorized = False
        max_attempts = max(1, min(int(max_attempts), 5))

        for attempt in range(1, max_attempts + 1):
            params: dict[str, Any] = {
                "app_key": self.app_key,
                "timestamp": int(time.time()),
            }
            if include_shop_cipher:
                params["shop_cipher"] = self.shop.shop_cipher
            params.update(
                {
                    str(key): _query_value(value)
                    for key, value in dict(query or {}).items()
                    if value is not None
                }
            )
            params["sign"] = sign_api_request(
                path=path,
                params=params,
                app_secret=self.app_secret,
                body=body_text,
            )
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "x-tts-access-token": self.access_token,
            }
            try:
                response = await (await self._client()).request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    content=body_text if body is not None else None,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                if attempt >= max_attempts:
                    raise APIError(
                        "TIKTOK_SHOP_UNAVAILABLE",
                        "TikTok Shop API is temporarily unavailable.",
                        502,
                        data={"transport_error": type(exc).__name__},
                    ) from exc
                await asyncio.sleep(self._retry_delay(attempt, None))
                continue

            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            code = _provider_code(payload)
            request_id = _request_id(payload)

            if response.status_code == 401 and not refreshed_after_unauthorized:
                refreshed_after_unauthorized = True
                await self._force_refresh()
                continue

            transient = (
                response.status_code == 429
                or response.status_code >= 500
                or code in _TRANSIENT_PROVIDER_CODES
            )
            if transient and attempt < max_attempts:
                retry_after = response.headers.get("retry-after")
                await asyncio.sleep(self._retry_delay(attempt, retry_after))
                continue

            if response.status_code >= 400 or code != 0:
                safe_code = code if code is not None else response.status_code
                logger.warning(
                    "TikTok Shop API request failed method=%s path=%s http_status=%s "
                    "provider_code=%s request_id=%s attempt=%s",
                    method,
                    path,
                    response.status_code,
                    safe_code,
                    request_id,
                    attempt,
                )
                raise APIError(
                    "TIKTOK_SHOP_API_ERROR",
                    _provider_message(payload),
                    429 if response.status_code == 429 else 502,
                    data={
                        "http_status": response.status_code,
                        "provider_code": safe_code,
                        "request_id": request_id,
                        "retryable": transient,
                    },
                )
            if not isinstance(payload, dict):
                raise APIError(
                    "TIKTOK_SHOP_INVALID_RESPONSE",
                    "TikTok Shop returned invalid JSON.",
                    502,
                )
            return TikTokShopRequestResult(
                data=payload.get("data"),
                request_id=request_id,
                provider_code=0,
            )

        raise APIError("TIKTOK_SHOP_UNAVAILABLE", "TikTok Shop API is unavailable.", 502)

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        try:
            requested = float(retry_after or 0)
        except ValueError:
            requested = 0.0
        if requested > 0:
            return min(requested, 30.0)
        return min(0.75 * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25), 8.0)

    async def authorization_shops(self) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/authorization/202309/shops",
            scope="seller.authorization.info",
            include_shop_cipher=False,
        )

    async def active_shops(self) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/seller/202309/shops",
            scope="seller.shop.info",
            include_shop_cipher=False,
        )

    async def permissions(self) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/seller/202309/permissions",
            scope="seller.shop.info",
            include_shop_cipher=False,
        )

    async def search_products(
        self,
        *,
        page_size: int = 100,
        page_token: str | None = None,
        status: str = "ALL",
    ) -> TikTokShopRequestResult:
        return await self.request(
            "POST",
            "/product/202502/products/search",
            scope="seller.product.basic",
            query={"page_size": min(max(page_size, 1), 100), "page_token": page_token},
            body={"status": status, "category_version": "v2"},
        )

    async def get_product(self, product_id: str) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            f"/product/202309/products/{product_id}",
            scope="seller.product.basic",
            query={"locale": "en-US"},
        )

    async def categories(self) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/product/202309/categories",
            scope="seller.product.basic",
            query={"category_version": "v2", "locale": "en-US"},
        )

    async def search_orders(
        self,
        *,
        create_time_ge: int,
        create_time_lt: int,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "POST",
            "/order/202309/orders/search",
            scope="seller.order.info",
            query={
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
                "sort_field": "create_time",
                "sort_order": "DESC",
            },
            body={"create_time_ge": create_time_ge, "create_time_lt": create_time_lt},
        )

    async def get_orders(self, order_ids: list[str]) -> TikTokShopRequestResult:
        ids = ",".join(str(value) for value in order_ids if str(value).strip())
        if not ids:
            raise APIError("INVALID_ORDER_IDS", "At least one order ID is required.", 400)
        return await self.request(
            "GET",
            "/order/202507/orders",
            scope="seller.order.info",
            query={"ids": ids},
        )

    async def statements(
        self,
        *,
        statement_time_ge: int,
        statement_time_lt: int,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/finance/202309/statements",
            scope="seller.finance.info",
            query={
                "statement_time_ge": statement_time_ge,
                "statement_time_lt": statement_time_lt,
                "sort_field": "statement_time",
                "sort_order": "DESC",
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
            },
        )

    async def statement_transactions(
        self,
        statement_id: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            f"/finance/202501/statements/{statement_id}/statement_transactions",
            scope="seller.finance.info",
            query={
                "sort_field": "order_create_time",
                "sort_order": "DESC",
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
            },
        )

    async def order_transactions(self, order_id: str) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            f"/finance/202501/orders/{order_id}/statement_transactions",
            scope="seller.finance.info",
        )

    async def withdrawals(
        self,
        *,
        create_time_ge: int,
        create_time_lt: int,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/finance/202309/withdrawals",
            scope="seller.finance.info",
            query={
                "types": "WITHDRAW,SETTLE,TRANSFER,REVERSE",
                "create_time_ge": create_time_ge,
                "create_time_lt": create_time_lt,
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
            },
        )

    async def payments(
        self,
        *,
        create_time_ge: int,
        create_time_lt: int,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/finance/202605/payments",
            scope="seller.finance.info",
            query={
                "create_time_ge": create_time_ge,
                "create_time_lt": create_time_lt,
                "sort_field": "create_time",
                "sort_order": "DESC",
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
            },
        )

    async def unsettled_transactions(
        self,
        *,
        search_time_ge: int,
        search_time_lt: int,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/finance/202507/orders/unsettled",
            scope="seller.finance.info",
            query={
                "search_time_ge": search_time_ge,
                "search_time_lt": search_time_lt,
                "sort_field": "order_create_time",
                "sort_order": "DESC",
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
            },
        )

    async def promotion_activities(
        self,
        *,
        page_size: int = 100,
        page_token: str | None = None,
        status: str | None = None,
        activity_type: str | None = None,
        activity_title: str | None = None,
    ) -> TikTokShopRequestResult:
        body: dict[str, Any] = {
            "page_size": min(max(page_size, 1), 100),
            "page_token": page_token or "",
        }
        if status:
            body["status"] = str(status)
        if activity_type:
            body["activity_type"] = str(activity_type)
        if activity_title:
            body["activity_title"] = str(activity_title)
        return await self.request(
            "POST",
            "/promotion/202309/activities/search",
            scope="seller.promotion.info",
            body=body,
        )

    async def coupons(
        self,
        *,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "POST",
            "/promotion/202406/coupons/search",
            scope="seller.promotion.info",
            query={"page_size": min(max(page_size, 1), 100), "page_token": page_token},
            body={},
        )

    async def update_activity(
        self,
        activity_id: str,
        payload: Mapping[str, Any],
    ) -> TikTokShopRequestResult:
        self._require_promotion_writes()
        return await self.request(
            "PUT",
            f"/promotion/202309/activities/{activity_id}",
            scope="seller.promotion.write",
            body=dict(payload),
            max_attempts=1,
        )

    def _require_promotion_writes(self) -> None:
        if bool(getattr(settings, "TT_SHOP_PROMOTION_WRITES_ENABLED", False)):
            return
        raise APIError(
            "TIKTOK_SHOP_WRITES_DISABLED",
            "TikTok Shop promotion writes are disabled by platform policy.",
            409,
        )

    async def get_activity(self, activity_id: str) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            f"/promotion/202309/activities/{activity_id}",
            scope="seller.promotion.info",
        )

    async def create_activity(
        self,
        payload: Mapping[str, Any],
    ) -> TikTokShopRequestResult:
        self._require_promotion_writes()
        return await self.request(
            "POST",
            "/promotion/202309/activities",
            scope="seller.promotion.write",
            body=dict(payload),
            max_attempts=1,
        )

    async def update_activity_products(
        self,
        activity_id: str,
        payload: Mapping[str, Any],
    ) -> TikTokShopRequestResult:
        self._require_promotion_writes()
        body = dict(payload)
        body.setdefault("activity_id", str(activity_id))
        return await self.request(
            "PUT",
            f"/promotion/202309/activities/{activity_id}/products",
            scope="seller.promotion.write",
            body=body,
            max_attempts=1,
        )

    async def remove_activity_products(
        self,
        activity_id: str,
        payload: Mapping[str, Any],
    ) -> TikTokShopRequestResult:
        self._require_promotion_writes()
        body = dict(payload)
        body.setdefault("activity_id", str(activity_id))
        return await self.request(
            "DELETE",
            f"/promotion/202309/activities/{activity_id}/products",
            scope="seller.promotion.write",
            body=body,
            max_attempts=1,
        )

    async def deactivate_activity(self, activity_id: str) -> TikTokShopRequestResult:
        self._require_promotion_writes()
        return await self.request(
            "POST",
            f"/promotion/202309/activities/{activity_id}/deactivate",
            scope="seller.promotion.write",
            body={},
            max_attempts=1,
        )

    async def search_global_products(
        self,
        *,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "POST",
            "/product/202312/global_products/search",
            scope="seller.global_product.info",
            include_shop_cipher=False,
            query={"page_size": min(max(page_size, 1), 100), "page_token": page_token},
            body={},
        )

    async def get_global_product(self, product_id: str) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            f"/product/202309/global_products/{product_id}",
            scope="seller.global_product.info",
            include_shop_cipher=False,
        )

    async def shop_hourly_performance(self, report_date: str) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            f"/analytics/202510/shop/performance/{report_date}/performance_per_hour",
            scope="data.shop_analytics.public.read",
            query={"currency": "USD"},
        )

    def _analytics_range(self, start_date: str, end_date_exclusive: str) -> dict[str, Any]:
        return {
            "start_date_ge": start_date,
            "end_date_lt": end_date_exclusive,
            "currency": "USD",
        }

    async def video_overview(
        self,
        start_date: str,
        end_date_exclusive: str,
        *,
        today: bool = False,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/analytics/202509/shop_videos/overview_performance",
            scope="data.shop_analytics.public.read",
            query={
                **self._analytics_range(start_date, end_date_exclusive),
                "today": today or None,
                "granularity": "ALL" if today else "1D",
                "account_type": "ALL",
            },
            max_attempts=2 if today else 4,
        )

    async def shop_performance(
        self,
        start_date: str,
        end_date_exclusive: str,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/analytics/202509/shop/performance",
            scope="data.shop_analytics.public.read",
            query={
                **self._analytics_range(start_date, end_date_exclusive),
                "granularity": "ALL",
            },
        )

    async def video_performance(
        self,
        start_date: str,
        end_date_exclusive: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/analytics/202605/shop_videos/performance",
            scope="data.shop_analytics.public.read",
            query={
                **self._analytics_range(start_date, end_date_exclusive),
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
                "sort_field": "gmv",
                "sort_order": "DESC",
                "account_type": "ALL",
            },
        )

    async def product_performance(
        self,
        start_date: str,
        end_date_exclusive: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/analytics/202605/shop_products/performance",
            scope="data.shop_analytics.public.read",
            query={
                **self._analytics_range(start_date, end_date_exclusive),
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
                "sort_field": "gmv",
                "sort_order": "DESC",
                "product_status_filter": "ALL",
            },
        )

    async def sku_performance(
        self,
        start_date: str,
        end_date_exclusive: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/analytics/202509/shop_skus/performance",
            scope="data.shop_analytics.public.read",
            query={
                **self._analytics_range(start_date, end_date_exclusive),
                "page_size": min(max(page_size, 1), 100),
                "page_token": page_token,
                "sort_field": "gmv",
                "sort_order": "DESC",
                "product_status_filter": "ALL",
            },
        )

    async def live_overview(
        self,
        start_date: str,
        end_date_exclusive: str,
        *,
        today: bool = False,
    ) -> TikTokShopRequestResult:
        return await self.request(
            "GET",
            "/analytics/202509/shop_lives/overview_performance",
            scope="data.shop_analytics.public.read",
            query={
                **self._analytics_range(start_date, end_date_exclusive),
                "today": today or None,
                "granularity": "ALL" if today else "1D",
                "account_type": "ALL",
            },
        )
