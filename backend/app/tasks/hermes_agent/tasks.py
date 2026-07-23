from __future__ import annotations

import asyncio
from typing import Any

from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.services.hermes_agent.service import execute_run

logger = get_task_logger(__name__)


@celery_app.task(
    name="hermes_agent.run",
    bind=True,
    queue=settings.HERMES_AGENT_TASK_QUEUE,
    max_retries=2,
    default_retry_delay=15,
)
def run_hermes_agent(self, *, workspace_id: int, run_id: str, **_: Any) -> dict[str, Any]:
    """Execute a persisted Hermes Agent run in the background."""
    db = SessionLocal()
    try:
        run = asyncio.run(execute_run(db, workspace_id=int(workspace_id), run_id=run_id))
        db.commit()
        return {
            "run_id": run.run_id,
            "workspace_id": int(run.workspace_id),
            "status": run.status,
            "error_code": run.error_code,
            "hermes_response_id": run.hermes_response_id,
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "Hermes Agent run failed",
            extra={"workspace_id": workspace_id, "run_id": run_id},
        )
        retries = int(getattr(self.request, "retries", 0) or 0)
        if retries < 2:
            raise self.retry(exc=exc)
        raise
    finally:
        db.close()
