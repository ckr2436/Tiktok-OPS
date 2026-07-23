from __future__ import annotations

from types import SimpleNamespace

from app.services.bandianwa.client import (
    extract_error,
    extract_status,
    extract_task_id,
    extract_video_urls,
    normalize_submit_path,
)
from app.tasks.bandianwa import video_tasks as video_tasks_module


def test_bandianwa_response_helpers_accept_documented_shapes() -> None:
    resp = {
        "data": {
            "task_id": "task-123",
            "status": "completed",
            "output": [{"url": "https://cdn.example.com/video.mp4"}],
        },
    }

    assert extract_task_id(resp) == "task-123"
    assert extract_status(resp) == "completed"
    assert extract_video_urls(resp) == ["https://cdn.example.com/video.mp4"]


def test_bandianwa_error_helper_extracts_nested_error() -> None:
    resp = {"error": {"code": "quota_exceeded", "message": "no credits"}}

    assert extract_error(resp) == ("quota_exceeded", "no credits")


def test_bandianwa_submit_path_normalization() -> None:
    assert normalize_submit_path("videos") == "/v1/videos"
    assert normalize_submit_path("/v1/videos") == "/v1/videos"
    assert normalize_submit_path("generate") == "/api/v1/generate"
    assert normalize_submit_path("/api/v1/generate") == "/api/v1/generate"


def test_provider_quota_exception_is_detected_before_celery_retry() -> None:
    exc = RuntimeError(
        'HTTP 403: {"code":"insufficient_user_quota","message":"remaining balance is too low"}'
    )

    assert video_tasks_module._provider_error_is_quota_failure(exc) is True
    assert video_tasks_module._provider_error_is_quota_failure(RuntimeError("temporary gateway timeout")) is False


def test_content_factory_quota_failure_can_switch_to_a_compatible_provider(monkeypatch) -> None:
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        result_json={},
        fail_code="provider_quota_exhausted",
        fail_msg="insufficient quota",
    )
    replacement = SimpleNamespace(id=7, provider_key="toapis")
    monkeypatch.setattr(video_tasks_module, "_next_provider_key", lambda _db, _task: replacement)

    assert video_tasks_module._should_switch_provider(object(), task) is True


def test_content_factory_non_quota_failure_stays_owned_by_project_recovery(monkeypatch) -> None:
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        result_json={},
        fail_code="generation_failed",
        fail_msg="provider rejected prompt",
    )
    monkeypatch.setattr(
        video_tasks_module,
        "_next_provider_key",
        lambda _db, _task: SimpleNamespace(id=7, provider_key="toapis"),
    )

    assert video_tasks_module._should_switch_provider(object(), task) is False
