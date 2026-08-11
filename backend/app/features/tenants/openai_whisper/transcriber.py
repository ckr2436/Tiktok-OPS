"""Thin wrapper around openai-whisper to extract subtitles and translate segments."""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import whisper
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    MarianMTModel,
    MarianTokenizer,
    pipeline,
)
from transformers.pipelines import TranslationPipeline

from app.core.config import settings

from .languages import get_language_label

logger = logging.getLogger("gmv.whisper")
_MODEL_LOCK = threading.Lock()
_MODEL = None
_TRANSLATORS: Dict[tuple[str, str], TranslationPipeline] = {}
_TRANSLATOR_LOCK = threading.Lock()
_NLLB_LOCK = threading.Lock()
_NLLB_TOKENIZER = None
_NLLB_MODEL = None

# MarianMT is fast and lightweight, but only some direct language pairs exist on Hugging Face.
# Never blindly construct model ids in production, otherwise pairs like nn->zh become
# Helsinki-NLP/opus-mt-nn-zh and fail with a raw Hugging Face error.
_MARIAN_MODEL_OVERRIDES = {
    ("es", "zh"): "Helsinki-NLP/opus-tatoeba-es-zh",
    ("en", "zh"): "Helsinki-NLP/opus-mt-en-zh",
    ("zh", "en"): "Helsinki-NLP/opus-mt-zh-en",
    ("es", "en"): "Helsinki-NLP/opus-mt-es-en",
    ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
    ("de", "en"): "Helsinki-NLP/opus-mt-de-en",
}

# NLLB-200 distilled is a free/open multilingual fallback. It is heavier than Marian,
# but it supports many pairs and avoids invalid Helsinki model ids.
_NLLB_LANGUAGE_CODES = {
    "af": "afr_Latn",
    "ar": "arb_Arab",
    "bg": "bul_Cyrl",
    "bn": "ben_Beng",
    "ca": "cat_Latn",
    "cs": "ces_Latn",
    "da": "dan_Latn",
    "de": "deu_Latn",
    "el": "ell_Grek",
    "en": "eng_Latn",
    "es": "spa_Latn",
    "et": "est_Latn",
    "fa": "pes_Arab",
    "fi": "fin_Latn",
    "fr": "fra_Latn",
    "he": "heb_Hebr",
    "hi": "hin_Deva",
    "hr": "hrv_Latn",
    "hu": "hun_Latn",
    "id": "ind_Latn",
    "it": "ita_Latn",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "lt": "lit_Latn",
    "lv": "lvs_Latn",
    "ms": "zsm_Latn",
    "nl": "nld_Latn",
    "no": "nob_Latn",
    "nb": "nob_Latn",
    "nn": "nno_Latn",
    "pl": "pol_Latn",
    "pt": "por_Latn",
    "ro": "ron_Latn",
    "ru": "rus_Cyrl",
    "sk": "slk_Latn",
    "sl": "slv_Latn",
    "sv": "swe_Latn",
    "sw": "swh_Latn",
    "th": "tha_Thai",
    "tr": "tur_Latn",
    "uk": "ukr_Cyrl",
    "ur": "urd_Arab",
    "vi": "vie_Latn",
    "zh": "zho_Hans",
    "zh-cn": "zho_Hans",
    "zh-hans": "zho_Hans",
    "zt": "zho_Hant",
    "zh-tw": "zho_Hant",
    "zh-hant": "zho_Hant",
}


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


def _normalize_lang_code(code: str | None) -> str:
    value = (code or "en").strip().lower()
    if value in {"zh-cn", "zh-hans"}:
        return "zh"
    if value in {"zh-tw", "zh-hant"}:
        return "zt"
    return value


def _translation_backend() -> str:
    return str(getattr(settings, "OPENAI_WHISPER_TRANSLATION_BACKEND", "auto") or "auto").strip().lower()


def _resolve_marian_model(source_language: str, target_language: str) -> str | None:
    key = (_normalize_lang_code(source_language), _normalize_lang_code(target_language))
    model_name = _MARIAN_MODEL_OVERRIDES.get(key)
    if model_name:
        return model_name
    if bool(getattr(settings, "OPENAI_WHISPER_ALLOW_DYNAMIC_MARIAN_MODEL_ID", False)):
        return f"Helsinki-NLP/opus-mt-{key[0]}-{key[1]}"
    return None


def _load_translation_pipeline(source_language: str, target_language: str) -> TranslationPipeline:
    model_name = _resolve_marian_model(source_language, target_language)
    if not model_name:
        raise RuntimeError(
            f"当前 Marian 免费模型未配置 {source_language}->{target_language} 语言对。"
        )
    cache_dir = Path(settings.OPENAI_WHISPER_STORAGE_DIR).expanduser() / "models"
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "loading MarianMT translation model",
        extra={"model": model_name, "source": source_language, "target": target_language, "cache_dir": str(cache_dir)},
    )
    tokenizer = MarianTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
    model = MarianMTModel.from_pretrained(model_name, cache_dir=cache_dir)
    return pipeline("translation", model=model, tokenizer=tokenizer)


def _get_translation_pipeline(source_language: str, target_language: str) -> TranslationPipeline:
    key = (_normalize_lang_code(source_language), _normalize_lang_code(target_language))
    translator = _TRANSLATORS.get(key)
    if translator:
        return translator
    with _TRANSLATOR_LOCK:
        translator = _TRANSLATORS.get(key)
        if translator is None:
            _TRANSLATORS[key] = _load_translation_pipeline(key[0], key[1])
        return _TRANSLATORS[key]


def _get_nllb():
    global _NLLB_TOKENIZER, _NLLB_MODEL
    if _NLLB_TOKENIZER is not None and _NLLB_MODEL is not None:
        return _NLLB_TOKENIZER, _NLLB_MODEL
    with _NLLB_LOCK:
        if _NLLB_TOKENIZER is None or _NLLB_MODEL is None:
            model_name = str(
                getattr(settings, "OPENAI_WHISPER_NLLB_MODEL_NAME", "facebook/nllb-200-distilled-600M")
                or "facebook/nllb-200-distilled-600M"
            )
            cache_dir = Path(settings.OPENAI_WHISPER_STORAGE_DIR).expanduser() / "models"
            cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("loading NLLB translation model", extra={"model": model_name, "cache_dir": str(cache_dir)})
            _NLLB_TOKENIZER = AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
            _NLLB_MODEL = AutoModelForSeq2SeqLM.from_pretrained(model_name, cache_dir=cache_dir)
    return _NLLB_TOKENIZER, _NLLB_MODEL


def _nllb_code(language: str) -> str:
    normalized = _normalize_lang_code(language)
    code = _NLLB_LANGUAGE_CODES.get(normalized)
    if not code:
        label = get_language_label(normalized) or normalized
        raise RuntimeError(f"当前免费 NLLB 翻译模型暂不支持语言：{label} ({normalized})。")
    return code


def _translate_text_nllb(text: str, *, source_language: str, target_language: str) -> str:
    if not text.strip():
        return ""
    tokenizer, model = _get_nllb()
    src_code = _nllb_code(source_language)
    tgt_code = _nllb_code(target_language)
    tokenizer.src_lang = src_code
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    forced_bos_token_id = tokenizer.convert_tokens_to_ids(tgt_code)
    translated_tokens = model.generate(
        **inputs,
        forced_bos_token_id=forced_bos_token_id,
        max_length=512,
        num_beams=4,
    )
    return tokenizer.batch_decode(translated_tokens, skip_special_tokens=True)[0].strip()


def ensure_ffmpeg_available() -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        logger.info("ffmpeg binary found", extra={"ffmpeg_path": ffmpeg_path})
        return
    message = "FFmpeg is required for Whisper transcription but was not found in PATH."
    logger.error(message, extra={"error": message})
    raise FileNotFoundError(message)


def _format_segments(raw_segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for idx, seg in enumerate(raw_segments or []):
        text = (seg.get("text") or "").strip()
        item = {
            "index": int(seg.get("id", idx)),
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": text,
        }
        # Preserve Whisper's speech-confidence evidence. Existing subtitle
        # consumers ignore these optional keys, while content analysis uses
        # them to avoid turning music/silence hallucinations into ad copy.
        for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            if seg.get(key) is not None:
                item[key] = float(seg[key])
        normalized.append(item)
    return normalized


def _build_prompt(target_language: Optional[str]) -> Optional[str]:
    if not target_language:
        return None
    label = get_language_label(target_language) or target_language
    return f"Translate the audio content into {label}."


def _translate_segments(segments: List[Dict[str, Any]], *, source_language: str, target_language: str) -> List[Dict[str, Any]]:
    source_lang = _normalize_lang_code(source_language)
    target_lang = _normalize_lang_code(target_language)
    if source_lang == target_lang:
        logger.info("skipping translation for identical language pair", extra={"source": source_lang, "target": target_lang})
        return [
            {"index": int(seg.get("index", seg.get("id", 0))), "start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0)), "text": (seg.get("text") or "").strip()}
            for seg in segments or []
        ]

    backend = _translation_backend()
    translated_segments: List[Dict[str, Any]] = []

    if backend in {"marian", "auto"} and _resolve_marian_model(source_lang, target_lang):
        translator = _get_translation_pipeline(source_lang, target_lang)
        for seg in segments or []:
            text = (seg.get("text") or "").strip()
            if text:
                translated = translator(text, max_length=512, clean_up_tokenization_spaces=True)
                translated_text = (translated[0].get("translation_text") or "").strip()
            else:
                translated_text = ""
            translated_segments.append({"index": int(seg.get("index", seg.get("id", 0))), "start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0)), "text": translated_text})
        return translated_segments

    if backend in {"nllb", "auto"}:
        for seg in segments or []:
            text = (seg.get("text") or "").strip()
            translated_text = _translate_text_nllb(text, source_language=source_lang, target_language=target_lang) if text else ""
            translated_segments.append({"index": int(seg.get("index", seg.get("id", 0))), "start": float(seg.get("start", 0.0)), "end": float(seg.get("end", 0.0)), "text": translated_text})
        return translated_segments

    raise RuntimeError(
        f"未配置可用的免费翻译模型：{source_lang}->{target_lang}。请在 .env 设置 OPENAI_WHISPER_TRANSLATION_BACKEND=auto 或 nllb。"
    )


def transcribe(video_path: Path, *, source_language: Optional[str] = None, translate: bool = False, target_language: Optional[str] = None) -> Dict[str, Any]:
    ensure_ffmpeg_available()
    model = _get_model()
    options: Dict[str, Any] = {}
    if source_language:
        options["language"] = source_language

    logger.info("starting whisper transcription", extra={"video": str(video_path), "translate": translate, "source_language": source_language, "target_language": target_language})
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
        translation_segments = _translate_segments(segments, source_language=translation_source, target_language=translation_language)

    payload = {
        "segments": segments,
        "source_language": source_language or detected_language,
        "detected_language": detected_language,
        "translation_segments": translation_segments,
        "translation_language": translation_language,
    }
    logger.info("whisper transcription finished", extra={"video": str(video_path), "status": "ok", "translate": translate})
    return payload
