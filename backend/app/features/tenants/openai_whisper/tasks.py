"""Celery tasks for running Whisper transcriptions in the background."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import mimetypes
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

from yt_dlp import YoutubeDL

from . import repository, storage, transcriber

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        host = (parsed.netloc or parsed.path).lower()
    except Exception:
        return None
    if any(token in host for token in ("douyin.com", "iesdouyin.com", "amemv.com", "snssdk.com")):
        return "douyin"
    if "tiktok.com" in host:
        return "tiktok"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
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
        if _is_authentication_required(exc):
            raise DownloadRequiresAuthError(str(exc)) from exc
        raise

    entry = _pick_entry(info or {})
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
    filename = entry.get("title") or entry.get("id") or "shared-video"

    target_path = video_path or directory / f"input.{ext}"
    if target_path.suffix:
        target_path = target_path.with_suffix(f".{ext}")
    else:
        target_path = target_path.with_name(target_path.name + f".{ext}")

    content_type, _ = mimetypes.guess_type(f"{filename}.{ext}")

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
        if _is_authentication_required(exc):
            raise DownloadRequiresAuthError(str(exc)) from exc
        raise

    if not target_path.exists():
        raise RuntimeError("视频下载失败，请稍后重试或更换链接。")

    final_name = f"{filename}.{ext}" if not filename.endswith(ext) else filename
    return target_path, final_name, content_type


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
    repository.update_download_status(db, workspace_id=workspace_id, job_id=job_id, status=status, message=message, download_url=download_url)
    if status == "success":
        repository.update_downloaded_file(db, workspace_id=workspace_id, job_id=job_id, filename=metadata.get("filename"), file_size=size, content_type=content_type, video_path=str(video_path) if video_path else None, download_url=download_url)
    db.flush()
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
    raw_video_path = metadata.get("video_path")
    video_path = Path(raw_video_path) if raw_video_path else None
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
            message = "视频下载失败，请稍后重试或更换链接。"
            _mark_download_status(db, workspace_id=workspace_id, job_id=job_id, status="failed", message=message)
            db.commit()
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
    name="openai_whisper.analyze_content_producer_reference",
    bind=True,
    queue=WHISPER_TASK_QUEUE,
)
def analyze_content_producer_reference(self, *, attachment_id: int) -> dict:
    """Add speech evidence to a staged Content Factory benchmark video.

    The API process prepares a small visual contact sheet.  The dedicated
    Whisper worker owns the expensive audio pass so uploads never load the
    speech model into Gunicorn or a Hermes worker.
    """

    from app.data.models.hermes_agent import HermesContentProducerAttachment

    with SessionLocal() as db:
        row = db.get(HermesContentProducerAttachment, int(attachment_id))
        if row is None:
            return {"status": "missing", "attachment_id": int(attachment_id)}
        if row.kind != "reference_video":
            return {"status": "ignored", "attachment_id": int(attachment_id)}
        if row.analysis_status == "ready" and dict(row.analysis_json or {}).get(
            "transcript_status"
        ) in {"success", "no_speech"}:
            return {"status": "ready", "attachment_id": int(attachment_id)}

        analysis = dict(row.analysis_json or {})
        analysis["transcript_status"] = "processing"
        analysis["transcript_started_at"] = _utcnow().isoformat()
        row.analysis_status = "processing"
        row.analysis_json = analysis
        db.commit()

        try:
            source = _producer_attachment_source(row.file_path)
            if _sha256_file(source) != str(row.sha256 or ""):
                raise RuntimeError("producer attachment checksum changed")
            if not _has_audio_stream(source):
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
            row.analysis_status = "ready"
            row.analysis_json = analysis
            db.commit()
            return {"status": "ready", "attachment_id": int(attachment_id)}
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            row = db.get(HermesContentProducerAttachment, int(attachment_id))
            if row is None:
                return {"status": "missing", "attachment_id": int(attachment_id)}
            analysis = dict(row.analysis_json or {})
            analysis.update(
                {
                    "transcript_status": "failed",
                    "transcript_error": type(exc).__name__[:120],
                    "transcript_completed_at": _utcnow().isoformat(),
                }
            )
            # The validated contact sheet remains useful visual evidence.  Do
            # not strand the conversation solely because speech extraction
            # failed; expose the bounded status to the producer instead.
            row.analysis_status = "ready"
            row.analysis_json = analysis
            db.commit()
            logger.exception(
                "producer reference transcription failed attachment_id=%s",
                attachment_id,
            )
            return {
                "status": "ready_with_transcript_failure",
                "attachment_id": int(attachment_id),
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
        stats["expired_jobs_deleted"] = repository.delete_jobs_by_ids(db, [row.job_id for row in expired_rows])
        db.commit()

    stats["uploads_deleted"] = storage.delete_uploads_older_than(None, upload_cutoff_ts, limit=batch_size)
    stats["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("openai whisper cleanup completed", extra=stats)
    return stats
