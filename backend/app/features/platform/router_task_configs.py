from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_platform_admin
from app.data.db import get_db
from app.services import platform_tasks


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/tasks",
    tags=["Platform / Task Config"],
)


class TaskCatalogStatusSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool
    last_run_at: Optional[str] = None


class TaskCatalogItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_key: str
    title: str
    description: Optional[str] = None
    visibility: str
    supports_whitelist: bool
    supports_blacklist: bool
    supports_tags: bool
    defaults: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    status: TaskCatalogStatusSchema


class TaskCatalogResponse(BaseModel):
    items: list[TaskCatalogItemSchema]


class ScheduleInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    interval_sec: Optional[int] = None
    cron: Optional[str] = None
    timezone: str
    start_at: Optional[str] = None
    end_at: Optional[str] = None


class RateLimitInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_workspace_min_interval_sec: Optional[int] = None
    global_concurrency: Optional[int] = None
    per_workspace_concurrency: Optional[int] = None


class TargetingInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whitelist_workspace_ids: list[int] = Field(default_factory=list)
    blacklist_workspace_ids: list[int] = Field(default_factory=list)
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)


class TaskMetadataInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class TaskConfigResponse(BaseModel):
    task_key: str
    is_enabled: bool
    schedule: ScheduleInfo
    rate_limit: RateLimitInfo
    targeting: TargetingInfo
    input: dict[str, Any] = Field(default_factory=dict)
    metadata: TaskMetadataInfo


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(pattern="^(interval|cron)$")
    interval_sec: Optional[int] = Field(default=None, ge=1)
    cron: Optional[str] = None
    timezone: str = Field(default="UTC", max_length=64)
    start_at: Optional[str] = None
    end_at: Optional[str] = None


class RateLimitUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_workspace_min_interval_sec: Optional[int] = Field(default=None, ge=1)
    global_concurrency: Optional[int] = Field(default=None, ge=1)
    per_workspace_concurrency: Optional[int] = Field(default=None, ge=1)


class TargetingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whitelist_workspace_ids: list[int] = Field(default_factory=list)
    blacklist_workspace_ids: list[int] = Field(default_factory=list)
    include_tags: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)


class TaskConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool
    schedule: ScheduleUpdate
    rate_limit: RateLimitUpdate
    targeting: TargetingUpdate
    input: dict[str, Any] = Field(default_factory=dict)


class TaskConfigUpdateResponse(BaseModel):
    ok: bool
    version: int
    target_count: Optional[int] = None
    dry_run: Optional[bool] = None
    target_preview: Optional[list[int]] = None
    summary: Optional[dict[str, Any]] = None


class TaskLastRunResponse(BaseModel):
    task_key: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_sec: Optional[int] = None
    summary: Optional[str] = None
    stats: dict[str, Any] = Field(default_factory=dict)
    workspace_samples: list[dict[str, Any]] = Field(default_factory=list)


class TaskRunItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_sec: Optional[int] = None
    stats: dict[str, Any] = Field(default_factory=dict)


class TaskRunsResponse(BaseModel):
    items: list[TaskRunItem]
    next_cursor: Optional[str] = None


class ScheduleApplyRequest(BaseModel):
    dry_run: bool = False


class ScheduleApplyResponse(BaseModel):
    ok: bool
    summary: dict[str, Any]
    violations: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: Optional[bool] = None


@router.get("/catalog", response_model=TaskCatalogResponse)
def list_task_catalog(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TaskCatalogResponse:
    items = [TaskCatalogItemSchema(**item) for item in platform_tasks.get_catalog(db)]
    return TaskCatalogResponse(items=items)


@router.get("/{task_key}/config", response_model=TaskConfigResponse)
def read_task_config(
    task_key: str,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TaskConfigResponse:
    catalog, config = platform_tasks.get_task_config(db, task_key)
    data = platform_tasks.serialize_task_config(catalog, config)
    return TaskConfigResponse(**data)


@router.put("/{task_key}/config", response_model=TaskConfigUpdateResponse)
def update_task_config(
    task_key: str,
    req: TaskConfigUpdateRequest,
    dry_run: bool = Query(default=False),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TaskConfigUpdateResponse:
    result = platform_tasks.update_task_config(
        db,
        task_key,
        req.model_dump(),
        actor_email=me.email or me.username,
        actor_user_id=me.id,
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )
    return TaskConfigUpdateResponse(**result)


@router.get("/{task_key}/last_run", response_model=TaskLastRunResponse)
def get_last_run(
    task_key: str,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TaskLastRunResponse:
    data = platform_tasks.get_last_run(db, task_key)
    return TaskLastRunResponse(**data)


@router.get("/{task_key}/runs", response_model=TaskRunsResponse)
def list_runs(
    task_key: str,
    status: Optional[str] = Query(default=None),
    workspace_id: Optional[int] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> TaskRunsResponse:
    data = platform_tasks.list_runs(
        db,
        task_key,
        status=status,
        workspace_id=workspace_id,
        limit=limit,
        cursor=cursor,
    )
    items = [TaskRunItem(**item) for item in data["items"]]
    return TaskRunsResponse(items=items, next_cursor=data.get("next_cursor"))


@router.post("/{task_key}/schedules/apply", response_model=ScheduleApplyResponse)
def apply_schedules(
    task_key: str,
    req: ScheduleApplyRequest,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> ScheduleApplyResponse:
    data = platform_tasks.apply_schedule_snapshot(db, task_key, dry_run=req.dry_run)
    return ScheduleApplyResponse(**data)

