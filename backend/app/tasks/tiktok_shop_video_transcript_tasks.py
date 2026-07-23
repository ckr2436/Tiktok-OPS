from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.celery_app import WHISPER_TASK_QUEUE, celery_app
from app.core.config import settings
from app.data.db import SessionLocal
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import TikTokShopVideoContentAnalysis
from app.services.gmvmax_creative_media_cache import resolve_creative_media
from app.services.tiktok_shop_video_analysis import FINAL_STATUSES, _media_row, utcnow


logger = logging.getLogger("gmv.tasks.tiktok_shop_video_transcript")
ANALYSIS_QUEUE = str(settings.HERMES_VIDEO_ANALYSIS_TASK_QUEUE)
MAX_TRANSCRIPT_CHARS = 100_000
MAX_TRANSCRIPT_SEGMENTS = 500
_NON_SPEECH_MARKER = re.compile(
    r"^(?:[\[（(]?(?:music|instrumental|background music|音乐|纯音乐|bgm|applause|掌声)"
    r"[\]）)]?|[♪♫♬\s.·…-]+)$",
    re.IGNORECASE,
)


def _minimal_result(row: TikTokShopVideoContentAnalysis) -> dict[str, object]:
    return {"analysis_id": int(row.id), "status": str(row.status)}


def _has_audio_stream(path: Path) -> bool:
    result = subprocess.run(
        [
            "/opt/apps/bin/ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=min(30, int(settings.HERMES_VIDEO_ANALYSIS_FFMPEG_TIMEOUT_SECONDS)),
    )
    return bool((json.loads(result.stdout or "{}") or {}).get("streams"))


def classify_whisper_result(result: dict[str, Any]) -> tuple[str, str | None, list[dict[str, Any]], str]:
    """Return READY/NO_SPEECH without promoting music hallucinations to copy."""

    normalized: list[dict[str, Any]] = []
    for raw in list(result.get("segments") or [])[:MAX_TRANSCRIPT_SEGMENTS]:
        if not isinstance(raw, dict):
            continue
        text_value = str(raw.get("text") or "").strip()
        if not text_value or _NON_SPEECH_MARKER.fullmatch(text_value):
            continue
        item: dict[str, Any] = {
            "index": int(raw.get("index", len(normalized))),
            "start": round(float(raw.get("start") or 0), 3),
            "end": round(float(raw.get("end") or 0), 3),
            "text": text_value[:2000],
        }
        for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            if raw.get(key) is not None:
                item[key] = round(float(raw[key]), 5)
        normalized.append(item)

    if not normalized:
        return "NO_SPEECH", "NO_RELIABLE_SPEECH", [], ""

    confident = [
        item for item in normalized
        if float(item.get("no_speech_prob", 0)) < 0.75
        or float(item.get("avg_logprob", 0)) > -1.0
    ]
    if not confident:
        return "NO_SPEECH", "LOW_SPEECH_CONFIDENCE", [], ""

    text_value = "\n".join(item["text"] for item in confident).strip()[:MAX_TRANSCRIPT_CHARS]
    if not text_value:
        return "NO_SPEECH", "NO_RELIABLE_SPEECH", [], ""
    return "READY", None, confident, text_value


def _dispatch_analysis(analysis_id: int) -> None:
    celery_app.send_task(
        "tiktok_shop_video_analysis.run",
        kwargs={"analysis_id": int(analysis_id)},
        queue=ANALYSIS_QUEUE,
    )


def _dispatch_analysis_or_fail(db, row: TikTokShopVideoContentAnalysis) -> None:
    try:
        _dispatch_analysis(int(row.id))
    except Exception as exc:  # noqa: BLE001
        row.status = "FAILED"
        row.error_code = "ANALYSIS_QUEUE_PUBLISH_FAILED"
        row.error_message = str(exc)[:1500]
        row.completed_at = utcnow()
        db.add(row)
        db.commit()
        raise


@celery_app.task(
    name="tiktok_shop_video_transcript.prepare",
    bind=True,
    queue=str(WHISPER_TASK_QUEUE),
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=18 * 60,
    time_limit=20 * 60,
)
def prepare_tiktok_shop_video_transcript(self, *, analysis_id: int) -> dict[str, object]:
    with SessionLocal() as db:
        row = db.scalar(
            select(TikTokShopVideoContentAnalysis)
            .where(TikTokShopVideoContentAnalysis.id == int(analysis_id))
            .with_for_update()
        )
        if not row:
            return {"analysis_id": int(analysis_id), "status": "missing"}
        if row.status in FINAL_STATUSES:
            return _minimal_result(row)
        if row.transcript_status in {"READY", "NO_SPEECH", "FAILED", "UNAVAILABLE"}:
            _dispatch_analysis_or_fail(db, row)
            return _minimal_result(row)
        if int(row.transcript_attempts or 0) >= 3:
            row.transcript_status = "FAILED"
            row.transcript_reason = "ATTEMPTS_EXHAUSTED"
            row.transcript_error_message = "Whisper retry budget was exhausted."
            row.transcript_completed_at = utcnow()
            db.commit()
            _dispatch_analysis_or_fail(db, row)
            return _minimal_result(row)

        row.transcript_status = "PROCESSING"
        row.transcript_source = "WHISPER_LOCAL"
        row.transcript_started_at = utcnow()
        row.transcript_completed_at = None
        row.transcript_reason = None
        row.transcript_error_message = None
        row.transcript_attempts = int(row.transcript_attempts or 0) + 1
        db.commit()

        try:
            shop = db.get(OAuthTikTokShopShop, int(row.shop_row_id))
            if not shop or int(shop.workspace_id) != int(row.workspace_id):
                raise RuntimeError("tenant-scoped TikTok Shop was not found")
            media_row = _media_row(
                db,
                workspace_id=int(row.workspace_id),
                provider_shop_id=str(shop.shop_id),
                video_id=str(row.video_id),
            )
            video = resolve_creative_media(media_row, "video") if media_row else None
            if not video:
                row.transcript_status = "UNAVAILABLE"
                row.transcript_reason = "LOCAL_VIDEO_UNAVAILABLE"
                row.transcript_completed_at = utcnow()
                db.commit()
                _dispatch_analysis_or_fail(db, row)
                return _minimal_result(row)

            video_path = Path(video[0])
            if not _has_audio_stream(video_path):
                row.transcript_status = "NO_SPEECH"
                row.transcript_reason = "NO_AUDIO_TRACK"
                row.transcript_text = None
                row.transcript_segments_json = []
                row.transcript_completed_at = utcnow()
                db.commit()
                _dispatch_analysis_or_fail(db, row)
                return _minimal_result(row)

            # Import lazily so API and lightweight video-analysis workers never
            # load torch/Whisper. Only the dedicated Whisper worker pays this cost.
            from app.features.tenants.openai_whisper import transcriber

            result = transcriber.transcribe(video_path, translate=False)
            status, reason, segments, text_value = classify_whisper_result(result)
            row.transcript_status = status
            row.transcript_language = str(
                result.get("detected_language") or result.get("source_language") or ""
            )[:32] or None
            row.transcript_reason = reason
            row.transcript_text = text_value or None
            row.transcript_segments_json = segments
            row.transcript_completed_at = utcnow()
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            row = db.get(TikTokShopVideoContentAnalysis, int(analysis_id))
            if not row:
                return {"analysis_id": int(analysis_id), "status": "missing"}
            row.transcript_status = "FAILED"
            row.transcript_reason = type(exc).__name__[:64]
            row.transcript_error_message = str(exc)[:1500]
            row.transcript_completed_at = utcnow()
            db.commit()
            logger.exception("TikTok Shop video transcription failed analysis_id=%s", analysis_id)

        _dispatch_analysis_or_fail(db, row)
        return _minimal_result(row)


__all__ = ["classify_whisper_result", "prepare_tiktok_shop_video_transcript"]
