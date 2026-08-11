from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.sub2api.client import Sub2ApiApiError
from app.services.doubao_provider.client import DoubaoProviderError
from app.services.ai_video.task_state import reset_video_task_for_retry
from app.tasks.ai_video.video_tasks import (
    _provider_error_is_pool_backpressure,
    _defer_provider_pool_backpressure,
    _should_advance_route_immediately,
    _prepare_provider_cycle_after_cooldown,
    _next_provider_key,
    _provider_failure_is_pool_wide,
    _remember_attempted_provider,
    _provider_retry_wait_seconds,
    _provider_submit_lease_seconds,
    _remember_attempted_provider_key,
    _retry_countdown_for_provider_error,
    _provider_error_is_terminal_request_rejection,
    _provider_error_is_route_unavailable,
    _is_explicit_prompt_violation,
    VideoProviderRouteUnavailable,
)


def test_provider_backoff_honors_retry_after():
    error = Sub2ApiApiError(
        "Sub2API Flow HTTP 503",
        status_code=503,
        retry_after_seconds=75,
    )
    assert _retry_countdown_for_provider_error(error, 0) == 75


def test_provider_backoff_never_retries_local_503_inside_cooldown():
    error = Sub2ApiApiError("Sub2API Flow HTTP 503", status_code=503)
    assert _retry_countdown_for_provider_error(error, 0) == 60
    assert _retry_countdown_for_provider_error(error, 2) == 180


def test_provider_backoff_keeps_nontransient_fast():
    assert _retry_countdown_for_provider_error(RuntimeError("bad request"), 0) == 15


def test_local_route_incompatibility_is_deterministic_not_transient():
    error = VideoProviderRouteUnavailable(
        "Selected video provider does not support the requested model inputs"
    )

    assert _provider_error_is_route_unavailable(error) is True
    assert getattr(error, "code", None) == "video_provider_route_unavailable"


def test_local_route_incompatibility_advances_before_any_celery_retry():
    source = Path(
        "/opt/gmv/GMV-OPS/backend/app/tasks/ai_video/video_tasks.py"
    ).read_text(encoding="utf-8")
    worker = source[
        source.index("def submit_and_poll_ai_video_task")
        : source.index(
            "@celery_app.task(",
            source.index("def submit_and_poll_ai_video_task"),
        )
    ]

    assert worker.count("_provider_error_is_route_unavailable(exc)") == 2
    for handler in worker.split("except PROVIDER_TASK_ERRORS as exc:")[1:3]:
        route_unavailable = handler.index("_provider_error_is_route_unavailable(exc)")
        advance = handler.index(
            "task = _fail_provider_and_advance(db, task, exc)",
            route_unavailable,
        )
        payload = handler.index("return _payload(task)", advance)
        retry = handler.index("self.retry", advance)
        assert route_unavailable < advance < payload < retry


def test_flow_request_rejection_is_terminal_not_account_outage():
    error = Sub2ApiApiError(
        "Flow request rejected",
        status_code=502,
        upstream_status_code=400,
        code="flow_request_rejected",
        retryable=False,
    )

    assert _provider_error_is_terminal_request_rejection(error) is True

    task = SimpleNamespace(
        fail_code="flow_request_rejected",
        fail_msg="Flow request rejected",
    )
    assert _is_explicit_prompt_violation(task) is True


def test_provider_failover_is_republished_to_target_lane_before_network_io():
    source = Path(
        "/opt/gmv/GMV-OPS/backend/app/tasks/ai_video/video_tasks.py"
    ).read_text(encoding="utf-8")
    switch = source[
        source.index("def _switch_to_next_provider_in_place")
        : source.index("def _switch_to_kyy_in_place")
    ]

    assert "queue=production_video_queue(task)" in switch
    assert "_submit_current_provider(db, task)" not in switch
    assert "poll_owner_task_id=None" in switch


def test_rate_limit_recovery_wait_cannot_precede_route_circuit():
    error = RuntimeError("Doubao account is temporarily rate limited")

    assert _provider_retry_wait_seconds(
        error,
        retry_number=3,
        recovery_wait_seconds=240,
    ) == 305


def test_content_factory_advances_after_explicit_transient_http_response():
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        prompt=None,
    )

    assert _should_advance_route_immediately(
        task,
        Sub2ApiApiError("temporary", status_code=503),
    ) is True


def test_content_factory_keeps_ambiguous_transport_failure_idempotent():
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        prompt=None,
    )

    assert _should_advance_route_immediately(
        task,
        Sub2ApiApiError("transport error"),
    ) is False


def test_content_factory_advances_after_doubao_pool_fails_before_remote_submit():
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        prompt=None,
    )

    assert _should_advance_route_immediately(
        task,
        DoubaoProviderError(
            "video composer did not become ready",
            code="doubao_composer_unavailable",
        ),
    ) is True


def test_healthy_doubao_lane_contention_is_backpressure_not_route_failure():
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        prompt=None,
    )
    error = DoubaoProviderError(
        "healthy browser lane is occupied",
        code="doubao_pool_busy",
    )

    assert _provider_error_is_pool_backpressure(error) is True
    assert _should_advance_route_immediately(task, error) is False


def test_transient_doubao_pool_unavailable_is_pre_submit_backpressure():
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        prompt=None,
    )
    error = DoubaoProviderError(
        "account rows are temporarily unavailable",
        code="doubao_pool_unavailable",
    )

    assert _provider_error_is_pool_backpressure(error) is True
    assert _should_advance_route_immediately(task, error) is False


def test_doubao_lane_backpressure_requeues_without_failure_budget(monkeypatch):
    published = []

    class _Db:
        committed = False

        def add(self, _task):
            return None

        def commit(self):
            self.committed = True

    task = SimpleNamespace(
        id=77,
        workspace_id=3,
        task_id="local-ai-video-backpressure",
        state="submitting",
        fail_code="stale_failure",
        fail_msg="stale message",
        input_json={"service_provider": "doubao"},
        result_json={
            "__local": {
                "active_provider": "doubao",
                "provider_retry_counts": {"doubao": 4},
                "doubao_pool_backpressure_count": 2,
                "poll_owner_task_id": "old-owner",
            }
        },
        updated_at=None,
    )
    db = _Db()
    from app.tasks.ai_video import video_tasks

    monkeypatch.setattr(
        video_tasks.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    payload = _defer_provider_pool_backpressure(
        db,
        task,
        workspace_id=3,
        local_task_id=77,
        interval_seconds=15,
        timeout_seconds=600,
    )

    meta = task.result_json["__local"]
    assert db.committed is True
    assert payload["state"] == "queued_local"
    assert payload["fail_code"] is None
    assert meta["doubao_pool_backpressure_count"] == 3
    assert meta["provider_retry_counts"] == {"doubao": 4}
    assert "poll_owner_task_id" not in meta
    assert published[0]["countdown"] == 30
    assert published[0]["queue"] == "gmv.tasks.ai_video.browser"


def test_content_factory_advances_after_doubao_text_only_response():
    task = SimpleNamespace(
        input_json={"content_factory_project_key": "cf_test"},
        prompt=None,
    )

    assert _should_advance_route_immediately(
        task,
        DoubaoProviderError(
            "remote conversation returned text without a video model task",
            code="doubao_text_only_response",
        ),
    ) is True


def test_non_content_video_advances_after_explicit_transient_http_response():
    task = SimpleNamespace(input_json={}, prompt=None)

    assert _should_advance_route_immediately(
        task,
        Sub2ApiApiError("temporary", status_code=503),
    ) is True


def test_cooldown_reopens_only_transient_attempt_round():
    task = SimpleNamespace(
        result_json={
            "__local": {
                "attempted_provider_key_ids": [11, 3, 7],
                "attempted_provider_keys": ["sub2api"],
                "provider_quota_failed_key_ids": [3],
                "provider_cycle_count": 1,
            }
        }
    )

    assert _prepare_provider_cycle_after_cooldown(task) is True
    meta = task.result_json["__local"]
    assert meta["attempted_provider_key_ids"] == []
    assert meta["attempted_provider_keys"] == []
    assert meta["provider_quota_failed_key_ids"] == [3]
    assert meta["provider_cycle_count"] == 2


def test_cooldown_does_not_create_empty_provider_cycles():
    task = SimpleNamespace(result_json={"__local": {}})

    assert _prepare_provider_cycle_after_cooldown(task) is False


def test_failed_provider_is_fenced_once_for_the_current_route_round():
    task = SimpleNamespace(
        key_id=11,
        result_json={"__local": {"attempted_provider_key_ids": [3, 7]}},
    )

    assert _remember_attempted_provider_key(task) is True
    assert task.result_json["__local"]["attempted_provider_key_ids"] == [3, 7, 11]
    assert _remember_attempted_provider_key(task) is False
    assert task.result_json["__local"]["attempted_provider_key_ids"] == [3, 7, 11]


def test_self_hosted_account_pool_failure_fences_provider_for_current_round():
    task = SimpleNamespace(
        input_json={"service_provider": "sub2api"},
        result_json={"__local": {"active_provider": "sub2api"}},
        fail_code="sub2api_flow_error",
        fail_msg="Local Omni account pool is temporarily unavailable",
    )

    assert _provider_failure_is_pool_wide(task) is True
    assert _remember_attempted_provider(task, "sub2api") is True
    assert _remember_attempted_provider(task, "sub2api") is False
    assert task.result_json["__local"]["attempted_provider_keys"] == ["sub2api"]


def test_next_provider_skips_another_key_for_pool_wide_failed_provider(monkeypatch):
    task = SimpleNamespace(
        key_id=11,
        model="omni_flash",
        input_json={
            "reference_file_paths": [],
            "reference_video_file_paths": [],
            "aspect_ratio": "9:16",
            "video_frame_mode": "reference",
            "seconds": "8",
            "resolution": "720p",
        },
        result_json={
            "__local": {
                "attempted_provider_key_ids": [11],
                "attempted_provider_keys": ["sub2api"],
            }
        },
    )
    duplicate_route = SimpleNamespace(id=10, provider_key="sub2api")

    def _resolve(*_args, **kwargs):
        if 10 not in set(kwargs.get("exclude_key_ids") or []):
            return duplicate_route
        raise ValueError("No compatible route")

    monkeypatch.setattr(
        "app.tasks.ai_video.video_tasks.resolve_video_model_key",
        _resolve,
    )

    assert _next_provider_key(object(), task) is None


def test_manual_retry_starts_fresh_transient_route_round(monkeypatch):
    class _Db:
        def add(self, _task):
            return None

        def flush(self):
            return None

    monkeypatch.setattr("app.services.ai_video.task_state.log_event", lambda *_a, **_k: None)
    task = SimpleNamespace(
        id=55,
        workspace_id=3,
        key_id=10,
        model="omni_flash",
        input_json={"model": "omni_flash", "service_provider": "auto"},
        result_json={
            "__local": {
                "attempted_provider_key_ids": [11, 3, 7],
                "provider_quota_failed_key_ids": [3],
                "provider_retry_counts": {"sub2api": 3},
                "active_provider": "sub2api",
                "active_provider_key_id": 11,
                "video_provider_recovery": {"action": "WAIT_RETRY_SAME"},
                "doubao_account_bridge_id": "br_old",
                "doubao_remote_accepted_at": "2026-07-27T00:00:00+00:00",
                "doubao_remote_timeout_at": "2026-07-27T00:10:00+00:00",
                "doubao_failed_account_bridge_ids": ["br_failed"],
            }
        },
        fail_code="provider_error",
        fail_msg="temporary",
        prompt=None,
    )

    reset_video_task_for_retry(_Db(), task=task, retry_kind="manual")
    meta = task.result_json["__local"]
    assert meta["attempted_provider_key_ids"] == []
    assert meta["provider_quota_failed_key_ids"] == [3]
    assert meta["provider_retry_counts"] == {}
    assert "active_provider" not in meta
    assert "doubao_account_bridge_id" not in meta
    assert "doubao_remote_accepted_at" not in meta
    assert "doubao_remote_timeout_at" not in meta
    assert "doubao_failed_account_bridge_ids" not in meta
    assert meta["generation_epoch"] == 1
    assert meta["download_name_base"] == "55-g1"


def test_provider_submit_retry_preserves_current_poll_owner(monkeypatch):
    class _Db:
        def add(self, _task):
            return None

        def flush(self):
            return None

    monkeypatch.setattr("app.services.ai_video.task_state.log_event", lambda *_a, **_k: None)
    old_heartbeat = "2026-07-29T00:00:00+00:00"
    task = SimpleNamespace(
        id=56,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        input_json={"model": "seedance_2_0_mini", "service_provider": "doubao"},
        result_json={
            "__local": {
                "poll_owner_task_id": "current-delivery",
                "poll_heartbeat_at": old_heartbeat,
                "poll_heartbeat_provider": "doubao",
                "doubao_failed_account_bridge_ids": ["br_failed"],
            }
        },
        fail_code=None,
        fail_msg=None,
        prompt="park at dusk",
    )

    reset_video_task_for_retry(
        _Db(), task=task, retry_kind="provider_submit_unconfirmed"
    )

    meta = task.result_json["__local"]
    assert meta["poll_owner_task_id"] == "current-delivery"
    assert meta["poll_heartbeat_provider"] == "doubao"
    assert meta["poll_heartbeat_at"] != old_heartbeat
    assert meta["doubao_failed_account_bridge_ids"] == ["br_failed"]
    assert datetime.fromisoformat(meta["poll_heartbeat_at"]) <= datetime.now(timezone.utc)


def test_retry_normalizes_provider_specific_seedance_alias(monkeypatch):
    class _Db:
        def add(self, _task):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(
        "app.services.ai_video.task_state.log_event",
        lambda *_a, **_k: None,
    )
    task = SimpleNamespace(
        id=57,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        input_json={"model": "seedance_2_0_mini", "prompt": "test"},
        result_json={},
        fail_code="upstream",
        fail_msg="temporary",
        prompt="test",
    )

    reset_video_task_for_retry(
        _Db(),
        task=task,
        input_params={"model": "doubao-seedance-2-0-mini-260615"},
        retry_kind="manual",
    )

    assert task.model == "seedance_2_0_mini"
    assert task.input_json["model"] == "seedance_2_0_mini"


def test_provider_submit_owner_fence_covers_bounded_recovery_wait():
    assert _provider_submit_lease_seconds(15) >= 10 * 60
