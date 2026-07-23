from __future__ import annotations

import asyncio
import time
from typing import Any

from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import SessionLocal
from app.data.models.kie_api import KieApiKey, KieTask
from app.services.globalaiopc.client import GlobalAiOpcApiError
from app.services.globalaiopc.tasks import (
    LOCAL_TASK_PREFIX,
    refresh_GlobalAiOpc_task_status,
    reset_GlobalAiOpc_task_for_retry,
    submit_GlobalAiOpc_task,
)
from app.services.kie_api.accounts import GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY
from app.services.kie_api.retry_policy import (
    MAX_AUTO_RETRIES,
    delete_task_result_files,
    retry_count,
    should_auto_retry,
)
from app.tasks.kie_ai.video_result_download_tasks import queue_task_result_download

logger = get_task_logger(__name__)


def _db_session() -> Session:
    return SessionLocal()


def _load_task(db: Session, *, workspace_id: int, local_task_id: int) -> KieTask:
    task = (
        db.query(KieTask)
        .join(KieApiKey, KieTask.key_id == KieApiKey.id)
        .filter(
            KieTask.id == int(local_task_id),
            KieTask.workspace_id == int(workspace_id),
            KieApiKey.provider_key == GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY,
        )
        .one_or_none()
    )
    if task is None:
        raise ValueError("GlobalAiOpc task not found")
    return task


def _payload(task: KieTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_id": task.task_id,
        "state": task.state,
        "fail_code": task.fail_code,
        "fail_msg": task.fail_msg,
    }


def _mark_failed(db: Session, task: KieTask, exc: Exception) -> dict[str, Any]:
    task.state = "failed"
    task.fail_code = task.fail_code or "globalaiopc_worker_error"
    task.fail_msg = str(exc)[:512]
    db.add(task)
    db.commit()
    return _payload(task)


def _auto_retry_in_place(db: Session, task: KieTask) -> KieTask:
    logger.info(
        "GlobalAiOpc task failed, auto retrying in place",
        extra={
            "workspace_id": task.workspace_id,
            "local_task_id": task.id,
            "state": task.state,
            "fail_code": task.fail_code,
            "fail_msg": task.fail_msg,
            "auto_retry": retry_count(task, "auto") + 1,
        },
    )
    delete_task_result_files(db, task)
    task = reset_GlobalAiOpc_task_for_retry(db, task=task, retry_kind="auto")
    task = asyncio.run(submit_GlobalAiOpc_task(db, task=task))
    db.commit()
    return task


@celery_app.task(
    name="globalaiopc.video.submit_and_poll",
    bind=True,
    queue="gmv.tasks.ai_video",
    max_retries=MAX_AUTO_RETRIES,
    default_retry_delay=15,
)
def submit_and_poll_GlobalAiOpc_video_task(
    self,
    *,
    workspace_id: int,
    local_task_id: int,
    interval_seconds: int = 15,
    timeout_seconds: int = 10 * 60,
    **_: Any,
) -> dict[str, Any]:
    db = _db_session()
    start_ts = time.monotonic()

    try:
        try:
            task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
            if str(task.task_id or "").startswith(LOCAL_TASK_PREFIX):
                task = asyncio.run(submit_GlobalAiOpc_task(db, task=task))
                db.commit()
                if str(task.state or "").lower() == "downloading":
                    queue_task_result_download(
                        workspace_id=int(workspace_id),
                        local_task_id=int(local_task_id),
                    )
                    return _payload(task)
        except GlobalAiOpcApiError as exc:
            db.rollback()
            task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
            if self.request.retries >= self.max_retries:
                return _mark_failed(db, task, exc)
            raise self.retry(exc=exc)

        while True:
            try:
                task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                task = asyncio.run(refresh_GlobalAiOpc_task_status(db, task=task))
                db.commit()
            except GlobalAiOpcApiError as exc:
                db.rollback()
                if self.request.retries >= self.max_retries:
                    task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                    return _mark_failed(db, task, exc)
                logger.warning(
                    "GlobalAiOpc query error, will retry",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "error": str(exc),
                    },
                )
                raise self.retry(exc=exc)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "GlobalAiOpc polling iteration failed",
                    extra={"workspace_id": workspace_id, "local_task_id": local_task_id},
                )
                raise exc

            state = (task.state or "").lower()
            if state == "downloading":
                queue_task_result_download(
                    workspace_id=int(workspace_id),
                    local_task_id=int(local_task_id),
                )
                return _payload(task)

            if state in {"failed", "error", "timeout"} and should_auto_retry(task):
                try:
                    task = _auto_retry_in_place(db, task)
                    start_ts = time.monotonic()
                    if str(task.state or "").lower() == "downloading":
                        queue_task_result_download(
                            workspace_id=int(workspace_id),
                            local_task_id=int(local_task_id),
                        )
                        return _payload(task)
                    continue
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    task = _load_task(db, workspace_id=workspace_id, local_task_id=local_task_id)
                    return _mark_failed(db, task, exc)

            if state in {"success", "failed", "error", "timeout"}:
                logger.info(
                    "GlobalAiOpc task reached terminal state",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "state": state,
                        "fail_code": task.fail_code,
                        "fail_msg": task.fail_msg,
                    },
                )
                return _payload(task)

            if time.monotonic() - start_ts > timeout_seconds:
                task.state = "in_progress"
                db.add(task)
                db.commit()
                submit_and_poll_GlobalAiOpc_video_task.apply_async(
                    kwargs={
                        "workspace_id": int(workspace_id),
                        "local_task_id": int(local_task_id),
                        "interval_seconds": int(interval_seconds),
                        "timeout_seconds": int(timeout_seconds),
                    },
                    countdown=30,
                    queue="gmv.tasks.ai_video",
                )
                return _payload(task)

            time.sleep(max(5, int(interval_seconds)))

    finally:
        db.close()
