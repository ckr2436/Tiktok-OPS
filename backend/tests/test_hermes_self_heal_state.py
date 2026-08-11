from datetime import datetime, timedelta
import os
from types import SimpleNamespace

from app.tasks.hermes_agent import content_factory_tasks
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.data.models.kie_api import KieTask
from app.tasks.hermes_agent.content_factory_tasks import (
    _finalize_variant_pipeline_state,
    _inherit_self_heal_circuit_history,
    _prepare_exhausted_self_heal_release,
    _pause_terminal_self_heal_stage,
    _pause_external_input_stage,
    _reset_production_plan_after_empty_reference_contract,
    _reset_production_plan_after_invalid_media_contract,
    _reconcile_invalid_media_contract_task_markers,
    _retry_release_routing,
    _schedule_exhausted_self_heal_stage,
    _resume_stage_for_serial_variant,
    _stage_output_variant,
    _terminal_self_heal_error_code,
    _upgrade_self_heal_policy_generation,
    _visual_api_provider_budget_exhausted,
    _visual_api_provider_retry_delay,
    _visual_grid_repair_instruction,
)


def test_self_heal_never_revives_removed_planning_stages():
    # The retired-stage compatibility constant and its recovery branch were
    # deliberately removed with the old pipeline.  A regression must not
    # reintroduce either name as an executable stage or restore a legacy
    # compatibility switch just to interpret stale rows.
    assert not hasattr(content_factory_tasks, "RETIRED_CONTENT_FACTORY_STAGES")
    assert {"CREATIVE", "MEDIA_DESIGN"}.isdisjoint(
        content_factory_tasks.VARIANT_STAGE_FLOW
    )


def test_due_database_retry_ignores_stale_execution_lease():
    now = datetime(2026, 8, 5, 6, 42, 0)
    stage = SimpleNamespace(
        status="retrying",
        input_json={
            "retry_after": "2026-08-05T06:40:42",
            "execution_lease_expires_at": "2026-08-05T07:01:21",
        },
        started_at=now,
        created_at=now,
        celery_task_id="finished-delivery",
    )

    assert content_factory_tasks._stage_owns_publish_lease(
        stage,
        now=now,
    ) is False


def test_self_heal_follows_publish_suppression_owner_instead_of_newer_row(
    monkeypatch,
):
    now = datetime(2026, 8, 9, 9, 30, 0)
    project = SimpleNamespace(id=187)
    owner = HermesContentFactoryStage(
        id=3288,
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=18,
        status="retrying",
        input_json={"retry_after": "2026-08-09T09:56:58"},
    )
    duplicate = HermesContentFactoryStage(
        id=3290,
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=19,
        status="failed",
        error_message=(
            "Duplicate publish suppressed: active stage 3288 "
            "(VISUAL_PREVIEW) still owns the project execution lease."
        ),
        completed_at=now,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_stage",
        lambda *_args, **_kwargs: duplicate,
    )
    db = SimpleNamespace(
        get=lambda _model, row_id: owner if row_id == 3288 else None,
        add=lambda _row: None,
    )

    selected = content_factory_tasks._authoritative_self_heal_stage(
        db,
        project,
        "VISUAL_PREVIEW",
        now=now,
    )

    assert selected is owner
    assert owner.status == "retrying"


def test_self_heal_repairs_reciprocal_duplicate_publish_tombstone(monkeypatch):
    now = datetime(2026, 8, 9, 9, 32, 0)
    project = SimpleNamespace(id=187)
    owner = HermesContentFactoryStage(
        id=3288,
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=18,
        status="failed",
        input_json={
            "retry_after": "2026-08-09T09:31:00",
            "last_recovery_supervisor_decision": {
                "diagnosis": "Project-scoped image upload timed out."
            },
        },
        error_message=(
            "Superseded by newer VISUAL_PREVIEW stage 3290; "
            "old broker delivery ignored before touching the browser."
        ),
        completed_at=None,
    )
    duplicate = HermesContentFactoryStage(
        id=3290,
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=19,
        status="failed",
        error_message=(
            "Duplicate publish suppressed: active stage 3288 "
            "(VISUAL_PREVIEW) still owns the project execution lease."
        ),
        completed_at=now,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_stage",
        lambda *_args, **_kwargs: duplicate,
    )
    added = []
    db = SimpleNamespace(
        get=lambda _model, row_id: owner if row_id == 3288 else None,
        add=added.append,
    )

    selected = content_factory_tasks._authoritative_self_heal_stage(
        db,
        project,
        "VISUAL_PREVIEW",
        now=now,
    )

    assert selected is owner
    assert owner.status == "retrying"
    assert owner.error_message == "Project-scoped image upload timed out."
    assert owner.input_json["duplicate_publish_race_duplicate_stage_id"] == 3290
    assert owner in added


def test_latest_stage_excludes_rejected_duplicate_publish_audit_row(db_session):
    project = SimpleNamespace(id=187)
    owner = HermesContentFactoryStage(
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=18,
        status="retrying",
        input_json={"retry_after": "2026-08-09T09:56:58"},
    )
    db_session.add(owner)
    db_session.flush()
    duplicate = HermesContentFactoryStage(
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=19,
        status="failed",
        error_message=(
            f"Duplicate publish suppressed: active stage {int(owner.id)} "
            "(VISUAL_PREVIEW) still owns the project execution lease."
        ),
        completed_at=datetime(2026, 8, 9, 9, 30, 0),
    )
    db_session.add(duplicate)
    db_session.flush()

    selected = content_factory_tasks._latest_stage(
        db_session,
        project,
        "VISUAL_PREVIEW",
    )

    assert selected is not None
    assert int(selected.id) == int(owner.id)


def test_attempt_allocator_counts_rejected_duplicate_publish_audit_row(
    db_session,
):
    owner = HermesContentFactoryStage(
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=18,
        status="success",
    )
    duplicate = HermesContentFactoryStage(
        project_id=187,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=19,
        status="failed",
        error_message=(
            "Duplicate publish suppressed: active stage 3288 "
            "(VISUAL_PREVIEW) still owns the project execution lease."
        ),
    )
    db_session.add_all([owner, duplicate])
    db_session.flush()

    assert content_factory_tasks._next_stage_attempt(
        db_session,
        project_id=187,
        stage_name="VISUAL_PREVIEW",
    ) == 20


def test_visual_provider_inheritance_does_not_poison_failure_ledger():
    original = {
        "api_route": "toapis:gpt-image-2",
        "visual_api": {
            "provider": "toapis",
            "provider_failures": {
                "bandianwa": {"error": "insufficient_user_quota"},
            },
            "boards": {
                "1": {"status": "failed", "task_id": "old-task"},
            },
        },
    }

    inherited = content_factory_tasks._prepare_visual_api_provider_inheritance(
        original,
        to_provider="sub2api",
    )

    assert inherited["api_route"] == "sub2api:gpt-image-2"
    assert inherited["visual_api"]["provider_failures"] == {
        "bandianwa": {"error": "insufficient_user_quota"},
    }
    assert inherited["visual_api"]["boards"]["1"]["task_id"] is None
    assert original["visual_api"]["boards"]["1"]["task_id"] == "old-task"


def test_synthetic_route_inheritance_is_not_a_real_provider_failure():
    failed = content_factory_tasks._visual_api_failed_providers({
        "visual_api": {
            "provider_failures": {
                "sub2api": {"error": "No available compatible accounts"},
                "toapis": {
                    "error": (
                        "Same visual variant already owns another API route; "
                        "inherit that provider before submitting the new concept."
                    )
                },
            }
        }
    })

    assert failed == {"sub2api"}


def _failed_video_task(
    task_id: int,
    *,
    provider: str = "bandianwa",
    fail_msg: str = "INTERNAL",
) -> KieTask:
    return KieTask(
        id=task_id,
        workspace_id=3,
        key_id=9,
        created_by_user_id=101,
        model="omni_flash",
        task_id=f"local-{task_id}",
        state="failed",
        input_json={"service_provider": provider},
        result_json={"__local": {}},
        fail_msg=fail_msg,
    )


def test_exhausted_video_retry_waits_then_allows_one_bandianwa_recovery():
    task = _failed_video_task(71)
    task.updated_at = datetime(2026, 7, 23, 1, 0, 0)

    assert content_factory_tasks._should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="1:7",
        effective_count=2,
        retry_limit=2,
        recovery_generations={},
        now=datetime(2026, 7, 23, 1, 2, 59),
        cooldown_seconds=180,
    ) is False
    assert content_factory_tasks._should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="1:7",
        effective_count=2,
        retry_limit=2,
        recovery_generations={},
        now=datetime(2026, 7, 23, 1, 3, 0),
        cooldown_seconds=180,
    ) is True
    assert content_factory_tasks._should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="1:7",
        effective_count=2,
        retry_limit=2,
        recovery_generations={"1:7": {"provider": "bandianwa", "round": 5}},
        now=datetime(2026, 7, 23, 2, 0, 0),
        cooldown_seconds=180,
    ) is False


def test_explicit_prompt_violation_is_not_retried():
    task = _failed_video_task(
        72,
        fail_msg="The prompt violates the provider content policy",
    )
    task.fail_code = "prompt_violation"

    assert (
        content_factory_tasks._content_factory_retryable_video_failure(task)
        is False
    )


def test_provider_cycle_retry_waits_for_cooldown():
    task = _failed_video_task(73, provider="toapis")
    task.updated_at = datetime(2026, 7, 23, 1, 0, 0)

    assert content_factory_tasks._content_factory_provider_cycle_cooldown_elapsed(
        task,
        now=datetime(2026, 7, 23, 1, 2, 59),
        cooldown_seconds=180,
    ) is False
    assert content_factory_tasks._content_factory_provider_cycle_cooldown_elapsed(
        task,
        now=datetime(2026, 7, 23, 1, 3, 0),
        cooldown_seconds=180,
    ) is True


def test_doubao_non_video_conversation_rotates_account_without_provider_cooldown():
    task = _failed_video_task(731, provider="doubao")

    task.fail_code = "doubao_text_only_response"
    assert content_factory_tasks._content_factory_account_rotation_failure(task) is True

    task.fail_code = "doubao_silent_timeout"
    assert content_factory_tasks._content_factory_account_rotation_failure(task) is True

    task.fail_code = "doubao_membership_required"
    task.input_json["seconds"] = 7
    assert content_factory_tasks._content_factory_account_rotation_failure(task) is True

    task.input_json["seconds"] = 12
    assert content_factory_tasks._content_factory_account_rotation_failure(task) is False

    task.fail_code = "bandianwa_worker_error"
    assert content_factory_tasks._content_factory_account_rotation_failure(task) is False


def test_released_provider_failure_is_not_blocked_by_stale_dependency_meta():
    task = _failed_video_task(74, provider="bandianwa")
    task.result_json = {
        "__local": {
            "dependency_failed_task_id": 73,
            "dependency_released_at": "2026-07-23T01:00:00",
        }
    }

    assert (
        content_factory_tasks._is_unreleasable_dependency_failure(task)
        is False
    )
    assert content_factory_tasks._content_factory_retryable_video_failure(task)


def test_cooldown_retry_restores_only_downstream_dependency_failures():
    root = _failed_video_task(81)
    downstream_a = _failed_video_task(82, fail_msg="Previous segment failed")
    downstream_b = _failed_video_task(83, fail_msg="Previous segment failed")
    unrelated = _failed_video_task(84, fail_msg="INTERNAL")
    downstream_a.fail_code = "dependency_failed"
    downstream_b.fail_code = "dependency_failed"
    downstream_a.result_json = {
        "__local": {"dependency_failed_task_id": 81}
    }
    downstream_b.result_json = {
        "__local": {"dependency_failed_task_id": 82}
    }
    group = {
        "video_index": 1,
        "segments": [
            {"segment_index": 7, "task_id": 81},
            {"segment_index": 8, "task_id": 82, "dependency_status": "failed"},
            {"segment_index": 9, "task_id": 83, "dependency_status": "failed"},
        ],
    }
    added = []
    fake_db = SimpleNamespace(add=added.append)

    restored = (
        content_factory_tasks._restore_failed_segment_dependencies_for_retry(
            fake_db,
            group=group,
            root_segment=group["segments"][0],
            task_by_id={
                81: root,
                82: downstream_a,
                83: downstream_b,
                84: unrelated,
            },
        )
    )

    assert restored == [82, 83]
    assert downstream_a.state == downstream_b.state == "waiting_dependency"
    assert downstream_a.fail_code is None and downstream_b.fail_code is None
    assert group["segments"][1]["dependency_status"] == "waiting_previous_segment"
    assert group["segments"][2]["dependency_status"] == "waiting_previous_segment"
    assert unrelated.state == "failed"
    assert added == [downstream_a, downstream_b]


def test_failed_dependency_remains_waiting_during_bounded_provider_recovery():
    project = HermesContentFactoryProject(
        id=1,
        project_key="cf_dependency_cooldown",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        config_json={"content_factory_video_retry_limit": 2},
        state_json={
            "ai_video_groups": [
                {
                    "video_index": 4,
                    "provider_retry_budget_per_segment": 2,
                }
            ],
            # A different completed variant must not consume this variant's
            # retry budget.
            "ai_video_segment_retry_counts": {"1:1": 2},
            "ai_video_exhausted_cooldown_retry_generations": {},
        },
    )
    dependency = _failed_video_task(91)
    dependency.input_json = {
        "content_factory_project_id": 1,
        "content_factory_video_index": 4,
        "content_factory_segment_index": 1,
    }
    chained = _failed_video_task(92)
    chained.state = "waiting_dependency"
    chained.fail_code = None
    chained.fail_msg = None
    chained.input_json = {
        "content_factory_project_id": 1,
        "content_factory_dependency_task_id": 91,
    }
    added = []
    fake_db = SimpleNamespace(add=added.append, flush=lambda: None)

    failed = content_factory_tasks._fail_unreleasable_segment_dependencies(
        fake_db,
        project,
        [dependency, chained],
    )

    assert failed == []
    assert chained.state == "waiting_dependency"
    assert chained.fail_code is None
    assert added == []


def test_failed_dependency_becomes_terminal_after_final_recovery_generation():
    project = HermesContentFactoryProject(
        id=1,
        project_key="cf_dependency_exhausted",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        config_json={"content_factory_video_retry_limit": 2},
        state_json={
            "ai_video_segment_retry_counts": {"4:1": 2},
            "ai_video_exhausted_cooldown_retry_generations": {
                "4:1": {"policy_version": "test"}
            },
        },
    )
    dependency = _failed_video_task(93)
    dependency.input_json = {
        "content_factory_project_id": 1,
        "content_factory_video_index": 4,
        "content_factory_segment_index": 1,
    }
    chained = _failed_video_task(94)
    chained.state = "waiting_dependency"
    chained.fail_code = None
    chained.fail_msg = None
    chained.input_json = {
        "content_factory_project_id": 1,
        "content_factory_dependency_task_id": 93,
    }
    added = []
    fake_db = SimpleNamespace(add=added.append, flush=lambda: None)

    failed = content_factory_tasks._fail_unreleasable_segment_dependencies(
        fake_db,
        project,
        [dependency, chained],
    )

    assert failed == [94]
    assert chained.state == "failed"
    assert chained.fail_code == "dependency_failed"
    assert chained in added
    assert project in added


def test_missing_authoritative_asset_is_the_only_actionable_input_pause():
    now = datetime(2026, 8, 7, 8, 30, 0)
    project = HermesContentFactoryProject(
        project_key="cf_missing_product_asset",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="failed",
        current_stage="FACTS",
        config_json={"auto_run": True},
        state_json={},
        last_error="CONTENT_PRODUCT_ASSETS_REQUIRED",
    )
    stage = HermesContentFactoryStage(
        project_id=1,
        workspace_id=3,
        user_id=101,
        stage="FACTS",
        attempt=5,
        status="failed",
        error_message="CONTENT_PRODUCT_ASSETS_REQUIRED",
        input_json={"retry_after": "2026-08-07T09:00:00"},
    )

    _pause_external_input_stage(
        project,
        stage,
        fault_class="MISSING_AUTHORITATIVE_PRODUCT_ASSET",
        now=now,
    )

    assert project.status == "paused"
    assert project.state_json["pause_reason_code"] == "external_input_required"
    assert project.state_json["missing_authority"] is True
    assert stage.input_json["external_input_required"] is True
    assert "retry_after" not in stage.input_json


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


def test_empty_signed_reference_contract_rewinds_to_fresh_production_plan(
    db_session,
    monkeypatch,
):
    project = HermesContentFactoryProject(
        project_key="cf_empty_reference_contract",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="VISUAL_PREVIEW",
        config_json={"auto_run": True, "video_count": 1},
        state_json={
            "active_variant_index": 1,
            "approved_production_plans_by_variant": {
                "1": {"variant_index": 1, "plan_sha256": "a" * 64}
            },
            "approved_production_plan": {
                "variant_index": 1,
                "plan_sha256": "a" * 64,
            },
            "pause_reason_code": "terminal_self_heal_contract_error",
            "terminal_self_heal_error": {
                "error_code": "PRODUCTION_PLAN_REFERENCE_CONTRACT_EMPTY"
            },
        },
        last_error=(
            "PRODUCTION_PLAN_REFERENCE_CONTRACT_EMPTY: signed media design "
            "has no reference rows"
        ),
    )
    db_session.add(project)
    db_session.flush()
    plan_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="PRODUCTION_PLAN",
        attempt=1,
        status="success",
        input_json={"variant_index": 1, "variant_total": 1},
    )
    failed_visual = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=1,
        status="failed",
        input_json={"variant_index": 1, "variant_total": 1},
        error_message=project.last_error,
    )
    db_session.add_all([plan_stage, failed_visual])
    db_session.flush()
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args, **_kwargs: "toapis:text",
    )

    repair = _reset_production_plan_after_empty_reference_contract(
        db_session,
        project,
        failed_visual,
        reason=failed_visual.error_message,
    )

    assert repair is not None
    assert repair.stage == "PRODUCTION_PLAN"
    assert repair.status == "queued"
    assert repair.attempt == 2
    assert repair.input_json["force_fresh_response"] is True
    assert repair.input_json["self_heal_action"] == (
        "replan_after_empty_signed_reference_contract"
    )
    assert plan_stage.status == "failed"
    assert failed_visual.status == "failed"
    assert project.status == "queued"
    assert project.current_stage == "PRODUCTION_PLAN"
    assert project.last_error is None
    assert project.state_json["approved_production_plans_by_variant"] == {}
    assert "approved_production_plan" not in project.state_json
    assert "terminal_self_heal_error" not in project.state_json
    assert project.state_json[
        "production_plan_reference_contract_recovery"
    ]["1"]["count"] == 1


def test_invalid_signed_media_contract_quarantines_unfinished_variants(
    db_session,
    monkeypatch,
):
    project = HermesContentFactoryProject(
        project_key="cf_invalid_media_contract",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="",
        market="US",
        status="paused",
        current_stage="VISUAL_PREVIEW",
        config_json={
            "auto_run": True,
            "manual_paused": True,
            "video_count": 6,
        },
        state_json={
            "active_variant_index": 6,
            "approved_production_plans_by_variant": {
                "5": {"variant_index": 5, "plan_sha256": "a" * 64},
                "6": {"variant_index": 6, "plan_sha256": "b" * 64},
            },
            "approved_production_plan": {
                "variant_index": 6,
                "plan_sha256": "b" * 64,
            },
            "ai_video_task_ids": [3091, 3092],
            "ai_video_groups": [{
                "video_index": 5,
                "source_stage_id": 10,
                "segments": [
                    {"segment_index": 1, "task_id": 3091},
                    {"segment_index": 2, "task_id": 3092},
                ],
            }],
            "video_variant_pipeline": {
                "target_count": 6,
                "active_index": 6,
                "completed_indices": [1, 2, 3, 4],
                "submitted_indices": [1, 2, 3, 4, 5],
                "failed_indices": [],
            },
            "pause_reason_code": "manual_pause",
        },
    )
    db_session.add(project)
    db_session.flush()
    plan5 = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="PRODUCTION_PLAN",
        attempt=1,
        status="success",
        input_json={"variant_index": 5, "variant_total": 6},
    )
    video5 = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="success",
        input_json={"variant_index": 5, "variant_total": 6},
    )
    plan6 = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="PRODUCTION_PLAN",
        attempt=2,
        status="success",
        input_json={"variant_index": 6, "variant_total": 6},
    )
    paused6 = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="VISUAL_PREVIEW",
        attempt=1,
        status="paused",
        input_json={"variant_index": 6, "variant_total": 6},
    )
    db_session.add_all([plan5, video5, plan6, paused6])
    db_session.flush()
    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args, **_kwargs: "toapis:text",
    )

    repair = _reset_production_plan_after_invalid_media_contract(
        db_session,
        project,
        plan5,
        reason=(
            "UNBOUND_PRODUCT_VISUAL_DEPICTION_FORBIDDEN: generic gummies"
        ),
    )

    assert repair.stage == "PRODUCTION_PLAN"
    assert repair.status == "queued"
    assert repair.input_json["variant_index"] == 5
    assert repair.input_json["invalidated_future_variants"] == [5, 6]
    assert repair.input_json["self_heal_action"] == (
        "replan_after_invalid_signed_media_contract"
    )
    assert plan5.status == video5.status == "failed"
    assert plan6.status == paused6.status == "failed"
    assert project.status == "queued"
    assert project.current_stage == "PRODUCTION_PLAN"
    assert project.config_json["manual_paused"] is False
    assert project.state_json["ai_video_groups"] == []
    assert project.state_json["ai_video_task_ids"] == []
    assert project.state_json["approved_production_plans_by_variant"] == {}
    assert "approved_production_plan" not in project.state_json
    pipeline = project.state_json["video_variant_pipeline"]
    assert pipeline["active_index"] == 5
    assert pipeline["completed_indices"] == [1, 2, 3, 4]
    assert pipeline["submitted_indices"] == [1, 2, 3, 4]
    assert project.state_json[
        "invalid_signed_media_contract_recovery"
    ]["quarantined_task_ids"] == [3091, 3092]


def test_director_generation_mismatch_is_not_treated_as_transient_retry():
    assert content_factory_tasks._is_production_plan_director_generation_mismatch(
        "PRODUCTION_PLAN_MEDIA_GATE_DIRECTOR_MISMATCH: "
        "the plan belongs to another Director artifact"
    ) is True
    assert content_factory_tasks._is_production_plan_director_generation_mismatch(
        "image provider temporarily unavailable"
    ) is False


def test_invalid_contract_marker_is_restored_after_late_provider_commit():
    project = HermesContentFactoryProject(
        id=1,
        project_key="cf_contract_marker_race",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="",
        market="US",
        state_json={
            "invalid_signed_media_contract_recovery": {
                "quarantined_task_ids": [3102],
            },
        },
    )
    task = KieTask(
        id=3102,
        workspace_id=3,
        key_id=9,
        created_by_user_id=101,
        model="omni_flash",
        task_id="sub2api-flow-3102",
        state="success",
        input_json={"content_factory_project_key": project.project_key},
        result_json={"__local": {"active_provider": "sub2api"}},
    )
    added = []
    fake_db = SimpleNamespace(add=added.append)
    original = content_factory_tasks._scoped_content_video_tasks
    content_factory_tasks._scoped_content_video_tasks = (
        lambda _db, _project, _ids: [task]
    )
    try:
        count = _reconcile_invalid_media_contract_task_markers(
            fake_db,
            project,
        )
    finally:
        content_factory_tasks._scoped_content_video_tasks = original

    assert count == 1
    assert task.state == "success"
    assert task.result_json["__local"]["superseded_reason"] == (
        "invalid_signed_media_contract"
    )
    assert task.result_json["__local"]["drain_non_authoritative"] is False
    assert added == [task]


def test_semantic_contract_repair_error_is_not_misclassified_as_terminal():
    assert _terminal_self_heal_error_code(
        "critic contract error: repair invalid JSON and retry"
    ) is None


def test_impossible_copy_transport_contract_is_terminal():
    assert _terminal_self_heal_error_code(
        "PRODUCTION_PLAN_COPY_TRANSPORT_CONTRACT_INVALID: approved script "
        "cannot fit registered transport segment 2 spoken lane"
    ) == "PRODUCTION_PLAN_COPY_TRANSPORT_CONTRACT_INVALID"


def test_policy_upgrade_releases_old_nonrunning_circuit_immediately():
    stage = HermesContentFactoryStage(
        stage="VISUAL_PREVIEW",
        status="retrying",
        input_json={
            "self_heal_policy_version": 1,
            "self_heal_count": 3,
            "self_heal_circuit_open": True,
            "retry_after": "2026-07-23T02:10:00",
            "run_token": "old-token",
            "visual_api": {"status": "failed"},
        },
    )

    count, values = _upgrade_self_heal_policy_generation(
        stage,
        now=datetime(2026, 7, 23, 2, 0, 0),
    )

    assert count == 0
    assert values["self_heal_policy_version"] == (
        content_factory_tasks.SELF_HEAL_POLICY_VERSION
    )
    assert values["self_heal_circuit_open"] is False
    assert "retry_after" not in values
    assert "run_token" not in values
    assert "visual_api" not in values
    assert values["self_heal_action"] == (
        "retry_immediately_after_source_policy_upgrade"
    )


def test_policy_upgrade_preserves_running_delivery_lease_and_token():
    stage = HermesContentFactoryStage(
        stage="VISUAL_PREVIEW",
        status="running",
        input_json={
            "self_heal_policy_version": 1,
            "self_heal_count": 3,
            "self_heal_circuit_open": True,
            "retry_after": "2026-07-23T02:10:00",
            "run_token": "active-token",
        },
    )

    count, values = _upgrade_self_heal_policy_generation(
        stage,
        now=datetime(2026, 7, 23, 2, 0, 0),
    )

    assert count == 0
    assert values["self_heal_policy_version"] == (
        content_factory_tasks.SELF_HEAL_POLICY_VERSION
    )
    assert values["self_heal_circuit_open"] is True
    assert values["retry_after"] == "2026-07-23T02:10:00"
    assert values["run_token"] == "active-token"


def test_control_plane_quality_pause_is_due_for_bounded_automatic_replan():
    now = datetime(2026, 7, 23, 2, 0, 0)
    project = HermesContentFactoryProject(
        project_key="cf_quality_replan",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="DIRECTOR",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "content_director_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:58:00",
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=now,
        generation="self-heal-test:profile-v5",
    )

    assert decision["eligible"] is True
    assert decision["due"] is True
    assert decision["attempt_count"] == 0


def test_final_video_quality_pause_extracts_only_evidenced_failed_task():
    project = HermesContentFactoryProject(
        id=184,
        project_key="cf_final_quality_recovery",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "final_video_quality_failure": {
                "message": (
                    "CONTENT_PRODUCT_VIDEO_VISUAL_QA_FAILED_TASK_3302: "
                    "product interaction is inconsistent"
                ),
            },
            "ai_video_failed_task_ids": [3270, 3290, 3299, 3302],
        },
    )

    assert content_factory_tasks._final_video_quality_failed_task_ids(project) == [3302]


def test_final_intent_pause_includes_every_model_named_affected_task():
    project = HermesContentFactoryProject(
        id=185,
        project_key="cf_final_intent_all_segments",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "final_video_quality_failure": {
                "message": "CONTENT_SEGMENT_EXECUTION_QA_FAILED_TASK_3466",
                "task_ids": [3466, 3435],
                "final_intent_report": {
                    "policy_version": (
                        content_factory_tasks.FINAL_INTENT_REVIEW_POLICY_VERSION
                    ),
                    "repair_scope": "segment_regeneration",
                    "affected_segment_indices": [1, 2, 3],
                    "affected_task_ids": [3466, 3435, 3469],
                },
            },
        },
    )

    assert content_factory_tasks._final_video_quality_failed_task_ids(project) == [
        3466,
        3435,
        3469,
    ]


def test_manual_pause_never_enters_final_video_quality_self_heal():
    project = HermesContentFactoryProject(
        id=184,
        project_key="cf_manual_final_quality",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True, "manual_paused": True},
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "final_video_quality_failure": {
                "message": "CONTENT_PRODUCT_VIDEO_VISUAL_QA_FAILED_TASK_3302"
            },
        },
    )

    retry_ids, decision = content_factory_tasks._resume_final_video_quality_pause(
        SimpleNamespace(),
        project,
        now=datetime(2026, 7, 30, 12, 0, 0),
    )

    assert retry_ids == []
    assert decision["eligible"] is False


def test_recent_final_video_quality_recovery_claim_blocks_duplicate_resume():
    now = datetime(2026, 8, 5, 13, 30, 0)
    project = HermesContentFactoryProject(
        id=184,
        project_key="cf_claimed_final_quality",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "final_video_quality_failure": {
                "message": "CONTENT_FINAL_INTENT_QA_FAILED",
            },
            "final_video_quality_recovery": {
                "status": "planning",
                "at": "2026-08-05T13:29:30",
                "failed_task_ids": [3302],
            },
        },
    )

    retry_ids, decision = content_factory_tasks._resume_final_video_quality_pause(
        SimpleNamespace(),
        project,
        now=now,
    )

    assert retry_ids == []
    assert decision["eligible"] is True
    assert decision["recovered"] is False
    assert decision["status"] == "recovery_in_progress"


def test_provider_success_with_blocking_quality_incident_is_retryable():
    task = KieTask(
        id=3638,
        workspace_id=3,
        created_by_user_id=101,
        state="success",
        input_json={"content_factory_project_id": 187},
    )
    content_factory_tasks.set_task_local_meta(
        task,
        content_quality_incident={
            "code": content_factory_tasks.PRODUCT_VISUAL_QA_FAIL_CODE,
            "message": "CONTENT_PRODUCT_VIDEO_VISUAL_QA_FAILED_TASK_3638",
            "evidence": {"contact_sheet_path": "/tmp/sheet.jpg"},
        },
    )

    assert content_factory_tasks._content_task_has_blocking_quality_incident(
        task
    ) is True
    assert content_factory_tasks._content_task_is_final_quality_repair_candidate(
        task
    ) is True


def test_provider_success_without_quality_incident_is_not_retryable():
    task = KieTask(
        id=3639,
        workspace_id=3,
        created_by_user_id=101,
        state="success",
        input_json={"content_factory_project_id": 187},
    )

    assert content_factory_tasks._content_task_has_blocking_quality_incident(
        task
    ) is False
    assert content_factory_tasks._content_task_is_final_quality_repair_candidate(
        task
    ) is False


def test_product_quality_incident_becomes_scoped_repair_report():
    project = HermesContentFactoryProject(
        id=187,
        project_key="cf_product_incident_report",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
    )
    task = KieTask(
        id=3638,
        workspace_id=3,
        created_by_user_id=101,
        state="success",
        input_json={
            "content_factory_project_id": 187,
            "content_factory_video_index": 6,
            "content_factory_segment_index": 3,
        },
    )
    content_factory_tasks.set_task_local_meta(
        task,
        content_quality_incident={
            "code": content_factory_tasks.PRODUCT_VISUAL_QA_FAIL_CODE,
            "message": "Product body and closure identity drifted.",
        },
        provider_product_video_review={
            "policy_version": "product-review-v3",
            "blocking_reasons": ["The rendered jar silhouette is wrong."],
            "repair_instruction": "Preserve the squat jar and gold lid structure.",
            "contact_sheet_path": "/tmp/product-task-3638.jpg",
        },
    )
    db = SimpleNamespace(get=lambda _model, row_id: task if row_id == 3638 else None)

    report = content_factory_tasks._quality_incident_repair_report(
        db,
        project,
        [3638],
    )

    assert report["repair_scope"] == "segment_regeneration"
    assert report["video_index"] == 6
    assert report["affected_segment_indices"] == [3]
    assert report["affected_task_ids"] == [3638]
    assert report["contact_sheet_path"] == "/tmp/product-task-3638.jpg"
    assert report["quality_incident_code"] == (
        content_factory_tasks.PRODUCT_VISUAL_QA_FAIL_CODE
    )


def test_premature_series_quality_pause_resumes_without_regenerating_media(
    monkeypatch,
):
    now = datetime(2026, 7, 31, 1, 0, 0)
    report = {
        "policy_version": content_factory_tasks.FINAL_INTENT_REVIEW_POLICY_VERSION,
        "blocking_requirement_ids": ["R-007"],
        "blocking_reasons": ["Other deliverables are not available."],
    }
    project = HermesContentFactoryProject(
        id=184,
        project_key="cf_series_scope_recovery",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={
            "auto_run": True,
            "manual_paused": False,
            "video_count": 3,
        },
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "final_video_quality_failure": {
                "message": (
                    "CONTENT_FINAL_QUALITY_GATE_FAILED: "
                    "CONTENT_FINAL_INTENT_QA_FAILED "
                    + __import__("json").dumps(report)[:-1]
                    + ', "repair_instruction": "truncated historical report'
                ),
            },
            "video_variant_pipeline": {
                "target_count": 3,
                "completed_indices": [2],
            },
            "ai_video_groups": [{
                "video_index": 2,
                "intent_requirements": [{
                    "requirement_id": "R-007",
                    "kind": "differentiation",
                    "scope": "project",
                    "deliverable_ordinals": [1, 2, 3],
                }],
            }],
        },
    )
    scheduled = []
    monkeypatch.setattr(
        content_factory_tasks,
        "_completed_video_assets_by_index",
        lambda *_args, **_kwargs: {2: object()},
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_schedule_video_wait",
        lambda *_args, **kwargs: scheduled.append(kwargs["reason"]) or "wait-1",
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        flush=lambda: None,
        get=lambda _model, _row_id: None,
    )

    retry_ids, decision = content_factory_tasks._resume_final_video_quality_pause(
        db,
        project,
        now=now,
    )

    assert retry_ids == []
    assert decision["recovered"] is True
    assert decision["status"] == "deferred_cross_deliverable_review"
    assert decision["completed_count"] == 1
    assert decision["target_count"] == 3
    assert project.status == "generating_video"
    assert "pause_reason_code" not in project.state_json
    assert project.state_json[
        "cross_deliverable_quality_review_deferred"
    ]["blocking_requirement_ids"] == ["R-007"]
    assert len(scheduled) == 1


def test_obsolete_final_intent_policy_is_requeued_for_current_review(
    monkeypatch,
):
    project = HermesContentFactoryProject(
        id=185,
        project_key="cf_obsolete_final_intent_review",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "final_video_quality_failure": {
                "message": (
                    "CONTENT_FINAL_QUALITY_GATE_FAILED: "
                    "CONTENT_FINAL_INTENT_QA_FAILED "
                    '{"policy_version":"2026-07-28-final-intent-guardian-v1",'
                    '"blocking_requirement_ids":["R-001"]}'
                ),
            },
        },
    )
    scheduled = []
    monkeypatch.setattr(
        content_factory_tasks,
        "_schedule_video_wait",
        lambda *_args, **kwargs: scheduled.append(kwargs["reason"]) or "wait-2",
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        flush=lambda: None,
        get=lambda _model, _row_id: None,
    )

    retry_ids, decision = content_factory_tasks._resume_final_video_quality_pause(
        db,
        project,
        now=datetime(2026, 7, 31, 2, 0, 0),
    )

    assert retry_ids == []
    assert decision["recovered"] is True
    assert decision["status"] == "obsolete_final_intent_policy_requeued"
    assert project.status == "generating_video"
    assert "pause_reason_code" not in project.state_json
    assert len(scheduled) == 1


def test_final_intent_director_replan_is_queued_without_manual_intervention(
    monkeypatch,
):
    report = {
        "policy_version": content_factory_tasks.FINAL_INTENT_REVIEW_POLICY_VERSION,
        "video_index": 1,
        "repair_scope": "director_replan",
        "blocking_requirement_ids": ["R-002", "R-006"],
        "blocking_reasons": ["The opening hook and conversion bridge are weak."],
        "affected_segment_indices": [1, 2, 3],
        "affected_task_ids": [3322, 3323, 3324],
        "repair_instruction": "Create a faster opening and a clear product bridge.",
    }
    project = HermesContentFactoryProject(
        id=185,
        project_key="cf_final_intent_director_replan",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={
            "auto_run": True,
            "manual_paused": False,
            "video_count": 5,
        },
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "active_variant_index": 2,
            "video_variant_pipeline": {
                "target_count": 5,
                "active_index": 2,
                "submitted_indices": [1, 2],
                "completed_indices": [],
            },
            "final_video_quality_failure": {
                "message": "CONTENT_FINAL_INTENT_QA_FAILED " + __import__("json").dumps(report),
                "final_intent_report": report,
            },
        },
    )
    source_stage = HermesContentFactoryStage(
        id=4101,
        project_id=185,
        workspace_id=3,
        user_id=101,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="success",
        input_json={"variant_index": 1},
    )
    repair_stage = HermesContentFactoryStage(
        id=4102,
        project_id=185,
        workspace_id=3,
        user_id=101,
        stage="DIRECTOR",
        attempt=2,
        status="queued",
        input_json={"variant_index": 1},
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_variant_stage",
        lambda _db, _project, stage, variant: (
            source_stage if stage == "VIDEO_PROMPTS" and variant == 1 else None
        ),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_create_repair_stage",
        lambda *_args, **_kwargs: repair_stage,
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        flush=lambda: None,
        get=lambda _model, _row_id: None,
    )

    retry_ids, decision = content_factory_tasks._resume_final_video_quality_pause(
        db,
        project,
        now=datetime(2026, 7, 31, 3, 0, 0),
    )

    assert retry_ids == []
    assert decision["recovered"] is True
    assert decision["status"] == "director_replanning"
    assert decision["repair_stage_id"] == 4102
    assert project.status == "queued"
    assert project.current_stage == "DIRECTOR"
    assert project.state_json["active_variant_index"] == 1
    assert "pause_reason_code" not in project.state_json
    assert repair_stage.input_json["director_replan_source_stage_id"] == 4101
    assert repair_stage.input_json["director_replan_feedback"][0]["code"] == (
        "FINAL_INTENT_DIRECTOR_REPLAN"
    )
    assert source_stage.status == "superseded"
    assert "late waiters" in str(source_stage.error_message)


def test_exhausted_segment_repair_asks_multimodal_scope_reviewer_then_replans(
    monkeypatch,
):
    report = {
        "policy_version": content_factory_tasks.FINAL_INTENT_REVIEW_POLICY_VERSION,
        "video_index": 3,
        "repair_scope": "segment_regeneration",
        "blocking_requirement_ids": ["R-004"],
        "blocking_reasons": ["The opening remains visually ordinary."],
        "affected_segment_indices": [1],
        "affected_task_ids": [3563],
        "repair_instruction": "Create a stronger first-half-second disruption.",
        "contact_sheet_path": "/tmp/unused-because-review-is-mocked.jpg",
    }
    project = HermesContentFactoryProject(
        id=186,
        project_key="cf_history_aware_replan",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={
            "auto_run": True,
            "manual_paused": False,
            "video_count": 5,
        },
        state_json={
            "pause_reason_code": "final_video_quality_gate",
            "video_variant_pipeline": {
                "target_count": 5,
                "active_index": 3,
                "submitted_indices": [1, 2, 3],
                "completed_indices": [1, 2],
            },
            "ai_video_retry_history": [{
                "video_segment": "3:1",
                "failed_task_id": 3560,
                "retry_task_id": 3563,
                "attempt": 1,
                "budget_kind": "quality",
                "final_intent_incident_repair": True,
            }],
            "final_video_quality_recovery": {
                "status": "bounded_retry_exhausted",
                "scope": "failed_segments_only",
                "failed_task_ids": [3563],
            },
            "final_video_quality_failure": {
                "message": "CONTENT_FINAL_INTENT_QA_FAILED " + __import__("json").dumps(report),
                "final_intent_report": report,
            },
        },
    )
    source_stage = HermesContentFactoryStage(
        id=4201,
        project_id=186,
        workspace_id=3,
        user_id=101,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="success",
        input_json={"variant_index": 3},
    )
    repair_stage = HermesContentFactoryStage(
        id=4202,
        project_id=186,
        workspace_id=3,
        user_id=101,
        stage="DIRECTOR",
        attempt=2,
        status="queued",
        input_json={"variant_index": 3},
    )
    failed_task = KieTask(
        id=3563,
        workspace_id=3,
        created_by_user_id=101,
        state="success",
        input_json={"content_factory_project_id": 186},
    )
    content_factory_tasks.set_task_local_meta(
        failed_task,
        content_factory_execution_replan_error=(
            "SEGMENT_EXECUTION_REPLAN_DIRECTION_RETRY_EXHAUSTED"
        ),
    )
    captured = {}

    def scope_review(_db, *, repair_history, **_kwargs):
        captured["repair_history"] = repair_history
        return {
            "repair_scope": "director_replan",
            "repair_instruction": "Replace the hook mechanism at story level.",
            "rationale": "The local strategy already failed.",
        }

    monkeypatch.setattr(
        content_factory_tasks,
        "review_final_intent_repair_scope_api",
        scope_review,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_variant_stage",
        lambda _db, _project, stage, variant: (
            source_stage if stage == "VIDEO_PROMPTS" and variant == 3 else None
        ),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_create_repair_stage",
        lambda *_args, **_kwargs: repair_stage,
    )
    db = SimpleNamespace(
        add=lambda _value: None,
        flush=lambda: None,
        get=lambda _model, row_id: failed_task if row_id == 3563 else None,
    )

    retry_ids, decision = content_factory_tasks._resume_final_video_quality_pause(
        db,
        project,
        now=datetime(2026, 8, 5, 1, 0, 0),
    )

    assert retry_ids == []
    assert decision["status"] == "director_replanning"
    assert decision["video_index"] == 3
    assert captured["repair_history"]["bounded_segment_repair_exhausted"] is True
    assert captured["repair_history"]["copy_authority"] == (
        "director_model_editable"
    )
    assert captured["repair_history"]["retry_attempts"][0]["retry_task_id"] == 3563
    assert captured["repair_history"]["local_repair_compiler_errors"] == [{
        "task_id": 3563,
        "error": "SEGMENT_EXECUTION_REPLAN_DIRECTION_RETRY_EXHAUSTED",
    }]
    assert repair_stage.input_json["director_replan_feedback"][0][
        "repair_instruction"
    ] == "Replace the hook mechanism at story level."
    assert source_stage.status == "superseded"


def test_historical_director_replan_supersedes_legacy_success_prompt_stage():
    source_stage = SimpleNamespace(
        id=4201,
        project_id=186,
        stage="VIDEO_PROMPTS",
        status="success",
    )
    newer_director = SimpleNamespace(
        input_json={"director_replan_source_stage_id": 4201},
    )

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [newer_director]

    db = SimpleNamespace(
        get=lambda _model, _stage_id: source_stage,
        query=lambda _model: _Query(),
    )
    project = SimpleNamespace(id=186)

    assert content_factory_tasks._media_group_source_is_superseded(
        db,
        project,
        {"source_stage_id": 4201},
    ) is True


def test_historical_director_replan_uses_affected_task_source_stage():
    source_stage = SimpleNamespace(
        id=4201,
        project_id=186,
        stage="VIDEO_PROMPTS",
        status="success",
    )
    affected_task = SimpleNamespace(
        workspace_id=3,
        input_json={
            "content_factory_project_id": 186,
            "content_factory_source_stage_id": 4201,
        },
    )
    newer_director = SimpleNamespace(
        input_json={
            # The latest prompt stage can differ from the prompt generation
            # that actually submitted the rejected provider task.
            "director_replan_source_stage_id": 4209,
            "final_intent_replan_report": {
                "affected_task_ids": [3563],
            },
        },
    )

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [newer_director]

    def _get(model, row_id):
        if model is HermesContentFactoryStage and row_id == 4201:
            return source_stage
        if model is KieTask and row_id == 3563:
            return affected_task
        return None

    db = SimpleNamespace(get=_get, query=lambda _model: _Query())
    project = SimpleNamespace(id=186, workspace_id=3)

    assert content_factory_tasks._media_group_source_is_superseded(
        db,
        project,
        {"source_stage_id": 4201},
    ) is True


def test_legacy_final_intent_pause_recovers_exact_preceding_result_sheet(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        content_factory_tasks,
        "CONTENT_FACTORY_STORAGE_ROOT",
        tmp_path,
    )
    project = HermesContentFactoryProject(
        id=186,
        project_key="cf_legacy_evidence",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        state_json={
            "final_video_quality_failure": {
                "final_intent_report": {
                    "video_index": 9,
                    "repair_scope": "segment_regeneration",
                    "affected_task_ids": [3563],
                },
            },
        },
    )
    review_dir = (
        tmp_path
        / "workspace_3"
        / "cf_legacy_evidence"
        / "generated"
        / "final_intent_reviews"
    )
    review_dir.mkdir(parents=True)
    stale_result = review_dir / "result-stale.jpg"
    exact_result = review_dir / "result-exact.jpg"
    segment_sheet = review_dir / "segment-1-task-3563-current.jpg"
    for path in (stale_result, exact_result, segment_sheet):
        path.write_bytes(b"x" * 2048)
    os.utime(stale_result, (10, 10))
    os.utime(exact_result, (20, 20))
    os.utime(segment_sheet, (21, 21))

    report = content_factory_tasks._final_intent_quality_failure_report(
        project
    )

    assert report["contact_sheet_path"] == str(exact_result.resolve())
    assert report["evidence_recovered_from_legacy_pause"] is True


def test_director_quality_recovery_retains_upstream_replan_evidence():
    project = HermesContentFactoryProject(
        project_key="cf_director_replan_evidence",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        state_json={
            "automatic_quality_upstream_replan": {
                "from_stage": "PRODUCTION_PLAN",
                "to_stage": "DIRECTOR",
                "source_stage_id": 2956,
                "feedback": [{
                    "code": "IMMUTABLE_HOOK_TIMING_CONTRADICTION",
                    "line_ids": ["s1_display_count", "s1_display_loss"],
                    "evidence": "The approved hook and delivery lanes disagree.",
                    "repair_instruction": "Create one authoritative boundary.",
                }],
            },
        },
    )

    feedback, source_stage_id, from_stage = (
        content_factory_tasks._persisted_director_replan_context(project)
    )

    assert source_stage_id == 2956
    assert from_stage == "PRODUCTION_PLAN"
    assert feedback == [{
        "code": "IMMUTABLE_HOOK_TIMING_CONTRADICTION",
        "line_ids": ["s1_display_count", "s1_display_loss"],
        "evidence": "The approved hook and delivery lanes disagree.",
        "repair_instruction": "Create one authoritative boundary.",
    }]


def test_director_quality_recovery_rejects_incomplete_saved_evidence():
    project = HermesContentFactoryProject(
        project_key="cf_director_replan_bad_evidence",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        state_json={
            "automatic_quality_upstream_replan": {
                "from_stage": "PRODUCTION_PLAN",
                "source_stage_id": 2956,
                "feedback": [{
                    "code": "MISSING_REPAIR_INSTRUCTION",
                    "evidence": "Incomplete evidence must not authorize rewrite.",
                }],
            },
        },
    )

    assert content_factory_tasks._persisted_director_replan_context(project) == (
        [],
        None,
        None,
    )


def test_new_policy_recovers_terminal_director_replan_context_loss():
    project = HermesContentFactoryProject(
        project_key="cf_old_replan_context_loss",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="DIRECTOR",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "terminal_self_heal_contract_error",
            "automatic_quality_paused_at": "2026-07-23T01:58:00",
            "content_director_quality_pause": {
                "variant_index": 2,
                "stage_id": 2957,
                "at": "2026-07-23T01:57:00",
            },
            "terminal_self_heal_error": {
                "stage": "DIRECTOR",
                "stage_id": 2958,
                "error_code": "DIRECTOR_RESUME_ANCESTOR_APPROVED",
            },
            "automatic_quality_recovery": {
                "status": "replanning",
                "generation": "self-heal-98:universal-short-video-v7",
                "pause_reason_code": "content_director_quality_pause",
                "incident_key": (
                    "content_director_quality_pause:stage-2957:"
                    "variant-2:2026-07-23T01:57:00"
                ),
                "attempt_count": 1,
                "variant_index": 2,
            },
            "automatic_quality_upstream_replan": {
                "from_stage": "PRODUCTION_PLAN",
                "to_stage": "DIRECTOR",
                "source_stage_id": 2956,
                "feedback": [{
                    "code": "IMMUTABLE_HOOK_TIMING_CONTRADICTION",
                    "line_ids": ["s1_display_count", "s1_display_loss"],
                    "evidence": "The approved hook and delivery lanes disagree.",
                    "repair_instruction": "Create one authoritative boundary.",
                }],
            },
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=datetime(2026, 7, 23, 2, 0, 0),
        generation="self-heal-99:universal-short-video-v7",
    )

    assert decision["eligible"] is True
    assert decision["due"] is True
    assert decision["pause_reason_code"] == "content_director_quality_pause"
    assert decision["attempt_count"] == 0


def test_terminal_director_pause_without_complete_evidence_is_not_retried():
    project = HermesContentFactoryProject(
        project_key="cf_unproven_terminal_director",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="DIRECTOR",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "terminal_self_heal_contract_error",
            "automatic_quality_paused_at": "2026-07-23T01:58:00",
            "terminal_self_heal_error": {
                "error_code": "DIRECTOR_RESUME_ANCESTOR_APPROVED",
            },
            "automatic_quality_recovery": {
                "status": "replanning",
                "generation": "self-heal-98:universal-short-video-v7",
                "pause_reason_code": "content_director_quality_pause",
            },
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=datetime(2026, 7, 23, 2, 0, 0),
        generation="self-heal-99:universal-short-video-v7",
    )

    assert decision["eligible"] is False
    assert decision["due"] is False


def test_automatic_quality_replan_is_bounded_per_policy_generation():
    now = datetime(2026, 7, 23, 2, 0, 0)
    generation = "self-heal-test:profile-v5"
    project = HermesContentFactoryProject(
        project_key="cf_quality_exhausted",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="DIRECTOR",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "content_director_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:00:00",
            "content_director_quality_pause": {
                "variant_index": 2,
                "stage_id": 2501,
                "at": "2026-07-23T01:00:00",
            },
            "automatic_quality_recovery": {
                "generation": generation,
                "pause_reason_code": "content_director_quality_pause",
                "incident_key": (
                    "content_director_quality_pause:stage-2501:"
                    "variant-2:2026-07-23T01:00:00"
                ),
                "attempt_count": (
                    content_factory_tasks.derive_quality_recovery_limit(
                        {"video_count": 1},
                        pause_reason="content_director_quality_pause",
                    )
                ),
                "next_retry_at": "2026-07-23T01:01:00",
            },
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=now,
        generation=generation,
    )

    assert decision["eligible"] is True
    assert decision["due"] is False
    assert decision["attempt_count"] == decision["attempt_limit"]


def test_automatic_quality_replan_resets_budget_for_new_variant_incident():
    now = datetime(2026, 7, 23, 2, 0, 0)
    generation = "self-heal-test:profile-v5"
    project = HermesContentFactoryProject(
        project_key="cf_quality_new_variant",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="PRODUCTION_PLAN",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "production_plan_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:59:00",
            "production_plan_quality_pause": {
                "variant_index": 3,
                "stage_id": 2695,
                "at": "2026-07-23T01:59:00",
            },
            "automatic_quality_recovery": {
                "generation": generation,
                "pause_reason_code": "production_plan_quality_pause",
                "incident_key": (
                    "production_plan_quality_pause:stage-2679:"
                    "variant-1:2026-07-23T01:00:00"
                ),
                "attempt_count": (
                    content_factory_tasks.derive_quality_recovery_limit(
                        {"video_count": 1},
                        pause_reason="production_plan_quality_pause",
                    )
                ),
                "next_retry_at": "2026-07-23T01:01:00",
            },
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=now,
        generation=generation,
    )

    assert decision["eligible"] is True
    assert decision["due"] is True
    assert decision["attempt_count"] == 0
    assert decision["source_stage_id"] == 2695
    assert decision["variant_index"] == 3
    assert decision["incident_key"].startswith(
        "production_plan_quality_pause:stage-2695:variant-3:"
    )


def test_automatic_quality_replan_keeps_budget_across_same_recovery_chain():
    now = datetime(2026, 7, 23, 2, 0, 0)
    generation = "self-heal-test:profile-v5"
    incident_key = (
        "production_plan_quality_pause:stage-2695:"
        "variant-3:2026-07-23T01:00:00"
    )
    project = HermesContentFactoryProject(
        project_key="cf_quality_same_chain",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="PRODUCTION_PLAN",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "production_plan_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:59:00",
            "production_plan_quality_pause": {
                "variant_index": 3,
                "stage_id": 2697,
                "at": "2026-07-23T01:59:00",
            },
            "automatic_quality_recovery": {
                "generation": generation,
                "pause_reason_code": "production_plan_quality_pause",
                "incident_key": incident_key,
                "variant_index": 3,
                "status": "replanning",
                "attempt_count": 1,
                "next_retry_at": "2026-07-23T01:01:00",
            },
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=now,
        generation=generation,
    )

    assert decision["eligible"] is True
    assert decision["due"] is True
    assert decision["attempt_count"] == 1
    assert decision["incident_key"] == incident_key


def test_successful_origin_stage_resolves_automatic_quality_chain():
    state = {
        "automatic_quality_recovery": {
            "generation": "self-heal-test:profile-v5",
            "pause_reason_code": "production_plan_quality_pause",
            "incident_key": "incident-1",
            "variant_index": 3,
            "status": "replanning",
            "attempt_count": 2,
        },
    }

    still_open = (
        content_factory_tasks._mark_automatic_quality_recovery_resolved(
            state,
            stage_name="DIRECTOR",
            stage_id=2698,
            variant_index=3,
        )
    )
    assert still_open["automatic_quality_recovery"]["status"] == "replanning"

    resolved = content_factory_tasks._mark_automatic_quality_recovery_resolved(
        still_open,
        stage_name="PRODUCTION_PLAN",
        stage_id=2699,
        variant_index=3,
    )
    assert resolved["automatic_quality_recovery"]["status"] == "resolved"
    assert resolved["automatic_quality_recovery"]["resolved_stage_id"] == 2699


def test_unbound_project_preserves_prose_and_binds_multimodal_authority():
    raw = {
        "creative_constraints": [
            "Keep the apartment and narrator consistent.",
            "The product appears as two deep purple-red raspberry-cluster "
            "gummies in her palm.",
        ],
        "truth_payload": {"required_verbatim_voiceover": "Two gummies."},
    }

    repaired = (
        content_factory_tasks._remove_unbound_product_visual_requirements(
            raw,
            product_required=False,
        )
    )

    constraints = repaired["creative_constraints"]
    assert "Keep the apartment and narrator consistent." in constraints
    assert any("raspberry-cluster" in value for value in constraints)
    assert any("multimodal Director" in value for value in constraints)
    audit = repaired["truth_payload"][
        "unbound_product_visual_constraint_repair"
    ]
    assert audit["server_semantic_filtering_used"] is False
    assert audit["preserved_inherited_constraint_count"] == 2
    assert audit["authority"] == "project_product_required_false"


def test_bound_project_keeps_product_visual_requirement():
    raw = {
        "creative_constraints": [
            "The approved product gummies appear in her palm.",
        ],
    }

    assert content_factory_tasks._remove_unbound_product_visual_requirements(
        raw,
        product_required=True,
    ) == raw


def test_operator_pause_never_enters_automatic_quality_replan():
    project = HermesContentFactoryProject(
        project_key="cf_manual_pause",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="DIRECTOR",
        config_json={"auto_run": True, "manual_paused": True},
        state_json={
            "pause_reason_code": "content_director_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:00:00",
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=datetime(2026, 7, 23, 2, 0, 0),
        generation="self-heal-test:profile-v5",
    )

    assert decision["eligible"] is False
    assert decision["due"] is False


def test_quality_recovery_preserves_binding_operator_instruction(
    db_session,
    monkeypatch,
):
    project = HermesContentFactoryProject(
        project_key="cf_quality_instruction",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="SERIES_DIRECTOR",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "series_director_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:00:00",
            "content_series_quality_pause": {
                "stage_id": 2501,
                "at": "2026-07-23T01:00:00",
            },
        },
    )
    db_session.add(project)
    db_session.flush()
    paused = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="SERIES_DIRECTOR",
        attempt=1,
        status="failed",
        instruction=(
            "Every 0-2 second opening must use an original high-energy "
            "visual disruption; calm default openings are forbidden."
        ),
        completed_at=datetime(2026, 7, 23, 1, 0, 0),
    )
    db_session.add(paused)
    db_session.commit()

    monkeypatch.setattr(
        content_factory_tasks,
        "_automatic_quality_recovery_decision",
        lambda *_args, **_kwargs: {
            "due": True,
            "pause_reason_code": "series_director_quality_pause",
            "attempt_count": 0,
            "attempt_limit": 2,
            "incident_key": "series-director-incident",
            "source_stage_id": paused.id,
            "variant_index": None,
        },
    )
    monkeypatch.setattr(
        "app.services.hermes_agent.content_factory.resume_project",
        lambda _db, value, **_kwargs: value,
    )

    repair, _decision = (
        content_factory_tasks._resume_automatic_quality_pause(
            db_session,
            project,
            now=datetime(2026, 7, 23, 1, 2, 0),
        )
    )

    assert repair is not None
    assert repair.instruction.startswith(paused.instruction)
    assert "Autonomy strategy:" in repair.instruction
    assert repair.input_json["inherited_operator_instruction"] is True
    assert (
        repair.input_json["inherited_operator_instruction_source"]
        == "paused_stage"
    )
    assert (
        repair.input_json[
            "inherited_operator_instruction_source_stage_id"
        ]
        == paused.id
    )


def test_quality_recovery_prefers_full_operator_restart_instruction_over_stage_local_text(
    db_session,
    monkeypatch,
):
    binding_instruction = (
        "Every deliverable must open with a visible original conflict, keep "
        "fast pacing, preserve locked copy, and use native expressive audio."
    )
    project = HermesContentFactoryProject(
        project_key="cf_quality_restart_instruction",
        workspace_id=3,
        user_id=101,
        title="Project",
        product_name="Product",
        market="US",
        status="paused",
        current_stage="PRODUCTION_PLAN",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "production_plan_quality_pause",
            "automatic_quality_paused_at": "2026-07-23T01:00:00",
            "last_restart": {
                "stage": "SERIES_DIRECTOR",
                "instruction": binding_instruction,
            },
        },
    )
    db_session.add(project)
    db_session.flush()
    paused = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=101,
        stage="PRODUCTION_PLAN",
        attempt=1,
        status="failed",
        instruction="Build the production plan from approved copy.",
        completed_at=datetime(2026, 7, 23, 1, 0, 0),
    )
    db_session.add(paused)
    db_session.commit()

    monkeypatch.setattr(
        content_factory_tasks,
        "_automatic_quality_recovery_decision",
        lambda *_args, **_kwargs: {
            "due": True,
            "pause_reason_code": "production_plan_quality_pause",
            "attempt_count": 0,
            "attempt_limit": 2,
            "incident_key": "production-plan-incident",
            "source_stage_id": paused.id,
            "variant_index": 1,
        },
    )
    monkeypatch.setattr(
        "app.services.hermes_agent.content_factory.resume_project",
        lambda _db, value, **_kwargs: value,
    )

    repair, _decision = (
        content_factory_tasks._resume_automatic_quality_pause(
            db_session,
            project,
            now=datetime(2026, 7, 23, 1, 2, 0),
        )
    )

    assert repair is not None
    assert repair.instruction.startswith(binding_instruction)
    assert "Autonomy strategy:" in repair.instruction
    assert (
        repair.input_json["inherited_operator_instruction_source"]
        == "project_last_restart"
    )
    assert (
        repair.input_json[
            "inherited_operator_instruction_restart_stage"
        ]
        == "SERIES_DIRECTOR"
    )
    assert "inherited_operator_instruction_source_stage_id" not in (
        repair.input_json
    )


def test_production_plan_director_replan_feedback_routes_upstream():
    stage = HermesContentFactoryStage(
        id=2493,
        project_id=170,
        workspace_id=3,
        user_id=101,
        stage="PRODUCTION_PLAN",
        attempt=3,
        status="failed",
        error_message=(
            "the approved copy artifact requires a Director replan; "
            "the production-plan stage cannot rewrite it"
        ),
        output_json={
            "evidence": {
                "reviews": [{
                    "verdict": {
                        "repair_scope": "director_replan",
                        "blocking_issues": [{
                            "code": "UNCONFIRMED_SHIPPING_CLAIM",
                            "line_ids": ["LOCKED-VO-011"],
                            "evidence": "shipping qualifier missing",
                            "repair_instruction": "bind current offer truth",
                        }],
                    },
                }],
            },
        },
    )

    feedback = (
        content_factory_tasks._production_plan_director_replan_feedback(
            stage
        )
    )

    assert feedback == [{
        "code": "UNCONFIRMED_SHIPPING_CLAIM",
        "line_ids": ["LOCKED-VO-011"],
        "evidence": "shipping qualifier missing",
        "repair_instruction": "bind current offer truth",
    }]


def test_production_plan_contract_recovery_preserves_exact_prior_errors():
    stage = HermesContentFactoryStage(
        id=2602,
        project_id=176,
        workspace_id=3,
        user_id=6,
        stage="PRODUCTION_PLAN",
        attempt=6,
        status="failed",
        output_json={
            "result": {
                "contract_errors": [
                    "copy deliveries must be ordered by start time",
                    "copy delivery intents must cover every approved script "
                    "line exactly once and in order",
                    "copy deliveries must be ordered by start time",
                ],
            },
        },
    )

    assert content_factory_tasks._production_plan_contract_recovery_errors(
        stage
    ) == [
        "copy deliveries must be ordered by start time",
        "copy delivery intents must cover every approved script line exactly "
        "once and in order",
    ]


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
    monkeypatch.setattr(
        content_factory_tasks,
        "_next_visual_api_route",
        lambda *_args, **_kwargs: None,
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
