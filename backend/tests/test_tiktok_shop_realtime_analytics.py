from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.core.errors import APIError
from app.services import tiktok_shop_api as api_module
from app.services import tiktok_shop_sync as sync_module
from app.services.tiktok_shop_api import TikTokShopAPIClient, TikTokShopRequestResult


SCOPE = "data.shop_analytics.public.read"


def test_analytics_pagination_rejects_a_repeated_token():
    seen_tokens: set[str] = set()

    assert sync_module._next_analytics_token(
        {"next_page_token": "page-2"},
        seen_tokens,
        dataset="video",
        request_id="req-page-1",
    ) == "page-2"

    with pytest.raises(APIError) as exc_info:
        sync_module._next_analytics_token(
            {"next_page_token": "page-2"},
            seen_tokens,
            dataset="video",
            request_id="req-page-2",
        )

    assert exc_info.value.code == "TIKTOK_SHOP_PAGINATION_REPEATED"
    assert exc_info.value.data == {"request_id": "req-page-2", "retryable": True}


@pytest.mark.asyncio
async def test_request_signs_and_sends_the_same_lowercase_boolean(monkeypatch):
    signed_params = {}

    def fake_sign(*, path, params, app_secret, body):
        signed_params.update(params)
        return "signed"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["today"] == "true"
        return httpx.Response(200, json={"code": 0, "data": {}, "request_id": "req-bool"})

    monkeypatch.setattr(api_module, "sign_api_request", fake_sign)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TikTokShopAPIClient(
        db=object(),
        workspace_id=3,
        account=SimpleNamespace(id=1, granted_scopes_json=[SCOPE]),
        shop=SimpleNamespace(shop_cipher="shop-cipher"),
        app_key="app-key",
        app_secret="app-secret",
        access_token="access-token",
        http_client=http_client,
    )
    try:
        result = await client.request(
            "GET",
            "/analytics/202509/shop_videos/overview_performance",
            scope=SCOPE,
            query={"today": True},
            max_attempts=1,
        )
    finally:
        await http_client.aclose()

    assert result.request_id == "req-bool"
    assert signed_params["today"] == "true"


class _RealtimeClient:
    def __init__(self, *, overview_error: bool = True):
        self.workspace_id = 3
        self.shop = SimpleNamespace(
            id=1,
            workspace_id=3,
            account_id=1,
            timezone_name="America/New_York",
        )
        self.overview_error = overview_error
        self.video_today = None
        self.live_today = None

    async def video_overview(self, start, end, *, today=False):
        self.video_today = today
        if self.overview_error:
            raise APIError(
                "TIKTOK_SHOP_API_ERROR",
                "Internal error",
                502,
                data={
                    "provider_code": 36009003,
                    "request_id": "req-overview-failed",
                    "retryable": True,
                },
            )
        return TikTokShopRequestResult(
            data={
                "latest_available_date": start,
                "performance": {
                    "intervals": [
                        {
                            "start_date": start,
                            "end_date": end,
                            "gmv": {"amount": "8.00", "currency": "USD"},
                            "avg_customers": 1,
                            "product_impressions": 100,
                            "product_clicks": 10,
                            "sku_orders": 1,
                            "click_through_rate": "0.05",
                        }
                    ]
                },
            },
            request_id="req-overview",
            provider_code=0,
        )

    async def shop_hourly_performance(self, report):
        return TikTokShopRequestResult(
            data={"performance": {"intervals": []}},
            request_id="req-hourly",
            provider_code=0,
        )

    async def video_performance(self, start, end, *, page_token=None):
        return TikTokShopRequestResult(
            data={"videos": [], "next_page_token": ""},
            request_id="req-videos",
            provider_code=0,
        )

    async def product_performance(self, start, end, *, page_token=None):
        return TikTokShopRequestResult(
            data={
                "latest_available_date": "2026-07-20",
                "next_page_token": "",
                "products": [
                    {
                        "id": "product-1",
                        "total_performance": {
                            "gmv": {"amount": "12.00", "currency": "USD"},
                            "orders": 2,
                        },
                        "seller_video_performance": {
                            "attributed_gmv": {"amount": "7.00", "currency": "USD"},
                            "attributed_sku_orders": 1,
                            "estimated_customers": 1,
                            "product_impressions": 70,
                            "product_clicks": 7,
                        },
                        "affiliate_video_performance": {
                            "attributed_video_gmv": {"amount": "5.00", "currency": "USD"},
                            "attributed_sku_orders": 1,
                            "estimated_customers": 1,
                            "product_impressions": 50,
                            "product_clicks": 5,
                        },
                    }
                ],
            },
            request_id="req-products",
            provider_code=0,
        )

    async def shop_performance(self, start, end):
        return TikTokShopRequestResult(
            data={
                "performance": {
                    "intervals": [
                        {
                            "sales": {
                                "gmv": {
                                    "breakdowns": [
                                        {
                                            "type": "VIDEO",
                                            "gmv": {"amount": "11.57", "currency": "USD"},
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            },
            request_id="req-shop",
            provider_code=0,
        )

    async def sku_performance(self, start, end, *, page_token=None):
        return TikTokShopRequestResult(
            data={"skus": [], "next_page_token": ""},
            request_id="req-skus",
            provider_code=0,
        )

    async def live_overview(self, start, end, *, today=False):
        self.live_today = today
        return TikTokShopRequestResult(
            data={"performance": {"intervals": []}},
            request_id="req-live",
            provider_code=0,
        )


@pytest.mark.asyncio
async def test_today_overview_failure_uses_official_field_fallback(monkeypatch):
    captured = []
    monkeypatch.setattr(sync_module, "shop_today", lambda _shop: date(2026, 7, 21))
    monkeypatch.setattr(
        sync_module,
        "_upsert",
        lambda _db, model, filters, values: captured.append((model, filters, values)),
    )
    client = _RealtimeClient(overview_error=True)
    stats = sync_module.SyncStats()

    await sync_module._sync_analytics_date(
        object(), client, stats, date(2026, 7, 21)
    )

    overview = next(
        values
        for model, _filters, values in captured
        if model is sync_module.TikTokShopVideoOverviewDailyMetric
    )
    assert client.video_today is True
    assert client.live_today is True
    assert overview["gmv"] == Decimal("11.57")
    assert overview["sku_orders"] == 2
    assert overview["product_impressions"] == 120
    assert overview["product_clicks"] == 12
    assert overview["click_through_rate"] is None
    assert overview["raw_json"]["_gmv_ops_meta"]["source"] == (
        "shop_and_product_video_channels"
    )
    assert overview["raw_json"]["_gmv_ops_meta"]["fallback_error"][
        "provider_code"
    ] == 36009003


@pytest.mark.asyncio
async def test_historical_overview_failure_is_not_hidden(monkeypatch):
    monkeypatch.setattr(sync_module, "shop_today", lambda _shop: date(2026, 7, 21))
    client = _RealtimeClient(overview_error=True)

    with pytest.raises(APIError):
        await sync_module._sync_analytics_date(
            object(), client, sync_module.SyncStats(), date(2026, 7, 20)
        )
    assert client.video_today is False


class _DateSession:
    def __init__(self, finalized_count):
        self.finalized_count = finalized_count

    def scalar(self, _statement):
        return self.finalized_count

    def begin_nested(self):
        return nullcontext()

    def flush(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finalized_count", "expected"),
    [
        (0, [date(2026, 7, 20), date(2026, 7, 21)]),
        (1, [date(2026, 7, 21)]),
    ],
)
async def test_scheduled_analytics_only_finalizes_yesterday_once(
    monkeypatch, finalized_count, expected
):
    seen = []

    async def fake_sync(_db, _client, _stats, report_date):
        seen.append(report_date)

    monkeypatch.setattr(sync_module, "shop_today", lambda _shop: date(2026, 7, 21))
    monkeypatch.setattr(sync_module, "_sync_analytics_date", fake_sync)
    client = _RealtimeClient(overview_error=False)

    await sync_module.sync_analytics(
        _DateSession(finalized_count),
        client,
        sync_module.SyncStats(),
        start_date=None,
        end_date_exclusive=None,
    )

    assert seen == expected
