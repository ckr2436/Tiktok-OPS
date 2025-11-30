from __future__ import annotations

import asyncio
import contextlib
import logging
"""Celery task layer orchestrating GMV Max syncs and actions."""

from datetime import datetime, timedelta
from uuid import uuid4
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import get_db
from app.data.models.ttb_gmvmax import TTBGmvMaxActionLog, TTBGmvMaxCampaign
from app.services.ttb_client_factory import build_ttb_gmvmax_client
from app.services.ttb_balances import sync_advertiser_balance
from app.services.gmvmax_heating import run_creative_heating_cycle
from app.services.ttb_gmvmax import (
    aggregate_recent_metrics,
    apply_campaign_action,
    decide_campaign_action,
    get_or_create_strategy_config,
    sync_gmvmax_reports_for_campaign,
    sync_gmvmax_campaigns,
    sync_gmvmax_metrics_daily,
    sync_gmvmax_metrics_hourly,
)
from app.services.redis_locks import RedisDistributedLock
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxOccupiedCustomShopAdsListRequest,
    GMVMaxReportGetRequest,
    GMVMaxStoreAdUsageCheckRequest,
)

logger = logging.getLogger("gmv.tasks.gmvmax")


T = TypeVar("T")

def _run_with_client(db: Session, auth_id: int, fn: Callable[[Any], Awaitable[T]]) -> T:
    async def _runner() -> T:
        client = build_ttb_gmvmax_client(db, auth_id=auth_id)
        try:
            return await fn(client)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("gmvmax client close failed", exc_info=True)

    return asyncio.run(_runner())


def _db_session() -> Session:
    gen = get_db()
    sess = next(gen)
    setattr(sess, "__gen__", gen)
    return sess


def _close_session(sess: Session) -> None:
    gen = getattr(sess, "__gen__", None)
    try:
        sess.close()
    finally:
        try:
            if gen:
                next(gen)
        except StopIteration:
            pass


def _iter_sync_scopes(db: Session) -> list[tuple[int, int, str]]:
    stmt = (
        select(
            TTBGmvMaxCampaign.workspace_id,
            TTBGmvMaxCampaign.auth_id,
            TTBGmvMaxCampaign.advertiser_id,
        )
        .where(TTBGmvMaxCampaign.advertiser_id.is_not(None))
        .distinct()
    )
    rows = []
    for workspace_id, auth_id, advertiser_id in db.execute(stmt):
        if not auth_id or not advertiser_id:
            continue
        rows.append((int(workspace_id), int(auth_id), str(advertiser_id)))
    return rows


def _find_campaign_row(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
) -> TTBGmvMaxCampaign | None:
    stmt = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    return db.execute(stmt).scalars().first()


def _iter_active_campaigns(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str | None = None,
    ) -> list[TTBGmvMaxCampaign]:
    query = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == workspace_id)
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.is_deleted.is_(False))
        .where(TTBGmvMaxCampaign.operation_status.notin_(("DELETE", "STATUS_DISABLE")))
    )
    query = query.where(
        or_(
            TTBGmvMaxCampaign.secondary_status.is_(None),
            TTBGmvMaxCampaign.secondary_status != "CAMPAIGN_STATUS_DELETE",
        )
    )
    if advertiser_id is not None:
        query = query.where(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))
    return list(db.execute(query).scalars().all())


# Beat-driven sweep (see scheduler_catalog) to sync all GMV Max scopes via
# TikTok /gmv_max/campaign/get/ and upsert TTBGmvMaxCampaign* tables.
@celery_app.task(
    bind=True,
    name="ttb.sync_gmvmax",
    queue="gmvmax",
)
def task_sync_gmvmax(self, **extra: Any) -> dict:
    """通过 Celery Beat 周期触发的全局 GMV Max 同步任务。

    - 仅允许单实例运行（Redis 锁）。
    - 按 workspace/auth/advertiser 维度串行同步，避免重叠调用。
    """

    lock = RedisDistributedLock(key="gmvmax:beat:sync", owner_token=self.request.id or str(uuid4()))
    if not lock.acquire(timeout=1.0, retry_interval=0.1):
        logger.info("gmvmax beat sync skipped: lock held")
        return {"status": "skipped", "reason": "inflight"}

    db = _db_session()
    success = 0
    failures: list[dict[str, Any]] = []
    scopes = _iter_sync_scopes(db)

    try:
        for workspace_id, auth_id, advertiser_id in scopes:
            try:
                result = _run_with_client(
                    db,
                    auth_id,
                    lambda client: sync_gmvmax_campaigns(
                        db,
                        client,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                    ),
                )
                db.commit()
                logger.info(
                    "gmvmax beat sync ok",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "result": result,
                    },
                )
                success += 1
            except Exception:
                db.rollback()
                failures.append(
                    {
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                    }
                )
                logger.exception(
                    "gmvmax beat sync failed",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                    },
                )
    finally:
        _close_session(db)
        with contextlib.suppress(Exception):
            lock.release()

    return {"status": "ok", "synced_scopes": success, "failed_scopes": failures}


# Sync campaigns for a specific binding (scheduled ~every 10 minutes); writes
# TTBGmvMaxCampaign/TTBGmvMaxCampaignProduct via TikTok /gmv_max/campaign/get/.
@celery_app.task(
    bind=True,
    name="gmvmax.sync_campaigns",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_sync_campaigns(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    filters: Optional[dict[str, Any]] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **extra: Any,
    ) -> dict:
    """同步 GMV Max Campaign 列表到本地 DB（幂等）。"""
    db = _db_session()
    try:
        payload_filters = dict(filters or {})
        if not payload_filters and params and isinstance(params, dict):
            payload_filters = dict(params.get("filters") or {})

        result = _run_with_client(
            db,
            auth_id,
            lambda client: sync_gmvmax_campaigns(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                **payload_filters,
            ),
        )
        db.commit()
        logger.info(
            "gmvmax.sync_campaigns done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "result": result,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        return result or {}
    except Exception:
        db.rollback()
        logger.exception(
            "gmvmax.sync_campaigns failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        raise
    finally:
        _close_session(db)


# Advertiser balance sync helper (triggered manually); no direct GMV Max API
# but persists balance snapshot used for eligibility checks.
@celery_app.task(
    bind=True,
    name="gmvmax.sync_advertiser_balance",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_sync_advertiser_balance(
    self,
    *,
    workspace_id: int,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    bc_id: str | None = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **_: Any,
) -> dict[str, Any]:
    params = params or {}
    auth_id = auth_id or params.get("auth_id")
    advertiser_id = advertiser_id or params.get("advertiser_id")
    bc_id = bc_id or params.get("bc_id")
    if auth_id is None or advertiser_id is None or bc_id is None:
        logger.warning(
            "gmvmax.sync_advertiser_balance missing required identifiers",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "params": params,
                "schedule_id": schedule_id,
            },
        )
        return {"status": "skipped", "reason": "missing identifiers"}
    db = _db_session()
    try:
        result = asyncio.run(
            sync_advertiser_balance(
                db,
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                bc_id=str(bc_id),
                advertiser_id=str(advertiser_id),
            )
        )
        db.commit()
        logger.info(
            "gmvmax.sync_advertiser_balance done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "params": params,
            },
        )
        return result
    except Exception:
        db.rollback()
        logger.exception(
            "gmvmax.sync_advertiser_balance failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "bc_id": bc_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "params": params,
            },
        )
        raise
    finally:
        _close_session(db)


# Metric sync (hourly/daily windows) scheduled via scheduler_catalog; calls
# TikTok GET /gmv_max/report/get/ and upserts TTBGmvMaxMetricsDaily/Hourly.
@celery_app.task(
    bind=True,
    name="gmvmax.sync_metrics",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_sync_metrics(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "HOUR",
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **extra: Any,
) -> dict:
    """按粒度同步 GMV Max 指标（幂等，底层 upsert）。"""
    db = _db_session()
    try:
        def _sync(client: Any) -> Awaitable[dict]:
            if campaign_id:
                row = _find_campaign_row(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    campaign_id=campaign_id,
                )
                if not row:
                    raise RuntimeError(f"campaign not found: {campaign_id}")

                effective_start = start_date or datetime.utcnow().date()
                effective_end = end_date or datetime.utcnow().date()

                if str(granularity).upper() == "DAY":
                    return sync_gmvmax_metrics_daily(
                        db,
                        client,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        campaign=row,
                        start_date=effective_start,
                        end_date=effective_end,
                    )

                return sync_gmvmax_metrics_hourly(
                    db,
                    client,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    campaign=row,
                    start_date=effective_start,
                    end_date=effective_end,
                )

            async def _sync_all() -> dict:
                campaigns = _iter_active_campaigns(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                )
                if not campaigns:
                    return {"campaign_rows": 0, "creative_rows": 0}

                today = datetime.utcnow().date()
                window_start = today - timedelta(days=2)
                totals = {"campaign_rows": 0, "creative_rows": 0}
                for campaign in campaigns:
                    result = await sync_gmvmax_reports_for_campaign(
                        db,
                        client,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        campaign=campaign,
                        start_date=window_start,
                        end_date=today,
                    )
                    totals["campaign_rows"] += result.get("campaign_rows", 0)
                    totals["creative_rows"] += result.get("creative_rows", 0)
                return totals

            return _sync_all()

        result = _run_with_client(db, auth_id, _sync)

        db.commit()
        logger.info(
            "gmvmax.sync_metrics done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "granularity": granularity,
                "result": result,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        return result or {}
    except Exception:
        db.rollback()
        logger.exception(
            "gmvmax.sync_metrics failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "granularity": granularity,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        raise
    finally:
        _close_session(db)


# Apply campaign actions (status/budget/strategy) and log to TTBGmvMaxActionLog;
# uses TikTok campaign status/update endpoints.
@celery_app.task(
    bind=True,
    name="gmvmax.apply_action",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_apply_action(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    action: str,
    payload: Optional[dict[str, Any]] = None,
    reason: Optional[str] = None,
    performed_by: str = "system",
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **extra: Any,
) -> dict:
    """对指定 Campaign 执行动作；成功会落 TTBGmvMaxActionLog（见 services.ttb_gmvmax）。"""
    db = _db_session()
    try:
        row = _find_campaign_row(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
        )
        if not row:
            raise RuntimeError(f"campaign not found: {campaign_id}")

        result_log = _run_with_client(
            db,
            auth_id,
            lambda client: apply_campaign_action(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=row,
                action=str(action).upper(),
                payload=payload or {},
                reason=reason,
                performed_by=performed_by,
            ),
        )
        db.commit()
        logger.info(
            "gmvmax.apply_action done",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "action": action,
                "result_id": getattr(result_log, "id", None),
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        return {"log_id": getattr(result_log, "id", None)}
    except Exception:
        db.rollback()
        logger.exception(
            "gmvmax.apply_action failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "action": action,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        raise
    finally:
        _close_session(db)


@celery_app.task(
    bind=True,
    name="gmvmax.evaluate_strategy",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_evaluate_strategy(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    campaign_id: str,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **extra: Any,
) -> dict:
    db = _db_session()
    try:
        row = _find_campaign_row(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
        )
        if not row:
            raise RuntimeError(f"campaign not found: {campaign_id}")

        strategy = get_or_create_strategy_config(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign=row,
        )
        if not strategy.enabled:
            return {"skipped": True, "reason": "strategy.disabled"}

        cooldown_minutes = strategy.cooldown_minutes or 0
        if cooldown_minutes > 0:
            stmt_log = (
                select(TTBGmvMaxActionLog)
                .where(TTBGmvMaxActionLog.campaign_id == row.id)
                .order_by(TTBGmvMaxActionLog.id.desc())
            )
            last_log = db.execute(stmt_log).scalars().first()
            if last_log and last_log.created_at:
                elapsed = datetime.utcnow() - last_log.created_at
                if elapsed.total_seconds() < cooldown_minutes * 60:
                    return {"skipped": True, "reason": "cooldown"}

        metrics = aggregate_recent_metrics(db, campaign=row)
        decision = decide_campaign_action(
            campaign=row,
            strategy=strategy,
            metrics=metrics,
        )
        if not decision:
            return {"skipped": True, "reason": "no_decision", "metrics": metrics}

        log_row = _run_with_client(
            db,
            auth_id,
            lambda client: apply_campaign_action(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                campaign=row,
                action=decision["action"],
                payload=decision.get("payload") or {},
                reason=decision.get("reason"),
                performed_by="auto-strategy",
            ),
        )
        db.commit()
        logger.info(
            "gmvmax.evaluate_strategy applied",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "decision": dict(decision),
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
                "log_id": getattr(log_row, "id", None),
            },
        )
        return {
            "applied": True,
            "decision": dict(decision),
            "log_id": getattr(log_row, "id", None),
        }
    except Exception:
        db.rollback()
        logger.exception(
            "gmvmax.evaluate_strategy failed",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "campaign_id": campaign_id,
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "run_id": run_id,
            },
        )
        raise
    finally:
        _close_session(db)


# Periodic every ~15 minutes (scheduler_catalog) to evaluate TTBGmvMaxCreativeHeating
# and stop creatives via TikTok /campaign/gmv_max/action/apply/ when needed.
@celery_app.task(
    bind=True,
    name="gmvmax.creative_heating_cycle",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_creative_heating_cycle(self, **extra: Any) -> dict:
    db = _db_session()
    try:
        result = asyncio.run(run_creative_heating_cycle(db))
        db.commit()
        logger.info("gmvmax.creative_heating_cycle done", extra=result)
        return result
    except Exception:
        db.rollback()
        logger.exception("gmvmax.creative_heating_cycle failed")
        raise
    finally:
        _close_session(db)


# Eligibility precheck task: calls /gmv_max/store/shop_ad_usage_check/,
# /gmv_max/identity/get/, and /gmv_max/occupied_custom_shop_ads/list/.
@celery_app.task(
    bind=True,
    name="gmvmax.precheck",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_precheck(
    self,
    *,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    identity_id: Optional[str] = None,
    product_item_group_ids: Optional[list[str]] = None,
    occupied_asset_type: Optional[str] = None,
    **extra: Any,
) -> dict:
    db = _db_session()
    try:
        async def _worker(client: Any) -> dict:
            usage_resp = await client.gmv_max_store_shop_ad_usage_check(
                GMVMaxStoreAdUsageCheckRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    store_authorized_bc_id=store_authorized_bc_id,
                )
            )
            identity_resp = await client.gmv_max_identity_get(
                GMVMaxIdentityGetRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    store_authorized_bc_id=str(store_authorized_bc_id),
                )
            )

            occupancy_resp = None
            asset_ids = []
            if identity_id:
                asset_ids.append(str(identity_id))
            if product_item_group_ids:
                asset_ids.extend([str(item) for item in product_item_group_ids])
            asset_ids = [item for item in asset_ids if item.strip()]
            if asset_ids:
                occupancy_resp = await client.gmv_max_occupied_custom_shop_ads_list(
                    GMVMaxOccupiedCustomShopAdsListRequest(
                        advertiser_id=str(advertiser_id),
                        store_id=str(store_id),
                        occupied_asset_type=str(occupied_asset_type or ("IDENTITY" if identity_id else "SPU")),
                        asset_ids=asset_ids,
                    )
                )

            return {
                "store_usage": usage_resp.data.model_dump(exclude_none=True),
                "identities": [
                    entry.model_dump(exclude_none=True)
                    for entry in getattr(identity_resp.data, "identity_list", [])
                ],
                "occupancy": occupancy_resp.data.model_dump(exclude_none=True)
                if occupancy_resp
                else None,
                "request_ids": {
                    "store_usage": usage_resp.request_id,
                    "identities": identity_resp.request_id,
                    "occupancy": occupancy_resp.request_id if occupancy_resp else None,
                },
            }

        return _run_with_client(db, auth_id, _worker)
    except Exception:
        logger.exception(
            "gmvmax.precheck failed",
            extra={
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "store_id": store_id,
            },
        )
        raise
    finally:
        _close_session(db)


# Async report fetch for metrics sync (TikTok GET /gmv_max/report/get/);
# caller persists results.
@celery_app.task(
    bind=True,
    name="gmvmax.report_get",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_report_get(
    self,
    *,
    auth_id: int,
    report_request: dict[str, Any],
    **extra: Any,
) -> dict:
    db = _db_session()
    try:
        async def _worker(client: Any) -> dict:
            request = GMVMaxReportGetRequest.model_validate(report_request)
            response = await client.gmv_max_report_get(request)
            return {
                "report": response.data.model_dump(exclude_none=True),
                "request_id": response.request_id,
            }

        return _run_with_client(db, auth_id, _worker)
    except Exception:
        logger.exception("gmvmax.report_get failed", extra={"auth_id": auth_id})
        raise
    finally:
        _close_session(db)


# Strategy preview helper calling TikTok GET /gmv_max/bid/recommend/ for
# recommendations.
@celery_app.task(
    bind=True,
    name="gmvmax.strategy_preview",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=5,
    queue="gmvmax",
)
def task_gmvmax_strategy_preview(
    self,
    *,
    auth_id: int,
    bid_request: dict[str, Any],
    **extra: Any,
) -> dict:
    db = _db_session()
    try:
        async def _worker(client: Any) -> dict:
            request = GMVMaxBidRecommendRequest.model_validate(bid_request)
            response = await client.gmv_max_bid_recommend(request)
            return {
                "recommendation": response.data.model_dump(exclude_none=True),
                "request_id": response.request_id,
            }

        return _run_with_client(db, auth_id, _worker)
    except Exception:
        logger.exception(
            "gmvmax.strategy_preview failed",
            extra={"auth_id": auth_id, "advertiser_id": bid_request.get("advertiser_id")},
        )
        raise
    finally:
        _close_session(db)
