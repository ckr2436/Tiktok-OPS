from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.website_ads import WebsiteAdsCreativeAsset, WebsiteAdsUploadFingerprint
from app.services.website_ads_media_cache import (
    ArchivedMedia,
    attach_archived_video,
    media_root,
    merge_tiktok_media_metadata,
    public_asset_media_url,
)


UPLOAD_JOB_KEY = "_upload_job"
UPLOAD_IN_PROGRESS_STATUSES = {"QUEUED", "PROCESSING", "RETRYING", "UPLOADING"}
UPLOAD_TERMINAL_STATUSES = {"UPLOADED", "FAILED"}
_UPLOAD_STALE_AFTER = timedelta(minutes=int(settings.WEBSITE_ADS_UPLOAD_STALE_MINUTES))


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def extract_uploaded_video_id(payload: object) -> str | None:
    queue = [payload]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for key in ("video_id", "material_id"):
                value = item.get(key)
                if value not in (None, ""):
                    return str(value)
            queue.extend(value for value in item.values() if isinstance(value, (dict, list)))
        elif isinstance(item, list):
            queue.extend(item)
    return None


def reserve_upload_fingerprint(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    content_sha256: str,
    fingerprint_type: str,
    file_name: str,
    file_size_bytes: int,
    initial_status: str = "UPLOADING",
) -> tuple[WebsiteAdsUploadFingerprint, bool]:
    lookup = (
        WebsiteAdsUploadFingerprint.workspace_id == workspace_id,
        WebsiteAdsUploadFingerprint.auth_id == auth_id,
        WebsiteAdsUploadFingerprint.advertiser_id == advertiser_id,
        WebsiteAdsUploadFingerprint.content_sha256 == content_sha256,
    )
    now = utcnow()
    row = db.scalar(select(WebsiteAdsUploadFingerprint).where(*lookup))
    if row:
        status = str(row.status or "").upper()
        updated_at = row.updated_at or row.created_at or now
        is_fresh_upload = status in UPLOAD_IN_PROGRESS_STATUSES and updated_at >= now - _UPLOAD_STALE_AFTER
        if status == "UPLOADED" or is_fresh_upload:
            return row, False
        row.status = str(initial_status or "UPLOADING").upper()
        row.fingerprint_type = fingerprint_type
        row.file_name = file_name
        row.file_size_bytes = file_size_bytes
        row.video_id = None
        row.response_json = None
        row.error_message = None
        row.updated_at = now
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, True

    row = WebsiteAdsUploadFingerprint(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        fingerprint_type=fingerprint_type,
        content_sha256=content_sha256,
        file_size_bytes=file_size_bytes,
        file_name=file_name,
        status=str(initial_status or "UPLOADING").upper(),
        updated_at=now,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row, True
    except IntegrityError:
        db.rollback()
        concurrent = db.scalar(select(WebsiteAdsUploadFingerprint).where(*lookup))
        if not concurrent:
            raise
        return concurrent, False


def upload_result(
    row: WebsiteAdsUploadFingerprint,
    payload: dict | None = None,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    raw = dict(payload or row.response_json or {})
    job = raw.pop(UPLOAD_JOB_KEY, None)
    result = raw
    data = dict(result.get("data") or {}) if isinstance(result.get("data"), dict) else {}
    video_id = row.video_id or extract_uploaded_video_id(result)
    if video_id:
        data.setdefault("video_id", video_id)
        result["video_id"] = video_id
    if data:
        result["data"] = data

    status = str(row.status or "UNKNOWN").upper()
    in_progress = status in UPLOAD_IN_PROGRESS_STATUSES
    public_status = "DUPLICATE" if deduplicated and status == "UPLOADED" else status
    original_name = str((job or {}).get("original_name") or getattr(row, "file_name", None) or "")
    created_at = getattr(row, "created_at", None)
    updated_at = getattr(row, "updated_at", None)
    result.update(
        {
            "upload_id": int(getattr(row, "id", 0) or 0),
            "file_name": original_name,
            "deduplicated": bool(deduplicated),
            "skipped": bool(deduplicated),
            "in_progress": in_progress,
            "queued": status in {"QUEUED", "RETRYING"},
            "failed": status == "FAILED",
            "upload_status": public_status,
            "error_message": getattr(row, "error_message", None) if status in {"FAILED", "RETRYING"} else None,
            "created_at": created_at.isoformat() if created_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }
    )
    return result


def upload_job(row: WebsiteAdsUploadFingerprint) -> dict[str, Any]:
    raw = dict(row.response_json or {})
    job = raw.get(UPLOAD_JOB_KEY)
    if not isinstance(job, dict):
        raise ValueError("Upload job metadata is missing")
    return dict(job)


def queue_upload_fingerprint(
    db: Session,
    row: WebsiteAdsUploadFingerprint,
    *,
    archived: ArchivedMedia,
    provider: str,
    original_name: str,
    upload_name: str,
    source_url: str | None = None,
    flaw_detect: bool = False,
    auto_fix_enabled: bool = False,
) -> WebsiteAdsUploadFingerprint:
    row.status = "QUEUED"
    row.response_json = {
        UPLOAD_JOB_KEY: {
            "version": 1,
            "provider": str(provider),
            "archive_path": str(archived.path),
            "sha256": archived.sha256,
            "md5": archived.md5,
            "size_bytes": int(archived.size_bytes),
            "content_type": archived.content_type,
            "original_name": str(original_name),
            "upload_name": str(upload_name),
            "source_url": str(source_url) if source_url else None,
            "flaw_detect": bool(flaw_detect),
            "auto_fix_enabled": bool(auto_fix_enabled),
            "queued_at": utcnow().isoformat(),
        }
    }
    row.error_message = None
    row.updated_at = utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def archived_media_for_upload(row: WebsiteAdsUploadFingerprint) -> ArchivedMedia:
    job = upload_job(row)
    root = media_root()
    path = Path(str(job.get("archive_path") or "")).expanduser().resolve()
    if path == root or root not in path.parents:
        raise ValueError("Upload archive path is outside the media store")
    if not path.is_file():
        raise FileNotFoundError(f"Upload archive is missing: {path.name}")
    expected_size = int(job.get("size_bytes") or row.file_size_bytes or 0)
    if expected_size <= 0 or path.stat().st_size != expected_size:
        raise ValueError("Upload archive size does not match the queued job")
    sha256 = str(job.get("sha256") or row.content_sha256 or "")
    if sha256 != str(row.content_sha256 or ""):
        raise ValueError("Upload archive fingerprint does not match the queued job")
    return ArchivedMedia(
        path=path,
        sha256=sha256,
        md5=str(job.get("md5") or ""),
        size_bytes=expected_size,
        content_type=str(job.get("content_type") or "video/mp4"),
        original_name=str(job.get("original_name") or row.file_name or path.name),
    )


def upsert_uploaded_asset(
    db: Session,
    *,
    fingerprint: WebsiteAdsUploadFingerprint,
    payload: dict,
    archived: ArchivedMedia,
    original_name: str,
    source_url: str | None = None,
) -> WebsiteAdsCreativeAsset:
    video_id = fingerprint.video_id or extract_uploaded_video_id(payload)
    if not video_id:
        raise ValueError("TikTok upload succeeded without returning a video_id")
    asset = db.scalar(
        select(WebsiteAdsCreativeAsset).where(
            WebsiteAdsCreativeAsset.workspace_id == int(fingerprint.workspace_id),
            WebsiteAdsCreativeAsset.auth_id == int(fingerprint.auth_id),
            WebsiteAdsCreativeAsset.advertiser_id == str(fingerprint.advertiser_id),
            WebsiteAdsCreativeAsset.video_id == str(video_id),
        )
    ) or WebsiteAdsCreativeAsset(
        workspace_id=int(fingerprint.workspace_id),
        auth_id=int(fingerprint.auth_id),
        advertiser_id=str(fingerprint.advertiser_id),
        video_id=str(video_id),
        title=Path(original_name).stem[:512] or str(video_id),
    )
    asset.file_name = original_name[:512]
    asset.source = "ADVERTISER_LIBRARY"
    asset.is_active = True
    asset.last_synced_at = utcnow()
    asset.raw_json = merge_tiktok_media_metadata(
        asset.raw_json if isinstance(asset.raw_json, dict) else {},
        payload,
        video_url=source_url,
    )
    db.add(asset)
    db.flush()
    attach_archived_video(asset, archived)
    db.add(asset)
    return asset


def complete_upload_fingerprint(
    db: Session,
    row: WebsiteAdsUploadFingerprint,
    payload: dict,
    *,
    archived: ArchivedMedia,
    original_name: str,
    source_url: str | None = None,
) -> dict[str, Any]:
    row.status = "UPLOADED"
    row.video_id = extract_uploaded_video_id(payload)
    if not row.video_id:
        raise ValueError("TikTok upload succeeded without returning a video_id")
    row.response_json = payload
    row.error_message = None
    row.updated_at = utcnow()
    db.add(row)
    asset = upsert_uploaded_asset(
        db,
        fingerprint=row,
        payload=payload,
        archived=archived,
        original_name=original_name,
        source_url=source_url,
    )
    db.commit()
    db.refresh(row)
    db.refresh(asset)
    result = upload_result(row, payload, deduplicated=False)
    result["asset_id"] = int(asset.id)
    result["preview_url"] = public_asset_media_url(asset, "video")
    return result


def fail_upload_fingerprint(db: Session, row: WebsiteAdsUploadFingerprint, exc: Exception) -> None:
    row.status = "FAILED"
    row.error_message = f"{type(exc).__name__}: {exc}"[:4000]
    row.updated_at = utcnow()
    db.add(row)
    db.commit()
