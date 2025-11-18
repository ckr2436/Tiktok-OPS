"""Language helpers built from openai-whisper tables."""

from __future__ import annotations

from typing import List, Optional

from whisper.tokenizer import LANGUAGES, TO_LANGUAGE_CODE

_LANGUAGE_OPTIONS = [
    {"code": code, "name": name.title()}
    for code, name in LANGUAGES.items()
]
_LANGUAGE_OPTIONS.sort(key=lambda item: item["name"])

_LANGUAGE_CODE_SET = {code.lower() for code in LANGUAGES.keys()}
_LANGUAGE_NAME_TO_CODE = {
    name.lower(): code for code, name in LANGUAGES.items()
}


def list_language_options() -> List[dict]:
    """Return a stable copy of the language options."""
    return [dict(item) for item in _LANGUAGE_OPTIONS]


def normalize_language_code(raw: Optional[str]) -> Optional[str]:
    """Convert a code or name to the canonical 2-letter language code."""
    if not raw:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in _LANGUAGE_CODE_SET:
        return value
    if value in TO_LANGUAGE_CODE:
        return TO_LANGUAGE_CODE[value]
    if value in _LANGUAGE_NAME_TO_CODE:
        return _LANGUAGE_NAME_TO_CODE[value]
    return None


def get_language_label(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    normalized = code.lower()
    name = LANGUAGES.get(normalized)
    if name:
        return name.title()
    return None

