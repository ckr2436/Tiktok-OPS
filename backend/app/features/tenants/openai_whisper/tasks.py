"""Celery tasks for running Whisper transcriptions in the background."""
from __future__ import annotations

import logging
import mimetypes
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

        raw_video_path = metadata.get("video_path")
        video_path = Path(raw_video_path) if raw_video_path else None
        share_url = (metadata.get("share_url") or "").strip()

        if share_url and (not video_path or not video_path.exists()):
            try:
                video_path, filename, content_type = _download_shared_video(
                    workspace_id, job_id, share_url, video_path
                )
                size = video_path.stat().st_size

                def _apply(meta: dict) -> dict:
                    meta["video_path"] = str(video_path)
                    meta["filename"] = filename or meta.get("filename") or video_path.name
                    meta["size"] = size
                    if content_type:
                        meta["content_type"] = content_type
                    return meta

                metadata = storage.update_metadata(workspace_id, job_id, _apply)
                repository.update_downloaded_file(
                    db,
                    workspace_id=workspace_id,
                    job_id=job_id,
                    filename=metadata.get("filename"),
                    file_size=size,
                    content_type=metadata.get("content_type"),
                    video_path=str(video_path),
                )
                db.commit()
            except DownloadRequiresAuthError as exc:
                message = "该分享视频需要登录授权才能下载，请登录后重新复制可访问的链接。"
                storage.mark_failed(workspace_id, job_id, message)
                repository.mark_failed(db, workspace_id, job_id, message)
                db.commit()
                logger.warning(
                    "whisper download requires auth",
                    extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)},
                )
                return job_id
            except Exception as exc:  # noqa: BLE001
                message = "视频下载失败，请稍后重试或更换链接。"
                storage.mark_failed(workspace_id, job_id, message)
                repository.mark_failed(db, workspace_id, job_id, message)
                db.commit()
                logger.exception(
                    "whisper download failed",
                    extra={"workspace_id": workspace_id, "job_id": job_id, "error": str(exc)},
                )
                return job_id

        if not video_path or not video_path.exists():
            storage.mark_failed(workspace_id, job_id, "视频源文件已丢失，无法继续。")
            repository.mark_failed(db, workspace_id, job_id, "视频源文件已丢失，无法继续。")
            db.commit()
            logger.error(
                "whisper video missing",
                extra={"workspace_id": workspace_id, "job_id": job_id, "video": str(video_path) if video_path else None},
            )
            return job_id

        storage.mark_processing(workspace_id, job_id)
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

