"""Database helpers for persisting OpenAI Whisper jobs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.data.models.openai_whisper import OpenAIWhisperJob

TERMINAL_STATUSES = {"success", "failed"}
ACTIVE_STATUSES = {"pending", "processing"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ensure_job(db: Session, workspace_id: int, job_id: str) -> OpenAIWhisperJob | None:
    stmt = select(OpenAIWhisperJob).where(
        OpenAIWhisperJob.workspace_id == int(workspace_id),
        OpenAIWhisperJob.job_id == job_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def _apply_overall_status(job: OpenAIWhisperJob) -> None:
    statuses = []
    for status in [job.subtitle_status, job.contact_sheet_status, job.download_status]:
        if status and status != "skipped":
            statuses.append(status)
    if not statuses:
        job.status = job.status or "pending"
        return
    if any(state == "failed" for state in statuses):
        job.status = "failed"
        return
    if any(state == "processing" for state in statuses):
        job.status = "processing"
        return
    if any(state == "pending" for state in statuses):
        job.status = "pending"
        return
    job.status = "success"


def create_job(db: Session, payload: dict) -> OpenAIWhisperJob:
    user_value = payload.get("user_id")
    do_subtitle = bool(payload.get("do_subtitle", True))
    do_contact_sheet = bool(payload.get("do_contact_sheet"))
    do_download_only = bool(payload.get("do_download_only"))
    subtitle_status = payload.get("subtitle_status") or ("pending" if do_subtitle else "skipped")
    contact_sheet_status = payload.get("contact_sheet_status") or (
        "pending" if do_contact_sheet else "skipped"
    )
    download_status = payload.get("download_status") or payload.get("download_state")
    if not download_status:
        download_status = "pending" if payload.get("share_url") else "skipped"
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
        do_subtitle=do_subtitle,
        do_contact_sheet=do_contact_sheet,
        do_download_only=do_download_only,
        contact_interval=payload.get("contact_interval"),
        subtitle_status=subtitle_status,
        contact_sheet_status=contact_sheet_status,
        download_status=download_status,
        subtitle_error=payload.get("subtitle_error"),
        contact_sheet_error=payload.get("contact_sheet_error"),
        download_error=payload.get("download_error"),
        contact_sheet_url=payload.get("contact_sheet_url"),
        download_url=payload.get("download_url"),
    )
    _apply_overall_status(job)
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
    job.subtitle_status = "processing"
    job.error = None
    job.subtitle_error = None
    if not job.started_at:
        job.started_at = _utcnow()
    job.updated_at = _utcnow()
    _apply_overall_status(job)
    db.add(job)
    return job


def mark_failed(db: Session, workspace_id: int, job_id: str, message: str) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.status = "failed"
    job.subtitle_status = "failed"
    job.error = message
    job.subtitle_error = message
    job.updated_at = _utcnow()
    _apply_overall_status(job)
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
    job.subtitle_status = "success"
    job.error = None
    job.subtitle_error = None
    job.detected_language = detected_language
    job.translation_language = translation_language
    job.segments_count = segments_count
    job.translation_segments_count = translation_segments_count
    job.completed_at = _utcnow()
    job.updated_at = _utcnow()
    _apply_overall_status(job)
    db.add(job)
    return job


def update_downloaded_file(
    db: Session,
    *,
    workspace_id: int,
    job_id: str,
    filename: str | None,
    file_size: int | None,
    content_type: str | None,
    video_path: str | None,
    download_url: str | None = None,
) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.filename = filename or job.filename
    job.file_size = file_size if file_size is not None else job.file_size
    job.content_type = content_type or job.content_type
    job.video_path = video_path or job.video_path
    job.download_url = download_url or job.download_url
    job.download_status = job.download_status or "success"
    job.download_error = None
    job.updated_at = _utcnow()
    _apply_overall_status(job)
    db.add(job)
    return job


def update_download_status(
    db: Session,
    *,
    workspace_id: int,
    job_id: str,
    status: str,
    message: str | None = None,
    download_url: str | None = None,
) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.download_status = status
    job.download_error = message
    if download_url is not None:
        job.download_url = download_url
    job.updated_at = _utcnow()
    _apply_overall_status(job)
    db.add(job)
    return job


def update_contact_sheet_status(
    db: Session,
    *,
    workspace_id: int,
    job_id: str,
    status: str,
    message: str | None = None,
    contact_sheet_url: str | None = None,
) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.contact_sheet_status = status
    job.contact_sheet_error = message
    if contact_sheet_url is not None:
        job.contact_sheet_url = contact_sheet_url
    job.updated_at = _utcnow()
    _apply_overall_status(job)
    db.add(job)
    return job


def clear_large_artifact_refs(db: Session, workspace_id: int, job_id: str) -> Optional[OpenAIWhisperJob]:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    job.video_path = None
    job.file_size = None
    job.download_url = None
    job.contact_sheet_url = None
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


def list_jobs_for_workspace(
    db: Session,
    workspace_id: int,
    *,
    include_active: bool = False,
    limit: int | None = None,
) -> list[OpenAIWhisperJob]:
    stmt = select(OpenAIWhisperJob).where(OpenAIWhisperJob.workspace_id == int(workspace_id))
    if not include_active:
        stmt = stmt.where(OpenAIWhisperJob.status.in_(TERMINAL_STATUSES))
    stmt = stmt.order_by(OpenAIWhisperJob.created_at.desc(), OpenAIWhisperJob.id.desc())
    if limit:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


def list_stale_active_jobs(db: Session, *, cutoff: datetime, limit: int) -> list[OpenAIWhisperJob]:
    stmt = (
        select(OpenAIWhisperJob)
        .where(OpenAIWhisperJob.status.in_(ACTIVE_STATUSES), OpenAIWhisperJob.updated_at < cutoff)
        .order_by(OpenAIWhisperJob.updated_at.asc(), OpenAIWhisperJob.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def list_jobs_for_large_artifact_cleanup(db: Session, *, cutoff: datetime, limit: int) -> list[OpenAIWhisperJob]:
    stmt = (
        select(OpenAIWhisperJob)
        .where(
            OpenAIWhisperJob.status == "success",
            OpenAIWhisperJob.updated_at < cutoff,
            or_(
                OpenAIWhisperJob.video_path.is_not(None),
                OpenAIWhisperJob.download_url.is_not(None),
                OpenAIWhisperJob.contact_sheet_url.is_not(None),
            ),
        )
        .order_by(OpenAIWhisperJob.updated_at.asc(), OpenAIWhisperJob.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def list_expired_terminal_jobs(
    db: Session,
    *,
    success_cutoff: datetime,
    failed_cutoff: datetime,
    limit: int,
) -> list[OpenAIWhisperJob]:
    stmt = (
        select(OpenAIWhisperJob)
        .where(
            or_(
                and_(OpenAIWhisperJob.status == "success", OpenAIWhisperJob.updated_at < success_cutoff),
                and_(OpenAIWhisperJob.status == "failed", OpenAIWhisperJob.updated_at < failed_cutoff),
            )
        )
        .order_by(OpenAIWhisperJob.updated_at.asc(), OpenAIWhisperJob.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def delete_job(db: Session, workspace_id: int, job_id: str) -> OpenAIWhisperJob | None:
    job = _ensure_job(db, workspace_id, job_id)
    if not job:
        return None
    db.delete(job)
    return job


def delete_jobs_by_ids(db: Session, job_ids: list[str]) -> int:
    if not job_ids:
        return 0
    stmt = delete(OpenAIWhisperJob).where(OpenAIWhisperJob.job_id.in_(job_ids))
    result = db.execute(stmt)
    return int(result.rowcount or 0)


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def get_job(db: Session, workspace_id: int, job_id: str) -> OpenAIWhisperJob | None:
    return _ensure_job(db, workspace_id, job_id)
