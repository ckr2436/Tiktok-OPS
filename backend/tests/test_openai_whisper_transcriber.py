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


def _make_executable(path, content):
    path.write_text(content)
    path.chmod(0o755)
    return str(path)


def test_ensure_ffmpeg_uses_first_working_candidate(monkeypatch, tmp_path):
    bad = _make_executable(tmp_path / "ffmpeg-bad", "#!/bin/sh\nexit 1\n")
    working = _make_executable(
        tmp_path / "ffmpeg-ok", "#!/bin/sh\necho 'ffmpeg version 6.0'\n"
    )

    monkeypatch.setattr(transcriber, "_candidate_ffmpeg_cmds", lambda: [bad, working])

    # Should not raise because the second candidate works.
    transcriber.ensure_ffmpeg_available()


def test_ensure_ffmpeg_raises_with_actionable_hint(monkeypatch, tmp_path):
    missing_cmd = tmp_path / "does-not-exist"
    monkeypatch.setattr(transcriber, "_candidate_ffmpeg_cmds", lambda: [str(missing_cmd)])

    with pytest.raises(RuntimeError) as exc_info:
        transcriber.ensure_ffmpeg_available()

    message = str(exc_info.value)
    assert "OPENAI_WHISPER_FFMPEG_BIN" in message
    assert "does-not-exist" in message

