from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.celery_app import celery_app
from app.data.db import SessionLocal
from app.gmvmax.domain.monitoring_strategy import GmvMaxMonitoringStrategyRepository
from app.gmvmax.services.sync_execution_lock import (
    acquire_account_sync_fence,
    build_account_sync_lock,
    release_account_sync_fence,
)
from app.gmvmax.services.sync_service import GmvMaxSyncService
from app.services.ttb_api import (
    TTBHttpError,
    TTBRateLimitBudgetError,
    ttb_retry_countdown,
)
from app.services.scheduler_schema_utils import validate_params_or_raise
from app.services.scheduler_task_registry import get_task_config

logger = logging.getLogger("gmv.tasks.gmvmax.strategy")


_MAX_STRATEGIES_PER_TICK = int(os.getenv("GMVMAX_SCHEDULER_MAX_STRATEGIES", "100"))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _validate_strategy_params(strategy: Any, config: dict[str, Any] | None) -> None:
    schema = strategy.input_schema_json or {}
    if not schema:
        schema = dict(config.get("input_schema") or {}) if config else {}
    params = strategy.params_json if hasattr(strategy, "params_json") else None
    if params is None:
        params = {}
    validate_params_or_raise(schema, params)


def _build_celery_kwargs(strategy: Any, config: dict[str, Any]) -> dict[str, Any]:
    kwargs = dict(strategy.params_json or {})
    if "workspace_id" not in kwargs and getattr(strategy, "workspace_id", None) is not None:
        kwargs["workspace_id"] = strategy.workspace_id
    if "auth_id" not in kwargs and getattr(strategy, "auth_id", None) is not None:
        kwargs["auth_id"] = strategy.auth_id
    if "advertiser_id" not in kwargs and getattr(strategy, "advertiser_id", None) is not None:
        kwargs["advertiser_id"] = strategy.advertiser_id
    if "store_id" not in kwargs and getattr(strategy, "store_id", None) is not None:
        kwargs["store_id"] = strategy.store_id

    if "scope" not in kwargs and config.get("default_scope"):
        kwargs["scope"] = config["default_scope"]
    return kwargs


@celery_app.task(name="gmvmax.sync.run_scheduler", ignore_result=True)
def run_gmvmax_sync_scheduler() -> dict[str, Any]:
    now = _now_utc()
    repo = GmvMaxMonitoringStrategyRepository()
    limit = _MAX_STRATEGIES_PER_TICK if _MAX_STRATEGIES_PER_TICK > 0 else None
    strategies = repo.get_due_strategies(now, limit=limit)

    dispatched = 0
    for strategy in strategies:
        category = strategy.category or "GMVMAX"
        task_name = strategy.task_name or "gmvmax.strategy"
        config = get_task_config(category, task_name)
        if not config:
            repo.mark_error(strategy.id, now, f"unknown task: {category}/{task_name}")
            logger.error(
                "strategy scheduler: unknown task",
                extra={"strategy_id": strategy.id, "category": category, "task_name": task_name},
            )
            continue

        try:
            _validate_strategy_params(strategy, config)
        except Exception as exc:  # noqa: BLE001
            repo.mark_error(strategy.id, now, str(exc))
            logger.error(
                "strategy scheduler: invalid params",
                exc_info=True,
                extra={"strategy_id": strategy.id, "category": category, "task_name": task_name},
            )
            continue

        if config.get("kind") == "gmvmax_strategy":
            try:
                repo.mark_started(strategy.id, now)
                celery_app.send_task(
                    "gmvmax.sync.run_for_strategy",
                    kwargs={"strategy_id": strategy.id},
                    queue="gmvmax_sync",
                )
                dispatched += 1
            except Exception:
                repo.mark_error(strategy.id, now, "dispatch_failed")
                logger.exception(
                    "gmvmax sync scheduler: dispatch failed",
                    extra={"strategy_id": strategy.id, "category": category},
                )
        elif config.get("kind") == "celery_task":
            try:
                repo.mark_started(strategy.id, now)
                kwargs = _build_celery_kwargs(strategy, config)
                celery_app.send_task(
                    config["celery_task"],
                    kwargs=kwargs,
                    queue=config.get("queue"),
                )
                repo.mark_success(strategy.id, now)
                dispatched += 1
            except Exception as exc:  # noqa: BLE001
                repo.mark_error(strategy.id, now, str(exc))
                logger.exception(
                    "strategy scheduler: dispatch failed",
                    extra={"strategy_id": strategy.id, "category": category, "task_name": task_name},
                )
        else:
            repo.mark_error(strategy.id, now, f"unsupported kind: {config.get('kind')}")
            logger.error(
                "strategy scheduler: unsupported kind",
                extra={"strategy_id": strategy.id, "category": category, "task_name": task_name},
            )

    logger.info(
        "gmvmax sync scheduler completed",
        extra={"timestamp": now.isoformat(), "strategies": len(strategies), "dispatched": dispatched},
    )
    return {"timestamp": now.isoformat(), "strategies": len(strategies), "dispatched": dispatched}


@celery_app.task(
    name="gmvmax.sync.run_for_strategy",
    bind=True,
    max_retries=10,
    default_retry_delay=60,
)
def run_gmvmax_sync_for_strategy(self, strategy_id: int) -> dict[str, Any]:
    now = _now_utc()
    repo = GmvMaxMonitoringStrategyRepository()

    strategy = repo.get_by_id(int(strategy_id))
    if not strategy:
        logger.info("gmvmax sync skipped: strategy not found", extra={"strategy_id": strategy_id})
        return {"skipped": True, "reason": "not_found"}
    if not strategy.enabled:
        logger.info(
            "gmvmax sync skipped: strategy disabled", extra={"strategy_id": strategy_id, "level": strategy.level}
        )
        return {"skipped": True, "reason": "disabled"}
    if strategy.auth_id is None:
        reason = "missing exact auth_id scope"
        repo.mark_error(strategy.id, now, reason)
        logger.error(
            "gmvmax sync skipped: strategy scope is incomplete",
            extra={
                "strategy_id": strategy.id,
                "workspace_id": strategy.workspace_id,
                "level": strategy.level,
            },
        )
        return {
            "skipped": True,
            "reason": "missing_auth_scope",
            "strategy_id": strategy.id,
        }

    logger.info(
        "gmvmax sync start",
        extra={"strategy_id": strategy.id, "workspace_id": strategy.workspace_id, "level": strategy.level},
    )
    lock = build_account_sync_lock(
        workspace_id=int(strategy.workspace_id),
        auth_id=int(strategy.auth_id),
        # A retry/redelivery keeps the Celery request id.  Add a fresh nonce
        # for every execution so a stale delivery can never verify or release
        # the newer delivery's Redis lock.
        owner_token=f"{self.request.id or f'strategy:{strategy.id}'}:{uuid4()}",
    )
    if not lock.acquire(timeout=1.0, retry_interval=0.1):
        # The inflight account sync may cover a different level or report
        # window. Retry this exact strategy instead of marking it successful.
        logger.warning(
            "gmvmax strategy deferred: account sync already running",
            extra={
                "strategy_id": strategy.id,
                "workspace_id": strategy.workspace_id,
                "auth_id": strategy.auth_id,
                "level": strategy.level,
            },
        )
        raise self.retry(
            exc=RuntimeError("GMV Max account sync already running"),
            countdown=min(
                120,
                15 * (int(self.request.retries or 0) + 1),
            ),
        )
    fence = None
    with SessionLocal() as fence_db:
        try:
            fence = acquire_account_sync_fence(
                fence_db,
                redis_lock=lock,
                workspace_id=int(strategy.workspace_id),
                auth_id=int(strategy.auth_id),
                owner_token=str(lock.owner_token),
            )
            if fence is None:
                fence_db.rollback()
            else:
                fence_db.commit()
        except Exception:
            fence_db.rollback()
            lock.release()
            raise
    if fence is None:
        lock.release()
        raise self.retry(
            exc=RuntimeError("GMV Max durable account sync lease is busy"),
            countdown=min(
                120,
                15 * (int(self.request.retries or 0) + 1),
            ),
        )
    service = GmvMaxSyncService(execution_guard=fence.assert_current)
    try:
        service.sync_strategy(strategy, now)
        repo.mark_success(strategy.id, now)
        logger.info(
            "gmvmax sync success",
            extra={"strategy_id": strategy.id, "level": strategy.level, "timestamp": now.isoformat()},
        )
        return {"ok": True, "strategy_id": strategy.id}
    except Exception as exc:  # noqa: BLE001
        repo.mark_error(strategy.id, now, str(exc))
        logger.exception("gmvmax sync failed", extra={"strategy_id": strategy.id, "level": strategy.level})
        countdown = (
            ttb_retry_countdown(exc)
            if isinstance(exc, (TTBRateLimitBudgetError, TTBHttpError))
            else 60
        )
        raise self.retry(exc=exc, countdown=countdown)
    finally:
        with SessionLocal() as release_db:
            try:
                release_account_sync_fence(release_db, fence=fence)
                release_db.commit()
            except Exception:  # noqa: BLE001
                release_db.rollback()
                logger.exception(
                    "gmvmax strategy durable fence release failed",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                        "auth_id": strategy.auth_id,
                    },
                )
        lock.release()
