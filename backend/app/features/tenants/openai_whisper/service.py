"""Service layer for tenant facing Whisper subtitle APIs."""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.errors import APIError

from . import repository, storage
from .languages import list_language_options, normalize_language_code
from .schemas import (
    LanguageListResponse,
    TranscriptionJobCreatedResponse,
    TranscriptionJobListResponse,
    TranscriptionJobStatusResponse,
    TranscriptionJobSummary,
    UploadedVideoResponse,
)


async def _save_upload_file(dest: Path, upload: UploadFile) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as buffer:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            buffer.write(chunk)
    await upload.seek(0)


def _normalize_language_or_error(value: Optional[str], field: str) -> Optional[str]:
    normalized = normalize_language_code(value)
    if value and not normalized:
        raise APIError("INVALID_LANGUAGE", f"Unsupported language for {field}.", 422)
    return normalized


def _validate_options(translate: bool, target_language: Optional[str]) -> Optional[str]:
    if not translate:
        return None
    normalized = _normalize_language_or_error(target_language, "target_language")
    if not normalized:
        raise APIError(
            "MISSING_TARGET_LANGUAGE",
            "请选择翻译目标语言。",
            422,
        )
    return normalized


def _merge_db_job_metadata(meta: Dict[str, object], job) -> Dict[str, object]:
    if not job:
        return meta
    if job.filename and not meta.get("filename"):
        meta["filename"] = job.filename
    if job.file_size is not None:
        meta["size"] = int(job.file_size)
    if job.content_type and not meta.get("content_type"):
        meta["content_type"] = job.content_type
    meta["status"] = job.status
    meta["error"] = job.error
    meta["translate"] = bool(job.translate)
    meta["show_bilingual"] = bool(job.show_bilingual)
    if job.source_language and not meta.get("source_language"):
        meta["source_language"] = job.source_language
    if job.target_language:
        meta["target_language"] = job.target_language
    result = meta.setdefault("result", {})
    if job.detected_language and not result.get("detected_language"):
        result["detected_language"] = job.detected_language
    if job.translation_language and not result.get("translation_language"):
        result["translation_language"] = job.translation_language
    meta["created_at"] = repository.ensure_aware(job.created_at)
    meta["updated_at"] = repository.ensure_aware(job.updated_at)
    meta["started_at"] = repository.ensure_aware(job.started_at)
    meta["completed_at"] = repository.ensure_aware(job.completed_at)
    return meta


def get_languages() -> LanguageListResponse:
    return LanguageListResponse(languages=list_language_options())


async def upload_video(
    *,
    workspace_id: int,
    user_id: int,
    upload: UploadFile,
) -> UploadedVideoResponse:
    if not upload:
        raise APIError("FILE_REQUIRED", "请上传需要识别的视频。", 422)

    original_name = os.path.basename(upload.filename or "video.mp4")
    ext = Path(original_name).suffix or ".mp4"
    upload_id = uuid.uuid4().hex
    directory = storage.upload_dir(workspace_id, upload_id)
    video_path = directory / f"upload{ext}"

    await _save_upload_file(video_path, upload)

    payload: Dict[str, object] = {
        "upload_id": upload_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "filename": original_name,
        "content_type": upload.content_type,
        "path": str(video_path),
        "size": video_path.stat().st_size,
    }
    storage.write_upload_metadata(workspace_id, upload_id, payload)
    return UploadedVideoResponse.model_validate(payload)


async def create_job(
    *,
    workspace_id: int,
    user_id: int,
    upload: Optional[UploadFile],
    upload_id: Optional[str],
    share_url: Optional[str],
    source_language: Optional[str],
    translate: bool,
    target_language: Optional[str],
    show_bilingual: bool,
    db: Session,
) -> TranscriptionJobCreatedResponse:
    share_url = (share_url or "").strip()
    if share_url and (upload or upload_id):
        raise APIError("FILE_REQUIRED", "请仅上传文件或粘贴分享链接中的一种。", 422)
    if not upload and not upload_id and not share_url:
        raise APIError("FILE_REQUIRED", "请上传需要识别的视频，或提供分享链接。", 422)

    normalized_source = _normalize_language_or_error(source_language, "source_language")
    normalized_target = _validate_options(translate, target_language)

    job_id = uuid.uuid4().hex
    directory = storage.job_dir(workspace_id, job_id)
    video_path: Path
    original_name: str

    content_type: Optional[str] = None
    if share_url:
        original_name = "分享链接视频.mp4"
        ext = Path(original_name).suffix or ".mp4"
        video_path = directory / f"input{ext}"
    elif upload:
        original_name = os.path.basename(upload.filename or "video.mp4")
        ext = Path(original_name).suffix or ".mp4"
        video_path = directory / f"input{ext}"
        await _save_upload_file(video_path, upload)
        content_type = upload.content_type
    else:
        try:
            upload_meta = storage.load_upload_metadata(workspace_id, upload_id)
        except FileNotFoundError as exc:
            raise APIError("UPLOAD_NOT_FOUND", "上传文件不存在或已失效，请重新上传。", 404) from exc

        raw_path = upload_meta.get("path")
        if not raw_path:
            storage.delete_upload(workspace_id, upload_id)
            raise APIError("UPLOAD_NOT_FOUND", "上传文件不存在或已失效，请重新上传。", 404)

        source_path = Path(str(raw_path))
        if not source_path.exists():
            storage.delete_upload(workspace_id, upload_id)
            raise APIError("UPLOAD_NOT_FOUND", "上传文件不存在或已失效，请重新上传。", 404)

        original_name = os.path.basename(upload_meta.get("filename") or source_path.name)
        ext = Path(original_name).suffix or source_path.suffix or ".mp4"
        video_path = directory / f"input{ext}"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), video_path)
        storage.delete_upload(workspace_id, upload_id)
        content_type = upload_meta.get("content_type")

    metadata: Dict[str, object] = {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "status": "pending",
        "error": None,
        "source_language": normalized_source,
        "target_language": normalized_target,
        "translate": bool(translate),
        "show_bilingual": bool(show_bilingual),
        "filename": original_name,
        "video_path": str(video_path),
    }
    if share_url:
        metadata["share_url"] = share_url
    if video_path.exists():
        metadata["size"] = video_path.stat().st_size
    if content_type:
        metadata["content_type"] = content_type
    storage.write_metadata(workspace_id, job_id, metadata)
    job_row = repository.create_job(db, metadata)
    db.flush()
    # Make the job visible to other transactions (Celery workers) before
    # dispatching the asynchronous task. Otherwise the worker may start
    # immediately, fail to find the DB row, and never update its status.
    db.commit()

    from . import tasks as whisper_tasks  # Lazy import to avoid circular refs

    failure_message = "暂时无法提交识别任务，请稍后重试。"

    try:
        async_result = whisper_tasks.transcribe_video.delay(
            workspace_id=workspace_id,
            job_id=job_id,
        )
    except Exception as exc:
        storage.mark_failed(workspace_id, job_id, failure_message)
        repository.mark_failed(db, workspace_id, job_id, failure_message)
        db.commit()
        raise APIError(
            "WHISPER_TASK_ENQUEUE_FAILED",
            failure_message,
            503,
        ) from exc

    def _apply(meta: Dict[str, object]) -> Dict[str, object]:
        meta["celery_task_id"] = async_result.id
        return meta

    updated_meta = storage.update_metadata(workspace_id, job_id, _apply)
    job_row = repository.update_celery_task(
        db,
        workspace_id=workspace_id,
        job_id=job_id,
        celery_task_id=async_result.id,
    ) or job_row
    db.flush()
    _merge_db_job_metadata(updated_meta, job_row)
    return TranscriptionJobCreatedResponse.from_metadata(updated_meta)


def get_job(workspace_id: int, job_id: str, db: Session) -> TranscriptionJobStatusResponse:
    db_job = repository.get_job(db, workspace_id, job_id)
    if not db_job:
        raise APIError("JOB_NOT_FOUND", "任务不存在或已删除。", 404)
    try:
        meta = storage.load_metadata(workspace_id, job_id)
    except FileNotFoundError as exc:
        raise APIError("JOB_NOT_FOUND", "任务不存在或已删除。", 404) from exc
    if int(meta.get("workspace_id")) != int(workspace_id):
        raise APIError("FORBIDDEN", "Job does not belong to this workspace.", 403)
    _merge_db_job_metadata(meta, db_job)
    return TranscriptionJobStatusResponse.from_metadata(meta)


def list_jobs(workspace_id: int, limit: int, db: Session) -> TranscriptionJobListResponse:
    rows = repository.list_jobs(db, workspace_id, limit)
    return TranscriptionJobListResponse(
        jobs=[
            TranscriptionJobSummary(
                job_id=row.job_id,
                filename=row.filename,
                status=row.status,
                error=row.error,
                translate=bool(row.translate),
                show_bilingual=bool(row.show_bilingual),
                source_language=row.source_language,
                detected_language=row.detected_language,
                target_language=row.target_language,
                translation_language=row.translation_language,
                created_at=repository.ensure_aware(row.created_at),
                updated_at=repository.ensure_aware(row.updated_at),
                started_at=repository.ensure_aware(row.started_at),
                completed_at=repository.ensure_aware(row.completed_at),
            )
            for row in rows
        ]
    )


def build_download(workspace_id: int, job_id: str, variant: str) -> Path:
    if variant not in {"source", "translation"}:
        raise APIError("INVALID_VARIANT", "variant must be source or translation", 422)
    try:
        path = storage.resolve_download_path(workspace_id, job_id, variant)
    except FileNotFoundError as exc:
        raise APIError("SUBTITLE_NOT_READY", "字幕尚未生成，请稍后再试。", 404) from exc
    return path

