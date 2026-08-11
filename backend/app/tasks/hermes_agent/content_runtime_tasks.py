from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentRuntimeEvent,
)
from app.services.hermes_agent.content_runtime import (
    reconcile_content_execution_ledger,
)


logger = get_task_logger(__name__)
HERMES_QUEUE = str(
    getattr(settings, "HERMES_AGENT_TASK_QUEUE", "gmv.tasks.hermes_agent")
)


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except (TypeError, ValueError):
        return None


def _project_has_recent_waiter(project: HermesContentFactoryProject) -> bool:
    state = dict(project.state_json or {})
    waiter_id = str(state.get("ai_video_wait_task_id") or "").strip()
    heartbeat = _parse_time(state.get("ai_video_wait_heartbeat_at"))
    return bool(
        waiter_id
        and heartbeat is not None
        and heartbeat
        >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
    )


def _runtime_event_retry_delay(attempts: int) -> timedelta:
    # Outbox recovery must remain bounded and calm during a broker/worker
    # incident.  The event itself is durable, so there is no value in a hot
    # retry loop.
    seconds = min(300, max(5, 5 * (2 ** min(max(int(attempts), 0), 6))))
    return timedelta(seconds=seconds)


def _finalize_claimed_runtime_events(
    event_ids: list[int],
    *,
    status: str,
    error: str | None = None,
) -> None:
    if not event_ids:
        return
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = (
            db.query(HermesContentRuntimeEvent)
            .filter(HermesContentRuntimeEvent.id.in_(tuple(event_ids)))
            .with_for_update()
            .all()
        )
        for row in rows:
            if str(row.status or "") != "processing":
                continue
            row.updated_at = now
            if status == "processed":
                row.status = "processed"
                row.processed_at = now
                row.last_error = None
            else:
                row.status = "retry"
                row.processed_at = None
                row.last_error = str(error or "runtime event processing failed")[:4000]
                row.available_at = now + _runtime_event_retry_delay(
                    int(row.attempts or 0)
                )
            db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Unable to finalize Content Factory runtime events",
            extra={"event_ids": event_ids, "status": status},
        )
        raise
    finally:
        db.close()


@celery_app.task(
    name="hermes_content_factory.process_runtime_events",
    queue=HERMES_QUEUE,
)
def process_content_runtime_events(*, limit: int = 200) -> dict[str, Any]:
    """Consume transactional media events and wake one project waiter.

    The outbox is the normal transition trigger.  The periodic reconciliation
    task below only repairs a missed broker wakeup or an expired lease.
    """

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stale_processing_cutoff = now - timedelta(minutes=5)
        (
            db.query(HermesContentRuntimeEvent)
            .filter(
                HermesContentRuntimeEvent.status == "processing",
                HermesContentRuntimeEvent.updated_at < stale_processing_cutoff,
            )
            .update(
                {
                    HermesContentRuntimeEvent.status: "retry",
                    HermesContentRuntimeEvent.available_at: now,
                    HermesContentRuntimeEvent.updated_at: now,
                    HermesContentRuntimeEvent.last_error: (
                        "Recovered an abandoned runtime-event processing lease."
                    ),
                },
                synchronize_session=False,
            )
        )
        rows = (
            db.query(HermesContentRuntimeEvent)
            .filter(
                HermesContentRuntimeEvent.status.in_(("pending", "retry")),
                HermesContentRuntimeEvent.available_at <= now,
            )
            .order_by(
                HermesContentRuntimeEvent.available_at.asc(),
                HermesContentRuntimeEvent.id.asc(),
            )
            .limit(max(1, min(int(limit), 1000)))
            .with_for_update(skip_locked=True)
            .all()
        )
        if not rows:
            db.commit()
            return {"processed": 0, "projects": [], "scheduled": []}
        event_ids_by_project: dict[int, list[int]] = {}
        event_origins_by_project: dict[int, set[str]] = {}
        for row in rows:
            row.status = "processing"
            row.attempts = int(row.attempts or 0) + 1
            row.processed_at = None
            row.last_error = None
            # Do not depend on MySQL's optional ON UPDATE DDL for lease
            # ownership.  SQLite-backed tests and older production schemas
            # must observe the same explicit claim timestamp.
            row.updated_at = now
            db.add(row)
            event_ids_by_project.setdefault(int(row.project_id), []).append(
                int(row.id)
            )
            origin = str(dict(row.payload_json or {}).get("event_origin") or "").strip()
            event_origins_by_project.setdefault(int(row.project_id), set()).add(
                origin
            )
        project_ids = sorted(event_ids_by_project)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Content Factory runtime outbox consumption failed")
        raise
    finally:
        db.close()

    scheduled: list[int] = []
    skipped: list[int] = []
    retried: list[int] = []
    for project_id in project_ids:
        event_ids = list(event_ids_by_project.get(int(project_id)) or [])
        project_db = SessionLocal()
        try:
            project = (
                project_db.query(HermesContentFactoryProject)
                .filter(HermesContentFactoryProject.id == int(project_id))
                .with_for_update()
                .one_or_none()
            )
            if project is None:
                project_db.rollback()
                _finalize_claimed_runtime_events(
                    event_ids,
                    status="processed",
                )
                continue
            status = str(project.status or "").strip().lower()
            state = dict(project.state_json or {})
            pause_reason = str(state.get("pause_reason_code") or "").strip().lower()
            manual_pause = bool(
                dict(project.config_json or {}).get("manual_paused", False)
                or pause_reason == "manual"
            )
            bounded_operator_pause = pause_reason == "operator_required"
            legacy_backfill_pause = bool(
                status == "paused"
                and pause_reason
                and not any(event_origins_by_project.get(int(project_id), set()))
            )
            if (
                status in {"complete", "completed", "deleted", "cancelled"}
                or manual_pause
                or bounded_operator_pause
                or legacy_backfill_pause
            ):
                # The ledger event remains useful evidence, but a bounded
                # quality decision is not a scheduling lease.  Draining media
                # is reconciled into the ledger without waking a waiter that
                # could otherwise turn an explicit operator boundary back
                # into paid generation.
                project_db.rollback()
                skipped.append(int(project_id))
                _finalize_claimed_runtime_events(
                    event_ids,
                    status="processed",
                )
                continue
            if _project_has_recent_waiter(project):
                project_db.rollback()
                skipped.append(int(project_id))
                _finalize_claimed_runtime_events(
                    event_ids,
                    status="processed",
                )
                continue
            from app.tasks.hermes_agent.content_factory_tasks import (
                _schedule_video_wait,
            )

            _schedule_video_wait(
                project_db,
                project,
                countdown=0,
                reason="execution-ledger segment event",
            )
            scheduled.append(int(project_id))
            _finalize_claimed_runtime_events(
                event_ids,
                status="processed",
            )
        except Exception as exc:
            project_db.rollback()
            retried.append(int(project_id))
            _finalize_claimed_runtime_events(
                event_ids,
                status="retry",
                error=str(exc),
            )
            logger.exception(
                "Unable to wake Content Factory project from runtime event",
                extra={"project_id": int(project_id)},
            )
        finally:
            project_db.close()
    if len(rows) >= max(1, min(int(limit), 1000)):
        # A single durable follow-up drains the next page. Concurrent
        # consumers still use SKIP LOCKED, so this cannot double-process a
        # claimed row.
        process_content_runtime_events.apply_async(
            kwargs={"limit": max(1, min(int(limit), 1000))},
            queue=HERMES_QUEUE,
        )
    return {
        "processed": len(rows),
        "projects": project_ids,
        "scheduled": scheduled,
        "skipped": skipped,
        "retried": retried,
    }


@celery_app.task(
    name="hermes_content_factory.reconcile_runtime_ledger",
    queue=HERMES_QUEUE,
)
def reconcile_content_runtime_ledger(*, limit: int = 1000) -> dict[str, Any]:
    """Backstop for missed ORM events; never acts as the normal scheduler."""

    db = SessionLocal()
    try:
        result = reconcile_content_execution_ledger(db, limit=limit)
        # Reconciliation publishes exactly one consumer after commit. The
        # generic after_commit hook is for provider-result transactions.
        db.info.pop("hermes_runtime_events_pending", None)
        db.commit()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        ready_event_count = (
            db.query(HermesContentRuntimeEvent)
            .filter(
                HermesContentRuntimeEvent.status.in_(("pending", "retry")),
                HermesContentRuntimeEvent.available_at <= now,
            )
            .count()
        )
        result["pending_events"] = int(ready_event_count)
    except Exception:
        db.rollback()
        logger.exception("Content Factory runtime ledger reconciliation failed")
        raise
    finally:
        db.close()
    if int(result.get("pending_events") or 0) > 0:
        process_content_runtime_events.apply_async(
            kwargs={
                "limit": max(
                    200,
                    min(1000, int(result.get("pending_events") or 0)),
                )
            },
            queue=HERMES_QUEUE,
        )
    return result


__all__ = [
    "process_content_runtime_events",
    "reconcile_content_runtime_ledger",
]
