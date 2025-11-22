import types
import sys

# Stub whisper to avoid loading the heavy dependency when importing the task module.
_dummy_whisper = types.ModuleType("whisper")
_dummy_tokenizer = types.ModuleType("whisper.tokenizer")
_dummy_tokenizer.LANGUAGES = {"en": "English"}
_dummy_tokenizer.TO_LANGUAGE_CODE = {"english": "en"}
_dummy_whisper.tokenizer = _dummy_tokenizer
_dummy_whisper.load_model = lambda name="small": object()
sys.modules.setdefault("whisper", _dummy_whisper)
sys.modules.setdefault("whisper.tokenizer", _dummy_tokenizer)

_dummy_transformers = types.ModuleType("transformers")
_dummy_transformers.MarianMTModel = object
_dummy_transformers.MarianTokenizer = object


def _dummy_pipeline(task="translation", model=None, tokenizer=None):  # noqa: ANN001, ANN202
    def _translator(text, *args, **kwargs):  # noqa: ANN001, ANN202
        if isinstance(text, str):
            items = [text]
        else:
            items = list(text)
        return [{"translation_text": f"translated:{item}"} for item in items]

    return _translator


_dummy_transformers.pipeline = _dummy_pipeline
_dummy_pipelines = types.ModuleType("transformers.pipelines")
_dummy_pipelines.TranslationPipeline = object
sys.modules.setdefault("transformers", _dummy_transformers)
sys.modules.setdefault("transformers.pipelines", _dummy_pipelines)

_dummy_yt_dlp = types.ModuleType("yt_dlp")
_dummy_yt_dlp.YoutubeDL = None  # placeholder, patched per-test
sys.modules.setdefault("yt_dlp", _dummy_yt_dlp)

from app.features.tenants.openai_whisper import repository, storage, tasks  # noqa: E402


class _AuthErrorYDL:
    def __init__(self, *args, **kwargs):  # noqa: ANN002, D401 - test stub
        """Dummy yt-dlp client."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # noqa: ANN001, ANN202
        return False

    def extract_info(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("Log in for access. Use --cookies from browser")

    def download(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("Log in for access. Use --cookies from browser")


def test_transcribe_video_reports_auth_required(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    monkeypatch.setattr(tasks.storage, "BASE_DIR", tmp_path)
    monkeypatch.setattr(tasks, "YoutubeDL", _AuthErrorYDL)

    workspace_id = 1
    job_id = "job123"
    video_path = storage.job_dir(workspace_id, job_id) / "input.mp4"

    metadata = {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "user_id": 99,
        "status": "pending",
        "error": None,
        "translate": False,
        "show_bilingual": False,
        "filename": "share.mp4",
        "video_path": str(video_path),
        "share_url": "https://tiktok.example/video/123",
    }
    storage.write_metadata(workspace_id, job_id, metadata)

    with tasks.SessionLocal() as session:
        repository.create_job(session, metadata)
        session.commit()

    result = tasks.transcribe_video.run(workspace_id=workspace_id, job_id=job_id)
    assert result == job_id

    refreshed_meta = storage.load_metadata(workspace_id, job_id)
    assert refreshed_meta["status"] == "failed"
    assert "登录授权" in refreshed_meta["error"]

    with tasks.SessionLocal() as session:
        db_job = repository.get_job(session, workspace_id, job_id)
        assert db_job.status == "failed"
        assert "登录授权" in db_job.error
