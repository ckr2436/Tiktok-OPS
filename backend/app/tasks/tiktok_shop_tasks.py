from __future__ import annotations

import asyncio
import logging
from datetime import date
from uuid import uuid4

from sqlalchemy import select

from app.celery_app import celery_app
from app.core.errors import APIError
from app.data.db import SessionLocal
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount, OAuthTikTokShopShop
from app.data.models.tiktok_shop import TikTokShopFlashSalePolicy
from app.services.oauth_tiktok_shop import refresh_account_token
from app.services.redis_locks import RedisDistributedLock
from app.services.tiktok_shop_flash_sale import reconcile_flash_sales
from app.services.tiktok_shop_sync import SUPPORTED_DOMAINS, sync_domain


logger = logging.getLogger("gmv.tasks.tiktok_shop")
QUEUE = "tiktok_shop"


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _active_shop_scopes() -> list[tuple[int, int, int]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(
                OAuthTikTokShopShop.workspace_id,
                OAuthTikTokShopShop.account_id,
                OAuthTikTokShopShop.id,
            )
            .join(
                OAuthTikTokShopAccount,
                OAuthTikTokShopAccount.id == OAuthTikTokShopShop.account_id,
            )
            .where(
                OAuthTikTokShopShop.is_active.is_(True),
                OAuthTikTokShopAccount.status == "active",
            )
            .order_by(OAuthTikTokShopShop.id.asc())
        ).all()
        return [(int(row[0]), int(row[1]), int(row[2])) for row in rows]


@celery_app.task(name="tiktok_shop.sync_domain", bind=True, max_retries=2)
def sync_shop_domain(
    self,
    *,
    workspace_id: int,
    account_id: int,
    shop_row_id: int,
    domain: str,
    trigger: str = "scheduled",
    start_date: str | None = None,
    end_date_exclusive: str | None = None,
) -> dict[str, object]:
    normalized = str(domain).strip().lower()
    if normalized not in SUPPORTED_DOMAINS:
        return {"status": "rejected", "reason": "invalid_domain", "domain": normalized}
    lock = RedisDistributedLock(
        key=f"gmv:tiktok_shop:sync:{workspace_id}:{account_id}:{shop_row_id}:{normalized}",
        owner_token=f"{self.request.id or 'direct'}:{uuid4().hex}",
        ttl_seconds=30 * 60,
        heartbeat_interval=60,
    )
    if not lock.acquire():
        return {"status": "skipped", "reason": "already_running", "domain": normalized}
    try:
        with SessionLocal() as db:
            try:
                run = asyncio.run(
                    sync_domain(
                        db,
                        workspace_id=int(workspace_id),
                        account_id=int(account_id),
                        shop_row_id=int(shop_row_id),
                        domain=normalized,
                        trigger=trigger,
                        start_date=_parse_date(start_date),
                        end_date_exclusive=_parse_date(end_date_exclusive),
                    )
                )
                return {
                    "status": run.status,
                    "run_id": int(run.id),
                    "domain": run.domain,
                    "pages": int(run.pages_fetched),
                    "rows": int(run.rows_upserted),
                }
            except APIError as exc:
                retryable = bool(isinstance(exc.data, dict) and exc.data.get("retryable"))
                if retryable and self.request.retries < self.max_retries:
                    raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))
                raise
    finally:
        lock.release()


def _dispatch(domains: tuple[str, ...]) -> dict[str, int]:
    scopes = _active_shop_scopes()
    queued = 0
    for scope_index, (workspace_id, account_id, shop_row_id) in enumerate(scopes):
        for domain_index, domain in enumerate(domains):
            sync_shop_domain.apply_async(
                kwargs={
                    "workspace_id": workspace_id,
                    "account_id": account_id,
                    "shop_row_id": shop_row_id,
                    "domain": domain,
                    "trigger": "scheduled",
                },
                queue=QUEUE,
                countdown=(scope_index * len(domains) + domain_index) * 2,
            )
            queued += 1
    return {"shops": len(scopes), "queued": queued}


@celery_app.task(name="tiktok_shop.dispatch_fast_syncs")
def dispatch_fast_syncs() -> dict[str, int]:
    return _dispatch(("orders", "analytics"))


@celery_app.task(name="tiktok_shop.dispatch_catalog_syncs")
def dispatch_catalog_syncs() -> dict[str, int]:
    return _dispatch(("catalog", "promotions"))


@celery_app.task(name="tiktok_shop.dispatch_finance_syncs")
def dispatch_finance_syncs() -> dict[str, int]:
    return _dispatch(("finance",))


@celery_app.task(name="tiktok_shop.refresh_tokens")
def refresh_tokens() -> dict[str, int]:
    refreshed = 0
    failed = 0
    with SessionLocal() as db:
        accounts = db.execute(
            select(OAuthTikTokShopAccount).where(OAuthTikTokShopAccount.status == "active")
        ).scalars().all()
        for account in accounts:
            try:
                asyncio.run(
                    refresh_account_token(
                        db,
                        workspace_id=int(account.workspace_id),
                        account_id=int(account.id),
                        force=False,
                    )
                )
                db.commit()
                refreshed += 1
            except Exception:
                db.rollback()
                failed += 1
                logger.exception("TikTok Shop token refresh failed account_id=%s", account.id)
    return {"refreshed": refreshed, "failed": failed}


@celery_app.task(name="tiktok_shop.reconcile_flash_sale_shop", bind=True, max_retries=2)
def reconcile_flash_sale_shop(
    self,
    *,
    workspace_id: int,
    account_id: int,
    shop_row_id: int,
    trigger: str = "scheduled",
    force_replace: bool = False,
) -> dict[str, object]:
    lock = RedisDistributedLock(
        key=f"gmv:tiktok_shop:flash-sale:{workspace_id}:{shop_row_id}",
        owner_token=f"{self.request.id or 'direct'}:{uuid4().hex}",
        ttl_seconds=20 * 60,
        heartbeat_interval=60,
    )
    if not lock.acquire():
        return {"status": "skipped", "reason": "already_running"}
    try:
        with SessionLocal() as db:
            try:
                return asyncio.run(
                    reconcile_flash_sales(
                        db,
                        workspace_id=int(workspace_id),
                        account_id=int(account_id),
                        shop_row_id=int(shop_row_id),
                        trigger=str(trigger),
                        force_replace=bool(force_replace),
                        verify_lock_ownership=lock.verify_ownership,
                    )
                )
            except APIError as exc:
                retryable = bool(isinstance(exc.data, dict) and exc.data.get("retryable"))
                if retryable and self.request.retries < self.max_retries:
                    raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
                raise
    finally:
        lock.release()


@celery_app.task(name="tiktok_shop.reconcile_flash_sales")
def dispatch_flash_sale_reconciliation() -> dict[str, int]:
    with SessionLocal() as db:
        rows = db.execute(
            select(
                TikTokShopFlashSalePolicy.workspace_id,
                TikTokShopFlashSalePolicy.account_id,
                TikTokShopFlashSalePolicy.shop_row_id,
            )
            .join(
                OAuthTikTokShopShop,
                OAuthTikTokShopShop.id == TikTokShopFlashSalePolicy.shop_row_id,
            )
            .join(
                OAuthTikTokShopAccount,
                OAuthTikTokShopAccount.id == TikTokShopFlashSalePolicy.account_id,
            )
            .where(
                TikTokShopFlashSalePolicy.enabled.is_(True),
                OAuthTikTokShopShop.is_active.is_(True),
                OAuthTikTokShopAccount.status == "active",
            )
            .distinct()
            .order_by(TikTokShopFlashSalePolicy.shop_row_id.asc())
        ).all()
    for index, row in enumerate(rows):
        reconcile_flash_sale_shop.apply_async(
            kwargs={
                "workspace_id": int(row[0]),
                "account_id": int(row[1]),
                "shop_row_id": int(row[2]),
                "trigger": "scheduled",
                "force_replace": False,
            },
            queue=QUEUE,
            countdown=index * 3,
        )
    return {"shops": len(rows), "queued": len(rows)}
