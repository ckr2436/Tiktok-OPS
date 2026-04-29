"""Production hardening patches for OpenAI Whisper background tasks.

This module is intentionally small and imported after ``tasks`` registration.  It
patches task helper functions that interact with third-party video metadata so a
bad title or platform response cannot leave jobs stuck in ``processing``.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse

from yt_dlp import YoutubeDL

from . import repository, storage, tasks

logger = logging.getLogger("gmv.tasks.openai_whisper.hardening")

MAX_DISPLAY_FILENAME_CHARS = 240
DEFAULT_EXTENSION = "mp4"
_BROWSER_UA = getattr(
    tasks,
    "DEFAULT_BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # avoid path separators / awkward file-system chars when the display name is
    # reused as a downloaded filename by browsers
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or "shared-video"


def _safe_extension(ext: object) -> str:
    value = str(ext or DEFAULT_EXTENSION).strip().lower().lstrip(".")
    value = re.sub(r"[^a-z0-9]+", "", value)[:12]
    return value or DEFAULT_EXTENSION


def safe_display_filename(title: object, ext: object = DEFAULT_EXTENSION, *, max_chars: int = MAX_DISPLAY_FILENAME_CHARS) -> str:
    """Return a DB/UI safe filename while preserving a useful title preview."""

    suffix = f".{_safe_extension(ext)}"
    base = _clean_text(title)
    if base.lower().endswith(suffix.lower()):
        base = base[: -len(suffix)].rstrip(" .") or "shared-video"
    max_base_chars = max(24, int(max_chars) - len(suffix))
    if len(base) > max_base_chars:
        base = base[: max_base_chars - 1].rstrip(" .") + "…"
    return f"{base}{suffix}"


def _detect_site_from_url(url: str) -> str | None:
    try:
        host = (urlparse(url).netloc or urlparse(url).path).lower()
    except Exception:
        return None
    if any(token in host for token in ("douyin.com", "iesdouyin.com", "amemv.com", "snssdk.com")):
        return "douyin"
    if "tiktok.com" in host:
        return "tiktok"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "facebook.com" in host or "fb.watch" in host or "fbcdn.net" in host:
        return "facebook"
    return None


def _site_headers(site: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if site == "douyin":
        headers["Referer"] = "https://www.douyin.com/"
        headers["Origin"] = "https://www.douyin.com"
    elif site == "tiktok":
        headers["Referer"] = "https://www.tiktok.com/"
        headers["Origin"] = "https://www.tiktok.com"
    elif site == "youtube":
        headers["Referer"] = "https://www.youtube.com/"
        headers["Origin"] = "https://www.youtube.com"
    elif site == "facebook":
        headers["Referer"] = "https://www.facebook.com/"
        headers["Origin"] = "https://www.facebook.com"
    return headers


def _probe_downloadable(share_url: str, cookiefile_path: str | None = None, site: str | None = None) -> Tuple[dict, str]:
    site = site or _detect_site_from_url(share_url)
    options = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "http_headers": _site_headers(site),
    }
    if cookiefile_path:
        options["cookiefile"] = cookiefile_path
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(share_url, download=False)
    except Exception as exc:  # noqa: BLE001
        if tasks._is_authentication_required(exc):
            raise tasks.DownloadRequiresAuthError(str(exc)) from exc
        raise

    entry = tasks._pick_entry(info or {})
    download_url = entry.get("url")
    if not download_url:
        for fmt in reversed(entry.get("formats") or []):
            if fmt.get("url"):
                download_url = fmt.get("url")
                break
    if not download_url:
        raise RuntimeError("分享链接无法生成下载地址，请更换链接。")
    return entry, _safe_extension(entry.get("ext") or DEFAULT_EXTENSION)


def _download_shared_video(
    workspace_id: int,
    job_id: str,
    share_url: str,
    video_path: Path | None,
    *,
    cookiefile_path: str | None,
) -> Tuple[Path, str, str | None]:
    site = _detect_site_from_url(share_url)
    entry, ext = _probe_downloadable(share_url, cookiefile_path, site=site)
    directory = storage.job_dir(workspace_id, job_id)
    display_filename = safe_display_filename(entry.get("title") or entry.get("id") or "shared-video", ext)

    target_path = video_path or directory / f"input.{ext}"
    target_path = target_path.with_suffix(f".{ext}") if target_path.suffix else target_path.with_name(target_path.name + f".{ext}")

    content_type, _ = mimetypes.guess_type(display_filename)
    options = {
        "outtmpl": str(target_path),
        "quiet": True,
        "noplaylist": True,
        "merge_output_format": ext,
        "nopart": True,
        "http_headers": _site_headers(site),
    }
    if cookiefile_path:
        options["cookiefile"] = cookiefile_path

    try:
        with YoutubeDL(options) as ydl:
            ydl.download([share_url])
    except Exception as exc:  # noqa: BLE001
        if tasks._is_authentication_required(exc):
            raise tasks.DownloadRequiresAuthError(str(exc)) from exc
        raise

    if not target_path.exists():
        raise RuntimeError("视频下载失败，请稍后重试或更换链接。")
    return target_path, display_filename, content_type


def _mark_download_status(
    db,
    *,
    workspace_id: int,
    job_id: str,
    status: str,
    message: str | None = None,
    download_url: str | None = None,
    video_path: Path | None = None,
    filename: str | None = None,
    size: int | None = None,
    content_type: str | None = None,
) -> dict:
    safe_filename = safe_display_filename(filename or (video_path.name if video_path else "shared-video"), Path(filename or video_path.name if (filename or video_path) else "shared-video.mp4").suffix or DEFAULT_EXTENSION) if filename or video_path else None

    def _apply(meta: dict) -> dict:
        meta["download_status"] = status
        meta["download_error"] = message
        if video_path:
            meta["video_path"] = str(video_path)
        if safe_filename:
            meta["filename"] = safe_filename
        if size is not None:
            meta["size"] = size
        if content_type:
            meta["content_type"] = content_type
        if download_url is not None:
            meta["download_url"] = download_url
        meta["status"] = storage.derive_overall_status(meta)
        return meta

    metadata = storage.update_metadata(workspace_id, job_id, _apply)
    try:
        repository.update_download_status(
            db,
            workspace_id=workspace_id,
            job_id=job_id,
            status=status,
            message=message,
            download_url=download_url,
        )
        if status == "success":
            repository.update_downloaded_file(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                filename=metadata.get("filename"),
                file_size=size,
                content_type=content_type,
                video_path=str(video_path) if video_path else None,
                download_url=download_url,
            )
        db.flush()
    except Exception as exc:  # noqa: BLE001
        # Never leave the worker session in PendingRollbackError.  The JSON
        # metadata has already been updated, and automatic cleanup can reconcile
        # terminal jobs later.
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception(
            "failed to persist download status to database",
            extra={
                "workspace_id": workspace_id,
                "job_id": job_id,
                "status": status,
                "filename_len": len(str(metadata.get("filename") or "")),
                "error": str(exc),
            },
        )
        raise
    return metadata


def _ensure_local_video(db, workspace_id: int, job_id: str, metadata: dict):
    raw_video_path = metadata.get("video_path")
    video_path = Path(raw_video_path) if raw_video_path else None
    share_url = (metadata.get("share_url") or "").strip()
    download_url = f"/api/v1/tenants/{workspace_id}/openai-whisper/jobs/{job_id}/video"

    if share_url:
        if video_path and video_path.exists():
            if (metadata.get("download_status") or "") != "success":
                metadata = _mark_download_status(
                    db,
                    workspace_id=workspace_id,
                    job_id=job_id,
                    status="success",
                    download_url=download_url,
                    video_path=video_path,
                    filename=metadata.get("filename"),
                    size=metadata.get("size"),
                    content_type=metadata.get("content_type"),
                )
            return metadata, video_path, None

        try:
            _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="processing")
            db.commit()
            cookiefile_path = tasks._load_cookiefile_for_share(share_url, job_id)
            video_path, filename, content_type = _download_shared_video(
                workspace_id,
                job_id,
                share_url,
                video_path,
                cookiefile_path=cookiefile_path,
            )
            size = video_path.stat().st_size
            metadata = _mark_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="success",
                download_url=download_url,
                video_path=video_path,
                filename=filename or video_path.name,
                size=size,
                content_type=content_type,
            )
            db.commit()
            return metadata, video_path, None
        except tasks.DownloadRequiresAuthError as exc:
            db.rollback()
            site = _detect_site_from_url(share_url)
            if site == "douyin":
                message = "该抖音分享视频仍被平台判定需要新鲜 Cookies。请在同一浏览器打开这条视频并确认能播放，然后重新导出完整 douyin.com Cookies 后再试。"
            else:
                message = "该分享视频需要登录授权才能下载，请登录后重新复制可访问的链接。"
            _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
            db.commit()
            logger.warning("whisper download requires auth", extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc), "site": site})
            return None, None, message
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            message = "视频下载失败，请稍后重试或更换链接。"
            try:
                _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("failed to persist failed download status", extra={"workspace_id": workspace_id, "job_id": job_id})
            logger.exception("whisper download failed", extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)})
            return None, None, message

    if not video_path or not video_path.exists():
        message = "视频源文件已丢失，无法继续。"
        _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
        db.commit()
        return None, None, message

    if (metadata.get("download_status") or "") in {"pending", "processing"}:
        metadata = _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="success")
        db.commit()
    return metadata, video_path, None


def apply() -> None:
    tasks._detect_site_from_url = _detect_site_from_url
    tasks._site_headers = _site_headers
    tasks._probe_downloadable = _probe_downloadable
    tasks._download_shared_video = _download_shared_video
    tasks._mark_download_status = _mark_download_status
    tasks._ensure_local_video = _ensure_local_video
    logger.info("OpenAI Whisper runtime hardening patches applied")
