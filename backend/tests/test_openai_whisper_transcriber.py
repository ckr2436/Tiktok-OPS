import sys
import types

import pytest


# Stub whisper to avoid pulling the heavyweight dependency during tests.
_dummy_whisper = types.ModuleType("whisper")
_dummy_whisper.load_model = lambda name="small": object()  # noqa: ARG005 - test stub
_dummy_tokenizer = types.ModuleType("whisper.tokenizer")
_dummy_tokenizer.LANGUAGES = {"en": "English"}
_dummy_tokenizer.TO_LANGUAGE_CODE = {"english": "en"}
_dummy_whisper.tokenizer = _dummy_tokenizer
sys.modules.setdefault("whisper", _dummy_whisper)
sys.modules.setdefault("whisper.tokenizer", _dummy_tokenizer)

from app.features.tenants.openai_whisper import transcriber  # noqa: E402  - stubbed above


def test_ensure_ffmpeg_available_when_present(monkeypatch, caplog):
    monkeypatch.setattr(transcriber.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    with caplog.at_level("INFO"):
        transcriber.ensure_ffmpeg_available()

    assert "ffmpeg binary found" in " ".join(caplog.messages)


def test_ensure_ffmpeg_available_missing(monkeypatch, caplog):
    monkeypatch.setattr(transcriber.shutil, "which", lambda _: None)

    with caplog.at_level("ERROR"):
        with pytest.raises(FileNotFoundError):
            transcriber.ensure_ffmpeg_available()

    assert "FFmpeg is required for Whisper transcription" in " ".join(caplog.messages)

