from datetime import datetime, timedelta
from decimal import Decimal

from app.data.models.oauth_tiktok_shop import (
    OAuthTikTokShopAccount,
    OAuthTikTokShopShop,
)
from app.data.models.oauth_ttb import OAuthProviderApp
from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopOrder,
    TikTokShopOrderLine,
    TikTokShopProduct,
)
from app.services.gmvmax_product_price import load_authoritative_product_prices
from app.data.models.workspaces import Workspace


def _seed_shop(db_session) -> OAuthTikTokShopShop:
    db_session.add(Workspace(id=1, name="Tenant", company_code="PRICE"))
    db_session.add(
        OAuthProviderApp(
            id=1,
            provider="tiktok-shop",
            name="TikTok Shop",
            client_id="client",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.test/callback",
        )
    )
    db_session.flush()
    db_session.add(
        OAuthTikTokShopAccount(
            id=2,
            workspace_id=1,
            provider_app_id=1,
            open_id="seller",
            access_token_cipher=b"access",
            refresh_token_cipher=b"refresh",
            token_fingerprint=b"p" * 32,
        )
    )
    db_session.flush()
    shop = OAuthTikTokShopShop(
        id=3,
        workspace_id=1,
        account_id=2,
        shop_id="shop-1",
        shop_cipher="cipher",
        timezone_name="America/New_York",
        is_active=True,
    )
    db_session.add(shop)
    db_session.flush()
    return shop


def _seed_transaction(
    db_session,
    *,
    shop: OAuthTikTokShopShop,
    product_id: str,
    order_id: str,
    price: str,
    paid_at: datetime,
) -> None:
    db_session.add(
        TikTokShopOrder(
            workspace_id=1,
            account_id=2,
            shop_row_id=shop.id,
            order_id=order_id,
            status="DELIVERED",
            paid_at=paid_at,
        )
    )
    db_session.add(
        TikTokShopOrderLine(
            workspace_id=1,
            account_id=2,
            shop_row_id=shop.id,
            order_id=order_id,
            line_item_id=f"line-{order_id}",
            product_id=product_id,
            sale_price=Decimal(price),
            quantity=1,
        )
    )


def test_authoritative_price_compares_latest_transaction_with_confirmed_flash_sale(
    db_session,
):
    shop = _seed_shop(db_session)
    now = datetime(2026, 7, 25, 8, 0, 0)
    db_session.add(
        TikTokShopProduct(
            workspace_id=1,
            account_id=2,
            shop_row_id=shop.id,
            product_id="product-flash",
            min_sale_price=Decimal("33.99"),
            max_sale_price=Decimal("33.99"),
            synced_at=now + timedelta(minutes=10),
        )
    )
    _seed_transaction(
        db_session,
        shop=shop,
        product_id="product-flash",
        order_id="order-old",
        price="14.99",
        paid_at=now - timedelta(hours=4),
    )
    db_session.add(
        TikTokShopFlashSalePolicy(
            workspace_id=1,
            account_id=2,
            shop_row_id=shop.id,
            product_id="product-flash",
            activity_price_amount=Decimal("10.99"),
            currency="USD",
            enabled=True,
            status="active",
            policy_revision=2,
            applied_revision=2,
            current_activity_status="ONGOING",
            current_begin_at=now - timedelta(hours=1),
            current_end_at=now + timedelta(hours=2),
            last_checked_at=now - timedelta(minutes=1),
        )
    )
    db_session.commit()

    result = load_authoritative_product_prices(
        db_session,
        workspace_id=1,
        store_id="shop-1",
        product_ids=["product-flash"],
        now=now,
    )["product-flash"]

    assert result["effective_price"] == 10.99
    assert result["effective_price_source"] == "tiktok_shop_flash_sale"
    assert result["latest_transaction_price"] == 14.99
    assert result["flash_sale_price"] == 10.99
    assert result["listing_price"] == 33.99


def test_authoritative_price_uses_newer_paid_transaction_and_ignores_unapplied_flash(
    db_session,
):
    shop = _seed_shop(db_session)
    now = datetime(2026, 7, 25, 8, 0, 0)
    _seed_transaction(
        db_session,
        shop=shop,
        product_id="product-order",
        order_id="order-new",
        price="7.99",
        paid_at=now - timedelta(minutes=5),
    )
    db_session.add(
        TikTokShopFlashSalePolicy(
            workspace_id=1,
            account_id=2,
            shop_row_id=shop.id,
            product_id="product-order",
            activity_price_amount=Decimal("9.99"),
            currency="USD",
            enabled=True,
            status="active",
            policy_revision=2,
            applied_revision=1,
            current_activity_status="NOT_START",
            current_begin_at=now + timedelta(hours=1),
            current_end_at=now + timedelta(hours=4),
            last_checked_at=now,
        )
    )
    db_session.commit()

    result = load_authoritative_product_prices(
        db_session,
        workspace_id=1,
        store_id="shop-1",
        product_ids=["product-order"],
        now=now,
    )["product-order"]

    assert result["effective_price"] == 7.99
    assert result["effective_price_source"] == "tiktok_shop_latest_transaction"
    assert result["flash_sale_price"] is None
