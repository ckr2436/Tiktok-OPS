# app/tasks/kie_ai/sora/sora2_image_to_video_tasks.py
from __future__ import annotations

import asyncio
import time
from typing import Any

from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import SessionLocal
from app.services.kie_api.tasks import refresh_sora2_task_status_by_task_id
from app.services.kie_api.sora2 import KieApiError

logger = get_task_logger(__name__)


def _db_session() -> Session:
    return SessionLocal()


@celery_app.task(
    name="kie_ai.sora2.refresh_task_status_once",
    bind=True,
    queue="gmv.tasks.kie_ai",
)
def refresh_sora2_task_status_once(
    self,
    *,
    workspace_id: int,
    local_task_id: int,
    **_: Any,
) -> dict[str, Any]:
    """
    保留一个“单次刷新”的任务（兼容老逻辑/手动排查时用）。
    """
    db = _db_session()
    try:
        result = asyncio.run(
            refresh_sora2_task_status_by_task_id(
                db,
                workspace_id=workspace_id,
                local_task_id=local_task_id,
            )
        )
        db.commit()
        return {
            "id": result.id,
            "task_id": result.task_id,
            "state": result.state,
            "fail_code": result.fail_code,
            "fail_msg": result.fail_msg,
        }
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "refresh_sora2_task_status_once failed",
            extra={"workspace_id": workspace_id, "local_task_id": local_task_id},
        )
        raise exc
    finally:
        db.close()


@celery_app.task(
    name="kie_ai.sora2.poll_task_status",
    bind=True,
    queue="gmv.tasks.kie_ai",
    max_retries=3,
    default_retry_delay=10,
)
def poll_sora2_task_status(
    self,
    *,
    workspace_id: int,
    local_task_id: int,
    interval_seconds: int = 8,
    timeout_seconds: int = 20 * 60,  # 20 分钟上限
    **_: Any,
) -> dict[str, Any]:
    """
    生产级轮询任务：

    - 周期性调用 recordInfo 刷新本地 KieTask
    - 遇到终态（success / failed / error / timeout）就退出
    - 超时则把 state 标记为 timeout
    - 对 KieApiError 做有限次重试
    """
    db = _db_session()
    start_ts = time.monotonic()

    try:
        while True:
            try:
                task = asyncio.run(
                    refresh_sora2_task_status_by_task_id(
                        db,
                        workspace_id=workspace_id,
                        local_task_id=local_task_id,
                    )
                )
                db.commit()
            except KieApiError as exc:
                db.rollback()
                # 对外部 API 错误做有限次重试
                logger.warning(
                    "KIE recordInfo error, will retry",
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
                    "poll_sora2_task_status iteration failed",
                    extra={"workspace_id": workspace_id, "local_task_id": local_task_id},
                )
                raise exc

            state = (task.state or "").lower()

            # 终态：按 KIE 定义来，这里兜一下常见几种
            if state in {"success", "failed", "error", "timeout"}:
                logger.info(
                    "Sora2 task reached terminal state",
                    extra={
                        "workspace_id": workspace_id,
                        "local_task_id": local_task_id,
                        "state": state,
                        "fail_code": task.fail_code,
                        "fail_msg": task.fail_msg,
                    },
                )
                return {
                    "id": task.id,
                    "task_id": task.task_id,
                    "state": task.state,
                    "fail_code": task.fail_code,
                    "fail_msg": task.fail_msg,
                }

            # 超时保护
            if time.monotonic() - start_ts > timeout_seconds:
                logger.warning(
                    "Sora2 task polling timeout, mark as timeout",
                    extra={"workspace_id": workspace_id, "local_task_id": local_task_id},
                )
                task.state = "timeout"
                db.add(task)
                db.commit()
                return {
                    "id": task.id,
                    "task_id": task.task_id,
                    "state": task.state,
                    "fail_code": task.fail_code,
                    "fail_msg": task.fail_msg,
                }

            # 未结束：睡一会儿再查
            time.sleep(max(3, int(interval_seconds)))

    finally:
        db.close()

