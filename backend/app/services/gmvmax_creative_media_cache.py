from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.gmvmax_creative_assets import ensure_creative_asset_cache_table
from app.services.website_ads_media_cache import (
    _download_asset_file as download_asset_file,
    _generate_cover as generate_video_cover,
)


logger = logging.getLogger("gmv.services.gmvmax.creative_media_cache")


def _safe_segment(value: object) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    return clean[:128] or "unknown"


def media_root() -> Path:
    return Path(str(settings.GMVMAX_MEDIA_STORAGE_DIR)).expanduser().resolve()


def ensure_media_root() -> Path:
    root = media_root()
    (root / "assets").mkdir(parents=True, exist_ok=True)
    return root


def asset_directory(row: Mapping[str, Any]) -> Path:
    return (
        ensure_media_root()
        / "assets"
        / str(int(row["workspace_id"]))
        / str(int(row["auth_id"]))
        / _safe_segment(row.get("advertiser_id"))
        / _safe_segment(row.get("store_id"))
        / f"{int(row['id'])}-{_safe_segment(row.get('item_id'))}"
    )


def public_creative_media_url(row: Mapping[str, Any], kind: str) -> str:
    normalized = "cover" if kind == "cover" else "video"
    return (
        f"/api/v1/tenants/{int(row['workspace_id'])}/providers/tiktok-business/accounts/{int(row['auth_id'])}"
        f"/gmvmax/creative-assets/{int(row['id'])}/{normalized}"
    )


def _contained_path(value: object) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    root = media_root()
    if path == root or root not in path.parents:
        return None
    return path


def resolve_creative_media(row: Mapping[str, Any], kind: str) -> tuple[Path, str] | None:
    if kind == "cover":
        path = _contained_path(row.get("local_cover_path"))
        content_type = str(row.get("cover_content_type") or "image/jpeg")
        pattern = "cover.*"
    else:
        path = _contained_path(row.get("local_preview_path"))
        content_type = str(row.get("preview_content_type") or "video/mp4")
        pattern = "video.*"
    if path and path.is_file() and path.stat().st_size > 0:
        return path, content_type

    try:
        directory = asset_directory(row)
    except (KeyError, TypeError, ValueError):
        return None
    for candidate in sorted(directory.glob(pattern)):
        if candidate.is_file() and candidate.stat().st_size > 0:
            fallback = "image/jpeg" if kind == "cover" else "video/mp4"
            return candidate, fallback
    return None


def creative_media_urls(row: Mapping[str, Any]) -> dict[str, str | None]:
    video = resolve_creative_media(row, "video")
    cover = resolve_creative_media(row, "cover")
    return {
        "preview_url": public_creative_media_url(row, "video") if video else None,
        "video_cover_url": public_creative_media_url(row, "cover") if cover else None,
    }


def claim_creative_media_cache_batch(db: Session, *, limit: int) -> list[int]:
    ensure_creative_asset_cache_table(db)
    rows = db.execute(
        text(
            """
            select id
            from gmvmax_creative_asset_cache
            where (preview_url is not null or video_cover_url is not null)
              and coalesce(
                    json_unquote(json_extract(raw_json, '$._gmv_ops_sync.active')),
                    'true'
                  ) <> 'false'
              and (
                media_cache_status in ('PENDING', 'PARTIAL', 'ERROR', 'SOURCE_EXPIRED')
                or (media_cache_status in ('QUEUED', 'PROCESSING')
                    and updated_at < date_sub(current_timestamp(6), interval 30 minute))
                or (media_cache_status='READY'
                    and (local_preview_path is null or local_cover_path is null))
              )
              and (media_cache_next_retry_at is null or media_cache_next_retry_at <= current_timestamp(6))
            order by
              coalesce(media_cache_next_retry_at, updated_at, fetched_at, created_at) asc,
              id asc
            limit :limit
            for update skip locked
            """
        ),
        {"limit": max(1, int(limit))},
    ).scalars().all()
    asset_ids = [int(value) for value in rows]
    if asset_ids:
        db.execute(
            text(
                """
                update gmvmax_creative_asset_cache
                set media_cache_status='QUEUED', media_cache_error=null,
                    media_cache_next_retry_at=null, updated_at=current_timestamp(6)
                where id in :asset_ids
                """
            ).bindparams(bindparam("asset_ids", expanding=True)),
            {"asset_ids": asset_ids},
        )
    db.commit()
    return asset_ids


def mark_creative_media_queue_error(db: Session, asset_id: int, exc: BaseException) -> None:
    db.execute(
        text(
            """
            update gmvmax_creative_asset_cache
            set media_cache_status='ERROR', media_cache_error=:error,
                media_cache_next_retry_at=date_add(current_timestamp(6), interval 5 minute),
                updated_at=current_timestamp(6)
            where id=:asset_id
            """
        ),
        {"asset_id": int(asset_id), "error": f"QueueError: {type(exc).__name__}: {exc}"[:2000]},
    )
    db.commit()


def _is_expired_source_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return int(exc.response.status_code) in {401, 403, 404, 410}
    message = str(exc).lower()
    return any(token in message for token in ("expired", "signature", "access denied", "forbidden"))


async def cache_creative_asset_media(db: Session, asset_id: int) -> dict[str, Any]:
    ensure_creative_asset_cache_table(db)
    row = db.execute(
        text("select * from gmvmax_creative_asset_cache where id=:asset_id limit 1"),
        {"asset_id": int(asset_id)},
    ).mappings().first()
    if not row:
        raise ValueError("GMV Max creative asset is unavailable")
    payload = dict(row)
    db.execute(
        text(
            """
            update gmvmax_creative_asset_cache
            set media_cache_status='PROCESSING', media_cache_error=null,
                updated_at=current_timestamp(6)
            where id=:asset_id
            """
        ),
        {"asset_id": int(asset_id)},
    )
    db.commit()

    video = resolve_creative_media(payload, "video")
    cover = resolve_creative_media(payload, "cover")
    errors: dict[str, str] = {}
    expired_source = False
    directory = asset_directory(payload)

    if video is None:
        source = str(payload.get("preview_url") or "").strip()
        if source.startswith(("http://", "https://")):
            try:
                entry = await download_asset_file(source, directory / "video", image=False)
                payload["local_preview_path"] = entry["path"]
                payload["preview_content_type"] = entry["content_type"]
                video = resolve_creative_media(payload, "video")
            except Exception as exc:  # noqa: BLE001
                errors["video"] = f"{type(exc).__name__}: {exc}"[:1000]
                expired_source = expired_source or _is_expired_source_error(exc)
        else:
            errors["video"] = "SourceUnavailable: TikTok did not return a downloadable video URL"

    # A cached video is the durable source of truth.  Generate its cover
    # locally before touching TikTok's temporary cover URL; this avoids an
    # unnecessary upstream request and keeps expired CDN URLs out of the hot
    # path.  The remote cover is only a fallback when local extraction fails
    # or the video itself is unavailable.
    if cover is None and video is not None:
        try:
            entry = generate_video_cover(video[0], directory / "cover.jpg")
            payload["local_cover_path"] = entry["path"]
            payload["cover_content_type"] = entry["content_type"]
            cover = resolve_creative_media(payload, "cover")
        except Exception as exc:  # noqa: BLE001
            errors["cover_generate"] = f"{type(exc).__name__}: {exc}"[:1000]

    if cover is None:
        source = str(payload.get("video_cover_url") or "").strip()
        if source.startswith(("http://", "https://")):
            try:
                entry = await download_asset_file(source, directory / "cover", image=True)
                payload["local_cover_path"] = entry["path"]
                payload["cover_content_type"] = entry["content_type"]
                cover = resolve_creative_media(payload, "cover")
                errors.pop("cover_generate", None)
            except Exception as exc:  # noqa: BLE001
                errors["cover_download"] = f"{type(exc).__name__}: {exc}"[:1000]
                expired_source = expired_source or _is_expired_source_error(exc)

    video_ready = video is not None
    cover_ready = cover is not None
    attempts = int(payload.get("media_cache_attempts") or 0) + (1 if errors else 0)
    if video_ready and cover_ready:
        cache_status = "READY"
        error_text = None
    else:
        cache_status = "SOURCE_EXPIRED" if expired_source else ("PARTIAL" if video_ready or cover_ready else "ERROR")
        delay_minutes = min(360, 5 * (2 ** min(max(attempts - 1, 0), 6)))
        error_text = "; ".join(f"{key}: {value}" for key, value in errors.items())[:4000]

    db.execute(
        text(
            """
            update gmvmax_creative_asset_cache
            set local_preview_path=:local_preview_path,
                local_cover_path=:local_cover_path,
                preview_content_type=:preview_content_type,
                cover_content_type=:cover_content_type,
                media_cache_status=:media_cache_status,
                media_cache_error=:media_cache_error,
                media_cache_attempts=:media_cache_attempts,
                media_cache_next_retry_at=case
                    when :ready=1 then null
                    else date_add(current_timestamp(6), interval :retry_delay_minutes minute)
                end,
                media_cached_at=case when :ready=1 then current_timestamp(6) else media_cached_at end,
                updated_at=current_timestamp(6)
            where id=:asset_id
            """
        ),
        {
            "asset_id": int(asset_id),
            "local_preview_path": payload.get("local_preview_path"),
            "local_cover_path": payload.get("local_cover_path"),
            "preview_content_type": payload.get("preview_content_type"),
            "cover_content_type": payload.get("cover_content_type"),
            "media_cache_status": cache_status,
            "media_cache_error": error_text,
            "media_cache_attempts": attempts,
            "retry_delay_minutes": 0 if cache_status == "READY" else delay_minutes,
            "ready": 1 if cache_status == "READY" else 0,
        },
    )
    db.commit()
    return {
        "asset_id": int(asset_id),
        "status": cache_status,
        "video_cached": video_ready,
        "cover_cached": cover_ready,
        "errors": errors,
    }


__all__ = [
    "cache_creative_asset_media",
    "claim_creative_media_cache_batch",
    "creative_media_urls",
    "ensure_media_root",
    "mark_creative_media_queue_error",
    "media_root",
    "public_creative_media_url",
    "resolve_creative_media",
]
