from __future__ import annotations

import json
import base64
import inspect
import signal
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from PIL import Image

from app.services.hermes_agent import direct_browser
from app.services.hermes_agent.content_factory import _project_bridge_lock_terminal
from app.tasks.hermes_agent import content_factory_tasks


def test_interruption_probe_only_uses_safe_dismiss_controls(monkeypatch):
    captured: dict[str, str] = {}

    def fake_eval(expression: str, *, timeout: int = 30, isolated: bool = True):
        captured["expression"] = expression
        return {
            "present": True,
            "dismissed": True,
            "blocked": "",
            "text": "What's new in ChatGPT",
            "control": "Not now",
        }

    monkeypatch.setattr(direct_browser, "_eval_timeout", fake_eval)
    state = direct_browser._chatgpt_interruption_state()

    assert state["dismissed"] is True
    assert "not now" in captured["expression"].lower()
    assert "purchase" not in captured["expression"].lower()
    assert "subscribe" not in captured["expression"].lower()


@pytest.mark.parametrize(
    ("blocked", "expected"),
    [
        ("quota", "CHATGPT_QUOTA_LIMIT"),
        ("login", "CHATGPT_SESSION_LOGIN_REQUIRED"),
        ("upload_limit", "CHATGPT_UPLOAD_LIMIT"),
    ],
)
def test_blocking_interruption_is_not_clicked(monkeypatch, blocked: str, expected: str):
    monkeypatch.setattr(
        direct_browser,
        "_chatgpt_interruption_state",
        lambda: {
            "present": True,
            "dismissed": False,
            "blocked": blocked,
            "text": "blocking account state",
        },
    )

    with pytest.raises((RuntimeError, direct_browser.ChatGPTStageError), match=expected):
        direct_browser._dismiss_chatgpt_interruptions()


def test_anonymous_composer_does_not_mask_visible_login_control():
    source = inspect.getsource(direct_browser._page_state)

    assert "const loginRequired = loginControl ||" in source


def test_bridge_auth_probe_blocks_logged_out_slot_only():
    logged_out = SimpleNamespace(
        meta_json={"chatgpt_auth_status": "login_required"},
        load_json={},
    )
    ready = SimpleNamespace(
        meta_json={"chatgpt_auth_status": "ready"},
        load_json={},
    )
    legacy = SimpleNamespace(meta_json={}, load_json={})

    assert content_factory_tasks._bridge_login_blocked(logged_out) is True
    assert content_factory_tasks._bridge_login_blocked(ready) is False
    assert content_factory_tasks._bridge_login_blocked(legacy) is False


def test_harmless_overlays_are_drained_before_polling(monkeypatch):
    states = iter(
        [
            {"present": True, "dismissed": True, "blocked": "", "text": "New feature"},
            {"present": True, "dismissed": True, "blocked": "", "text": "Survey"},
            {"present": False, "dismissed": False, "blocked": "", "text": ""},
        ]
    )
    monkeypatch.setattr(direct_browser, "_chatgpt_interruption_state", lambda: next(states))
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    final = direct_browser._dismiss_chatgpt_interruptions(rounds=3)

    assert final["present"] is False


def test_chinese_temporary_rate_limit_is_classified_for_adaptive_backoff():
    error = RuntimeError("CHATGPT_TEMPORARY_RATE_LIMIT: 请求过于频繁，请稍等几分钟后重试")

    assert content_factory_tasks._is_chatgpt_quota_limit_error(str(error)) is True
    assert content_factory_tasks._rate_limit_kind(str(error)) == "temporary"


def test_zero_file_upload_capacity_is_classified_as_quota():
    error = RuntimeError("CHATGPT_UPLOAD_LIMIT: 无法上传图片。一次最多可上传 0 个文件")

    assert content_factory_tasks._is_chatgpt_rate_limit_error(str(error)) is True
    assert content_factory_tasks._rate_limit_kind(str(error)) == "quota"


def test_adaptive_rate_limit_wait_learns_from_recovery_samples():
    now = datetime(2026, 7, 12, 9, 0, 0)
    cold_wait = content_factory_tasks._adaptive_rate_limit_wait_seconds(
        {}, kind="temporary", episode_started_at=now, now=now,
        failed_probe_count=1, entropy_key="cold-device",
    )
    learned_wait = content_factory_tasks._adaptive_rate_limit_wait_seconds(
        {
            "recovery_samples_seconds": [1500, 1680, 1800, 2100],
            "ewma_recovery_seconds": 1770,
        },
        kind="temporary", episode_started_at=now, now=now,
        failed_probe_count=1, entropy_key="learned-device",
    )

    assert 300 <= cold_wait <= 600
    assert learned_wait >= 1500
    assert learned_wait != cold_wait


def test_browser_pacing_state_isolated_per_project(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_BROWSER_PACING_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(direct_browser, "CDP_URL", "http://127.0.0.1:9324")

    monkeypatch.setattr(direct_browser, "PACING_SCOPE", "cf_project_a")
    first = direct_browser._pacing_state_path()
    monkeypatch.setattr(direct_browser, "PACING_SCOPE", "cf_project_b")
    second = direct_browser._pacing_state_path()

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first != second


def test_failed_probe_raises_observed_cooldown_lower_bound():
    started = datetime(2026, 7, 12, 8, 0, 0)
    first_wait = content_factory_tasks._adaptive_rate_limit_wait_seconds(
        {"ewma_recovery_seconds": 900, "recovery_samples_seconds": [900]},
        kind="temporary", episode_started_at=started,
        now=started + timedelta(minutes=15), failed_probe_count=2,
        entropy_key="same-device",
    )
    later_wait = content_factory_tasks._adaptive_rate_limit_wait_seconds(
        {
            "ewma_recovery_seconds": 900,
            "recovery_samples_seconds": [900],
            "observed_lower_bound_seconds": 1800,
        },
        kind="temporary", episode_started_at=started,
        now=started + timedelta(minutes=30), failed_probe_count=3,
        entropy_key="same-device",
    )

    assert first_wait >= 90
    assert later_wait >= first_wait


def test_explicit_rate_limit_hint_caps_stale_multi_hour_learning():
    wait = content_factory_tasks._bounded_rate_limit_retry_delay(
        "CHATGPT_TEMPORARY_RATE_LIMIT: retry_after_seconds=316",
        14400,
        entropy_key="cf_project:VISUAL_PREVIEW:4",
    )

    assert 316 <= wait <= 355


def test_image_generation_quota_parses_combined_reset_duration():
    text = (
        "You've hit the Plus plan limit for image generations requests. "
        "You can create more images when the limit resets in 6 hours and 4 minutes."
    )

    assert direct_browser._quota_limit_marker(text) is True
    assert direct_browser._explicit_rate_limit_seconds(text) == 6 * 3600 + 4 * 60
    assert content_factory_tasks._is_chatgpt_rate_limit_error(text) is True
    assert content_factory_tasks._rate_limit_kind(text) == "quota"


def test_multi_hour_explicit_quota_is_not_truncated_to_four_hours():
    wait = content_factory_tasks._bounded_rate_limit_retry_delay(
        "CHATGPT_TEMPORARY_RATE_LIMIT: retry_after_seconds=21840",
        3600,
        entropy_key="cf_project:VISUAL_PREVIEW:image-quota",
    )

    assert 21840 <= wait <= 24570


def test_rate_limit_learning_remains_authoritative_without_explicit_hint():
    assert content_factory_tasks._bounded_rate_limit_retry_delay(
        "CHATGPT_TEMPORARY_RATE_LIMIT: please wait a few minutes",
        1770,
        entropy_key="cf_project:CREATIVE:2",
    ) == 1770


def test_blocking_creative_review_routes_to_visual_repair_instead_of_failing():
    envelope = {
        "status": "FAIL",
        "result": {
            "approved_for_split": False,
            "repair_brief": "Regenerate panels 4-6 with the real package.",
        },
        "issues": [{"severity": "blocking", "issue": "Wrong product package"}],
    }

    changed = content_factory_tasks._normalize_creative_review_repair_decision(envelope)

    assert changed is True
    assert envelope["status"] == "PASS"
    assert envelope["result"]["approved_for_split"] is False
    assert envelope["evidence"]["creative_review_requested_visual_repair"] is True
    assert envelope["evidence"]["rejected_visual_was_not_accepted"] is True


def test_visual_repair_stage_preserves_variant_and_forces_a_new_board(monkeypatch):
    latest = SimpleNamespace(
        attempt=4,
        status="success",
        error_message=None,
        input_json={
            "variant_index": 6,
            "variant_total": 20,
            "variant_mode": "serial_one_complete_video_at_a_time",
            "self_heal_count": 2,
        },
    )
    monkeypatch.setattr(content_factory_tasks, "_latest_stage", lambda *_args: latest)
    monkeypatch.setattr(content_factory_tasks, "_latest_variant_stage", lambda *_args: latest)
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda *_args: ("br_same", "http://127.0.0.1:9376", "queue"),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: None,
    )
    added = []
    db = SimpleNamespace(add=added.append, flush=lambda: None)
    project = SimpleNamespace(
        id=161,
        workspace_id=3,
        user_id=6,
        state_json={"video_variant_pipeline": {"active_index": 6, "target_count": 20}},
        config_json={"video_count": 20},
        current_stage="CREATIVE_REVIEW",
        status="failed",
        last_error="review failed",
    )

    stage = content_factory_tasks._create_repair_stage(
        db,
        project,
        "VISUAL_PREVIEW",
        reason="Creative review rejected the wrong product package.",
    )

    assert added == [stage]
    assert stage.input_json["variant_index"] == 6
    assert stage.input_json["variant_total"] == 20
    assert stage.input_json["force_fresh_response"] is True
    assert stage.input_json["allow_visible_visual_recovery"] is False
    assert stage.input_json["self_heal_count"] == 3


def test_variant_repair_never_inherits_the_next_variants_latest_stage(monkeypatch):
    active_v24 = SimpleNamespace(
        attempt=44,
        status="success",
        error_message=None,
        input_json={
            "variant_index": 24,
            "variant_total": 50,
            "variant_mode": "serial_one_complete_video_at_a_time",
            "self_heal_count": 3,
            "variant_marker": "v24-only",
        },
    )
    global_latest_v25 = SimpleNamespace(
        attempt=46,
        status="success",
        error_message=None,
        input_json={
            "variant_index": 25,
            "variant_total": 50,
            "variant_mode": "serial_one_complete_video_at_a_time",
            "self_heal_count": 9,
            "variant_marker": "v25-must-not-leak",
        },
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_stage",
        lambda *_args: global_latest_v25,
    )
    selected_variants = []

    def latest_variant(_db, _project, _stage_name, variant_index):
        selected_variants.append(variant_index)
        return active_v24

    monkeypatch.setattr(content_factory_tasks, "_latest_variant_stage", latest_variant)
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: "toapis:text",
    )
    added = []
    db = SimpleNamespace(add=added.append, flush=lambda: None)
    project = SimpleNamespace(
        id=168,
        workspace_id=3,
        user_id=6,
        state_json={
            "active_variant_index": 24,
            "video_variant_pipeline": {"active_index": 24, "target_count": 50},
        },
        config_json={"video_count": 50},
        current_stage="DIRECTOR",
        status="running",
        last_error=None,
    )

    stage = content_factory_tasks._create_repair_stage(
        db,
        project,
        "DIRECTOR",
        reason="repair active variant only",
    )

    assert selected_variants == [24]
    assert stage.attempt == 47
    assert stage.input_json["variant_index"] == 24
    assert stage.input_json["variant_total"] == 50
    assert stage.input_json["self_heal_count"] == 4
    assert "v25" not in str(stage.input_json)
    assert global_latest_v25.error_message is None
    assert active_v24.error_message.startswith("Superseded by self-heal")


def test_stage_delivery_scope_uses_project_first_lock_order():
    events = []
    stage = SimpleNamespace(id=1883, project_id=168)
    project = SimpleNamespace(id=168)

    class Query:
        def __init__(self, entity):
            self.entity = entity
            self.locked = False

        def filter(self, *_args):
            return self

        def with_for_update(self):
            self.locked = True
            return self

        def populate_existing(self):
            events.append(("refresh", self.entity))
            return self

        def one_or_none(self):
            if self.entity is content_factory_tasks.HermesContentFactoryStage.project_id:
                events.append(("identity", self.locked))
                return (168,)
            if self.entity is content_factory_tasks.HermesContentFactoryProject:
                events.append(("project", self.locked))
                return project
            if self.entity is content_factory_tasks.HermesContentFactoryStage:
                events.append(("stage", self.locked))
                return stage
            raise AssertionError(f"unexpected entity: {self.entity}")

    db = SimpleNamespace(query=lambda entity: Query(entity))

    locked_stage, locked_project, project_id = (
        content_factory_tasks._lock_stage_delivery_scope(db, 1883)
    )

    assert locked_stage is stage
    assert locked_project is project
    assert project_id == 168
    assert events == [
        ("identity", False),
        ("refresh", content_factory_tasks.HermesContentFactoryProject),
        ("project", True),
        ("refresh", content_factory_tasks.HermesContentFactoryStage),
        ("stage", True),
    ]


def test_stage_completion_relocks_after_durable_capture_before_validation():
    source = (
        content_factory_tasks.Path(content_factory_tasks.__file__)
        .read_text(encoding="utf-8")
    )
    worker = source[
        source.index("def run_content_factory_stage")
        : source.index("def release_content_factory_stage_retry")
    ]

    capture = worker.index("_persist_completed_stage_capture(")
    completion_lock = worker.index(
        "_lock_stage_delivery_scope(db, int(stage_row.id))",
        capture,
    )
    validation = worker.index(
        "envelope = _extract_envelope(",
        completion_lock,
    )
    success_write = worker.index(
        'stage_row.status = "success"',
        validation,
    )

    assert capture < completion_lock < validation < success_write


def test_stage_error_relocks_and_refreshes_before_honoring_manual_pause():
    source = (
        content_factory_tasks.Path(content_factory_tasks.__file__)
        .read_text(encoding="utf-8")
    )
    worker = source[
        source.index("def run_content_factory_stage")
        : source.index("def release_content_factory_stage_retry")
    ]

    rollback = worker.index("db.rollback()", worker.index("except Exception as exc:"))
    recovery_lock = worker.index(
        "stage_row, project, _ = _lock_stage_delivery_scope(",
        rollback,
    )
    manual_pause_guard = worker.index(
        'str(project.status or "").lower() == "paused"',
        recovery_lock,
    )

    assert rollback < recovery_lock < manual_pause_guard
    assert "stage_row = db.get(HermesContentFactoryStage" not in worker[
        rollback:manual_pause_guard
    ]


def test_mysql_deadlock_detection_is_specific_to_error_1213():
    deadlock = content_factory_tasks.OperationalError(
        "SELECT ... FOR UPDATE",
        {},
        Exception(1213, "Deadlock found when trying to get lock; try restarting transaction"),
    )
    lock_timeout = content_factory_tasks.OperationalError(
        "SELECT ... FOR UPDATE",
        {},
        Exception(1205, "Lock wait timeout exceeded"),
    )

    assert content_factory_tasks._is_mysql_deadlock_error(deadlock) is True
    assert content_factory_tasks._is_mysql_deadlock_error(lock_timeout) is False


def test_requeue_preserves_root_repair_contract(monkeypatch):
    stage = SimpleNamespace(
        stage="CREATIVE",
        status="queued",
        error_message=None,
        celery_task_id=None,
        started_at=None,
        completed_at=None,
        input_json={
            "execution_backend": "api",
            "api_route": "toapis:text",
            "self_heal_reason": (
                "Use exactly one fictional adult protagonist and no second person."
            ),
            "self_heal_count": 1,
        },
    )
    project = SimpleNamespace(
        current_stage="CREATIVE",
        status="queued",
        last_error=None,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args: "toapis:text",
    )

    content_factory_tasks._queue_existing_stage(
        SimpleNamespace(),
        project,
        stage,
        reason="periodic self-heal republished queued stage",
    )

    assert stage.input_json["self_heal_reason"].startswith(
        "Use exactly one fictional adult"
    )
    assert stage.input_json["root_self_heal_reason"] == stage.input_json["self_heal_reason"]
    assert stage.input_json["last_requeue_reason"] == (
        "periodic self-heal republished queued stage"
    )


def test_production_plan_retry_remains_api_first_without_browser_bridge(monkeypatch):
    project = SimpleNamespace(
        current_stage="PRODUCTION_PLAN",
        status="queued",
        last_error=(
            "Successor delivery is waiting for the preceding stage to release "
            "the project execution lock."
        ),
    )
    stage = SimpleNamespace(
        stage="PRODUCTION_PLAN",
        status="retrying",
        error_message=project.last_error,
        celery_task_id=None,
        started_at=None,
        completed_at=None,
        input_json={
            "execution_backend": "api",
            "api_route": "hermes:content-director",
            "retry_after": "2026-07-21T19:25:39",
        },
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("API-first production planning must not acquire a browser")
        ),
    )

    assert (
        content_factory_tasks._content_factory_api_route(
            SimpleNamespace(), "PRODUCTION_PLAN", stage.input_json
        )
        == "hermes:content-director"
    )
    content_factory_tasks._queue_existing_stage(
        SimpleNamespace(),
        project,
        stage,
        reason="periodic self-heal released due retry",
    )

    assert stage.status == "queued"
    assert stage.input_json["execution_backend"] == "api"
    assert stage.input_json["api_route"] == "hermes:content-director"
    assert "browser_bridge_id" not in stage.input_json


def test_rate_limit_learning_bootstraps_from_existing_stage_history():
    started = datetime(2026, 7, 12, 8, 0, 0)
    rows = [
        SimpleNamespace(
            input_json={"browser_bridge_id": "br_device_1"},
            error_message="CHATGPT_TEMPORARY_RATE_LIMIT: requests are too frequent",
            status="failed", completed_at=started, created_at=started,
        ),
        SimpleNamespace(
            input_json={"browser_bridge_id": "br_other"},
            error_message="", status="success",
            completed_at=started + timedelta(minutes=4), updated_at=started + timedelta(minutes=4),
        ),
        SimpleNamespace(
            input_json={"browser_bridge_id": "br_device_1"},
            error_message="", status="success",
            completed_at=started + timedelta(minutes=18), updated_at=started + timedelta(minutes=18),
        ),
    ]

    assert content_factory_tasks._historical_rate_limit_recovery_samples(
        rows, bridge_id="br_device_1"
    ) == [18 * 60]


def test_request_pacing_honors_account_reservation_and_prompt_review_time():
    now = datetime(2026, 7, 12, 10, 0, 0)
    reserved = direct_browser._request_pacing_delay_seconds(
        {
            "execution_id": "exec-pacing",
            "current_stage": "CREATIVE",
            "chatgpt_send_not_before": (now + timedelta(seconds=75)).isoformat(),
        },
        prompt_length=4000,
        attachment_count=0,
        now=now,
    )
    visual_review = direct_browser._request_pacing_delay_seconds(
        {"execution_id": "exec-visual", "current_stage": "VISUAL_PREVIEW"},
        prompt_length=12000,
        attachment_count=5,
        now=now,
    )

    assert reserved == pytest.approx(75, abs=0.1)
    assert visual_review >= 15


def test_account_request_gap_increases_after_learned_rate_limit():
    now = datetime(2026, 7, 12, 10, 0, 0)
    base_gap = content_factory_tasks._adaptive_chatgpt_request_gap_seconds(
        {}, stage="CREATIVE", attachment_count=0, now=now, entropy_key="base",
    )
    learned_gap = content_factory_tasks._adaptive_chatgpt_request_gap_seconds(
        {
            "chatgpt_request_pacing": {"pressure_score": 3},
            "chatgpt_rate_limit_learning": {
                "temporary": {
                    "ewma_recovery_seconds": 1800,
                    "last_recovered_at": (now - timedelta(hours=1)).isoformat(),
                }
            },
        },
        stage="CREATIVE", attachment_count=0, now=now, entropy_key="learned",
    )

    assert base_gap >= 30
    assert learned_gap >= 150
    assert learned_gap > base_gap


def test_rate_limited_project_releases_live_browser_lease():
    project = SimpleNamespace(
        status="waiting_bridge",
        config_json={"auto_run": True},
        state_json={"chatgpt_session": {"status": "temporarily_rate_limited"}},
    )

    assert _project_bridge_lock_terminal(project, active_stage=None) is True


def test_completed_answer_is_harvested_before_rate_limit_popup(monkeypatch):
    packet = {
        "project_id": "cf_popup_recovery",
        "current_stage": "CREATIVE",
        "execution_id": "exec-popup-1",
    }
    answer = json.dumps({
        "schema_version": "1.0",
        "execution_id": "exec-popup-1",
        "project_id": "cf_popup_recovery",
        "stage": "CREATIVE",
        "status": "PASS",
        "result": {"concepts": [{"id": "C1"}]},
        "evidence": {},
        "issues": [],
        "repair_brief": None,
        "next_stage": "VISUAL_PREVIEW",
    })
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda: {
            "busy": False,
            "count": 1,
            "messageTexts": [answer],
            "text": answer,
            "url": "https://chatgpt.com/c/test",
            "generatedImages": [],
        },
    )
    monkeypatch.setattr(direct_browser, "_raise_if_chatgpt_login_required", lambda _state: None)
    monkeypatch.setattr(
        direct_browser,
        "_dismiss_chatgpt_interruptions",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("popup must be checked after answer recovery")),
    )

    state = direct_browser._wait_for_answer(
        0,
        set(),
        timeout_seconds=2,
        minimum_images=0,
        packet=packet,
    )

    assert state["completedBehindPopup"] is True
    assert state["text"] == answer


def test_image_analysis_placeholder_waits_for_structured_answer(monkeypatch):
    packet = {
        "project_id": "cf_vision_wait",
        "current_stage": "CREATIVE_REVIEW",
        "execution_id": "exec-vision-wait",
    }
    answer = json.dumps({
        "schema_version": "1.0",
        "execution_id": "exec-vision-wait",
        "project_id": "cf_vision_wait",
        "stage": "CREATIVE_REVIEW",
        "status": "PASS",
        "result": {
            "creative_review": {},
            "approved_for_split": True,
            "reference_image_count": 4,
            "reference_checks": [],
        },
        "evidence": {},
        "issues": [],
        "repair_brief": None,
        "next_stage": "FINAL_ASSETS",
    })
    states = iter([
        {
            "busy": False,
            "count": 1,
            "messageTexts": ["正在分析 幅图片"],
            "text": "正在分析 幅图片",
            "url": "https://chatgpt.com/?temporary-chat=true",
            "generatedImages": [],
        },
        {
            "busy": False,
            "count": 1,
            "messageTexts": ["正在思考"],
            "text": "正在思考",
            "url": "https://chatgpt.com/?temporary-chat=true",
            "generatedImages": [],
        },
        {
            "busy": False,
            "count": 1,
            "messageTexts": [answer],
            "text": answer,
            "url": "https://chatgpt.com/?temporary-chat=true",
            "generatedImages": [],
        },
    ])
    monkeypatch.setattr(direct_browser, "_page_state", lambda: next(states))
    monkeypatch.setattr(direct_browser, "_dismiss_nonblocking_chatgpt_overlays", lambda: None)
    monkeypatch.setattr(direct_browser, "_raise_if_chatgpt_login_required", lambda _state: None)
    monkeypatch.setattr(direct_browser, "_raise_if_rate_limited", lambda _state: None)
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    state = direct_browser._wait_for_answer(
        0,
        set(),
        timeout_seconds=2,
        minimum_images=0,
        packet=packet,
    )

    assert state["text"] == answer
    assert direct_browser._looks_like_image_analysis_placeholder("Analyzing 5 images") is True
    assert direct_browser._looks_like_image_analysis_placeholder("正在分析 4 张图片") is True
    assert direct_browser._looks_like_response_processing_placeholder("正在思考") is True
    assert direct_browser._looks_like_response_processing_placeholder("Thinking...") is True


def test_due_rate_limit_probe_acknowledges_only_known_popup(monkeypatch):
    captured: dict[str, str] = {}

    def fake_eval(expression: str, *, timeout: int = 30, isolated: bool = True):
        captured["expression"] = expression
        return {"found": True, "clicked": True}

    monkeypatch.setattr(direct_browser, "_eval_timeout", fake_eval)

    assert direct_browser._acknowledge_rate_limit_popup() is True
    assert "请求" not in captured["expression"]  # Unicode escapes keep the browser script ASCII-safe.
    assert "got it" in captured["expression"].lower()
    assert "purchase" not in captured["expression"].lower()


def test_literal_newline_escapes_are_rendered_for_chatgpt():
    value = "Hook\\n\\n0-3s: conflict\\n3-10s: product"

    assert direct_browser._humanize_project_text(value) == (
        "Hook\n\n0-3s: conflict\n3-10s: product"
    )


def test_truncated_chatgpt_response_has_bounded_extended_retry_budget():
    error = RuntimeError("ChatGPT stage returned an incomplete or truncated text response")

    assert all(
        content_factory_tasks._stage_retry_delay(error, attempt) is not None
        for attempt in range(1, 7)
    )
    assert content_factory_tasks._stage_retry_delay(error, 7) is None


def test_recoverable_response_retry_starts_with_late_response_probe():
    plan = content_factory_tasks._recoverable_response_retry_plan({})

    assert plan is not None
    assert plan["force_fresh_response"] is False
    assert plan["browser_recovery_mode"] == "recover_existing_response_then_fresh_composer"
    assert plan["response_recovery_probe_count"] == 1
    assert plan["response_fresh_regeneration_count"] == 0


def test_invalid_recovered_response_permits_one_fresh_generation():
    plan = content_factory_tasks._recoverable_response_retry_plan({
        "browser_recovery_mode": "recover_existing_response_then_fresh_composer",
        "response_recovery_probe_count": 1,
    })

    assert plan is not None
    assert plan["force_fresh_response"] is True
    assert plan["clear_stale_composer_before_send"] is True
    assert plan["browser_recovery_mode"] == "fresh_composer_after_invalid_recovered_response"
    assert plan["response_fresh_regeneration_count"] == 1


@pytest.mark.parametrize(
    "stage_input",
    [
        {
            "browser_recovery_mode": "recover_existing_response_then_fresh_composer",
            "response_fresh_regeneration_count": 3,
        },
        {
            "browser_recovery_mode": "fresh_composer_after_invalid_recovered_response",
            "response_recovery_probe_count": 3,
        },
    ],
)
def test_recoverable_response_retry_cycles_are_bounded(stage_input):
    assert content_factory_tasks._recoverable_response_retry_plan(stage_input) is None


def test_visual_retry_starts_by_recovering_visible_media():
    plan = content_factory_tasks._recoverable_visual_retry_plan({})

    assert plan is not None
    assert plan["force_fresh_response"] is False
    assert plan["allow_visible_visual_recovery"] is True
    assert plan["browser_recovery_mode"] == "recover_visible_visual_from_project_tabs"
    assert plan["visual_recovery_probe_count"] == 1
    assert plan["visual_fresh_regeneration_count"] == 0


def test_missing_visible_media_permits_one_fresh_visual_generation():
    plan = content_factory_tasks._recoverable_visual_retry_plan({
        "browser_recovery_mode": "recover_visible_visual_from_project_tabs",
        "visual_recovery_probe_count": 1,
    })

    assert plan is not None
    assert plan["force_fresh_response"] is True
    assert plan["allow_visible_visual_recovery"] is False
    assert plan["clear_stale_composer_before_send"] is True
    assert plan["browser_recovery_mode"] == "fresh_visual_after_missing_visible_media"
    assert plan["visual_fresh_regeneration_count"] == 1


@pytest.mark.parametrize(
    "stage_input",
    [
        {
            "browser_recovery_mode": "recover_visible_visual_from_project_tabs",
            "visual_fresh_regeneration_count": 3,
        },
        {
            "browser_recovery_mode": "fresh_visual_after_missing_visible_media",
            "visual_recovery_probe_count": 3,
        },
    ],
)
def test_visual_retry_cycles_are_bounded(stage_input):
    assert content_factory_tasks._recoverable_visual_retry_plan(stage_input) is None


def test_recent_running_stage_keeps_single_project_publish_lease():
    now = datetime(2026, 7, 14, 3, 0, 0)
    stage = SimpleNamespace(
        status="running",
        input_json={"run_token": "owner"},
        celery_task_id="task-owner",
        started_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=3),
    )

    assert content_factory_tasks._stage_owns_publish_lease(stage, now=now) is True


def test_completion_fence_accepts_live_control_stage_behind_video_wait_pointer():
    assert content_factory_tasks._completion_pointer_allows_parallel_video_lane(
        project_stage="WAITING_VIDEO_INPUT",
        task_stage="DIRECTOR",
        task_has_live_lease=True,
        latest_same_stage_is_task=True,
        competing_stage_has_live_lease=False,
    ) is True


def test_completion_fence_rejects_real_stage_change_or_competing_lease():
    assert content_factory_tasks._completion_pointer_allows_parallel_video_lane(
        project_stage="PRODUCTION_PLAN",
        task_stage="DIRECTOR",
        task_has_live_lease=True,
        latest_same_stage_is_task=True,
        competing_stage_has_live_lease=False,
    ) is False
    assert content_factory_tasks._completion_pointer_allows_parallel_video_lane(
        project_stage="WAITING_VIDEO_INPUT",
        task_stage="DIRECTOR",
        task_has_live_lease=True,
        latest_same_stage_is_task=True,
        competing_stage_has_live_lease=True,
    ) is False


def test_completion_fence_db_wrapper_keeps_newest_live_director(monkeypatch):
    now = datetime.now()
    stage = SimpleNamespace(
        id=2930,
        project_id=184,
        stage="DIRECTOR",
        status="running",
        input_json={"execution_lease_expires_at": (
            now + timedelta(minutes=20)
        ).isoformat()},
        celery_task_id="director-delivery",
        started_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=6),
    )
    project = SimpleNamespace(
        id=184,
        current_stage="WAITING_VIDEO_INPUT",
    )

    class EmptyQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

    db = SimpleNamespace(query=lambda *_args: EmptyQuery())
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_stage",
        lambda *_args, **_kwargs: stage,
    )

    assert content_factory_tasks._stage_completion_pointer_is_authoritative(
        db,
        project,
        stage,
    ) is True


def test_series_director_execution_budget_scales_with_page_count(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks.settings,
        "HERMES_CONTENT_STAGE_SOFT_LIMIT_SECONDS",
        900,
    )
    monkeypatch.setattr(
        content_factory_tasks.settings,
        "HERMES_CONTENT_STAGE_HARD_LIMIT_GRACE_SECONDS",
        60,
    )
    monkeypatch.setattr(
        content_factory_tasks.settings,
        "HERMES_CONTENT_SERIES_BASE_BUDGET_SECONDS",
        300,
    )
    monkeypatch.setattr(
        content_factory_tasks.settings,
        "HERMES_CONTENT_MODEL_CALL_BUDGET_SECONDS",
        150,
    )
    project = SimpleNamespace(config_json={
        "video_count": 50,
        "director_series_brief": {"target_count": 50},
        "director_loop_policy": {"series_page_size": 10},
    })

    soft, hard = content_factory_tasks._content_stage_execution_limits(
        "SERIES_DIRECTOR",
        project,
    )

    assert soft == 2250
    assert hard == 2310


def test_stage_execution_budget_can_be_project_configured(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks.settings,
        "HERMES_CONTENT_MAX_STAGE_SOFT_LIMIT_SECONDS",
        7200,
    )
    project = SimpleNamespace(config_json={
        "stage_execution_budgets": {
            "SERIES_DIRECTOR": {
                "soft_seconds": 2700,
                "hard_seconds": 2800,
            },
        },
    })

    assert content_factory_tasks._content_stage_execution_limits(
        "SERIES_DIRECTOR",
        project,
    ) == (2700, 2800)


def test_task_fallback_limit_does_not_undercut_control_stage_budget():
    """Recovered broker deliveries may lose their per-message time limits."""

    project = SimpleNamespace(config_json={})
    control_soft, control_hard = (
        content_factory_tasks._content_stage_execution_limits(
            "PRODUCTION_PLAN",
            project,
        )
    )

    assert (
        content_factory_tasks.run_content_factory_stage.soft_time_limit
        >= control_soft
    )
    assert content_factory_tasks.run_content_factory_stage.time_limit >= control_hard


def test_explicit_execution_lease_matches_the_published_stage_budget():
    now = datetime(2026, 7, 21, 8, 0, 0)
    stage = SimpleNamespace(
        status="running",
        input_json={
            "execution_lease_expires_at": (
                now + timedelta(minutes=25)
            ).isoformat(),
        },
        celery_task_id="series-task",
        started_at=now - timedelta(minutes=20),
        created_at=now - timedelta(minutes=21),
    )

    assert content_factory_tasks._stage_owns_publish_lease(
        stage,
        now=now,
    ) is True
    assert content_factory_tasks._stage_owns_publish_lease(
        stage,
        now=now + timedelta(minutes=26),
    ) is False


def test_expired_running_stage_can_be_replaced_after_execution_lease():
    now = datetime(2026, 7, 14, 3, 0, 0)
    stage = SimpleNamespace(
        status="running",
        input_json={"run_token": "stale"},
        celery_task_id="task-stale",
        started_at=now - timedelta(minutes=16),
        created_at=now - timedelta(minutes=17),
    )

    assert content_factory_tasks._stage_owns_publish_lease(stage, now=now) is False


def test_scheduled_retry_keeps_publish_lease_even_after_old_start_time():
    now = datetime(2026, 7, 14, 3, 0, 0)
    stage = SimpleNamespace(
        status="retrying",
        input_json={"retry_after": (now + timedelta(minutes=5)).isoformat()},
        celery_task_id="retry-release-task",
        started_at=now - timedelta(hours=1),
        created_at=now - timedelta(hours=1),
    )

    assert content_factory_tasks._stage_owns_publish_lease(stage, now=now) is True


def test_browser_subprocess_is_killed_when_celery_soft_limit_arrives(monkeypatch):
    class FakeProcess:
        pid = 4321
        returncode = None

        def communicate(self, timeout=None):
            raise SoftTimeLimitExceeded()

        def kill(self):
            raise AssertionError("process-group kill should be attempted first")

    released = []
    killed = []
    monkeypatch.setattr(direct_browser, "_acquire_browser_lock", lambda session: object())
    monkeypatch.setattr(direct_browser, "_release_browser_lock", lambda lock: released.append(lock))
    monkeypatch.setattr(direct_browser.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(direct_browser.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(SoftTimeLimitExceeded):
        direct_browser._run("tab", "list", timeout=60)

    assert killed == [(4321, signal.SIGKILL)]
    assert len(released) == 1


def test_browser_command_rebuilds_stale_daemon_once_after_timeout(monkeypatch):
    class TimedOutProcess:
        pid = 4322
        returncode = None

        def communicate(self, timeout=None):
            raise direct_browser.subprocess.TimeoutExpired(cmd="agent-browser", timeout=timeout)

        def kill(self):
            return None

    class RecoveredProcess:
        pid = 4323
        returncode = 0

        def communicate(self, timeout=None):
            return '{"success":true,"data":{"tabs":[]}}', ""

    processes = iter((TimedOutProcess(), RecoveredProcess()))
    resets = []
    monkeypatch.setattr(direct_browser, "_acquire_browser_lock", lambda session: object())
    monkeypatch.setattr(direct_browser, "_release_browser_lock", lambda lock: None)
    monkeypatch.setattr(direct_browser.subprocess, "Popen", lambda *args, **kwargs: next(processes))
    monkeypatch.setattr(direct_browser.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(
        direct_browser, "_reset_agent_browser_daemon", lambda session: resets.append(session) or True,
    )

    payload = direct_browser._run("tab", "list", timeout=10)

    assert payload["success"] is True
    assert resets == [direct_browser._session_name()]


def test_browser_daemon_reset_enumerates_overwritten_session_pids(monkeypatch, tmp_path):
    session = "hermes-cdp-test"
    (tmp_path / f"{session}.pid").write_text("111", encoding="utf-8")
    terminated = []
    monkeypatch.setattr(direct_browser, "_agent_browser_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        direct_browser,
        "_agent_browser_daemon_pids",
        lambda target: [111, 222] if target == session else [],
    )
    monkeypatch.setattr(
        direct_browser,
        "_agent_browser_daemon_matches",
        lambda pid, target: pid == 111 and target == session,
    )
    monkeypatch.setattr(
        direct_browser,
        "_terminate_agent_browser_daemons",
        lambda pids: terminated.extend(sorted(set(pids))) or sorted(set(pids)),
    )

    assert direct_browser._reset_agent_browser_daemon(session) is True
    assert terminated == [111, 222]
    assert not (tmp_path / f"{session}.pid").exists()


def test_browser_stage_always_closes_local_daemon(monkeypatch):
    closed = []
    leases = []
    monkeypatch.setattr(
        direct_browser,
        "_execute_chatgpt_stage",
        lambda packet: ("ok", "https://chatgpt.com/c/1", []),
    )
    monkeypatch.setattr(
        direct_browser,
        "_acquire_browser_stage_lease",
        lambda session: leases.append(("acquired", session)) or object(),
    )
    monkeypatch.setattr(
        direct_browser,
        "_release_browser_lock",
        lambda _lease: leases.append(("released", None)),
    )
    monkeypatch.setattr(
        direct_browser,
        "close_agent_browser_session_best_effort",
        lambda: closed.append(True) or True,
    )

    assert direct_browser.execute_chatgpt_stage({"current_stage": "CREATIVE"})[0] == "ok"
    assert closed == [True]
    assert leases[0][0] == "acquired"
    assert leases[-1] == ("released", None)


def test_browser_stage_closes_local_daemon_after_failure(monkeypatch):
    closed = []
    released = []

    def fail(_packet):
        raise RuntimeError("stage failed")

    monkeypatch.setattr(direct_browser, "_execute_chatgpt_stage", fail)
    monkeypatch.setattr(
        direct_browser,
        "_acquire_browser_stage_lease",
        lambda _session: object(),
    )
    monkeypatch.setattr(
        direct_browser,
        "_release_browser_lock",
        lambda _lease: released.append(True),
    )
    monkeypatch.setattr(
        direct_browser,
        "close_agent_browser_session_best_effort",
        lambda: closed.append(True) or True,
    )

    with pytest.raises(RuntimeError, match="stage failed"):
        direct_browser.execute_chatgpt_stage({"current_stage": "CREATIVE"})
    assert closed == [True]
    assert released == [True]


def test_attachment_status_probes_use_short_remote_bridge_timeouts(monkeypatch):
    timeouts = []

    def fake_eval(_expression: str, *, timeout: int = 30, isolated: bool = True):
        timeouts.append(timeout)
        return {} if len(timeouts) == 1 else {"found": False, "sendable": False}

    monkeypatch.setattr(direct_browser, "_eval_timeout", fake_eval)

    direct_browser._attachment_upload_state()

    assert timeouts == [25, 20]


def test_upload_file_has_bounded_bridge_retries(monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append((args, kwargs))
        raise TimeoutError("bridge command timed out")

    monkeypatch.delenv("HERMES_CHATGPT_UPLOAD_RETRIES", raising=False)
    monkeypatch.setattr(direct_browser, "_prepare_chatgpt_upload_input", unavailable)
    monkeypatch.setattr(direct_browser, "_attachment_upload_state", lambda: {})
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="Could not upload"):
        direct_browser._upload_file(r"C:\\HermesInbox\\reference.png", expected_count=1)

    assert len(calls) == 3


def test_upload_file_opens_modern_chatgpt_upload_menu(monkeypatch):
    calls = []
    states = iter([
        {"attachmentCount": 0},
        {"attachmentCount": 1, "pending": False, "sendable": True},
        {"attachmentCount": 1, "pending": False, "sendable": True},
    ])

    monkeypatch.setattr(
        direct_browser,
        "_prepare_chatgpt_upload_input",
        lambda **_kwargs: 'input[data-testid="upload-photos-input"]',
    )
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(direct_browser, "_attachment_upload_state", lambda: next(states))
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    direct_browser._upload_file(r"C:\\HermesInbox\\reference.png", expected_count=1)

    assert calls == [(
        ("upload", 'input[data-testid="upload-photos-input"]', r"C:\\HermesInbox\\reference.png"),
        {"timeout": 90},
    )]


def test_upload_file_rebuilds_silent_input_without_reuploading_prior_files(monkeypatch):
    prepared = []
    uploaded = []
    direct_uploads = []
    states = iter([
        {"attachmentCount": 3},
        {"attachmentCount": 3, "pending": False, "sendable": True},
        {"attachmentCount": 3, "pending": False, "sendable": True},
        {"attachmentCount": 3},
        {"attachmentCount": 4, "pending": False, "sendable": True},
        {"attachmentCount": 4, "pending": False, "sendable": True},
    ])

    monkeypatch.setenv("HERMES_CHATGPT_UPLOAD_ACCEPT_SECONDS", "0")
    monkeypatch.setattr(
        direct_browser,
        "_prepare_chatgpt_upload_input",
        lambda **kwargs: prepared.append(bool(kwargs.get("force_refresh"))) or f"input-{len(prepared)}",
    )
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: uploaded.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(
        direct_browser,
        "_direct_cdp_upload_file",
        lambda selector, path: direct_uploads.append((selector, path)),
    )
    monkeypatch.setattr(direct_browser, "_attachment_upload_state", lambda: next(states))
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    direct_browser._upload_file(r"C:\\HermesInbox\\reference-4.png", expected_count=4)

    assert prepared == [False, True]
    assert [call[0][1] for call in uploaded] == ["input-1"]
    assert direct_uploads == [("input-2", r"C:\\HermesInbox\\reference-4.png")]
    assert all(call[0][2].endswith("reference-4.png") for call in uploaded)


def test_upload_file_stops_immediately_when_chatgpt_accepts_zero_files(monkeypatch):
    uploads = []
    states = iter([
        {"attachmentCount": 0},
        {
            "attachmentCount": 0,
            "pending": False,
            "sendable": True,
            "uploadLimited": True,
            "uploadLimitText": "无法上传 reference.png。一次最多可上传 0 个文件",
        },
    ])

    monkeypatch.setenv("HERMES_CHATGPT_UPLOAD_RETRIES", "3")
    monkeypatch.setattr(
        direct_browser,
        "_prepare_chatgpt_upload_input",
        lambda **_kwargs: 'input[data-testid="upload-photos-input"]',
    )
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: uploads.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(direct_browser, "_attachment_upload_state", lambda: next(states))

    with pytest.raises(direct_browser.ChatGPTStageError, match="CHATGPT_UPLOAD_LIMIT"):
        direct_browser._upload_file(r"C:\\HermesInbox\\reference.png", expected_count=1)

    assert len(uploads) == 1


def test_video_prompt_reference_sheet_preserves_every_ordered_panel(tmp_path):
    assets = []
    for index in range(1, 8):
        source = tmp_path / f"reference-{index}.png"
        color = (index * 25, 30, 255 - index * 20)
        Image.new("RGB", (180, 320), color).save(source)
        assets.append(SimpleNamespace(
            file_path=str(source),
            mime_type="image/png",
            original_name=source.name,
            kind="generated_image",
            stage="FINAL_ASSETS",
            meta_json={"reference_index": index, "semantic_roles": ["character_anchor"]},
        ))

    target = tmp_path / "reference-sheet.jpg"
    content_factory_tasks._render_video_prompt_reference_sheet(assets, target)

    assert target.is_file()
    with Image.open(target) as rendered:
        assert rendered.width > 1000
        assert rendered.height > 1800


def test_creative_review_browser_fallback_uses_one_indexed_reference_sheet():
    name, note = content_factory_tasks._browser_reference_sheet_spec(
        "CREATIVE_REVIEW",
        variant_index=24,
    )

    assert name == "v24-creative-review-reference-sheet.jpg"
    assert "visual_anchor" in note
    assert "exactly one reference_checks row" in note
    assert "no row for a product_anchor" in note
    assert "use product_anchor only to judge product accuracy" in note
    assert content_factory_tasks._browser_reference_sheet_spec(
        "VISUAL_PREVIEW",
        variant_index=24,
    ) is None


def test_visual_stage_leaves_temporary_chat_before_upload(monkeypatch):
    states = iter([
        {"url": "https://chatgpt.com/?temporary-chat=false", "text": ""},
    ])
    calls = []
    monkeypatch.setattr(direct_browser, "_page_state", lambda **_kwargs: next(states))
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(direct_browser, "_composer_ready", lambda _timeout=20: True)

    assert direct_browser._ensure_normal_chat_for_visual_stage() is True
    assert calls == []


def test_visual_stage_ignores_historical_temporary_refusal_in_normal_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda **_kwargs: {
            "url": "https://chatgpt.com/?temporary-chat=false",
            "text": "Image generation isn't available in this temporary chat.",
        },
    )
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )

    assert direct_browser._ensure_normal_chat_for_visual_stage() is True
    assert calls == []


def test_visual_stage_explicitly_disables_temporary_mode_from_plain_home(monkeypatch):
    states = iter([
        {"url": "https://chatgpt.com/", "text": ""},
        {"url": "https://chatgpt.com/?temporary-chat=false", "text": ""},
    ])
    calls = []
    monkeypatch.setattr(direct_browser, "_page_state", lambda **_kwargs: next(states))
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(direct_browser, "_composer_ready", lambda _timeout=20: True)

    assert direct_browser._ensure_normal_chat_for_visual_stage() is True
    assert calls[0][0] == ("open", "https://chatgpt.com/?temporary-chat=false")


def test_visual_stage_rejects_persistent_temporary_chat(monkeypatch):
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda **_kwargs: {
            "url": "https://chatgpt.com/?temporary-chat=true",
            "text": "Image generation isn't available in this temporary chat.",
        },
    )
    monkeypatch.setattr(direct_browser, "_run", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(direct_browser, "_composer_ready", lambda _timeout=20: True)

    with pytest.raises(RuntimeError, match="still trapped in Temporary Chat"):
        direct_browser._ensure_normal_chat_for_visual_stage()


def test_early_streaming_json_fragment_is_not_treated_as_final_response():
    assert direct_browser._looks_like_incomplete_stage_json(
        '{\n"schema_version":_',
        "cf_project",
        "VIDEO_PROMPTS",
    ) is True


def test_complete_stage_json_repairs_unescaped_dialogue_quotes():
    raw = (
        '{"schema_version":"1.0","project_id":"cf_project","stage":"CREATIVE",'
        '"status":"PASS","result":{"concepts":[{"dialogue":"She says, "Really?""}]},'
        '"next_stage":"VISUAL_PREVIEW"}'
    )

    repaired = direct_browser._complete_stage_json_text(raw)

    assert json.loads(repaired)["result"]["concepts"][0]["dialogue"] == 'She says, "Really?"'


def test_complete_stage_json_does_not_complete_streaming_fragment():
    assert direct_browser._complete_stage_json_text(
        '{"schema_version":"1.0","project_id":"cf_project","stage":"CREATIVE","result":{'
    ) == ""


def test_task_layer_recovers_complete_malformed_json_from_browser_error():
    raw = (
        '{"schema_version":"1.0","project_id":"cf_project","stage":"CREATIVE",'
        '"status":"PASS","result":{"concepts":[{"dialogue":"She says, "Really?""}]},'
        '"next_stage":"VISUAL_PREVIEW"}'
    )
    error = direct_browser.ChatGPTStageError(
        "ChatGPT stage returned an incomplete or truncated text response",
        raw_text=raw,
        chat_url="https://chatgpt.com/c/example",
    )

    recovered = content_factory_tasks._recover_completed_text_from_browser_error(
        error,
        "cf_project",
        "CREATIVE",
    )

    assert recovered == raw


def test_task_layer_does_not_recover_a_streaming_fragment_from_browser_error():
    error = direct_browser.ChatGPTStageError(
        "ChatGPT stage returned an incomplete or truncated text response",
        raw_text='{"schema_version":"1.0","project_id":"cf_project"',
    )

    assert content_factory_tasks._recover_completed_text_from_browser_error(
        error,
        "cf_project",
        "CREATIVE",
    ) is None


def test_task_layer_recovers_a_complete_persisted_stage_response():
    raw = (
        '{"schema_version":"1.0","project_id":"cf_project","stage":"CREATIVE",'
        '"status":"PASS","result":{"concepts":[{"dialogue":"She says, "Really?""}]},'
        '"next_stage":"VISUAL_PREVIEW"}'
    )

    assert content_factory_tasks._recover_completed_stage_response(
        raw,
        "cf_project",
        "CREATIVE",
    ) == raw


def test_streaming_fragment_cannot_overwrite_complete_persisted_response():
    persisted = (
        '{"schema_version":"1.0","project_id":"cf_project","stage":"CREATIVE",'
        '"status":"PASS","result":{"concepts":[{"dialogue":"She says, "Really?""}]},'
        '"next_stage":"VISUAL_PREVIEW"}'
    )

    selected = content_factory_tasks._prefer_chatgpt_response_snapshot(
        persisted,
        '{\n"',
        "cf_project",
        "CREATIVE",
    )

    assert selected == persisted


def test_self_heal_recovers_existing_response_before_resending(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda _db, _project, _stage_input: ("br_same", "http://127.0.0.1:9373", "queue"),
    )
    project = SimpleNamespace(
        current_stage="VIDEO_PROMPTS",
        status="failed",
        last_error="ChatGPT stage returned an incomplete or truncated text response",
    )
    stage = SimpleNamespace(
        stage="VIDEO_PROMPTS",
        status="failed",
        error_message="ChatGPT stage returned an incomplete or truncated text response",
        input_json={
            "automatic_retry_count": 4,
            "force_fresh_response": True,
            "response_recovery_probe_count": 3,
            "response_fresh_regeneration_count": 3,
        },
        celery_task_id="old-task",
        started_at=datetime.now(),
        completed_at=datetime.now(),
    )

    content_factory_tasks._queue_existing_stage(None, project, stage, reason="periodic self-heal")

    assert stage.input_json["automatic_retry_count"] == 0
    assert stage.input_json["force_fresh_response"] is False
    assert stage.input_json["browser_recovery_mode"] == "recover_existing_response_then_fresh_composer"
    assert "response_recovery_probe_count" not in stage.input_json
    assert "response_fresh_regeneration_count" not in stage.input_json
    assert stage.status == "queued"
    assert project.status == "queued"


def test_due_retry_preserves_bounded_response_recovery_sequence(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda _db, _project, _stage_input: (
            "br_same",
            "http://127.0.0.1:9373",
            "queue",
        ),
    )
    project = SimpleNamespace(
        current_stage="VISUAL_PREVIEW",
        status="queued",
        last_error="ChatGPT stage returned an incomplete or truncated text response",
    )
    stage = SimpleNamespace(
        stage="VISUAL_PREVIEW",
        status="retrying",
        error_message="ChatGPT stage returned an incomplete or truncated text response",
        input_json={
            "automatic_retry_count": 2,
            "force_fresh_response": True,
            "browser_recovery_mode": "fresh_composer_after_invalid_recovered_response",
            "clear_stale_composer_before_send": True,
            "response_recovery_probe_count": 1,
            "response_fresh_regeneration_count": 1,
        },
        celery_task_id=None,
        started_at=None,
        completed_at=None,
    )

    content_factory_tasks._queue_existing_stage(
        None,
        project,
        stage,
        reason="periodic self-heal released due retry",
    )

    assert stage.input_json["automatic_retry_count"] == 0
    assert stage.input_json["force_fresh_response"] is True
    assert stage.input_json["clear_stale_composer_before_send"] is True
    assert stage.input_json["browser_recovery_mode"] == (
        "fresh_composer_after_invalid_recovered_response"
    )
    assert stage.input_json["response_recovery_probe_count"] == 1
    assert stage.input_json["response_fresh_regeneration_count"] == 1


def test_formal_browser_requeue_resets_exhausted_delivery_budget(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda _db, _project, _stage_input: ("br_same", "http://127.0.0.1:9376", "queue"),
    )
    project = SimpleNamespace(
        current_stage="VISUAL_PREVIEW",
        status="failed",
        last_error="ChatGPT did not consume the prompt",
    )
    stage = SimpleNamespace(
        stage="VISUAL_PREVIEW",
        status="failed",
        error_message="ChatGPT did not consume the prompt",
        input_json={"automatic_retry_count": 5, "force_fresh_response": False},
        celery_task_id="old-task",
        started_at=datetime.now(),
        completed_at=datetime.now(),
    )

    content_factory_tasks._queue_existing_stage(
        None,
        project,
        stage,
        reason="browser composer recovery",
    )

    assert stage.input_json["automatic_retry_count"] == 0
    assert stage.input_json["force_fresh_response"] is True
    assert stage.input_json["browser_recovery_mode"] == "fresh_composer_after_prompt_stuck"
    assert stage.status == "queued"
    assert project.status == "queued"


def test_long_prompt_uses_real_keyboard_input_before_dom_fallback(monkeypatch):
    prompt = "x" * 12000
    calls: list[tuple] = []
    monkeypatch.setattr(direct_browser, "_clear_composer_strict", lambda: calls.append(("clear",)))
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: calls.append(tuple(args)),
    )
    monkeypatch.setattr(direct_browser, "_composer_text_length", lambda: len(prompt))
    monkeypatch.setattr(direct_browser, "_wake_composer_for_send", lambda _text: len(prompt))
    monkeypatch.setattr(direct_browser, "_attachment_upload_state", lambda: {"sendable": True})
    monkeypatch.setattr(
        direct_browser,
        "_set_composer_text_dom",
        lambda _text: pytest.fail("DOM fallback should not run after application-ready keyboard input"),
    )
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    direct_browser._fill_prompt_text(prompt)

    insert_calls = [call for call in calls if call[:2] == ("keyboard", "inserttext")]
    assert len(insert_calls) == 6
    assert "".join(call[2] for call in insert_calls) == prompt


def test_live_capacity_probe_preserves_late_quota_response(monkeypatch):
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda: {
            "rateLimited": True,
            "quotaLimited": True,
            "rateLimitText": "You can create more images when the limit resets in 5 hours and 2 minutes.",
            "url": "https://chatgpt.com/c/project-owned-chat",
        },
    )
    monkeypatch.setattr(direct_browser, "_dismiss_rate_limit_acknowledgement", lambda: False)
    monkeypatch.setattr(direct_browser, "_record_rate_limit", lambda _text: 18120)

    with pytest.raises(direct_browser.ChatGPTStageError, match="retry_after_seconds=18120") as error:
        direct_browser._raise_if_live_capacity_limited()

    assert error.value.chat_url == "https://chatgpt.com/c/project-owned-chat"
    assert "5 hours and 2 minutes" in error.value.raw_text


def test_live_capacity_probe_ignores_cdp_transport_failure(monkeypatch):
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda: (_ for _ in ()).throw(RuntimeError("CDP unavailable")),
    )

    direct_browser._raise_if_live_capacity_limited()


def test_quota_reset_duration_wins_over_generic_rate_limit_dialog(monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(direct_browser, "_dismiss_rate_limit_acknowledgement", lambda: True)

    def record(text: str) -> int:
        captured["text"] = text
        return direct_browser._explicit_rate_limit_seconds(text) or 0

    monkeypatch.setattr(direct_browser, "_record_rate_limit", record)
    state = {
        "rateLimited": True,
        "quotaLimited": True,
        "quotaLimitText": "You can create more images when the limit resets in 4 hours and 23 minutes.",
        "rateLimitDialogText": "Request too frequent. Please wait a few minutes.",
        "rateLimitText": "Request too frequent. Please wait a few minutes.",
    }

    with pytest.raises(direct_browser.ChatGPTStageError, match="retry_after_seconds=15780"):
        direct_browser._raise_if_rate_limited(state)

    assert captured["text"].startswith("You can create more images")


def test_structured_stage_packet_does_not_repeat_user_brief_as_json():
    packet = {
        "project_id": "cf_test",
        "brief": "Hook\\n\\n0-3s: conflict",
        "project_requirements": "ONLY $7.99",
        "user_instruction": "Keep the pacing fast",
        "current_stage": "CREATIVE",
        "browser_cdp_url": "http://127.0.0.1:9373",
        "browser_asset_paths": ["C:/private/file.png"],
        "project_state": {"celery_task_id": "internal"},
        "browser_assets": [{"id": 1, "name": "anchor.png"}],
        "project_assets": [{"id": 1, "name": "anchor.png"}],
    }

    structured = direct_browser._packet_for_prompt(packet)

    assert structured == {
        "project_id": "cf_test",
        "current_stage": "CREATIVE",
        "browser_assets": [{"id": 1, "name": "anchor.png"}],
    }


def test_page_state_classifies_generated_media_in_turn_based_chatgpt_dom(monkeypatch):
    captured: dict[str, str] = {}

    def fake_eval(expression: str, *, timeout: int = 30, isolated: bool = True):
        captured["expression"] = expression
        return {}

    monkeypatch.setattr(direct_browser, "_eval_timeout", fake_eval)

    direct_browser._page_state()

    expression = captured["expression"]
    assert "[data-turn=\"assistant\"]" in expression
    assert "turnAssistant" in expression
    assert "!(user || turnUser)" in expression


def test_pro_upgrade_toast_is_closed_as_a_nonblocking_overlay(monkeypatch):
    captured: dict[str, str] = {}

    def fake_eval(expression: str, *, timeout: int = 30, isolated: bool = True):
        captured["expression"] = expression
        return 1

    monkeypatch.setattr(direct_browser, "_eval", fake_eval)
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    assert direct_browser._dismiss_nonblocking_chatgpt_overlays() == 1
    expression = captured["expression"]
    assert "Get\\s*Pro" in expression
    assert "Upgrade(?:\\s+to)?\\s+Pro" in expression
    assert "close|dismiss" in expression


def test_visual_persistence_falls_back_to_authenticated_cdp_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(direct_browser, "_download_visual_from_page_chunks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        direct_browser,
        "_download_visual_via_cdp_resource",
        lambda _signature: {
            "ok": True,
            "type": "image/png",
            "source": "cdp_network_resource",
            "data": base64.b64encode(b"test-image-payload").decode("ascii"),
        },
    )
    monkeypatch.setattr(direct_browser.time, "sleep", lambda _seconds: None)

    saved = direct_browser._persist_visuals(tmp_path, "VISUAL_PREVIEW", ["https://chatgpt.com/image"], 1)

    assert len(saved) == 1
    assert (tmp_path / "visual_preview-1.png").read_bytes() == b"test-image-payload"
