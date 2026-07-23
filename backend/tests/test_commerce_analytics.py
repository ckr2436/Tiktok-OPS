from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.data.models.commerce import CommerceProductCostVersion
from app.data.models.gmv_restructured import GmvProductMetricsDaily
from app.data.models.gmvmax_campaign_catalog import GmvmaxProductCampaignCatalog
from app.data.models.oauth_tiktok_shop import (
    OAuthTikTokShopAccount,
    OAuthTikTokShopShop,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.tiktok_shop import (
    TikTokShopOrder,
    TikTokShopOrderLine,
    TikTokShopProduct,
    TikTokShopSku,
)
from app.data.models.ttb_entities import TTBAdvertiser, TTBAdvertiserStoreLink
from app.data.models.workspaces import Workspace
from app.services.commerce_analytics import (
    CommerceScope,
    commerce_context,
    commerce_overview,
    date_range,
)
from app.features.tenants.commerce.router import ProductCostRequest
from app.services.commerce_orders import (
    CommerceOrderError,
    order_summary,
    validate_timezone,
)


def test_product_cost_currency_is_normalized_and_validated() -> None:
    request = ProductCostRequest(shop_id=1, currency=" usd ")
    assert request.currency == "USD"

    with pytest.raises(ValidationError):
        ProductCostRequest(shop_id=1, currency="US_D")


def test_advertiser_timezone_never_falls_back_to_utc() -> None:
    with pytest.raises(CommerceOrderError, match="Advertiser timezone"):
        validate_timezone(None)


def _seed_scope(db_session) -> OAuthTikTokShopShop:
    db_session.add(Workspace(id=1, name="MYUPONA", company_code="0001"))
    db_session.add(
        OAuthProviderApp(
            id=1,
            provider="tiktok-business",
            name="TikTok",
            client_id="client",
            client_secret_cipher=b"secret",
            redirect_uri="https://example.test/callback",
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            OAuthAccountTTB(
                id=11,
                workspace_id=1,
                provider_app_id=1,
                alias="Business",
                access_token_cipher=b"business-token",
                token_fingerprint=b"b" * 32,
            ),
            OAuthTikTokShopAccount(
                id=12,
                workspace_id=1,
                provider_app_id=1,
                alias="Shop",
                open_id="seller",
                access_token_cipher=b"shop-token",
                refresh_token_cipher=b"shop-refresh-token",
                token_fingerprint=b"s" * 32,
            ),
        ]
    )
    db_session.flush()
    shop = OAuthTikTokShopShop(
        id=21,
        workspace_id=1,
        account_id=12,
        shop_id="shop-provider-id",
        shop_code="US-001",
        shop_cipher="cipher",
        shop_name="MYUPONA",
        region="US",
        timezone_name="Etc/GMT+8",
        timezone_source="merchant_confirmed_fixed_utc_minus_8",
        timezone_locked=True,
        is_active=True,
    )
    db_session.add(shop)
    db_session.add_all(
        [
            TTBAdvertiser(
                workspace_id=1,
                auth_id=11,
                advertiser_id="adv-alaska",
                name="Alaska account",
                display_timezone="America/Anchorage",
                timezone="Etc/GMT+9",
                currency="USD",
            ),
            TTBAdvertiser(
                workspace_id=1,
                auth_id=11,
                advertiser_id="adv-main",
                name="Main account",
                display_timezone="America/New_York",
                timezone="Etc/GMT+5",
                currency="USD",
            ),
            TTBAdvertiserStoreLink(
                workspace_id=1,
                auth_id=11,
                advertiser_id="adv-alaska",
                store_id="shop-provider-id",
            ),
            TTBAdvertiserStoreLink(
                workspace_id=1,
                auth_id=11,
                advertiser_id="adv-main",
                store_id="shop-provider-id",
            ),
            GmvmaxProductCampaignCatalog(
                workspace_id=1,
                auth_id=11,
                advertiser_id="adv-main",
                campaign_id="campaign-main",
                campaign_name="Main GMV Max",
                store_id="shop-provider-id",
                shopping_ads_type="PRODUCT",
            ),
        ]
    )
    db_session.flush()
    return shop


def _metric(*, day: date, cost_cents: int = 500) -> GmvProductMetricsDaily:
    return GmvProductMetricsDaily(
        workspace_id=1,
        auth_id=11,
        advertiser_id="adv-main",
        store_id="shop-provider-id",
        campaign_id="campaign-main",
        item_group_id="product-1",
        stat_time_day=day,
        cost_cents=cost_cents,
        gross_revenue_cents=1000,
        orders=1,
        ingested_at=datetime(2026, 7, 3, 12),
    )


def test_context_prioritizes_advertiser_with_real_metrics(db_session) -> None:
    _seed_scope(db_session)
    db_session.add(_metric(day=date(2026, 7, 1)))
    db_session.commit()

    payload = commerce_context(db_session, workspace_id=1)

    assert payload["default_advertiser_id"] == "adv-main"
    assert payload["default_reporting_timezone"] == "America/New_York"
    advertisers = payload["shops"][0]["advertisers"]
    assert advertisers[0]["has_ad_metrics"] is True
    assert advertisers[0]["timezone_source"] == "tiktok_business_api"
    assert payload["shops"][0]["timezone"] == "Etc/GMT+8"
    assert payload["shops"][0]["timezone_locked"] is True


def test_advertiser_day_bounds_preserve_dst_transition() -> None:
    scope = CommerceScope(
        workspace_id=1,
        shop=object(),  # type: ignore[arg-type]
        advertiser_id="adv-main",
        advertiser_name="Main",
        reporting_timezone="America/New_York",
        reporting_timezone_source="tiktok_business_api",
        currency="USD",
    )

    _, _, start_utc, end_utc = date_range(
        scope=scope,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
    )

    assert start_utc == datetime(2026, 3, 8, 5)
    assert end_utc == datetime(2026, 3, 9, 4)
    assert (end_utc - start_utc).total_seconds() == 23 * 60 * 60


def test_order_summary_uses_advertiser_timezone_not_shop_timezone(db_session) -> None:
    shop = _seed_scope(db_session)
    db_session.add(
        TikTokShopOrder(
            workspace_id=1,
            account_id=12,
            shop_row_id=shop.id,
            order_id="order-boundary",
            status="IN_TRANSIT",
            currency="USD",
            total_amount=Decimal("12.00"),
            paid_at=datetime(2026, 3, 8, 4, 30),
        )
    )
    db_session.commit()

    march_7 = order_summary(
        db_session,
        workspace_id=1,
        store_id="shop-provider-id",
        start_date=date(2026, 3, 7),
        end_date=date(2026, 3, 7),
        advertiser_timezone="America/New_York",
    )
    march_8 = order_summary(
        db_session,
        workspace_id=1,
        store_id="shop-provider-id",
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        advertiser_timezone="America/New_York",
    )

    assert march_7["order_count"] == 1
    assert march_8["order_count"] == 0
    assert march_7["shop_timezone"] == "Etc/GMT+8"
    assert march_7["range"]["timezone"] == "America/New_York"


def test_overview_allocates_paid_total_and_uses_effective_cost_version(
    db_session,
) -> None:
    shop = _seed_scope(db_session)
    db_session.add_all(
        [
            TikTokShopProduct(
                workspace_id=1,
                account_id=12,
                shop_row_id=shop.id,
                product_id="product-1",
                title="Body balm",
                status="ACTIVATE",
                currency="USD",
                min_sale_price=Decimal("20.00"),
            ),
            TikTokShopSku(
                workspace_id=1,
                account_id=12,
                shop_row_id=shop.id,
                product_id="product-1",
                sku_id="sku-1",
                currency="USD",
                sale_price=Decimal("20.00"),
                inventory_quantity=25,
            ),
            TikTokShopOrder(
                workspace_id=1,
                account_id=12,
                shop_row_id=shop.id,
                order_id="order-1",
                status="IN_TRANSIT",
                currency="USD",
                total_amount=Decimal("20.00"),
                paid_at=datetime(2026, 7, 1, 16),
            ),
            TikTokShopOrderLine(
                workspace_id=1,
                account_id=12,
                shop_row_id=shop.id,
                order_id="order-1",
                line_item_id="line-1",
                product_id="product-1",
                product_name="Body balm",
                sku_id="sku-1",
                currency="USD",
                sale_price=Decimal("18.00"),
                quantity=1,
            ),
            CommerceProductCostVersion(
                workspace_id=1,
                shop_row_id=shop.id,
                product_id="product-1",
                sku_id="",
                effective_from=datetime(2026, 6, 1),
                currency="USD",
                unit_cost=Decimal("4"),
                packaging_cost=Decimal("1"),
                platform_fee_rate=Decimal("0.10"),
            ),
            CommerceProductCostVersion(
                workspace_id=1,
                shop_row_id=shop.id,
                product_id="product-1",
                sku_id="",
                effective_from=datetime(2026, 7, 2),
                currency="USD",
                unit_cost=Decimal("9"),
            ),
            _metric(day=date(2026, 7, 1), cost_cents=500),
        ]
    )
    db_session.commit()

    payload = commerce_overview(
        db_session,
        workspace_id=1,
        shop_id=shop.id,
        advertiser_id="adv-main",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 3),
    )

    assert payload["summary"]["order_paid_sales"] == 20.0
    assert payload["summary"]["allocated_product_sales"] == 20.0
    assert payload["summary"]["order_reconciliation_delta"] == 0.0
    assert payload["summary"]["ad_spend"] == 5.0
    assert payload["summary"]["contribution_profit"] == 8.0
    assert payload["summary"]["contribution_margin"] == 0.4
    assert payload["products"][0]["actual_sales"] == 20.0
    assert payload["products"][0]["current_cost"]["unit_cost"] == 9.0
    assert payload["data_health"]["finance"]["coverage_ratio"] == 0.0
    assert payload["data_health"]["sales_basis"] == "shop_order_paid_total"
