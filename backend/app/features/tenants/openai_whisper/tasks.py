"""Celery tasks for running Whisper transcriptions in the background."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, List

from app.celery_app import celery_app
from app.data.db import SessionLocal

from . import repository, storage, transcriber

logger = logging.getLogger("gmv.tasks.openai_whisper")


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


@celery_app.task(name="openai_whisper.transcribe_video", bind=True, queue="gmv.tasks.default")
def transcribe_video(self, *, workspace_id: int, job_id: str) -> str:
    with SessionLocal() as db:
        try:
            metadata = storage.load_metadata(workspace_id, job_id)
        except FileNotFoundError:
            logger.error("whisper job metadata missing", extra={"workspace_id": workspace_id, "job_id": job_id})
            repository.mark_failed(db, workspace_id, job_id, "任务元数据缺失，无法继续。")
            db.commit()
            return job_id

        video_path = Path(metadata.get("video_path") or "")
        if not video_path.exists():
            storage.mark_failed(workspace_id, job_id, "视频源文件已丢失，无法继续。")
            repository.mark_failed(db, workspace_id, job_id, "视频源文件已丢失，无法继续。")
            db.commit()
            logger.error(
                "whisper video missing",
                extra={"workspace_id": workspace_id, "job_id": job_id, "video": str(video_path)},
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

