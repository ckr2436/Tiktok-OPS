"""Utilities for managing TikTok Business GMV Max binding configuration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.scheduling import Schedule
from app.data.models.ttb_entities import TTBBindingConfig


log = logging.getLogger(__name__)

_AUTO_SCOPE = "products"
_AUTO_TASK_NAME = "ttb.sync.products"
_AUTO_PROVIDER = "tiktok-business"
_MIN_INTERVAL_SECONDS = 900


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _auto_sync_interval() -> int:
    candidate = int(getattr(settings, "TTB_GMV_AUTO_SYNC_INTERVAL_SECONDS", _MIN_INTERVAL_SECONDS))
    schedule_min = int(getattr(settings, "SCHEDULE_MIN_INTERVAL_SECONDS", 60))
    return max(_MIN_INTERVAL_SECONDS, candidate, schedule_min)


class BindingConfigStorageNotReady(RuntimeError):
    """Raised when the GMV Max binding configuration storage is unavailable."""


def _is_missing_table_error(exc: Exception) -> bool:
    if isinstance(exc, (ProgrammingError, OperationalError)):
        orig = getattr(exc, "orig", None)
        if orig is not None:
            # MySQL reports error code 1146, SQLite raises "no such table" messages.
            args = getattr(orig, "args", ())
            if args:
                code = args[0]
                if code == 1146:
                    return True
            message = str(orig).lower()
            if "no such table" in message:
                return True
    return False


def _handle_missing_table(exc: Exception) -> None:
    if _is_missing_table_error(exc):
        log.warning("GMV Max binding configuration table missing; migrations not applied", exc_info=exc)
        raise BindingConfigStorageNotReady("GMV Max binding configuration storage is unavailable") from exc
    raise exc


def get_binding_config(db: Session, *, workspace_id: int, auth_id: int) -> Optional[TTBBindingConfig]:
    try:
        return (
            db.query(TTBBindingConfig)
            .filter(
                TTBBindingConfig.workspace_id == int(workspace_id),
                TTBBindingConfig.auth_id == int(auth_id),
            )
            .one_or_none()
        )
    except (ProgrammingError, OperationalError) as exc:  # pragma: no cover - defensive branch
        _handle_missing_table(exc)
        raise  # pragma: no cover - _handle_missing_table always raises


def _build_auto_options(
    *, bc_id: Optional[str], advertiser_id: Optional[str], store_id: Optional[str]
) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "mode": "incremental",
        "product_eligibility": "gmv_max",
    }
    if advertiser_id:
        options["advertiser_id"] = advertiser_id
    if store_id:
        options["store_id"] = store_id
    if bc_id:
        options["bc_id"] = bc_id
    return options


def _ensure_auto_schedule(
    db: Session,
    *,
    config: TTBBindingConfig,
    workspace_id: int,
    auth_id: int,
    bc_id: Optional[str],
    advertiser_id: Optional[str],
    store_id: Optional[str],
    actor_user_id: Optional[int],
) -> None:
    schedule: Optional[Schedule] = None
    if config.auto_sync_schedule_id:
        schedule = db.get(Schedule, int(config.auto_sync_schedule_id))

    if not config.auto_sync_products:
        if schedule:
            schedule.enabled = False
            db.add(schedule)
        return

    options = _build_auto_options(bc_id=bc_id, advertiser_id=advertiser_id, store_id=store_id)
    params_json: Dict[str, Any] = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "scope": _AUTO_SCOPE,
        "provider": _AUTO_PROVIDER,
        "options": options,
    }
    envelope = {
        "envelope_version": 1,
        "provider": _AUTO_PROVIDER,
        "scope": _AUTO_SCOPE,
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "options": options,
        "meta": {},
    }
    params_json["envelope"] = envelope

    interval_seconds = _auto_sync_interval()

    if schedule is None:
        schedule = Schedule(
            workspace_id=int(workspace_id),
            task_name=_AUTO_TASK_NAME,
            schedule_type="interval",
            interval_seconds=interval_seconds,
            params_json=params_json,
            timezone="UTC",
            enabled=True,
            created_by_user_id=int(actor_user_id) if actor_user_id is not None else None,
            updated_by_user_id=int(actor_user_id) if actor_user_id is not None else None,
        )
    else:
        schedule.task_name = _AUTO_TASK_NAME
        schedule.schedule_type = "interval"
        schedule.interval_seconds = interval_seconds
        schedule.params_json = params_json
        schedule.timezone = schedule.timezone or "UTC"
        schedule.enabled = True
        if actor_user_id is not None:
            schedule.updated_by_user_id = int(actor_user_id)
    db.add(schedule)
    db.flush()
    config.auto_sync_schedule_id = int(schedule.id)


def upsert_binding_config(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    bc_id: Optional[str],
    advertiser_id: Optional[str],
    store_id: Optional[str],
    auto_sync_products: bool,
    actor_user_id: Optional[int],
) -> TTBBindingConfig:
    normalized_bc = _normalize_identifier(bc_id)
    normalized_adv = _normalize_identifier(advertiser_id)
    normalized_store = _normalize_identifier(store_id)

    try:
        row = get_binding_config(db, workspace_id=workspace_id, auth_id=auth_id)
    except BindingConfigStorageNotReady:
        raise
    except (ProgrammingError, OperationalError) as exc:  # pragma: no cover - defensive branch
        _handle_missing_table(exc)
        raise
    if row is None:
        row = TTBBindingConfig(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            bc_id=normalized_bc,
            advertiser_id=normalized_adv,
            store_id=normalized_store,
            auto_sync_products=bool(auto_sync_products),
        )
    else:
        row.bc_id = normalized_bc
        row.advertiser_id = normalized_adv
        row.store_id = normalized_store
        row.auto_sync_products = bool(auto_sync_products)
    db.add(row)
    try:
        db.flush()
    except (ProgrammingError, OperationalError) as exc:  # pragma: no cover - defensive branch
        _handle_missing_table(exc)
        raise

    try:
        _ensure_auto_schedule(
            db,
            config=row,
            workspace_id=workspace_id,
            auth_id=auth_id,
            bc_id=normalized_bc,
            advertiser_id=normalized_adv,
            store_id=normalized_store,
            actor_user_id=actor_user_id,
        )
        db.add(row)
    except (ProgrammingError, OperationalError) as exc:  # pragma: no cover - defensive branch
        _handle_missing_table(exc)
        raise
    return row


def record_products_sync_result(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: Optional[str],
    store_id: Optional[str],
    summary: Dict[str, Any] | None,
    triggered_by_auto: bool,
) -> None:
    if not advertiser_id or not store_id:
        return
    try:
        row = get_binding_config(db, workspace_id=workspace_id, auth_id=auth_id)
    except BindingConfigStorageNotReady:
        return
    except (ProgrammingError, OperationalError) as exc:  # pragma: no cover - defensive branch
        _handle_missing_table(exc)
        return
    if not row:
        return
    if _normalize_identifier(advertiser_id) != _normalize_identifier(row.advertiser_id):
        return
    if _normalize_identifier(store_id) != _normalize_identifier(row.store_id):
        return

    payload = summary or {}
    now = _utcnow()
    if triggered_by_auto:
        row.last_auto_synced_at = now
        row.last_auto_sync_summary_json = payload or None
    else:
        row.last_manual_synced_at = now
        row.last_manual_sync_summary_json = payload or None
    db.add(row)
