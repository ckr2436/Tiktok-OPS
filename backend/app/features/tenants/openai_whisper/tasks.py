"""Celery tasks for running Whisper transcriptions in the background."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Tuple
from urllib.parse import urlparse

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.services import video_site_cookies
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from yt_dlp import YoutubeDL

from . import repository, storage, transcriber
from .url_security import (
    UnsafeShareURLError,
    resolve_safe_share_url,
    validate_extracted_media_urls,
)

logger = logging.getLogger("gmv.tasks.openai_whisper")
WHISPER_TASK_QUEUE = (
    getattr(settings, "OPENAI_WHISPER_TASK_QUEUE", None)
    or getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "gmv.tasks.default")
)
ALLOWED_CONTACT_INTERVALS = {0.5, 1.0, 1.5, 2.0}
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SESSION_COOKIE_FALLBACK_TTL_SECONDS = 30 * 24 * 3600


class DownloadRequiresAuthError(RuntimeError):
    """Raised when a share link requires authentication to download."""


class DownloadTooLargeError(RuntimeError):
    """Raised when a remote file exceeds a byte or workspace quota."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ensure_producer_analysis_ready_notification(db, row, analysis: dict) -> bool:
    """Persist one user-visible Producer notification for imported analyses.

    Public benchmark links carry an explicit pending Producer turn and are
    continued by ``continue_producer_benchmark_turn``.  TikTok Shop analysis
    handoffs are different: the user may already have discussed the partial
    report while pixel-level analysis was still running, so there is no
    pending turn to resume.  Persist a scoped assistant event for that path so
    an open page receives it on the next poll and a closed page sees it when
    the user returns.  ``run_id`` makes recovery and late worker completion
    idempotent.
    """

    from app.data.models.hermes_agent import (
        HermesAgentConversation,
        HermesAgentMessage,
    )
    from app.services.hermes_agent import repository as hermes_repository

    attachment_meta = dict(row.meta_json or {})
    if str(attachment_meta.get("analysis_request_context") or "").strip():
        return False
    conversation = db.scalar(
        select(HermesAgentConversation)
        .where(
            HermesAgentConversation.id == int(row.conversation_id),
            HermesAgentConversation.workspace_id == int(row.workspace_id),
            HermesAgentConversation.user_id == int(row.user_id),
        )
        .with_for_update()
    )
    if conversation is None:
        return False
    conversation_meta = dict(conversation.meta_json or {})
    if str(conversation_meta.get("source_type") or "") != "tiktok_shop_video_analysis":
        return False

    event_run_id = f"benchmark_ready_{int(row.id)}"
    existing = db.scalar(
        select(HermesAgentMessage.id).where(
            HermesAgentMessage.conversation_id == int(conversation.id),
            HermesAgentMessage.workspace_id == int(row.workspace_id),
            HermesAgentMessage.user_id == int(row.user_id),
            HermesAgentMessage.role == "assistant",
            HermesAgentMessage.run_id == event_run_id,
        )
    )
    if existing is not None:
        return False

    hermes_repository.add_message(
        db,
        conversation=conversation,
        workspace_id=int(row.workspace_id),
        user_id=int(row.user_id),
        role="assistant",
        content_text=(
            "对标视频的完整多模态分析已经完成：关键画面、口播、开场钩子、"
            "节奏、叙事推进、产品进入和转化结构均已形成可用结论。现在可以继续"
            "让我基于这份完整分析补全并确认优化方案；在你确认前，我不会自动创建"
            "制作项目。"
        ),
        content_json={
            "event_type": "benchmark_multimodal_analysis_ready",
            "attachment_id": int(row.id),
            "analysis_status": "ready",
            "multimodal_status": "success",
        },
        run_id=event_run_id,
    )
    analysis["producer_notification_status"] = "success"
    analysis["producer_notification_completed_at"] = _utcnow().isoformat()
    analysis["producer_notification_run_id"] = event_run_id
    row.analysis_json = analysis
    flag_modified(row, "analysis_json")
    conversation_meta["last_producer_event"] = {
        "type": "benchmark_multimodal_analysis_ready",
        "attachment_id": int(row.id),
        "run_id": event_run_id,
        "completed_at": analysis["producer_notification_completed_at"],
    }
    conversation.meta_json = conversation_meta
    db.add(conversation)
    return True


def _cutoff_days(days: int) -> datetime:
    return _utcnow() - timedelta(days=max(0, int(days)))


def _producer_attachment_source(path_value: str) -> Path:
    root = (
        Path(
            getattr(
                settings,
                "CONTENT_FACTORY_STORAGE_ROOT",
                "/data/gmv_ops/hermes_content_factory",
            )
        ).expanduser()
        / "producer_intake"
    ).resolve()
    path = Path(path_value).resolve()
    if path == root or root not in path.parents or not path.is_file():
        raise RuntimeError("producer attachment is outside the intake storage scope")
    return path


def _producer_attachment_target(path_value: str) -> Path:
    root = (
        Path(
            getattr(
                settings,
                "CONTENT_FACTORY_STORAGE_ROOT",
                "/data/gmv_ops/hermes_content_factory",
            )
        ).expanduser()
        / "producer_intake"
    ).resolve()
    path = Path(path_value).resolve(strict=False)
    if path == root or root not in path.parents:
        raise RuntimeError("producer attachment target is outside intake storage")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _download_producer_reference_url(row) -> Path:
    meta = dict(row.meta_json or {})
    share_url = str(meta.get("source_url") or "").strip()
    if not share_url:
        raise RuntimeError("producer benchmark URL is missing")
    site = _detect_site_from_url(share_url)
    cookiefile_path = _load_cookiefile_for_share(
        share_url,
        str(getattr(row, "attachment_key", "producer-benchmark")),
    )
    try:
        entry, _ext = _probe_downloadable(
            share_url,
            cookiefile_path,
            site=site,
        )
    except Exception:
        if cookiefile_path:
            Path(cookiefile_path).unlink(missing_ok=True)
        raise
    target = _producer_attachment_target(row.file_path)
    base = target.with_suffix("")
    byte_limit = min(
        200 * 1024 * 1024,
        int(settings.OPENAI_WHISPER_MAX_REMOTE_DOWNLOAD_BYTES),
    )

    def enforce_size(status: dict) -> None:
        downloaded = int(status.get("downloaded_bytes") or 0)
        total = int(
            status.get("total_bytes")
            or status.get("total_bytes_estimate")
            or 0
        )
        if max(downloaded, total) > byte_limit:
            raise DownloadTooLargeError("远程对标视频超过 200 MB 限制。")

    options = {
        "outtmpl": f"{base}.%(ext)s",
        "quiet": True,
        "noplaylist": True,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "max_filesize": byte_limit,
        "progress_hooks": [enforce_size],
        "http_headers": _site_headers(site),
    }
    if cookiefile_path:
        options["cookiefile"] = cookiefile_path
    for stale in target.parent.glob(f"{base.name}.*"):
        if stale.is_file():
            stale.unlink(missing_ok=True)
    try:
        with YoutubeDL(options) as ydl:
            download_entry = dict(entry)
            download_entry.pop("_gmv_safe_share_url", None)
            ydl.process_ie_result(download_entry, download=True)
    except Exception:
        for partial in target.parent.glob(f"{base.name}.*"):
            partial.unlink(missing_ok=True)
        raise
    finally:
        if cookiefile_path:
            Path(cookiefile_path).unlink(missing_ok=True)
    candidates = [
        path
        for path in target.parent.glob(f"{base.name}.*")
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        raise RuntimeError("benchmark video download did not produce a file")
    downloaded = max(candidates, key=lambda path: path.stat().st_size)
    if downloaded.stat().st_size <= 1024 or downloaded.stat().st_size > byte_limit:
        downloaded.unlink(missing_ok=True)
        raise DownloadTooLargeError("远程对标视频为空或超过 200 MB 限制。")
    downloaded.chmod(0o664)
    return downloaded


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _has_audio_stream(path: Path) -> bool:
    command = [
        "/opt/apps/bin/ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(path),
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return bool(str(completed.stdout or "").strip())


def _pick_entry(info: dict) -> dict:
    entries = info.get("entries") or []
    if entries:
        for entry in entries:
            if entry:
                return entry
    return info


def _is_authentication_required(error: Exception) -> bool:
    message = str(error).lower()
    markers = [
        "log in",
        "login",
        "sign in",
        "cookies",
        "authentication",
        "private",
        "fresh cookies",
    ]
    return any(marker in message for marker in markers)


def _detect_site_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = str(parsed.hostname or "").rstrip(".").lower()
    except Exception:
        return None
    def matches(suffix: str) -> bool:
        return host == suffix or host.endswith(f".{suffix}")

    if any(matches(token) for token in ("douyin.com", "iesdouyin.com", "amemv.com", "snssdk.com")):
        return "douyin"
    if matches("tiktok.com"):
        return "tiktok"
    if matches("youtube.com") or matches("youtu.be"):
        return "youtube"
    if matches("kuaishou.com") or matches("gifshow.com") or matches("kwai.com"):
        return "kuaishou"
    if matches("facebook.com") or matches("fb.watch"):
        return "facebook"
    site_suffixes = {
        "instagram": ("instagram.com",),
        "twitter": ("x.com", "twitter.com"),
        "bilibili": ("bilibili.com", "b23.tv"),
        "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
        "weibo": ("weibo.com", "weibo.cn"),
        "vimeo": ("vimeo.com", "vimeo.app.link"),
        "reddit": ("reddit.com", "redd.it"),
        "twitch": ("twitch.tv",),
        "dailymotion": ("dailymotion.com", "dai.ly"),
        "pinterest": ("pinterest.com", "pin.it"),
        "linkedin": ("linkedin.com",),
        "nicovideo": ("nicovideo.jp", "nico.ms"),
        "youku": ("youku.com",),
        "iqiyi": ("iqiyi.com", "iq.com"),
    }
    for site, suffixes in site_suffixes.items():
        if any(matches(suffix) for suffix in suffixes):
            return site
    return None


def _site_headers(site: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_BROWSER_USER_AGENT,
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
    elif site == "kuaishou":
        headers["Referer"] = "https://www.kuaishou.com/"
        headers["Origin"] = "https://www.kuaishou.com"
    elif site == "facebook":
        headers["Referer"] = "https://www.facebook.com/"
        headers["Origin"] = "https://www.facebook.com"
    return headers


def _probe_downloadable(share_url: str, cookiefile_path: str | None = None, site: str | None = None) -> Tuple[dict, str]:
    safe_share_url = resolve_safe_share_url(share_url)
    site = site or _detect_site_from_url(safe_share_url)
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
            info = ydl.extract_info(safe_share_url, download=False)
    except Exception as exc:  # noqa: BLE001
        if _is_authentication_required(exc):
            raise DownloadRequiresAuthError(str(exc)) from exc
        raise

    entry = _pick_entry(info or {})
    validate_extracted_media_urls(entry)
    expected_size = int(entry.get("filesize") or entry.get("filesize_approx") or 0)
    if expected_size > int(settings.OPENAI_WHISPER_MAX_REMOTE_DOWNLOAD_BYTES):
        raise DownloadTooLargeError("远程视频超过允许大小。")
    entry["_gmv_safe_share_url"] = safe_share_url
    download_url = entry.get("url")
    if not download_url:
        for fmt in reversed(entry.get("formats") or []):
            if fmt.get("url"):
                download_url = fmt.get("url")
                break
    if not download_url:
        raise RuntimeError("分享链接无法生成下载地址，请更换链接。")

    ext = entry.get("ext") or "mp4"
    return entry, ext


def _download_shared_video(workspace_id: int, job_id: str, share_url: str, video_path: Path | None, *, cookiefile_path: str | None) -> Tuple[Path, str, str | None]:
    site = _detect_site_from_url(share_url)
    entry, ext = _probe_downloadable(share_url, cookiefile_path, site=site)
    directory = storage.job_dir(workspace_id, job_id)
    filename = _safe_display_filename(entry.get("title") or entry.get("id") or "shared-video", ext)

    target_path = video_path or directory / f"input.{ext}"
    if target_path.suffix:
        target_path = target_path.with_suffix(f".{ext}")
    else:
        target_path = target_path.with_name(target_path.name + f".{ext}")

    content_type, _ = mimetypes.guess_type(f"{filename}.{ext}")

    remaining = storage.workspace_remaining_bytes(
        workspace_id,
        int(settings.OPENAI_WHISPER_WORKSPACE_STORAGE_QUOTA_BYTES),
    )
    byte_limit = min(int(settings.OPENAI_WHISPER_MAX_REMOTE_DOWNLOAD_BYTES), remaining)
    if byte_limit <= 0:
        raise DownloadTooLargeError("工作区文件空间已用完。")

    def enforce_size(status: dict) -> None:
        downloaded = int(status.get("downloaded_bytes") or 0)
        total = int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
        if max(downloaded, total) > byte_limit:
            raise DownloadTooLargeError("远程视频超过允许大小或工作区剩余空间。")

    options = {
        "outtmpl": str(target_path),
        "quiet": True,
        "noplaylist": True,
        "merge_output_format": ext,
        "nopart": False,
        "max_filesize": byte_limit,
        "progress_hooks": [enforce_size],
        "http_headers": _site_headers(site),
    }
    if cookiefile_path:
        options["cookiefile"] = cookiefile_path

    try:
        with YoutubeDL(options) as ydl:
            download_entry = dict(entry)
            download_entry.pop("_gmv_safe_share_url", None)
            ydl.process_ie_result(download_entry, download=True)
    except Exception as exc:  # noqa: BLE001
        target_path.unlink(missing_ok=True)
        target_path.with_name(target_path.name + ".part").unlink(missing_ok=True)
        if isinstance(exc, DownloadTooLargeError):
            raise
        if _is_authentication_required(exc):
            raise DownloadRequiresAuthError(str(exc)) from exc
        raise

    if not target_path.exists():
        raise RuntimeError("视频下载失败，请稍后重试或更换链接。")

    if target_path.stat().st_size > byte_limit:
        target_path.unlink(missing_ok=True)
        raise DownloadTooLargeError("远程视频超过允许大小或工作区剩余空间。")

    return target_path, filename, content_type


def _safe_display_filename(title: object, ext: object) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9]+", "", str(ext or "mp4").lstrip("."))[:12] or "mp4"
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]+", " ", str(title or "shared-video"))
    base = re.sub(r"\s+", " ", base).strip(" .") or "shared-video"
    ending = f".{suffix}"
    if base.lower().endswith(ending.lower()):
        base = base[: -len(ending)].rstrip(" .") or "shared-video"
    max_base = 240 - len(ending)
    if len(base) > max_base:
        base = base[: max_base - 1].rstrip(" .") + "…"
    return f"{base}{ending}"


def _format_timestamp_ms(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02},{millis:03}" if hours == -1 else f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _segments_to_srt(segments: Iterable[dict]) -> str:
    lines: List[str] = []
    for idx, seg in enumerate(segments or [], start=1):
        start = _format_timestamp_ms(float(seg.get("start", 0.0)))
        end = _format_timestamp_ms(float(seg.get("end", 0.0)))
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _mark_download_status(db: SessionLocal, *, workspace_id: int, job_id: str, status: str, message: str | None = None, download_url: str | None = None, video_path: Path | None = None, filename: str | None = None, size: int | None = None, content_type: str | None = None) -> dict:
    if filename:
        filename = _safe_display_filename(filename, Path(filename).suffix or "mp4")

    def _apply(meta: dict) -> dict:
        meta["download_status"] = status
        meta["download_error"] = message
        if video_path:
            meta["video_path"] = str(video_path)
        if filename:
            meta["filename"] = filename
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
        repository.update_download_status(db, workspace_id=workspace_id, job_id=job_id, status=status, message=message, download_url=download_url)
        if status == "success":
            repository.update_downloaded_file(db, workspace_id=workspace_id, job_id=job_id, filename=metadata.get("filename"), file_size=size, content_type=content_type, video_path=str(video_path) if video_path else None, download_url=download_url)
        db.flush()
    except Exception:
        db.rollback()
        logger.exception(
            "failed to persist Whisper download status",
            extra={"workspace_id": workspace_id, "job_id": job_id, "status": status},
        )
        raise
    return metadata


def _cookie_expiry(item: dict) -> int:
    expires_raw = item.get("expires") or item.get("expirationDate") or item.get("expiry")
    try:
        expires = int(float(expires_raw)) if expires_raw is not None else 0
    except Exception:  # noqa: BLE001
        expires = 0
    # Browser exporters often mark important Douyin cookies as session cookies.
    # Writing them as epoch 0 can make cookie loaders treat them as expired, so
    # use a local future timestamp. This does not extend server-side validity;
    # it only prevents the Netscape file reader from dropping the row.
    if expires <= 0:
        expires = int(time.time()) + SESSION_COOKIE_FALLBACK_TTL_SECONDS
    return expires


def _write_temp_cookiefile(cookies_json: str, site: str, job_id: str) -> str | None:
    try:
        cookies = json.loads(cookies_json)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse cookies JSON for %s: %s", site, exc)
        return None
    if not cookies:
        logger.warning("No cookies found for site", extra={"site": site, "job_id": job_id})
        return None

    tmp_dir = Path(tempfile.gettempdir())
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cookie_path = tmp_dir / f"yt_dlp_cookies_{site}_{job_id}.txt"

    lines = ["# Netscape HTTP Cookie File"]
    written_names: list[str] = []
    skipped = 0
    for item in cookies:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or item.get("host") or "").strip()
        if not domain:
            skipped += 1
            continue
        path_value = str(item.get("path") or "/")
        host_only = bool(item.get("hostOnly"))
        include_subdomains = "FALSE" if host_only else ("TRUE" if domain.startswith(".") else "FALSE")
        secure = "TRUE" if item.get("secure") else "FALSE"
        expires = _cookie_expiry(item)
        lines.append("\t".join([domain, include_subdomains, path_value, secure, str(expires), name, value]))
        written_names.append(name)

    if not written_names:
        logger.warning("No valid cookies were written", extra={"site": site, "job_id": job_id, "skipped": skipped})
        return None

    cookie_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    key_names = [name for name in written_names if name.lower() in {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "ttwid", "msToken".lower(), "odin_tt", "passport_csrf_token"}]
    logger.info(
        "yt-dlp cookiefile prepared",
        extra={
            "site": site,
            "job_id": job_id,
            "path": str(cookie_path),
            "written_count": len(written_names),
            "skipped_count": skipped,
            "key_cookie_names": sorted(set(key_names)),
        },
    )
    return str(cookie_path)


def _load_cookiefile_for_share(share_url: str, job_id: str) -> str | None:
    site = _detect_site_from_url(share_url)
    if not site:
        logger.info("No matching cookie site for share URL", extra={"job_id": job_id, "share_url_host": urlparse(share_url).netloc if share_url else None})
        return None
    db = SessionLocal()
    try:
        record = video_site_cookies.get_active_site_cookies(db, site)
    finally:
        db.close()
    if not record or not record.cookies_json:
        logger.warning("No active cookies configured for share URL", extra={"site": site, "job_id": job_id})
        return None
    logger.info("active site cookies loaded", extra={"site": site, "job_id": job_id, "cookie_id": record.id, "label": record.label})
    return _write_temp_cookiefile(record.cookies_json, site, job_id)


def _ensure_local_video(db, workspace_id: int, job_id: str, metadata: dict) -> tuple[dict | None, Path | None, str | None]:
    video_path = storage.resolve_job_artifact_path(
        workspace_id,
        job_id,
        metadata.get("video_path"),
    )
    share_url = (metadata.get("share_url") or "").strip()
    download_url = f"/api/v1/tenants/{workspace_id}/openai-whisper/jobs/{job_id}/video"

    if share_url:
        if video_path and video_path.exists():
            if (metadata.get("download_status") or "") != "success":
                metadata = _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="success", download_url=download_url, video_path=video_path, filename=metadata.get("filename"), size=metadata.get("size"), content_type=metadata.get("content_type"))
            return metadata, video_path, None

        try:
            _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="processing")
            db.commit()
            cookiefile_path = _load_cookiefile_for_share(share_url, job_id)
            video_path, filename, content_type = _download_shared_video(workspace_id, job_id, share_url, video_path, cookiefile_path=cookiefile_path)
            size = video_path.stat().st_size
            metadata = _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="success", download_url=download_url, video_path=video_path, filename=filename or video_path.name, size=size, content_type=content_type)
            db.commit()
            return metadata, video_path, None
        except DownloadRequiresAuthError as exc:
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
        except (UnsafeShareURLError, DownloadTooLargeError) as exc:
            db.rollback()
            message = str(exc)
            _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
            db.commit()
            logger.warning(
                "whisper download blocked by security boundary",
                extra={"workspace_id": workspace_id, "job_id": job_id, "error": message},
            )
            return None, None, message
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            message = "视频下载失败，请稍后重试或更换链接。"
            try:
                _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "failed to persist failed Whisper download status",
                    extra={"workspace_id": workspace_id, "job_id": job_id},
                )
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


def _best_grid(n: int) -> tuple[int, int]:
    best_rows, best_cols = 1, n
    best_diff = abs(best_rows - best_cols)
    best_area = best_rows * best_cols
    for rows in range(1, n + 1):
        cols = math.ceil(n / rows)
        area = rows * cols
        diff = abs(rows - cols)
        if diff < best_diff or (diff == best_diff and area < best_area):
            best_rows, best_cols = rows, cols
            best_diff = diff
            best_area = area
    return best_rows, best_cols


def _extract_frames(video_path: Path, frames_dir: Path, interval: float) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = frames_dir / "frame_%03d.png"
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vf", f"fps=1/{interval},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920", str(pattern)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frames = sorted(frames_dir.glob("frame_*.png"))
    if frames:
        return frames
    fallback = frames_dir / "frame_001.png"
    fallback_cmd = ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920", str(fallback)]
    subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [fallback]


def _render_contact_sheet(video_path: Path, workspace_id: int, job_id: str, interval: float) -> Path:
    directory = storage.job_dir(workspace_id, job_id)
    frames_dir = directory / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = _extract_frames(video_path, frames_dir, interval)
    frame_count = max(1, len(frames))
    try:
        probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
        duration_raw = subprocess.check_output(probe_cmd, stderr=subprocess.STDOUT).decode().strip()
        duration_seconds = max(0.0, float(duration_raw)) if duration_raw else 0.0
    except Exception:
        duration_seconds = 0.0
    expected_frames = frame_count
    if duration_seconds > 0 and interval > 0:
        expected_frames = max(expected_frames, math.ceil(duration_seconds / interval))
    rows, cols = _best_grid(expected_frames)
    output_path = storage.contact_sheet_path(directory)
    tile_cmd = ["ffmpeg", "-y", "-i", str(frames_dir / "frame_%03d.png"), "-frames:v", "1", "-vf", f"tile={cols}x{rows}:padding=4:margin=10", str(output_path)]
    subprocess.run(tile_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(frames_dir, ignore_errors=True)
    return output_path


@celery_app.task(name="openai_whisper.transcribe_video", bind=True, queue=WHISPER_TASK_QUEUE)
def transcribe_video(self, *, workspace_id: int, job_id: str) -> str:
    with SessionLocal() as db:
        try:
            metadata = storage.load_metadata(workspace_id, job_id)
        except FileNotFoundError:
            logger.error("whisper job metadata missing", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.mark_failed(db, workspace_id, job_id, "任务元数据缺失，无法继续。")
            db.commit()
            return job_id
        except storage.MetadataCorruptedError:
            logger.error("whisper job metadata corrupted", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.mark_failed(db, workspace_id, job_id, "任务元数据损坏，无法继续。")
            db.commit()
            return job_id
        metadata, video_path, video_error = _ensure_local_video(db, workspace_id, job_id, metadata)
        if video_error:
            storage.update_component_status(workspace_id, job_id, "subtitle", status="failed", error=video_error)
            repository.mark_failed(db, workspace_id, job_id, video_error)
            db.commit()
            return job_id
        if not video_path or not video_path.exists():
            message = "视频源文件已丢失，无法继续。"
            storage.update_component_status(workspace_id, job_id, "subtitle", status="failed", error=message)
            repository.mark_failed(db, workspace_id, job_id, message)
            db.commit()
            logger.error("whisper video missing", extra={"workspace_id": workspace_id, "job_id": job_id, "video": str(video_path) if video_path else None})
            return job_id
        storage.update_component_status(workspace_id, job_id, "subtitle", status="processing")
        repository.mark_processing(db, workspace_id, job_id)
        db.commit()
        try:
            result = transcriber.transcribe(video_path, source_language=metadata.get("source_language"), translate=bool(metadata.get("translate")), target_language=metadata.get("target_language"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("whisper transcription failed", extra={"workspace_id": workspace_id, "job_id": job_id})
            storage.mark_failed(workspace_id, job_id, str(exc))
            repository.mark_failed(db, workspace_id, job_id, str(exc))
            db.commit()
            raise
        source_srt = _segments_to_srt(result.get("segments") or [])
        storage.write_subtitles_file(workspace_id, job_id, "source", source_srt)
        translation_segments = result.get("translation_segments")
        if translation_segments:
            storage.write_subtitles_file(workspace_id, job_id, "translation", _segments_to_srt(translation_segments))
        storage.save_results(workspace_id, job_id, result)
        repository.mark_completed(db, workspace_id, job_id, detected_language=result.get("detected_language") or result.get("source_language"), translation_language=result.get("translation_language"), segments_count=len(result.get("segments") or []), translation_segments_count=len(result.get("translation_segments") or []))
        db.commit()
        logger.info("whisper transcription completed", extra={"workspace_id": workspace_id, "job_id": job_id})
    return job_id


@celery_app.task(
    name="openai_whisper.ingest_content_producer_reference_url",
    bind=True,
    queue=WHISPER_TASK_QUEUE,
    max_retries=3,
)
def ingest_content_producer_reference_url(self, *, attachment_id: int) -> dict:
    """Safely download a producer benchmark URL before multimodal analysis."""

    from app.data.models.hermes_agent import HermesContentProducerAttachment
    from app.services.hermes_agent.content_producer import (
        _probe_reference_video,
        _render_video_preview,
    )

    with SessionLocal() as db:
        row = db.get(HermesContentProducerAttachment, int(attachment_id))
        if row is None:
            return {"status": "missing", "attachment_id": int(attachment_id)}
        if row.kind != "reference_video":
            return {"status": "ignored", "attachment_id": int(attachment_id)}
        analysis = dict(row.analysis_json or {})
        if (
            int(row.size_bytes or 0) > 0
            and analysis.get("download_status") == "success"
        ):
            analyze_content_producer_reference.apply_async(
                kwargs={"attachment_id": int(row.id)},
                queue=WHISPER_TASK_QUEUE,
            )
            return {"status": "downloaded", "attachment_id": int(row.id)}
        analysis.update({
            "download_status": "processing",
            "download_started_at": _utcnow().isoformat(),
        })
        row.analysis_status = "processing"
        row.analysis_json = analysis
        flag_modified(row, "analysis_json")
        db.commit()
        try:
            source = _download_producer_reference_url(row)
            preview = _producer_attachment_target(row.preview_path)
            probe = _probe_reference_video(source)
            _render_video_preview(
                source,
                preview,
                duration_seconds=float(probe["duration_seconds"]),
            )
            analysis = dict(row.analysis_json or {})
            analysis.update(probe)
            analysis.update({
                "download_status": "success",
                "download_completed_at": _utcnow().isoformat(),
                "preview_available": True,
                "transcript_status": "queued",
                "multimodal_status": "queued",
            })
            row.file_path = str(source)
            row.preview_path = str(preview)
            row.mime_type = mimetypes.guess_type(source.name)[0] or "video/mp4"
            row.size_bytes = int(source.stat().st_size)
            row.sha256 = _sha256_file(source)
            row.original_name = _safe_display_filename(
                entry_title := dict(row.meta_json or {}).get("source_title")
                or Path(row.original_name).stem,
                source.suffix.lstrip("."),
            )
            row.analysis_status = "processing"
            row.analysis_json = analysis
            flag_modified(row, "analysis_json")
            db.commit()
            analyze_content_producer_reference.apply_async(
                kwargs={"attachment_id": int(row.id)},
                queue=WHISPER_TASK_QUEUE,
            )
            return {"status": "downloaded", "attachment_id": int(row.id)}
        except (UnsafeShareURLError, DownloadTooLargeError, DownloadRequiresAuthError) as exc:
            db.rollback()
            row = db.get(HermesContentProducerAttachment, int(attachment_id))
            if row is None:
                return {"status": "missing", "attachment_id": int(attachment_id)}
            analysis = dict(row.analysis_json or {})
            analysis.update({
                "download_status": "failed",
                "download_error": type(exc).__name__[:120],
                "download_completed_at": _utcnow().isoformat(),
                "multimodal_status": "failed",
            })
            row.analysis_status = "failed"
            row.analysis_json = analysis
            flag_modified(row, "analysis_json")
            db.commit()
            return {"status": "failed", "attachment_id": int(attachment_id)}
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            if int(self.request.retries or 0) < int(self.max_retries or 3):
                raise self.retry(exc=exc, countdown=15 * (2 ** int(self.request.retries or 0)))
            row = db.get(HermesContentProducerAttachment, int(attachment_id))
            if row is not None:
                analysis = dict(row.analysis_json or {})
                analysis.update({
                    "download_status": "failed",
                    "download_error": type(exc).__name__[:120],
                    "download_completed_at": _utcnow().isoformat(),
                    "multimodal_status": "failed",
                })
                row.analysis_status = "failed"
                row.analysis_json = analysis
                flag_modified(row, "analysis_json")
                db.commit()
            logger.exception(
                "producer benchmark URL download failed attachment_id=%s",
                attachment_id,
            )
            return {"status": "failed", "attachment_id": int(attachment_id)}


@celery_app.task(
    name="openai_whisper.analyze_content_producer_reference",
    bind=True,
    queue=WHISPER_TASK_QUEUE,
    max_retries=3,
)
def analyze_content_producer_reference(self, *, attachment_id: int) -> dict:
    """Add speech evidence to a staged Content Factory benchmark video.

    The API process prepares a small visual contact sheet.  The dedicated
    Whisper worker owns the expensive audio pass so uploads never load the
    speech model into Gunicorn or a Hermes worker.
    """

    from app.data.models.hermes_agent import HermesContentProducerAttachment

    with SessionLocal() as db:
        row = db.scalar(
            select(HermesContentProducerAttachment)
            .where(HermesContentProducerAttachment.id == int(attachment_id))
            .with_for_update()
        )
        if row is None:
            return {"status": "missing", "attachment_id": int(attachment_id)}
        if row.kind != "reference_video":
            return {"status": "ignored", "attachment_id": int(attachment_id)}
        if (
            row.analysis_status == "ready"
            and dict(row.analysis_json or {}).get("transcript_status")
            in {"success", "no_speech"}
            and dict(row.analysis_json or {}).get("multimodal_status")
            == "success"
        ):
            return {"status": "ready", "attachment_id": int(attachment_id)}

        analysis = dict(row.analysis_json or {})
        multimodal_started_at = str(analysis.get("multimodal_started_at") or "")
        try:
            multimodal_started = datetime.fromisoformat(multimodal_started_at).replace(
                tzinfo=None
            )
        except (TypeError, ValueError):
            multimodal_started = None
        if (
            str(analysis.get("multimodal_status") or "") == "processing"
            and multimodal_started is not None
            and multimodal_started > _utcnow() - timedelta(minutes=45)
        ):
            return {"status": "processing", "attachment_id": int(attachment_id)}

        # Transcription and visual analysis are independent durable phases.
        # If the latter fails, a Celery retry must reuse the transcript that
        # was committed immediately above rather than spending another full
        # Whisper pass on the same immutable source video.
        reuse_transcript = str(
            analysis.get("transcript_status") or ""
        ) in {"ready", "success", "no_speech"}
        if not reuse_transcript:
            analysis["transcript_status"] = "processing"
            analysis["transcript_started_at"] = _utcnow().isoformat()
        analysis["multimodal_status"] = "processing"
        analysis["multimodal_started_at"] = _utcnow().isoformat()
        analysis["multimodal_task_id"] = str(self.request.id or "")[:80]
        analysis.pop("multimodal_error", None)
        analysis.pop("multimodal_completed_at", None)
        analysis.pop("multimodal_retry_scheduled_at", None)
        row.analysis_status = "processing"
        row.analysis_json = analysis
        flag_modified(row, "analysis_json")
        db.commit()

        try:
            source = _producer_attachment_source(row.file_path)
            if _sha256_file(source) != str(row.sha256 or ""):
                raise RuntimeError("producer attachment checksum changed")
            if reuse_transcript:
                reused_segments = [
                    dict(item)
                    for item in list(
                        analysis.get("segments")
                        or analysis.get("transcript_segments")
                        or []
                    )[:160]
                    if isinstance(item, dict)
                ]
                reused_text = str(
                    analysis.get("transcript")
                    or analysis.get("transcript_text")
                    or ""
                ).strip()
                analysis.update(
                    {
                        "transcript_status": (
                            "success" if reused_text or reused_segments else "no_speech"
                        ),
                        "detected_language": analysis.get("detected_language")
                        or analysis.get("transcript_language"),
                        "transcript": reused_text[:24000],
                        "segments": reused_segments,
                    }
                )
            elif not _has_audio_stream(source):
                analysis.update(
                    {
                        "transcript_status": "no_speech",
                        "detected_language": None,
                        "transcript": "",
                        "segments": [],
                    }
                )
            else:
                result = transcriber.transcribe(
                    source,
                    source_language=None,
                    translate=False,
                )
                segments = [
                    {
                        "index": int(segment.get("index") or index),
                        "start": round(float(segment.get("start") or 0), 2),
                        "end": round(float(segment.get("end") or 0), 2),
                        "text": str(segment.get("text") or "").strip()[:600],
                    }
                    for index, segment in enumerate(
                        list(result.get("segments") or [])[:160],
                        1,
                    )
                    if str(segment.get("text") or "").strip()
                ]
                transcript = "\n".join(
                    f"{item['start']:.1f}-{item['end']:.1f}s {item['text']}"
                    for item in segments
                )
                analysis.update(
                    {
                        "transcript_status": "success" if segments else "no_speech",
                        "detected_language": result.get("detected_language")
                        or result.get("source_language"),
                        "transcript": transcript[:24000],
                        "segments": segments,
                    }
                )
            analysis["transcript_completed_at"] = _utcnow().isoformat()
            row.analysis_json = analysis
            flag_modified(row, "analysis_json")
            db.commit()

            from app.services.hermes_agent.content_factory_api import (
                analyze_benchmark_storyboard_api,
            )
            from app.tasks.hermes_agent.content_factory_tasks import (
                _render_benchmark_contact_sheets,
            )

            output_dir = source.parent / f"{row.attachment_key}_benchmark_analysis"
            duration = float(analysis.get("duration_seconds") or 0)
            sheet_rows = _render_benchmark_contact_sheets(
                source,
                output_dir,
                source_asset_id=int(row.id),
                duration_seconds=duration,
            )
            request_context = str(
                dict(row.meta_json or {}).get("analysis_request_context") or ""
            ).strip()
            visual_semantic_analysis = analyze_benchmark_storyboard_api(
                db,
                contact_sheet_paths=[str(item["path"]) for item in sheet_rows],
                transcript=str(analysis.get("transcript") or ""),
                project_requirement=(
                    request_context[:4000]
                    or "Analyze this benchmark before discussing an original short-video adaptation with the user."
                ),
                transformation_contract={
                    "fidelity": "adaptive",
                    "transfer_mode": "semantic_structure",
                    "source_media_reuse": "forbidden",
                    "analysis_phase": "producer_discussion",
                },
                execution_id=f"producer:{int(row.conversation_id)}:{int(row.id)}",
            )
            shutil.rmtree(output_dir, ignore_errors=True)
            analysis = dict(row.analysis_json or {})
            analysis.update({
                "visual_semantic_analysis": visual_semantic_analysis,
                "multimodal_status": "success",
                "multimodal_completed_at": _utcnow().isoformat(),
                "frame_count": sum(
                    int(item.get("frame_count") or 0) for item in sheet_rows
                ),
                "keyframe_sheets": [
                    {
                        key: item[key]
                        for key in (
                            "board_index", "frame_start", "frame_end",
                            "start_second", "end_second", "frame_count",
                            "interval_seconds",
                        )
                    }
                    for item in sheet_rows
                ],
            })
            analysis.pop("multimodal_error", None)
            analysis.pop("multimodal_retry_scheduled_at", None)
            row.analysis_status = "ready"
            row.analysis_json = analysis
            meta = dict(row.meta_json or {})
            has_pending_producer_turn = bool(
                str(meta.get("analysis_request_context") or "").strip()
            )
            if has_pending_producer_turn:
                # Publish the durable handoff state in the same transaction as
                # analysis completion.  The browser must never observe
                # ``ready`` without knowing that the Producer reply is still
                # in flight and stop polling in that narrow commit gap.
                analysis["producer_turn_status"] = "queued"
                row.analysis_json = analysis
            else:
                _ensure_producer_analysis_ready_notification(db, row, analysis)
            # ``analysis`` is the same dict that was assigned before the
            # intermediate commit.  SQLAlchemy's committed JSON snapshot can
            # therefore share that object, and the in-place ``update`` above
            # is otherwise invisible to dirty tracking.  Explicitly mark the
            # JSON column so a successful transcription cannot remain stuck
            # at ``transcript_status=processing`` in durable state.
            flag_modified(row, "analysis_json")
            db.commit()
            if has_pending_producer_turn:
                try:
                    celery_app.send_task(
                        "hermes_content_factory.continue_producer_benchmark_turn",
                        kwargs={"attachment_id": int(row.id)},
                        queue=str(settings.HERMES_AGENT_TASK_QUEUE),
                    )
                except Exception as exc:  # noqa: BLE001
                    analysis = dict(row.analysis_json or {})
                    analysis.update({
                        "producer_turn_status": "failed",
                        "producer_turn_error": type(exc).__name__[:120],
                        "producer_turn_completed_at": _utcnow().isoformat(),
                    })
                    row.analysis_json = analysis
                    flag_modified(row, "analysis_json")
                    db.commit()
            return {"status": "ready", "attachment_id": int(attachment_id)}
        except Exception as exc:  # noqa: BLE001
            if "output_dir" in locals():
                shutil.rmtree(output_dir, ignore_errors=True)
            db.rollback()
            row = db.get(HermesContentProducerAttachment, int(attachment_id))
            if row is None:
                return {"status": "missing", "attachment_id": int(attachment_id)}
            analysis = dict(row.analysis_json or {})
            retries = int(self.request.retries or 0)
            if retries < int(self.max_retries or 3):
                if analysis.get("transcript_status") == "processing":
                    analysis["transcript_status"] = "queued"
                analysis.update({
                    "multimodal_status": "queued",
                    "multimodal_error": type(exc).__name__[:120],
                    "multimodal_retry_scheduled_at": _utcnow().isoformat(),
                })
                row.analysis_status = "processing"
                row.analysis_json = analysis
                flag_modified(row, "analysis_json")
                db.commit()
                raise self.retry(
                    exc=exc,
                    countdown=min(180, 15 * (2 ** retries)),
                )
            if analysis.get("transcript_status") == "processing":
                analysis.update(
                    {
                        "transcript_status": "failed",
                        "transcript_error": type(exc).__name__[:120],
                        "transcript_completed_at": _utcnow().isoformat(),
                    }
                )
            analysis.update({
                "multimodal_status": "failed",
                "multimodal_error": type(exc).__name__[:120],
                "multimodal_completed_at": _utcnow().isoformat(),
            })
            row.analysis_status = "failed"
            row.analysis_json = analysis
            flag_modified(row, "analysis_json")
            db.commit()
            logger.exception(
                "producer reference analysis failed attachment_id=%s",
                attachment_id,
            )
            return {"status": "failed", "attachment_id": int(attachment_id)}


@celery_app.task(
    name="openai_whisper.recover_content_producer_reference_analyses",
    queue=WHISPER_TASK_QUEUE,
)
def recover_content_producer_reference_analyses(
    *,
    stale_seconds: int = 120,
    processing_stale_minutes: int = 45,
    limit: int = 100,
) -> dict:
    """Re-dispatch orphaned Producer benchmark analyses.

    The database row is the durable source of truth.  This repairs the narrow
    commit-to-broker gap without polling browsers or creating duplicate model
    work; the analysis task itself owns the processing lease.
    """

    from app.data.models.hermes_agent import HermesContentProducerAttachment

    utc_now = _utcnow()
    processing_cutoff = utc_now - timedelta(
        minutes=max(1, int(processing_stale_minutes))
    )
    with SessionLocal() as db:
        # ``created_at`` is populated by the database.  Production MySQL uses
        # the server session's local wall clock while task JSON timestamps are
        # UTC-naive.  Compare database-owned timestamps with database time so
        # an orphan cannot be mistaken for a row created hours in the future.
        database_now = db.scalar(select(func.now())) or datetime.now()
        queued_cutoff = database_now.replace(tzinfo=None) - timedelta(
            seconds=max(0, int(stale_seconds))
        )
        candidates = list(
            db.scalars(
                select(HermesContentProducerAttachment)
                .where(HermesContentProducerAttachment.kind == "reference_video")
                .order_by(HermesContentProducerAttachment.id.desc())
                .limit(max(1, min(int(limit), 500)))
            ).all()
        )
        attachment_ids: list[int] = []
        normalized_success = 0
        notified_ready = 0
        for row in candidates:
            meta = dict(row.meta_json or {})
            if meta.get("active_for_current_requirement", True) is False:
                continue
            analysis = dict(row.analysis_json or {})
            status = str(analysis.get("multimodal_status") or "")
            if status == "success":
                if _ensure_producer_analysis_ready_notification(db, row, analysis):
                    notified_ready += 1
                if any(
                    key in analysis
                    for key in ("multimodal_error", "multimodal_retry_scheduled_at")
                ):
                    analysis.pop("multimodal_error", None)
                    analysis.pop("multimodal_retry_scheduled_at", None)
                    row.analysis_json = analysis
                    flag_modified(row, "analysis_json")
                    normalized_success += 1
                continue
            if row.analysis_status == "failed":
                continue
            try:
                started_at = datetime.fromisoformat(
                    str(analysis.get("multimodal_started_at") or "")
                ).replace(tzinfo=None)
            except (TypeError, ValueError):
                started_at = None
            try:
                retry_at = datetime.fromisoformat(
                    str(analysis.get("multimodal_retry_scheduled_at") or "")
                ).replace(tzinfo=None)
            except (TypeError, ValueError):
                retry_at = None
            if retry_at is not None and retry_at > utc_now - timedelta(minutes=10):
                continue
            orphaned = (
                status == "queued"
                and (row.created_at or database_now).replace(tzinfo=None)
                <= queued_cutoff
            ) or (
                status == "processing"
                and (started_at is None or started_at <= processing_cutoff)
            )
            if not orphaned:
                continue
            analysis.update(
                {
                    "multimodal_status": "queued",
                    "multimodal_recovery_dispatched_at": utc_now.isoformat(),
                    "multimodal_recovery_count": int(
                        analysis.get("multimodal_recovery_count") or 0
                    )
                    + 1,
                }
            )
            row.analysis_status = "processing"
            row.analysis_json = analysis
            flag_modified(row, "analysis_json")
            attachment_ids.append(int(row.id))
        db.commit()

    dispatched: list[int] = []
    for attachment_id in attachment_ids:
        try:
            celery_app.send_task(
                "openai_whisper.analyze_content_producer_reference",
                kwargs={"attachment_id": int(attachment_id)},
                queue=str(WHISPER_TASK_QUEUE),
            )
            dispatched.append(int(attachment_id))
        except Exception:  # noqa: BLE001
            logger.exception(
                "producer benchmark recovery dispatch failed attachment_id=%s",
                attachment_id,
            )
    return {
        "checked": len(candidates),
        "eligible": len(attachment_ids),
        "dispatched": len(dispatched),
        "normalized_success": normalized_success,
        "notified_ready": notified_ready,
        "attachment_ids": dispatched,
    }


@celery_app.task(name="openai_whisper.download_shared_video", bind=True, queue=WHISPER_TASK_QUEUE)
def download_shared_video(self, *, workspace_id: int, job_id: str) -> str:
    with SessionLocal() as db:
        try:
            metadata = storage.load_metadata(workspace_id, job_id)
        except FileNotFoundError:
            logger.error("download metadata missing", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message="任务元数据缺失，无法继续。")
            db.commit()
            return job_id
        except storage.MetadataCorruptedError:
            logger.error("download metadata corrupted", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message="任务元数据损坏，无法继续。")
            db.commit()
            return job_id
        metadata, video_path, video_error = _ensure_local_video(db, workspace_id, job_id, metadata)
        if video_error:
            storage.update_component_status(workspace_id, job_id, "download", status="failed", error=video_error)
            repository.update_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=video_error)
            db.commit()
            return job_id
        if video_path and video_path.exists():
            storage.update_component_status(workspace_id, job_id, "download", status="success", url=metadata.get("download_url"))
            repository.update_download_status(db, workspace_id=workspace_id, job_id=job_id, status="success", download_url=metadata.get("download_url"))
            db.commit()
        if metadata.get("share_url"):
            spawned_tasks: list[str] = []
            if metadata.get("do_subtitle") and metadata.get("subtitle_status") == "pending":
                spawned_tasks.append(transcribe_video.delay(workspace_id=workspace_id, job_id=job_id).id)
            if metadata.get("do_contact_sheet") and metadata.get("contact_sheet_status") == "pending":
                spawned_tasks.append(generate_contact_sheet.delay(workspace_id=workspace_id, job_id=job_id, contact_interval=metadata.get("contact_interval")).id)
            if spawned_tasks:
                def _apply(meta: dict) -> dict:
                    existing: list[str] = list(meta.get("celery_task_ids") or [])
                    meta["celery_task_ids"] = existing + spawned_tasks
                    if not meta.get("celery_task_id"):
                        meta["celery_task_id"] = meta["celery_task_ids"][0]
                    return meta
                storage.update_metadata(workspace_id, job_id, _apply)
                db.flush()
        return job_id


@celery_app.task(name="openai_whisper.generate_contact_sheet", bind=True, queue=WHISPER_TASK_QUEUE)
def generate_contact_sheet(self, *, workspace_id: int, job_id: str, contact_interval: float | None = None) -> str:
    with SessionLocal() as db:
        try:
            metadata = storage.load_metadata(workspace_id, job_id)
        except FileNotFoundError:
            logger.error("contact sheet metadata missing", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message="任务元数据缺失，无法继续。")
            db.commit()
            return job_id
        except storage.MetadataCorruptedError:
            logger.error("contact sheet metadata corrupted", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message="任务元数据损坏，无法继续。")
            db.commit()
            return job_id
        metadata, video_path, video_error = _ensure_local_video(db, workspace_id, job_id, metadata)
        if video_error:
            storage.update_component_status(workspace_id, job_id, "contact_sheet", status="failed", error=video_error)
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=video_error)
            db.commit()
            return job_id
        if not video_path or not video_path.exists():
            message = "视频源文件已丢失，无法生成拼图。"
            storage.update_component_status(workspace_id, job_id, "contact_sheet", status="failed", error=message)
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
            db.commit()
            return job_id
        interval_value = contact_interval or metadata.get("contact_interval")
        try:
            interval_value = float(interval_value)
        except (TypeError, ValueError):
            interval_value = None
        if interval_value not in ALLOWED_CONTACT_INTERVALS:
            message = "抽帧间隔不合法，无法生成拼图。"
            storage.update_component_status(workspace_id, job_id, "contact_sheet", status="failed", error=message)
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
            db.commit()
            return job_id
        storage.update_component_status(workspace_id, job_id, "contact_sheet", status="processing")
        repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="processing")
        db.commit()
        try:
            output_path = _render_contact_sheet(video_path, workspace_id, job_id, interval_value)
            download_url = f"/api/v1/tenants/{workspace_id}/openai-whisper/jobs/{job_id}/contact-sheet"
            storage.update_component_status(workspace_id, job_id, "contact_sheet", status="success", url=download_url)
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="success", contact_sheet_url=download_url)
            db.commit()
            logger.info("contact sheet generated", extra={"workspace_id": workspace_id, "job_id": job_id, "path": str(output_path)})
        except Exception as exc:  # noqa: BLE001
            message = "拆解图片失败，请稍后再试。"
            storage.update_component_status(workspace_id, job_id, "contact_sheet", status="failed", error=message)
            repository.update_contact_sheet_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
            db.commit()
            logger.exception("contact sheet generation failed", extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)})
        return job_id


@celery_app.task(name="openai_whisper.cleanup_jobs", bind=True, queue=WHISPER_TASK_QUEUE)
def cleanup_jobs(self) -> dict:
    started = time.monotonic()
    batch_size = max(1, int(getattr(settings, "OPENAI_WHISPER_CLEANUP_BATCH_SIZE", 500)))
    failed_cutoff = _cutoff_days(getattr(settings, "OPENAI_WHISPER_FAILED_RETENTION_DAYS", 7))
    success_cutoff = _cutoff_days(getattr(settings, "OPENAI_WHISPER_SUCCESS_RETENTION_DAYS", 90))
    large_cutoff = _cutoff_days(getattr(settings, "OPENAI_WHISPER_LARGE_ARTIFACT_RETENTION_DAYS", 30))
    stale_cutoff = _utcnow() - timedelta(hours=max(1, int(getattr(settings, "OPENAI_WHISPER_STALE_ACTIVE_HOURS", 24))))
    upload_cutoff_ts = time.time() - max(1, int(getattr(settings, "OPENAI_WHISPER_UPLOAD_RETENTION_HOURS", 24))) * 3600

    stats = {
        "stale_marked_failed": 0,
        "large_artifacts_purged": 0,
        "expired_jobs_deleted": 0,
        "uploads_deleted": 0,
        "elapsed_ms": 0,
    }
    with SessionLocal() as db:
        stale_rows = repository.list_stale_active_jobs(db, cutoff=stale_cutoff, limit=batch_size)
        for row in stale_rows:
            message = "任务超过处理时限，已由系统自动标记为失败。"
            try:
                storage.update_component_status(row.workspace_id, row.job_id, "subtitle", status="failed", error=message)
            except Exception:
                pass
            repository.mark_failed(db, row.workspace_id, row.job_id, message)
            stats["stale_marked_failed"] += 1
        db.commit()

        large_rows = repository.list_jobs_for_large_artifact_cleanup(db, cutoff=large_cutoff, limit=batch_size)
        for row in large_rows:
            removed = storage.purge_large_artifacts(row.workspace_id, row.job_id)
            if removed:
                repository.clear_large_artifact_refs(db, row.workspace_id, row.job_id)
                stats["large_artifacts_purged"] += 1
        db.commit()

        expired_rows = repository.list_expired_terminal_jobs(db, success_cutoff=success_cutoff, failed_cutoff=failed_cutoff, limit=batch_size)
        for row in expired_rows:
            storage.delete_job_files(row.workspace_id, row.job_id)
        expired_by_workspace: dict[int, list[str]] = {}
        for row in expired_rows:
            expired_by_workspace.setdefault(int(row.workspace_id), []).append(str(row.job_id))
        stats["expired_jobs_deleted"] = sum(
            repository.delete_jobs_by_ids(db, workspace_id, job_ids)
            for workspace_id, job_ids in expired_by_workspace.items()
        )
        db.commit()

    stats["uploads_deleted"] = storage.delete_uploads_older_than(None, upload_cutoff_ts, limit=batch_size)
    stats["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("openai whisper cleanup completed", extra=stats)
    return stats
