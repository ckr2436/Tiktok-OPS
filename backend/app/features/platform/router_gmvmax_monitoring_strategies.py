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
    level: MonitoringStrategyLevel
    interval_minutes: int = Field(gt=0)
    max_campaigns_per_run: int | None = None
    enabled: bool = True


class MonitoringStrategyCreate(MonitoringStrategyBase):
    pass


class MonitoringStrategyUpdate(BaseModel):
    promotion_type: PromotionTypeEnum | None = None
    level: MonitoringStrategyLevel | None = None
    interval_minutes: int | None = Field(default=None, gt=0)
    max_campaigns_per_run: int | None = None
    enabled: bool | None = None


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


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_out(row: GmvMonitoringStrategy) -> MonitoringStrategyOut:
    return MonitoringStrategyOut(
        id=int(row.id),
        workspace_id=int(row.workspace_id),
        auth_id=int(row.auth_id) if row.auth_id is not None else None,
        advertiser_id=row.advertiser_id,
        store_id=row.store_id,
        promotion_type=row.promotion_type,
        level=MonitoringStrategyLevel(row.level),
        interval_minutes=int(row.interval_minutes),
        max_campaigns_per_run=row.max_campaigns_per_run,
        enabled=bool(row.enabled),
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


@router.get("", response_model=MonitoringStrategyPage)
def list_monitoring_strategies(
    workspace_id: int | None = Query(default=None),
    auth_id: int | None = Query(default=None),
    store_id: str | None = Query(default=None),
    promotion_type: PromotionTypeEnum | None = Query(default=None),
    level: MonitoringStrategyLevel | None = Query(default=None),
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
    _validate_interval(data.get("interval_minutes"))

    row = GmvMonitoringStrategy(**data)
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
    _validate_interval(updates.get("interval_minutes"))

    for key, value in updates.items():
        setattr(row, key, value)

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

