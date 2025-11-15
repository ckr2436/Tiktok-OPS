from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import get_db
from app.data.models.ttb_gmvmax import TTBGmvMaxActionLog, TTBGmvMaxCampaign
from app.services.ttb_client_factory import build_ttb_client
from app.services.gmvmax_heating import run_creative_heating_cycle
from app.services.ttb_gmvmax import (
    aggregate_recent_metrics,
    apply_campaign_action,
    decide_campaign_action,
    get_or_create_strategy_config,
    sync_gmvmax_campaigns,
    sync_gmvmax_metrics_daily,
    sync_gmvmax_metrics_hourly,
)

logger = logging.getLogger("gmv.tasks.gmvmax")


def _close_client(client: Any | None) -> None:
    if client is None:
        return
    try:
        asyncio.run(client.aclose())
    except Exception:  # noqa: BLE001
        logger.warning("gmvmax client close failed", exc_info=True)


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
        .where(TTBGmvMaxCampaign.auth_id == auth_id)
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    return db.execute(stmt).scalars().first()


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
    client = None
    try:
        payload_filters = dict(filters or {})
        if not payload_filters and params and isinstance(params, dict):
            payload_filters = dict(params.get("filters") or {})

        client = build_ttb_client(db, auth_id=auth_id)
        result = asyncio.run(
            sync_gmvmax_campaigns(
                db,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                **payload_filters,
            )
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
        _close_client(client)
        _close_session(db)


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
    campaign_id: str,
    start_date: str,
    end_date: str,
    granularity: str = "HOUR",
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    **extra: Any,
) -> dict:
    """按粒度同步 GMV Max 指标（幂等，底层 upsert）。"""
    db = _db_session()
    client = None
    try:
        row = _find_campaign_row(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
        )
        if not row:
            raise RuntimeError(f"campaign not found: {campaign_id}")

        client = build_ttb_client(db, auth_id=auth_id)

        if str(granularity).upper() == "DAY":
            result = asyncio.run(
                sync_gmvmax_metrics_daily(
                    db,
                    client,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    campaign=row,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        else:
            result = asyncio.run(
                sync_gmvmax_metrics_hourly(
                    db,
                    client,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    campaign=row,
                    start_date=start_date,
                    end_date=end_date,
                )
            )

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
        _close_client(client)
        _close_session(db)


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
    client = None
    try:
        row = _find_campaign_row(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
        )
        if not row:
            raise RuntimeError(f"campaign not found: {campaign_id}")

        client = build_ttb_client(db, auth_id=auth_id)
        result_log = asyncio.run(
            apply_campaign_action(
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
            )
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
        _close_client(client)
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
    client = None
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

        client = build_ttb_client(db, auth_id=auth_id)
        log_row = asyncio.run(
            apply_campaign_action(
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
            )
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
        _close_client(client)
        _close_session(db)


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
