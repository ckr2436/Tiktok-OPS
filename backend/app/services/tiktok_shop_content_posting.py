from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import UploadFile

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.tiktok_shop_content_posting import TikTokShopContentPost


ALLOWED_VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".mkv", ".wmv", ".webm", ".avi", ".3gp", ".flv", ".mpeg", ".mpg"}
)
TERMINAL_WORKFLOW_STATUSES = frozenset({"SUCCESS", "FAILED", "PRECHECK_FAILED", "PUBLISH_UNCERTAIN"})
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_idempotency_key(value: str) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_PATTERN.fullmatch(normalized):
        raise APIError(
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must be 8-128 characters using letters, numbers, dot, underscore, colon, or hyphen.",
            400,
        )
    return normalized


def validate_product_link_title(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized or len(normalized) > 30:
        raise APIError(
            "INVALID_PRODUCT_LINK_TITLE",
            "Product link title must contain 1-30 characters.",
            422,
        )
    if any(unicodedata.category(char).startswith(("P", "S")) for char in normalized):
        raise APIError(
            "INVALID_PRODUCT_LINK_TITLE",
            "Product link title cannot contain punctuation or emoji.",
            422,
        )
    return normalized


def validate_video_title(value: str | None) -> str | None:
    normalized = str(value or "").strip() or None
    if normalized is None:
        return None
    utf16_units = len(normalized.encode("utf-16-le")) // 2
    if utf16_units > 2200:
        raise APIError(
            "VIDEO_TITLE_TOO_LONG",
            "Video title cannot exceed 2200 UTF-16 code units.",
            422,
        )
    return normalized


def normalize_optional_identifier(value: str | None, *, field: str, max_length: int = 128) -> str | None:
    normalized = str(value or "").strip() or None
    if normalized is not None and len(normalized) > max_length:
        raise APIError("INVALID_FIELD", f"{field} is too long.", 422, data={"field": field})
    return normalized


def request_fingerprint(
    *,
    sha256_digest: bytes,
    product_id: str,
    product_link_title: str,
    video_title: str | None,
    cover_uri: str | None,
    cover_timestamp_ms: int | None,
    music_id: str | None,
) -> bytes:
    payload = {
        "sha256": sha256_digest.hex(),
        "product_id": product_id,
        "product_link_title": product_link_title,
        "video_title": video_title,
        "cover_uri": cover_uri,
        "cover_timestamp_ms": cover_timestamp_ms,
        "music_id": music_id,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def _storage_root() -> Path:
    return Path(str(settings.TT_SHOP_CONTENT_POSTING_STORAGE_ROOT)).resolve()


def _workflow_directory(workspace_id: int, account_id: int) -> Path:
    root = _storage_root()
    workspace_root = (root / f"workspace_{int(workspace_id)}").resolve()
    target = (workspace_root / f"account_{int(account_id)}").resolve()
    if target != root and root not in target.parents:
        raise APIError("CONTENT_POSTING_STORAGE_INVALID", "Content posting storage path is invalid.", 500)
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    workspace_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    target.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chmod(root, 0o750)
    os.chmod(workspace_root, 0o750)
    os.chmod(target, 0o750)
    return target


async def persist_uploaded_video(
    upload: UploadFile,
    *,
    workspace_id: int,
    account_id: int,
) -> tuple[Path, str, int, bytes]:
    original_name = Path(str(upload.filename or "video.mp4")).name[:255]
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise APIError(
            "UNSUPPORTED_VIDEO_FORMAT",
            "Supported formats: MP4, MOV, MKV, WMV, WEBM, AVI, 3GP, FLV, MPEG.",
            422,
        )
    directory = _workflow_directory(workspace_id, account_id)
    stem = uuid.uuid4().hex
    temporary = directory / f".{stem}{extension}.uploading"
    destination = directory / f"{stem}{extension}"
    digest = hashlib.sha256()
    size = 0
    maximum = max(1, int(settings.TT_SHOP_CONTENT_POSTING_MAX_VIDEO_BYTES))
    try:
        with temporary.open("xb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise APIError(
                        "VIDEO_TOO_LARGE",
                        f"Video file cannot exceed {maximum // (1024 * 1024)} MB.",
                        413,
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size <= 0:
            raise APIError("EMPTY_VIDEO_FILE", "Video file is empty.", 422)
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        return destination, original_name, size, digest.digest()
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        await upload.close()


def resolve_workflow_file(row: TikTokShopContentPost) -> Path:
    root = _storage_root()
    path = Path(str(row.local_file_path)).resolve()
    if root not in path.parents or not path.is_file():
        raise APIError("CONTENT_POSTING_FILE_MISSING", "The locally archived video is unavailable.", 409)
    return path


def build_precheck_body(row: TikTokShopContentPost) -> dict[str, Any]:
    video_info: dict[str, Any] = {"file_id": str(row.official_file_id)}
    if row.video_title:
        video_info["title"] = row.video_title
    if row.cover_timestamp_ms is not None:
        video_info["cover_timestamp_ms"] = int(row.cover_timestamp_ms)
    return {
        "video_info": video_info,
        "product_link_info": {
            "product_id": row.product_id,
            "title": row.product_link_title,
        },
    }


def build_publish_body(row: TikTokShopContentPost) -> dict[str, Any]:
    video_info: dict[str, Any] = {"file_id": str(row.official_file_id)}
    if row.video_title:
        video_info["title"] = row.video_title
    if row.cover_uri:
        video_info["cover_uri"] = row.cover_uri
    if row.cover_timestamp_ms is not None:
        video_info["cover_timestamp_ms"] = int(row.cover_timestamp_ms)
    if row.music_id:
        video_info["music_id"] = row.music_id
    return {
        "video_info": video_info,
        "product_link_info": {
            "product_id": row.product_id,
            "title": row.product_link_title,
        },
    }


def update_request_id(row: TikTokShopContentPost, stage: str, request_id: str | None) -> None:
    values = dict(row.provider_request_ids_json or {})
    if request_id:
        values[str(stage)] = str(request_id)[:128]
    row.provider_request_ids_json = values


def safe_error_details(error: APIError) -> tuple[str, str, str | None, bool]:
    data = error.data if isinstance(error.data, Mapping) else {}
    request_id = str(data.get("request_id") or "").strip()[:128] or None
    return (
        str(error.code or "CONTENT_POSTING_ERROR")[:64],
        str(error.message or "Content posting failed.").replace("\r", " ").replace("\n", " ")[:2000],
        request_id,
        bool(data.get("retryable")),
    )


def serialize_content_post(row: TikTokShopContentPost) -> dict[str, Any]:
    path = Path(str(row.local_file_path))
    return {
        "id": int(row.id),
        "account_id": int(row.account_id),
        "created_by_user_id": int(row.created_by_user_id),
        "idempotency_key": row.idempotency_key,
        "original_filename": row.original_filename,
        "file_size": int(row.file_size),
        "local_file_available": path.is_file(),
        "product_id": row.product_id,
        "product_link_title": row.product_link_title,
        "video_title": row.video_title,
        "cover_timestamp_ms": int(row.cover_timestamp_ms) if row.cover_timestamp_ms is not None else None,
        "music_id": row.music_id,
        "official_file_id": row.official_file_id,
        "precheck_task_id": row.precheck_task_id,
        "precheck_status": row.precheck_status,
        "precheck_issues": list(row.precheck_issues_json or []),
        "video_id": row.video_id,
        "post_status": row.post_status,
        "post_time": row.post_time.isoformat() if row.post_time else None,
        "workflow_status": row.workflow_status,
        "publish_requested": bool(row.publish_requested),
        "poll_attempts": int(row.poll_attempts or 0),
        "next_poll_at": row.next_poll_at.isoformat() if row.next_poll_at else None,
        "provider_request_ids": dict(row.provider_request_ids_json or {}),
        "api_versions": dict(row.api_versions_json or {}),
        "last_error_code": row.last_error_code,
        "last_error_message": row.last_error_message,
        "last_error_request_id": row.last_error_request_id,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


__all__ = [
    "TERMINAL_WORKFLOW_STATUSES",
    "build_precheck_body",
    "build_publish_body",
    "normalize_optional_identifier",
    "persist_uploaded_video",
    "request_fingerprint",
    "resolve_workflow_file",
    "safe_error_details",
    "serialize_content_post",
    "update_request_id",
    "utcnow_naive",
    "validate_idempotency_key",
    "validate_product_link_title",
    "validate_video_title",
]
