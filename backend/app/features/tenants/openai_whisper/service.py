"""Service layer for tenant facing Whisper subtitle APIs."""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import UploadFile

from app.core.errors import APIError

from . import storage
from .languages import list_language_options, normalize_language_code
from .schemas import (
    LanguageListResponse,
    TranscriptionJobCreatedResponse,
    TranscriptionJobStatusResponse,
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
    source_language: Optional[str],
    translate: bool,
    target_language: Optional[str],
    show_bilingual: bool,
) -> TranscriptionJobCreatedResponse:
    if not upload and not upload_id:
        raise APIError("FILE_REQUIRED", "请上传需要识别的视频。", 422)

    normalized_source = _normalize_language_or_error(source_language, "source_language")
    normalized_target = _validate_options(translate, target_language)

    job_id = uuid.uuid4().hex
    directory = storage.job_dir(workspace_id, job_id)
    video_path: Path
    original_name: str

    if upload:
        original_name = os.path.basename(upload.filename or "video.mp4")
        ext = Path(original_name).suffix or ".mp4"
        video_path = directory / f"input{ext}"
        await _save_upload_file(video_path, upload)
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
    storage.write_metadata(workspace_id, job_id, metadata)

    from . import tasks as whisper_tasks  # Lazy import to avoid circular refs

    async_result = whisper_tasks.transcribe_video.delay(
        workspace_id=workspace_id,
        job_id=job_id,
    )

    def _apply(meta: Dict[str, object]) -> Dict[str, object]:
        meta["celery_task_id"] = async_result.id
        return meta

    updated_meta = storage.update_metadata(workspace_id, job_id, _apply)
    return TranscriptionJobCreatedResponse.from_metadata(updated_meta)


def get_job(workspace_id: int, job_id: str) -> TranscriptionJobStatusResponse:
    meta = storage.load_metadata(workspace_id, job_id)
    if int(meta.get("workspace_id")) != int(workspace_id):
        raise APIError("FORBIDDEN", "Job does not belong to this workspace.", 403)
    return TranscriptionJobStatusResponse.from_metadata(meta)


def build_download(workspace_id: int, job_id: str, variant: str) -> Path:
    if variant not in {"source", "translation"}:
        raise APIError("INVALID_VARIANT", "variant must be source or translation", 422)
    try:
        path = storage.resolve_download_path(workspace_id, job_id, variant)
    except FileNotFoundError as exc:
        raise APIError("SUBTITLE_NOT_READY", "字幕尚未生成，请稍后再试。", 404) from exc
    return path

