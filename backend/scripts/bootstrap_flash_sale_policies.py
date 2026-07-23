from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.core.config import settings
from app.data.db import SessionLocal
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import TikTokShopFlashSalePolicy
from app.services.tiktok_shop_api import TikTokShopAPIClient
from app.services.tiktok_shop_flash_sale import (
    _live_flash_sales,
    coverage_end,
    reconcile_flash_sales,
    utc_now,
)


async def run(*, apply: bool, verify_hold: bool, shop_row_id: int | None) -> None:
    with SessionLocal() as db:
        shops = list(
            db.scalars(
                select(OAuthTikTokShopShop)
                .where(
                    OAuthTikTokShopShop.is_active.is_(True),
                    *(
                        [OAuthTikTokShopShop.id == int(shop_row_id)]
                        if shop_row_id
                        else []
                    ),
                )
                .order_by(OAuthTikTokShopShop.id.asc())
            )
        )
        for shop in shops:
            client = await TikTokShopAPIClient.create(
                db,
                workspace_id=int(shop.workspace_id),
                account_id=int(shop.account_id),
                shop_row_id=int(shop.id),
            )
            async with client:
                activities = await _live_flash_sales(client)
            product_ids = sorted(
                {
                    product_id
                    for activity in activities
                    for product_id in activity.products
                }
            )
            print(
                f"shop={shop.id} name={shop.shop_name!r} timezone={shop.timezone_name} "
                f"activities={len(activities)} products={len(product_ids)}"
            )
            now = utc_now()
            for product_id in product_ids:
                matches = [
                    activity for activity in activities if product_id in activity.products
                ]
                latest = max(matches, key=lambda item: item.end_at)
                covered_until = coverage_end(
                    [(item.begin_at, item.end_at) for item in matches],
                    now=now,
                    max_gap_seconds=int(settings.TT_SHOP_FLASH_SALE_GAP_SECONDS) + 5,
                )
                price = latest.products[product_id]
                print(
                    f"  product={product_id} price={price} "
                    f"activity={latest.activity_id} coverage={covered_until}"
                )
                if not apply:
                    continue
                row = db.scalar(
                    select(TikTokShopFlashSalePolicy).where(
                        TikTokShopFlashSalePolicy.shop_row_id == int(shop.id),
                        TikTokShopFlashSalePolicy.product_id == product_id,
                    )
                )
                if row is not None:
                    continue
                db.add(
                    TikTokShopFlashSalePolicy(
                        workspace_id=int(shop.workspace_id),
                        account_id=int(shop.account_id),
                        shop_row_id=int(shop.id),
                        product_id=product_id,
                        enabled=True,
                        activity_price_amount=price,
                        currency="USD",
                        status="active",
                        policy_revision=1,
                        applied_revision=1,
                        current_activity_id=latest.activity_id,
                        current_activity_status=latest.status,
                        current_begin_at=latest.begin_at,
                        current_end_at=covered_until,
                        next_renewal_at=(
                            covered_until
                            - timedelta(
                                seconds=int(
                                    settings.TT_SHOP_FLASH_SALE_MIN_COVERAGE_SECONDS
                                )
                            )
                            if covered_until
                            else now
                        ),
                        last_checked_at=now,
                        last_applied_at=now,
                    )
                )
            if apply:
                db.commit()
            if verify_hold:
                result = await reconcile_flash_sales(
                    db,
                    workspace_id=int(shop.workspace_id),
                    account_id=int(shop.account_id),
                    shop_row_id=int(shop.id),
                    trigger="bootstrap_verify",
                    force_replace=False,
                )
                print(f"  reconciliation={result}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialize flash-sale automation from live TikTok Shop activities."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-hold", action="store_true")
    parser.add_argument("--shop-row-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(
        run(
            apply=bool(args.apply),
            verify_hold=bool(args.verify_hold),
            shop_row_id=args.shop_row_id,
        )
    )
