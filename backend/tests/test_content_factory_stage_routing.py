from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.data.models.hermes_agent import HermesContentFactoryProject
from app.services.hermes_agent.stage_routing import (
    clear_external_retry_barriers_for_local_stage,
    is_local_worker_stage,
    stage_execution_backend,
)


def test_final_assets_split_runs_locally_without_browser() -> None:
    assert is_local_worker_stage("FINAL_ASSETS") is True
    assert stage_execution_backend("FINAL_ASSETS") == "local"


def test_forced_final_assets_rebuild_still_uses_browser() -> None:
    stage_input = {"force_chatgpt_rebuild": True}
    assert is_local_worker_stage("FINAL_ASSETS", stage_input) is False
    assert stage_execution_backend("FINAL_ASSETS", stage_input=stage_input) == "browser"


def test_local_final_assets_discards_stale_external_retry_barriers() -> None:
    stage_input = {
        "retry_after": "2099-01-01T00:00:00",
        "recovery_api_probe_pending": True,
        "api_force_browser_fallback": True,
        "execution_backend": "browser",
        "signed_reference_manifest": {"reference_count": 4},
    }

    normalized = clear_external_retry_barriers_for_local_stage(
        "FINAL_ASSETS",
        stage_input,
    )

    assert normalized == {
        "signed_reference_manifest": {"reference_count": 4},
    }
    assert stage_input["retry_after"] == "2099-01-01T00:00:00"


def test_forced_final_assets_rebuild_keeps_external_retry_barriers() -> None:
    stage_input = {
        "force_chatgpt_rebuild": True,
        "retry_after": "2099-01-01T00:00:00",
        "api_force_browser_fallback": True,
    }

    assert clear_external_retry_barriers_for_local_stage(
        "FINAL_ASSETS",
        stage_input,
    ) == stage_input


def test_api_route_takes_precedence_over_local_policy() -> None:
    assert stage_execution_backend(
        "FINAL_ASSETS",
        api_route="provider:model",
    ) == "api"


def test_visual_preview_without_api_requires_browser() -> None:
    assert is_local_worker_stage("VISUAL_PREVIEW") is False
    assert stage_execution_backend("VISUAL_PREVIEW") == "browser"


def test_signed_segment_compile_can_never_activate_api_or_browser() -> None:
    assert is_local_worker_stage("VIDEO_PROMPTS") is True
    assert stage_execution_backend("VIDEO_PROMPTS") == "local"
    assert stage_execution_backend(
        "VIDEO_PROMPTS",
        api_route="toapis:text",
        stage_input={"api_force_browser_fallback": True},
    ) == "local"


def test_feasibility_replan_keeps_review_repair_and_provider_direction() -> None:
    from app.tasks.hermes_agent.content_factory_tasks import (
        _apply_ai_segment_execution_replan,
    )

    retry_input = {
        "service_provider": "doubao",
        "prompt": "\n".join([
            "Refs: @image1=story; @image2=package",
            "Beats: 0-3s: old action | 3-7s: old finish",
            "Dialogue: 'Exact locked copy.'",
        ]),
        "content_factory_dialogue_lines": [{
            "line_id": "l1",
            "speaker_id": "woman_1",
            "line": "Exact locked copy.",
            "delivery_method": "provider_dialogue",
        }],
        "content_factory_segment_execution_repair": {
            "source": "ai_feasibility_replan",
            "policy_version": "2026-08-05-prompt-authority-role-complete-anchors-v5",
            "repair_instruction": "obsolete feasibility note",
            "upstream_repair_policy_version": (
                "2026-08-05-segment-evidence-continuity-v4"
            ),
            "upstream_repair_instruction": (
                "Revise segment 3 to continue the same shoulder application "
                "from segment 2; do not change to leg application."
            ),
        },
    }
    replan = {
        "policy_version": "2026-08-05-provider-direction-complete-v6",
        "provider_visual_context_zh": "风格化2D成年女性，明亮普拉提馆",
        "provider_direction_zh": (
            "风格化2D；每0.8至1.5秒一次快切；微距切至肩部近景，禁止缓慢推进"
        ),
        "provider_instruction": "execute the new compact choreography",
        "timeline": [
            {
                "start_second": 0,
                "end_second": 3,
                "provider_action_zh": "打开罐子并快速取少量",
            },
            {
                "start_second": 3,
                "end_second": 7,
                "provider_action_zh": "在同一肩部按摩并停在吸收状态",
            },
        ],
    }

    result = _apply_ai_segment_execution_replan(retry_input, replan)

    assert "Direction: 风格化2D" in result["prompt"]
    assert "0.8至1.5秒" in result["prompt"]
    assert "Repair: keep shoulder application" in result["prompt"]
    assert "no leg change" in result["prompt"]
    assert "Dialogue: 'Exact locked copy.'" in result["prompt"]
    repair = result["content_factory_segment_execution_repair"]
    assert "shoulder application" in repair["upstream_repair_instruction"]


def test_signed_segment_compile_does_not_expose_waiting_without_task_ledger(
    monkeypatch,
) -> None:
    """Self-heal must not supersede a live multimodal prompt-authoring pass."""

    from app.services.hermes_agent import content_production_compiler
    from app.tasks.hermes_agent import content_factory_tasks

    artifact = SimpleNamespace(
        artifact_sha256="artifact-sha",
        script=SimpleNamespace(canonical_text_sha256="script-sha"),
    )
    plan = SimpleNamespace(plan_sha256="plan-sha")
    project = SimpleNamespace(
        id=185,
        project_key="cf_atomic_video_submit",
        status="running",
        current_stage="VIDEO_PROMPTS",
        config_json={"manual_paused": False},
        last_error=None,
    )
    stage = SimpleNamespace(
        id=3020,
        status="running",
        error_message=None,
        output_json=None,
        response_text=None,
        completed_at=None,
    )
    db = SimpleNamespace(commit=MagicMock())

    monkeypatch.setattr(
        content_factory_tasks,
        "_stage_variant_index",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_approved_production_plan_bundle_for_variant",
        lambda *_args, **_kwargs: {
            "artifact": artifact,
            "plan": plan,
            "production_plan_stage_id": 3008,
            "production_plan_audit_id": 93,
        },
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_video_model_policy",
        lambda _project: {"reference_limit": 10},
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_configured_video_resolution",
        lambda _project: "720p",
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_video_language_label",
        lambda _project: "English (US)",
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_project_uses_product",
        lambda _project: True,
    )
    monkeypatch.setattr(
        content_production_compiler,
        "compile_production_plan_to_video_result",
        lambda *_args, **_kwargs: {"videos": []},
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_normalize_video_plan",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_lock_stage_delivery_scope",
        lambda *_args, **_kwargs: (stage, project, project.id),
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_is_current_stage_delivery",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_latest_stage",
        lambda *_args, **_kwargs: stage,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_record_stage_quality_learning",
        lambda *_args, **_kwargs: None,
    )

    observed = {}

    def queue_tasks(_db, observed_project, _envelope, **_kwargs):
        observed["stage_status"] = stage.status
        observed["project_status"] = observed_project.status
        observed["project_stage"] = observed_project.current_stage
        observed["completed_at"] = stage.completed_at
        return [3217, 3218, 3219]

    monkeypatch.setattr(
        content_factory_tasks,
        "_queue_ai_video_tasks",
        queue_tasks,
    )

    result = content_factory_tasks._run_content_segment_compile_stage(
        db,
        stage_row=stage,
        project=project,
        request_id="task-id",
        delivery_run_token="run-token",
    )

    assert result["ai_video_task_ids"] == [3217, 3218, 3219]
    assert observed == {
        "stage_status": "running",
        "project_status": "running",
        "project_stage": "VIDEO_PROMPTS",
        "completed_at": None,
    }


def test_multimodal_reference_choice_drops_conflicting_still_but_keeps_product() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    refs = [
        {"index": 1, "is_product_anchor": False},
        {"index": 2, "is_product_anchor": False},
        {"index": 3, "is_product_anchor": False},
        {"index": 0, "is_product_anchor": True},
    ]

    indices, aliases = content_factory_tasks._authored_reference_selection(
        ["@image3"],
        refs,
        product_required=True,
    )

    assert indices == [3]
    assert aliases == ["@image3", "@image4"]


def test_copy_video_refs_promotes_character_anchor_to_shared_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    source = tmp_path / "character.png"
    source.write_bytes(b"clean-anchor")
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(
        content_factory_tasks.settings,
        "BANDIANWA_UPLOAD_STORAGE_DIR",
        str(upload_root),
    )
    project = SimpleNamespace(id=185, workspace_id=3)
    asset = SimpleNamespace(
        id=5791,
        file_path=str(source),
        original_name="character.png",
        mime_type="image/png",
        kind="visual_preview",
        stage="FINAL_ASSETS",
        meta_json={
            "asset_role": "visual_preview",
            "semantic_roles": ["character_anchor", "action_anchor"],
            "reference_segment": 1,
        },
    )

    refs = content_factory_tasks._copy_video_refs(project, [asset])

    assert refs[0]["reference_segment"] == 0
    assert refs[0]["semantic_roles"] == [
        "character_anchor",
        "action_anchor",
    ]


def test_text_to_video_skips_every_reference_image_stage() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = SimpleNamespace(
        config_json={"video_generation_mode": "text_to_video"},
    )

    assert content_factory_tasks._configured_video_generation_mode(project) == (
        "text_to_video"
    )
    assert content_factory_tasks._configured_next_stage(
        project,
        "PRODUCTION_PLAN",
    ) == "VIDEO_PROMPTS"


def test_text_to_video_empty_reference_set_is_complete_and_never_repaired() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = SimpleNamespace(
        config_json={"video_generation_mode": "text_to_video"},
    )

    assert content_factory_tasks._final_assets_are_complete(None, project) == (
        True,
        0,
        0,
    )
    assert content_factory_tasks._repair_final_assets_if_needed(
        None,
        project,
        reason="legacy final-assets guard",
    ) == (None, None)


def test_text_to_video_retry_cannot_inherit_reference_media() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = SimpleNamespace(
        config_json={"video_generation_mode": "text_to_video"},
    )
    image_file = SimpleNamespace(kind="reference_upload")
    video_file = SimpleNamespace(kind="reference_video_upload")
    result_file = SimpleNamespace(kind="result")

    normalized, files = content_factory_tasks._normalize_omni_retry_references(
        {
            "model": "omni_flash",
            "prompt": (
                "A quiet empty park path at dawn. Segment scope: 1/1; "
                "show only this segment."
            ),
            "content_factory_base_prompt": "A quiet empty park path at dawn.",
            "reference_file_paths": [{"path": "/tmp/stale.png"}],
            "reference_video_file_paths": [{"path": "/tmp/stale.mp4"}],
            "content_factory_reference_manifest": [{"asset_id": 99}],
            "content_factory_first_frame": True,
        },
        [image_file, video_file, result_file],
        set(),
        retry_attempt=2,
        project=project,
    )

    assert normalized["prompt"] == (
        "A quiet empty park path at dawn. Segment scope: 1/1; show only "
        "this segment."
    )
    assert normalized["reference_file_paths"] == []
    assert normalized["reference_video_file_paths"] == []
    assert normalized["content_factory_reference_manifest"] == []
    assert normalized["content_factory_first_frame"] is False
    assert normalized["content_factory_product_render_mode"] == "text_only"
    assert files == [result_file]


def test_text_to_video_plan_strips_model_authored_reference_requests() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = HermesContentFactoryProject(
        project_key="cf_text_to_video_test",
        workspace_id=1,
        user_id=1,
        title="Text to video",
        product_name="",
        market="US",
        status="ready",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_generation_mode": "text_to_video",
            "video_model": "omni_flash",
            "video_count": 1,
            "video_duration_min_seconds": 10,
            "video_duration_max_seconds": 10,
            "video_reference_limit": 7,
            "allow_reference_video": False,
            "product_required": False,
            "video_aspect_ratio": "9:16",
        },
        state_json={},
    )
    envelope = {
        "result": {
            "videos": [{
                "version_name": "v01",
                "duration_seconds": 10,
                "visual_style": "Stylized 2D adult editorial animation.",
                "visual_grammar": "Three rapid hard cuts, then a locked product close-up.",
                "segments": [{
                    "prompt": "An empty American park path at sunrise.",
                    "reference_indices": [1, 2],
                    "reference_ids": ["ref-1"],
                    "product_anchor_required": True,
                    "timeline": [{
                        "start_second": 0,
                        "end_second": 10,
                        "action": "Walk forward along the empty path.",
                    }],
                }],
            }],
        },
    }

    plan = content_factory_tasks._normalize_video_plan(project, envelope)
    segment = plan[0]["segments"][0]
    assert segment["reference_indices"] == []
    assert segment["reference_ids"] == []
    assert segment["product_anchor_required"] is False
    assert segment["product_render_mode"] == "none"


def test_normalized_video_plan_preserves_signed_intent_and_segment_contract() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = HermesContentFactoryProject(
        project_key="cf_signed_intent_passthrough",
        workspace_id=1,
        user_id=1,
        title="Signed intent passthrough",
        product_name="MYUPONA",
        market="US",
        status="ready",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_generation_mode": "image_to_video",
            "video_model": "omni_flash",
            "video_count": 1,
            "video_duration_min_seconds": 10,
            "video_duration_max_seconds": 10,
            "video_reference_limit": 7,
            "video_aspect_ratio": "9:16",
        },
        state_json={},
    )
    requirement = {
        "requirement_id": "R-001",
        "priority": "critical",
        "intent": "Use the authoritative product visual only.",
        "observable_checks": ["The package identity matches."],
    }
    execution = {
        "requirement_id": "R-001",
        "implementation": "Attach the product_visual authority.",
    }
    envelope = {
        "result": {
            "videos": [{
                "version_name": "v01",
                "duration_seconds": 10,
                "intent_manifest_sha256": "signed-manifest",
                "visual_style": "Stylized 2D adult editorial animation.",
                "visual_grammar": "Three rapid hard cuts, then a locked product close-up.",
                "intent_requirements": [requirement],
                "requirement_execution": [execution],
                "production_requirement_execution": [execution],
                "audio_mode": "spoken",
                "segments": [{
                    "prompt": "The actor naturally lifts the approved bottle.",
                    "reference_indices": [1],
                    "intent_manifest_sha256": "signed-manifest",
                    "requirement_ids": ["R-001"],
                    "requirement_contract": [requirement],
                    "continuity_dependency": "independent",
                    "voice_lock": [{
                        "speaker_id": "woman",
                        "gender": "female",
                    }],
                    "authoritative_product_composites": [{"asset_id": 42}],
                    "compile_source": "signed_production_plan",
                    "timeline": [{
                        "start_second": 0,
                        "end_second": 10,
                        "action": "Lift the bottle naturally.",
                    }],
                }],
            }],
        },
    }

    video = content_factory_tasks._normalize_video_plan(project, envelope)[0]
    segment = video["segments"][0]

    assert video["intent_manifest_sha256"] == "signed-manifest"
    assert video["intent_requirements"] == [requirement]
    assert video["requirement_execution"] == [execution]
    assert video["production_requirement_execution"] == [execution]
    assert video["audio_mode"] == "spoken"
    assert segment["visual_style"] == "Stylized 2D adult editorial animation."
    assert segment["visual_grammar"] == (
        "Three rapid hard cuts, then a locked product close-up."
    )
    assert segment["intent_manifest_sha256"] == "signed-manifest"
    assert segment["requirement_ids"] == ["R-001"]
    assert segment["requirement_contract"] == [requirement]
    assert segment["continuity_dependency"] == "independent"
    assert segment["voice_lock"] == [{
        "speaker_id": "woman",
        "gender": "female",
    }]
    assert segment["authoritative_product_composites"] == [{"asset_id": 42}]


def test_normalized_video_plan_preserves_long_authoring_prompt_for_semantic_compiler() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = HermesContentFactoryProject(
        project_key="cf_complete_authoring_prompt",
        workspace_id=1,
        user_id=1,
        title="Complete authoring prompt",
        product_name="MYUPONA",
        market="US",
        status="ready",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_generation_mode": "image_to_video",
            "video_model": "seedance_2_0_mini",
            "video_count": 1,
            "video_duration_min_seconds": 10,
            "video_duration_max_seconds": 10,
            "video_reference_limit": 10,
            "video_aspect_ratio": "9:16",
        },
        state_json={},
    )
    complete_authoring_prompt = "Detailed segment-local action and emotion. " * 60
    assert len(complete_authoring_prompt) > 1800
    envelope = {
        "result": {
            "videos": [{
                "duration_seconds": 10,
                "segments": [{
                    "prompt": complete_authoring_prompt,
                    "timeline": [{
                        "start_second": 0,
                        "end_second": 10,
                        "action": "She lowers the phone and turns toward the clock.",
                    }],
                }],
            }],
        },
    }

    segment = content_factory_tasks._normalize_video_plan(
        project,
        envelope,
    )[0]["segments"][0]

    assert segment["prompt"] == complete_authoring_prompt.strip()


def test_normalized_video_plan_does_not_reject_semantic_scope_words() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = HermesContentFactoryProject(
        project_key="cf_semantic_scope_words",
        workspace_id=1,
        user_id=1,
        title="Semantic scope words",
        product_name="MYUPONA",
        market="US",
        status="ready",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_generation_mode": "text_to_video",
            "video_model": "omni_flash",
            "video_count": 1,
            "video_duration_min_seconds": 10,
            "video_duration_max_seconds": 10,
            "video_aspect_ratio": "9:16",
        },
        state_json={},
    )
    prompt = (
        "This opening shot establishes the entire video's emotional theme; "
        "within this segment, hard-cut from a ringing clock to an alert face."
    )
    envelope = {
        "result": {
            "videos": [{
                "duration_seconds": 10,
                "segments": [{
                    "prompt": prompt,
                    "timeline": [{
                        "start_second": 0,
                        "end_second": 10,
                        "action": "Hard-cut from the clock to the reaction.",
                    }],
                }],
            }],
        },
    }

    segment = content_factory_tasks._normalize_video_plan(
        project,
        envelope,
    )[0]["segments"][0]

    assert segment["prompt"] == prompt


def test_legacy_media_group_recovers_contract_from_exact_source_stage() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = HermesContentFactoryProject(
        id=19,
        project_key="cf_contract_recovery",
        workspace_id=1,
        user_id=1,
        title="Contract recovery",
        product_name="MYUPONA",
        market="US",
        status="ready",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={
            "video_generation_mode": "image_to_video",
            "video_model": "omni_flash",
            "video_count": 1,
            "video_duration_min_seconds": 10,
            "video_duration_max_seconds": 10,
            "video_reference_limit": 7,
            "video_aspect_ratio": "9:16",
        },
        state_json={},
    )
    requirement = {
        "requirement_id": "R-001",
        "priority": "critical",
        "intent": "Use only the authoritative package image.",
    }
    stage = SimpleNamespace(
        id=88,
        project_id=19,
        stage="VIDEO_PROMPTS",
        status="success",
        output_json={
            "result": {
                "videos": [{
                    "version_name": "v01",
                    "duration_seconds": 10,
                    "intent_manifest_sha256": "signed-manifest",
                    "intent_requirements": [requirement],
                    "requirement_execution": [{"requirement_id": "R-001"}],
                    "production_requirement_execution": [{
                        "requirement_id": "R-001"
                    }],
                    "audio_mode": "spoken",
                    "segments": [{
                        "prompt": "The actor naturally lifts the approved bottle.",
                        "intent_manifest_sha256": "signed-manifest",
                        "requirement_ids": ["R-001"],
                        "requirement_contract": [requirement],
                        "voice_lock": [{
                            "speaker_id": "woman",
                            "gender": "female",
                        }],
                        "compile_source": "signed_production_plan",
                        "timeline": [{
                            "start_second": 0,
                            "end_second": 10,
                            "action": "Lift the approved bottle.",
                        }],
                    }],
                }],
            },
        },
    )
    db = MagicMock()
    db.get.return_value = stage

    restored = content_factory_tasks._restore_media_group_signed_contract(
        db,
        project,
        {
            "video_index": 1,
            "source_stage_id": 88,
            "segments": [{"segment_index": 1, "task_id": 7001}],
        },
    )

    assert restored["intent_manifest_sha256"] == "signed-manifest"
    assert restored["audio_mode"] == "spoken"
    assert restored["intent_requirements"] == [requirement]
    assert restored["segments"][0]["requirement_ids"] == ["R-001"]
    assert restored["segments"][0]["requirement_contract"] == [requirement]
    assert restored["segments"][0]["voice_lock"] == [{
        "speaker_id": "woman",
        "gender": "female",
    }]
    assert restored["signed_contract_recovered_from_stage_id"] == 88


def test_final_intent_guardian_fails_closed_when_signed_contract_is_lost(
    tmp_path,
) -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = SimpleNamespace(
        config_json={
            "producer_intent_spec": {
                "intent_manifest": {
                    "requirements": [{
                        "requirement_id": "R-001",
                        "priority": "critical",
                    }],
                },
            },
        },
    )

    result = content_factory_tasks._final_intent_fidelity_report(
        MagicMock(),
        project,
        tmp_path / "not-read.mp4",
        group={"intent_requirements": []},
        source_diff={"status": "PASS"},
    )

    assert result["status"] == "fail"
    assert result["blocking"] is True
    assert result["blocking_requirement_ids"] == ["R-001"]
    assert result["repair_scope"] == "contract_recovery"


def test_cross_deliverable_differentiation_is_not_a_single_video_gate() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    requirement = {
        "requirement_id": "R-series",
        "kind": "differentiation",
        "scope": "project",
        "deliverable_ordinals": [1, 2, 3],
    }

    assert (
        content_factory_tasks._is_cross_deliverable_intent_requirement(
            requirement
        )
        is True
    )
    assert (
        content_factory_tasks._is_cross_deliverable_intent_requirement({
            **requirement,
            "kind": "visual",
        })
        is False
    )
    assert (
        content_factory_tasks._is_cross_deliverable_intent_requirement({
            **requirement,
            "kind": "objective",
        })
        is True
    )
    assert (
        content_factory_tasks._is_cross_deliverable_intent_requirement({
            **requirement,
            "deliverable_ordinals": [1],
        })
        is False
    )


def test_signed_display_copy_never_becomes_provider_dialogue(
    monkeypatch,
) -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    project = HermesContentFactoryProject(
        project_key="cf_copy_lane_test",
        workspace_id=1,
        user_id=1,
        title="Copy lane test",
        product_name="MYUPONA",
        market="US",
        status="ready",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_generation_mode": "image_to_video",
            "video_model": "omni_flash",
            "video_count": 1,
            "video_duration_min_seconds": 10,
            "video_duration_max_seconds": 10,
            "video_reference_limit": 7,
            "video_aspect_ratio": "9:16",
        },
        state_json={},
    )
    spoken = {
        "line_id": "voice-1",
        "speaker_id": "narrator",
        "speaker": "narrator",
        "line": "Check what the label lists.",
        "delivery_mode": "spoken",
    }
    display = {
        "line_id": "overlay-1",
        "speaker_id": "narrator",
        "speaker": "narrator",
        "line": "TIRED. STILL THINKING.",
        "delivery_mode": "display",
        "delivery_method": "local_overlay",
    }
    monkeypatch.setattr(
        content_factory_tasks,
        "_creative_blueprint_for_variant",
        lambda _project, _variant: (
            {
                "audio_mode": "spoken",
                "segments": [{
                    "segment_index": 1,
                    "story_function": "Open with the problem.",
                    "dialogue_lines": [spoken, display],
                }],
            },
            {"speakers": []},
        ),
    )
    envelope = {
        "result": {
            "videos": [{
                "duration_seconds": 10,
                "segments": [{
                    "prompt": "A quiet animated bedroom.",
                    "timeline": [{
                        "start_second": 0,
                        "end_second": 10,
                        "action": "Abstract thought shapes multiply.",
                    }],
                    "dialogue_lines": [spoken],
                    "display_lines": [display],
                }],
            }],
        },
    }

    segment = content_factory_tasks._normalize_video_plan(
        project,
        envelope,
    )[0]["segments"][0]
    assert [row["line_id"] for row in segment["dialogue_lines"]] == [
        "voice-1"
    ]
    assert [row["line_id"] for row in segment["display_lines"]] == [
        "overlay-1"
    ]
    prompt = content_factory_tasks._compact_provider_segment_prompt(
        segment,
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
    )
    assert "Check what the label lists." in prompt
    assert "TIRED. STILL THINKING." not in prompt


def test_retry_sanitizes_display_copy_from_frozen_provider_prompt() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    fixed = content_factory_tasks._sanitize_content_task_copy_delivery({
        "prompt": (
            "Dialogue: narrator: 'Speak this.' | narrator: 'DISPLAY ONLY'\n"
            "Voice lock: same narrator."
        ),
        "content_factory_base_prompt": (
            "Dialogue: narrator: 'Speak this.' | narrator: 'DISPLAY ONLY'\n"
            "Voice lock: same narrator."
        ),
        "content_factory_dialogue_lines": [
            {
                "line_id": "voice-1",
                "speaker": "narrator",
                "line": "Speak this.",
                "delivery_mode": "spoken",
            },
            {
                "line_id": "overlay-1",
                "speaker": "narrator",
                "line": "DISPLAY ONLY",
            },
        ],
        "content_factory_display_lines": [{
            "line_id": "overlay-1",
            "line": "DISPLAY ONLY",
            "delivery_mode": "display",
        }],
    })

    assert [
        row["line_id"]
        for row in fixed["content_factory_dialogue_lines"]
    ] == ["voice-1"]
    assert "DISPLAY ONLY" not in fixed["prompt"]
    assert "DISPLAY ONLY" not in fixed["content_factory_base_prompt"]
    assert fixed["content_factory_display_lines"][0]["line_id"] == (
        "overlay-1"
    )
    assert fixed["content_factory_copy_delivery_repaired"] is True


def test_spoken_copy_retry_uses_dynamic_executable_rate_and_replaces_old_clause() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    exact = (
        "MYUPONA Sleep Easy Gummies: Melatonin-free, just what I wanted. "
        "Blueberry flavor, two gummies per serving, L-Theanine, GABA, "
        "Magnesium Glycinate. Tap TikTok Shop to view."
    )
    fixed = content_factory_tasks._apply_exact_spoken_copy_retry_prompt(
        {
            "seconds": 10,
            "prompt": (
                "COPY DELIVERY REPAIR (old): stale instruction\n"
                "Dialogue: woman: '" + exact + "'\nVoice lock: 150 WPM."
            ),
            "content_factory_base_prompt": "Dialogue: woman: '" + exact + "'",
        },
        exact,
        missing_tokens=["melatonin", "free"],
    )

    assert fixed["prompt"].startswith(
        "COPY DELIVERY REPAIR (highest audio priority):"
    )
    assert fixed["prompt"].count("COPY DELIVERY REPAIR (") == 1
    assert "approximately 180 words per minute" in fixed["prompt"]
    assert "Voice lock: 180 WPM." in fixed["prompt"]
    assert exact in fixed["prompt"]
    assert "melatonin, free" in fixed["prompt"]
    assert "words before the first colon are spoken dialogue" in fixed["prompt"]
    assert 'first say exactly: "MYUPONA Sleep Easy Gummies."' in fixed["prompt"]
    assert 'continue exactly with: "Melatonin-free' in fixed["prompt"]
    assert "Stop speaking immediately after the exact final word" in fixed["prompt"]
    assert fixed["content_factory_copy_delivery_target_wpm"] == 180


def test_spoken_copy_retry_does_not_add_opening_split_without_leading_colon() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    fixed = content_factory_tasks._apply_exact_spoken_copy_retry_prompt(
        {"seconds": 10, "prompt": "Voice lock: 150 WPM."},
        "This sentence has no leading metadata separator.",
    )

    assert "words before the first colon are spoken dialogue" not in fixed["prompt"]
    assert "This sentence has no leading metadata separator." in fixed["prompt"]


def test_spoken_copy_retry_never_slows_signed_short_hook_voice() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    fixed = content_factory_tasks._apply_exact_spoken_copy_retry_prompt(
        {
            "seconds": 4,
            "prompt": "Voice lock: 220 WPM. Final second is silent.",
            "content_factory_base_prompt": (
                "Voice lock: 220 WPM. Final second is silent."
            ),
            "content_factory_voice_lock": [{
                "speaker_id": "speaker_1",
                "speech_rate": 220,
                "speech_rate_unit": "words_per_minute",
            }],
        },
        "My body clocked out. My brain started a shift.",
    )

    assert fixed["content_factory_copy_delivery_target_wpm"] == 220
    assert "approximately 220 words per minute" in fixed["prompt"]
    assert "Voice lock: 220 WPM." in fixed["prompt"]


def test_local_voiceover_keeps_visual_motion_but_not_provider_speech() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    prompt = content_factory_tasks._compact_provider_segment_prompt(
        {
            "compile_source": "signed_production_plan",
            "prompt": "Segment 1: animated late-scroll hook",
            "timeline": [{
                "start_second": 0,
                "end_second": 4,
                "action": "A generic bedroom preamble. Phone shards multiply and snap toward her eyes.",
                "subject_action": "Phone shards multiply and snap toward her eyes.",
                "motion_and_transition": "Rapid scale distortion, then a hard snap.",
                "camera": "Fast push-in to her eyes.",
            }],
            "dialogue_lines": [{
                "line_id": "hook",
                "speaker": "female_narrator",
                "line": "One more video was forty-three videos ago.",
                "delivery_mode": "spoken",
                "delivery_method": "local_voiceover",
            }],
            "voice_lock": [{
                "speaker_id": "female_narrator",
                "gender": "female",
                "accent": "US English",
            }],
        },
        resolution="720p",
        aspect_ratio="9:16",
        language_label="English (US)",
        requirement_contract=[],
    )

    assert "Phone shards multiply" in prompt
    assert "Rapid scale distortion" in prompt
    assert "Fast push-in" in prompt
    assert "generic bedroom preamble" not in prompt
    assert "One more video" not in prompt
    assert "Voice lock for this segment" not in prompt
    assert "visible characters remain silent with no lip-sync" in prompt
    assert "voiceover is added during local post-production" in prompt


def test_signed_provider_prompt_does_not_pretruncate_timed_execution() -> None:
    from app.services.ai_video.prompt_budget import (
        compact_structured_video_prompt,
        validate_structured_video_prompt_fidelity,
    )
    from app.tasks.hermes_agent import content_factory_tasks

    middle_action = (
        "Continuation of the same night bedroom, now transformed locally "
        "around the pillow and bedside table into a tiny improvised "
        "boardroom with miniature folders and gesturing suited figures. "
        "The woman props herself up in startled disbelief as more miniature "
        "workers crowd the pillow edge, point at blank folders, and silently "
        "argue around a tiny table."
    )
    middle_camera = (
        "Rapid alternating medium close-up of the woman and low macro angle "
        "across the pillow-level meeting table; keep her face and the tiny "
        "arguing figures in the same visual geography."
    )
    semantic = content_factory_tasks._compact_provider_segment_prompt(
        {
            "compile_source": "signed_production_plan",
            "prompt": "Segment 1: immediate recognition hook",
            "pacing": (
                "Start on the impossible visual in the first frame, escalate "
                "through rapid reaction cuts, then stop on a silent freeze."
            ),
            "timeline": [{
                "start_second": 0.0,
                "end_second": 0.6,
                "action": "Her eyes snap open as three tiny workers erupt from the pillow.",
                "motion_and_transition": "Crash zoom and cut on the first landing.",
                "camera": "Extreme vertical close-up at pillow height.",
            }, {
                "start_second": 0.6,
                "end_second": 3.0,
                "action": middle_action,
                "motion_and_transition": "Two brisk reaction cuts as the meeting crowds in.",
                "camera": middle_camera,
            }, {
                "start_second": 3.0,
                "end_second": 4.0,
                "action": "Every worker freezes and she gives camera a wordless stare.",
                "motion_and_transition": "Hold the final comedic tableau without dialogue.",
                "camera": "Centered tight medium close-up.",
            }],
            "dialogue_lines": [{
                "line_id": "hook",
                "speaker_id": "woman",
                "line": "You close your eyes—and your brain calls an emergency meeting.",
                "delivery_method": "provider_dialogue",
            }],
            "voice_lock": [{
                "speaker_id": "woman",
                "gender": "female",
                "accent": "General American English",
                "screen_relation": "on_screen_character",
                "speech_rate": 220,
            }],
        },
        resolution="720p",
        aspect_ratio="9:16",
        language_label="English (US)",
        requirement_contract=[],
    )
    actual = compact_structured_video_prompt(
        semantic,
        max_characters=12000,
    )

    assert middle_action in actual
    assert middle_camera in actual
    assert "0.6-3s:" in actual
    assert "emergency meeting" in actual
    validate_structured_video_prompt_fidelity(semantic, actual)


def test_seedance_small_budget_keeps_model_authored_opening_motion_before_context() -> None:
    from app.services.ai_video.prompt_budget import compact_structured_video_prompt
    from app.tasks.hermes_agent import content_factory_tasks

    semantic = content_factory_tasks._compact_provider_segment_prompt(
        {
            "compile_source": "signed_production_plan",
            "prompt": "Segment 1: travel-day interruption",
            "visual_style": "Clean 2.5D animated hotel-night lifestyle realism.",
            "timeline": [{
                "start_second": 0,
                "end_second": 2.4,
                "action": (
                    "成年女性旅客拖箱进门，行李突然落地，她呼气；镜头立即甩向床头。"
                    "；场景：2.5D动画成年女性旅客，夜间暖灯酒店房间，含门口和床角"
                ),
                "camera": "快速推进后甩镜到床头。",
            }, {
                "start_second": 2.4,
                "end_second": 7,
                "action": "产品罐落在床头柜，快速对焦标签；她开罐并开始轻柔按摩前臂。",
                "camera": "产品和手部近景。",
                "dialogue_key": "v5.l1",
            }],
            "dialogue_lines": [{
                "line_id": "v5.l1",
                "speaker_id": "vo",
                "line": "Travel day has me ready for a reset.",
                "delivery_method": "provider_dialogue",
            }],
            "voice_lock": [{
                "speaker_id": "vo",
                "gender": "female",
                "accent": "General American",
                "screen_relation": "character_voiceover",
            }],
        },
        resolution="720p",
        aspect_ratio="9:16",
        language_label="English (US)",
        requirement_contract=[],
        product_required=True,
        product_name="MYUPONA Soothing Body Balm",
        product_presentation_policy={
            "authority_mode": "uploaded_source_only",
        },
    )
    first_pass = compact_structured_video_prompt(
        semantic,
        max_characters=495,
    )
    with_refs = content_factory_tasks._seedance_reference_prompt(
        first_pass,
        [{
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        }, {
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        }],
    )
    actual = compact_structured_video_prompt(
        with_refs,
        max_characters=495,
    )

    assert "行李突然落地" in actual
    assert "镜头立即甩向床头" in actual
    assert "产品罐落在床头柜" in actual
    assert "@image1" in actual and "@image2" in actual
    assert len(actual) <= 495


def test_timeline_normalizer_preserves_ai_authored_visual_lanes() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    rows = content_factory_tasks._normalize_segment_timeline(
        [{
            "start_seconds": 0,
            "end_seconds": 3,
            "action": "Room context followed by a portal hook.",
            "subject_action": "Phone portal pulls luminous shards.",
            "motion_and_transition": "Shards multiply then snap to her eyes.",
            "environment": "Blue-black animated bedroom.",
            "camera": "Rapid vertical push-in.",
        }],
        segment_duration=3,
        segment_offset=0,
        video_index=1,
        segment_index=1,
    )

    assert rows[0]["subject_action"] == "Phone portal pulls luminous shards."
    assert rows[0]["motion_and_transition"] == "Shards multiply then snap to her eyes."
    assert rows[0]["environment"] == "Blue-black animated bedroom."


def test_dialogue_normalizer_preserves_signed_local_delivery_lane() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    rows = content_factory_tasks._spoken_dialogue_lines([{
        "line_id": "hook",
        "speaker_id": "female_narrator",
        "speaker": "Narrator",
        "line": "Exact words.",
        "delivery_mode": "spoken",
        "delivery_method": "local_voiceover",
        "start_seconds": 0.0,
        "end_seconds": 1.5,
    }])

    assert rows == [{
        "line_id": "hook",
        "speaker_id": "female_narrator",
        "speaker": "Narrator",
        "line": "Exact words.",
        "delivery_mode": "spoken",
        "delivery_method": "local_voiceover",
        "start_seconds": 0.0,
        "end_seconds": 1.5,
    }]


def test_frozen_local_voiceover_retry_removes_only_audio_lane() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    prompt = content_factory_tasks._prompt_without_local_voiceover_delivery(
        "Timeline (this segment only): 0-4s: phone shards multiply.\n"
        "Motion and effects: hard snap toward her eyes.\n"
        "Dialogue: female: 'Exact words.'\n"
        "Voice lock for this segment: female, US accent.\n"
        "Continuity: warm bedside transition."
    )

    assert "phone shards multiply" in prompt
    assert "hard snap toward her eyes" in prompt
    assert "warm bedside transition" in prompt
    assert "Exact words" not in prompt
    assert "Voice lock" not in prompt
    assert "Exact signed voiceover is added locally" in prompt


def test_explicit_browser_fallback_wins_over_available_api_route() -> None:
    assert stage_execution_backend(
        "CREATIVE",
        api_route="toapis:text",
        stage_input={"api_fallback_to_browser": True},
    ) == "browser"
    assert stage_execution_backend(
        "VISUAL_PREVIEW",
        api_route="bandianwa:gpt-image-2",
        stage_input={"visual_api_force_browser_fallback": True},
    ) == "browser"
    assert stage_execution_backend(
        "CREATIVE",
        api_route="toapis:text",
        stage_input={"api_force_browser_fallback": True},
    ) == "browser"


def test_visual_progress_checkpoint_restamps_current_policy() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    class CommitRecorder:
        commits = 0

        def commit(self) -> None:
            self.commits += 1

    db = CommitRecorder()
    stage = SimpleNamespace(input_json=None)
    inherited_input = {
        "self_heal_policy_version": 49,
        "hermes_learning_policy_version": "old-policy",
    }

    content_factory_tasks._commit_visual_api_progress(
        db,
        stage_row=stage,
        stage_input=inherited_input,
        api_state={"status": "submitted"},
    )

    assert db.commits == 1
    assert stage.input_json["self_heal_policy_version"] == (
        content_factory_tasks.SELF_HEAL_POLICY_VERSION
    )
    assert stage.input_json["hermes_learning_policy_version"] == (
        content_factory_tasks.HERMES_LEARNING_POLICY_VERSION
    )
    assert stage.input_json["visual_api"] == {"status": "submitted"}


def test_provider_exception_retry_restamps_current_policy() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    refreshed = content_factory_tasks._restamp_stage_runtime_policy({
        "self_heal_policy_version": 49,
        "hermes_learning_policy_version": "old-policy",
        "automatic_retry_count": 3,
    })

    assert refreshed["self_heal_policy_version"] == (
        content_factory_tasks.SELF_HEAL_POLICY_VERSION
    )
    assert refreshed["hermes_learning_policy_version"] == (
        content_factory_tasks.HERMES_LEARNING_POLICY_VERSION
    )
    assert refreshed["automatic_retry_count"] == 3
