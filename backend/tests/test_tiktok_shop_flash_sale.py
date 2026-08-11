from __future__ import annotations

import json
import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request
from pydantic import ValidationError

from app.core.config import settings
from app.core.deps import SessionUser
from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopFlashSaleRun,
    TikTokShopFlashSaleSchedule,
    TikTokShopProduct,
)
from app.services.tiktok_shop_api import TikTokShopAPIClient
from app.services import tiktok_shop_flash_sale as flash_sale_service
from app.features.tenants.commerce.router import (
    FlashSalePlanRequest,
    FlashSalePolicyRequest,
    _flash_sale_configuration_token,
    _flash_sale_schedule_item,
    apply_flash_sale_plan,
    save_flash_sale_policy,
)
from app.services.tiktok_shop_flash_sale import (
    LiveActivity,
    _coverage_gap_seconds,
    _deactivate,
    _matches_configured_schedule,
    activity_windows,
    activity_title,
    build_product_payload,
    coverage_end,
    reconcile_flash_sales,
)


def test_three_hour_schedule_builds_24_adjacent_windows() -> None:
    begin_at = datetime(2026, 7, 23, 12, 3)
    windows = activity_windows(
        begin_at=begin_at,
        target_until=datetime(2026, 7, 26, 12, 0),
        duration_minutes=180,
        boundary_seconds=1,
    )

    assert len(windows) == 24
    assert windows[0] == (
        begin_at,
        datetime(2026, 7, 23, 15, 2, 59),
    )
    assert windows[-1][1] == datetime(2026, 7, 26, 12, 2, 59)
    assert all(
        next_begin == previous_end + timedelta(seconds=1)
        for (_, previous_end), (next_begin, _) in zip(windows, windows[1:])
    )
    assert all(
        end_at - begin_at == timedelta(hours=3) - timedelta(seconds=1)
        for begin_at, end_at in windows
    )


def test_partial_three_hour_schedule_is_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 23, 12, 0)
    monkeypatch.setattr(settings, "TT_SHOP_FLASH_SALE_START_DELAY_SECONDS", 180)
    monkeypatch.setattr(settings, "TT_SHOP_FLASH_SALE_MAX_GAP_SECONDS", 65)
    policies = [
        SimpleNamespace(
            product_id=product_id,
            activity_price_amount=price,
        )
        for product_id, price in (
            ("product-1", Decimal("7.99")),
            ("product-2", Decimal("12.99")),
        )
    ]
    partial_windows = activity_windows(
        begin_at=now + timedelta(minutes=3),
        target_until=now + timedelta(hours=12),
        duration_minutes=180,
        boundary_seconds=1,
    )
    partial = [
        LiveActivity(
            activity_id=f"activity-{index}",
            title=f"Activity {index}",
            status="NOT_START",
            begin_at=begin_at,
            end_at=end_at,
            products={
                "product-1": Decimal("7.99"),
                "product-2": Decimal("12.99"),
            },
        )
        for index, (begin_at, end_at) in enumerate(partial_windows, start=1)
    ]

    assert _matches_configured_schedule(
        partial,
        policies,
        duration_minutes=180,
        boundary_seconds=1,
    )
    covered_until = coverage_end(
        [(item.begin_at, item.end_at) for item in partial],
        now=now,
        max_gap_seconds=_coverage_gap_seconds(),
    )
    assert covered_until == partial[-1].end_at
    remaining = activity_windows(
        begin_at=covered_until + timedelta(seconds=1),
        target_until=now + timedelta(hours=72),
        duration_minutes=180,
        boundary_seconds=1,
    )
    assert len(partial) + len(remaining) == 24


def test_flash_sale_request_and_response_expose_shop_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = FlashSalePolicyRequest(
        shop_id=9,
        activity_price_amount=Decimal("7.99"),
        activity_duration_minutes=180,
    )
    monkeypatch.setattr(settings, "TT_SHOP_FLASH_SALE_TARGET_COVERAGE_SECONDS", 72 * 60 * 60)

    assert payload.activity_duration_minutes == 180
    assert _flash_sale_schedule_item(
        SimpleNamespace(activity_duration_minutes=180)
    ) == {
        "activity_duration_minutes": 180,
        "target_coverage_seconds": 72 * 60 * 60,
        "planned_activity_count": 24,
    }
    with pytest.raises(ValidationError):
        FlashSalePolicyRequest(
            shop_id=9,
            activity_price_amount=Decimal("7.99"),
            activity_duration_minutes=59,
        )


def test_saving_shop_duration_revisions_all_enabled_products(
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
    product = TikTokShopProduct(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        product_id="product-1",
        title="Product 1",
        currency="USD",
    )
    schedule = TikTokShopFlashSaleSchedule(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        activity_duration_minutes=72 * 60,
    )
    policies = [
        TikTokShopFlashSalePolicy(
            workspace_id=1,
            account_id=2,
            shop_row_id=9,
            product_id=product_id,
            enabled=True,
            activity_price_amount=price,
            currency="USD",
            status="active",
            policy_revision=1,
            applied_revision=1,
        )
        for product_id, price in (
            ("product-1", Decimal("7.99")),
            ("product-2", Decimal("12.99")),
        )
    ]
    db_session.add_all([shop, product, schedule, *policies])
    db_session.commit()
    queued: list[dict[str, object]] = []

    def send_task(_name, *, kwargs, queue):
        queued.append({"kwargs": kwargs, "queue": queue})
        return SimpleNamespace(id="task-1")

    commerce_router_module = importlib.import_module(
        "app.features.tenants.commerce.router"
    )
    monkeypatch.setattr(commerce_router_module.celery_app, "send_task", send_task)
    monkeypatch.setattr(
        commerce_router_module,
        "log_event",
        lambda *_args, **_kwargs: None,
    )
    me = SessionUser(
        id=5,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=1,
        role="owner",
        is_active=True,
    )

    response = save_flash_sale_policy(
        workspace_id=1,
        product_id="product-1",
        payload=FlashSalePolicyRequest(
            shop_id=9,
            activity_price_amount=Decimal("7.99"),
            activity_duration_minutes=180,
        ),
        request=Request(
            {
                "type": "http",
                "method": "PUT",
                "path": "/",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        ),
        me=me,
        db=db_session,
    )

    db_session.expire_all()
    assert response["policy"]["activity_duration_minutes"] == 180
    assert db_session.query(TikTokShopFlashSaleSchedule).one().activity_duration_minutes == 180
    assert {
        item.product_id: (item.policy_revision, item.applied_revision)
        for item in db_session.query(TikTokShopFlashSalePolicy).all()
    } == {
        "product-1": (2, 1),
        "product-2": (2, 1),
    }
    assert queued == [
        {
            "kwargs": {
                "workspace_id": 1,
                "account_id": 2,
                "shop_row_id": 9,
                "trigger": "user_policy_update",
                "force_replace": True,
            },
            "queue": "tiktok_shop",
        }
    ]


def test_batch_plan_applies_all_product_choices_and_queues_once(
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
    products = [
        TikTokShopProduct(
            workspace_id=1,
            account_id=2,
            shop_row_id=9,
            product_id=product_id,
            title=product_id,
            currency="USD",
        )
        for product_id in ("product-1", "product-2", "product-3")
    ]
    schedule = TikTokShopFlashSaleSchedule(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        activity_duration_minutes=180,
    )
    policies = [
        TikTokShopFlashSalePolicy(
            workspace_id=1,
            account_id=2,
            shop_row_id=9,
            product_id=product_id,
            enabled=True,
            activity_price_amount=price,
            currency="USD",
            status="active",
            policy_revision=1,
            applied_revision=1,
        )
        for product_id, price in (
            ("product-1", Decimal("7.99")),
            ("product-2", Decimal("9.99")),
        )
    ]
    db_session.add_all([shop, schedule, *products, *policies])
    db_session.commit()
    base_token = _flash_sale_configuration_token(schedule, policies)
    queued: list[dict[str, object]] = []

    def send_task(_name, *, kwargs, queue):
        queued.append({"kwargs": kwargs, "queue": queue})
        return SimpleNamespace(id="batch-task-1")

    commerce_router_module = importlib.import_module(
        "app.features.tenants.commerce.router"
    )
    monkeypatch.setattr(commerce_router_module.celery_app, "send_task", send_task)
    monkeypatch.setattr(
        commerce_router_module,
        "log_event",
        lambda *_args, **_kwargs: None,
    )
    me = SessionUser(
        id=5,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=1,
        role="owner",
        is_active=True,
    )

    response = apply_flash_sale_plan(
        workspace_id=1,
        payload=FlashSalePlanRequest(
            shop_id=9,
            activity_duration_minutes=240,
            base_configuration_token=base_token,
            products=[
                {
                    "product_id": "product-1",
                    "enabled": True,
                    "activity_price_amount": "8.99",
                },
                {"product_id": "product-2", "enabled": False},
                {
                    "product_id": "product-3",
                    "enabled": True,
                    "activity_price_amount": "12.99",
                },
            ],
        ),
        request=Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/flash-sales/apply",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        ),
        me=me,
        db=db_session,
    )

    db_session.expire_all()
    persisted = {
        row.product_id: row
        for row in db_session.query(TikTokShopFlashSalePolicy).all()
    }
    assert response["status"] == "queued"
    assert response["enabled_count"] == 2
    assert response["disabled_count"] == 1
    assert db_session.query(TikTokShopFlashSaleSchedule).one().activity_duration_minutes == 240
    assert persisted["product-1"].enabled is True
    assert persisted["product-1"].activity_price_amount == Decimal("8.990000")
    assert persisted["product-1"].policy_revision == 2
    assert persisted["product-2"].enabled is False
    assert persisted["product-2"].policy_revision == 2
    assert persisted["product-3"].enabled is True
    assert persisted["product-3"].policy_revision == 1
    assert queued == [
        {
            "kwargs": {
                "workspace_id": 1,
                "account_id": 2,
                "shop_row_id": 9,
                "trigger": "user_batch_apply",
                "force_replace": True,
            },
            "queue": "tiktok_shop",
        }
    ]


def test_batch_plan_rejects_stale_configuration_without_queueing(
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
    product = TikTokShopProduct(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        product_id="product-1",
        title="Product 1",
        currency="USD",
    )
    db_session.add_all([shop, product])
    db_session.commit()
    commerce_router_module = importlib.import_module(
        "app.features.tenants.commerce.router"
    )
    monkeypatch.setattr(
        commerce_router_module.celery_app,
        "send_task",
        lambda *_args, **_kwargs: pytest.fail("stale plan must not queue"),
    )
    me = SessionUser(
        id=5,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode=None,
        is_platform_admin=False,
        workspace_id=1,
        role="owner",
        is_active=True,
    )

    with pytest.raises(APIError) as exc_info:
        apply_flash_sale_plan(
            workspace_id=1,
            payload=FlashSalePlanRequest(
                shop_id=9,
                activity_duration_minutes=180,
                base_configuration_token="0" * 64,
                products=[
                    {
                        "product_id": "product-1",
                        "enabled": True,
                        "activity_price_amount": "7.99",
                    }
                ],
            ),
            request=Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/flash-sales/apply",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                }
            ),
            me=me,
            db=db_session,
        )

    assert exc_info.value.code == "FLASH_SALE_CONFIGURATION_CHANGED"

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


@pytest.mark.asyncio
async def test_reconcile_creates_configured_three_hour_schedule(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_now = datetime(2026, 7, 23, 12, 0)
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
    schedule = TikTokShopFlashSaleSchedule(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        activity_duration_minutes=180,
    )
    policies = [
        TikTokShopFlashSalePolicy(
            workspace_id=1,
            account_id=2,
            shop_row_id=9,
            product_id=product_id,
            enabled=True,
            activity_price_amount=price,
            currency="USD",
            status="active",
            policy_revision=2,
            applied_revision=1,
        )
        for product_id, price in (
            ("product-1", Decimal("7.99")),
            ("product-2", Decimal("12.99")),
        )
    ]
    db_session.add_all([shop, schedule, *policies])
    db_session.commit()

    created: list[dict[str, object]] = []
    products_by_activity: dict[str, list[dict[str, object]]] = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def create_activity(self, payload):
            activity_id = f"activity-{len(created) + 1}"
            created.append({"activity_id": activity_id, **dict(payload)})
            return SimpleNamespace(
                data={"activity_id": activity_id},
                request_id=f"req-create-{activity_id}",
            )

        async def update_activity_products(self, activity_id, payload):
            products_by_activity[activity_id] = list(payload["products"])
            return SimpleNamespace(data={}, request_id=f"req-update-{activity_id}")

        async def get_activity(self, activity_id):
            return SimpleNamespace(
                data={
                    "activity": {
                        "activity_id": activity_id,
                        "products": products_by_activity[activity_id],
                    }
                },
                request_id=f"req-get-{activity_id}",
            )

        async def deactivate_activity(self, _activity_id):
            raise AssertionError("No existing activity should be deactivated")

    async def create_client(*_args, **_kwargs):
        return Client()

    async def no_live_activities(_client):
        return []

    monkeypatch.setattr(flash_sale_service, "utc_now", lambda: fixed_now)
    monkeypatch.setattr(flash_sale_service.TikTokShopAPIClient, "create", create_client)
    monkeypatch.setattr(flash_sale_service, "_live_flash_sales", no_live_activities)

    result = await reconcile_flash_sales(
        db_session,
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        force_replace=True,
    )

    assert result["status"] == "succeeded"
    assert result["activity_count"] == 24
    assert result["activity_duration_minutes"] == 180
    assert len(created) == 24
    assert created[0]["begin_time"] == int(
        (fixed_now + timedelta(minutes=3)).replace(tzinfo=timezone.utc).timestamp()
    )
    assert all(
        int(current["begin_time"]) - int(previous["begin_time"]) == 3 * 60 * 60
        for previous, current in zip(created, created[1:])
    )
    assert all(
        int(item["end_time"]) - int(item["begin_time"]) == 3 * 60 * 60 - 1
        for item in created
    )
    assert all(
        {product["id"] for product in products_by_activity[item["activity_id"]]}
        == {"product-1", "product-2"}
        for item in created
    )
    db_session.expire_all()
    persisted = db_session.query(TikTokShopFlashSalePolicy).all()
    assert all(item.applied_revision == item.policy_revision == 2 for item in persisted)
    run = db_session.query(TikTokShopFlashSaleRun).one()
    assert run.new_activity_id == "activity-24"
    assert run.new_activity_ids_json == [f"activity-{index}" for index in range(1, 25)]


@pytest.mark.asyncio
async def test_reconcile_removes_disabled_product_from_replacement(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_now = datetime(2026, 7, 23, 12, 0)
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
    schedule = TikTokShopFlashSaleSchedule(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        activity_duration_minutes=180,
    )
    enabled = TikTokShopFlashSalePolicy(
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
    disabled = TikTokShopFlashSalePolicy(
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        product_id="product-2",
        enabled=False,
        activity_price_amount=Decimal("9.99"),
        currency="USD",
        status="paused",
        policy_revision=2,
        applied_revision=1,
    )
    db_session.add_all([shop, schedule, enabled, disabled])
    db_session.commit()
    old_activity = LiveActivity(
        activity_id="old-activity",
        title="Old",
        status="NOT_START",
        begin_at=fixed_now + timedelta(minutes=3),
        end_at=fixed_now + timedelta(hours=3, minutes=2, seconds=59),
        products={
            "product-1": Decimal("7.99"),
            "product-2": Decimal("9.99"),
        },
    )
    deactivated: list[str] = []
    submitted_products: list[dict[str, object]] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def deactivate_activity(self, activity_id):
            deactivated.append(activity_id)
            return SimpleNamespace(request_id=f"req-deactivate-{activity_id}")

        async def create_activity(self, _payload):
            return SimpleNamespace(
                data={"activity_id": "new-activity"},
                request_id="req-create",
            )

        async def update_activity_products(self, _activity_id, payload):
            submitted_products.extend(payload["products"])
            return SimpleNamespace(data={}, request_id="req-update")

        async def get_activity(self, _activity_id):
            return SimpleNamespace(
                data={"activity": {"products": list(submitted_products)}},
                request_id="req-get",
            )

    async def create_client(*_args, **_kwargs):
        return Client()

    async def live_activities(_client):
        return [old_activity]

    monkeypatch.setattr(settings, "TT_SHOP_FLASH_SALE_TARGET_COVERAGE_SECONDS", 3 * 60 * 60)
    monkeypatch.setattr(flash_sale_service, "utc_now", lambda: fixed_now)
    monkeypatch.setattr(flash_sale_service.TikTokShopAPIClient, "create", create_client)
    monkeypatch.setattr(flash_sale_service, "_live_flash_sales", live_activities)

    result = await reconcile_flash_sales(
        db_session,
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        trigger="user_batch_apply",
        force_replace=True,
    )

    assert result["status"] == "succeeded"
    assert deactivated == ["old-activity"]
    assert {item["id"] for item in submitted_products} == {"product-1"}
    db_session.expire_all()
    persisted = {
        row.product_id: row
        for row in db_session.query(TikTokShopFlashSalePolicy).all()
    }
    assert persisted["product-1"].applied_revision == 2
    assert persisted["product-2"].applied_revision == 2
    assert persisted["product-2"].current_activity_id is None


@pytest.mark.asyncio
async def test_reconcile_deactivates_schedule_when_all_products_disabled(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_now = datetime(2026, 7, 23, 12, 0)
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
        enabled=False,
        activity_price_amount=Decimal("7.99"),
        currency="USD",
        status="paused",
        policy_revision=2,
        applied_revision=1,
    )
    db_session.add_all([shop, policy])
    db_session.commit()
    activity = LiveActivity(
        activity_id="old-activity",
        title="Old",
        status="NOT_START",
        begin_at=fixed_now + timedelta(minutes=3),
        end_at=fixed_now + timedelta(hours=3),
        products={"product-1": Decimal("7.99")},
    )
    deactivated: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def deactivate_activity(self, activity_id):
            deactivated.append(activity_id)
            return SimpleNamespace(request_id="req-deactivate")

    async def create_client(*_args, **_kwargs):
        return Client()

    async def live_activities(_client):
        return [activity]

    monkeypatch.setattr(flash_sale_service, "utc_now", lambda: fixed_now)
    monkeypatch.setattr(flash_sale_service.TikTokShopAPIClient, "create", create_client)
    monkeypatch.setattr(flash_sale_service, "_live_flash_sales", live_activities)

    result = await reconcile_flash_sales(
        db_session,
        workspace_id=1,
        account_id=2,
        shop_row_id=9,
        trigger="user_batch_apply",
        force_replace=True,
    )

    assert result == {
        "status": "succeeded",
        "action": "disable",
        "products": 0,
        "deactivated_activities": 1,
    }
    assert deactivated == ["old-activity"]
    db_session.expire_all()
    persisted = db_session.query(TikTokShopFlashSalePolicy).one()
    assert persisted.applied_revision == persisted.policy_revision == 2
    assert persisted.status == "paused"
