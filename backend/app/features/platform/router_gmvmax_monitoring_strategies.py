"""Admin APIs for GMV Max monitoring strategies (platform domain)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, and_, func, select, text
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError
from app.core.config import settings
from app.data.db import get_db
from app.data.models.gmv_restructured import (
    GmvMonitoringStrategy,
    GmvStrategyConfig,
    PromotionTypeEnum,
)
from app.data.models.providers import PlatformPolicy
from app.data.models.workspaces import Workspace
from app.services.scheduler_schema_utils import validate_params_or_raise
from app.services.scheduler_task_registry import SCHEDULED_TASKS

router = APIRouter(
    prefix="/api/v1/admin/platform/gmvmax/monitoring-strategies",
    tags=["Admin / Platform GMVMax Monitoring"],
)


class MonitoringStrategyLevel(str, Enum):
    OVERVIEW_DAILY = "OVERVIEW_DAILY"
    OVERVIEW_HOURLY = "OVERVIEW_HOURLY"
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

    model_config = ConfigDict(from_attributes=True)


class MonitoringStrategyPage(BaseModel):
    items: list[MonitoringStrategyOut]
    page: int
    page_size: int
    total: int


_DEF_LIMIT = 50
_MAX_LIMIT = 200
_DEFAULT_CATEGORY = "GMVMAX"
_DEFAULT_TASK_NAME = "gmvmax.strategy"
_TIKTOK_APPROVED_QPS = 20


def _as_utc(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime | None) -> str | None:
    normalized = _as_utc(dt)
    return normalized.isoformat().replace("+00:00", "Z") if normalized else None


def _age_seconds(dt: datetime | None, now_utc: datetime) -> int | None:
    normalized = _as_utc(dt)
    if normalized is None:
        return None
    return max(0, int((now_utc - normalized).total_seconds()))


def _runtime_status(last_seen: datetime | None, expected_seconds: int, now_utc: datetime) -> str:
    age = _age_seconds(last_seen, now_utc)
    if age is None:
        return "waiting"
    return "healthy" if age <= max(expected_seconds * 3, 180) else "delayed"


def _schedule_status(
    row: GmvMonitoringStrategy,
    *,
    workspace_deleted: bool,
    now_utc: datetime,
) -> tuple[str, int | None, datetime | None]:
    if workspace_deleted:
        return "legacy", _age_seconds(row.last_success_at, now_utc), None
    if not row.enabled:
        return "disabled", _age_seconds(row.last_success_at, now_utc), None
    if row.last_error and row.last_error_at and (
        not row.last_success_at or row.last_error_at > row.last_success_at
    ):
        return "error", _age_seconds(row.last_success_at, now_utc), None

    age = _age_seconds(row.last_success_at, now_utc)
    next_run = None
    if row.last_run_at:
        next_run = _as_utc(row.last_run_at) + timedelta(minutes=max(1, int(row.interval_minutes)))
    if age is None:
        return "waiting", None, next_run
    late_after = max(int(row.interval_minutes) * 120, 300)
    return ("delayed" if age > late_after else "healthy"), age, next_run


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
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=_MAX_LIMIT),
    offset: int | None = Query(default=None, ge=0),
    limit: int | None = Query(default=None, ge=1, le=_MAX_LIMIT),
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

    effective_page_size = int(
        page_size if page_size is not None else limit if limit is not None else _DEF_LIMIT
    )
    if page is not None or page_size is not None:
        effective_page = int(page or 1)
        effective_offset = (effective_page - 1) * effective_page_size
    else:
        effective_offset = int(offset or 0)
        effective_page = (effective_offset // effective_page_size) + 1

    stmt = _apply_filters(select(GmvMonitoringStrategy), filters).order_by(
        GmvMonitoringStrategy.created_at.desc(),
        GmvMonitoringStrategy.id.desc(),
    )
    stmt = stmt.offset(effective_offset).limit(effective_page_size)

    rows = db.scalars(stmt).all()

    total_stmt = _apply_filters(
        select(func.count()).select_from(GmvMonitoringStrategy), filters
    )
    total = db.scalar(total_stmt) or 0

    return MonitoringStrategyPage(
        items=[_to_out(row) for row in rows],
        page=effective_page,
        page_size=effective_page_size,
        total=int(total),
    )


@router.get("/control-center")
def get_automation_control_center(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Return one platform-managed view of API, sync, guard, and Hermes health."""

    now_utc = datetime.now(timezone.utc)
    schedules = list(
        db.scalars(
            select(GmvMonitoringStrategy).order_by(
                GmvMonitoringStrategy.workspace_id,
                GmvMonitoringStrategy.category,
                GmvMonitoringStrategy.level,
            )
        ).all()
    )
    workspaces = list(db.scalars(select(Workspace).order_by(Workspace.id)).all())
    workspace_map = {int(row.id): row for row in workspaces}

    schedule_items: list[dict[str, Any]] = []
    health_counts = {
        "healthy": 0,
        "delayed": 0,
        "error": 0,
        "waiting": 0,
        "disabled": 0,
        "legacy": 0,
    }
    for row in schedules:
        workspace = workspace_map.get(int(row.workspace_id))
        workspace_deleted = bool(workspace and workspace.deleted_at)
        health, lag_seconds, next_run = _schedule_status(
            row,
            workspace_deleted=workspace_deleted,
            now_utc=now_utc,
        )
        health_counts[health] += 1
        schedule_items.append(
            {
                "id": int(row.id),
                "workspace_id": int(row.workspace_id),
                "workspace_name": workspace.name if workspace else f"Workspace {row.workspace_id}",
                "workspace_deleted": workspace_deleted,
                "auth_id": int(row.auth_id) if row.auth_id is not None else None,
                "advertiser_id": row.advertiser_id,
                "store_id": row.store_id,
                "category": row.category or _DEFAULT_CATEGORY,
                "task_name": row.task_name or _DEFAULT_TASK_NAME,
                "level": row.level,
                "enabled": bool(row.enabled),
                "interval_minutes": int(row.interval_minutes),
                "max_campaigns_per_run": row.max_campaigns_per_run,
                "health": health,
                "lag_seconds": lag_seconds,
                "last_run_at": _iso_utc(row.last_run_at),
                "last_success_at": _iso_utc(row.last_success_at),
                "last_error_at": _iso_utc(row.last_error_at),
                "last_error": row.last_error,
                "next_run_at": _iso_utc(next_run),
            }
        )

    policies = list(db.scalars(select(PlatformPolicy).order_by(PlatformPolicy.id)).all())
    active_policies = [row for row in policies if row.is_enabled]
    obsolete_policies = [
        {
            "id": int(row.id),
            "name": row.name,
            "domains": list(row.domains_json or []),
        }
        for row in active_policies
        if any("drafyn" in str(domain).lower() for domain in (row.domains_json or []))
    ]
    configured_qps = [
        int(row.rate_limit_rps)
        for row in active_policies
        if row.rate_limit_rps is not None and int(row.rate_limit_rps) > 0
    ]

    realtime = db.execute(
        text(
            """
            select max(last_checked_at) as last_checked_at,
                   sum(case when operation_status = 'ENABLE' then 1 else 0 end) as enabled_campaigns,
                   count(*) as campaigns
              from gmv_campaign_realtime_state
            """
        )
    ).mappings().first()
    guard_activity = {
        row["event_type"]: row["last_event_at"]
        for row in db.execute(
            text(
                """
                select event_type, max(created_at) as last_event_at
                  from gmv_campaign_guard_events
                 group by event_type
                """
            )
        ).mappings()
    }
    creative_snapshot_at = db.scalar(
        text(
            """
            select max(source_observed_at)
            from gmv_creative_10min_batch_manifests
            where complete=1
            """
        )
    )

    strategy_configs = list(
        db.scalars(select(GmvStrategyConfig).where(GmvStrategyConfig.enabled.is_(True))).all()
    )
    latest_hermes_review: datetime | None = None
    hermes_review_count = 0
    runtime_rows = db.execute(
        text(
            """
            select strategy_id, runtime_json
            from gmv_campaign_realtime_state
            where strategy_id is not null
            """
        )
    ).mappings().all()
    runtime_by_strategy: dict[int, dict[str, Any]] = {}
    for runtime_row in runtime_rows:
        runtime_value = runtime_row.get("runtime_json")
        if isinstance(runtime_value, str):
            try:
                runtime_value = json.loads(runtime_value)
            except (TypeError, ValueError):
                runtime_value = {}
        if isinstance(runtime_value, dict):
            runtime_by_strategy[int(runtime_row["strategy_id"])] = runtime_value
    for strategy in strategy_configs:
        config = strategy.config_json if isinstance(strategy.config_json, dict) else {}
        runtime = runtime_by_strategy.get(int(strategy.id), {})
        state = runtime.get("smart_guard_state") if isinstance(runtime, dict) else {}
        if not state and isinstance(config, dict):
            state = config.get("smart_guard_state")
        decision = state.get("last_decision") if isinstance(state, dict) else {}
        review = decision.get("hermes_review") if isinstance(decision, dict) else {}
        reviewed_at = review.get("reviewed_at") if isinstance(review, dict) else None
        if not reviewed_at:
            continue
        try:
            parsed = datetime.fromisoformat(str(reviewed_at).replace("Z", "+00:00"))
            parsed = _as_utc(parsed)
        except (TypeError, ValueError):
            continue
        hermes_review_count += 1
        if latest_hermes_review is None or parsed > latest_hermes_review:
            latest_hermes_review = parsed

    smart_guard_seconds = int(getattr(settings, "GMVMAX_SMART_GUARD_INTERVAL", 60))
    creative_guard_seconds = int(getattr(settings, "GMVMAX_CREATIVE_GUARD_INTERVAL", 60))
    hermes_seconds = int(getattr(settings, "GMVMAX_HERMES_ADVISOR_INTERVAL", 600))
    scheduler_seconds = int(getattr(settings, "GMVMAX_SCHEDULER_INTERVAL_SECONDS", 60))
    api_default_qps = float(getattr(settings, "TTB_API_DEFAULT_QPS", 5.0))

    realtime_checked_at = (realtime or {}).get("last_checked_at")
    smart_event_at = guard_activity.get("SMART_GUARD")
    creative_event_at = guard_activity.get("CREATIVE_GUARD")
    active_current_schedules = [
        item
        for item in schedule_items
        if item["enabled"] and not item["workspace_deleted"]
    ]
    active_legacy_count = len(
        [item for item in schedule_items if item["enabled"] and item["workspace_deleted"]]
    )
    creative_schedule_at = max(
        (
            _as_utc(row.last_success_at)
            for row in schedules
            if row.enabled
            and row.level == MonitoringStrategyLevel.CREATIVE_10MIN.value
            and not bool(workspace_map.get(int(row.workspace_id)) and workspace_map[int(row.workspace_id)].deleted_at)
            and row.last_success_at
        ),
        default=None,
    )
    warnings: list[dict[str, Any]] = []
    if obsolete_policies:
        warnings.append(
            {
                "code": "OBSOLETE_DOMAIN_POLICY",
                "severity": "warning",
                "message": "存在旧 Drafyn 域名策略；当前同步不携带域名，规则不会命中。",
            }
        )
    if active_legacy_count:
        warnings.append(
            {
                "code": "LEGACY_SCHEDULES_ACTIVE",
                "severity": "warning",
                "message": f"已删除公司仍有 {active_legacy_count} 条调度任务处于启用状态。",
            }
        )
    if not configured_qps:
        warnings.append(
            {
                "code": "NO_EFFECTIVE_QPS_POLICY",
                "severity": "info",
                "message": "API 护栏未设置有效 QPS；运行时使用客户端默认速率。",
            }
        )

    return {
        "generated_at": _iso_utc(now_utc),
        "profile": {
            "name": "生产默认配置",
            "management_mode": "PLATFORM_MANAGED",
            "tenant_customization": False,
            "api_runtime_qps": api_default_qps,
            "api_approved_qps": int(
                getattr(settings, "TTB_API_APPROVED_QPS", _TIKTOK_APPROVED_QPS)
            ),
            "scheduler_seconds": scheduler_seconds,
            "smart_guard_seconds": smart_guard_seconds,
            "creative_guard_seconds": creative_guard_seconds,
            "hermes_advisor_seconds": hermes_seconds,
            "active_schedule_count": len(active_current_schedules),
        },
        "summary": {
            "workspaces": len([row for row in workspaces if not row.deleted_at and int(row.id) != 1]),
            "active_strategies": len(strategy_configs),
            "tracked_campaigns": int((realtime or {}).get("campaigns") or 0),
            "enabled_campaigns": int((realtime or {}).get("enabled_campaigns") or 0),
            "schedule_health": health_counts,
        },
        "automation": [
            {
                "key": "sync_scheduler",
                "name": "数据同步调度器",
                "status": "healthy" if active_current_schedules else "waiting",
                "cadence_seconds": scheduler_seconds,
                "last_activity_at": max(
                    (item["last_success_at"] for item in active_current_schedules if item["last_success_at"]),
                    default=None,
                ),
                "detail": f"管理 {len(active_current_schedules)} 条平台托管任务",
            },
            {
                "key": "smart_guard",
                "name": "实时止损守护",
                "status": _runtime_status(
                    realtime_checked_at,
                    max(smart_guard_seconds, 180),
                    now_utc,
                ),
                "cadence_seconds": smart_guard_seconds,
                "last_activity_at": _iso_utc(realtime_checked_at or smart_event_at),
                "detail": f"监控 {len(strategy_configs)} 条启用策略",
            },
            {
                "key": "creative_guard",
                "name": "素材质量守护",
                "status": _runtime_status(
                    creative_schedule_at or creative_snapshot_at,
                    max(creative_guard_seconds, 180),
                    now_utc,
                ),
                "cadence_seconds": creative_guard_seconds,
                "last_activity_at": _iso_utc(creative_schedule_at or creative_snapshot_at or creative_event_at),
                "detail": "按素材同步心跳执行排除与恢复判断",
            },
            {
                "key": "hermes",
                "name": "Hermes 决策审批",
                "status": "healthy" if strategy_configs else "waiting",
                "cadence_seconds": hermes_seconds,
                "last_activity_at": _iso_utc(latest_hermes_review),
                "detail": f"已有 {hermes_review_count} 条策略保存审批记录",
            },
        ],
        "api_guardrail": {
            "active_policy_count": len(active_policies),
            "effective_qps": min(configured_qps) if configured_qps else None,
            "runtime_default_qps": api_default_qps,
            "approved_qps": int(
                getattr(settings, "TTB_API_APPROVED_QPS", _TIKTOK_APPROVED_QPS)
            ),
            "obsolete_policies": obsolete_policies,
        },
        "warnings": warnings,
        "schedules": schedule_items,
    }


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
