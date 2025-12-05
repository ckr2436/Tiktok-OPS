from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.celery_app import celery_app
from app.gmvmax.domain.monitoring_strategy import GmvMaxMonitoringStrategyRepository
from app.gmvmax.services.sync_service import GmvMaxSyncService

logger = logging.getLogger("gmv.tasks.gmvmax.strategy")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@celery_app.task(name="gmvmax.sync.run_scheduler")
def run_gmvmax_sync_scheduler() -> dict[str, Any]:
    now = _now_utc()
    repo = GmvMaxMonitoringStrategyRepository()
    strategies = repo.get_due_strategies(now)

    dispatched = 0
    for strategy in strategies:
        try:
            repo.mark_started(strategy.id, now)
        except Exception:  # noqa: BLE001
            logger.warning(
                "gmvmax sync scheduler: failed to mark started",
                exc_info=True,
                extra={"strategy_id": strategy.id},
            )
            continue

        celery_app.send_task(
            "gmvmax.sync.run_for_strategy",
            kwargs={"strategy_id": strategy.id},
            queue="gmvmax_sync",
        )
        dispatched += 1

    logger.info(
        "gmvmax sync scheduler completed",
        extra={"timestamp": now.isoformat(), "strategies": len(strategies), "dispatched": dispatched},
    )
    return {"timestamp": now.isoformat(), "strategies": len(strategies), "dispatched": dispatched}


@celery_app.task(
    name="gmvmax.sync.run_for_strategy",
    max_retries=3,
    default_retry_delay=60,
)
def run_gmvmax_sync_for_strategy(strategy_id: int) -> dict[str, Any]:
    now = _now_utc()
    repo = GmvMaxMonitoringStrategyRepository()
    service = GmvMaxSyncService()

    strategy = repo.get_by_id(int(strategy_id))
    if not strategy:
        logger.info("gmvmax sync skipped: strategy not found", extra={"strategy_id": strategy_id})
        return {"skipped": True, "reason": "not_found"}
    if not strategy.enabled:
        logger.info(
            "gmvmax sync skipped: strategy disabled", extra={"strategy_id": strategy_id, "level": strategy.level}
        )
        return {"skipped": True, "reason": "disabled"}

    logger.info(
        "gmvmax sync start",
        extra={"strategy_id": strategy.id, "workspace_id": strategy.workspace_id, "level": strategy.level},
    )
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
        raise
