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


@celery_app.task(
    name="hermes_agent.reconcile_yt_dlp_cookie_keepalives",
    queue=settings.HERMES_MAINTENANCE_TASK_QUEUE,
)
def reconcile_yt_dlp_cookie_keepalives() -> dict[str, int]:
    from app.services.yt_dlp_browser_onboarding import reconcile_cookie_keepalives

    db = SessionLocal()
    try:
        result = reconcile_cookie_keepalives(db)
        db.commit()
        return result
    except Exception:
        db.rollback()
        logger.exception("yt-dlp cookie keepalive reconciliation failed")
        raise
    finally:
        db.close()


@celery_app.task(
    name="hermes_agent.reconcile_flow_auto_reauth",
    queue=settings.HERMES_MAINTENANCE_TASK_QUEUE,
)
def reconcile_flow_auto_reauth() -> dict[str, int]:
    """Refresh expired Flow grants through each account's fixed Profile.

    Flow2API remains authoritative for server-side token health.  This task
    converts its explicit GRANT_EXPIRED state into one bounded renderer visit
    plus credential capture per Windows device. The immutable Profile and proxy
    remain account-scoped throughout the transaction.
    """
    from app.data.models.hermes_agent import HermesBrowserBridge
    from app.services.flow2api_admin import Flow2ApiAdminClient
    from app.services.flow_browser_onboarding import (
        is_flow_account_slot,
        reconcile_flow_browser_bindings_from_upstream,
    )

    db = SessionLocal()
    try:
        upstream = asyncio.run(Flow2ApiAdminClient().request("GET", "/api/tokens"))
        upstream_tokens = (
            [item for item in upstream if isinstance(item, dict)]
            if isinstance(upstream, list)
            else []
        )
        owners = {
            (int(row.workspace_id), int(row.user_id))
            for row in db.query(HermesBrowserBridge).filter(
                HermesBrowserBridge.status != "retired"
            ).all()
            if is_flow_account_slot(row)
        }
        changed = 0
        for workspace_id, user_id in sorted(owners):
            changed += reconcile_flow_browser_bindings_from_upstream(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                upstream_tokens=upstream_tokens,
            )
        db.commit()
        return {
            "owners": len(owners),
            "accounts": len(upstream_tokens),
            "changed": changed,
        }
    except Exception:
        db.rollback()
        logger.exception("Flow automatic reauthorization reconciliation failed")
        raise
    finally:
        db.close()
