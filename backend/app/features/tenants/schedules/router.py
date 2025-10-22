# app/features/tenants/schedules/router.py
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import require_tenant_admin, SessionUser
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.scheduling import TaskCatalog, Schedule, ScheduleRun
from app.data.models.platform_tasks import IdempotencyKey
from app.services.scheduler_catalog import validate_params_or_raise

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/schedules",
    tags=["Tenant / Schedules"],
)


def _hash_idempotency_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _schedule_scope(action: str, workspace_id: int, schedule_id: Optional[int] = None) -> str:
    scope = f"tenant.schedule.{action}.{workspace_id}"
    if schedule_id is not None:
        scope = f"{scope}.{schedule_id}"
    return scope


def _require_idempotency_key(value: Optional[str]) -> str:
    if not value:
        raise APIError("INVALID_ARGUMENT", "Idempotency-Key header is required", 400)
    if len(value) > 256:
        raise APIError("INVALID_ARGUMENT", "Idempotency-Key too long", 400)
    return value


def _load_idempotency_entry(db: Session, scope: str, key: str) -> IdempotencyKey | None:
    return db.scalar(
        select(IdempotencyKey)
        .where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .with_for_update()
    )


def _persist_idempotency_entry(
    db: Session,
    entry: IdempotencyKey | None,
    *,
    scope: str,
    key: str,
    payload_hash: str,
    response: dict[str, Any],
) -> None:
    if entry:
        entry.payload_hash = payload_hash
        entry.response_json = response
    else:
        db.add(
            IdempotencyKey(
                scope=scope,
                key=key,
                payload_hash=payload_hash,
                response_json=response,
            )
        )

# -------- DTOs --------
class CatalogItem(BaseModel):
    task_name: str
    impl_version: int
    visibility: str
    is_enabled: bool
    rate_limit: Optional[str] = None
    timeout_s: Optional[int] = None
    max_retries: Optional[int] = None
    default_queue: Optional[str] = None
    input_schema_json: Optional[dict[str, Any]] = None

class CatalogResp(BaseModel):
    items: list[CatalogItem]

@router.get("/catalog", response_model=CatalogResp)
def list_catalog(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.execute(
            select(TaskCatalog).where(
                TaskCatalog.is_enabled.is_(True),
                TaskCatalog.visibility == "tenant",
            ).order_by(TaskCatalog.task_name.asc())
        ).scalars().all()
    )
    items = [
        CatalogItem(
            task_name=x.task_name,
            impl_version=int(x.impl_version),
            visibility=x.visibility or "tenant",
            is_enabled=bool(x.is_enabled),
            rate_limit=x.rate_limit,
            timeout_s=x.timeout_s,
            max_retries=x.max_retries,
            default_queue=x.default_queue,
            input_schema_json=x.input_schema_json or None,
        )
        for x in rows
    ]
    return CatalogResp(items=items)


class ScheduleCreateReq(BaseModel):
    task_name: str = Field(max_length=128)
    schedule_type: str = Field(pattern="^(interval|crontab|oneoff)$")
    params_json: Optional[dict[str, Any]] = None
    timezone: Optional[str] = Field(default="UTC", max_length=64)
    interval_seconds: Optional[int] = Field(default=None, ge=60)
    crontab_expr: Optional[str] = Field(default=None, max_length=64)
    oneoff_run_at: Optional[datetime] = None  # ISO8601; 服务端将转 UTC
    misfire_grace_s: Optional[int] = Field(default=300, ge=0)
    jitter_s: Optional[int] = Field(default=0, ge=0)
    enabled: Optional[bool] = True

class ScheduleItem(BaseModel):
    id: int
    workspace_id: int
    task_name: str
    schedule_type: str
    timezone: str | None
    interval_seconds: int | None
    crontab_expr: str | None
    oneoff_run_at: str | None
    enabled: bool
    next_fire_at: str | None
    misfire_grace_s: int | None
    jitter_s: int | None

class ScheduleListResp(BaseModel):
    items: list[ScheduleItem]

def _to_item(x: Schedule) -> ScheduleItem:
    return ScheduleItem(
        id=int(x.id),
        workspace_id=int(x.workspace_id),
        task_name=x.task_name,
        schedule_type=str(x.schedule_type),
        timezone=x.timezone,
        interval_seconds=x.interval_seconds,
        crontab_expr=x.crontab_expr,
        oneoff_run_at=x.oneoff_run_at.isoformat() if x.oneoff_run_at else None,
        enabled=bool(x.enabled),
        next_fire_at=x.next_fire_at.isoformat() if x.next_fire_at else None,
        misfire_grace_s=x.misfire_grace_s,
        jitter_s=x.jitter_s,
    )

@router.get("", response_model=ScheduleListResp)
def list_schedules(
    workspace_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.execute(
            select(Schedule)
            .where(Schedule.workspace_id == int(workspace_id))
            .order_by(Schedule.id.asc())
        ).scalars().all()
    )
    return ScheduleListResp(items=[_to_item(x) for x in rows])

@router.post("", response_model=ScheduleItem)
def create_schedule(
    workspace_id: int,
    req: ScheduleCreateReq,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    idem_key = _require_idempotency_key(idempotency_key)
    scope = _schedule_scope("create", workspace_id)
    payload_hash = _hash_idempotency_payload(req.model_dump())
    entry = _load_idempotency_entry(db, scope, idem_key)
    if entry:
        if entry.payload_hash and entry.payload_hash != payload_hash:
            raise APIError(
                "IDEMPOTENCY_CONFLICT",
                "Payload differs for the same Idempotency-Key",
                409,
                data={
                    "payload_hash": entry.payload_hash,
                    "schedule_id": (entry.response_json or {}).get("id") if entry.response_json else None,
                },
            )
        if entry.response_json:
            return ScheduleItem(**entry.response_json)

    cat = db.scalar(select(TaskCatalog).where(TaskCatalog.task_name == req.task_name, TaskCatalog.is_enabled.is_(True)))
    if not cat or (cat.visibility or "tenant") != "tenant":
        raise APIError("TASK_NOT_ALLOWED", "Task not found or not tenant-visible.", 400)

    # 参数 JSON 校验（根据目录的 schema）
    validate_params_or_raise(cat.input_schema_json or {}, req.params_json or {})

    # 校验触发类型与字段
    if req.schedule_type == "interval":
        if not req.interval_seconds or req.interval_seconds < int(getattr(settings, "SCHEDULE_MIN_INTERVAL_SECONDS", 60)):
            raise APIError("INTERVAL_TOO_SMALL", "interval too small.", 400)
    elif req.schedule_type == "crontab":
        if not req.crontab_expr:
            raise APIError("CRONTAB_REQUIRED", "crontab_expr required.", 400)
    elif req.schedule_type == "oneoff":
        if not req.oneoff_run_at:
            raise APIError("ONEOFF_REQUIRED", "oneoff_run_at required.", 400)

    row = Schedule(
        workspace_id=int(workspace_id),
        task_name=req.task_name,
        schedule_type=req.schedule_type,
        params_json=req.params_json or {},
        timezone=req.timezone or "UTC",
        interval_seconds=req.interval_seconds,
        crontab_expr=req.crontab_expr,
        oneoff_run_at=req.oneoff_run_at,
        misfire_grace_s=req.misfire_grace_s,
        jitter_s=req.jitter_s,
        enabled=bool(req.enabled),
        created_by_user_id=int(me.id),
        updated_by_user_id=int(me.id),
        next_fire_at=None,  # 由 Beat 计算
    )
    db.add(row)
    db.flush()
    item = _to_item(row)
    _persist_idempotency_entry(
        db,
        entry,
        scope=scope,
        key=idem_key,
        payload_hash=payload_hash,
        response=item.model_dump(),
    )
    return item

class SchedulePatchReq(BaseModel):
    params_json: Optional[dict[str, Any]] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    interval_seconds: Optional[int] = Field(default=None, ge=60)
    crontab_expr: Optional[str] = Field(default=None, max_length=64)
    oneoff_run_at: Optional[datetime] = None
    misfire_grace_s: Optional[int] = Field(default=None, ge=0)
    jitter_s: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None

@router.patch("/{schedule_id}", response_model=ScheduleItem)
def patch_schedule(
    workspace_id: int,
    schedule_id: int,
    req: SchedulePatchReq,
    me: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    idem_key = _require_idempotency_key(idempotency_key)
    scope = _schedule_scope("patch", workspace_id, schedule_id)
    payload_hash = _hash_idempotency_payload(req.model_dump(exclude_none=True))
    entry = _load_idempotency_entry(db, scope, idem_key)
    if entry:
        if entry.payload_hash and entry.payload_hash != payload_hash:
            raise APIError(
                "IDEMPOTENCY_CONFLICT",
                "Payload differs for the same Idempotency-Key",
                409,
                data={"payload_hash": entry.payload_hash, "schedule_id": schedule_id},
            )
        if entry.response_json:
            return ScheduleItem(**entry.response_json)

    row = db.get(Schedule, int(schedule_id))
    if not row or row.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "Schedule not found.", 404)

    cat = db.scalar(select(TaskCatalog).where(TaskCatalog.task_name == row.task_name, TaskCatalog.is_enabled.is_(True)))
    if not cat:
        raise APIError("TASK_DISABLED", "Task disabled.", 400)

    if req.params_json is not None:
        validate_params_or_raise(cat.input_schema_json or {}, req.params_json)
        row.params_json = req.params_json

    if req.timezone is not None:
        row.timezone = req.timezone

    if row.schedule_type == "interval" and req.interval_seconds is not None:
        if req.interval_seconds < int(getattr(settings, "SCHEDULE_MIN_INTERVAL_SECONDS", 60)):
            raise APIError("INTERVAL_TOO_SMALL", "interval too small.", 400)
        row.interval_seconds = req.interval_seconds

    if row.schedule_type == "crontab" and req.crontab_expr is not None:
        row.crontab_expr = req.crontab_expr

    if row.schedule_type == "oneoff" and req.oneoff_run_at is not None:
        row.oneoff_run_at = req.oneoff_run_at

    if req.misfire_grace_s is not None:
        row.misfire_grace_s = req.misfire_grace_s
    if req.jitter_s is not None:
        row.jitter_s = req.jitter_s
    if req.enabled is not None:
        row.enabled = bool(req.enabled)

    row.updated_by_user_id = int(me.id)
    db.add(row)
    db.flush()
    item = _to_item(row)
    _persist_idempotency_entry(
        db,
        entry,
        scope=scope,
        key=idem_key,
        payload_hash=payload_hash,
        response=item.model_dump(),
    )
    return item

@router.delete("/{schedule_id}")
def delete_schedule(
    workspace_id: int,
    schedule_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    idem_key = _require_idempotency_key(idempotency_key)
    scope = _schedule_scope("delete", workspace_id, schedule_id)
    payload_hash = _hash_idempotency_payload({"schedule_id": schedule_id})
    entry = _load_idempotency_entry(db, scope, idem_key)
    if entry:
        if entry.payload_hash and entry.payload_hash != payload_hash:
            raise APIError(
                "IDEMPOTENCY_CONFLICT",
                "Payload differs for the same Idempotency-Key",
                409,
                data={"payload_hash": entry.payload_hash, "schedule_id": schedule_id},
            )
        if entry.response_json:
            return entry.response_json

    row = db.get(Schedule, int(schedule_id))
    if not row or row.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "Schedule not found.", 404)
    db.delete(row)
    response = {"ok": True, "schedule_id": schedule_id}
    _persist_idempotency_entry(
        db,
        entry,
        scope=scope,
        key=idem_key,
        payload_hash=payload_hash,
        response=response,
    )
    return response

class RunListItem(BaseModel):
    id: int
    schedule_id: int
    scheduled_for: str
    status: str
    broker_msg_id: str | None
    enqueued_at: str | None
    duration_ms: int | None
    error_code: str | None
    error_message: str | None

class RunListResp(BaseModel):
    items: list[RunListItem]

@router.get("/{schedule_id}/runs", response_model=RunListResp)
def list_runs(
    workspace_id: int,
    schedule_id: int,
    _: SessionUser = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
):
    row = db.get(Schedule, int(schedule_id))
    if not row or row.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "Schedule not found.", 404)

    q = (
        select(ScheduleRun)
        .where(ScheduleRun.schedule_id == int(schedule_id))
        .order_by(ScheduleRun.id.desc())
        .limit(limit)
    )
    items = []
    for r in db.execute(q).scalars().all():
        items.append(
            RunListItem(
                id=int(r.id),
                schedule_id=int(r.schedule_id),
                scheduled_for=r.scheduled_for.isoformat(),
                status=str(r.status),
                broker_msg_id=r.broker_msg_id,
                enqueued_at=r.enqueued_at.isoformat() if r.enqueued_at else None,
                duration_ms=r.duration_ms,
                error_code=r.error_code,
                error_message=r.error_message,
            )
        )
    return RunListResp(items=items)

