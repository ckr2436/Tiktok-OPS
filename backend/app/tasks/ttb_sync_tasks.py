from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery import Task
from celery.utils.log import get_task_logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthAccountTTB
from app.data.models.scheduling import ScheduleRun
from app.services.audit import log_event
from app.services.db_locks import binding_action_lock_key, mysql_advisory_lock
from app.services.provider_registry import load_builtin_providers, provider_registry
from app.services.providers.tiktok_business import ProviderExecutionError

load_builtin_providers()

logger = get_task_logger(__name__)


@dataclass(slots=True)
class EnvelopeMeta:
    run_id: Optional[int]
    schedule_id: Optional[int]
    idempotency_key: Optional[str]


@dataclass(slots=True)
class ProviderEnvelope:
    version: int
    provider: str
    scope: str
    workspace_id: int
    auth_id: int
    options: Dict[str, Any]
    meta: EnvelopeMeta


class _ContextLogger:
    def __init__(self, envelope: ProviderEnvelope):
        self._envelope = envelope

    def info(self, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = _log_payload(self._envelope, extra or {})
        logger.info(message, extra=payload)

    def warning(self, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = _log_payload(self._envelope, extra or {})
        logger.warning(message, extra=payload)

    def error(self, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = _log_payload(self._envelope, extra or {})
        logger.error(message, extra=payload)


def _log_payload(envelope: ProviderEnvelope, extra: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "provider": envelope.provider,
        "scope": envelope.scope,
        "workspace_id": envelope.workspace_id,
        "auth_id": envelope.auth_id,
        "run_id": envelope.meta.run_id,
    }
    payload.update(extra)
    return payload


def _db_session() -> Session:
    gen = get_db()
    db = next(gen)
    setattr(db, "__GEN__", gen)
    return db


def _db_close(db: Session) -> None:
    gen = getattr(db, "__GEN__", None)
    with contextlib.suppress(Exception):
        if gen:
            next(gen, None)
    with contextlib.suppress(Exception):
        db.close()


def _push_recent_job(envelope: ProviderEnvelope, task_id: str, max_len: int = 200) -> None:
    backend = getattr(celery_app, "backend", None)
    client = getattr(backend, "client", None)
    if not client:
        return
    try:
        key = f"jobs:{envelope.provider}:{envelope.workspace_id}:{envelope.auth_id}"
        client.lpush(key, task_id)
        client.ltrim(key, 0, max_len - 1)
    except Exception:  # noqa: BLE001
        pass


def _extract_envelope(params: Optional[Dict[str, Any]]) -> ProviderEnvelope:
    if not params or "envelope" not in params:
        raise ValueError("missing params.envelope")
    envelope = params.get("envelope") or {}
    if not isinstance(envelope, dict):
        raise ValueError("params.envelope must be a dict")
    meta = envelope.get("meta") or {}
    return ProviderEnvelope(
        version=int(envelope.get("envelope_version") or envelope.get("version") or 1),
        provider=str(envelope.get("provider") or "").strip(),
        scope=str(envelope.get("scope") or "").strip(),
        workspace_id=int(envelope.get("workspace_id")),
        auth_id=int(envelope.get("auth_id")),
        options=dict(envelope.get("options") or {}),
        meta=EnvelopeMeta(
            run_id=int(meta.get("run_id")) if meta.get("run_id") is not None else None,
            schedule_id=int(meta.get("schedule_id")) if meta.get("schedule_id") is not None else None,
            idempotency_key=meta.get("idempotency_key"),
        ),
    )


def _get_run(
    db: Session,
    *,
    run_id: Optional[int],
    idempotency_key: Optional[str],
) -> Optional[ScheduleRun]:
    if run_id:
        return db.get(ScheduleRun, int(run_id))
    if idempotency_key:
        stmt = (
            select(ScheduleRun)
            .where(ScheduleRun.idempotency_key == idempotency_key)
            .order_by(ScheduleRun.id.desc())
            .limit(1)
        )
        return db.execute(stmt).scalar_one_or_none()
    return None


def _merge_stats(run: ScheduleRun, extra: Dict[str, Any]) -> None:
    current = dict(run.stats_json or {})
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            merged = dict(current[key])
            merged.update(value)
            current[key] = merged
        else:
            current[key] = value
    run.stats_json = current


def _mark_run_running(
    db: Session,
    *,
    run_id: Optional[int],
    idempotency_key: Optional[str],
    broker_id: str,
) -> Optional[ScheduleRun]:
    run = _get_run(db, run_id=run_id, idempotency_key=idempotency_key)
    if run:
        run.status = "running"
        run.broker_msg_id = broker_id
        if not run.enqueued_at:
            run.enqueued_at = datetime.now(timezone.utc)
        db.add(run)
    return run


def _finish_run(
    run: Optional[ScheduleRun],
    *,
    status: str,
    duration_ms: int,
    retries: int,
    processed: Optional[Dict[str, Any]] = None,
    errors: Optional[list] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    if not run:
        return
    run.status = status
    run.duration_ms = duration_ms
    run.error_code = error_code
    run.error_message = error_message[:512] if error_message else None
    payload: Dict[str, Any] = {"retries": retries}
    if processed is not None:
        processed_payload = dict(processed)
        summary = dict(processed_payload.get("summary") or {})
        if errors:
            summary["partial"] = True
        else:
            summary.setdefault("partial", False)
        processed_payload["summary"] = summary
        payload["processed"] = processed_payload
    if errors is not None:
        payload["errors"] = errors
    _merge_stats(run, payload)


def _audit_event(
    db: Session,
    *,
    envelope: ProviderEnvelope,
    event: str,
    schedule_run_id: Optional[int],
    details: Optional[Dict[str, Any]] = None,
) -> None:
    payload = {
        "auth_id": int(envelope.auth_id),
        "provider": envelope.provider,
        "scope": envelope.scope,
    }
    if schedule_run_id:
        payload["schedule_run_id"] = int(schedule_run_id)
    if details:
        payload.update(details)
    log_event(
        db,
        action=event,
        resource_type="ttb_sync",
        resource_id=schedule_run_id,
        actor_workspace_id=int(envelope.workspace_id),
        workspace_id=int(envelope.workspace_id),
        details=payload,
    )


def _should_mark_invalid(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status in (401, 403):
        return True
    if isinstance(code, str) and code.lower() in {"token_not_found", "token_invalid", "unauthorized", "auth_failed"}:
        return True
    return False


def _mark_account_invalid(db: Session, envelope: ProviderEnvelope, reason: Optional[str] = None) -> None:
    acc = db.get(OAuthAccountTTB, int(envelope.auth_id))
    if not acc or acc.status == "invalid":
        return
    acc.status = "invalid"
    db.add(acc)


def _build_processed_stats(phases: list[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, Dict[str, int]] = {}
    cursors: Dict[str, Any] = {}
    timings: Dict[str, Dict[str, int]] = {}
    summary: Dict[str, int] = {}
    total = 0
    for phase in phases:
        scope = str(phase.get("scope") or "").strip() or "unknown"
        stats = phase.get("stats") or {}
        duration = int(phase.get("duration_ms") or 0)
        total += max(duration, 0)
        timings[scope] = {"duration_ms": max(duration, 0)}
        counts[scope] = {
            "fetched": int(stats.get("fetched") or 0),
            "upserts": int(stats.get("upserts") or 0),
            "skipped": int(stats.get("skipped") or 0),
        }
        summary[f"{scope}_count"] = counts[scope]["upserts"]
        if stats.get("cursor") is not None:
            cursors[scope] = stats["cursor"]
    summary.setdefault("partial", False)
    return {
        "counts": counts,
        "summary": summary,
        "cursors": cursors,
        "timings": {"total_ms": total, "phases": timings},
    }


def _serialize_error(stage: str, exc: Exception) -> Dict[str, Any]:
    code = getattr(exc, "code", None) or exc.__class__.__name__
    status = getattr(exc, "status", None)
    return {
        "stage": stage,
        "code": str(code),
        "message": str(exc)[:512],
        "status": status,
    }


def _envelope_to_dict(envelope: ProviderEnvelope) -> Dict[str, Any]:
    return {
        "envelope_version": envelope.version,
        "provider": envelope.provider,
        "scope": envelope.scope,
        "workspace_id": envelope.workspace_id,
        "auth_id": envelope.auth_id,
        "options": envelope.options,
        "meta": {
            "run_id": envelope.meta.run_id,
            "schedule_id": envelope.meta.schedule_id,
            "idempotency_key": envelope.meta.idempotency_key,
        },
    }


class TTBSyncTask(Task):
    abstract = True
    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = 60
    retry_jitter = True

    def on_retry(self, exc, task_id, args, kwargs, einfo):  # type: ignore[override]
        params = kwargs.get("params") if isinstance(kwargs, dict) else None
        try:
            envelope = _extract_envelope(params)
        except Exception:  # noqa: BLE001
            return super().on_retry(exc, task_id, args, kwargs, einfo)

        db = _db_session()
        retry_count: Optional[int] = None
        try:
            run = _get_run(
                db,
                run_id=envelope.meta.run_id,
                idempotency_key=envelope.meta.idempotency_key,
            )
            if run:
                retry_count = int((run.stats_json or {}).get("retries", 0)) + 1
                _merge_stats(run, {"retries": retry_count, "last_error": str(exc)[:256]})
                run.status = "enqueued"
                db.add(run)
            _audit_event(
                db,
                envelope=envelope,
                event=f"ttb.sync.{envelope.scope}.retry",
                schedule_run_id=run.id if run else envelope.meta.run_id,
                details={"error": str(exc)[:256], "retry": retry_count},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "ttb sync retry hook failed",
                extra=_log_payload(envelope, {"task": self.name}),
            )
        finally:
            _db_close(db)
        super().on_retry(exc, task_id, args, kwargs, einfo)


def _execute_task(
    self: TTBSyncTask,
    *,
    expected_scope: str,
    workspace_id: int,
    auth_id: int,
    scope: str,
    params: Optional[Dict[str, Any]],
    run_id: Optional[int],
    idempotency_key: Optional[str],
) -> Dict[str, Any]:
    envelope = _extract_envelope(params)
    if envelope.scope != expected_scope:
        raise ValueError(f"envelope scope {envelope.scope} mismatch for task {expected_scope}")

    context_logger = _ContextLogger(envelope)
    _push_recent_job(envelope, self.request.id)

    db = _db_session()
    run: Optional[ScheduleRun] = None
    started_ns = time.perf_counter_ns()
    processed: Dict[str, Any] | None = None
    errors: list[Dict[str, Any]] = []

    try:
        run = _mark_run_running(
            db,
            run_id=envelope.meta.run_id,
            idempotency_key=envelope.meta.idempotency_key,
            broker_id=self.request.id,
        )
        _audit_event(
            db,
            envelope=envelope,
            event=f"ttb.sync.{expected_scope}.start",
            schedule_run_id=run.id if run else envelope.meta.run_id,
            details={"schedule_id": envelope.meta.schedule_id},
        )

        lock_key = binding_action_lock_key(envelope.workspace_id, envelope.auth_id, expected_scope)
        with mysql_advisory_lock(db, lock_key, wait_seconds=5) as got:
            if not got:
                msg = "another sync job running for this binding"
                context_logger.warning(
                    "ttb sync skipped due to concurrent job",
                    extra={"reason": msg},
                )
                duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
                _finish_run(
                    run,
                    status="failed",
                    duration_ms=duration_ms,
                    retries=self.request.retries,
                    processed={
                        "counts": {},
                        "summary": {"skipped": True},
                        "cursors": {},
                        "timings": {"total_ms": duration_ms, "phases": {}},
                    },
                    errors=[{"stage": expected_scope, "code": "lock_not_acquired", "message": msg}],
                    error_code="lock_not_acquired",
                    error_message=msg,
                )
                if run:
                    db.add(run)
                _audit_event(
                    db,
                    envelope=envelope,
                    event=f"ttb.sync.{expected_scope}.failed",
                    schedule_run_id=run.id if run else envelope.meta.run_id,
                    details={"reason": msg},
                )
                db.commit()
                return {"error": msg}

            handler = provider_registry.get(envelope.provider)
            try:
                result = asyncio.run(
                    handler.run_scope(
                        db=db,
                        envelope=_envelope_to_dict(envelope),
                        scope=expected_scope,
                        logger=context_logger,
                    )
                )
                phases = result.get("phases") or []
                processed = _build_processed_stats(phases)
                errors.extend(result.get("errors") or [])
            except ProviderExecutionError as exc:
                phases = [
                    {"scope": phase.scope, "stats": phase.stats, "duration_ms": phase.duration_ms}
                    for phase in exc.phases
                ]
                processed = _build_processed_stats(phases)
                original = exc.original
                errors.append(_serialize_error(exc.stage, original))
                if _should_mark_invalid(original):
                    _mark_account_invalid(db, envelope, reason=str(original))
            except Exception as exc:
                errors.append(_serialize_error(expected_scope, exc))
                if _should_mark_invalid(exc):
                    _mark_account_invalid(db, envelope, reason=str(exc))
                raise

        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        status = "success"
        _finish_run(
            run,
            status=status,
            duration_ms=duration_ms,
            retries=self.request.retries,
            processed=processed or _build_processed_stats([]),
            errors=errors,
        )
        if run:
            db.add(run)
        _audit_event(
            db,
            envelope=envelope,
            event=f"ttb.sync.{expected_scope}.{status}",
            schedule_run_id=run.id if run else envelope.meta.run_id,
            details={"processed": processed, "errors": errors},
        )
        context_logger.info(
            "ttb sync completed",
            extra={"status": status},
        )
        db.commit()
        return {"status": status, "processed": processed, "errors": errors}
    except Exception as exc:
        db.rollback()
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        error_code = getattr(exc, "code", None) or exc.__class__.__name__
        error_message = str(exc)
        _finish_run(
            run,
            status="failed",
            duration_ms=duration_ms,
            retries=self.request.retries,
            processed=processed,
            errors=errors or [_serialize_error(expected_scope, exc)],
            error_code=str(error_code),
            error_message=error_message,
        )
        if run:
            try:
                db.add(run)
                db.commit()
            except Exception:
                db.rollback()
        context_logger.error(
            "ttb sync task failed",
            extra={"error": error_message, "code": error_code},
        )
        raise
    finally:
        _db_close(db)


@celery_app.task(name="ttb.sync.bc", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_bc(
    self,
    workspace_id: int,
    auth_id: int,
    scope: str,
    params: Optional[Dict[str, Any]] = None,
    run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    return _execute_task(
        self,
        expected_scope="bc",
        workspace_id=workspace_id,
        auth_id=auth_id,
        scope=scope,
        params=params,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )


@celery_app.task(name="ttb.sync.advertisers", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_advertisers(
    self,
    workspace_id: int,
    auth_id: int,
    scope: str,
    params: Optional[Dict[str, Any]] = None,
    run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    return _execute_task(
        self,
        expected_scope="advertisers",
        workspace_id=workspace_id,
        auth_id=auth_id,
        scope=scope,
        params=params,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )


@celery_app.task(name="ttb.sync.shops", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_shops(
    self,
    workspace_id: int,
    auth_id: int,
    scope: str,
    params: Optional[Dict[str, Any]] = None,
    run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    return _execute_task(
        self,
        expected_scope="shops",
        workspace_id=workspace_id,
        auth_id=auth_id,
        scope=scope,
        params=params,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )


@celery_app.task(name="ttb.sync.products", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_products(
    self,
    workspace_id: int,
    auth_id: int,
    scope: str,
    params: Optional[Dict[str, Any]] = None,
    run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    return _execute_task(
        self,
        expected_scope="products",
        workspace_id=workspace_id,
        auth_id=auth_id,
        scope=scope,
        params=params,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )


@celery_app.task(name="ttb.sync.all", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_all(
    self,
    workspace_id: int,
    auth_id: int,
    scope: str,
    params: Optional[Dict[str, Any]] = None,
    run_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    **_: Any,
) -> Dict[str, Any]:
    return _execute_task(
        self,
        expected_scope="all",
        workspace_id=workspace_id,
        auth_id=auth_id,
        scope=scope,
        params=params,
        run_id=run_id,
        idempotency_key=idempotency_key,
    )
