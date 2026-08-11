from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.celery_app import (
    AI_VIDEO_API_TASK_QUEUE,
    AI_VIDEO_BROWSER_TASK_QUEUE,
    AI_VIDEO_BROWSER_POLL_TASK_QUEUE,
    AI_VIDEO_DOWNLOAD_TASK_QUEUE,
    AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    HERMES_AGENT_TASK_QUEUE,
    HERMES_MAINTENANCE_TASK_QUEUE,
    beat_schedule,
    celery_app,
)
from app.tasks.hermes_agent import content_factory_tasks
from app.tasks.ai_video import video_tasks
from app.tasks.hermes_agent.content_factory_tasks import (
    release_content_factory_stage_retry,
    self_heal_content_factory_projects,
)
from app.services.ai_video.queues import polling_video_queue, production_video_queue


def test_content_factory_tasks_are_isolated_on_hermes_queue() -> None:
    assert HERMES_AGENT_TASK_QUEUE == "gmv.tasks.hermes_agent"
    assert celery_app.conf.task_routes["hermes_content_factory.*"]["queue"] == (
        HERMES_AGENT_TASK_QUEUE
    )
    assert self_heal_content_factory_projects.queue == HERMES_AGENT_TASK_QUEUE
    assert release_content_factory_stage_retry.queue == HERMES_AGENT_TASK_QUEUE


def test_content_factory_self_heal_beat_entry_uses_hermes_queue() -> None:
    entry = beat_schedule["hermes_content_factory_self_heal"]
    assert entry["task"] == "hermes_content_factory.self_heal"
    assert entry["options"]["queue"] == HERMES_AGENT_TASK_QUEUE


def test_content_factory_self_heal_skips_overlapping_global_sweep(monkeypatch) -> None:
    lock_args = {}

    class BusySelfHealLock:
        def __init__(self, **kwargs):
            lock_args.update(kwargs)

        def acquire(self, *, timeout=0):
            assert timeout == 0
            return False

        def release(self):
            raise AssertionError("a delivery that did not acquire must not release")

    monkeypatch.setattr(
        content_factory_tasks,
        "RedisDistributedLock",
        BusySelfHealLock,
    )

    result = self_heal_content_factory_projects.run()

    assert result["status"] == "skipped_overlapping_self_heal"
    assert result["checked"] == 0
    assert lock_args["key"] == "gmv:content_factory:self_heal:v1"
    assert lock_args["ttl_seconds"] > lock_args["heartbeat_interval"]


def test_video_and_browser_maintenance_queues_are_semantically_isolated() -> None:
    assert len({
        AI_VIDEO_API_TASK_QUEUE,
        AI_VIDEO_BROWSER_TASK_QUEUE,
        AI_VIDEO_BROWSER_POLL_TASK_QUEUE,
        AI_VIDEO_DOWNLOAD_TASK_QUEUE,
        AI_VIDEO_MAINTENANCE_TASK_QUEUE,
        HERMES_AGENT_TASK_QUEUE,
        HERMES_MAINTENANCE_TASK_QUEUE,
    }) == 7
    assert beat_schedule["doubao_provider_auth_probe"]["options"]["queue"] == (
        AI_VIDEO_MAINTENANCE_TASK_QUEUE
    )
    assert beat_schedule["yt_dlp_cookie_keepalive_reconciliation"]["options"]["queue"] == (
        HERMES_MAINTENANCE_TASK_QUEUE
    )


def test_provider_task_routes_to_its_owned_production_lane() -> None:
    doubao = SimpleNamespace(
        input_json={"service_provider": "doubao"}, result_json={}
    )
    flow = SimpleNamespace(
        input_json={"service_provider": "sub2api"}, result_json={}
    )
    auto_doubao = SimpleNamespace(
        input_json={"service_provider": "auto"},
        result_json={"__local": {"active_provider": "doubao"}},
    )

    assert production_video_queue(doubao) == AI_VIDEO_BROWSER_TASK_QUEUE
    assert production_video_queue(auto_doubao) == AI_VIDEO_BROWSER_TASK_QUEUE
    assert production_video_queue(flow) == AI_VIDEO_API_TASK_QUEUE
    assert polling_video_queue(doubao) == AI_VIDEO_BROWSER_POLL_TASK_QUEUE
    assert polling_video_queue(flow) == AI_VIDEO_API_TASK_QUEUE


def test_poll_discovered_retry_is_republished_to_submit_lane(monkeypatch) -> None:
    task = SimpleNamespace(
        id=3583,
        workspace_id=3,
        state="failed",
        fail_code="doubao_text_only_response",
        fail_msg="text only",
        input_json={"service_provider": "doubao"},
        result_json={"__local": {"active_provider": "doubao"}},
        key_id=1,
        model="seedance_2_0_mini",
        task_id="doubao:old-conversation",
    )

    class FakeDb:
        def add(self, _row):
            return None

        def commit(self):
            return None

    dispatched: dict[str, object] = {}
    monkeypatch.setattr(video_tasks, "_load_task", lambda *_args, **_kwargs: task)
    monkeypatch.setattr(video_tasks, "delete_task_result_files", lambda *_args: None)

    def fake_reset(_db, *, task, retry_kind):
        assert retry_kind == "auto"
        task.state = "queued_local"
        task.task_id = "local-3583-retry"
        return task

    monkeypatch.setattr(video_tasks, "reset_video_task_for_retry", fake_reset)
    monkeypatch.setattr(
        video_tasks,
        "_submit_current_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("poll worker must not submit the paid retry")
        ),
    )
    monkeypatch.setattr(
        video_tasks.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: dispatched.update(kwargs),
    )

    result = video_tasks._auto_retry_in_place(FakeDb(), task)

    assert result.state == "queued_local"
    assert dispatched["queue"] == AI_VIDEO_BROWSER_TASK_QUEUE
    assert dispatched["kwargs"]["local_task_id"] == 3583


def test_dispatched_queue_recovery_is_shorter_than_execution_lease() -> None:
    """A deploy-lost broker message must recover in minutes, not 30 minutes."""
    assert 60 <= content_factory_tasks.QUEUED_DELIVERY_RECOVERY_SECONDS <= 300
    assert content_factory_tasks.QUEUED_DELIVERY_RECOVERY_SECONDS < (
        content_factory_tasks.STAGE_EXECUTION_LEASE_MINUTES * 60
    )


def test_self_heal_never_launches_browser_probe_for_api_stage(monkeypatch) -> None:
    now = datetime(2026, 7, 20, 13, 0, 0)
    stage = SimpleNamespace(
        status="running",
        stage="DIRECTOR",
        started_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
        created_at=now - timedelta(minutes=30),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: "toapis:text",
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_page_state",
        lambda: (_ for _ in ()).throw(
            AssertionError("API self-heal must not start agent-browser"),
        ),
    )

    assert content_factory_tasks._running_stage_browser_idle(
        object(),
        SimpleNamespace(),
        stage,
        {"execution_backend": "api"},
        now=now,
    ) == (False, None)


def test_self_heal_browser_probe_closes_local_daemon(monkeypatch) -> None:
    now = datetime(2026, 7, 20, 13, 0, 0)
    stage = SimpleNamespace(
        status="running",
        stage="VISUAL_PREVIEW",
        started_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
        created_at=now - timedelta(minutes=30),
    )
    closed = []
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_browser_bridge_fresh_for_stage",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_browser_cdp_reachable_for_stage",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_bridge_cdp_url_for_stage",
        lambda *_args: "http://127.0.0.1:9326",
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_page_state",
        lambda: {"url": "https://chatgpt.com/", "busy": False},
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_attachment_upload_state",
        lambda: {},
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "_composer_text_length",
        lambda: 0,
    )
    monkeypatch.setattr(
        content_factory_tasks.direct_browser_runtime,
        "close_agent_browser_session_best_effort",
        lambda session: closed.append(session) or True,
    )

    idle, reason = content_factory_tasks._running_stage_browser_idle(
        object(),
        SimpleNamespace(),
        stage,
        {"execution_backend": "browser"},
        now=now,
    )

    assert idle is True
    assert reason and "browser idle while stage running" in reason
    assert closed == ["hermes-cdp-http---127-0-0-1-9326"]


def test_video_wait_heartbeat_uses_stage_database_clock(monkeypatch) -> None:
    fixed_now = datetime(2026, 7, 20, 13, 40, 0)
    project = SimpleNamespace(id=38, state_json={})
    commits = []
    published = []
    monkeypatch.setattr(content_factory_tasks, "_stage_now", lambda: fixed_now)
    monkeypatch.setattr(
        content_factory_tasks.wait_for_content_factory_videos,
        "apply_async",
        lambda **kwargs: published.append(kwargs) or SimpleNamespace(id="wait-123"),
    )

    task_id = content_factory_tasks._schedule_video_wait(
        SimpleNamespace(commit=lambda: commits.append(True)),
        project,
        countdown=20,
        reason="test local-clock heartbeat",
    )

    assert task_id == "wait-123"
    assert project.state_json["ai_video_wait_heartbeat_at"] == fixed_now.isoformat()
    assert project.state_json["ai_video_wait_task_id"] == "wait-123"
    assert commits == [True]
    assert published[0]["queue"] == HERMES_AGENT_TASK_QUEUE


def test_terminal_failed_project_does_not_recreate_global_video_waiter() -> None:
    now = datetime(2026, 7, 20, 14, 0, 0)
    stale = now - timedelta(minutes=10)

    assert content_factory_tasks._should_recover_global_video_waiter(
        SimpleNamespace(status="failed"),
        {"ai_video_terminal_failure": "provider rejected segment"},
        [2353, 2354],
        stale,
        now=now,
    ) is False
    assert content_factory_tasks._should_recover_global_video_waiter(
        SimpleNamespace(status="running"),
        {},
        [2614, 2615],
        stale,
        now=now,
    ) is True


def test_video_waiter_uses_short_lease_in_waiting_video_input_recovery() -> None:
    now = datetime(2026, 7, 20, 14, 0, 0)

    assert content_factory_tasks._video_wait_heartbeat_is_stale(
        now - timedelta(seconds=46),
        now=now,
    ) is True
    assert content_factory_tasks._video_wait_heartbeat_is_stale(
        now - timedelta(seconds=44),
        now=now,
    ) is False


def test_spoken_copy_comparison_blocks_a_missing_product_fact() -> None:
    review = content_factory_tasks._spoken_copy_comparison(
        "Melatonin-free, blueberry, with L-Theanine, GABA, and Magnesium Glycinate.",
        "Melatonin-free with L-Theanine, GABA, and Magnesium Glycinate.",
    )

    assert review["status"] == "fail"
    assert review["missing_tokens"] == ["blueberry"]


def test_spoken_copy_comparison_keeps_project_words_as_review_evidence() -> None:
    review = content_factory_tasks._spoken_copy_comparison(
        "Take 2 MYUPONA gummies with L-Theanine and find MYUPONA on TikTok.",
        "Take two My Upon A gummies with Eltheanine and find Mayupona on Tick Tock.",
    )

    assert review["status"] == "fail"
    assert "myupona" in review["missing_tokens"]
    assert "l" in review["missing_tokens"]


def test_spoken_copy_comparison_treats_percent_symbol_as_spoken_percent() -> None:
    review = content_factory_tasks._spoken_copy_comparison(
        "One percent. Still scrolling?",
        "1% still scrolling",
    )

    assert review["status"] == "pass"
    assert review["missing_tokens"] == []


def test_spoken_copy_qa_uses_medium_adjudication_before_rejecting(monkeypatch) -> None:
    transcripts = {
        "small": "MYUPONA Sleep Easy Gum with Gabba.",
        "medium": "MYUPONA Sleep Easy Gummies with GABA.",
    }
    calls: list[str] = []

    def fake_transcribe(_path, *, model_name="small"):
        calls.append(model_name)
        return transcripts[model_name]

    monkeypatch.setattr(
        content_factory_tasks,
        "_transcribe_spoken_copy_external",
        fake_transcribe,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_segment_execution_contact_sheet",
        lambda _source, target: target.write_bytes(b"x" * 2048),
    )
    captured: dict[str, object] = {}

    def fake_semantic_review(_db, **kwargs):
        captured.update(kwargs)
        return {
            "status": "pass",
            "blocking": False,
            "semantic_fidelity": "exact",
            "policy_version": "test",
        }

    monkeypatch.setattr(
        content_factory_tasks,
        "review_spoken_copy_semantics_api",
        fake_semantic_review,
    )

    review = content_factory_tasks._review_spoken_copy_with_asr(
        object(),
        "MYUPONA Sleep Easy Gummies with GABA.",
        Path("/tmp/segment.mp4"),
        execution_id="test-copy",
    )

    assert calls == ["small", "medium"]
    assert review["status"] == "pass"
    assert review["asr_model"] == "medium"
    assert review["primary_missing_tokens"] == ["gummies", "gaba"]
    assert captured["adjudicated_transcript"] == (
        "MYUPONA Sleep Easy Gummies with GABA."
    )


def test_spoken_copy_token_mismatch_is_evidence_not_a_fixed_veto(monkeypatch) -> None:
    transcripts = {
        "small": "I am adding a bedtime dummy to my routine.",
        "medium": "I'm adding a bedtime dummy to my routine.",
    }

    monkeypatch.setattr(
        content_factory_tasks,
        "_transcribe_spoken_copy_external",
        lambda _path, *, model_name="small": transcripts[model_name],
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_segment_execution_contact_sheet",
        lambda _source, target: target.write_bytes(b"x" * 2048),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "review_spoken_copy_semantics_api",
        lambda _db, **_kwargs: {
            "status": "pass",
            "blocking": False,
            "semantic_fidelity": "meaning_preserved",
            "likely_asr_error": True,
            "policy_version": "test",
        },
    )

    review = content_factory_tasks._review_spoken_copy_with_asr(
        object(),
        "I'm adding a bedtime gummy to my routine.",
        Path("/tmp/segment.mp4"),
        execution_id="test-homophone",
    )

    assert review["status"] == "pass"
    assert review["blocking"] is False
    assert review["semantic_fidelity"] == "meaning_preserved"
    assert review["asr_diagnostics"]["adjudicated"]["status"] == "fail"


def test_local_voiceover_rows_are_not_rejected_for_provider_silence() -> None:
    params = {
        "content_factory_dialogue_lines": [{
            "line_id": "voice-1",
            "line": "Exact local narration.",
            "delivery_mode": "spoken",
            "delivery_method": "local_voiceover",
        }],
    }

    assert content_factory_tasks._uses_only_local_voiceover(params) is True
    assert content_factory_tasks._local_voice_name({
        "gender": "female",
        "accent": "US English",
    }) == "en-US-JennyNeural"


def test_spoken_copy_normalization_does_not_embed_brand_aliases() -> None:
    expected = content_factory_tasks._normalize_spoken_copy_tokens(
        "MYUPONA Sleep Easy Gummies"
    )

    assert content_factory_tasks._normalize_spoken_copy_tokens(
        "Myuponis Sleep Easy Gummies"
    ) != expected
    assert content_factory_tasks._normalize_spoken_copy_tokens(
        "My eupan Sleep Easy Gummies"
    ) != expected
    assert content_factory_tasks._normalize_spoken_copy_tokens(
        "Sleep Gummies"
    ) != expected


def test_spoken_copy_fused_product_name_remains_review_evidence() -> None:
    review = content_factory_tasks._spoken_copy_comparison(
        "MYUPONA Sleep Ease gummies are melatonin-free.",
        "Myupona Sleepies gummies are melatonin free.",
    )

    assert review["status"] == "fail"
    assert "ease" in review["missing_tokens"]


def test_spoken_copy_still_rejects_partial_product_name() -> None:
    review = content_factory_tasks._spoken_copy_comparison(
        "MYUPONA Sleep Ease gummies are melatonin-free.",
        "Myupona Sleep gummies are melatonin free.",
    )

    assert review["status"] == "fail"
    assert review["missing_tokens"] == ["ease"]


def test_terminal_video_tasks_still_recover_missing_local_composition() -> None:
    project = SimpleNamespace(status="generating_video")
    state = {
        "video_variant_pipeline": {
            "submitted_indices": [1, 2, 3, 4],
            "completed_indices": [1, 2, 3],
        },
    }

    assert content_factory_tasks._terminal_video_reconcile_needed(
        project,
        state,
        [2827, 2828, 2829, 2830, 2831, 2832],
    ) is True
    assert content_factory_tasks._should_recover_global_video_waiter(
        project,
        state,
        [],
        datetime(2026, 7, 20, 13, 0, 0),
        now=datetime(2026, 7, 20, 13, 2, 0),
        terminal_reconcile_needed=True,
    ) is True

    state["video_variant_pipeline"]["completed_indices"] = [1, 2, 3, 4]
    assert content_factory_tasks._terminal_video_reconcile_needed(
        project,
        state,
        [2827, 2828, 2829, 2830, 2831, 2832],
    ) is False


def test_completed_historical_video_ids_do_not_recreate_waiter() -> None:
    class FakeQuery:
        def filter(self, *_args):
            return self

        def all(self):
            return [(2618, "success"), (2619, "failed"), (2620, "in_progress")]

    fake_db = SimpleNamespace(query=lambda *_args: FakeQuery())
    project = SimpleNamespace(workspace_id=3, user_id=6)

    assert content_factory_tasks._active_video_task_ids_for_waiter(
        fake_db,
        project,
        [2618, 2619, 2620, 2621],
    ) == [2620, 2621]
