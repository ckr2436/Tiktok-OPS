from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.models.scheduling import Schedule, ScheduleRun
from app.services.audit import log_event
from app.services.provider_registry import load_builtin_providers, provider_registry

load_builtin_providers()

SYNC_TASKS: Dict[str, str] = {
    "bc": "ttb.sync.bc",
    "advertisers": "ttb.sync.advertisers",
    "shops": "ttb.sync.shops",
    "products": "ttb.sync.products",
    "all": "ttb.sync.all",
}


@dataclass
class DispatchResult:
    run: ScheduleRun
    task_id: Optional[str]
    status: str
    idempotent: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _create_schedule_and_run(
    db: Session,
    *,
    workspace_id: int,
    task_name: str,
    params_json: Dict[str, Any],
    requested_stats: Dict[str, Any],
    actor_user_id: int,
    idempotency_key: Optional[str],
) -> ScheduleRun:
    schedule = Schedule(
        workspace_id=int(workspace_id),
        task_name=task_name,
        schedule_type="oneoff",
        params_json=params_json,
        timezone="UTC",
        enabled=False,
        oneoff_run_at=_now(),
        created_by_user_id=int(actor_user_id),
        updated_by_user_id=int(actor_user_id),
    )
    db.add(schedule)
    db.flush()

    run = ScheduleRun(
        schedule_id=int(schedule.id),
        workspace_id=int(workspace_id),
        scheduled_for=_now(),
        enqueued_at=_now(),
        status="enqueued",
        idempotency_key=idempotency_key or uuid4().hex,
        stats_json={"requested": requested_stats, "errors": []},
    )
    db.add(run)
    db.flush()
    return run


def _find_existing_run(
    db: Session,
    *,
    workspace_id: int,
    task_name: str,
    idempotency_key: str,
    max_age: timedelta = timedelta(hours=24),
) -> ScheduleRun | None:
    threshold = _now() - max_age
    query = (
        db.query(ScheduleRun)
        .join(Schedule, Schedule.id == ScheduleRun.schedule_id)
        .filter(
            ScheduleRun.workspace_id == int(workspace_id),
            ScheduleRun.idempotency_key == idempotency_key,
            Schedule.task_name == task_name,
            ScheduleRun.created_at >= threshold,
        )
        .order_by(ScheduleRun.id.desc())
    )
    return query.first()


def dispatch_sync(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    scope: str,
    params: Dict[str, Any],
    actor_user_id: int,
    actor_workspace_id: int,
    actor_ip: Optional[str],
    idempotency_key: Optional[str],
) -> DispatchResult:
    if scope not in SYNC_TASKS:
        raise ValueError(f"unsupported scope: {scope}")

    task_name = SYNC_TASKS[scope]

    handler = provider_registry.get(provider)
    normalized_options = handler.validate_options(scope=scope, options=params)
    filtered_params = {k: v for k, v in normalized_options.items() if v is not None}

    if idempotency_key:
        existing = _find_existing_run(
            db,
            workspace_id=workspace_id,
            task_name=task_name,
            idempotency_key=idempotency_key,
        )
        if existing:
            log_event(
                db,
                action="ttb.sync.dispatch.idempotent",
                resource_type="ttb_sync",
                resource_id=int(existing.id),
                actor_user_id=actor_user_id,
                actor_workspace_id=actor_workspace_id,
                workspace_id=int(workspace_id),
                actor_ip=actor_ip,
                details={
                    "auth_id": int(auth_id),
                    "scope": scope,
                    "task_name": task_name,
                    "idempotency_key": idempotency_key,
                    "status": existing.status,
                },
            )
            db.commit()
            return DispatchResult(
                run=existing,
                task_id=str(existing.broker_msg_id) if existing.broker_msg_id else None,
                status=existing.status,
                idempotent=True,
            )

    params_json = {
        "provider": provider,
        "auth_id": int(auth_id),
        "scope": scope,
        "options": filtered_params,
    }
    actor_workspace_val = int(actor_workspace_id) if actor_workspace_id is not None else None

    requested_stats = {
        "provider": provider,
        "auth_id": int(auth_id),
        "scope": scope,
        "options": filtered_params,
        "actor": {
            "user_id": int(actor_user_id),
            "workspace_id": actor_workspace_val,
            "ip": actor_ip,
        },
    }
    run = _create_schedule_and_run(
        db,
        workspace_id=workspace_id,
        task_name=task_name,
        params_json=params_json,
        requested_stats=requested_stats,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
    )

    requested_stats["idempotency_key"] = run.idempotency_key
    run.stats_json = {**(run.stats_json or {}), "requested": requested_stats, "errors": []}
    db.add(run)

    run_id = int(run.id)
    schedule_id = int(run.schedule_id)
    db.commit()

    envelope = {
        "envelope_version": 1,
        "provider": provider,
        "scope": scope,
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "options": filtered_params,
        "meta": {
            "run_id": run_id,
            "schedule_id": schedule_id,
            "idempotency_key": run.idempotency_key,
        },
    }

    payload = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "scope": scope,
        "params": {"envelope": envelope},
        "run_id": run_id,
        "idempotency_key": run.idempotency_key,
    }
    task = celery_app.send_task(task_name, kwargs=payload, queue="gmv.tasks.events")

    persisted_run = db.get(ScheduleRun, run_id)
    if persisted_run:
        persisted_run.broker_msg_id = str(task.id)
        db.add(persisted_run)
    log_event(
        db,
        action="ttb.sync.dispatch",
        resource_type="ttb_sync",
        resource_id=run_id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_val,
        workspace_id=int(workspace_id),
        actor_ip=actor_ip,
        details={
            "auth_id": int(auth_id),
            "scope": scope,
            "task_name": task_name,
            "schedule_id": schedule_id,
            "task_id": str(task.id),
            "params": filtered_params,
            "idempotency_key": run.idempotency_key,
            "provider": provider,
            "run_id": run_id,
        },
    )
    db.commit()
    return DispatchResult(
        run=run,
        task_id=str(task.id),
        status="enqueued",
        idempotent=False,
    )
