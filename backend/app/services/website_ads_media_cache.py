from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.data.models.website_ads import WebsiteAdsCreativeAsset


LOCAL_CACHE_KEY = "_local_media_cache"
TIKTOK_SOURCE_KEY = "_tiktok_media_source"
MAX_MEDIA_BYTES = 500 * 1024 * 1024

_VIDEO_CONTENT_TYPES = {
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
}
_IMAGE_CONTENT_TYPES = {
    ".avif": "image/avif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class ArchivedMedia:
    path: Path
    sha256: str
    md5: str
    size_bytes: int
    content_type: str
    original_name: str


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def media_root() -> Path:
    return Path(str(settings.WEBSITE_ADS_MEDIA_STORAGE_DIR)).expanduser().resolve()


def ensure_media_root() -> Path:
    root = media_root()
    for relative in ("incoming", "uploads/sha256", "assets"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def cleanup_stale_media_partials(*, retention_minutes: int | None = None) -> int:
    root = ensure_media_root()
    minutes = max(
        30,
        int(retention_minutes or settings.WEBSITE_ADS_MEDIA_PARTIAL_RETENTION_MINUTES),
    )
    cutoff = time.time() - minutes * 60
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file() or not (path.name.endswith(".part") or ".part." in path.name):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def _safe_segment(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._")
    if text:
        return text[:128]
    return hashlib.sha256(str(value or "empty").encode("utf-8")).hexdigest()[:24]


def _extension(file_name: str, content_type: str, *, image: bool = False) -> str:
    allowed = _IMAGE_CONTENT_TYPES if image else _VIDEO_CONTENT_TYPES
    suffix = Path(file_name or "").suffix.lower()
    if suffix in allowed:
        return suffix
    guessed = mimetypes.guess_extension(str(content_type or "").split(";", 1)[0].strip()) or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    return guessed if guessed in allowed else (".jpg" if image else ".mp4")


def _content_type(path: Path, fallback: str, *, image: bool = False) -> str:
    known = _IMAGE_CONTENT_TYPES if image else _VIDEO_CONTENT_TYPES
    return known.get(path.suffix.lower()) or str(fallback or "").split(";", 1)[0] or (
        "image/jpeg" if image else "video/mp4"
    )


def _contained_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser().resolve()
    root = media_root()
    if path == root or root not in path.parents:
        return None
    return path


def archive_stream(
    stream: BinaryIO,
    *,
    file_name: str,
    content_type: str,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> ArchivedMedia:
    root = ensure_media_root()
    suffix = _extension(file_name, content_type)
    partial = root / "incoming" / f"{uuid4().hex}{suffix}.part"
    sha256_digest = hashlib.sha256()
    md5_digest = hashlib.md5(usedforsecurity=False)
    total = 0
    try:
        if hasattr(stream, "seek"):
            stream.seek(0)
        with partial.open("xb") as handle:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > int(max_bytes):
                    raise ValueError("Video file exceeds the 500 MB TikTok limit")
                sha256_digest.update(chunk)
                md5_digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total <= 0:
            raise ValueError("Video file is empty")
        sha256 = sha256_digest.hexdigest()
        destination = root / "uploads" / "sha256" / sha256[:2] / f"{sha256}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_size == total:
            partial.unlink(missing_ok=True)
        else:
            partial.replace(destination)
        return ArchivedMedia(
            path=destination,
            sha256=sha256,
            md5=md5_digest.hexdigest(),
            size_bytes=total,
            content_type=_content_type(destination, content_type),
            original_name=str(file_name or destination.name),
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise


async def archive_remote_url(
    url: str,
    *,
    file_name: str,
    max_bytes: int = MAX_MEDIA_BYTES,
) -> ArchivedMedia:
    root = ensure_media_root()
    initial_suffix = _extension(file_name or Path(urlsplit(url).path).name, "video/mp4")
    partial = root / "incoming" / f"{uuid4().hex}{initial_suffix}.part"
    sha256_digest = hashlib.sha256()
    md5_digest = hashlib.md5(usedforsecurity=False)
    total = 0
    content_type = "video/mp4"
    timeout_seconds = float(settings.WEBSITE_ADS_MEDIA_DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=20.0),
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url, headers={"User-Agent": "GMV-Ops/1.0"}) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("content-type") or "video/mp4").split(";", 1)[0]
                if content_type in {"application/octet-stream", "binary/octet-stream"}:
                    content_type = "video/mp4"
                if not content_type.startswith("video/"):
                    raise ValueError(f"Remote URL did not return a video ({content_type})")
                with partial.open("xb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > int(max_bytes):
                            raise ValueError("Video file exceeds the 500 MB TikTok limit")
                        sha256_digest.update(chunk)
                        md5_digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        if total <= 0:
            raise ValueError("Remote video download returned an empty file")
        sha256 = sha256_digest.hexdigest()
        suffix = _extension(file_name or Path(urlsplit(url).path).name, content_type)
        destination = root / "uploads" / "sha256" / sha256[:2] / f"{sha256}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_size == total:
            partial.unlink(missing_ok=True)
        else:
            partial.replace(destination)
        return ArchivedMedia(
            path=destination,
            sha256=sha256,
            md5=md5_digest.hexdigest(),
            size_bytes=total,
            content_type=_content_type(destination, content_type),
            original_name=str(file_name or destination.name),
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def unique_tiktok_file_name(file_name: str, content_sha256: str) -> str:
    original = Path(str(file_name or "video.mp4").strip()).name
    suffix = _extension(original, "video/mp4")
    stem = Path(original).stem.strip() or "video"
    token = f"-{content_sha256[:10]}"
    max_stem = max(1, 100 - len(token) - len(suffix))
    return f"{stem[:max_stem]}{token}{suffix}"


def asset_directory(asset: WebsiteAdsCreativeAsset) -> Path:
    return (
        ensure_media_root()
        / "assets"
        / str(int(asset.workspace_id))
        / str(int(asset.auth_id))
        / _safe_segment(asset.advertiser_id)
        / f"{int(asset.id)}-{_safe_segment(asset.video_id)}"
    )


def public_asset_media_url(asset: WebsiteAdsCreativeAsset, kind: str) -> str:
    normalized = "cover" if kind == "cover" else "video"
    return (
        f"/api/v1/tenants/{int(asset.workspace_id)}/providers/tiktok-business/accounts/{int(asset.auth_id)}"
        f"/website-ads/creative-assets/{int(asset.id)}/{normalized}"
    )


def resolve_asset_media(asset: WebsiteAdsCreativeAsset, kind: str) -> tuple[Path, str] | None:
    raw = asset.raw_json if isinstance(asset.raw_json, dict) else {}
    cache = raw.get(LOCAL_CACHE_KEY) if isinstance(raw.get(LOCAL_CACHE_KEY), dict) else {}
    entry = cache.get(kind) if isinstance(cache.get(kind), dict) else {}
    path = _contained_path(entry.get("path"))
    fallback = "image/jpeg" if kind == "cover" else "video/mp4"
    if path and path.is_file() and path.stat().st_size > 0:
        return path, str(entry.get("content_type") or fallback)

    allowed = _IMAGE_CONTENT_TYPES if kind == "cover" else _VIDEO_CONTENT_TYPES
    directory = asset_directory(asset)
    for candidate in sorted(directory.glob(f"{kind}.*")):
        if candidate.suffix.lower() in allowed and candidate.is_file() and candidate.stat().st_size > 0:
            return candidate, _content_type(candidate, fallback, image=kind == "cover")
    return None


def _recovered_media_entry(path: Path, content_type: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {
        "path": str(path),
        "content_type": content_type,
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "cached_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "recovered_from_disk": True,
    }


def merge_tiktok_media_metadata(
    existing_raw: dict[str, Any] | None,
    incoming_raw: dict[str, Any] | None,
    *,
    video_url: object = None,
    cover_url: object = None,
) -> dict[str, Any]:
    existing = dict(existing_raw or {})
    merged = dict(incoming_raw or {})
    cache = existing.get(LOCAL_CACHE_KEY)
    if isinstance(cache, dict):
        merged[LOCAL_CACHE_KEY] = dict(cache)
    source = dict(existing.get(TIKTOK_SOURCE_KEY) or {}) if isinstance(existing.get(TIKTOK_SOURCE_KEY), dict) else {}
    should_retry = False
    for key, value in (("video_url", video_url), ("cover_url", cover_url)):
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            cache_key = "video" if key == "video_url" else "cover"
            if source.get(key) != text and not isinstance((cache or {}).get(cache_key), dict):
                should_retry = True
            source[key] = text
    if source:
        merged[TIKTOK_SOURCE_KEY] = source
    if should_retry and isinstance(merged.get(LOCAL_CACHE_KEY), dict):
        merged[LOCAL_CACHE_KEY]["state"] = "PENDING"
        merged[LOCAL_CACHE_KEY].pop("checked_at", None)
        merged[LOCAL_CACHE_KEY].pop("last_errors", None)
    return merged


def _remote_source(asset: WebsiteAdsCreativeAsset, kind: str) -> str:
    raw = asset.raw_json if isinstance(asset.raw_json, dict) else {}
    source = raw.get(TIKTOK_SOURCE_KEY) if isinstance(raw.get(TIKTOK_SOURCE_KEY), dict) else {}
    video_info = raw.get("video_info") if isinstance(raw.get("video_info"), dict) else {}
    if kind == "cover":
        values = (
            source.get("cover_url"),
            raw.get("video_cover_url"),
            raw.get("cover_url"),
            video_info.get("poster_url"),
            asset.cover_url,
        )
    else:
        values = (
            source.get("video_url"),
            raw.get("preview_url"),
            raw.get("video_url"),
            raw.get("play_url"),
            raw.get("download_url"),
            video_info.get("preview_url"),
            asset.preview_url,
        )
    for value in values:
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            return text
    return ""


def _should_retry_media_download(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(exc.response.status_code)
        return status in {408, 409, 416, 425, 429} or status >= 500
    if isinstance(exc, ValueError):
        return False
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError, OSError))


async def _download_asset_file(url: str, target_stem: Path, *, image: bool) -> dict[str, Any]:
    timeout_seconds = float(settings.WEBSITE_ADS_MEDIA_DOWNLOAD_TIMEOUT_SECONDS)
    partial = target_stem.parent / f".{target_stem.name}-{uuid4().hex}.part"
    content_type = "image/jpeg" if image else "video/mp4"
    attempts = max(1, int(settings.WEBSITE_ADS_MEDIA_DOWNLOAD_ATTEMPTS))
    try:
        target_stem.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=20.0),
            follow_redirects=True,
        ) as client:
            for attempt in range(attempts):
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {"User-Agent": "GMV-Ops/1.0", "Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                try:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code == 416 and offset:
                            partial.unlink(missing_ok=True)
                            raise httpx.HTTPStatusError(
                                "CDN rejected the resume range",
                                request=response.request,
                                response=response,
                            )
                        response.raise_for_status()
                        content_type = str(response.headers.get("content-type") or content_type).split(";", 1)[0]
                        expected_prefix = "image/" if image else "video/"
                        if content_type in {"application/octet-stream", "binary/octet-stream"}:
                            content_type = "image/jpeg" if image else "video/mp4"
                        if not content_type.startswith(expected_prefix):
                            raise ValueError(
                                f"Media URL returned {content_type}, expected {expected_prefix.rstrip('/')}"
                            )
                        append = bool(offset and response.status_code == 206)
                        if not append:
                            offset = 0
                        content_length = response.headers.get("content-length")
                        expected_total = offset + int(content_length) if content_length else None
                        if expected_total is not None and expected_total > MAX_MEDIA_BYTES:
                            raise ValueError("Cached media exceeds the 500 MB limit")
                        total = offset
                        with partial.open("ab" if append else "wb") as handle:
                            async for chunk in response.aiter_raw(1024 * 1024):
                                total += len(chunk)
                                if total > MAX_MEDIA_BYTES:
                                    raise ValueError("Cached media exceeds the 500 MB limit")
                                handle.write(chunk)
                            handle.flush()
                            os.fsync(handle.fileno())
                        if expected_total is not None and total != expected_total:
                            raise IOError(f"Incomplete CDN response: received {total} of {expected_total} bytes")
                        break
                except Exception as exc:
                    if attempt + 1 >= attempts or not _should_retry_media_download(exc):
                        raise
                    await asyncio.sleep(min(8, 2 ** attempt))
            else:
                raise IOError("Media download retries were exhausted")
        total = partial.stat().st_size if partial.exists() else 0
        if total <= 0:
            raise ValueError("Cached media download returned an empty file")
        suffix = _extension(Path(urlsplit(url).path).name, content_type, image=image)
        target = target_stem.with_suffix(suffix)
        partial.replace(target)
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return {
            "path": str(target),
            "content_type": _content_type(target, content_type, image=image),
            "size_bytes": total,
            "sha256": digest.hexdigest(),
            "cached_at": _utcnow_iso(),
        }
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _link_archived_video(asset: WebsiteAdsCreativeAsset, archived: ArchivedMedia) -> dict[str, Any]:
    directory = asset_directory(asset)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"video{archived.path.suffix.lower()}"
    if not target.exists() or target.stat().st_size != archived.size_bytes:
        target.unlink(missing_ok=True)
        try:
            os.link(archived.path, target)
        except OSError:
            shutil.copy2(archived.path, target)
    return {
        "path": str(target),
        "content_type": archived.content_type,
        "size_bytes": archived.size_bytes,
        "sha256": archived.sha256,
        "cached_at": _utcnow_iso(),
        "archive_path": str(archived.path),
    }


def attach_archived_video(asset: WebsiteAdsCreativeAsset, archived: ArchivedMedia) -> None:
    raw = dict(asset.raw_json or {})
    cache = dict(raw.get(LOCAL_CACHE_KEY) or {}) if isinstance(raw.get(LOCAL_CACHE_KEY), dict) else {}
    cache["video"] = _link_archived_video(asset, archived)
    raw[LOCAL_CACHE_KEY] = cache
    asset.raw_json = raw
    asset.preview_url = public_asset_media_url(asset, "video")


def _generate_cover(video_path: Path, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}-{uuid4().hex}.part.jpg")
    command = [
        str(settings.OPENAI_WHISPER_FFMPEG_BIN),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0.5",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(partial),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=90)
        if not partial.exists() or partial.stat().st_size <= 0:
            raise ValueError("ffmpeg did not produce a cover image")
        partial.replace(target)
        content = target.read_bytes()
        return {
            "path": str(target),
            "content_type": "image/jpeg",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "cached_at": _utcnow_iso(),
            "generated_from_video": True,
        }
    finally:
        partial.unlink(missing_ok=True)


async def ensure_asset_media_cache(db: Session, asset_id: int) -> dict[str, Any]:
    asset = db.get(WebsiteAdsCreativeAsset, int(asset_id))
    if not asset:
        raise ValueError("Creative asset is unavailable")
    raw = dict(asset.raw_json or {})
    cache = dict(raw.get(LOCAL_CACHE_KEY) or {}) if isinstance(raw.get(LOCAL_CACHE_KEY), dict) else {}
    cache["state"] = "PROCESSING"
    cache["processing_at"] = _utcnow_iso()
    raw[LOCAL_CACHE_KEY] = cache
    asset.raw_json = raw
    db.add(asset)
    db.commit()
    errors: dict[str, str] = {}

    video = resolve_asset_media(asset, "video")
    if video is not None and not isinstance(cache.get("video"), dict):
        cache["video"] = await asyncio.to_thread(_recovered_media_entry, video[0], video[1])
    if video is None:
        source_url = _remote_source(asset, "video")
        if source_url:
            try:
                cache["video"] = await _download_asset_file(
                    source_url,
                    asset_directory(asset) / "video",
                    image=False,
                )
                raw[LOCAL_CACHE_KEY] = cache
                asset.raw_json = raw
                video = resolve_asset_media(asset, "video")
            except Exception as exc:
                errors["video"] = f"{type(exc).__name__}: {exc}"[:1000]
        else:
            errors["video"] = "SourceUnavailable: TikTok did not return a downloadable video URL"

    cover = resolve_asset_media(asset, "cover")
    if cover is not None and not isinstance(cache.get("cover"), dict):
        cache["cover"] = await asyncio.to_thread(_recovered_media_entry, cover[0], cover[1])
    if cover is None:
        cover_url = _remote_source(asset, "cover")
        if cover_url:
            try:
                cache["cover"] = await _download_asset_file(
                    cover_url,
                    asset_directory(asset) / "cover",
                    image=True,
                )
            except Exception as exc:
                errors["cover_download"] = f"{type(exc).__name__}: {exc}"[:1000]
        if not isinstance(cache.get("cover"), dict) and video is not None:
            try:
                cache["cover"] = await asyncio.to_thread(
                    _generate_cover,
                    video[0],
                    asset_directory(asset) / "cover.jpg",
                )
                errors.pop("cover_download", None)
            except Exception as exc:
                errors["cover"] = f"{type(exc).__name__}: {exc}"[:1000]

    raw[LOCAL_CACHE_KEY] = cache
    if errors:
        raw[LOCAL_CACHE_KEY]["last_errors"] = errors
        raw[LOCAL_CACHE_KEY]["state"] = "PARTIAL"
    else:
        raw[LOCAL_CACHE_KEY].pop("last_errors", None)
        raw[LOCAL_CACHE_KEY]["state"] = "READY"
    raw[LOCAL_CACHE_KEY]["checked_at"] = _utcnow_iso()
    asset.raw_json = dict(raw)
    flag_modified(asset, "raw_json")
    if resolve_asset_media(asset, "video"):
        asset.preview_url = public_asset_media_url(asset, "video")
    if resolve_asset_media(asset, "cover"):
        asset.cover_url = public_asset_media_url(asset, "cover")
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return {
        "asset_id": int(asset.id),
        "video_cached": resolve_asset_media(asset, "video") is not None,
        "cover_cached": resolve_asset_media(asset, "cover") is not None,
        "errors": errors,
    }
