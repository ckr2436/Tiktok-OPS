import sys
import types


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


def test_ensure_ffmpeg_is_noop(caplog):
    with caplog.at_level("INFO"):
        transcriber.ensure_ffmpeg_available()

    assert "skipping ffmpeg availability check" in " ".join(caplog.messages)

