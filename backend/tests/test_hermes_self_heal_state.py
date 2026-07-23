from datetime import datetime, timedelta
from types import SimpleNamespace

from app.tasks.hermes_agent import content_factory_tasks
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    RETIRED_CONTENT_FACTORY_STAGES,
    _finalize_exhausted_self_heal_stage,
    _finalize_variant_pipeline_state,
    _inherit_self_heal_circuit_history,
    _prepare_exhausted_self_heal_release,
    _pause_terminal_self_heal_stage,
    _retry_release_routing,
    _schedule_exhausted_self_heal_stage,
    _resume_stage_for_serial_variant,
    _stage_output_variant,
    _terminal_self_heal_error_code,
    _visual_api_provider_budget_exhausted,
    _visual_api_provider_retry_delay,
    _visual_grid_repair_instruction,
)


def test_self_heal_never_revives_removed_planning_stages():
    assert RETIRED_CONTENT_FACTORY_STAGES == {"CREATIVE", "MEDIA_DESIGN"}
    assert not RETIRED_CONTENT_FACTORY_STAGES.intersection(
        content_factory_tasks.VARIANT_STAGE_FLOW
    )


def test_exhausted_stage_finalizes_project_and_stage_together():
    project = HermesContentFactoryProject(
        project_key="cf_exhausted",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="running",
        current_stage="VIDEO_PROMPTS",
        config_json={"auto_run": True},
        state_json={},
        last_error=None,
    )
    stage = HermesContentFactoryStage(
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="VIDEO_PROMPTS",
        attempt=5,
        status="running",
        error_message="Automatic recovery limit reached",
    )

    message = _finalize_exhausted_self_heal_stage(project, stage)

    assert message == "Automatic recovery limit reached"
    assert project.status == "failed"
    assert stage.status == "failed"
    assert project.current_stage == stage.stage
    assert stage.celery_task_id is None


def test_terminal_contract_failure_enters_one_time_automatic_quality_pause():
    now = datetime(2026, 7, 23, 1, 0, 0)
    project = HermesContentFactoryProject(
        project_key="cf_terminal_contract",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="failed",
        current_stage="DIRECTOR",
        config_json={"auto_run": True},
        state_json={},
        last_error=(
            "DIRECTOR_RESUME_ANCESTOR_APPROVED: accepted copy cannot be "
            "silently rewritten"
        ),
    )
    stage = HermesContentFactoryStage(
        id=77,
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="DIRECTOR",
        attempt=2,
        status="failed",
        celery_task_id="delivery-to-revoke-by-owner",
        error_message=project.last_error,
        input_json={"self_heal_count": 1, "retry_after": "2026-07-23T01:01:00"},
    )

    code = _terminal_self_heal_error_code(stage.error_message)
    message = _pause_terminal_self_heal_stage(
        project,
        stage,
        error_code=code,
        now=now,
    )

    assert code == "DIRECTOR_RESUME_ANCESTOR_APPROVED"
    assert message.startswith(code)
    assert project.status == "paused"
    assert project.config_json["manual_paused"] is False
    assert project.state_json["pause_reason_code"] == "terminal_self_heal_contract_error"
    assert project.state_json["terminal_self_heal_error"]["stage_id"] == 77
    assert stage.status == "failed"
    assert stage.celery_task_id is None
    assert stage.input_json["self_heal_terminal"] is True
    assert "retry_after" not in stage.input_json


def test_semantic_contract_repair_error_is_not_misclassified_as_terminal():
    assert _terminal_self_heal_error_code(
        "critic contract error: repair invalid JSON and retry"
    ) is None


def test_completed_variant_pipeline_clears_stale_recovery_markers():
    state = {
        "ai_video_pending_task_ids": [103],
        "ai_video_failed_task_ids": [99],
        "ai_video_wait_task_id": "old-waiter",
        "ai_video_terminal_failure": "old provider error",
        "video_variant_pipeline": {
            "target_count": 3,
            "completed_indices": [1, 2],
            "failed_indices": [2, 3],
            "completion_blocked_missing_indices": [1, 2, 3],
            "awaiting_completed_variant_index": 3,
            "awaiting_completion_since": "2026-07-23T01:00:00",
            "last_completion_gate_requeue_at": "2026-07-23T01:10:00",
        },
    }

    finalized = _finalize_variant_pipeline_state(
        state,
        completed_indices=[1, 2, 3],
        target=3,
    )

    pipeline = finalized["video_variant_pipeline"]
    assert pipeline["completed_indices"] == [1, 2, 3]
    assert pipeline["failed_indices"] == []
    assert pipeline["completion_blocked_missing_indices"] == []
    assert "awaiting_completed_variant_index" not in pipeline
    assert "awaiting_completion_since" not in pipeline
    assert finalized["ai_video_pending_task_ids"] == []
    assert finalized["ai_video_wait_task_id"] is None
    assert "ai_video_terminal_failure" not in finalized


def test_exhausted_recoverable_stage_enters_durable_cooldown():
    now = datetime(2026, 7, 15, 18, 30, 0)
    project = HermesContentFactoryProject(
        project_key="cf_cooldown",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="failed",
        current_stage="VISUAL_PREVIEW",
        config_json={"auto_run": True},
        state_json={},
        last_error="provider overloaded",
    )
    stage = HermesContentFactoryStage(
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=13,
        status="failed",
        error_message="provider overloaded",
        input_json={"self_heal_count": 5, "visual_api": {"task_id": "stale"}},
    )

    retry_at = _schedule_exhausted_self_heal_stage(project, stage, now=now)

    assert retry_at == now + timedelta(minutes=5)
    assert stage.status == "retrying"
    assert stage.input_json["self_heal_circuit_open"] is True
    assert stage.input_json["retry_after"] == retry_at.isoformat()
    assert project.status == "queued"
    assert "will resume at" in project.last_error


def test_cooldown_release_resets_short_budget_and_stale_visual_delivery():
    now = datetime(2026, 7, 15, 18, 40, 0)
    stage = HermesContentFactoryStage(
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=13,
        status="retrying",
        input_json={
            "self_heal_count": 5,
            "self_heal_circuit_open": True,
            "retry_after": "2026-07-15T18:35:00",
            "visual_recovery_exhausted": True,
            "visual_grid_repair_count": 4,
            "visual_api": {"task_id": "stale"},
            "api_primary_error": {"message": "provider overloaded"},
        },
    )

    _prepare_exhausted_self_heal_release(stage, now=now)

    assert stage.input_json["self_heal_count"] == 0
    assert stage.input_json["self_heal_circuit_open"] is False
    assert stage.input_json["force_fresh_response"] is True
    assert stage.input_json["visual_grid_repair_count"] == 0
    assert "retry_after" not in stage.input_json
    assert "visual_api" not in stage.input_json
    assert "api_primary_error" not in stage.input_json


def test_five_panel_visual_repair_uses_explicit_three_plus_two_layout():
    instruction = _visual_grid_repair_instruction(None, None, expected_panel_count=5)

    assert "exactly three equal vertical 9:16 panels on top" in instruction
    assert "two centered vertical 9:16 panels below" in instruction
    assert "do not draw a sixth cell" in instruction
    assert "pure-white gutters" in instruction
    assert "scripted character/action keyframe explicitly needs it" in instruction
    assert "copy the attached user product reference exactly" in instruction
    assert "Never create a standalone product-only panel" in instruction
    assert "white-background packshot" in instruction
    assert "Keep hands empty" not in instruction


def test_repair_stage_inherits_circuit_backoff_history():
    target = {"self_heal_count": 3}

    result = _inherit_self_heal_circuit_history({
        "self_heal_daily_cooldown_cycle": 2,
        "self_heal_total_cooldown_cycles": 4,
        "self_heal_cooldown_day": "2026-07-15",
        "unrelated": "discarded",
    }, target)

    assert result is target
    assert result["self_heal_daily_cooldown_cycle"] == 2
    assert result["self_heal_total_cooldown_cycles"] == 4
    assert result["self_heal_cooldown_day"] == "2026-07-15"
    assert "unrelated" not in result


def test_stage_variant_falls_back_to_dispatch_input_when_error_output_has_no_evidence():
    stage = HermesContentFactoryStage(
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="CREATIVE",
        attempt=2,
        status="retrying",
        input_json={"variant_index": 5},
        output_json={
            "chatgpt_stage_error": {
                "type": "non_json_or_incomplete_response",
                "message": "temporary failure",
            }
        },
    )

    assert _stage_output_variant(stage) == 5


def test_stage_variant_prefers_success_evidence_over_dispatch_input():
    stage = HermesContentFactoryStage(
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="success",
        input_json={"variant_index": 4},
        output_json={"evidence": {"content_factory_variant_index": 6}},
    )

    assert _stage_output_variant(stage) == 6


def test_rejected_creative_review_resumes_visual_preview_not_final_assets(monkeypatch):
    rejected_review = SimpleNamespace(
        status="success",
        output_json={"result": {"approved_for_split": False}},
    )

    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_variant_stage",
        lambda _db, _project, stage, _variant: (
            rejected_review if stage == "CREATIVE_REVIEW" else None
        ),
    )

    assert _resume_stage_for_serial_variant(None, SimpleNamespace(), 11) == "VISUAL_PREVIEW"


def test_failed_visual_api_job_uses_its_own_retry_generation():
    stage_input = {"visual_api": {"provider_retry_generation": 1}}

    assert _visual_api_provider_retry_delay(
        "VISUAL_PREVIEW",
        stage_input,
        "Bandianwa image failed: task_failed adjust prompt",
    ) == 20

    assert _visual_api_provider_retry_delay(
        "VISUAL_PREVIEW",
        stage_input,
        "Bandianwa image transport error during POST /v1/images/generations: ReadTimeout",
    ) == 20

    stage_input["visual_api"]["provider_retry_generation"] = 2
    assert _visual_api_provider_retry_delay(
        "VISUAL_PREVIEW",
        stage_input,
        "Bandianwa image failed: task_failed adjust prompt",
    ) == 40


def test_failed_visual_api_job_stops_after_provider_budget():
    assert _visual_api_provider_retry_delay(
        "VISUAL_PREVIEW",
        {"visual_api": {"provider_retry_generation": 4}},
        "Bandianwa image failed: task_failed adjust prompt",
    ) is None
    assert _visual_api_provider_retry_delay(
        "CREATIVE",
        {"visual_api": {"provider_retry_generation": 1}},
        "Bandianwa image failed: task_failed adjust prompt",
    ) is None


def test_visual_api_budget_is_exhausted_only_after_bounded_generations():
    assert _visual_api_provider_budget_exhausted(
        "VISUAL_PREVIEW",
        {
            "visual_api": {
                "status": "failed",
                "task_id": None,
                "provider_retry_generation": 4,
            }
        },
    ) is True
    assert _visual_api_provider_budget_exhausted(
        "VISUAL_PREVIEW",
        {
            "visual_api": {
                "status": "failed",
                "task_id": None,
                "provider_retry_generation": 3,
            }
        },
    ) is False


def test_api_retry_release_does_not_acquire_a_browser_slot(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda _db, _stage, _stage_input=None: "bandianwa:gpt-image-2",
    )

    def unexpected_browser_lock(*_args, **_kwargs):
        raise AssertionError("API retry must not acquire a Chrome slot")

    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        unexpected_browser_lock,
    )
    route = _retry_release_routing(
        None,
        SimpleNamespace(),
        SimpleNamespace(stage="VISUAL_PREVIEW"),
        {"visual_api": {"provider_retry_generation": 1}},
    )

    assert route == (None, None, "gmv.tasks.hermes_agent")


def test_exhausted_visual_api_retry_requires_browser_fallback(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda _db, _stage, _stage_input=None: "bandianwa:gpt-image-2",
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda _db, _project, _input: ("slot-1", "http://127.0.0.1:9322", "slot.queue"),
    )
    route = _retry_release_routing(
        None,
        SimpleNamespace(),
        SimpleNamespace(stage="VISUAL_PREVIEW"),
        {
            "visual_api": {
                "status": "failed",
                "task_id": None,
                "provider_retry_generation": 4,
            }
        },
    )

    assert route == ("slot-1", "http://127.0.0.1:9322", "slot.queue")
