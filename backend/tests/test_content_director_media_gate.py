from __future__ import annotations

import pytest

from app.data.models.hermes_agent import (
    HermesContentDirectorArtifact,
    HermesContentDirectorBrief,
    HermesContentFactoryProject,
    HermesContentFactoryAsset,
    HermesContentFactoryStage,
    HermesContentProductionPlanAudit,
)
from app.services.hermes_agent.content_director import (
    ConversionIntent,
    CopyReviewCriterion,
    DirectorCapabilityNode,
    ScriptLine,
    ScriptSegmentAllocation,
    VideoProgramSpec,
    build_directed_content_artifact,
    build_script_package,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _approved_director_artifact_for_variant,
    _assert_director_media_authorization,
    _assert_director_script_lock,
    _director_locked_creative_result,
)
from app.tasks.hermes_agent import content_factory_tasks
from app.services.hermes_agent.content_factory import (
    _promote_approved_paused_control_stage,
    resume_project,
)
from app.services.hermes_agent.content_production_compiler import (
    compile_production_plan_for_media,
)
from app.services.hermes_agent.content_production_plan import (
    AudioProgramDraft,
    CopyDeliveryProgramDraft,
    DirectorProductionPlanDraft,
    SpeakerVoiceIntent,
    TimedScriptDelivery,
    VisualBeat,
    VisualProgramDraft,
    VisualReferenceIntent,
    finalize_director_production_plan,
)


def _artifact():
    criterion = CopyReviewCriterion(
        criterion_id="clarity",
        instruction="The audience understands every line on first listen.",
        minimum_score=80,
    )
    program = VideoProgramSpec(
        program_id="program-1",
        objective="Explain one useful idea.",
        content_type="animated explainer",
        platform="TikTok",
        locale="en-US",
        audience="US adults.",
        target_duration_seconds=20,
        aspect_ratio="9:16",
        conversion=ConversionIntent(product_required=False),
        execution_graph=[
            DirectorCapabilityNode(
                node_id="copy",
                capability="copy.write",
                input_contract="VideoProgramSpec",
                output_contract="ScriptPackage",
                policy={"media_spend": False},
            )
        ],
        copy_review_criteria=[criterion],
    )
    script = build_script_package(
        script_id="script-1",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=20,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="line-1",
                speaker_id="narrator",
                text="The first line remains exactly as approved.",
                beat_id="beat-1",
                purpose="opening",
            ),
            ScriptLine(
                line_id="line-2",
                speaker_id="narrator",
                text="The second line completes the explanation.",
                beat_id="beat-2",
                purpose="resolution",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["line-1"],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=10,
                line_ids=["line-2"],
            ),
        ],
    )
    return build_directed_content_artifact(
        artifact_id="artifact-1",
        revision=1,
        program=program,
        script=script,
    )


def _creative_input() -> dict:
    return {
        "complete_video_script": {
            "duration_seconds": 20,
            "target_edit_duration_seconds": 18,
            "story_outline": {
                "opening": "Set up the idea.",
                "development": "Show why it matters.",
                "resolution": "Complete the explanation.",
            },
            "segments": [
                {
                    "segment_index": 1,
                    "duration_seconds": 10,
                    "story_function": "opening",
                    "visual_action": "A lever appears beside a heavy box.",
                    "dialogue_lines": [
                        {
                            "speaker_id": "narrator",
                            "line": "A model tried to rewrite this sentence.",
                        }
                    ],
                },
                {
                    "segment_index": 2,
                    "duration_seconds": 10,
                    "story_function": "resolution",
                    "visual_action": "The lever lifts the box.",
                    "dialogue_lines": [
                        {
                            "speaker_id": "narrator",
                            "line": "This sentence was also rewritten.",
                        }
                    ],
                },
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "narrator",
            "speakers": [
                {
                    "speaker_id": "narrator",
                    "name": "Narrator",
                    "gender": "neutral adult",
                    "timbre": "clear",
                    "pitch": "medium",
                    "accent": "en-US",
                    "delivery": "natural",
                    "speech_rate": 150,
                }
            ],
        },
    }


def test_director_lock_replaces_model_copy_but_preserves_production_design():
    artifact = _artifact()
    project = HermesContentFactoryProject(
        id=168,
        project_key="cf-director-lock",
        workspace_id=1,
        user_id=None,
        title="Director lock",
        product_name="",
        status="ready",
        current_stage="CREATIVE",
        config_json={"content_director_mode": "enforce"},
        state_json={},
    )

    locked = _director_locked_creative_result(
        project,
        _creative_input(),
        artifact=artifact,
    )

    segments = locked["complete_video_script"]["segments"]
    assert segments[0]["visual_action"] == "A lever appears beside a heavy box."
    assert segments[0]["dialogue_lines"][0] == {
        "line_id": "line-1",
        "speaker_id": "narrator",
        "speaker": "narrator",
        "line": "The first line remains exactly as approved.",
    }
    _assert_director_script_lock(project, locked, artifact=artifact)

    segments[1]["dialogue_lines"][0]["line"] = "Changed after approval."
    with pytest.raises(ValueError, match="TEXT_MISMATCH"):
        _assert_director_script_lock(project, locked, artifact=artifact)


def _production_plan(artifact):
    draft = DirectorProductionPlanDraft(
        visual=VisualProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=20,
            aspect_ratio="9:16",
            style_language="Simple adult animation.",
            visual_grammar="One continuous explanation.",
            references=[
                VisualReferenceIntent(
                    reference_id="explanation",
                    roles=["scene", "action"],
                    purpose="Keep the explanation visually continuous.",
                    generation_brief="Create one reusable explanation scene.",
                )
            ],
            beats=[
                VisualBeat(
                    beat_id="complete",
                    start_seconds=0,
                    end_seconds=20,
                    line_ids=["line-1", "line-2"],
                    purpose="Explain and resolve one idea.",
                    environment="One uncluttered adult workspace.",
                    subject_action="A lever lifts one heavy box.",
                    camera_composition="Stable medium composition.",
                    motion_and_transition="One continuous lift.",
                    continuity_state="Keep the box and lever unchanged.",
                    reference_ids=["explanation"],
                )
            ],
        ),
        audio=AudioProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=20,
            audio_mode="spoken",
            voices=[
                SpeakerVoiceIntent(
                    speaker_id="narrator",
                    identity="One adult US narrator.",
                    delivery_direction="Clear and natural.",
                    continuity_rule="Use the same voice throughout.",
                )
            ],
            cues=[],
            mix_intent="Keep the narration clear.",
        ),
        copy_delivery=CopyDeliveryProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=20,
            deliveries=[
                TimedScriptDelivery(
                    line_id="line-1",
                    start_seconds=0,
                    end_seconds=8,
                    method="local_voiceover",
                    speaker_id="narrator",
                ),
                TimedScriptDelivery(
                    line_id="line-2",
                    start_seconds=10,
                    end_seconds=18,
                    method="local_voiceover",
                    speaker_id="narrator",
                ),
            ],
        ),
    )
    return finalize_director_production_plan(
        draft,
        artifact,
        plan_id="plan-1",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs=set(),
        authoritative_product_asset_refs=set(),
    )


def test_media_gate_requires_audited_plan_and_exact_script_lock(db_session):
    artifact = _artifact()
    project = HermesContentFactoryProject(
        id=168,
        project_key="cf-director-media-gate",
        workspace_id=1,
        user_id=None,
        title="Director media gate",
        product_name="",
        status="ready",
        current_stage="VISUAL_PREVIEW",
        config_json={"content_director_mode": "enforce"},
        state_json={},
    )
    db_session.add(project)
    db_session.flush()

    audit_brief = HermesContentDirectorBrief(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        brief_key="brief-1",
        variant_index=1,
        brief_version=1,
        mode="enforce",
        status="approved",
        brief_sha256="a" * 64,
        brief_json={"brief_id": "brief-1"},
    )
    db_session.add(audit_brief)
    db_session.flush()
    db_session.add(
        HermesContentDirectorArtifact(
            brief_id=audit_brief.id,
            project_id=project.id,
            workspace_id=project.workspace_id,
            user_id=project.user_id,
            variant_index=1,
            artifact_key=artifact.artifact_id,
            revision=artifact.revision,
            parent_artifact_sha256=None,
            artifact_sha256=artifact.artifact_sha256,
            accepted=True,
            artifact_json=artifact.model_dump(mode="json"),
        )
    )
    director_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="DIRECTOR",
        attempt=1,
        status="success",
        input_json={"variant_index": 1},
        output_json={
            "content_factory_variant_index": 1,
            "result": {
                "director_artifact": artifact.model_dump(mode="json"),
            },
        },
    )
    db_session.add(director_stage)
    db_session.flush()
    valid_pointer = {
        "variant_index": 1,
        "artifact_sha256": artifact.artifact_sha256,
        "script_sha256": artifact.script.canonical_text_sha256,
        "director_stage_id": director_stage.id,
    }
    project.state_json = {
        "approved_director_artifacts_by_variant": {
            "1": valid_pointer,
        },
        "approved_director_artifact": {
            "variant_index": 2,
            "artifact_sha256": "f" * 64,
            "script_sha256": "e" * 64,
            "director_stage_id": 999999,
        },
    }

    plan = _production_plan(artifact)
    compiled = compile_production_plan_for_media(
        artifact,
        plan,
        asset_registry={},
    )
    plan_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="PRODUCTION_PLAN",
        attempt=1,
        status="success",
        input_json={"variant_index": 1},
        output_json={
            "content_factory_variant_index": 1,
            "result": {
                "production_plan": plan.model_dump(mode="json"),
                "compiled_media_design": compiled,
            },
        },
    )
    db_session.add(plan_stage)
    db_session.flush()
    audit = HermesContentProductionPlanAudit(
        project_id=project.id,
        stage_id=plan_stage.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        variant_index=1,
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        director_artifact_sha256=artifact.artifact_sha256,
        plan_sha256=plan.plan_sha256,
        status="approved",
        accepted=True,
        plan_json=plan.model_dump(mode="json"),
        attempts_json=[],
        critic_attempts_json=[],
        reviews_json=[],
        contract_errors_json=[],
        reason="approved",
    )
    db_session.add(audit)
    db_session.flush()
    plan_pointer = {
        "variant_index": 1,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "director_artifact_sha256": artifact.artifact_sha256,
        "production_plan_stage_id": plan_stage.id,
        "audit_row_id": audit.id,
    }
    project.state_json = {
        **dict(project.state_json or {}),
        "approved_production_plans_by_variant": {"1": plan_pointer},
        "approved_production_plan": plan_pointer,
    }
    db_session.commit()

    loaded = _approved_director_artifact_for_variant(
        db_session,
        project,
        variant_index=1,
    )
    assert loaded.artifact_sha256 == artifact.artifact_sha256
    authorization = _assert_director_media_authorization(
        db_session,
        project,
        variant_index=1,
    )
    assert authorization["production_plan_stage_id"] == plan_stage.id
    assert authorization["production_plan_sha256"] == plan.plan_sha256
    assert (
        authorization["director_script_sha256"]
        == artifact.script.canonical_text_sha256
    )

    damaged = dict(plan_stage.output_json or {})
    damaged_result = dict(damaged.get("result") or {})
    damaged_compiled = dict(
        damaged_result.get("compiled_media_design") or {}
    )
    damaged_script = dict(
        damaged_compiled.get("complete_video_script") or {}
    )
    damaged_segments = [
        dict(item) for item in list(damaged_script.get("segments") or [])
    ]
    damaged_dialogue = [
        dict(item)
        for item in list(damaged_segments[0].get("dialogue_lines") or [])
    ]
    damaged_dialogue[0]["line"] = "Tampered."
    damaged_segments[0]["dialogue_lines"] = damaged_dialogue
    damaged_script["segments"] = damaged_segments
    damaged_compiled["complete_video_script"] = damaged_script
    damaged_result["compiled_media_design"] = damaged_compiled
    damaged["result"] = damaged_result
    plan_stage.output_json = damaged
    db_session.add(plan_stage)
    db_session.commit()
    with pytest.raises(ValueError, match="TEXT_MISMATCH"):
        _assert_director_media_authorization(
            db_session,
            project,
            variant_index=1,
        )


def test_resume_promotes_audited_paused_production_plan_without_replanning(
    db_session,
):
    artifact = _artifact()
    plan = _production_plan(artifact)
    compiled = compile_production_plan_for_media(
        artifact,
        plan,
        asset_registry={},
    )
    project = HermesContentFactoryProject(
        project_key="cf-paused-plan-promotion",
        workspace_id=1,
        user_id=None,
        title="Paused plan",
        product_name="",
        status="paused",
        current_stage="PRODUCTION_PLAN",
        config_json={
            "content_director_mode": "enforce",
            "manual_paused": True,
        },
        state_json={
            "approved_director_artifacts_by_variant": {
                "1": {
                    "variant_index": 1,
                    "artifact_sha256": artifact.artifact_sha256,
                    "script_sha256": (
                        artifact.script.canonical_text_sha256
                    ),
                }
            }
        },
    )
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="PRODUCTION_PLAN",
        attempt=1,
        status="paused",
        input_json={"variant_index": 1},
        output_json={
            "status": "PASS",
            "content_factory_variant_index": 1,
            "result": {
                "loop_status": "approved",
                "production_plan": plan.model_dump(mode="json"),
                "compiled_media_design": compiled,
            },
        },
    )
    db_session.add(stage)
    db_session.flush()
    audit = HermesContentProductionPlanAudit(
        project_id=project.id,
        stage_id=stage.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        variant_index=1,
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        director_artifact_sha256=artifact.artifact_sha256,
        plan_sha256=plan.plan_sha256,
        status="approved",
        accepted=True,
        plan_json=plan.model_dump(mode="json"),
        attempts_json=[],
        critic_attempts_json=[],
        reviews_json=[],
        contract_errors_json=[],
        reason="approved",
    )
    db_session.add(audit)
    db_session.flush()

    state, promoted = _promote_approved_paused_control_stage(
        db_session,
        project,
        stage,
        dict(project.state_json or {}),
    )

    assert promoted is True
    assert stage.status == "success"
    assert project.current_stage == "VISUAL_PREVIEW"
    pointer = state["approved_production_plans_by_variant"]["1"]
    assert pointer["production_plan_stage_id"] == stage.id
    assert pointer["audit_row_id"] == audit.id
    assert pointer["plan_sha256"] == plan.plan_sha256


def test_resume_legacy_visual_pause_restarts_at_series_director(db_session):
    project = HermesContentFactoryProject(
        project_key="cf-resume-series-control",
        workspace_id=1,
        user_id=None,
        title="Resume through control plane",
        product_name="",
        status="paused",
        current_stage="VISUAL_PREVIEW",
        config_json={
            "content_director_mode": "enforce",
            "manual_paused": True,
            "director_series_brief": {"series_id": "series-1"},
        },
        state_json={
            "active_variant_index": 41,
            "video_variant_pipeline": {
                "active_index": 41,
                "target_count": 50,
            },
            "pending_visual_api_resume": {"stage_id": 999},
        },
    )
    db_session.add(project)
    db_session.flush()
    paused = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VISUAL_PREVIEW",
        attempt=1,
        status="paused",
        input_json={"variant_index": 41},
    )
    db_session.add(paused)
    db_session.commit()

    resumed = resume_project(db_session, project)
    state = dict(resumed.state_json or {})

    assert resumed.status == "ready"
    assert resumed.current_stage == "SERIES_DIRECTOR"
    assert resumed.config_json["manual_paused"] is False
    assert "pending_visual_api_resume" not in state
    assert state["resume_control_reset"] == {
        "reason": "approved_series_slate_missing",
        "variant_index": 41,
        "next_stage": "SERIES_DIRECTOR",
        "at": state["resume_control_reset"]["at"],
    }
    assert paused.status == "failed"
