from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery import Task
from celery.utils.log import get_task_logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.config import settings
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.scheduling import ScheduleRun
from app.services.audit import log_event
from app.services.db_locks import mysql_advisory_lock, binding_action_lock_key
from app.services.oauth_ttb import (
    get_access_token_for_auth_id,
    get_credentials_for_auth_id,
)
from app.services.ttb_api import TTBApiClient
from app.services.ttb_sync import TTBSyncService, run_sync_all
from app.data.models.oauth_ttb import OAuthAccountTTB

logger = get_task_logger(__name__)


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


def _push_recent_job(workspace_id: int, auth_id: int, task_id: str, max_len: int = 200) -> None:
    backend = getattr(celery_app, "backend", None)
    client = getattr(backend, "client", None)
    if not client:
        return
    try:
        key = f"jobs:ttb:{workspace_id}:{auth_id}"
        client.lpush(key, task_id)
        client.ltrim(key, 0, max_len - 1)
    except Exception:  # noqa: BLE001
        pass


def _build_client(token: str) -> TTBApiClient:
    qps = float(getattr(settings, "TTB_QPS", 10.0))
    return TTBApiClient(access_token=token, qps=qps)


def _get_run(
    db: Session,
    *,
    schedule_run_id: Optional[int],
    idempotency_key: Optional[str],
) -> Optional[ScheduleRun]:
    if schedule_run_id:
        return db.get(ScheduleRun, int(schedule_run_id))
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


def _mark_run_consumed(
    db: Session,
    *,
    schedule_run_id: Optional[int],
    idempotency_key: Optional[str],
    broker_id: str,
) -> Optional[ScheduleRun]:
    run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
    if run:
        run.status = "consumed"
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
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    if not run:
        return
    run.status = status
    run.duration_ms = duration_ms
    run.error_code = error_code
    run.error_message = error_message[:512] if error_message else None
    payload: Dict[str, Any] = {"retries": retries}
    if stats:
        payload.update(stats)
    _merge_stats(run, payload)


def _audit_event(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    event: str,
    schedule_run_id: Optional[int],
    details: Optional[Dict[str, Any]] = None,
) -> None:
    payload = {"auth_id": int(auth_id)}
    if schedule_run_id:
        payload["schedule_run_id"] = int(schedule_run_id)
    if details:
        payload.update(details)
    log_event(
        db,
        action=event,
        resource_type="ttb_sync",
        resource_id=schedule_run_id,
        actor_workspace_id=int(workspace_id),
        workspace_id=int(workspace_id),
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


def _mark_account_invalid(db: Session, auth_id: int, reason: str | None = None) -> None:
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc or acc.status == "invalid":
        return
    acc.status = "invalid"
    db.add(acc)


class TTBSyncTask(Task):
    abstract = True
    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = 60
    retry_jitter = True

    def on_retry(self, exc, task_id, args, kwargs, einfo):  # type: ignore[override]
        workspace_id = kwargs.get("workspace_id")
        auth_id = kwargs.get("auth_id")
        schedule_run_id = kwargs.get("schedule_run_id")
        idempotency_key = kwargs.get("idempotency_key")

        db = _db_session()
        retry_count: Optional[int] = None
        try:
            run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
            if run:
                retry_count = int((run.stats_json or {}).get("retries", 0)) + 1
                _merge_stats(run, {"retries": retry_count, "last_error": str(exc)[:256]})
                run.status = "enqueued"
                db.add(run)
            if workspace_id and auth_id:
                _audit_event(
                    db,
                    workspace_id=int(workspace_id),
                    auth_id=int(auth_id),
                    event=f"{self.name}.retry",
                    schedule_run_id=run.id if run else schedule_run_id,
                    details={
                        "error": str(exc)[:256],
                        "retry": retry_count,
                    },
                )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("ttb sync retry hook failed", extra={"task": self.name})
        finally:
            _db_close(db)
        super().on_retry(exc, task_id, args, kwargs, einfo)


@celery_app.task(name="ttb.sync.bc", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_bc(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    schedule_run_id: Optional[int] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    _push_recent_job(workspace_id, auth_id, self.request.id)
    db = _db_session()
    client: Optional[TTBApiClient] = None
    run: Optional[ScheduleRun] = None
    started_ns = time.perf_counter_ns()
    result: Dict[str, Any] = {}
    try:
        run = _mark_run_consumed(
            db,
            schedule_run_id=schedule_run_id,
            idempotency_key=idempotency_key,
            broker_id=self.request.id,
        )
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.bc.start",
            schedule_run_id=run.id if run else schedule_run_id,
            details={"schedule_id": schedule_id},
        )
        lock_key = binding_action_lock_key(workspace_id, auth_id, "bc")
        with mysql_advisory_lock(db, lock_key, wait_seconds=5) as got:
            if not got:
                msg = "another bc job running for this binding"
                logger.warning(
                    "ttb sync bc skipped due to concurrent job",
                    extra={"workspace_id": workspace_id, "auth_id": auth_id},
                )
                if run:
                    duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
                    _finish_run(
                        run,
                        status="skipped",
                        duration_ms=duration_ms,
                        retries=self.request.retries,
                        error_code="lock_not_acquired",
                        error_message=msg,
                    )
                    db.add(run)
                    _audit_event(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        event="ttb.sync.bc.skipped",
                        schedule_run_id=run.id,
                        details={"reason": msg},
                    )
                    db.commit()
                return {"error": msg}
            token = get_access_token_for_auth_id(db, int(auth_id))
            client = _build_client(token)
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            limit = int(params.get("limit") or 200)
            result = asyncio.run(svc.sync_bc(limit=limit))
            asyncio.run(client.aclose())
            client = None
            db.flush()
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        if run:
            _finish_run(
                run,
                status="success",
                duration_ms=duration_ms,
                retries=self.request.retries,
                stats={"phases": {"bc": result}},
            )
            db.add(run)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.bc.success",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "duration_ms": duration_ms,
                "fetched": result.get("fetched"),
                "upserts": result.get("upserts"),
            },
        )
        db.commit()
        return {"result": result}
    except APIError as exc:
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=str(exc.code),
                error_message=exc.message,
                stats={"phases": {"bc": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, exc.message)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.bc.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": exc.message,
                "code": exc.code,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                stats={"phases": {"bc": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, str(exc))
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.bc.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": str(exc)[:256],
                "code": exc.__class__.__name__,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    finally:
        if client:
            with contextlib.suppress(Exception):
                asyncio.run(client.aclose())
        _db_close(db)


@celery_app.task(name="ttb.sync.advertisers", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_advertisers(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    schedule_run_id: Optional[int] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    _push_recent_job(workspace_id, auth_id, self.request.id)
    db = _db_session()
    client: Optional[TTBApiClient] = None
    run: Optional[ScheduleRun] = None
    started_ns = time.perf_counter_ns()
    result: Dict[str, Any] = {}
    try:
        run = _mark_run_consumed(
            db,
            schedule_run_id=schedule_run_id,
            idempotency_key=idempotency_key,
            broker_id=self.request.id,
        )
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.advertisers.start",
            schedule_run_id=run.id if run else schedule_run_id,
            details={"schedule_id": schedule_id},
        )
        lock_key = binding_action_lock_key(workspace_id, auth_id, "advertisers")
        with mysql_advisory_lock(db, lock_key, wait_seconds=5) as got:
            if not got:
                msg = "another advertisers job running for this binding"
                logger.warning(
                    "ttb sync advertisers skipped due to concurrent job",
                    extra={"workspace_id": workspace_id, "auth_id": auth_id},
                )
                if run:
                    duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
                    _finish_run(
                        run,
                        status="skipped",
                        duration_ms=duration_ms,
                        retries=self.request.retries,
                        error_code="lock_not_acquired",
                        error_message=msg,
                    )
                    db.add(run)
                    _audit_event(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        event="ttb.sync.advertisers.skipped",
                        schedule_run_id=run.id,
                        details={"reason": msg},
                    )
                    db.commit()
                return {"error": msg}
            token = get_access_token_for_auth_id(db, int(auth_id))
            app_id, secret, _ = get_credentials_for_auth_id(db, int(auth_id))
            client = _build_client(token)
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            limit = int(params.get("limit") or 200)
            result = asyncio.run(svc.sync_advertisers(limit=limit, app_id=app_id, secret=secret))
            asyncio.run(client.aclose())
            client = None
            db.flush()
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        if run:
            _finish_run(
                run,
                status="success",
                duration_ms=duration_ms,
                retries=self.request.retries,
                stats={"phases": {"advertisers": result}},
            )
            db.add(run)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.advertisers.success",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "duration_ms": duration_ms,
                "fetched": result.get("fetched"),
                "upserts": result.get("upserts"),
            },
        )
        db.commit()
        return {"result": result}
    except APIError as exc:
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=str(exc.code),
                error_message=exc.message,
                stats={"phases": {"advertisers": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, exc.message)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.advertisers.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": exc.message,
                "code": exc.code,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                stats={"phases": {"advertisers": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, str(exc))
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.advertisers.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": str(exc)[:256],
                "code": exc.__class__.__name__,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    finally:
        if client:
            with contextlib.suppress(Exception):
                asyncio.run(client.aclose())
        _db_close(db)


@celery_app.task(name="ttb.sync.shops", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_shops(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    schedule_run_id: Optional[int] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    _push_recent_job(workspace_id, auth_id, self.request.id)
    db = _db_session()
    client: Optional[TTBApiClient] = None
    run: Optional[ScheduleRun] = None
    started_ns = time.perf_counter_ns()
    result: Dict[str, Any] = {}
    try:
        run = _mark_run_consumed(
            db,
            schedule_run_id=schedule_run_id,
            idempotency_key=idempotency_key,
            broker_id=self.request.id,
        )
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.shops.start",
            schedule_run_id=run.id if run else schedule_run_id,
            details={"schedule_id": schedule_id},
        )
        lock_key = binding_action_lock_key(workspace_id, auth_id, "shops")
        with mysql_advisory_lock(db, lock_key, wait_seconds=5) as got:
            if not got:
                msg = "another shops job running for this binding"
                logger.warning(
                    "ttb sync shops skipped due to concurrent job",
                    extra={"workspace_id": workspace_id, "auth_id": auth_id},
                )
                if run:
                    duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
                    _finish_run(
                        run,
                        status="skipped",
                        duration_ms=duration_ms,
                        retries=self.request.retries,
                        error_code="lock_not_acquired",
                        error_message=msg,
                    )
                    db.add(run)
                    _audit_event(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        event="ttb.sync.shops.skipped",
                        schedule_run_id=run.id,
                        details={"reason": msg},
                    )
                    db.commit()
                return {"error": msg}
            token = get_access_token_for_auth_id(db, int(auth_id))
            client = _build_client(token)
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            limit = int(params.get("limit") or 200)
            result = asyncio.run(svc.sync_shops(limit=limit))
            asyncio.run(client.aclose())
            client = None
            db.flush()
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        if run:
            _finish_run(
                run,
                status="success",
                duration_ms=duration_ms,
                retries=self.request.retries,
                stats={"phases": {"shops": result}},
            )
            db.add(run)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.shops.success",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "duration_ms": duration_ms,
                "fetched": result.get("fetched"),
                "upserts": result.get("upserts"),
            },
        )
        db.commit()
        return {"result": result}
    except APIError as exc:
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=str(exc.code),
                error_message=exc.message,
                stats={"phases": {"shops": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, exc.message)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.shops.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": exc.message,
                "code": exc.code,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                stats={"phases": {"shops": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, str(exc))
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.shops.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": str(exc)[:256],
                "code": exc.__class__.__name__,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    finally:
        if client:
            with contextlib.suppress(Exception):
                asyncio.run(client.aclose())
        _db_close(db)


@celery_app.task(name="ttb.sync.products", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_products(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    schedule_run_id: Optional[int] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    _push_recent_job(workspace_id, auth_id, self.request.id)
    db = _db_session()
    client: Optional[TTBApiClient] = None
    run: Optional[ScheduleRun] = None
    started_ns = time.perf_counter_ns()
    result: Dict[str, Any] = {}
    try:
        run = _mark_run_consumed(
            db,
            schedule_run_id=schedule_run_id,
            idempotency_key=idempotency_key,
            broker_id=self.request.id,
        )
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.products.start",
            schedule_run_id=run.id if run else schedule_run_id,
            details={"schedule_id": schedule_id},
        )
        lock_key = binding_action_lock_key(workspace_id, auth_id, "products")
        with mysql_advisory_lock(db, lock_key, wait_seconds=5) as got:
            if not got:
                msg = "another products job running for this binding"
                logger.warning(
                    "ttb sync products skipped due to concurrent job",
                    extra={"workspace_id": workspace_id, "auth_id": auth_id},
                )
                if run:
                    duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
                    _finish_run(
                        run,
                        status="skipped",
                        duration_ms=duration_ms,
                        retries=self.request.retries,
                        error_code="lock_not_acquired",
                        error_message=msg,
                    )
                    db.add(run)
                    _audit_event(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        event="ttb.sync.products.skipped",
                        schedule_run_id=run.id,
                        details={"reason": msg},
                    )
                    db.commit()
                return {"error": msg}
            token = get_access_token_for_auth_id(db, int(auth_id))
            client = _build_client(token)
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            limit = int(params.get("limit") or 200)
            shop_id = params.get("shop_id")
            result = asyncio.run(svc.sync_products(limit=limit, shop_id=shop_id))
            asyncio.run(client.aclose())
            client = None
            db.flush()
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        if run:
            _finish_run(
                run,
                status="success",
                duration_ms=duration_ms,
                retries=self.request.retries,
                stats={"phases": {"products": result}},
            )
            db.add(run)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.products.success",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "duration_ms": duration_ms,
                "fetched": result.get("fetched"),
                "upserts": result.get("upserts"),
            },
        )
        db.commit()
        return {"result": result}
    except APIError as exc:
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=str(exc.code),
                error_message=exc.message,
                stats={"phases": {"products": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, exc.message)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.products.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": exc.message,
                "code": exc.code,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                stats={"phases": {"products": result}},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, str(exc))
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.products.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": str(exc)[:256],
                "code": exc.__class__.__name__,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    finally:
        if client:
            with contextlib.suppress(Exception):
                asyncio.run(client.aclose())
        _db_close(db)


@celery_app.task(name="ttb.sync.all", base=TTBSyncTask, bind=True, queue="gmv.tasks.events")
def task_sync_all(
    self,
    *,
    workspace_id: int,
    auth_id: int,
    schedule_run_id: Optional[int] = None,
    schedule_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    _push_recent_job(workspace_id, auth_id, self.request.id)
    db = _db_session()
    client: Optional[TTBApiClient] = None
    run: Optional[ScheduleRun] = None
    started_ns = time.perf_counter_ns()
    phases: Dict[str, Any] = {}
    try:
        run = _mark_run_consumed(
            db,
            schedule_run_id=schedule_run_id,
            idempotency_key=idempotency_key,
            broker_id=self.request.id,
        )
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.all.start",
            schedule_run_id=run.id if run else schedule_run_id,
            details={"schedule_id": schedule_id, "scope": params.get("scope", "all")},
        )
        lock_key = binding_action_lock_key(workspace_id, auth_id, "all")
        with mysql_advisory_lock(db, lock_key, wait_seconds=5) as got:
            if not got:
                msg = "another sync job running for this binding"
                logger.warning(
                    "ttb sync all skipped due to concurrent job",
                    extra={"workspace_id": workspace_id, "auth_id": auth_id},
                )
                if run:
                    duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
                    _finish_run(
                        run,
                        status="skipped",
                        duration_ms=duration_ms,
                        retries=self.request.retries,
                        error_code="lock_not_acquired",
                        error_message=msg,
                    )
                    db.add(run)
                    _audit_event(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        event="ttb.sync.all.skipped",
                        schedule_run_id=run.id,
                        details={"reason": msg},
                    )
                    db.commit()
                return {"error": msg}
            token = get_access_token_for_auth_id(db, int(auth_id))
            app_id, secret, _ = get_credentials_for_auth_id(db, int(auth_id))
            client = _build_client(token)
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            limit = int(params.get("limit") or 200)
            product_limit = params.get("product_limit")
            phases = asyncio.run(
                run_sync_all(
                    svc,
                    limit=limit,
                    app_id=app_id,
                    secret=secret,
                    product_limit=int(product_limit) if product_limit else None,
                )
            )
            asyncio.run(client.aclose())
            client = None
            db.flush()
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        if run:
            _finish_run(
                run,
                status="success",
                duration_ms=duration_ms,
                retries=self.request.retries,
                stats={"phases": phases},
            )
            db.add(run)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.all.success",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "duration_ms": duration_ms,
                "phases": {k: {"fetched": v.get("fetched"), "upserts": v.get("upserts")} for k, v in phases.items()},
            },
        )
        db.commit()
        return {"result": {"phases": phases}}
    except APIError as exc:
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=str(exc.code),
                error_message=exc.message,
                stats={"phases": phases},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, exc.message)
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.all.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": exc.message,
                "code": exc.code,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = _get_run(db, schedule_run_id=schedule_run_id, idempotency_key=idempotency_key)
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        mark_invalid = _should_mark_invalid(exc)
        if run:
            _finish_run(
                run,
                status="failed",
                duration_ms=duration_ms,
                retries=self.request.retries,
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                stats={"phases": phases},
            )
            db.add(run)
        if mark_invalid:
            _mark_account_invalid(db, auth_id, str(exc))
        _audit_event(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            event="ttb.sync.all.failed",
            schedule_run_id=run.id if run else schedule_run_id,
            details={
                "error": str(exc)[:256],
                "code": exc.__class__.__name__,
                "account_invalidated": mark_invalid,
            },
        )
        db.commit()
        raise
    finally:
        if client:
            with contextlib.suppress(Exception):
                asyncio.run(client.aclose())
        _db_close(db)
