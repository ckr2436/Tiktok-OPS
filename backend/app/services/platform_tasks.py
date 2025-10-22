"""Business logic for platform scheduled tasks and targeting."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from croniter import croniter
from jsonschema import Draft202012Validator
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, nullslast, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError, RateLimitExceeded
from app.core.metrics import get_counter
from app.data.models.platform_tasks import (
    IdempotencyKey,
    PlatformTaskCatalog,
    PlatformTaskConfig,
    PlatformTaskRun,
    PlatformTaskRunWorkspace,
    RateLimitToken,
    TenantSyncJob,
    WorkspaceTag,
)
from app.data.models.workspaces import Workspace


@dataclass(slots=True)
class TaskCatalogStatus:
    is_enabled: bool
    last_run_at: Optional[datetime]


@dataclass(slots=True)
class TenantSyncJobView:
    job_id: str
    workspace_id: int
    provider: str
    auth_id: int
    kind: str
    status: str
    triggered_at: Optional[datetime]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    duration_sec: Optional[int]
    summary: Optional[str]
    next_allowed_at: Optional[datetime]


_RATE_LIMIT_COUNTER = get_counter(
    "rate_limit_hits_total",
    "Rate limit hits by scope",
    labelnames=("scope",),
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _enforce_rate_limit(
    db: Session,
    scope: str,
    token_key: str,
    *,
    window_seconds: int,
    metric_scope: str,
) -> tuple[int, int, int]:
    now = _utc_now()
    stmt = (
        select(RateLimitToken)
        .where(RateLimitToken.scope == scope, RateLimitToken.token_key == token_key)
        .with_for_update()
    )
    token = db.scalar(stmt)
    if token is None:
        token = RateLimitToken(scope=scope, token_key=token_key)
        db.add(token)

    if token.next_allowed_at and token.next_allowed_at > now:
        _RATE_LIMIT_COUNTER.labels(scope=metric_scope).inc()
        next_allowed = token.next_allowed_at
        raise RateLimitExceeded(
            "Too many requests.",
            next_allowed_at=next_allowed,
            limit=1,
            remaining=0,
            reset_ts=int(next_allowed.timestamp()),
        )

    token.last_seen_at = now
    token.next_allowed_at = now + timedelta(seconds=window_seconds)
    return 1, 0, int(token.next_allowed_at.timestamp())


def enforce_schedule_apply_rate_limit(db: Session, task_key: str, actor_id: int) -> tuple[int, int, int]:
    token_key = f"{task_key}:{actor_id}"
    return _enforce_rate_limit(
        db,
        scope="platform.schedule.apply",
        token_key=token_key,
        window_seconds=60,
        metric_scope="global",
    )


def _isoformat(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_tz(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _min_interval_seconds() -> int:
    base = getattr(settings, "SCHEDULE_MIN_INTERVAL_SECONDS", 60)
    return max(300, int(base))


def _validate_timezone(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except Exception as exc:  # pragma: no cover - ZoneInfo raises generic Exception on invalid tz
        raise APIError("INVALID_SCHEDULE", f"Unknown timezone: {tz}", 400, data={"field": "timezone"}) from exc
    return tz


def validate_schedule_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    mode = (raw.get("mode") or "interval").strip().lower()
    if mode not in {"interval", "cron"}:
        raise APIError("INVALID_SCHEDULE", "schedule.mode must be interval or cron", 400)

    timezone = _validate_timezone(raw.get("timezone") or "UTC")

    min_interval = _min_interval_seconds()

    interval_sec: Optional[int] = None
    cron_expr: Optional[str] = None

    if mode == "interval":
        interval_raw = raw.get("interval_sec")
        if interval_raw is None:
            raise APIError(
                "INVALID_SCHEDULE",
                "schedule.interval_sec is required for interval mode",
                400,
                data={"field": "interval_sec", "min_interval_sec": min_interval},
            )
        try:
            interval_sec = int(interval_raw)
        except Exception as exc:  # pragma: no cover
            raise APIError("INVALID_SCHEDULE", "schedule.interval_sec must be integer", 400) from exc
        if interval_sec < min_interval:
            raise APIError(
                "INVALID_SCHEDULE",
                f"interval must be >= {min_interval}",
                400,
                data={"field": "interval_sec", "min_interval_sec": min_interval},
            )
    else:
        cron_expr = raw.get("cron")
        if not cron_expr or not isinstance(cron_expr, str):
            raise APIError(
                "INVALID_SCHEDULE",
                "schedule.cron is required for cron mode",
                400,
                data={"field": "cron"},
            )
        if not croniter.is_valid(cron_expr):
            raise APIError(
                "INVALID_SCHEDULE",
                "Invalid cron expression",
                400,
                data={"field": "cron"},
            )

    def _parse_dt(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception as exc:  # pragma: no cover
                raise APIError("INVALID_SCHEDULE", "Invalid datetime format", 400) from exc
        raise APIError("INVALID_SCHEDULE", "Invalid datetime format", 400)

    start_at = _ensure_tz(_parse_dt(raw.get("start_at")))
    end_at = _ensure_tz(_parse_dt(raw.get("end_at")))
    if start_at and end_at and start_at > end_at:
        raise APIError(
            "INVALID_SCHEDULE",
            "schedule.start_at must be before end_at",
            400,
            data={"field": "start_at"},
        )

    return {
        "mode": mode,
        "interval_sec": interval_sec,
        "cron": cron_expr,
        "timezone": timezone,
        "start_at": start_at,
        "end_at": end_at,
    }


def validate_rate_limit_payload(raw: Dict[str, Any]) -> Dict[str, Optional[int]]:
    per_workspace_min = raw.get("per_workspace_min_interval_sec")
    min_interval = _min_interval_seconds()
    if per_workspace_min is not None:
        try:
            per_workspace_min = int(per_workspace_min)
        except Exception as exc:  # pragma: no cover
            raise APIError("VALIDATION_ERROR", "per_workspace_min_interval_sec must be integer", 400) from exc
        if per_workspace_min < min_interval:
            raise APIError(
                "INVALID_SCHEDULE",
                f"per_workspace_min_interval_sec must be >= {min_interval}",
                400,
                data={"field": "per_workspace_min_interval_sec", "min_interval_sec": min_interval},
            )

    def _optional_positive(name: str) -> Optional[int]:
        value = raw.get(name)
        if value is None:
            return None
        try:
            ivalue = int(value)
        except Exception as exc:  # pragma: no cover
            raise APIError("VALIDATION_ERROR", f"{name} must be integer", 400) from exc
        if ivalue < 1:
            raise APIError("VALIDATION_ERROR", f"{name} must be >= 1", 400, data={"field": name})
        return ivalue

    return {
        "per_workspace_min_interval_sec": per_workspace_min,
        "global_concurrency": _optional_positive("global_concurrency"),
        "per_workspace_concurrency": _optional_positive("per_workspace_concurrency"),
    }


def _normalize_workspace_ids(ids: Sequence[int] | None) -> List[int]:
    if not ids:
        return []
    result: List[int] = []
    seen: set[int] = set()
    for raw in ids:
        try:
            wid = int(raw)
        except Exception as exc:  # pragma: no cover
            raise APIError("VALIDATION_ERROR", "workspace id must be integer", 400) from exc
        if wid <= 0:
            raise APIError("VALIDATION_ERROR", "workspace id must be positive", 400)
        if wid not in seen:
            seen.add(wid)
            result.append(wid)
    return result


def _normalize_tags(tags: Sequence[str] | None) -> List[str]:
    if not tags:
        return []
    result: List[str] = []
    seen: set[str] = set()
    for raw in tags:
        if raw is None:
            continue
        tag = str(raw).strip()
        if not tag:
            continue
        if len(tag) > 64:
            raise APIError("VALIDATION_ERROR", "tag must be <=64 characters", 400)
        lowered = tag.lower()
        if lowered not in seen:
            seen.add(lowered)
            result.append(tag)
    return result


def _workspace_exists(db: Session, ids: Iterable[int]) -> Tuple[set[int], set[int]]:
    requested = set(ids)
    if not requested:
        return requested, set()
    rows = db.execute(select(Workspace.id).where(Workspace.id.in_(requested))).scalars().all()
    existing = {int(r) for r in rows}
    missing = requested - existing
    return existing, missing


def validate_targeting_payload(db: Session, raw: Dict[str, Any]) -> Dict[str, List]:
    whitelist = _normalize_workspace_ids(raw.get("whitelist_workspace_ids"))
    blacklist = _normalize_workspace_ids(raw.get("blacklist_workspace_ids"))
    include_tags = _normalize_tags(raw.get("include_tags"))
    exclude_tags = _normalize_tags(raw.get("exclude_tags"))

    conflicts = sorted(set(whitelist) & set(blacklist))
    if conflicts:
        raise APIError(
            "LIST_CONFLICT",
            "whitelist intersects blacklist",
            409,
            data={"conflicts": conflicts},
        )

    existing_whitelist, missing_whitelist = _workspace_exists(db, whitelist)
    existing_blacklist, missing_blacklist = _workspace_exists(db, blacklist)
    missing = sorted(missing_whitelist | missing_blacklist)
    if missing:
        raise APIError(
            "WORKSPACE_NOT_FOUND",
            "Unknown workspace ids",
            404,
            data={"workspace_ids": missing},
        )

    normalized = {
        "whitelist_workspace_ids": sorted(existing_whitelist),
        "blacklist_workspace_ids": sorted(existing_blacklist),
        "include_tags": include_tags,
        "exclude_tags": exclude_tags,
    }
    return normalized


def _fetch_workspace_tags(db: Session, workspace_ids: Optional[Iterable[int]] = None) -> Dict[int, set[str]]:
    stmt = select(WorkspaceTag.workspace_id, WorkspaceTag.tag)
    if workspace_ids:
        stmt = stmt.where(WorkspaceTag.workspace_id.in_(set(workspace_ids)))
    rows = db.execute(stmt).all()
    mapping: Dict[int, set[str]] = {}
    for wid, tag in rows:
        tag_lower = (tag or "").strip().lower()
        if not tag_lower:
            continue
        mapping.setdefault(int(wid), set()).add(tag_lower)
    return mapping


def compute_target_workspace_ids(
    db: Session,
    targeting: Dict[str, List[Any]],
) -> Tuple[List[int], Dict[str, Any]]:
    whitelist = targeting.get("whitelist_workspace_ids") or []
    blacklist = set(targeting.get("blacklist_workspace_ids") or [])
    include_tags = {t.lower() for t in targeting.get("include_tags") or []}
    exclude_tags = {t.lower() for t in targeting.get("exclude_tags") or []}

    if whitelist:
        candidate_ids = set(whitelist)
    else:
        candidate_ids = set(
            int(row)
            for row in db.execute(select(Workspace.id)).scalars().all()
        )

    if not candidate_ids:
        return [], {"total": 0, "whitelist_only": bool(whitelist)}

    candidate_ids -= blacklist

    tags_map = _fetch_workspace_tags(db, candidate_ids)

    def _include(wid: int) -> bool:
        tags = tags_map.get(wid, set())
        if include_tags and not (tags & include_tags):
            return False
        if exclude_tags and (tags & exclude_tags):
            return False
        return True

    selected = [wid for wid in sorted(candidate_ids) if _include(wid)]
    summary = {
        "total": len(selected),
        "whitelist_only": bool(whitelist),
        "include_tags": sorted(include_tags),
        "exclude_tags": sorted(exclude_tags),
    }
    return selected, summary


def validate_input_payload(input_payload: Dict[str, Any] | None, schema: Dict[str, Any] | None) -> Dict[str, Any]:
    if input_payload is None:
        input_payload = {}
    if not isinstance(input_payload, dict):
        raise APIError("VALIDATION_ERROR", "input must be an object", 400)
    if not schema:
        return input_payload
    try:
        validator = Draft202012Validator(schema)
    except Exception as exc:  # pragma: no cover - schema misconfiguration
        raise APIError("INTERNAL_ERROR", "Invalid input schema", 500) from exc

    errors = list(validator.iter_errors(input_payload))
    if errors:
        details = [
            {"path": list(err.absolute_path), "message": err.message} for err in errors[:10]
        ]
        raise APIError(
            "VALIDATION_ERROR",
            details[0]["message"],
            400,
            data={"fields": details},
        )
    return input_payload


def get_catalog(db: Session) -> List[Dict[str, Any]]:
    rows = (
        db.execute(
            select(PlatformTaskCatalog)
            .where(PlatformTaskCatalog.is_active.is_(True))
            .order_by(PlatformTaskCatalog.task_key.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    task_keys = [row.task_key for row in rows]

    last_runs: Dict[str, PlatformTaskRun] = {}
    if task_keys:
        run_subq = (
            select(
                PlatformTaskRun.run_id,
                PlatformTaskRun.task_key,
                PlatformTaskRun.status,
                PlatformTaskRun.started_at,
                PlatformTaskRun.finished_at,
                PlatformTaskRun.duration_sec,
            )
            .where(PlatformTaskRun.task_key.in_(task_keys))
            .order_by(
                PlatformTaskRun.task_key.asc(),
                PlatformTaskRun.started_at.desc().nullslast(),
                PlatformTaskRun.run_id.desc(),
            )
        )
        rows_iter = db.execute(run_subq).all()
        for row in rows_iter:
            key = row.task_key
            if key not in last_runs:
                last_runs[key] = {
                    "finished_at": row.finished_at,
                }

    items: List[Dict[str, Any]] = []
    for task in rows:
        config = task.config
        status = TaskCatalogStatus(
            is_enabled=bool(config.is_enabled) if config else False,
            last_run_at=(
            last_runs.get(task.task_key, {}).get("finished_at")
            ),
        )
        items.append(
            {
                "task_key": task.task_key,
                "title": task.title,
                "description": task.description,
                "visibility": task.visibility,
                "supports_whitelist": bool(task.supports_whitelist),
                "supports_blacklist": bool(task.supports_blacklist),
                "supports_tags": bool(task.supports_tags),
                "defaults": task.defaults_json or {},
                "input_schema": task.input_schema_json or {},
                "status": {
                    "is_enabled": status.is_enabled,
                    "last_run_at": _isoformat(status.last_run_at),
                },
            }
        )
    return items


def get_task_config(db: Session, task_key: str) -> Tuple[PlatformTaskCatalog, Optional[PlatformTaskConfig]]:
    catalog = db.scalar(
        select(PlatformTaskCatalog).where(
            PlatformTaskCatalog.task_key == task_key,
            PlatformTaskCatalog.is_active.is_(True),
        )
    )
    if not catalog:
        raise APIError("TASK_NOT_FOUND", "task not found", 404, data={"task_key": task_key})
    config = db.get(PlatformTaskConfig, task_key)
    return catalog, config


def serialize_task_config(catalog: PlatformTaskCatalog, config: Optional[PlatformTaskConfig]) -> Dict[str, Any]:
    defaults = catalog.defaults_json or {}
    schedule_defaults = defaults.get("schedule", {}) if isinstance(defaults, dict) else {}
    rate_defaults = defaults.get("rate_limit", {}) if isinstance(defaults, dict) else {}

    schedule = {
        "mode": config.schedule_mode if config else schedule_defaults.get("mode", "interval"),
        "interval_sec": (
            config.schedule_interval_sec
            if config and config.schedule_mode == "interval"
            else schedule_defaults.get("interval_sec")
        ),
        "cron": (
            config.schedule_cron
            if config and config.schedule_mode == "cron"
            else schedule_defaults.get("cron")
        ),
        "timezone": config.schedule_timezone if config else schedule_defaults.get("timezone", "UTC"),
        "start_at": _isoformat(config.schedule_start_at if config else schedule_defaults.get("start_at")),
        "end_at": _isoformat(config.schedule_end_at if config else schedule_defaults.get("end_at")),
    }

    rate_limit = {
        "per_workspace_min_interval_sec": (
            config.rate_limit_per_workspace_min_interval_sec
            if config
            else rate_defaults.get("per_workspace_min_interval_sec")
        ),
        "global_concurrency": (
            config.rate_limit_global_concurrency if config else rate_defaults.get("global_concurrency")
        ),
        "per_workspace_concurrency": (
            config.rate_limit_per_workspace_concurrency
            if config
            else rate_defaults.get("per_workspace_concurrency")
        ),
    }

    targeting = {
        "whitelist_workspace_ids": list(config.targeting_whitelist_workspace_ids or []),
        "blacklist_workspace_ids": list(config.targeting_blacklist_workspace_ids or []),
        "include_tags": list(config.targeting_include_tags or []),
        "exclude_tags": list(config.targeting_exclude_tags or []),
    } if config else {
        "whitelist_workspace_ids": [],
        "blacklist_workspace_ids": [],
        "include_tags": [],
        "exclude_tags": [],
    }

    metadata = {
        "version": int(config.version) if config else 0,
        "updated_by": config.updated_by if config else None,
        "updated_at": _isoformat(config.updated_at if config else None),
    }

    return {
        "task_key": catalog.task_key,
        "is_enabled": bool(config.is_enabled) if config else False,
        "schedule": schedule,
        "rate_limit": rate_limit,
        "targeting": targeting,
        "input": config.input_payload or {},
        "metadata": metadata,
    }


def _hash_payload(payload: Dict[str, Any]) -> str:
    dumped = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _idempotency_scope(task_key: str) -> str:
    return f"platform.task_config.{task_key}"


def _schedule_apply_scope(task_key: str) -> str:
    return f"platform.schedule.apply.{task_key}"


def update_task_config(
    db: Session,
    task_key: str,
    payload: Dict[str, Any],
    actor_email: Optional[str],
    actor_user_id: Optional[int],
    *,
    idempotency_key: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    catalog, current = get_task_config(db, task_key)

    schedule = validate_schedule_payload(payload.get("schedule") or {})
    rate_limit = validate_rate_limit_payload(payload.get("rate_limit") or {})
    targeting_payload = validate_targeting_payload(db, payload.get("targeting") or {})
    input_payload = validate_input_payload(payload.get("input"), catalog.input_schema_json)
    is_enabled = bool(payload.get("is_enabled", False))

    normalized_payload = {
        "is_enabled": is_enabled,
        "schedule": {
            "mode": schedule["mode"],
            "interval_sec": schedule["interval_sec"],
            "cron": schedule["cron"],
            "timezone": schedule["timezone"],
            "start_at": _isoformat(schedule["start_at"]),
            "end_at": _isoformat(schedule["end_at"]),
        },
        "rate_limit": rate_limit,
        "targeting": targeting_payload,
        "input": input_payload,
    }

    payload_hash = _hash_payload(normalized_payload)

    if idempotency_key:
        existing = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _idempotency_scope(task_key),
                IdempotencyKey.key == idempotency_key,
            )
        )
        if existing:
            if existing.payload_hash and existing.payload_hash != payload_hash:
                raise APIError(
                    "IDEMPOTENCY_CONFLICT",
                    "Payload differs for the same Idempotency-Key",
                    409,
                    data={"scope": existing.scope, "key": idempotency_key},
                )
            if existing.response_json and not dry_run:
                return existing.response_json

    targets, target_summary = compute_target_workspace_ids(db, targeting_payload)

    if dry_run:
        return {
            "ok": True,
            "version": int(current.version) if current else 0,
            "dry_run": True,
            "target_count": len(targets),
            "target_preview": targets[:50],
            "summary": target_summary,
        }

    if current is None:
        current = PlatformTaskConfig(task_key=task_key)
        db.add(current)
        version = 1
    else:
        version = int(current.version) + 1

    current.is_enabled = is_enabled
    current.schedule_mode = schedule["mode"]
    current.schedule_interval_sec = schedule["interval_sec"]
    current.schedule_cron = schedule["cron"]
    current.schedule_timezone = schedule["timezone"]
    current.schedule_start_at = schedule["start_at"]
    current.schedule_end_at = schedule["end_at"]
    current.rate_limit_per_workspace_min_interval_sec = rate_limit["per_workspace_min_interval_sec"]
    current.rate_limit_global_concurrency = rate_limit["global_concurrency"]
    current.rate_limit_per_workspace_concurrency = rate_limit["per_workspace_concurrency"]
    current.targeting_whitelist_workspace_ids = targeting_payload["whitelist_workspace_ids"]
    current.targeting_blacklist_workspace_ids = targeting_payload["blacklist_workspace_ids"]
    current.targeting_include_tags = targeting_payload["include_tags"]
    current.targeting_exclude_tags = targeting_payload["exclude_tags"]
    current.input_payload = input_payload
    current.version = version
    current.updated_by = actor_email
    current.updated_by_user_id = actor_user_id
    current.target_snapshot_workspace_ids = targets
    current.target_snapshot_generated_at = _utc_now()

    db.flush()

    response = {"ok": True, "version": version, "target_count": len(targets)}

    if idempotency_key:
        entry = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _idempotency_scope(task_key),
                IdempotencyKey.key == idempotency_key,
            )
        )
        if entry:
            entry.payload_hash = payload_hash
            entry.response_json = response
        else:
            db.add(
                IdempotencyKey(
                    scope=_idempotency_scope(task_key),
                    key=idempotency_key,
                    payload_hash=payload_hash,
                    response_json=response,
                )
            )

    return response


def get_last_run(db: Session, task_key: str) -> Dict[str, Any]:
    get_task_config(db, task_key)  # ensure task exists
    run = db.scalar(
        select(PlatformTaskRun)
        .where(PlatformTaskRun.task_key == task_key)
        .order_by(PlatformTaskRun.started_at.desc().nullslast(), PlatformTaskRun.run_id.desc())
    )
    if not run:
        raise APIError("RUN_NOT_FOUND", "No runs found", 404, data={"task_key": task_key})

    samples = (
        db.execute(
            select(PlatformTaskRunWorkspace)
            .where(PlatformTaskRunWorkspace.run_id == run.run_id)
            .order_by(PlatformTaskRunWorkspace.workspace_id.asc())
            .limit(20)
        )
        .scalars()
        .all()
    )

    stats = run.stats_json or {}
    duration = run.duration_sec
    if duration is None and run.started_at and run.finished_at:
        duration = int((run.finished_at - run.started_at).total_seconds())

    return {
        "task_key": task_key,
        "status": run.status,
        "started_at": _isoformat(run.started_at),
        "finished_at": _isoformat(run.finished_at),
        "duration_sec": duration,
        "summary": run.summary,
        "stats": stats,
        "workspace_samples": [
            {
                "workspace_id": int(sample.workspace_id),
                "status": sample.status,
                "count": sample.count,
                "error_code": sample.error_code,
            }
            for sample in samples
        ],
    }


def _apply_cursor(query: Select, cursor: Optional[str]) -> Select:
    if not cursor:
        return query
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
    except Exception as exc:
        raise APIError("VALIDATION_ERROR", "Invalid cursor", 400) from exc

    start_str = data.get("started_at")
    run_id = data.get("run_id")
    if run_id is None:
        raise APIError("VALIDATION_ERROR", "cursor missing run_id", 400)

    if start_str:
        try:
            started_at = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except Exception as exc:  # pragma: no cover
            raise APIError("VALIDATION_ERROR", "invalid cursor timestamp", 400) from exc
        cond = or_(
            PlatformTaskRun.started_at < started_at,
            and_(
                PlatformTaskRun.started_at == started_at,
                PlatformTaskRun.run_id < run_id,
            ),
        )
    else:
        cond = PlatformTaskRun.run_id < run_id

    return query.where(cond)


def _encode_cursor(run: PlatformTaskRun) -> str:
    payload = {
        "started_at": _isoformat(run.started_at),
        "run_id": run.run_id,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")


def list_runs(
    db: Session,
    task_key: str,
    *,
    status: Optional[str] = None,
    workspace_id: Optional[int] = None,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    get_task_config(db, task_key)

    limit = max(1, min(100, int(limit)))

    query = select(PlatformTaskRun).where(PlatformTaskRun.task_key == task_key)

    if status:
        query = query.where(PlatformTaskRun.status == status)

    if workspace_id is not None:
        query = (
            query.join(PlatformTaskRunWorkspace)
            .where(PlatformTaskRunWorkspace.workspace_id == int(workspace_id))
            .distinct()
        )

    query = _apply_cursor(query, cursor)

    query = query.order_by(
        nullslast(PlatformTaskRun.started_at.desc()),
        PlatformTaskRun.run_id.desc(),
    ).limit(limit + 1)

    rows = db.execute(query).scalars().all()

    has_next = len(rows) > limit
    rows = rows[:limit]

    items = []
    for run in rows:
        duration = run.duration_sec
        if duration is None and run.started_at and run.finished_at:
            duration = int((run.finished_at - run.started_at).total_seconds())
        items.append(
            {
                "run_id": run.run_id,
                "status": run.status,
                "started_at": _isoformat(run.started_at),
                "finished_at": _isoformat(run.finished_at),
                "duration_sec": duration,
                "stats": run.stats_json or {},
            }
        )

    next_cursor = _encode_cursor(rows[-1]) if has_next and rows else None
    return {"items": items, "next_cursor": next_cursor}


def apply_schedule_snapshot(
    db: Session,
    task_key: str,
    *,
    dry_run: bool = False,
    idempotency_key: Optional[str] = None,
    payload_hash: Optional[str] = None,
) -> Dict[str, Any]:
    catalog, config = get_task_config(db, task_key)
    if not config:
        raise APIError("TASK_NOT_FOUND", "task not configured", 404, data={"task_key": task_key})

    if payload_hash is None:
        payload_hash = _hash_payload({"dry_run": dry_run})

    if idempotency_key:
        scope = _schedule_apply_scope(task_key)
        existing = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == scope,
                IdempotencyKey.key == idempotency_key,
            )
        )
        if existing:
            if existing.payload_hash and existing.payload_hash != payload_hash:
                raise APIError(
                    "IDEMPOTENCY_CONFLICT",
                    "Payload differs for the same Idempotency-Key",
                    409,
                    data={"payload_hash": existing.payload_hash},
                )
            if existing.response_json:
                return existing.response_json

    targets = config.target_snapshot_workspace_ids or []
    summary = {
        "created": 0,
        "updated": 0,
        "disabled": 0,
        "skipped": 0,
        "total_targets": len(targets),
    }
    response = {
        "ok": True,
        "summary": summary,
        "violations": [],
    }
    if dry_run:
        response["dry_run"] = True

    if idempotency_key:
        scope = _schedule_apply_scope(task_key)
        entry = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == scope,
                IdempotencyKey.key == idempotency_key,
            )
        )
        if entry:
            entry.payload_hash = payload_hash
            entry.response_json = response
        else:
            db.add(
                IdempotencyKey(
                    scope=scope,
                    key=idempotency_key,
                    payload_hash=payload_hash,
                    response_json=response,
                )
            )

    return response


def get_last_sync_job(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    kind: str,
) -> Optional[TenantSyncJobView]:
    job = db.scalar(
        select(TenantSyncJob)
        .where(
            TenantSyncJob.workspace_id == workspace_id,
            TenantSyncJob.provider == provider,
            TenantSyncJob.auth_id == auth_id,
            TenantSyncJob.kind == kind,
        )
        .order_by(TenantSyncJob.triggered_at.desc(), TenantSyncJob.job_id.desc())
    )

    if not job:
        return None

    duration: Optional[int] = getattr(job, "duration_sec", None)
    if duration is None and job.finished_at is not None:
        start = job.started_at or job.triggered_at
        if start is not None:
            total = int((job.finished_at - start).total_seconds())
            duration = total if total >= 0 else 0

    return TenantSyncJobView(
        job_id=job.job_id,
        workspace_id=int(job.workspace_id),
        provider=job.provider,
        auth_id=int(job.auth_id),
        kind=job.kind,
        status=job.status,
        triggered_at=job.triggered_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_sec=duration,
        summary=job.summary,
        next_allowed_at=job.next_allowed_at,
    )

