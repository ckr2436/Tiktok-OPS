from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.data.models.hermes_agent import (
    HermesContentDirectorArtifact,
    HermesContentDirectorAttempt,
    HermesContentDirectorBrief,
    HermesContentDirectorReview,
)
from app.services.hermes_agent.content_director import (
    ConversionIntent,
    CopyReviewCriterion,
    CopyCriticCriterionEvidence,
    DirectorCapabilityNode,
    DirectorCapabilitySpec,
    DirectorProjectBrief,
    IndependentCopyCriticVerdict,
    ScriptLine,
    ScriptSegmentAllocation,
    VideoProgramSpec,
    build_directed_content_artifact,
    build_script_package,
    preflight_script_copy,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorAttemptRecord,
    DirectorLoopResult,
    DirectorReviewRecord,
)
from app.services.hermes_agent.content_director_store import (
    persist_content_director_loop,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _latest_director_ancestor_for_brief,
)


def _approved_loop():
    capability = DirectorCapabilitySpec(
        capability="visual.story",
        input_contract="VideoProgramSpec",
        output_contract="VisualProgram",
        policy={"product_optional": True},
    )
    criterion = CopyReviewCriterion(
        criterion_id="clarity",
        instruction="The audience understands the line on first listen.",
        minimum_score=80,
    )
    brief = DirectorProjectBrief(
        brief_id="brief-variant-41",
        brief_version=1,
        objective="Explain one useful idea.",
        content_type_hint="animated explainer",
        platform="TikTok",
        locale="en-US",
        audience="US adults.",
        target_duration_seconds=10,
        edit_headroom_seconds=1,
        speech_rate_wpm=150,
        aspect_ratio="9:16",
        conversion=ConversionIntent(product_required=False),
        truth_payload={"facts": ["A lever trades force for distance."]},
        capability_catalog=[capability],
        copy_review_criteria=[criterion],
    )
    program = VideoProgramSpec(
        program_id="program-variant-41",
        objective=brief.objective,
        content_type=brief.content_type_hint,
        platform=brief.platform,
        locale=brief.locale,
        audience=brief.audience,
        target_duration_seconds=brief.target_duration_seconds,
        aspect_ratio=brief.aspect_ratio,
        conversion=brief.conversion,
        execution_graph=[
            DirectorCapabilityNode(
                node_id="story",
                capability=capability.capability,
                input_contract=capability.input_contract,
                output_contract=capability.output_contract,
                policy=capability.policy,
            )
        ],
        copy_review_criteria=[criterion],
    )
    script = build_script_package(
        script_id="script-variant-41",
        program_id=program.program_id,
        locale=brief.locale,
        target_duration_seconds=brief.target_duration_seconds,
        edit_headroom_seconds=brief.edit_headroom_seconds,
        speech_rate_wpm=brief.speech_rate_wpm,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="l1",
                speaker_id="narrator",
                text="A lever trades force for distance.",
                beat_id="beat-1",
                purpose="explain",
            )
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["l1"],
            )
        ],
    )
    artifact = build_directed_content_artifact(
        artifact_id="artifact-variant-41",
        revision=1,
        program=program,
        script=script,
    )
    preflight = preflight_script_copy(program, script)
    verdict = IndependentCopyCriticVerdict(
        approved=True,
        scores={"clarity": 95},
        criterion_evidence={
            "clarity": CopyCriticCriterionEvidence(
                line_ids=["l1"],
                quotes=["A lever trades force for distance."],
                rationale=(
                    "The exact quote explains the tradeoff clearly in one sentence."
                ),
            )
        },
        blocking_issues=[],
        repair_scope="copy_only",
    )
    result = DirectorLoopResult(
        status="approved",
        final_artifact=artifact,
        artifacts=[artifact],
        director_attempts=[
            DirectorAttemptRecord(
                operation="initial",
                artifact_id=artifact.artifact_id,
                revision=1,
                contract_repair_attempt=0,
                latency_ms=1200,
                response_sha256="a" * 64,
                outcome="accepted",
                model="director-test",
                request_id="director-request-1",
            )
        ],
        reviews=[
            DirectorReviewRecord(
                artifact_sha256=artifact.artifact_sha256,
                preflight=preflight,
                verdict=verdict,
                critic_latency_ms=500,
                critic_response_sha256="b" * 64,
            )
        ],
        reason="approved in test",
    )
    return brief, result


def test_persist_content_director_loop_is_idempotent_and_immutable(db_session):
    project = SimpleNamespace(
        id=168,
        workspace_id=12,
        user_id=34,
    )
    brief, result = _approved_loop()

    first = persist_content_director_loop(
        db_session,
        project=project,
        variant_index=41,
        mode="enforce",
        brief=brief,
        result=result,
    )
    db_session.commit()
    second = persist_content_director_loop(
        db_session,
        project=project,
        variant_index=41,
        mode="enforce",
        brief=brief,
        result=result,
    )
    db_session.commit()

    assert first.id == second.id
    for model in (
        HermesContentDirectorBrief,
        HermesContentDirectorArtifact,
        HermesContentDirectorAttempt,
        HermesContentDirectorReview,
    ):
        assert db_session.scalar(select(func.count()).select_from(model)) == 1
    artifact = db_session.scalar(select(HermesContentDirectorArtifact))
    assert artifact.accepted is True

    changed_brief = brief.model_copy(update={"audience": "A different audience."})
    with pytest.raises(ValueError, match="conflicts with project scope or version"):
        persist_content_director_loop(
            db_session,
            project=project,
            variant_index=41,
            mode="enforce",
            brief=changed_brief,
            result=result,
        )


def test_new_execution_key_preserves_a_new_immutable_audit_generation(
    db_session,
):
    project = SimpleNamespace(
        id=169,
        workspace_id=12,
        user_id=34,
    )
    brief, result = _approved_loop()

    first = persist_content_director_loop(
        db_session,
        project=project,
        variant_index=41,
        mode="enforce",
        brief=brief,
        result=result,
        execution_key="director-stage-100",
    )
    second = persist_content_director_loop(
        db_session,
        project=project,
        variant_index=41,
        mode="enforce",
        brief=brief,
        result=result,
        execution_key="director-stage-101",
    )
    db_session.commit()

    assert first.id != second.id
    assert first.brief_version == second.brief_version == 1
    assert first.execution_key == "director-stage-100"
    assert second.execution_key == "director-stage-101"
    assert db_session.scalar(
        select(func.count()).select_from(HermesContentDirectorBrief)
    ) == 2
    assert db_session.scalar(
        select(func.count()).select_from(HermesContentDirectorArtifact)
    ) == 2
    assert db_session.scalar(
        select(func.count()).select_from(HermesContentDirectorAttempt)
    ) == 2
    assert db_session.scalar(
        select(func.count()).select_from(HermesContentDirectorReview)
    ) == 2


def test_new_director_brief_never_resumes_cross_brief_artifact_ancestry(
    db_session,
):
    project = SimpleNamespace(id=170, workspace_id=12, user_id=34)
    old_brief, _result = _approved_loop()
    old_brief = old_brief.model_copy(update={
        "brief_id": "series.variant-009.v7",
        "brief_version": 7,
    })
    new_brief = old_brief.model_copy(update={
        "brief_id": "series.variant-009.v9",
        "brief_version": 9,
        "objective": "Show a released community workshop RSVP.",
    })

    def brief_sha256(brief):
        payload = brief.model_dump(mode="json")
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    old_row = HermesContentDirectorBrief(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        brief_key=old_brief.brief_id,
        execution_key="old-execution",
        variant_index=9,
        brief_version=old_brief.brief_version,
        mode="enforce",
        status="quality_pause",
        brief_sha256=brief_sha256(old_brief),
        brief_json=old_brief.model_dump(mode="json"),
    )
    new_row = HermesContentDirectorBrief(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        brief_key=new_brief.brief_id,
        execution_key="new-execution-contaminated",
        variant_index=9,
        brief_version=new_brief.brief_version,
        mode="enforce",
        status="quality_pause",
        brief_sha256=brief_sha256(new_brief),
        brief_json=new_brief.model_dump(mode="json"),
    )
    db_session.add_all([old_row, new_row])
    db_session.flush()

    old_artifact = HermesContentDirectorArtifact(
        brief_id=old_row.id,
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        variant_index=9,
        artifact_key="project.variant-009.content",
        revision=1,
        parent_artifact_sha256=None,
        artifact_sha256="a" * 64,
        accepted=False,
        artifact_json={},
    )
    contaminated = HermesContentDirectorArtifact(
        brief_id=new_row.id,
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        variant_index=9,
        artifact_key="project.variant-009.content",
        revision=2,
        parent_artifact_sha256=old_artifact.artifact_sha256,
        artifact_sha256="b" * 64,
        accepted=False,
        artifact_json={},
    )
    db_session.add_all([old_artifact, contaminated])
    db_session.flush()

    assert _latest_director_ancestor_for_brief(
        db_session,
        project=project,
        variant_index=9,
        artifact_id="project.variant-009.content",
        brief=new_brief,
    ) is None

    clean_row = HermesContentDirectorBrief(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        brief_key=new_brief.brief_id,
        execution_key="new-execution-clean",
        variant_index=9,
        brief_version=new_brief.brief_version,
        mode="enforce",
        status="quality_pause",
        brief_sha256=brief_sha256(new_brief),
        brief_json=new_brief.model_dump(mode="json"),
    )
    db_session.add(clean_row)
    db_session.flush()
    clean_artifact = HermesContentDirectorArtifact(
        brief_id=clean_row.id,
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        variant_index=9,
        artifact_key="project.variant-009.content",
        revision=1,
        parent_artifact_sha256=None,
        artifact_sha256="c" * 64,
        accepted=False,
        artifact_json={},
    )
    db_session.add(clean_artifact)
    db_session.flush()

    selected = _latest_director_ancestor_for_brief(
        db_session,
        project=project,
        variant_index=9,
        artifact_id="project.variant-009.content",
        brief=new_brief,
    )
    assert selected is not None
    assert selected.id == clean_artifact.id
