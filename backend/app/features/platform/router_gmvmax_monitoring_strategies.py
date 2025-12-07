"""Admin APIs for GMV Max monitoring strategies (platform domain)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.gmv_restructured import GmvMonitoringStrategy, PromotionTypeEnum
from app.services.scheduler_schema_utils import validate_params_or_raise
from app.services.scheduler_task_registry import SCHEDULED_TASKS

router = APIRouter(
    prefix="/api/v1/admin/platform/gmvmax/monitoring-strategies",
    tags=["Admin / Platform GMVMax Monitoring"],
)


class MonitoringStrategyLevel(str, Enum):
    OVERVIEW_DAILY = "OVERVIEW_DAILY"
    CAMPAIGN_DAILY = "CAMPAIGN_DAILY"
    CAMPAIGN_HOURLY = "CAMPAIGN_HOURLY"
    PRODUCT_DAILY = "PRODUCT_DAILY"
    PRODUCT_HOURLY = "PRODUCT_HOURLY"
    CREATIVE_10MIN = "CREATIVE_10MIN"
    LIVESTREAM_DAILY = "LIVESTREAM_DAILY"
    LIVESTREAM_HOURLY = "LIVESTREAM_HOURLY"
    DURATION_DAILY = "DURATION_DAILY"
    DURATION_HOURLY = "DURATION_HOURLY"


class MonitoringStrategyBase(BaseModel):
    workspace_id: int
    auth_id: int | None = None
    advertiser_id: str | None = None
    store_id: str | None = None
    promotion_type: PromotionTypeEnum | None = None
    level: MonitoringStrategyLevel | None = None
    interval_minutes: int = Field(gt=0)
    max_campaigns_per_run: int | None = None
    enabled: bool = True
    category: str | None = Field(default=None, max_length=32)
    task_name: str | None = Field(default=None, max_length=128)
    params_json: dict[str, Any] | None = Field(default=None)
    input_schema_json: dict[str, Any] | None = Field(default=None)


class MonitoringStrategyCreate(MonitoringStrategyBase):
    pass


class MonitoringStrategyUpdate(BaseModel):
    promotion_type: PromotionTypeEnum | None = None
    level: MonitoringStrategyLevel | None = None
    interval_minutes: int | None = Field(default=None, gt=0)
    max_campaigns_per_run: int | None = None
    enabled: bool | None = None
    category: str | None = Field(default=None, max_length=32)
    task_name: str | None = Field(default=None, max_length=128)
    params_json: dict[str, Any] | None = Field(default=None)
    input_schema_json: dict[str, Any] | None = Field(default=None)


class MonitoringStrategyOut(MonitoringStrategyBase):
    id: int
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class MonitoringStrategyPage(BaseModel):
    items: list[MonitoringStrategyOut]
    total: int


_DEF_LIMIT = 50
_MAX_LIMIT = 200
_DEFAULT_CATEGORY = "GMVMAX"
_DEFAULT_TASK_NAME = "gmvmax.strategy"


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_out(row: GmvMonitoringStrategy) -> MonitoringStrategyOut:
    category = row.category or _DEFAULT_CATEGORY
    task_name = row.task_name or _DEFAULT_TASK_NAME
    return MonitoringStrategyOut(
        id=int(row.id),
        workspace_id=int(row.workspace_id),
        auth_id=int(row.auth_id) if row.auth_id is not None else None,
        advertiser_id=row.advertiser_id,
        store_id=row.store_id,
        promotion_type=row.promotion_type,
        level=MonitoringStrategyLevel(row.level) if row.level else None,
        interval_minutes=int(row.interval_minutes),
        max_campaigns_per_run=row.max_campaigns_per_run,
        enabled=bool(row.enabled),
        category=category,
        task_name=task_name,
        params_json=row.params_json or {},
        input_schema_json=row.input_schema_json or None,
        last_run_at=_as_utc(row.last_run_at),
        last_success_at=_as_utc(row.last_success_at),
        last_error_at=_as_utc(row.last_error_at),
        last_error=row.last_error,
        created_at=_as_utc(row.created_at) or datetime.now(timezone.utc),
        updated_at=_as_utc(row.updated_at) or datetime.now(timezone.utc),
    )


def _apply_filters(base: Select[Any], filters: list[Any]) -> Select[Any]:
    if filters:
        return base.where(and_(*filters))
    return base


def _validate_interval(interval: int | None) -> None:
    """Ensure interval_minutes is a positive value when provided."""
    if interval is None:
        return
    if interval <= 0:
        raise APIError("INVALID_INTERVAL", "interval_minutes must be > 0", 422)


def _normalize_category_task(category: str | None, task_name: str | None) -> tuple[str, str]:
    return category or _DEFAULT_CATEGORY, task_name or _DEFAULT_TASK_NAME


def _resolve_input_schema(category: str, task_name: str, provided: dict[str, Any] | None) -> dict[str, Any]:
    if provided is not None:
        return provided
    config = SCHEDULED_TASKS.get((category, task_name)) or {}
    return dict(config.get("input_schema") or {})


def _validate_category_fields(category: str, level: MonitoringStrategyLevel | None) -> None:
    if category == "GMVMAX" and level is None:
        raise APIError("LEVEL_REQUIRED", "level is required for GMVMAX schedules.", 422)


def _build_scope_filters(
    *,
    workspace_id: int,
    promotion_type: PromotionTypeEnum | None,
    level: str,
    auth_id: int | None,
    advertiser_id: str | None,
    store_id: str | None,
) -> list[Any]:
    filters: list[Any] = [
        GmvMonitoringStrategy.workspace_id == workspace_id,
        GmvMonitoringStrategy.promotion_type == promotion_type,
        GmvMonitoringStrategy.level == level,
    ]

    if auth_id is None:
        filters.append(GmvMonitoringStrategy.auth_id.is_(None))
    else:
        filters.append(GmvMonitoringStrategy.auth_id == auth_id)

    if advertiser_id is None:
        filters.append(GmvMonitoringStrategy.advertiser_id.is_(None))
    else:
        filters.append(GmvMonitoringStrategy.advertiser_id == advertiser_id)

    if store_id is None:
        filters.append(GmvMonitoringStrategy.store_id.is_(None))
    else:
        filters.append(GmvMonitoringStrategy.store_id == store_id)

    return filters


@router.get("", response_model=MonitoringStrategyPage)
def list_monitoring_strategies(
    workspace_id: int | None = Query(default=None),
    auth_id: int | None = Query(default=None),
    store_id: str | None = Query(default=None),
    promotion_type: PromotionTypeEnum | None = Query(default=None),
    level: MonitoringStrategyLevel | None = Query(default=None),
    category: str | None = Query(default=None),
    task_name: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEF_LIMIT, ge=1, le=_MAX_LIMIT),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    filters: list[Any] = []
    if workspace_id is not None:
        filters.append(GmvMonitoringStrategy.workspace_id == int(workspace_id))
    if auth_id is not None:
        filters.append(GmvMonitoringStrategy.auth_id == int(auth_id))
    if store_id is not None:
        filters.append(GmvMonitoringStrategy.store_id == store_id)
    if promotion_type is not None:
        filters.append(GmvMonitoringStrategy.promotion_type == promotion_type)
    if level is not None:
        filters.append(GmvMonitoringStrategy.level == level.value)
    if category is not None:
        filters.append(GmvMonitoringStrategy.category == category)
    if task_name is not None:
        filters.append(GmvMonitoringStrategy.task_name == task_name)
    if enabled is not None:
        filters.append(GmvMonitoringStrategy.enabled.is_(bool(enabled)))

    stmt = _apply_filters(select(GmvMonitoringStrategy), filters).order_by(
        GmvMonitoringStrategy.created_at.desc()
    )
    stmt = stmt.offset(offset).limit(min(limit, _MAX_LIMIT))

    rows = db.scalars(stmt).all()

    total_stmt = _apply_filters(
        select(func.count()).select_from(GmvMonitoringStrategy), filters
    )
    total = db.scalar(total_stmt) or 0

    return MonitoringStrategyPage(
        items=[_to_out(row) for row in rows],
        total=int(total),
    )


@router.post("", response_model=MonitoringStrategyOut, status_code=status.HTTP_201_CREATED)
def create_monitoring_strategy(
    payload: MonitoringStrategyCreate,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    data = payload.dict()
    category, task_name = _normalize_category_task(data.get("category"), data.get("task_name"))
    data["category"] = category
    data["task_name"] = task_name

    _validate_interval(data.get("interval_minutes"))
    _validate_category_fields(category, payload.level)

    input_schema = _resolve_input_schema(category, task_name, data.get("input_schema_json"))
    params_json = data.get("params_json") or {}
    validate_params_or_raise(input_schema, params_json)

    # Avoid passing duplicate keyword args when constructing the ORM model
    data.pop("level", None)
    data.pop("params_json", None)
    data.pop("input_schema_json", None)

    if category == "GMVMAX":
        filters = _build_scope_filters(
            workspace_id=int(payload.workspace_id),
            promotion_type=payload.promotion_type,
            level=payload.level.value if payload.level else None,
            auth_id=payload.auth_id,
            advertiser_id=payload.advertiser_id,
            store_id=payload.store_id,
        )

        existing = db.scalar(select(GmvMonitoringStrategy).where(and_(*filters)).limit(1))
        if existing:
            raise APIError(
                "DUPLICATE_STRATEGY",
                "Monitoring strategy already exists with the same scope.",
                status.HTTP_409_CONFLICT,
            )

    row = GmvMonitoringStrategy(
        **data,
        level=payload.level.value if payload.level else None,
        params_json=params_json,
        input_schema_json=input_schema or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("/{strategy_id}", response_model=MonitoringStrategyOut)
def get_monitoring_strategy(
    strategy_id: int,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    row = db.get(GmvMonitoringStrategy, int(strategy_id))
    if not row:
        raise APIError("NOT_FOUND", "Monitoring strategy not found.", 404)
    return _to_out(row)


@router.patch("/{strategy_id}", response_model=MonitoringStrategyOut)
def update_monitoring_strategy(
    strategy_id: int,
    payload: MonitoringStrategyUpdate,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    row = db.get(GmvMonitoringStrategy, int(strategy_id))
    if not row:
        raise APIError("NOT_FOUND", "Monitoring strategy not found.", 404)

    updates = payload.dict(exclude_unset=True)
    new_category, new_task = _normalize_category_task(
        updates.get("category", row.category), updates.get("task_name", row.task_name)
    )
    new_level = updates.get("level")
    current_level = MonitoringStrategyLevel(row.level) if row.level else None
    target_level = new_level or current_level

    _validate_interval(updates.get("interval_minutes", row.interval_minutes))
    _validate_category_fields(new_category, target_level)

    input_schema = _resolve_input_schema(
        new_category, new_task, updates.get("input_schema_json", row.input_schema_json)
    )
    params_json = updates.get("params_json", row.params_json or {}) or {}
    validate_params_or_raise(input_schema, params_json)

    if new_category == "GMVMAX":
        filters = _build_scope_filters(
            workspace_id=int(row.workspace_id),
            promotion_type=updates.get("promotion_type", row.promotion_type),
            level=target_level.value if target_level else None,
            auth_id=row.auth_id,
            advertiser_id=row.advertiser_id,
            store_id=row.store_id,
        )
        filters.append(GmvMonitoringStrategy.id != row.id)
        existing = db.scalar(select(GmvMonitoringStrategy).where(and_(*filters)).limit(1))
        if existing:
            raise APIError(
                "DUPLICATE_STRATEGY",
                "Monitoring strategy already exists with the same scope.",
                status.HTTP_409_CONFLICT,
            )

    if "promotion_type" in updates:
        row.promotion_type = updates["promotion_type"]
    if "interval_minutes" in updates:
        row.interval_minutes = updates["interval_minutes"]
    if "max_campaigns_per_run" in updates:
        row.max_campaigns_per_run = updates["max_campaigns_per_run"]
    if "enabled" in updates:
        row.enabled = bool(updates["enabled"])

    row.level = target_level.value if target_level else None
    row.category = new_category
    row.task_name = new_task
    row.params_json = params_json
    row.input_schema_json = input_schema or None

    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_monitoring_strategy(
    strategy_id: int,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    row = db.get(GmvMonitoringStrategy, int(strategy_id))
    if not row:
        raise APIError("NOT_FOUND", "Monitoring strategy not found.", 404)
    db.delete(row)
    db.commit()
    return None


@router.post("/{strategy_id}/enable", response_model=MonitoringStrategyOut)
def enable_monitoring_strategy(
    strategy_id: int,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    row = db.get(GmvMonitoringStrategy, int(strategy_id))
    if not row:
        raise APIError("NOT_FOUND", "Monitoring strategy not found.", 404)
    row.enabled = True
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/{strategy_id}/disable", response_model=MonitoringStrategyOut)
def disable_monitoring_strategy(
    strategy_id: int,
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    row = db.get(GmvMonitoringStrategy, int(strategy_id))
    if not row:
        raise APIError("NOT_FOUND", "Monitoring strategy not found.", 404)
    row.enabled = False
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)

