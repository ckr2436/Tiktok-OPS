"""Thin wrapper around openai-whisper to extract subtitles."""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import whisper
from transformers import MarianMTModel, MarianTokenizer, pipeline
from transformers.pipelines import TranslationPipeline

from app.core.config import settings

from .languages import get_language_label

logger = logging.getLogger("gmv.whisper")
_MODEL_LOCK = threading.Lock()
_MODEL = None
_TRANSLATORS: Dict[tuple[str, str], TranslationPipeline] = {}
_TRANSLATOR_LOCK = threading.Lock()


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


def _load_translation_pipeline(
    source_language: str, target_language: str
) -> TranslationPipeline:
    model_name = f"Helsinki-NLP/opus-mt-{source_language}-{target_language}"
    logger.info(
        "loading MarianMT translation model",
        extra={"model": model_name, "source": source_language, "target": target_language},
    )
    tokenizer = MarianTokenizer.from_pretrained(model_name)
    model = MarianMTModel.from_pretrained(model_name)
    return pipeline("translation", model=model, tokenizer=tokenizer)


def _get_translation_pipeline(
    source_language: str, target_language: str
) -> TranslationPipeline:
    key = (source_language, target_language)
    translator = _TRANSLATORS.get(key)
    if translator:
        return translator

    with _TRANSLATOR_LOCK:
        translator = _TRANSLATORS.get(key)
        if translator is None:
            _TRANSLATORS[key] = _load_translation_pipeline(source_language, target_language)
        return _TRANSLATORS[key]


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


def _translate_segments(
    segments: List[Dict[str, Any]],
    *,
    source_language: str,
    target_language: str,
) -> List[Dict[str, Any]]:
    source_lang = (source_language or "en").lower()
    target_lang = (target_language or "en").lower()
    if source_lang == target_lang:
        logger.info(
            "skipping translation for identical language pair",
            extra={"source": source_lang, "target": target_lang},
        )
        return [
            {
                "index": int(seg.get("index", seg.get("id", 0))),
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": (seg.get("text") or "").strip(),
            }
            for seg in segments or []
        ]

    translator = _get_translation_pipeline(source_lang, target_lang)
    translated_segments: List[Dict[str, Any]] = []

    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if text:
            translated = translator(
                text,
                max_length=512,
                clean_up_tokenization_spaces=True,
            )
            translated_text = (translated[0].get("translation_text") or "").strip()
        else:
            translated_text = ""

        translated_segments.append(
            {
                "index": int(seg.get("index", seg.get("id", 0))),
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": translated_text,
            }
        )

    return translated_segments


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
        translation_language = target_language or "en"
        translation_source = source_language or detected_language or "en"
        prompt = _build_prompt(target_language)
        if prompt:
            logger.info("whisper translation prompt", extra={"prompt": prompt})
        translation_segments = _translate_segments(
            segments,
            source_language=translation_source,
            target_language=translation_language,
        )

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

