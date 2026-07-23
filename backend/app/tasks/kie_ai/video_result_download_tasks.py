from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.data.models.kie_api import KieFile, KieTask
from app.services.kie_api.local_storage import (
    RESULT_FILE_KINDS,
    get_local_path,
    get_task_local_meta,
    get_task_download_filename,
    save_remote_file_locally,
    set_task_local_meta,
)
from app.services.kie_api.retry_policy import MAX_AUTO_RETRIES

logger = get_task_logger(__name__)


def _parse_meta_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _db_session() -> Session:
    return SessionLocal()


def _load_task(
    db: Session,
    *,
    workspace_id: int,
    local_task_id: int,
    for_update: bool = False,
) -> KieTask | None:
    query = (
        db.query(KieTask)
        .filter(
            KieTask.id == int(local_task_id),
            KieTask.workspace_id == int(workspace_id),
        )
    )
    if for_update:
        query = query.populate_existing().with_for_update()
    return query.one_or_none()


def _load_result_files(db: Session, *, task_id: int) -> list[KieFile]:
    return (
        db.query(KieFile)
        .filter(
            KieFile.task_id == int(task_id),
            KieFile.kind.in_(tuple(sorted(RESULT_FILE_KINDS))),
        )
        .order_by(KieFile.id.asc())
        .all()
    )


def _serialize_task(task: KieTask) -> dict[str, Any]:
    return {
        "id": int(task.id),
        "task_id": str(task.task_id),
        "state": str(task.state or ""),
        "fail_code": task.fail_code,
        "fail_msg": task.fail_msg,
    }


def _mark_failed(db: Session, task: KieTask, *, code: str, message: str) -> dict[str, Any]:
    task.state = "failed"
    task.updated_at = datetime.now()
    task.fail_code = code
    task.fail_msg = str(message or "Local download failed")[:512]
    set_task_local_meta(
        task,
        download_enqueued_at=None,
        download_started_at=None,
        download_execution_token=None,
        download_finished_at=datetime.now(timezone.utc).isoformat(),
        download_error=task.fail_msg,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _serialize_task(task)


def _mark_success(db: Session, task: KieTask) -> dict[str, Any]:
    task.state = "success"
    task.updated_at = datetime.now()
    task.fail_code = None
    task.fail_msg = None
    set_task_local_meta(
        task,
        download_enqueued_at=None,
        download_started_at=None,
        download_execution_token=None,
        download_finished_at=datetime.now(timezone.utc).isoformat(),
        download_error=None,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return _serialize_task(task)


def queue_task_result_download(*, workspace_id: int, local_task_id: int, force: bool = False) -> None:
    download_task_id = str(uuid4())
    remote_task_id = ""
    db = _db_session()
    try:
        task = (
            db.query(KieTask)
            .filter(KieTask.id == int(local_task_id), KieTask.workspace_id == int(workspace_id))
            .with_for_update()
            .one_or_none()
        )
        if task is None or str(task.state or "").lower() == "success":
            return
        meta = dict(get_task_local_meta(task) or {})
        enqueued_raw = str(meta.get("download_enqueued_at") or "")
        try:
            enqueued_at = datetime.fromisoformat(enqueued_raw.replace("Z", "+00:00"))
            if enqueued_at.tzinfo is None:
                enqueued_at = enqueued_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            enqueued_at = None
        if enqueued_at and not force and enqueued_at > datetime.now(timezone.utc) - timedelta(minutes=3):
            return
        remote_task_id = str(task.task_id or "")
        set_task_local_meta(
            task,
            download_enqueued_at=datetime.now(timezone.utc).isoformat(),
            download_task_id=download_task_id,
            download_remote_task_id=remote_task_id,
            download_started_at=None,
            download_execution_token=None,
        )
        task.updated_at = datetime.now()
        db.add(task)
        db.commit()
    finally:
        db.close()
    try:
        download_task_result_files.apply_async(
            kwargs={
                "workspace_id": int(workspace_id),
                "local_task_id": int(local_task_id),
                "remote_task_id": remote_task_id,
            },
            # Downloads belong to the provider-neutral AI-video worker. Sending this to the
            # generic default queue can leave a provider-completed video in
            # ``downloading`` forever when that optional queue has no running
            # consumer.  Keep enqueue, stale recovery, execution, and retry
            # on one durable worker queue.
            queue="gmv.tasks.ai_video",
            task_id=download_task_id,
        )
    except Exception:
        db = _db_session()
        try:
            task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
            if task is not None:
                meta = dict(get_task_local_meta(task) or {})
                if str(meta.get("download_task_id") or "") == download_task_id:
                    set_task_local_meta(
                        task,
                        download_enqueued_at=None,
                        download_task_id=None,
                        download_remote_task_id=None,
                    )
                    db.add(task)
                    db.commit()
        finally:
            db.close()
        raise


@celery_app.task(
    name="ai_video.result.recover_stale_downloads",
    queue="gmv.tasks.ai_video",
)
def recover_stale_result_downloads(*, stale_minutes: int = 10, limit: int = 200) -> dict[str, Any]:
    db = _db_session()
    try:
        now = datetime.now()
        cutoff = now - timedelta(minutes=max(1, int(stale_minutes)))
        tasks = (
            db.query(KieTask)
            .filter(
                KieTask.state == "downloading",
            )
            .order_by(KieTask.updated_at.asc())
            .limit(max(1, min(int(limit), 500)))
            .all()
        )
        queued_ids: list[int] = []
        finalized_ids: list[int] = []
        for candidate in tasks:
            task = (
                db.query(KieTask)
                .filter(
                    KieTask.id == int(candidate.id),
                    KieTask.state == "downloading",
                )
                .with_for_update(skip_locked=True)
                .one_or_none()
            )
            if task is None:
                continue
            files = _load_result_files(db, task_id=int(task.id))
            if files and all(get_local_path(file) is not None for file in files):
                _mark_success(db, task)
                finalized_ids.append(int(task.id))
                continue
            if not files:
                continue
            task_meta = dict(get_task_local_meta(task) or {})
            enqueued_at = _parse_meta_time(task_meta.get("download_enqueued_at"))
            started_at = _parse_meta_time(task_meta.get("download_started_at"))
            has_download_task_id = bool(str(task_meta.get("download_task_id") or "").strip())
            missing_files = [file for file in files if get_local_path(file) is None]
            missing_statuses = {
                str(dict(file.meta_json or {}).get("local_download_status") or "").lower()
                for file in missing_files
            }
            should_recover = (
                task.updated_at is None
                or task.updated_at <= cutoff
                or not has_download_task_id
                or (enqueued_at is not None and enqueued_at <= cutoff)
                or (started_at is not None and started_at <= cutoff)
                or (
                    enqueued_at is None
                    and started_at is None
                    and bool(missing_statuses.intersection({"queued", "failed", "downloading", ""}))
                    and task.updated_at is not None
                    and task.updated_at <= cutoff
                )
            )
            if not should_recover:
                continue
            set_task_local_meta(
                task,
                download_recovered_at=datetime.now(timezone.utc).isoformat(),
            )
            task.updated_at = now
            db.add(task)
            db.commit()
            queue_task_result_download(
                workspace_id=int(task.workspace_id),
                local_task_id=int(task.id),
                force=True,
            )
            queued_ids.append(int(task.id))
        return {
            "queued": len(queued_ids),
            "task_ids": queued_ids,
            "finalized": len(finalized_ids),
            "finalized_task_ids": finalized_ids,
        }
    finally:
        db.close()


def _retry_or_fail(
    self,
    db: Session,
    task: KieTask,
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    if self.request.retries < self.max_retries:
        task.state = "downloading"
        task.updated_at = datetime.now()
        task.fail_code = None
        task.fail_msg = None
        set_task_local_meta(
            task,
            download_error=str(message)[:512],
            download_finished_at=None,
            download_started_at=None,
            download_execution_token=None,
        )
        db.add(task)
        db.commit()
        raise self.retry(exc=RuntimeError(message))
    return _mark_failed(db, task, code=code, message=message)


@celery_app.task(
    name="ai_video.result.download_task_result_files",
    bind=True,
    queue="gmv.tasks.ai_video",
    max_retries=MAX_AUTO_RETRIES,
    default_retry_delay=10,
)
def download_task_result_files(
    self,
    *,
    workspace_id: int,
    local_task_id: int,
    remote_task_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    db = _db_session()
    try:
        task = _load_task(
            db,
            workspace_id=workspace_id,
            local_task_id=local_task_id,
            for_update=True,
        )
        if task is None:
            return {
                "id": int(local_task_id),
                "task_id": "",
                "state": "missing",
                "fail_code": "task_not_found",
                "fail_msg": "Task not found",
            }

        request_id = str(getattr(self.request, "id", "") or "")
        task_meta = dict(get_task_local_meta(task) or {})
        registered_id = str(task_meta.get("download_task_id") or "")
        if registered_id and request_id and registered_id != request_id:
            return {
                **_serialize_task(task),
                "state": "ignored_superseded_download",
                "registered_task_id": registered_id,
                "request_id": request_id,
            }

        expected_remote_task_id = str(
            remote_task_id or task_meta.get("download_remote_task_id") or ""
        ).strip()
        if expected_remote_task_id and str(task.task_id or "") != expected_remote_task_id:
            return {
                **_serialize_task(task),
                "state": "ignored_superseded_generation",
                "expected_remote_task_id": expected_remote_task_id,
                "current_remote_task_id": str(task.task_id or ""),
            }

        files = _load_result_files(db, task_id=int(task.id))
        if not files:
            return _retry_or_fail(
                self,
                db,
                task,
                code="missing_result_file",
                message="No result file is available for local download.",
            )

        current_meta = dict(get_task_local_meta(task) or {})
        existing_execution_token = str(current_meta.get("download_execution_token") or "").strip()
        existing_started_at = _parse_meta_time(current_meta.get("download_started_at"))
        active_cutoff = datetime.now() - timedelta(
            seconds=max(60, int(getattr(settings, "AI_VIDEO_RESULT_DOWNLOAD_TIMEOUT_SECONDS", 600)))
        )
        if (
            existing_execution_token
            and existing_started_at is not None
            and existing_started_at > active_cutoff
        ):
            db.commit()
            return {
                **_serialize_task(task),
                "state": "ignored_active_download",
                "registered_task_id": registered_id,
            }
        execution_token = str(uuid4())
        set_task_local_meta(
            task,
            download_started_at=datetime.now(timezone.utc).isoformat(),
            download_execution_token=execution_token,
            download_finished_at=None,
            download_error=None,
        )
        task.updated_at = datetime.now()
        db.add(task)
        db.commit()
        db.refresh(task)

        for file in files:
            file_id = int(file.id)
            if get_local_path(file) is not None:
                continue
            asyncio.run(
                save_remote_file_locally(
                    db,
                    file=file,
                    owner_user_id=task.created_by_user_id,
                    preferred_filename=get_task_download_filename(task, file),
                )
            )
            refreshed_file = db.get(KieFile, file_id)
            if refreshed_file is None or get_local_path(refreshed_file) is None:
                current_task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                if current_task is None:
                    return {
                        "id": int(local_task_id),
                        "task_id": "",
                        "state": "missing",
                        "fail_code": "task_not_found",
                        "fail_msg": "Task not found",
                    }
                current_file = refreshed_file or db.query(KieFile).filter(KieFile.id == file_id).one_or_none()
                meta = dict(current_file.meta_json or {}) if current_file is not None else {}
                error_text = str(meta.get("local_download_error") or "Local download failed")
                return _retry_or_fail(
                    self,
                    db,
                    current_task,
                    code="local_download_failed",
                    message=error_text,
                )

        current_task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
        if current_task is None:
            return {
                "id": int(local_task_id),
                "task_id": "",
                "state": "missing",
                "fail_code": "task_not_found",
                "fail_msg": "Task not found",
            }

        if expected_remote_task_id and str(current_task.task_id or "") != expected_remote_task_id:
            return {
                **_serialize_task(current_task),
                "state": "ignored_superseded_generation",
                "expected_remote_task_id": expected_remote_task_id,
                "current_remote_task_id": str(current_task.task_id or ""),
            }

        # The locally persisted result is the success authority for this
        # immutable provider generation.  A concurrent poll/recovery writer
        # can legitimately replace or clear the advisory download lease while
        # network I/O is in flight.  Once every result row points to a real
        # local file, leaving the task in ``downloading`` solely because that
        # lease changed creates a permanent false-negative.  The remote task
        # identity check above still prevents a stale generation from winning.
        current_files = _load_result_files(db, task_id=int(current_task.id))
        missing_files = [
            file.id for file in current_files if get_local_path(file) is None
        ]
        if current_files and not missing_files:
            return _mark_success(db, current_task)

        current_meta = dict(get_task_local_meta(current_task) or {})
        if str(current_meta.get("download_execution_token") or "") != execution_token:
            return {
                **_serialize_task(current_task),
                "state": "ignored_superseded_download",
                "registered_task_id": str(current_meta.get("download_task_id") or ""),
                "request_id": request_id,
            }

        if missing_files:
            return _retry_or_fail(
                self,
                db,
                current_task,
                code="local_download_failed",
                message=f"Result files are still missing after download: {missing_files}",
            )

        return _mark_success(db, current_task)
    finally:
        db.close()
