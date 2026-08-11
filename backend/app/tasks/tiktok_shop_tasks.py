from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.celery_app import celery_app
from app.core.errors import APIError
from app.data.db import SessionLocal
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount, OAuthTikTokShopShop
from app.data.models.tiktok_shop import TikTokShopFlashSalePolicy
from app.data.models.tiktok_shop_content_posting import TikTokShopContentPost
from app.services.oauth_tiktok_shop import refresh_account_token
from app.services.redis_locks import RedisDistributedLock
from app.services.tiktok_shop_flash_sale import reconcile_flash_sales
from app.services.tiktok_shop_content_posting import (
    TERMINAL_WORKFLOW_STATUSES,
    build_precheck_body,
    build_publish_body,
    resolve_workflow_file,
    safe_error_details,
    update_request_id,
    utcnow_naive,
)
from app.services.tiktok_shop_creator_api import TikTokShopCreatorAPIClient
from app.services.tiktok_shop_sync import SUPPORTED_DOMAINS, sync_domain


logger = logging.getLogger("gmv.tasks.tiktok_shop")
QUEUE = "tiktok_shop"


def _content_post_poll_delay() -> int:
    from app.core.config import settings

    return max(5, min(int(settings.TT_SHOP_CONTENT_POSTING_POLL_INTERVAL_SECONDS), 60))


def _content_post_max_polls() -> int:
    from app.core.config import settings

    return max(6, min(int(settings.TT_SHOP_CONTENT_POSTING_MAX_POLL_ATTEMPTS), 360))


def _extract_mapping(value: object, key: str) -> dict:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return dict(nested) if isinstance(nested, dict) else {}


def _parse_post_time(value: object) -> datetime | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)


async def _advance_content_post(db, row: TikTokShopContentPost) -> tuple[dict[str, object], int | None]:
    client = await TikTokShopCreatorAPIClient.create(
        db,
        workspace_id=int(row.workspace_id),
        account_id=int(row.account_id),
    )
    async with client:
        if not row.official_file_id:
            row.workflow_status = "UPLOADING"
            row.last_error_code = None
            row.last_error_message = None
            db.add(row)
            db.commit()
            path = resolve_workflow_file(row)
            with path.open("rb") as file_obj:
                result = await client.upload_video_file(
                    file_name=row.original_filename,
                    file_obj=file_obj,
                    media_type=row.media_type,
                )
            video_file = _extract_mapping(result.data, "video_file")
            file_id = str(video_file.get("id") or "").strip()
            if not file_id:
                raise APIError(
                    "TIKTOK_SHOP_UPLOAD_INVALID_RESPONSE",
                    "TikTok Shop upload response did not include video_file.id.",
                    502,
                )
            row.official_file_id = file_id[:192]
            row.upload_md5 = str(video_file.get("md5") or "").strip()[:64] or None
            update_request_id(row, "upload", result.request_id)
            row.workflow_status = "PRECHECKING"
            db.add(row)
            db.commit()

        if not row.precheck_task_id:
            result = await client.create_precheck(build_precheck_body(row))
            precheck = _extract_mapping(result.data, "precheck")
            task_id = str(precheck.get("task_id") or "").strip()
            if not task_id:
                raise APIError(
                    "TIKTOK_SHOP_PRECHECK_INVALID_RESPONSE",
                    "TikTok Shop precheck response did not include precheck.task_id.",
                    502,
                )
            row.precheck_task_id = task_id[:192]
            row.precheck_status = "PROCESSING"
            row.workflow_status = "PRECHECKING"
            row.poll_attempts = 0
            row.next_poll_at = utcnow_naive() + timedelta(seconds=_content_post_poll_delay())
            update_request_id(row, "precheck_create", result.request_id)
            db.add(row)
            db.commit()
            return {"status": row.workflow_status, "post_id": int(row.id)}, _content_post_poll_delay()

        if row.precheck_status != "SUCCESS":
            result = await client.precheck_status(str(row.precheck_task_id))
            precheck = _extract_mapping(result.data, "precheck_task")
            status = str(precheck.get("result") or "").strip().upper()
            issues = precheck.get("issues")
            row.precheck_status = status or "PROCESSING"
            row.precheck_issues_json = list(issues) if isinstance(issues, list) else []
            update_request_id(row, "precheck_status", result.request_id)
            if row.precheck_status == "FAIL":
                row.workflow_status = "PRECHECK_FAILED"
                row.completed_at = utcnow_naive()
                row.next_poll_at = None
                db.add(row)
                db.commit()
                return {"status": row.workflow_status, "post_id": int(row.id)}, None
            if row.precheck_status != "SUCCESS":
                row.poll_attempts = int(row.poll_attempts or 0) + 1
                if row.poll_attempts >= _content_post_max_polls():
                    row.workflow_status = "PRECHECK_TIMEOUT"
                    row.next_poll_at = None
                    db.add(row)
                    db.commit()
                    return {"status": row.workflow_status, "post_id": int(row.id)}, None
                row.workflow_status = "PRECHECKING"
                row.next_poll_at = utcnow_naive() + timedelta(seconds=_content_post_poll_delay())
                db.add(row)
                db.commit()
                return {"status": row.workflow_status, "post_id": int(row.id)}, _content_post_poll_delay()
            row.workflow_status = "READY_TO_PUBLISH"
            row.poll_attempts = 0
            row.next_poll_at = None
            db.add(row)
            db.commit()

        if not row.publish_requested:
            return {"status": row.workflow_status, "post_id": int(row.id)}, None

        if not row.video_id:
            row.workflow_status = "PUBLISHING"
            db.add(row)
            db.commit()
            result = await client.publish_video(build_publish_body(row))
            video = _extract_mapping(result.data, "video")
            video_id = str(video.get("id") or "").strip()
            if not video_id:
                raise APIError(
                    "TIKTOK_SHOP_PUBLISH_INVALID_RESPONSE",
                    "TikTok Shop publish response did not include video.id.",
                    502,
                    data={"request_id": result.request_id, "retryable": False},
                )
            row.video_id = video_id[:192]
            row.post_status = "PROCESSING"
            row.workflow_status = "PROCESSING"
            row.poll_attempts = 0
            row.next_poll_at = utcnow_naive() + timedelta(seconds=_content_post_poll_delay())
            update_request_id(row, "publish", result.request_id)
            db.add(row)
            db.commit()
            return {"status": row.workflow_status, "post_id": int(row.id)}, _content_post_poll_delay()

        result = await client.video_status(str(row.video_id))
        video = _extract_mapping(result.data, "video")
        post_status = str(video.get("post_status") or "").strip().upper() or "PROCESSING"
        row.post_status = post_status
        row.post_time = _parse_post_time(video.get("post_time")) or row.post_time
        update_request_id(row, "publish_status", result.request_id)
        if post_status == "SUCCESS":
            row.workflow_status = "SUCCESS"
            row.completed_at = utcnow_naive()
            row.next_poll_at = None
            db.add(row)
            db.commit()
            return {"status": row.workflow_status, "post_id": int(row.id), "video_id": row.video_id}, None
        if post_status == "FAIL":
            row.workflow_status = "FAILED"
            row.completed_at = utcnow_naive()
            row.next_poll_at = None
            db.add(row)
            db.commit()
            return {"status": row.workflow_status, "post_id": int(row.id), "video_id": row.video_id}, None
        row.workflow_status = "PROCESSING"
        row.poll_attempts = int(row.poll_attempts or 0) + 1
        if row.poll_attempts >= _content_post_max_polls():
            row.workflow_status = "STATUS_UNKNOWN"
            row.next_poll_at = None
            db.add(row)
            db.commit()
            return {"status": row.workflow_status, "post_id": int(row.id), "video_id": row.video_id}, None
        row.next_poll_at = utcnow_naive() + timedelta(seconds=_content_post_poll_delay())
        db.add(row)
        db.commit()
        return {"status": row.workflow_status, "post_id": int(row.id)}, _content_post_poll_delay()


@celery_app.task(name="tiktok_shop.process_content_post", bind=True)
def process_content_post(self, *, post_id: int) -> dict[str, object]:
    lock = RedisDistributedLock(
        key=f"gmv:tiktok_shop:content-post:{int(post_id)}",
        owner_token=f"{self.request.id or 'direct'}:{uuid4().hex}",
        ttl_seconds=15 * 60,
        heartbeat_interval=30,
    )
    if not lock.acquire():
        return {"status": "skipped", "reason": "already_running", "post_id": int(post_id)}
    reschedule: int | None = None
    try:
        with SessionLocal() as db:
            row = db.get(TikTokShopContentPost, int(post_id))
            if row is None:
                return {"status": "missing", "post_id": int(post_id)}
            if row.workflow_status in TERMINAL_WORKFLOW_STATUSES:
                return {"status": row.workflow_status, "post_id": int(row.id)}
            try:
                result, reschedule = asyncio.run(_advance_content_post(db, row))
            except APIError as exc:
                db.rollback()
                row = db.get(TikTokShopContentPost, int(post_id))
                if row is None:
                    raise
                error_code, error_message, request_id, retryable = safe_error_details(exc)
                row.last_error_code = error_code
                row.last_error_message = error_message
                row.last_error_request_id = request_id
                if row.workflow_status == "PUBLISHING" and (
                    error_code
                    in {
                        "TIKTOK_SHOP_CREATOR_UNAVAILABLE",
                        "TIKTOK_SHOP_PUBLISH_INVALID_RESPONSE",
                    }
                    or request_id is None
                    or retryable
                ):
                    row.workflow_status = "PUBLISH_UNCERTAIN"
                else:
                    row.workflow_status = "FAILED"
                row.completed_at = utcnow_naive()
                row.next_poll_at = None
                db.add(row)
                db.commit()
                logger.warning(
                    "TikTok Shop content post failed post_id=%s stage=%s error_code=%s request_id=%s",
                    row.id,
                    row.workflow_status,
                    error_code,
                    request_id,
                )
                result = {"status": row.workflow_status, "post_id": int(row.id), "error_code": error_code}
    finally:
        lock.release()
    if reschedule is not None:
        process_content_post.apply_async(
            kwargs={"post_id": int(post_id)},
            queue=QUEUE,
            countdown=int(reschedule),
        )
    return result


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
        if str(trigger) == "user_batch_apply":
            raise self.retry(countdown=15, max_retries=20)
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
