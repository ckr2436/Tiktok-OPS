"""Service layer for tenant facing Whisper subtitle APIs."""
from __future__ import annotations

import os
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


async def create_job(
    *,
    workspace_id: int,
    user_id: int,
    upload: UploadFile,
    source_language: Optional[str],
    translate: bool,
    target_language: Optional[str],
    show_bilingual: bool,
) -> TranscriptionJobCreatedResponse:
    if not upload:
        raise APIError("FILE_REQUIRED", "请上传需要识别的视频。", 422)

    normalized_source = _normalize_language_or_error(source_language, "source_language")
    normalized_target = _validate_options(translate, target_language)

    original_name = os.path.basename(upload.filename or "video.mp4")
    ext = Path(original_name).suffix or ".mp4"
    job_id = uuid.uuid4().hex
    directory = storage.job_dir(workspace_id, job_id)
    video_path = directory / f"input{ext}"

    await _save_upload_file(video_path, upload)

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

