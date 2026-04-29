"""Service layer for tenant facing Whisper subtitle APIs."""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
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

ALLOWED_CONTACT_INTERVALS = {0.5, 1.0, 1.5, 2.0}
TERMINAL_STATUSES = {"success", "failed"}
ACTIVE_STATUSES = {"pending", "processing"}


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
    meta["subtitle_status"] = job.subtitle_status or meta.get("subtitle_status")
    meta["contact_sheet_status"] = job.contact_sheet_status or meta.get("contact_sheet_status")
    meta["download_status"] = job.download_status or meta.get("download_status")
    meta["error"] = job.error
    meta["subtitle_error"] = job.subtitle_error
    meta["contact_sheet_error"] = job.contact_sheet_error
    meta["download_error"] = job.download_error
    meta["translate"] = bool(job.translate)
    meta["show_bilingual"] = bool(job.show_bilingual)
    meta["do_subtitle"] = bool(job.do_subtitle)
    meta["do_contact_sheet"] = bool(job.do_contact_sheet)
    meta["do_download_only"] = bool(job.do_download_only)
    if job.contact_interval is not None:
        meta["contact_interval"] = float(job.contact_interval)
    if job.contact_sheet_url:
        meta["contact_sheet_url"] = job.contact_sheet_url
    if job.download_url:
        meta["download_url"] = job.download_url
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
    do_subtitle: bool,
    do_contact_sheet: bool,
    contact_interval: Optional[float],
    do_download_only: bool,
    db: Session,
) -> TranscriptionJobCreatedResponse:
    share_url = (share_url or "").strip()
    if share_url and (upload or upload_id):
        raise APIError("FILE_REQUIRED", "请仅上传文件或粘贴分享链接中的一种。", 422)
    if not upload and not upload_id and not share_url:
        raise APIError("FILE_REQUIRED", "请上传需要识别的视频，或提供分享链接。", 422)

    do_subtitle = bool(do_subtitle)
    do_contact_sheet = bool(do_contact_sheet)
    do_download_only = bool(do_download_only)
    if not (do_subtitle or do_contact_sheet or do_download_only):
        raise APIError(
            "TASK_REQUIRED",
            "请选择至少一个任务：识别字幕、拆解图片或下载源视频。",
            422,
        )

    if do_download_only and not share_url:
        raise APIError("SHARE_URL_REQUIRED", "仅下载模式需要提供分享链接。", 422)

    if do_contact_sheet:
        if contact_interval is None:
            raise APIError("CONTACT_INTERVAL_REQUIRED", "请选择抽帧间隔。", 422)
        try:
            interval_value = round(float(contact_interval), 2)
        except (TypeError, ValueError) as exc:
            raise APIError("INVALID_CONTACT_INTERVAL", "抽帧间隔格式不正确。", 422) from exc
        if interval_value not in ALLOWED_CONTACT_INTERVALS:
            raise APIError("INVALID_CONTACT_INTERVAL", "抽帧间隔仅支持 0.5/1/1.5/2 秒。", 422)
        contact_interval = interval_value
    else:
        contact_interval = None

    translate = bool(do_subtitle and translate)

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
        "subtitle_error": None,
        "contact_sheet_error": None,
        "download_error": None,
        "source_language": normalized_source,
        "target_language": normalized_target,
        "translate": bool(translate),
        "show_bilingual": bool(show_bilingual),
        "filename": original_name,
        "video_path": str(video_path),
        "do_subtitle": do_subtitle,
        "do_contact_sheet": do_contact_sheet,
        "do_download_only": do_download_only,
        "contact_interval": contact_interval,
        "subtitle_status": "pending" if do_subtitle else "skipped",
        "contact_sheet_status": "pending" if do_contact_sheet else "skipped",
        "download_status": "pending" if share_url else "skipped",
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
    db.commit()

    from . import tasks as whisper_tasks

    failure_message = "暂时无法提交识别任务，请稍后重试。"

    task_ids = []
    try:
        if share_url:
            download_result = whisper_tasks.download_shared_video.delay(
                workspace_id=workspace_id,
                job_id=job_id,
            )
            task_ids.append(download_result.id)
        else:
            if do_subtitle:
                async_result = whisper_tasks.transcribe_video.delay(
                    workspace_id=workspace_id,
                    job_id=job_id,
                )
                task_ids.append(async_result.id)
            if do_contact_sheet:
                contact_result = whisper_tasks.generate_contact_sheet.delay(
                    workspace_id=workspace_id,
                    job_id=job_id,
                    contact_interval=contact_interval,
                )
                task_ids.append(contact_result.id)
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
        if task_ids:
            meta["celery_task_id"] = task_ids[0]
            meta["celery_task_ids"] = task_ids
        return meta

    updated_meta = storage.update_metadata(workspace_id, job_id, _apply)
    if task_ids:
        job_row = (
            repository.update_celery_task(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                celery_task_id=task_ids[0],
            )
            or job_row
        )
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
                do_subtitle=bool(row.do_subtitle),
                do_contact_sheet=bool(row.do_contact_sheet),
                do_download_only=bool(row.do_download_only),
                subtitle_status=row.subtitle_status,
                contact_sheet_status=row.contact_sheet_status,
                download_status=row.download_status,
                created_at=repository.ensure_aware(row.created_at),
                updated_at=repository.ensure_aware(row.updated_at),
                started_at=repository.ensure_aware(row.started_at),
                completed_at=repository.ensure_aware(row.completed_at),
            )
            for row in rows
        ]
    )


def delete_job(workspace_id: int, job_id: str, db: Session, *, force: bool = False) -> dict:
    row = repository.get_job(db, workspace_id, job_id)
    if not row:
        raise APIError("JOB_NOT_FOUND", "任务不存在或已删除。", 404)
    if row.status in ACTIVE_STATUSES and not (force or settings.OPENAI_WHISPER_MANUAL_DELETE_ACTIVE_ALLOWED):
        raise APIError("JOB_ACTIVE", "任务仍在处理中，请等待完成后再删除。", 409)

    storage.delete_job_files(workspace_id, job_id)
    repository.delete_job(db, workspace_id, job_id)
    db.commit()
    return {"deleted": 1, "job_id": job_id}


def clear_jobs(workspace_id: int, db: Session, *, scope: str = "terminal", force: bool = False, limit: int = 500) -> dict:
    scope = (scope or "terminal").lower()
    include_active = bool(force and scope == "all")
    if scope not in {"terminal", "failed", "success", "all"}:
        raise APIError("INVALID_SCOPE", "scope must be terminal, failed, success, or all.", 422)

    rows = repository.list_jobs_for_workspace(
        db,
        workspace_id,
        include_active=include_active,
        limit=max(1, min(int(limit or 500), 1000)),
    )
    selected = []
    for row in rows:
        if scope == "failed" and row.status != "failed":
            continue
        if scope == "success" and row.status != "success":
            continue
        if scope == "terminal" and row.status not in TERMINAL_STATUSES:
            continue
        if scope == "all" and row.status in ACTIVE_STATUSES and not include_active:
            continue
        selected.append(row)

    for row in selected:
        storage.delete_job_files(row.workspace_id, row.job_id)
    deleted = repository.delete_jobs_by_ids(db, [row.job_id for row in selected])
    db.commit()
    return {"deleted": deleted, "scope": scope}


def build_download(workspace_id: int, job_id: str, variant: str) -> Path:
    if variant not in {"source", "translation"}:
        raise APIError("INVALID_VARIANT", "variant must be source or translation", 422)
    try:
        path = storage.resolve_download_path(workspace_id, job_id, variant)
    except FileNotFoundError as exc:
        raise APIError("SUBTITLE_NOT_READY", "字幕尚未生成，请稍后再试。", 404) from exc
    return path


def build_contact_sheet_download(workspace_id: int, job_id: str) -> Path:
    try:
        return storage.resolve_contact_sheet_path(workspace_id, job_id)
    except FileNotFoundError as exc:
        raise APIError("CONTACT_SHEET_NOT_READY", "拆解图片尚未生成，请稍后再试。", 404) from exc


def build_video_download(workspace_id: int, job_id: str) -> tuple[Path, str, str]:
    try:
        metadata = storage.load_metadata(workspace_id, job_id)
    except FileNotFoundError as exc:
        raise APIError("JOB_NOT_FOUND", "任务不存在或已删除。", 404) from exc
    raw_video_path = metadata.get("video_path")
    path = Path(str(raw_video_path)) if raw_video_path else None
    if not path or not path.exists():
        raise APIError("VIDEO_NOT_READY", "视频尚未下载完成或已被自动清理。", 404)
    filename = metadata.get("filename") or f"{job_id}.mp4"
    content_type = metadata.get("content_type") or "application/octet-stream"
    return path, filename, content_type
