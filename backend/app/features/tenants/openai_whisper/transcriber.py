"""Thin wrapper around openai-whisper to extract subtitles."""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import whisper

from app.core.config import settings

from .languages import get_language_label

logger = logging.getLogger("gmv.whisper")
_MODEL_LOCK = threading.Lock()
_MODEL = None


def _load_model():
    model_name = getattr(settings, "WHISPER_MODEL_NAME", "small")
    logger.info("loading whisper model", extra={"model": model_name})
    return whisper.load_model(model_name)


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            _MODEL = _load_model()
    return _MODEL


def ensure_ffmpeg_available() -> None:
    """Ensure FFmpeg is available before attempting transcription."""

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info("ffmpeg binary found", extra={"ffmpeg_path": ffmpeg_path})
        return

    message = (
        "FFmpeg is required for Whisper transcription but was not found in PATH. "
        "Please install ffmpeg and ensure it is available on the system PATH."
    )
    logger.error(message, extra={"error": message})
    raise FileNotFoundError(message)


def _format_segments(raw_segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for idx, seg in enumerate(raw_segments or []):
        text = (seg.get("text") or "").strip()
        normalized.append(
            {
                "index": int(seg.get("id", idx)),
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": text,
            }
        )
    return normalized


def _build_prompt(target_language: Optional[str]) -> Optional[str]:
    if not target_language:
        return None
    label = get_language_label(target_language) or target_language
    return f"Translate the audio content into {label}."


def transcribe(
    video_path: Path,
    *,
    source_language: Optional[str] = None,
    translate: bool = False,
    target_language: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_ffmpeg_available()
    model = _get_model()
    options: Dict[str, Any] = {}
    if source_language:
        options["language"] = source_language

    logger.info(
        "starting whisper transcription",
        extra={
            "video": str(video_path),
            "translate": translate,
            "source_language": source_language,
            "target_language": target_language,
        },
    )
    result = model.transcribe(str(video_path), **options)
    detected_language = result.get("language") or source_language
    segments = _format_segments(result.get("segments", []))

    translation_segments = None
    translation_language = None
    if translate:
        translate_options: Dict[str, Any] = {"task": "translate"}
        if source_language:
            translate_options["language"] = source_language
        prompt = _build_prompt(target_language)
        if prompt:
            translate_options["initial_prompt"] = prompt
        translated = model.transcribe(str(video_path), **translate_options)
        translation_segments = _format_segments(translated.get("segments", []))
        translation_language = target_language or "en"

    payload = {
        "segments": segments,
        "source_language": source_language or detected_language,
        "detected_language": detected_language,
        "translation_segments": translation_segments,
        "translation_language": translation_language,
    }
    logger.info(
        "whisper transcription finished",
        extra={"video": str(video_path), "status": "ok", "translate": translate},
    )
    return payload

