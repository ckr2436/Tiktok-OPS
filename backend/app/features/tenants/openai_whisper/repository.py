"""Database helpers for persisting OpenAI Whisper jobs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.openai_whisper import OpenAIWhisperJob


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ensure_job(db: Session, workspace_id: int, job_id: str) -> OpenAIWhisperJob | None:
    stmt = select(OpenAIWhisperJob).where(
        OpenAIWhisperJob.workspace_id == int(workspace_id),
        OpenAIWhisperJob.job_id == job_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def create_job(db: Session, payload: dict) -> OpenAIWhisperJob:
    user_value = payload.get("user_id")
    job = OpenAIWhisperJob(
        job_id=str(payload.get("job_id")),
        workspace_id=int(payload.get("workspace_id")),
        user_id=int(user_value) if user_value is not None else None,
        filename=payload.get("filename"),
        file_size=payload.get("size"),
        content_type=payload.get("content_type"),
        video_path=payload.get("video_path"),
        status=str(payload.get("status", "pending")),
        source_language=payload.get("source_language"),
        target_language=payload.get("target_language"),
        translate=bool(payload.get("translate")),
        show_bilingual=bool(payload.get("show_bilingual")),
    )
    db.add(job)
    return job


def update_celery_task(
    db: Session,
    *,
    workspace_id: int,
    job_id: str,
    celery_task_id: str,
) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.celery_task_id = celery_task_id
    job.updated_at = _utcnow()
    db.add(job)
    return job


def mark_processing(db: Session, workspace_id: int, job_id: str) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.status = "processing"
    job.error = None
    if not job.started_at:
        job.started_at = _utcnow()
    job.updated_at = _utcnow()
    db.add(job)
    return job


def mark_failed(db: Session, workspace_id: int, job_id: str, message: str) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.status = "failed"
    job.error = message
    job.updated_at = _utcnow()
    db.add(job)
    return job


def mark_completed(
    db: Session,
    workspace_id: int,
    job_id: str,
    *,
    detected_language: str | None,
    translation_language: str | None,
    segments_count: Optional[int],
    translation_segments_count: Optional[int],
) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.status = "success"
    job.error = None
    job.detected_language = detected_language
    job.translation_language = translation_language
    job.segments_count = segments_count
    job.translation_segments_count = translation_segments_count
    job.completed_at = _utcnow()
    job.updated_at = _utcnow()
    db.add(job)
    return job


def list_jobs(db: Session, workspace_id: int, limit: int) -> Iterable[OpenAIWhisperJob]:
    stmt = (
        select(OpenAIWhisperJob)
        .where(OpenAIWhisperJob.workspace_id == int(workspace_id))
        .order_by(OpenAIWhisperJob.created_at.desc(), OpenAIWhisperJob.id.desc())
        .limit(limit)
    )
    return db.execute(stmt).scalars().all()


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def get_job(db: Session, workspace_id: int, job_id: str) -> OpenAIWhisperJob | None:
    return _ensure_job(db, workspace_id, job_id)
