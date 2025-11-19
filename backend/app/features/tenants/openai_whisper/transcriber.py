"""Thin wrapper around openai-whisper to extract subtitles."""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import whisper

from app.core.config import settings

from .languages import get_language_label

logger = logging.getLogger("gmv.whisper")
_MODEL_LOCK = threading.Lock()
_MODEL = None


def _get_ffmpeg_cmd() -> str:
    return getattr(settings, "OPENAI_WHISPER_FFMPEG_BIN", None) or "ffmpeg"


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
    """Ensure FFmpeg is available before running Whisper.

    Raises:
        RuntimeError: If FFmpeg cannot be located in PATH.
    """

    ffmpeg_cmd = _get_ffmpeg_cmd()

    try:
        proc = subprocess.run(
            [ffmpeg_cmd, "-version"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            logger.debug("ffmpeg responded to version check", extra={"ffmpeg_cmd": ffmpeg_cmd})
            return
        error_detail = (proc.stderr or proc.stdout or "unknown error").strip()
    except FileNotFoundError:
        error_detail = "command not found"
    except Exception as exc:  # pragma: no cover - defensive
        error_detail = str(exc)

    resolved = shutil.which(ffmpeg_cmd)
    if resolved:
        logger.debug("ffmpeg located", extra={"ffmpeg_cmd": ffmpeg_cmd, "resolved": resolved})

    raise RuntimeError(
        "FFmpeg 未安装或不可用，无法执行字幕生成任务。 "
        f"尝试的命令：{ffmpeg_cmd}。错误：{error_detail}"
    )


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

