from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.celery_app import celery_app
from app.core.config import settings
from app.core.errors import APIError
from app.data.db import SessionLocal
from app.data.models.tiktok_shop import TikTokShopVideoContentAnalysis
from app.services.tiktok_shop_video_analysis import (
    FINAL_STATUSES,
    run_analysis,
    utcnow,
)


logger = logging.getLogger("gmv.tasks.tiktok_shop_video_analysis")
QUEUE = str(settings.HERMES_VIDEO_ANALYSIS_TASK_QUEUE)


def _task_result(row: TikTokShopVideoContentAnalysis) -> dict[str, object]:
    # Celery stores and logs task return values. Keep business metrics, model
    # advice, creator metadata, and media identifiers in the tenant-scoped DB
    # response only; the queue result is deliberately minimal.
    return {"analysis_id": int(row.id), "status": str(row.status)}


@celery_app.task(
    name="tiktok_shop_video_analysis.run",
    bind=True,
    queue=QUEUE,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=8 * 60,
    time_limit=10 * 60,
)
def analyze_tiktok_shop_video(self, *, analysis_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        row = db.scalar(
            select(TikTokShopVideoContentAnalysis)
            .where(TikTokShopVideoContentAnalysis.id == int(analysis_id))
            .with_for_update()
        )
        if not row:
            return {"status": "missing", "analysis_id": int(analysis_id)}
        now = utcnow()
        if row.status in FINAL_STATUSES:
            return _task_result(row)
        if (
            row.status == "RUNNING"
            and row.lease_expires_at is not None
            and row.lease_expires_at > now
        ):
            return {"status": "already_running", "analysis_id": int(row.id)}
        if int(row.attempts or 0) >= 3:
            row.status = "FAILED"
            row.error_code = "ATTEMPTS_EXHAUSTED"
            row.error_message = "The bounded analysis retry budget was exhausted."
            row.completed_at = now
            row.lease_expires_at = None
            db.commit()
            return _task_result(row)
        row.status = "RUNNING"
        row.started_at = now
        row.completed_at = None
        row.error_code = None
        row.error_message = None
        row.attempts = int(row.attempts or 0) + 1
        row.lease_expires_at = now + timedelta(
            seconds=max(60, int(settings.HERMES_VIDEO_ANALYSIS_LEASE_SECONDS))
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        try:
            completed = asyncio.run(run_analysis(db, row))
            return _task_result(completed)
        except Exception as exc:
            db.rollback()
            failed = db.get(TikTokShopVideoContentAnalysis, int(analysis_id))
            if failed:
                failed.status = "FAILED"
                failed.error_code = exc.code if isinstance(exc, APIError) else type(exc).__name__[:64]
                failed.error_message = str(exc)[:1500]
                failed.completed_at = utcnow()
                failed.lease_expires_at = None
                db.add(failed)
                db.commit()
            logger.exception("TikTok Shop video analysis failed analysis_id=%s", analysis_id)
            return _task_result(failed) if failed else {
                "status": "failed",
                "analysis_id": int(analysis_id),
            }


__all__ = ["analyze_tiktok_shop_video"]
