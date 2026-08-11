from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.bandianwa.client import (
    extract_error,
    extract_status,
    extract_task_id,
    extract_video_urls,
    normalize_submit_path,
)
from app.services.bandianwa.tasks import build_bandianwa_payload
from app.data.models.kie_api import KieFile, KieTask
from app.tasks.ai_video import video_tasks as video_tasks_module
from app.features.tenants.ai_video import router as router_videos_module
from app.services.ai_video.accounts import SUB2API_PROVIDER_KEY


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


def test_inline_refresh_is_read_only_while_celery_poller_owns_task() -> None:
    owned = SimpleNamespace(
        input_json={"service_provider": "bandianwa"},
        result_json={
            "__local": {
                "poll_owner_task_id": "celery-poll-owner",
                "poll_heartbeat_at": "2026-07-25T03:37:37+00:00",
            },
        },
    )
    unowned = SimpleNamespace(
        input_json={"service_provider": "bandianwa"},
        result_json={"__local": {}},
    )

    assert router_videos_module._supports_inline_refresh(owned) is False
    assert router_videos_module._supports_inline_refresh(unowned) is True


def test_ai_video_read_paths_include_sub2api_tasks() -> None:
    """Creation and read-side provider catalogs must stay symmetrical."""

    assert SUB2API_PROVIDER_KEY in router_videos_module.VIDEO_PROVIDER_KEYS


def test_sub2api_is_preserved_as_the_active_provider_before_submit_commit() -> None:
    """A rolled-back submit must still be attributed to its selected route."""

    task = SimpleNamespace(
        input_json={"service_provider": "sub2api"},
        # Reproduces metadata already polluted by the pre-fix rollback path.
        result_json={"__local": {"active_provider": "bandianwa"}},
    )

    assert video_tasks_module._service_provider(task) == SUB2API_PROVIDER_KEY
    assert video_tasks_module._active_provider(task) == SUB2API_PROVIDER_KEY
    assert router_videos_module._normalize_service_provider("sub2api") == SUB2API_PROVIDER_KEY


def test_auto_routing_intent_is_not_changed_into_a_pinned_provider() -> None:
    task = SimpleNamespace(
        input_json={
            "service_provider": "sub2api",
            "routing_mode": "auto",
            "requested_service_provider": "auto",
        },
        result_json={"__local": {"active_provider": "sub2api"}},
    )

    assert video_tasks_module._service_provider(task) == "auto"
    assert video_tasks_module._active_provider(task) == "sub2api"


def test_explicit_provider_routing_remains_pinned() -> None:
    task = SimpleNamespace(
        input_json={
            "service_provider": "bandianwa",
            "routing_mode": "pinned",
            "requested_service_provider": "sub2api",
        },
        result_json={"__local": {"active_provider": "bandianwa"}},
    )

    assert video_tasks_module._service_provider(task) == "sub2api"
    assert video_tasks_module._active_provider(task) == "sub2api"


def test_provider_switch_keeps_polling_the_replacement_provider() -> None:
    source = open(video_tasks_module.__file__, encoding="utf-8").read()
    worker = source[
        source.index("def submit_and_poll_ai_video_task"):
        source.index('@celery_app.task(', source.index("def submit_and_poll_ai_video_task"))
    ]
    initial_handler = worker[
        worker.index("except PROVIDER_TASK_ERRORS as exc:"):
        worker.index("while True:")
    ]
    poll_handler = worker[worker.index("while True:"):]

    assert "switched_state = str(task.state or \"\").lower()" in initial_handler
    assert "if switched_state in {\"failed\", \"error\", \"timeout\"}" in initial_handler
    switch_at = poll_handler.index("recovery_decision.action == VideoRecoveryAction.SWITCH_PROVIDER")
    continue_at = poll_handler.index("continue", switch_at)
    next_pause_at = poll_handler.index("VideoRecoveryAction.PAUSE_AUTH", switch_at)
    assert continue_at < next_pause_at


def test_task_status_explains_local_retry_and_active_provider() -> None:
    task = SimpleNamespace(
        input_json={
            "service_provider": "sub2api",
            "routing_mode": "auto",
            "requested_service_provider": "auto",
        },
        result_json={
            "__local": {
                "active_provider": "sub2api",
                "auto_retry_count": 1,
            },
        },
        state="queued_local",
        fail_msg=None,
    )

    router_videos_module._attach_batch_fields([task])

    assert task.routing_mode == "auto"
    assert task.current_provider == "sub2api"
    assert "自动重试或切换" in task.status_message


def test_task_status_explains_doubao_account_switch_without_exposing_failure() -> None:
    task = SimpleNamespace(
        input_json={"service_provider": "doubao", "routing_mode": "auto"},
        result_json={
            "__local": {
                "active_provider": "doubao",
                "doubao_submit_phase": "switching_account",
            },
        },
        state="submitting",
        fail_msg="doubao_captcha_required: private account detail",
    )

    router_videos_module._attach_batch_fields([task])

    assert task.current_provider == "doubao"
    assert task.status_message == "当前账号不可用，正在快速切换可用账号"
    assert "captcha" not in task.status_message.lower()


def test_task_status_explains_doubao_remote_generation_elapsed_time() -> None:
    accepted_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    task = SimpleNamespace(
        input_json={"service_provider": "doubao", "routing_mode": "auto"},
        result_json={
            "__local": {
                "active_provider": "doubao",
                "doubao_remote_accepted_at": accepted_at,
            },
        },
        state="queued",
        fail_msg=None,
    )

    router_videos_module._attach_batch_fields([task])

    assert "豆包已接收" in task.status_message
    assert "3 分钟" in task.status_message


def test_bandianwa_omni_text_to_video_uses_json_empty_reference_contract() -> None:
    payload = build_bandianwa_payload({
        "model": "omni_flash",
        "prompt": "An empty American park path at sunrise.",
        "seconds": "10",
        "aspect_ratio": "9:16",
        "reference_file_paths": [],
        "reference_video_file_paths": [],
        "content_factory_video_generation_mode": "text_to_video",
        "submit_path": "/v1/videos",
    })

    assert payload["input_reference"] == "[]"
    assert "reference_file_paths" not in payload
    assert "reference_video_file_paths" not in payload


def test_text_to_video_segment_never_waits_for_a_continuity_frame() -> None:
    task = SimpleNamespace(
        input_json={
            "content_factory_project_key": "cf_text_to_video",
            "content_factory_video_generation_mode": "text_to_video",
            "content_factory_segment_index": 7,
            "content_factory_first_frame": False,
            "model": "omni_flash",
        },
    )

    assert video_tasks_module._content_factory_dependency_pending(task) is False


def test_text_to_video_last_mile_deletes_stale_reference_rows(
    db_session,
    monkeypatch,
) -> None:
    import hashlib

    final_prompt = (
        "An empty park path at dawn. Segment scope: 1/1; show only this "
        "segment's actions."
    )
    task = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="local-text-to-video-fence",
        state="queued_local",
        input_json={
            "model": "omni_flash",
            "prompt": final_prompt,
            "content_factory_base_prompt": "An empty park path at dawn.",
            "content_factory_video_generation_mode": "text_to_video",
            "reference_file_paths": [{"path": "/tmp/stale.png"}],
            "reference_video_file_paths": [{"path": "/tmp/stale.mp4"}],
            "content_factory_provider_prompt_contract": {
                "validated": True,
                "actual_sha256": hashlib.sha256(
                    final_prompt.encode("utf-8")
                ).hexdigest(),
            },
        },
        result_json={},
    )
    db_session.add(task)
    db_session.flush()
    db_session.add_all([
        KieFile(
            workspace_id=3,
            key_id=1,
            task_id=task.id,
            kind="reference_upload",
            file_url="/tmp/stale.png",
        ),
        KieFile(
            workspace_id=3,
            key_id=1,
            task_id=task.id,
            kind="reference_video_upload",
            file_url="/tmp/stale.mp4",
        ),
    ])
    db_session.flush()

    key = SimpleNamespace(id=1, provider_key="bandianwa")
    monkeypatch.setattr(video_tasks_module, "_task_key", lambda *_args: key)

    async def fake_submit(_db, *, task):
        return task

    monkeypatch.setattr(
        video_tasks_module,
        "submit_bandianwa_task",
        fake_submit,
    )

    result = video_tasks_module._submit_current_provider(db_session, task)
    params = dict(result.input_json or {})
    remaining = db_session.query(KieFile).filter(
        KieFile.task_id == task.id,
        KieFile.kind.in_(("reference_upload", "reference_video_upload")),
    ).count()

    assert params["prompt"] == final_prompt
    assert (
        video_tasks_module._content_factory_provider_prompt_contract_error(
            result
        )
        is None
    )
    assert params["reference_file_paths"] == []
    assert params["reference_video_file_paths"] == []
    assert remaining == 0


def test_provider_quota_exception_is_detected_before_celery_retry() -> None:
    exc = RuntimeError(
        'HTTP 403: {"code":"insufficient_user_quota","message":"remaining balance is too low"}'
    )

    assert video_tasks_module._provider_error_is_quota_failure(exc) is True
    assert video_tasks_module._provider_error_is_quota_failure(RuntimeError("temporary gateway timeout")) is False


def test_content_factory_quota_failure_switches_by_api_priority(monkeypatch) -> None:
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        result_json={},
        fail_code="provider_quota_exhausted",
        fail_msg="insufficient quota",
    )
    replacement = SimpleNamespace(id=7, provider_key="toapis")
    monkeypatch.setattr(video_tasks_module, "_next_provider_key", lambda _db, _task: replacement)

    assert video_tasks_module._should_switch_provider(object(), task) is True


def test_content_factory_prompt_violation_does_not_switch_provider(monkeypatch) -> None:
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        result_json={},
        fail_code="prompt_violation",
        fail_msg="explicit content policy violation",
    )
    monkeypatch.setattr(
        video_tasks_module,
        "_next_provider_key",
        lambda _db, _task: SimpleNamespace(id=7, provider_key="toapis"),
    )

    assert video_tasks_module._should_switch_provider(object(), task) is False


def test_content_factory_non_quota_failure_advances_to_next_provider(monkeypatch) -> None:
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

    assert video_tasks_module._should_switch_provider(object(), task) is True


def test_exhausted_sub2api_transport_retries_advance_without_provider_counter(
    monkeypatch,
) -> None:
    """Celery retries and paid provider retries are separate counters."""

    task = SimpleNamespace(
        key_id=11,
        input_json={"service_provider": "sub2api"},
        result_json={"__local": {"provider_retry_counts": {}}},
        fail_code="provider_worker_error",
        fail_msg="Sub2API Flow HTTP 503: Service temporarily unavailable",
    )
    replacement = SimpleNamespace(id=3, provider_key="bandianwa")
    monkeypatch.setattr(video_tasks_module, "_next_provider_key", lambda _db, _task: replacement)

    assert video_tasks_module._provider_retry_count(task) == 0
    assert video_tasks_module._should_advance_exhausted_provider(object(), task) is True


def test_explicit_provider_route_replaces_an_existing_different_provider_key(
    monkeypatch,
) -> None:
    bandianwa = SimpleNamespace(
        id=3,
        provider_key="bandianwa",
        is_active=True,
    )
    toapis = SimpleNamespace(
        id=7,
        provider_key="toapis",
        is_active=True,
    )
    db = SimpleNamespace(get=lambda _model, _key_id: bandianwa)
    task = SimpleNamespace(
        key_id=3,
        model="omni_flash",
        input_json={
            "service_provider": "toapis",
            "reference_file_paths": [{"path": "a"}, {"path": "b"}, {"path": "c"}],
            "reference_video_file_paths": [],
            "aspect_ratio": "9:16",
            "reference_mode": "reference",
            "duration": 10,
            "resolution": "720p",
        },
    )
    monkeypatch.setattr(
        video_tasks_module,
        "get_effective_key",
        lambda *_args, **_kwargs: toapis,
    )

    def _resolve(*_args, **kwargs):
        assert kwargs["key_id"] == 7
        assert kwargs["reference_count"] == 3
        return toapis

    monkeypatch.setattr(video_tasks_module, "resolve_video_model_key", _resolve)

    selected = video_tasks_module._task_key(db, task)

    assert selected is toapis
    assert task.key_id == 7


def test_late_content_factory_delivery_cannot_revive_terminal_task() -> None:
    terminal = SimpleNamespace(
        state="failed",
        input_json={"content_factory_project_key": "cf_terminal"},
    )
    explicitly_reset = SimpleNamespace(
        state="queued_local",
        input_json={"content_factory_project_key": "cf_terminal"},
    )

    assert video_tasks_module._content_factory_terminal_delivery(terminal) is True
    assert (
        video_tasks_module._content_factory_terminal_delivery(explicitly_reset)
        is False
    )

    direct_terminal = SimpleNamespace(
        state="failed",
        input_json={"prompt": "direct task"},
    )
    assert (
        video_tasks_module._content_factory_terminal_delivery(direct_terminal)
        is True
    )
