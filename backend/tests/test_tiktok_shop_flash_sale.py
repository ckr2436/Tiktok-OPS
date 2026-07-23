from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopFlashSaleRun,
)
from app.services.tiktok_shop_api import TikTokShopAPIClient
from app.services import tiktok_shop_flash_sale as flash_sale_service
from app.services.tiktok_shop_flash_sale import (
    LiveActivity,
    _deactivate,
    activity_title,
    build_product_payload,
    coverage_end,
    reconcile_flash_sales,
)


def test_build_product_payload_uses_product_level_flash_sale_fields() -> None:
    payload = build_product_payload(
        {
            "product-b": Decimal("7.99"),
            "product-a": Decimal("6.90"),
        }
    )

    assert payload == {
        "products": [
            {
                "id": "product-a",
                "activity_price_amount": "6.90",
                "quantity_limit": -1,
                "quantity_per_user": -1,
                "skus": [],
            },
            {
                "id": "product-b",
                "activity_price_amount": "7.99",
                "quantity_limit": -1,
                "quantity_per_user": -1,
                "skus": [],
            },
        ]
    }


def test_coverage_end_requires_contiguous_intervals() -> None:
    now = datetime(2026, 7, 20, 12, 0)
    assert coverage_end(
        [
            (now - timedelta(hours=1), now + timedelta(hours=24)),
            (
                now + timedelta(hours=24, seconds=60),
                now + timedelta(hours=72),
            ),
        ],
        now=now,
        max_gap_seconds=65,
    ) == now + timedelta(hours=72)
    assert coverage_end(
        [
            (now - timedelta(hours=1), now + timedelta(hours=24)),
            (
                now + timedelta(hours=25),
                now + timedelta(hours=72),
            ),
        ],
        now=now,
        max_gap_seconds=65,
    ) == now + timedelta(hours=24)


def test_activity_title_is_unique_short_and_uses_shop_timezone() -> None:
    shop = SimpleNamespace(id=9, timezone_name="Etc/GMT+8")
    first = activity_title(shop, datetime(2026, 7, 20, 8, 0))
    second = activity_title(shop, datetime(2026, 7, 20, 8, 1))

    assert first.startswith("MYUPONA Flash 0720-0000-")
    assert first != second
    assert len(first) <= 50


@pytest.mark.asyncio
async def test_promotion_write_methods_follow_official_paths_and_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}")
        requests.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path.endswith("/activities"):
            data = {"activity_id": "activity-1", "status": "NOT_START"}
        else:
            data = {"activity_id": "activity-1"}
        return httpx.Response(
            200,
            json={"code": 0, "message": "Success", "request_id": "req-1", "data": data},
        )

    monkeypatch.setattr(settings, "TT_SHOP_PROMOTION_WRITES_ENABLED", True)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TikTokShopAPIClient(
        db=SimpleNamespace(),
        workspace_id=1,
        account=SimpleNamespace(
            id=1,
            granted_scopes_json=["seller.promotion.info", "seller.promotion.write"],
        ),
        shop=SimpleNamespace(shop_cipher="cipher"),
        app_key="app-key",
        app_secret="app-secret",
        access_token="token",
        http_client=http_client,
    )
    try:
        await client.create_activity(
            {
                "title": "test",
                "activity_type": "FLASHSALE",
                "product_level": "PRODUCT",
                "duration_type": "NORMAL",
                "begin_time": 1784534400,
                "end_time": 1784793540,
            }
        )
        await client.update_activity_products(
            "activity-1",
            build_product_payload({"product-1": Decimal("7.99")}),
        )
        await client.deactivate_activity("activity-1")
    finally:
        await http_client.aclose()

    assert requests[0][0:2] == ("POST", "/promotion/202309/activities")
    assert requests[1][0:2] == (
        "PUT",
        "/promotion/202309/activities/activity-1/products",
    )
    assert requests[1][2]["activity_id"] == "activity-1"
    assert requests[2][0:2] == (
        "POST",
        "/promotion/202309/activities/activity-1/deactivate",
    )


@pytest.mark.asyncio
async def test_deactivate_stops_before_next_provider_write_after_lock_loss() -> None:
    calls: list[str] = []
    ownership = iter((True, False))

    class Client:
        async def deactivate_activity(self, activity_id: str):
            calls.append(activity_id)
            return SimpleNamespace(request_id=f"req-{activity_id}")

    now = datetime(2026, 7, 20, 12, 0)
    activities = [
        LiveActivity(
            activity_id=activity_id,
            title=activity_id,
            status="ONGOING",
            begin_at=now,
            end_at=now + timedelta(hours=1),
            products={"product-1": Decimal("7.99")},
        )
        for activity_id in ("activity-1", "activity-2")
    ]

    with pytest.raises(APIError) as exc_info:
        await _deactivate(
            Client(),
            activities,
            verify_lock_ownership=lambda: next(ownership),
        )

    assert exc_info.value.code == "TIKTOK_SHOP_FLASH_SALE_LOCK_LOST"
    assert exc_info.value.data == {"retryable": True}
    assert calls == ["activity-1"]


@pytest.mark.asyncio
async def test_lock_loss_rolls_back_without_publishing_failure_state(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = OAuthTikTokShopShop(
        id=9,
        workspace_id=1,
        account_id=2,
        shop_id="shop-provider-9",
        shop_cipher="cipher",
        timezone_name="UTC",
        status="active",
        is_active=True,
    )
    policy = TikTokShopFlashSalePolicy(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        product_id="product-1",
        enabled=True,
        activity_price_amount=Decimal("7.99"),
        currency="USD",
        status="active",
        policy_revision=2,
        applied_revision=1,
    )
    db_session.add_all([shop, policy])
    db_session.commit()

    ownership = {"owned": True}
    provider_calls: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def create_activity(self, _payload):
            provider_calls.append("create")
            return SimpleNamespace(
                data={"activity_id": "activity-new"}, request_id="req-create"
            )

        async def update_activity_products(self, activity_id, _payload):
            provider_calls.append(f"update:{activity_id}")
            return SimpleNamespace(data={}, request_id="req-update")

        async def get_activity(self, activity_id):
            provider_calls.append(f"get:{activity_id}")
            ownership["owned"] = False
            return SimpleNamespace(
                data={
                    "activity": {
                        "activity_id": activity_id,
                        "products": [
                            {
                                "id": "product-1",
                                "activity_price_amount": "7.99",
                            }
                        ],
                    }
                },
                request_id="req-get",
            )

        async def deactivate_activity(self, activity_id):
            provider_calls.append(f"deactivate:{activity_id}")
            return SimpleNamespace(data={}, request_id="req-deactivate")

    async def create_client(*_args, **_kwargs):
        return Client()

    async def no_live_activities(_client):
        return []

    monkeypatch.setattr(flash_sale_service.TikTokShopAPIClient, "create", create_client)
    monkeypatch.setattr(flash_sale_service, "_live_flash_sales", no_live_activities)

    with pytest.raises(APIError) as exc_info:
        await reconcile_flash_sales(
            db_session,
            workspace_id=1,
            account_id=2,
            shop_row_id=9,
            force_replace=True,
            verify_lock_ownership=lambda: ownership["owned"],
        )

    assert exc_info.value.code == "TIKTOK_SHOP_FLASH_SALE_LOCK_LOST"
    assert provider_calls == ["create", "update:activity-new", "get:activity-new"]
    db_session.expire_all()
    persisted_policy = db_session.get(TikTokShopFlashSalePolicy, policy.id)
    assert persisted_policy.status == "active"
    assert persisted_policy.applied_revision == 1
    assert db_session.query(TikTokShopFlashSaleRun).count() == 0
