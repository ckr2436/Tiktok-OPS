"""Celery tasks for running Whisper transcriptions in the background."""
from __future__ import annotations

import logging
import math
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, List, Tuple

from app.celery_app import celery_app
from app.core.config import settings
from app.data.db import SessionLocal

from yt_dlp import YoutubeDL

from . import repository, storage, transcriber

logger = logging.getLogger("gmv.tasks.openai_whisper")
WHISPER_TASK_QUEUE = (
    getattr(settings, "OPENAI_WHISPER_TASK_QUEUE", None)
    or getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "gmv.tasks.default")
)
ALLOWED_CONTACT_INTERVALS = {0.5, 1.0, 1.5, 2.0}


class DownloadRequiresAuthError(RuntimeError):
    """Raised when a share link requires authentication to download."""


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
    ]
    return any(marker in message for marker in markers)


def _probe_downloadable(share_url: str) -> Tuple[dict, str]:
    options = {"quiet": True, "skip_download": True, "noplaylist": True}
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


def _download_shared_video(
    workspace_id: int, job_id: str, share_url: str, video_path: Path | None
) -> Tuple[Path, str, str | None]:
    entry, ext = _probe_downloadable(share_url)
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
        # Avoid temp ``.part`` files whose rename step may fail under
        # concurrent downloads.
        "nopart": True,
    }

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
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


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


def _mark_download_status(
    db: SessionLocal,
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
    return metadata


def _ensure_local_video(db, workspace_id: int, job_id: str, metadata: dict) -> tuple[dict | None, Path | None, str | None]:
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
            _mark_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="processing",
            )
            db.commit()
            video_path, filename, content_type = _download_shared_video(
                workspace_id, job_id, share_url, video_path
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
        except DownloadRequiresAuthError as exc:
            message = "该分享视频需要登录授权才能下载，请登录后重新复制可访问的链接。"
            _mark_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=message,
            )
            db.commit()
            logger.warning(
                "whisper download requires auth",
                extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)},
            )
            return None, None, message
        except Exception as exc:  # noqa: BLE001
            message = "视频下载失败，请稍后重试或更换链接。"
            _mark_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=message,
            )
            db.commit()
            logger.exception(
                "whisper download failed",
                extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)},
            )
            return None, None, message

    if not video_path or not video_path.exists():
        message = "视频源文件已丢失，无法继续。"
        _mark_download_status(
            db,
            workspace_id=workspace_id,
            job_id=job_id,
            status="failed",
            message=message,
        )
        db.commit()
        return None, None, message

    if (metadata.get("download_status") or "") in {"pending", "processing"}:
        metadata = _mark_download_status(
            db,
            workspace_id=workspace_id,
            job_id=job_id,
            status="success",
        )
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
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps=1/{interval},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        str(pattern),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frames = sorted(frames_dir.glob("frame_*.png"))
    if frames:
        return frames

    fallback = frames_dir / "frame_001.png"
    fallback_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        str(fallback),
    ]
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
        probe_cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        duration_raw = subprocess.check_output(probe_cmd, stderr=subprocess.STDOUT).decode().strip()
        duration_seconds = max(0.0, float(duration_raw)) if duration_raw else 0.0
    except Exception:  # noqa: BLE001
        duration_seconds = 0.0

    expected_frames = frame_count
    if duration_seconds > 0 and interval > 0:
        expected_frames = max(expected_frames, math.ceil(duration_seconds / interval))

    rows, cols = _best_grid(expected_frames)
    output_path = storage.contact_sheet_path(directory)
    tile_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(frames_dir / "frame_%03d.png"),
        "-frames:v",
        "1",
        "-vf",
        f"tile={cols}x{rows}:padding=4:margin=10",
        str(output_path),
    ]
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
            storage.update_component_status(
                workspace_id,
                job_id,
                "subtitle",
                status="failed",
                error=video_error,
            )
            repository.mark_failed(db, workspace_id, job_id, video_error)
            db.commit()
            return job_id

        if not video_path or not video_path.exists():
            message = "视频源文件已丢失，无法继续。"
            storage.update_component_status(
                workspace_id,
                job_id,
                "subtitle",
                status="failed",
                error=message,
            )
            repository.mark_failed(db, workspace_id, job_id, message)
            db.commit()
            logger.error(
                "whisper video missing",
                extra={"workspace_id": workspace_id, "job_id": job_id, "video": str(video_path) if video_path else None},
            )
            return job_id

        storage.update_component_status(workspace_id, job_id, "subtitle", status="processing")
        repository.mark_processing(db, workspace_id, job_id)
        db.commit()
        try:
            result = transcriber.transcribe(
                video_path,
                source_language=metadata.get("source_language"),
                translate=bool(metadata.get("translate")),
                target_language=metadata.get("target_language"),
            )
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
            translation_srt = _segments_to_srt(translation_segments)
            storage.write_subtitles_file(workspace_id, job_id, "translation", translation_srt)

        storage.save_results(workspace_id, job_id, result)
        repository.mark_completed(
            db,
            workspace_id,
            job_id,
            detected_language=result.get("detected_language") or result.get("source_language"),
            translation_language=result.get("translation_language"),
            segments_count=len(result.get("segments") or []),
            translation_segments_count=len(result.get("translation_segments") or []),
        )
        db.commit()
        logger.info("whisper transcription completed", extra={"workspace_id": workspace_id, "job_id": job_id})
    return job_id


@celery_app.task(name="openai_whisper.download_shared_video", bind=True, queue=WHISPER_TASK_QUEUE)
def download_shared_video(self, *, workspace_id: int, job_id: str) -> str:
    with SessionLocal() as db:
        try:
            metadata = storage.load_metadata(workspace_id, job_id)
        except FileNotFoundError:
            logger.error("download metadata missing", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message="任务元数据缺失，无法继续。",
            )
            db.commit()
            return job_id
        except storage.MetadataCorruptedError:
            logger.error("download metadata corrupted", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message="任务元数据损坏，无法继续。",
            )
            db.commit()
            return job_id

        metadata, video_path, video_error = _ensure_local_video(db, workspace_id, job_id, metadata)
        if video_error:
            storage.update_component_status(
                workspace_id,
                job_id,
                "download",
                status="failed",
                error=video_error,
            )
            repository.update_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=video_error,
            )
            db.commit()
            return job_id

        if video_path and video_path.exists():
            storage.update_component_status(
                workspace_id,
                job_id,
                "download",
                status="success",
                url=metadata.get("download_url"),
            )
            repository.update_download_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="success",
                download_url=metadata.get("download_url"),
            )
            db.commit()

        if metadata.get("share_url"):
            spawned_tasks: list[str] = []
            if metadata.get("do_subtitle") and metadata.get("subtitle_status") == "pending":
                async_result = transcribe_video.delay(
                    workspace_id=workspace_id,
                    job_id=job_id,
                )
                spawned_tasks.append(async_result.id)
            if metadata.get("do_contact_sheet") and metadata.get("contact_sheet_status") == "pending":
                contact_result = generate_contact_sheet.delay(
                    workspace_id=workspace_id,
                    job_id=job_id,
                    contact_interval=metadata.get("contact_interval"),
                )
                spawned_tasks.append(contact_result.id)

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
def generate_contact_sheet(
    self,
    *,
    workspace_id: int,
    job_id: str,
    contact_interval: float | None = None,
) -> str:
    with SessionLocal() as db:
        try:
            metadata = storage.load_metadata(workspace_id, job_id)
        except FileNotFoundError:
            logger.error("contact sheet metadata missing", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message="任务元数据缺失，无法继续。",
            )
            db.commit()
            return job_id
        except storage.MetadataCorruptedError:
            logger.error("contact sheet metadata corrupted", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message="任务元数据损坏，无法继续。",
            )
            db.commit()
            return job_id

        metadata, video_path, video_error = _ensure_local_video(db, workspace_id, job_id, metadata)
        if video_error:
            storage.update_component_status(
                workspace_id,
                job_id,
                "contact_sheet",
                status="failed",
                error=video_error,
            )
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=video_error,
            )
            db.commit()
            return job_id

        if not video_path or not video_path.exists():
            message = "视频源文件已丢失，无法生成拼图。"
            storage.update_component_status(
                workspace_id,
                job_id,
                "contact_sheet",
                status="failed",
                error=message,
            )
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=message,
            )
            db.commit()
            return job_id

        interval_value = contact_interval or metadata.get("contact_interval")
        try:
            interval_value = float(interval_value)
        except (TypeError, ValueError):
            interval_value = None
        if interval_value not in ALLOWED_CONTACT_INTERVALS:
            message = "抽帧间隔不合法，无法生成拼图。"
            storage.update_component_status(
                workspace_id,
                job_id,
                "contact_sheet",
                status="failed",
                error=message,
            )
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=message,
            )
            db.commit()
            return job_id

        storage.update_component_status(workspace_id, job_id, "contact_sheet", status="processing")
        repository.update_contact_sheet_status(
            db,
            workspace_id=workspace_id,
            job_id=job_id,
            status="processing",
        )
        db.commit()
        try:
            output_path = _render_contact_sheet(video_path, workspace_id, job_id, interval_value)
            download_url = f"/api/v1/tenants/{workspace_id}/openai-whisper/jobs/{job_id}/contact-sheet"
            storage.update_component_status(
                workspace_id,
                job_id,
                "contact_sheet",
                status="success",
                url=download_url,
            )
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="success",
                contact_sheet_url=download_url,
            )
            db.commit()
            logger.info(
                "contact sheet generated",
                extra={"workspace_id": workspace_id, "job_id": job_id, "path": str(output_path)},
            )
        except Exception as exc:  # noqa: BLE001
            message = "拆解图片失败，请稍后再试。"
            storage.update_component_status(
                workspace_id,
                job_id,
                "contact_sheet",
                status="failed",
                error=message,
            )
            repository.update_contact_sheet_status(
                db,
                workspace_id=workspace_id,
                job_id=job_id,
                status="failed",
                message=message,
            )
            db.commit()
            logger.exception(
                "contact sheet generation failed",
                extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)},
            )
        return job_id

