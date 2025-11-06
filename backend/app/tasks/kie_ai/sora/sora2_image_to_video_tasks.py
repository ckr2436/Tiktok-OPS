# app/tasks/kie_ai/sora/sora2_image_to_video_tasks.py
from __future__ import annotations

import asyncio
from typing import Any

from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import SessionLocal
from app.services.kie_api.tasks import refresh_sora2_task_status_by_task_id

logger = get_task_logger(__name__)


def _db_session() -> Session:
    db = SessionLocal()
    return db


@celery_app.task(
    name="kie_ai.sora2.refresh_task_status",
    bind=True,
    queue="gmv.tasks.kie_ai",  # 你会起 gmv-celery-worker@gmv.tasks.kie_ai.service
)
def refresh_sora2_task_status_task(
    self,
    *,
    workspace_id: int,
    local_task_id: int,
    **_: Any,
) -> dict[str, Any]:
    """
    简单封装：异步刷新某个任务的状态。
    供后台定期轮询 / 手动触发使用。
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
            "refresh_sora2_task_status_task failed",
            extra={"workspace_id": workspace_id, "local_task_id": local_task_id},
        )
        raise exc
    finally:
        db.close()

