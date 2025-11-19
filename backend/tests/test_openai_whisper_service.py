import asyncio
import io
import types
import sys


# Stub the optional whisper dependency so the service module can be imported without
# pulling the heavyweight model (which is not available in CI).
_dummy_whisper = types.ModuleType("whisper")
_dummy_tokenizer = types.ModuleType("whisper.tokenizer")
_dummy_tokenizer.LANGUAGES = {"en": "English"}
_dummy_tokenizer.TO_LANGUAGE_CODE = {"english": "en"}
_dummy_whisper.tokenizer = _dummy_tokenizer
_dummy_whisper.load_model = lambda name="small": object()
sys.modules.setdefault("whisper", _dummy_whisper)
sys.modules.setdefault("whisper.tokenizer", _dummy_tokenizer)

from app.features.tenants.openai_whisper import service, storage, tasks as whisper_tasks


class _DummyAsyncResult:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


class _DummyUpload:
    def __init__(self, content: bytes, filename: str, content_type: str):
        self._buffer = io.BytesIO(content)
        self.filename = filename
        self.content_type = content_type

    async def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    async def seek(self, offset: int) -> None:
        self._buffer.seek(offset)


def test_create_job_commits_before_enqueue(monkeypatch, db_session, tmp_path):
    """The job row must be committed before the Celery task is dispatched."""

    # Redirect Whisper storage to a temp directory so the test does not touch real paths.
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)

    upload = _DummyUpload(b"fake video", filename="sample.mp4", content_type="video/mp4")

    events: list[str] = []
    original_commit = db_session.commit

    def tracking_commit() -> None:
        events.append("commit")
        original_commit()

    monkeypatch.setattr(db_session, "commit", tracking_commit)

    dummy_result = _DummyAsyncResult("celery-task-id")

    def fake_delay(*, workspace_id: int, job_id: str):  # noqa: ARG001 - signature parity
        events.append("delay")
        assert workspace_id == 1
        assert isinstance(job_id, str)
        return dummy_result

    monkeypatch.setattr(whisper_tasks.transcribe_video, "delay", fake_delay)

    response = asyncio.run(
        service.create_job(
            workspace_id=1,
            user_id=42,
            upload=upload,
            upload_id=None,
            source_language=None,
            translate=False,
            target_language=None,
            show_bilingual=False,
            db=db_session,
        )
    )

    assert events[0] == "commit"
    assert events[1] == "delay"
    assert response.celery_task_id == dummy_result.id
