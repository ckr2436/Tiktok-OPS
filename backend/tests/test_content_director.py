from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from app.services.hermes_agent.content_director import (
    ConversionIntent,
    CopyReviewCriterion,
    DirectorCapabilityNode,
    DirectorAuthorDraftPayload,
    DirectorCapabilitySpec,
    DirectedContentArtifact,
    DirectorProjectBrief,
    ScriptLine,
    ScriptSegmentAllocation,
    VideoProductionContract,
    VideoProgramSpec,
    build_independent_copy_critic_packet,
    build_directed_content_artifact,
    build_director_revision_packet,
    build_initial_director_packet,
    build_delivery_budget_contract,
    build_script_package,
    director_author_output_contract,
    director_draft_output_contract,
    finalize_director_author_draft,
    parse_independent_copy_critic_response,
    parse_director_draft_response,
    preflight_script_copy,
    run_content_director_shadow_preflight,
    script_package_from_creative_result,
    validate_directed_artifact_against_brief,
)
from app.services.hermes_agent.content_intent import (
    CreativeIntentManifest,
    CreativeIntentRequirement,
    sign_creative_intent_manifest,
)


def _graph() -> list[DirectorCapabilityNode]:
    return [
        DirectorCapabilityNode(
            node_id="facts",
            capability="truth.normalize",
            input_contract="ProjectInputs",
            output_contract="TruthPacket",
        ),
        DirectorCapabilityNode(
            node_id="copy",
            capability="copy.write",
            depends_on=["facts"],
            input_contract="VideoProgramSpec",
            output_contract="ScriptPackage",
        ),
        DirectorCapabilityNode(
            node_id="critic",
            capability="copy.review",
            depends_on=["copy"],
            input_contract="ScriptPackage",
            output_contract="CopyReview",
        ),
    ]


def _criteria(*, product_required: bool = True) -> list[CopyReviewCriterion]:
    criteria = [
        CopyReviewCriterion(
            criterion_id="first_pass_comprehension",
            instruction="The configured audience understands every line on first listen.",
            minimum_score=80,
        ),
        CopyReviewCriterion(
            criterion_id="locale_naturalness",
            instruction="The phrasing sounds natural in the configured locale.",
            minimum_score=80,
        ),
        CopyReviewCriterion(
            criterion_id="complete_story_ending",
            instruction="The final line completes the same story.",
            minimum_score=75,
        ),
    ]
    if product_required:
        criteria.extend([
            CopyReviewCriterion(
                criterion_id="confirmed_product_rationale",
                instruction="The copy gives a confirmed reason to choose the product.",
                minimum_score=80,
            ),
            CopyReviewCriterion(
                criterion_id="conversion_bridge",
                instruction="The product follows a credible human decision without overclaim.",
                minimum_score=80,
            ),
        ])
    return criteria


def _brief(*, product_required: bool = True) -> DirectorProjectBrief:
    program = _program() if product_required else None
    conversion = (
        program.conversion
        if program is not None
        else ConversionIntent(product_required=False)
    )
    return DirectorProjectBrief(
        brief_id="brief-test",
        objective=(
            program.objective
            if program is not None
            else "Teach a mechanical concept."
        ),
        content_type_hint=(
            program.content_type
            if program is not None
            else "animated explainer"
        ),
        platform=program.platform if program is not None else "YouTube Shorts",
        locale="en-US",
        audience=program.audience if program is not None else "Adult beginners.",
        target_duration_seconds=40 if program is not None else 55,
        edit_headroom_seconds=2,
        speech_rate_wpm=165 if program is not None else 150,
        aspect_ratio="9:16",
        conversion=conversion,
        truth_payload={"facts": ["Only supplied facts may appear."]},
        capability_catalog=[
            DirectorCapabilitySpec(
                capability=node.capability,
                input_contract=node.input_contract,
                output_contract=node.output_contract,
            )
            for node in _graph()
        ],
        copy_review_criteria=_criteria(product_required=product_required),
        quality_rubric=program.quality_rubric if program is not None else [],
        source_truth_refs=program.source_truth_refs if program is not None else [],
    )


def test_latest_restart_instruction_is_bound_to_each_director_variant() -> None:
    from types import SimpleNamespace

    from app.tasks.hermes_agent.content_factory_tasks import (
        _bind_latest_operator_instruction_to_director_brief,
    )

    instruction = (
        "Open every deliverable with a distinct high-energy visual conflict; "
        "a calm shoulder touch is not an acceptable hook."
    )
    project = SimpleNamespace(
        state_json={
            "restart_count": 7,
            "last_restart": {
                "stage": "SERIES_DIRECTOR",
                "instruction": instruction,
                "replace_completed": True,
                "at": "2026-08-01T07:17:12.258186",
            },
        }
    )

    bound = _bind_latest_operator_instruction_to_director_brief(
        _brief(),
        project=project,
    )

    assert bound.truth_payload["operator_stage_instruction"] == instruction
    authority = bound.truth_payload[
        "operator_stage_instruction_authority"
    ]
    assert authority["source"] == "project_last_restart"
    assert authority["restart_generation"] == 7
    assert authority["replace_completed"] is True
    assert any(
        instruction in constraint
        for constraint in bound.creative_constraints
    )


def _program(**conversion_overrides) -> VideoProgramSpec:
    conversion = {
        "product_required": True,
        "product_name": "MYUPONA Sleep Ease Gummies",
        "reveal_after_fraction": 0.75,
        "confirmed_differentiators": [
            "melatonin-free",
            "GABA",
            "L-Theanine",
        ],
        "minimum_differentiators_in_copy": 2,
        "expected_human_change": (
            "Make a repeatable nighttime transition instead of carrying the "
            "unfinished workday forward."
        ),
        "outcome_boundary": (
            "The routine does not restore a lost client, repair professional "
            "trust, or guarantee sleep."
        ),
        "offer_text": "$7.99",
        "cta_text": "yellow cart",
        "protected_stake_terms": ["client", "album", "reliability", "leave"],
        "require_post_cta_human_agency": True,
        "post_cta_agency_terms": ["decision", "choice", "album"],
    }
    conversion.update(conversion_overrides)
    return VideoProgramSpec(
        program_id="program-v40-review",
        objective="Convert cold TikTok viewers with a complete short story.",
        content_type="animated emotional direct-response story",
        platform="TikTok",
        locale="en-US",
        audience="US adults seeking a simple melatonin-free nighttime routine.",
        target_duration_seconds=40,
        aspect_ratio="9:16",
        conversion=ConversionIntent(**conversion),
        execution_graph=_graph(),
        copy_review_criteria=_criteria(),
        quality_rubric=[
            "American audience understands every line on first listen",
            "product transition follows a human decision",
        ],
    )


def _historic_v40_result() -> dict:
    return {
        "complete_video_script": {
            "duration_seconds": 40,
            "segments": [
                {
                    "segment_index": 1,
                    "duration_seconds": 10,
                    "story_function": "LOSS HOOK",
                    "dialogue_lines": [{
                        "speaker_id": "mara",
                        "line": (
                            "At age forty-two, I watched a returning client carry "
                            "her portrait album toward another photographer."
                        ),
                    }],
                },
                {
                    "segment_index": 2,
                    "duration_seconds": 10,
                    "story_function": "KNIFE-TWIST MOMENT",
                    "dialogue_lines": [{
                        "speaker_id": "mara",
                        "line": (
                            "She said, “I need someone who can finish this,” and "
                            "slid my marked proofs away."
                        ),
                    }],
                },
                {
                    "segment_index": 3,
                    "duration_seconds": 10,
                    "story_function": "RECOGNITION AND BRIDGE",
                    "dialogue_lines": [{
                        "speaker_id": "mara",
                        "line": (
                            "That was not fatigue; my reliability was disappearing, "
                            "so I chose a clean ending nightly."
                        ),
                    }],
                },
                {
                    "segment_index": 4,
                    "duration_seconds": 10,
                    "story_function": "PRODUCT AND CONVERSION",
                    "dialogue_lines": [{
                        "speaker_id": "mara",
                        "line": (
                            "MYUPONA fits clean endings; $7.99—find it in the "
                            "yellow cart below before clients leave."
                        ),
                    }],
                },
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "mara",
            "speakers": [{"speaker_id": "mara", "speech_rate": 165}],
        },
    }


def test_video_program_graph_is_generic_and_acyclic():
    program = VideoProgramSpec(
        program_id="education-no-product",
        objective="Teach a mechanical concept.",
        content_type="animated explainer",
        platform="YouTube Shorts",
        locale="en-US",
        audience="Adult beginners.",
        target_duration_seconds=55,
        aspect_ratio="9:16",
        conversion=ConversionIntent(product_required=False),
        execution_graph=_graph(),
        copy_review_criteria=_criteria(product_required=False),
    )

    assert program.content_type == "animated explainer"
    assert program.conversion.product_required is False

    cyclic = [node.model_copy(deep=True) for node in _graph()]
    cyclic[0].depends_on = ["critic"]
    with pytest.raises(ValidationError, match="acyclic"):
        program.model_copy(update={"execution_graph": cyclic}).model_validate(
            program.model_copy(update={"execution_graph": cyclic}).model_dump()
        )


def test_current_director_contract_requires_v2_audio_authority():
    output_contract = director_draft_output_contract()
    definitions = output_contract["$defs"]

    assert definitions["VideoProgramSpec"]["properties"][
        "schema_version"
    ]["const"] == "2.0"
    assert "audio_mode" in definitions["VideoProgramSpec"]["required"]
    assert "audio_mode" in definitions["ScriptDraft"]["required"]

    with pytest.raises(ValidationError, match="explicit audio_mode"):
        _program().model_copy(update={"schema_version": "2.0"}).model_validate(
            _program().model_copy(
                update={"schema_version": "2.0"}
            ).model_dump(mode="json")
        )


def _author_only_payload(*, capability: str = "copy.write") -> dict:
    return {
        "program": {
            "schema_version": "2.0",
            "program_id": "author-only-program",
            "content_type": "animated emotional direct-response story",
            "audio_mode": "spoken",
            "creative_strategy": {
                "format": "one complete first-person story",
            },
            "execution_graph": [
                {
                    "node_id": "copy",
                    "capability": capability,
                    "depends_on": [],
                    "required": True,
                }
            ],
        },
        "script": {
            "schema_version": "2.0",
            "script_id": "author-only-script",
            "program_id": "author-only-program",
            "audio_mode": "spoken",
            "primary_speaker_id": "narrator",
            "lines": [
                {
                    "line_id": "l1",
                    "speaker_id": "narrator",
                    "text": (
                        "MYUPONA Sleep Ease Gummies give me a "
                        "melatonin-free nighttime choice for $7.99 in the "
                        "yellow cart."
                    ),
                    "beat_id": "choice",
                    "purpose": "conversion",
                    "delivery_mode": "spoken",
                }
            ],
            "segments": [
                {
                    "segment_index": 1,
                    "line_ids": ["l1"],
                }
            ],
        },
    }


def test_author_only_contract_excludes_every_runtime_owned_field():
    brief = _brief()
    contract = director_author_output_contract(brief)
    serialized = __import__("json").dumps(contract, sort_keys=True)

    for forbidden in (
        '"conversion"',
        '"copy_review_criteria"',
        '"quality_rubric"',
        '"source_truth_refs"',
        '"input_contract"',
        '"output_contract"',
        '"policy"',
        '"target_duration_seconds"',
        '"speech_rate_wpm"',
        '"display_reading_rate_wpm"',
        '"duration_seconds"',
    ):
        assert forbidden not in serialized

    definitions = contract["$defs"]
    assert definitions["DirectorProgramAuthorDraft"]["properties"][
        "content_type"
    ]["const"] == brief.content_type_hint
    assert set(
        definitions["DirectorCapabilitySelection"]["properties"]
        ["capability"]["enum"]
    ) == {item.capability for item in brief.capability_catalog}


def test_author_only_contract_machine_locks_verbatim_spoken_block_count():
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": (
                    "First immutable block.\n\n"
                    "Second immutable block.\n\n"
                    "Third immutable block."
                )
            }
        }
    )

    contract = director_author_output_contract(brief)
    lines = contract["$defs"]["DirectorScriptAuthorDraft"][
        "properties"
    ]["lines"]

    assert lines["minContains"] == 3
    assert lines["maxContains"] == 3
    assert lines["contains"]["properties"]["delivery_mode"] == {
        "const": "spoken"
    }


def test_author_only_draft_materializes_exact_project_owned_contract():
    brief = _brief()
    authoritative = []
    for spec in brief.capability_catalog:
        authoritative.append(
            spec.model_copy(
                update={"policy": {"authority": spec.capability}}
            )
        )
    brief = brief.model_copy(update={"capability_catalog": authoritative})
    draft = DirectorAuthorDraftPayload.model_validate(
        _author_only_payload()
    )

    artifact = finalize_director_author_draft(
        draft,
        brief,
        artifact_id="author-only-artifact",
        revision=1,
    )

    assert artifact.program.schema_version == "2.0"
    assert artifact.script.schema_version == "2.0"
    assert artifact.program.objective == brief.objective
    assert artifact.program.conversion == brief.conversion
    assert artifact.program.copy_review_criteria == brief.copy_review_criteria
    assert artifact.program.quality_rubric == brief.quality_rubric
    assert artifact.program.source_truth_refs == brief.source_truth_refs
    assert artifact.script.locale == brief.locale
    assert artifact.script.target_duration_seconds == (
        brief.target_duration_seconds
    )
    assert artifact.script.edit_headroom_seconds == (
        brief.edit_headroom_seconds
    )
    assert artifact.script.speech_rate_wpm == brief.speech_rate_wpm
    assert artifact.script.segments[0].duration_seconds == (
        brief.target_duration_seconds
    )
    assert artifact.program.execution_graph[0].policy == {
        "authority": "copy.write"
    }


def test_director_must_bind_and_map_signed_high_priority_intent():
    manifest = sign_creative_intent_manifest(
        CreativeIntentManifest(
            objective="Create an original stopping-power opening.",
            requirements=[CreativeIntentRequirement(
                requirement_id="R-001",
                kind="reference_transfer",
                priority="high",
                intent="Keep the benchmark hook force without copying it.",
                evidence_quote="Keep the hook force, but do not copy the original.",
                interpretation=(
                    "Create a new immediately readable contradiction in the opening."
                ),
                observable_checks=[
                    "The contradiction is readable before the setup is explained."
                ],
                creative_freedom=["Invent a new setting and visual mechanism."],
                must_not_reuse=["benchmark actors", "benchmark wording"],
            )],
        )
    )
    brief = _brief().model_copy(update={
        "truth_payload": {
            "producer_intent_spec": {
                "intent_manifest": manifest.model_dump(mode="json"),
            },
            "series_intent": {"variant_index": 1},
        },
    })
    contract = director_author_output_contract(brief)
    program_contract = contract["$defs"]["DirectorProgramAuthorDraft"]
    assert "requirement_execution" in program_contract["required"]
    assert program_contract["properties"]["requirement_execution"][
        "minItems"
    ] == 1
    payload = _author_only_payload()
    payload["program"]["requirement_execution"] = [{
        "requirement_id": "R-001",
        "implementation": (
            "Use the first line and first segment to reveal a newly invented "
            "visual contradiction."
        ),
        "script_line_ids": ["l1"],
        "capability_node_ids": ["copy"],
        "segment_indices": [1],
        "evidence_plan": [
            "Opening pixels and the first line jointly establish the contradiction."
        ],
    }]

    artifact = finalize_director_author_draft(
        DirectorAuthorDraftPayload.model_validate(payload),
        brief,
        artifact_id="intent-bound-artifact",
        revision=1,
    )

    assert artifact.program.intent_manifest_sha256 == manifest.manifest_sha256
    assert artifact.program.intent_requirements == manifest.requirements
    assert artifact.program.requirement_execution[0].requirement_id == "R-001"

    missing = _author_only_payload()
    with pytest.raises(ValueError, match="lack execution mappings"):
        finalize_director_author_draft(
            DirectorAuthorDraftPayload.model_validate(missing),
            brief,
            artifact_id="intent-unmapped-artifact",
            revision=1,
        )


def test_finalizer_keeps_program_v2_when_script_uses_compiled_continuation():
    payload = _author_only_payload()
    locked_copy = payload["script"]["lines"][0]["text"]
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": locked_copy,
                "required_verbatim_voiceover_lines": [
                    {"segment_index": 1, "text": locked_copy}
                ],
                "locked_voiceover_segment_boundary_policy": {
                    "mode": "runtime_compiled_continuation",
                    "authority": "runtime",
                },
            }
        }
    )

    artifact = finalize_director_author_draft(
        DirectorAuthorDraftPayload.model_validate(payload),
        brief,
        artifact_id="compiled-continuation-finalizer",
        revision=1,
    )

    assert artifact.program.schema_version == "2.0"
    assert artifact.script.schema_version == "2.1"


def test_author_only_draft_rejects_invented_capability_before_signing():
    draft = DirectorAuthorDraftPayload.model_validate(
        _author_only_payload(capability="media.magic")
    )
    with pytest.raises(ValueError, match="unregistered capability"):
        finalize_director_author_draft(
            draft,
            _brief(),
            artifact_id="invented-capability",
            revision=1,
        )


def test_series_audio_authority_binds_episode_contract_and_finalizer():
    brief = _brief().model_copy(
        update={"audio_mode_hint": "sound_design"}
    )
    contract = director_author_output_contract(brief)
    definitions = contract["$defs"]
    assert definitions["DirectorProgramAuthorDraft"]["properties"][
        "audio_mode"
    ]["const"] == "sound_design"
    assert definitions["DirectorScriptAuthorDraft"]["properties"][
        "audio_mode"
    ]["const"] == "sound_design"

    draft = DirectorAuthorDraftPayload.model_validate(
        _author_only_payload()
    )
    with pytest.raises(ValueError, match="requested audio mode"):
        finalize_director_author_draft(
            draft,
            brief,
            artifact_id="audio-authority-mismatch",
            revision=1,
        )


def test_v2_program_and_script_audio_modes_must_match():
    program = _program().model_copy(update={
        "schema_version": "2.0",
        "audio_mode": "sound_design",
    })
    script = build_script_package(
        script_id="audio-mismatch-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=40,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        audio_mode="spoken",
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="one",
                speaker_id="narrator",
                text="One complete spoken line.",
                beat_id="one",
                purpose="test",
            )
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=index,
                duration_seconds=10,
                line_ids=["one"] if index == 1 else [],
            )
            for index in range(1, 5)
        ],
    )

    with pytest.raises(ValidationError, match="audio mode"):
        build_directed_content_artifact(
            artifact_id="audio-mismatch-artifact",
            revision=1,
            parent_artifact_sha256=None,
            program=program,
            script=script,
        )


def test_v1_artifact_without_audio_field_keeps_legacy_hash():
    program = _program()
    script = build_script_package(
        script_id="legacy-audio-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=40,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="one",
                speaker_id="narrator",
                text="One legacy line.",
                beat_id="one",
                purpose="test",
            )
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=index,
                duration_seconds=10,
                line_ids=["one"] if index == 1 else [],
            )
            for index in range(1, 5)
        ],
    )
    artifact = build_directed_content_artifact(
        artifact_id="legacy-audio-artifact",
        revision=1,
        parent_artifact_sha256=None,
        program=program,
        script=script,
    )
    legacy_payload = artifact.model_dump(mode="json")
    legacy_payload["program"].pop("audio_mode")

    loaded = DirectedContentArtifact.model_validate(legacy_payload)

    assert loaded.program.audio_mode is None
    assert loaded.artifact_sha256 == artifact.artifact_sha256


def test_director_rejects_segments_outside_registered_model_contract():
    program = _program()
    lines = [
        ScriptLine(
            line_id=f"l{index}",
            speaker_id="mara",
            text=f"Complete line {index}.",
            beat_id=f"b{index}",
            purpose="story",
        )
        for index in range(1, 9)
    ]
    script = build_script_package(
        script_id="script-illegal-five-second-segments",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=40,
        edit_headroom_seconds=2,
        speech_rate_wpm=165,
        primary_speaker_id="mara",
        lines=lines,
        segments=[
            ScriptSegmentAllocation(
                segment_index=index,
                duration_seconds=5,
                line_ids=[f"l{index}"],
            )
            for index in range(1, 9)
        ],
    )
    artifact = build_directed_content_artifact(
        artifact_id="artifact-illegal-five-second-segments",
        revision=1,
        parent_artifact_sha256=None,
        program=program,
        script=script,
    )
    brief = _brief().model_copy(update={
        "production_contract": VideoProductionContract(
            model_id="omni_flash",
            segment_duration_minimum_seconds=10,
            segment_duration_maximum_seconds=10,
            allowed_segment_durations_seconds=[10],
            reference_image_limit=7,
            reference_video_limit=0,
        ),
    })

    with pytest.raises(
        ValueError,
        match="registered video production contract",
    ):
        validate_directed_artifact_against_brief(artifact, brief)


def test_script_package_rejects_dropped_or_reordered_lines():
    lines = [
        ScriptLine(
            line_id="l1",
            speaker_id="narrator",
            text="This is the opening.",
            beat_id="hook",
            purpose="hook",
        ),
        ScriptLine(
            line_id="l2",
            speaker_id="narrator",
            text="This is the resolution.",
            beat_id="resolution",
            purpose="resolution",
        ),
    ]
    with pytest.raises(ValidationError, match="every canonical line exactly once"):
        build_script_package(
            script_id="script",
            program_id="program",
            locale="en-US",
            target_duration_seconds=20,
            edit_headroom_seconds=2,
            speech_rate_wpm=150,
            primary_speaker_id="narrator",
            lines=lines,
            segments=[
                ScriptSegmentAllocation(
                    segment_index=1,
                    duration_seconds=10,
                    line_ids=["l1"],
                ),
                ScriptSegmentAllocation(
                    segment_index=2,
                    duration_seconds=10,
                    line_ids=["l1"],
                ),
            ],
        )


def test_preflight_rejects_a_spoken_segment_that_cannot_fit_its_clip():
    program = _program().model_copy(
        update={
            "program_id": "segment-paced-program",
            "target_duration_seconds": 20,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    script = build_script_package(
        script_id="segment-paced-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=20,
        edit_headroom_seconds=0,
        speech_rate_wpm=120,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="dense",
                speaker_id="narrator",
                text=(
                    "This first segment deliberately carries too many spoken "
                    "words for one short clip even though the complete script "
                    "still fits its overall delivery budget."
                ),
                beat_id="dense",
                purpose="test pacing",
            ),
            ScriptLine(
                line_id="light",
                speaker_id="narrator",
                text="This part fits.",
                beat_id="light",
                purpose="test pacing",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["dense"],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=10,
                line_ids=["light"],
            ),
        ],
    )

    report = preflight_script_copy(program, script)

    assert script.spoken_word_count <= script.spoken_budget_words
    assert report.approved is False
    assert "SPOKEN_SEGMENT_OVER_BUDGET" in {
        issue.code for issue in report.issues
    }


def test_preflight_allows_small_segment_pacing_variance_under_global_budget():
    program = _program().model_copy(
        update={
            "program_id": "segment-tolerance-program",
            "target_duration_seconds": 40,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    script = build_script_package(
        script_id="segment-tolerance-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=40,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="l1",
                speaker_id="narrator",
                text=(
                    "One two three four five six seven eight nine ten eleven "
                    "twelve thirteen fourteen fifteen sixteen seventeen "
                    "eighteen nineteen twenty twenty-one twenty-two "
                    "twenty-three twenty-four twenty-five twenty-six."
                ),
                beat_id="slightly-fast",
                purpose="test natural pacing tolerance",
            ),
            ScriptLine(
                line_id="l2",
                speaker_id="narrator",
                text="This line fits.",
                beat_id="two",
                purpose="test",
            ),
            ScriptLine(
                line_id="l3",
                speaker_id="narrator",
                text="This line fits.",
                beat_id="three",
                purpose="test",
            ),
            ScriptLine(
                line_id="l4",
                speaker_id="narrator",
                text="This line fits.",
                beat_id="four",
                purpose="test",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=index,
                duration_seconds=10,
                line_ids=[f"l{index}"],
            )
            for index in range(1, 5)
        ],
    )

    report = preflight_script_copy(program, script)

    assert script.spoken_word_count <= script.spoken_budget_words
    assert "SPOKEN_SEGMENT_OVER_BUDGET" not in {
        issue.code for issue in report.issues
    }


def test_preflight_treats_colon_leadin_and_quote_as_one_spoken_segment():
    program = _program().model_copy(
        update={
            "program_id": "colon-leadin-program",
            "target_duration_seconds": 10,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    script = build_script_package(
        script_id="colon-leadin-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=10,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="lead-in",
                speaker_id="narrator",
                text="And you keep asking yourself:",
                beat_id="question",
                purpose="lead into the exact question",
            ),
            ScriptLine(
                line_id="question",
                speaker_id="narrator",
                text="Why can’t I just turn my brain off?",
                beat_id="question",
                purpose="complete the question",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["lead-in", "question"],
            )
        ],
    )

    report = preflight_script_copy(program, script)

    assert "INCOMPLETE_SPOKEN_SENTENCE" not in {
        issue.code for issue in report.issues
    }


def test_preflight_blocks_spoken_segment_that_ends_on_colon_leadin():
    program = _program().model_copy(
        update={
            "program_id": "split-colon-program",
            "target_duration_seconds": 20,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    script = build_script_package(
        script_id="split-colon-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=20,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="lead-in",
                speaker_id="narrator",
                text="And you keep asking yourself:",
                beat_id="question",
                purpose="lead into the exact question",
            ),
            ScriptLine(
                line_id="question",
                speaker_id="narrator",
                text="Why can’t I just turn my brain off?",
                beat_id="answer",
                purpose="complete the question",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["lead-in"],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=10,
                line_ids=["question"],
            ),
        ],
    )

    report = preflight_script_copy(program, script)

    issue = next(
        issue
        for issue in report.issues
        if issue.code == "INCOMPLETE_SPOKEN_SENTENCE"
    )
    assert issue.line_ids == ["lead-in"]


def test_preflight_allows_only_audited_internal_continuation_splice():
    program = _program().model_copy(
        update={
            "program_id": "compiled-continuation-program",
            "target_duration_seconds": 20,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    script = build_script_package(
        script_id="compiled-continuation-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=20,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        schema_version="2.1",
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="splice-1",
                speaker_id="narrator",
                text="And you keep asking yourself:",
                beat_id="question",
                purpose="runtime compiled first excerpt",
            ),
            ScriptLine(
                line_id="splice-2",
                speaker_id="narrator",
                text="Why can’t I just turn my brain off?",
                beat_id="answer",
                purpose="runtime compiled continuation",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["splice-1"],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=10,
                line_ids=["splice-2"],
            ),
        ],
    )

    report = preflight_script_copy(program, script)
    packet = build_independent_copy_critic_packet(
        program,
        script,
        report,
    )

    assert "INCOMPLETE_SPOKEN_SENTENCE" not in {
        issue.code for issue in report.issues
    }
    assert "one continuous performance" in packet["review_method"][
        "runtime_compiled_continuation"
    ]


def test_compiled_continuation_still_requires_complete_final_ending():
    program = _program().model_copy(
        update={
            "program_id": "compiled-incomplete-ending-program",
            "target_duration_seconds": 10,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    script = build_script_package(
        script_id="compiled-incomplete-ending-script",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=10,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        schema_version="2.1",
        primary_speaker_id="narrator",
        lines=[
            ScriptLine(
                line_id="unfinished",
                speaker_id="narrator",
                text="Because the final thought still",
                beat_id="ending",
                purpose="prove the full narration still needs closure",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["unfinished"],
            ),
        ],
    )

    report = preflight_script_copy(program, script)

    issue = next(
        issue for issue in report.issues
        if issue.code == "INCOMPLETE_SPOKEN_SENTENCE"
    )
    assert issue.line_ids == ["unfinished"]


def test_historic_v40_is_blocked_before_visual_spend():
    program = _program()
    script = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="historic-v40",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )

    report = preflight_script_copy(program, script)
    codes = {issue.code for issue in report.issues}

    assert report.approved is False
    assert "WHY_CHOOSE_PRODUCT_MISSING" in codes
    assert "PRODUCT_CAUSAL_STAKE_COLLISION" in codes
    assert "POST_CTA_HUMAN_AGENCY_MISSING" in codes


def test_natural_product_transition_passes_deterministic_gate_then_requires_critic():
    program = _program()
    lines = [
        ScriptLine(
            line_id="s1",
            speaker_id="mara",
            beat_id="hook",
            purpose="loss_hook",
            text=(
                "A returning client quietly lifted the album Mara had spent "
                "weeks building and carried it to another photographer."
            ),
        ),
        ScriptLine(
            line_id="s2",
            speaker_id="mara",
            beat_id="stakes",
            purpose="knife_twist",
            text=(
                "At the door, she said, “I need someone who can finish it,” "
                "and Mara realized tired was no longer how clients saw her."
            ),
        ),
        ScriptLine(
            line_id="s3",
            speaker_id="mara",
            beat_id="decision",
            purpose="human_decision",
            text=(
                "She could not win that album back, but she could stop carrying "
                "unfinished days forward with a real cutoff between work and night."
            ),
        ),
            ScriptLine(
                line_id="s4a",
                speaker_id="mara",
                beat_id="conversion",
                purpose="why_product",
                text=(
                    "MYUPONA Sleep Ease Gummies became my step—"
                    "melatonin-free, with GABA and L-Theanine, $7.99 in the "
                    "yellow cart."
                ),
        ),
        ScriptLine(
            line_id="s4b",
                speaker_id="mara",
                beat_id="agency",
                purpose="human_agency",
                text="The album was gone; her next choice was hers.",
        ),
    ]
    script = build_script_package(
        script_id="candidate-v40",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=40,
        edit_headroom_seconds=2,
        speech_rate_wpm=165,
        primary_speaker_id="mara",
        lines=lines,
        segments=[
            ScriptSegmentAllocation(segment_index=1, duration_seconds=10, line_ids=["s1"]),
            ScriptSegmentAllocation(segment_index=2, duration_seconds=10, line_ids=["s2"]),
            ScriptSegmentAllocation(segment_index=3, duration_seconds=10, line_ids=["s3"]),
            ScriptSegmentAllocation(
                segment_index=4,
                duration_seconds=10,
                line_ids=["s4a", "s4b"],
            ),
        ],
    )

    report = preflight_script_copy(program, script)
    packet = build_independent_copy_critic_packet(program, script, report)

    assert report.approved is True
    assert report.critic_required is True
    assert report.differentiators_found == [
        "melatonin-free",
        "GABA",
        "L-Theanine",
    ]
    assert packet["role"] == "independent_copy_critic"
    assert packet["store"] is False
    assert "author_conversation" not in packet


def test_creative_adapter_does_not_rewrite_historic_copy():
    original = _historic_v40_result()
    snapshot = copy.deepcopy(original)
    script = script_package_from_creative_result(
        original,
        script_id="historic-v40",
        program_id="program-v40-review",
        locale="en-US",
    )

    assert original == snapshot
    assert [line.text for line in script.lines] == [
        segment["dialogue_lines"][0]["line"]
        for segment in original["complete_video_script"]["segments"]
    ]


def test_shadow_mode_records_would_block_without_enforcement():
    program = _program()

    observation = run_content_director_shadow_preflight(
        project_config={
            "content_director_mode": "shadow",
            "director_program_spec": program.model_dump(mode="json"),
            "director_edit_headroom_seconds": 2,
        },
        creative_result=_historic_v40_result(),
        stage_id=2231,
    )

    assert observation is not None
    assert observation["evaluated"] is True
    assert observation["would_block"] is True
    assert observation["enforced"] is False
    assert "WHY_CHOOSE_PRODUCT_MISSING" in observation["issue_codes"]


def test_shadow_mode_is_inert_without_explicit_project_opt_in():
    assert run_content_director_shadow_preflight(
        project_config={},
        creative_result=_historic_v40_result(),
        stage_id=2231,
    ) is None


def test_independent_critic_response_is_strict_and_fail_closed():
    program = _program()
    script = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="historic-v40",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    preflight = preflight_script_copy(program, script)
    packet = build_independent_copy_critic_packet(program, script, preflight)
    scores = {
        criterion["criterion_id"]: 50
        for criterion in packet["review_criteria"]
    }
    payload = {
        "approved": False,
        "scores": scores,
        "criterion_evidence": {
            criterion_id: {
                "line_ids": [script.lines[0].line_id],
                "quotes": [script.lines[0].text],
                "rationale": (
                    "The quoted opening does not prove this criterion at its "
                    "configured blocking threshold."
                ),
            }
            for criterion_id in scores
        },
        "blocking_issues": [{
            "code": "WHY_CHOOSE_PRODUCT_MISSING",
            "line_ids": ["s004.l001"],
            "evidence": "No configured differentiator appears.",
            "repair_instruction": "Use confirmed differentiators only.",
        }],
        "repair_scope": "copy_only",
    }

    verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(payload),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    assert verdict.approved is False

    # JSON models often add presentation quotes around an otherwise verbatim
    # citation. The interior still has to match the cited authoritative line.
    first_criterion = next(iter(scores))
    original_quote = payload["criterion_evidence"][first_criterion]["quotes"]
    payload["criterion_evidence"][first_criterion]["quotes"] = [
        f'"{script.lines[0].text}"'
    ]
    quoted_verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(payload),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    assert quoted_verdict.approved is False
    payload["criterion_evidence"][first_criterion]["quotes"] = original_quote

    # Display copy such as "43" or "2:17 AM" is legitimate audit evidence.
    # Grounding is enforced by exact membership in the cited script line, not
    # by an arbitrary minimum quote length.
    payload["criterion_evidence"][first_criterion]["quotes"] = [
        script.lines[0].text[:3]
    ]
    short_quote_verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(payload),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    assert short_quote_verdict.approved is False
    payload["criterion_evidence"][first_criterion]["quotes"] = original_quote

    # Repeated audience-facing copy may occur at more than one immutable
    # coordinate (a CTA at the opening and close is a common example). Quote
    # text repetition is therefore valid as long as every citation remains
    # grounded in the authoritative cited line set.
    payload["criterion_evidence"][first_criterion]["quotes"] = [
        script.lines[0].text,
        script.lines[0].text,
    ]
    repeated_quote_verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(payload),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    assert repeated_quote_verdict.approved is False
    payload["criterion_evidence"][first_criterion]["quotes"] = original_quote

    # A critic may quote a contiguous spoken sentence that the delivery
    # allocator split across two adjacent script lines. Both line IDs still
    # provide exact, ordered evidence and must not trigger a contract retry.
    spanning_quote = (
        f"{script.lines[0].text[-12:]} {script.lines[1].text[:12]}"
    )
    payload["criterion_evidence"][first_criterion] = {
        "line_ids": [script.lines[0].line_id, script.lines[1].line_id],
        "quotes": [spanning_quote],
        "rationale": "The evidence spans two adjacent delivery lines.",
    }
    spanning_verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(payload),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    assert spanning_verdict.approved is False
    payload["criterion_evidence"][first_criterion] = {
        "line_ids": [script.lines[0].line_id],
        "quotes": original_quote,
        "rationale": (
            "The quoted opening does not prove this criterion at its "
            "configured blocking threshold."
        ),
    }

    fenced = "```json\n" + __import__("json").dumps(payload) + "\n```"
    with pytest.raises(ValueError, match="without markdown fences"):
        parse_independent_copy_critic_response(
            fenced,
            packet=packet,
            script=script,
            preflight=preflight,
        )

    payload["blocking_issues"][0]["line_ids"] = ["invented-line"]
    with pytest.raises(ValueError, match="unknown script line_ids"):
        parse_independent_copy_critic_response(
            __import__("json").dumps(payload),
            packet=packet,
            script=script,
            preflight=preflight,
        )

    payload["blocking_issues"][0]["line_ids"] = ["s004.l001"]
    payload["criterion_evidence"][first_criterion]["quotes"] = [
        "This sentence was never in the final script."
    ]
    with pytest.raises(ValueError, match="quote is not present"):
        parse_independent_copy_critic_response(
            __import__("json").dumps(payload),
            packet=packet,
            script=script,
            preflight=preflight,
        )


def test_delayed_reveal_reason_must_cite_an_earlier_decision_basis():
    conversion = ConversionIntent(
        product_required=True,
        product_name="MYUPONA",
        confirmed_differentiators=["melatonin-free", "GABA"],
        minimum_differentiators_in_copy=2,
        offer_text="$7.99",
        cta_text="yellow cart",
    )
    criterion = CopyReviewCriterion(
        criterion_id="reason_to_choose",
        instruction=(
            "Connect a confirmed attribute to a decision basis established "
            "before a delayed product reveal."
        ),
        minimum_score=90,
    )
    program = _program().model_copy(update={
        "conversion": conversion,
        "copy_review_criteria": [criterion],
    })

    def make_script(first_line: str):
        return build_script_package(
            script_id="delayed-reveal-selection-basis",
            program_id=program.program_id,
            locale=program.locale,
            target_duration_seconds=40,
            edit_headroom_seconds=2,
            speech_rate_wpm=150,
            primary_speaker_id="narrator",
            lines=[
                ScriptLine(
                    line_id="l1",
                    speaker_id="narrator",
                    text=first_line,
                    beat_id="decision",
                    purpose="establish the viewer decision basis",
                ),
                ScriptLine(
                    line_id="l2",
                    speaker_id="narrator",
                    text="I made one small nighttime routine I could repeat.",
                    beat_id="routine",
                    purpose="continue the use case",
                ),
                ScriptLine(
                    line_id="l3",
                    speaker_id="narrator",
                    text=(
                        "Want a melatonin-free bedtime gummy? MYUPONA is "
                        "melatonin-free and made with GABA."
                    ),
                    beat_id="reveal",
                    purpose="reveal the product",
                ),
                ScriptLine(
                    line_id="l4",
                    speaker_id="narrator",
                    text="It is $7.99 in the yellow cart.",
                    beat_id="action",
                    purpose="state the offer and action",
                ),
            ],
            segments=[
                ScriptSegmentAllocation(
                    segment_index=index,
                    duration_seconds=10,
                    line_ids=[f"l{index}"],
                )
                for index in range(1, 5)
            ],
        )

    circular = make_script("My workday ended, and I put the list away.")
    circular_preflight = preflight_script_copy(program, circular)
    assert circular_preflight.approved is True
    circular_packet = build_independent_copy_critic_packet(
        program,
        circular,
        circular_preflight,
    )
    circular_payload = {
        "approved": True,
        "scores": {"reason_to_choose": 95},
        "criterion_evidence": {
            "reason_to_choose": {
                "line_ids": ["l3", "l4"],
                "quotes": [circular.lines[2].text, circular.lines[3].text],
                "rationale": "The reveal states matching product attributes.",
            }
        },
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }
    with pytest.raises(ValueError, match="before the product reveal"):
        parse_independent_copy_critic_response(
            __import__("json").dumps(circular_payload),
            packet=circular_packet,
            script=circular,
            preflight=circular_preflight,
        )

    earned = make_script(
        "I wanted my nighttime gummy choice to be melatonin-free."
    )
    earned_preflight = preflight_script_copy(program, earned)
    earned_packet = build_independent_copy_critic_packet(
        program,
        earned,
        earned_preflight,
    )
    earned_payload = copy.deepcopy(circular_payload)
    earned_payload["criterion_evidence"]["reason_to_choose"] = {
        "line_ids": ["l1", "l3"],
        "quotes": [earned.lines[0].text, earned.lines[2].text],
        "rationale": (
            "The earlier line completes the selection requirement before "
            "MYUPONA is revealed, and the confirmed attribute matches it."
        ),
    }
    verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(earned_payload),
        packet=earned_packet,
        script=earned,
        preflight=earned_preflight,
    )
    assert verdict.approved is True


def test_product_first_locked_copy_does_not_inherit_delayed_reveal_rule():
    program = _program(reveal_after_fraction=None).model_copy(
        update={
            "copy_review_criteria": [
                CopyReviewCriterion(
                    criterion_id="reason_to_choose",
                    instruction=(
                        "State or clearly show a confirmed reason to consider "
                        "the product."
                    ),
                    minimum_score=85,
                )
            ]
        }
    )
    original = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="product-first-locked-copy-review",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                **_brief().truth_payload,
                "required_verbatim_voiceover": " ".join(
                    line.text for line in original.lines
                ),
            }
        }
    )
    preflight = preflight_script_copy(program, original)
    packet = build_independent_copy_critic_packet(
        program,
        original,
        preflight,
        brief=brief,
    )

    assert packet["review_method"]["product_reveal_strategy"] == "product_first"
    assert "Do not apply the delayed-reveal earlier-line rule" in packet[
        "review_method"
    ]["preference_is_not_a_reason"]
    assert "multimodal visual review" in packet["review_method"][
        "visual_proof_review_boundary"
    ]


def test_director_artifact_is_runtime_hashed_and_revision_is_parented():
    program = _program()
    package = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="director-v40",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    raw = {
        "program": program.model_dump(mode="json"),
        "script": {
            key: value
            for key, value in package.model_dump(mode="json").items()
            if key != "canonical_text_sha256"
        },
    }
    artifact = parse_director_draft_response(
        __import__("json").dumps(raw),
        artifact_id="artifact-v40",
        revision=1,
    )

    assert artifact.script.canonical_text_sha256 == package.canonical_text_sha256
    assert artifact.parent_artifact_sha256 is None
    assert len(artifact.artifact_sha256) == 64

    revision = build_directed_content_artifact(
        artifact_id=artifact.artifact_id,
        revision=2,
        parent_artifact_sha256=artifact.artifact_sha256,
        program=artifact.program,
        script=artifact.script,
    )
    assert revision.parent_artifact_sha256 == artifact.artifact_sha256
    assert revision.artifact_sha256 != artifact.artifact_sha256


def test_revision_packet_is_explicit_and_keeps_artifact_ancestry():
    program = _program()
    script = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="director-v40",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    artifact = build_directed_content_artifact(
        artifact_id="artifact-v40",
        revision=1,
        program=program,
        script=script,
    )
    preflight = preflight_script_copy(program, script)
    packet = build_independent_copy_critic_packet(program, script, preflight)
    critic_payload = {
        "approved": False,
        "scores": {
            criterion["criterion_id"]: 20
            for criterion in packet["review_criteria"]
        },
        "criterion_evidence": {
            criterion["criterion_id"]: {
                "line_ids": [script.lines[0].line_id],
                "quotes": [script.lines[0].text],
                "rationale": (
                    "The quoted opening does not meet this criterion's configured "
                    "blocking threshold."
                ),
            }
            for criterion in packet["review_criteria"]
        },
        "blocking_issues": [{
            "code": "REPLAN",
            "line_ids": [],
            "evidence": "The conversion does not follow the story.",
            "repair_instruction": "Rebuild the bridge using supplied facts.",
        }],
        "repair_scope": "director_replan",
    }
    verdict = parse_independent_copy_critic_response(
        __import__("json").dumps(critic_payload),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    revision_packet = build_director_revision_packet(
        artifact,
        brief=_brief(),
        preflight=preflight,
        verdict=verdict,
    )

    assert revision_packet["revision_contract"]["revision"] == 2
    assert (
        revision_packet["revision_contract"]["parent_artifact_sha256"]
        == artifact.artifact_sha256
    )
    assert revision_packet["revision_contract"]["must_improve"]
    assert (
        revision_packet["delivery_budget_contract"]
        ["segments"][0]["spoken_max_words"]
        == 27
    )
    assert all(
        row["current_score"] < row["minimum_score"]
        for row in revision_packet["revision_contract"]["must_improve"]
    )
    assert "conversation" not in revision_packet


def test_variant_director_packet_exposes_only_current_deliverable_copy():
    current_seed = {
        "deliverable_ordinal": 7,
        "text": "Why do my thoughts bring a megaphone to bed?",
        "target_duration_seconds": 4,
    }
    brief = _brief().model_copy(update={
        "target_duration_seconds": 4,
        "edit_headroom_seconds": 1,
        "speech_rate_wpm": 220,
        "production_contract": VideoProductionContract(
            model_id="omni_flash",
            segment_duration_minimum_seconds=4,
            segment_duration_maximum_seconds=10,
            allowed_segment_durations_seconds=[4, 6, 8, 10],
            required_segment_durations_seconds=[4],
            reference_image_limit=7,
            reference_video_limit=0,
        ),
        "truth_payload": {
            "series_intent": {"variant_index": 7},
            "creative_copy_contract": {
                "copy_authority": "producer_draft_editable",
                "director_seed_voiceover": current_seed,
                "director_seed_voiceovers": [
                    {
                        "deliverable_ordinal": 1,
                        "text": "Sibling copy must not enter this request.",
                    },
                    current_seed,
                ],
            },
            "producer_intent_spec": {
                "user_goal": "Create two independent videos.",
                "deliverables": [
                    {"ordinal": 1, "script_text": "Sibling copy."},
                    {"ordinal": 7, "script_text": current_seed["text"]},
                ],
            },
        },
    })

    packet = build_initial_director_packet(brief)
    truth = packet["project_brief"]["truth_payload"]

    assert "director_seed_voiceovers" not in truth[
        "creative_copy_contract"
    ]
    assert truth["creative_copy_contract"][
        "director_seed_voiceover"
    ] == current_seed
    assert truth["producer_intent_spec"]["deliverables"] == [{
        "ordinal": 7,
        "script_text": current_seed["text"],
    }]
    assert truth["director_author_scope"] == {
        "current_deliverable_only": True,
        "current_deliverable_ordinal": 7,
        "expected_script_segment_count": 1,
        "series_deliverable_count": 2,
        "instruction": (
            "Author only the current deliverable. Project-wide target counts "
            "describe sibling final videos and must never become script "
            "segments in this Director response. Map project requirements to "
            "observable evidence in this current deliverable only."
        ),
    }
    assert "Sibling copy" not in __import__("json").dumps(
        packet["project_brief"]
    )


def test_segment_delivery_ceiling_is_capped_by_global_editing_budget():
    brief = _brief().model_copy(update={
        "target_duration_seconds": 4,
        "edit_headroom_seconds": 1,
        "speech_rate_wpm": 220,
        "display_reading_rate_wpm": 220,
        "production_contract": VideoProductionContract(
            model_id="omni_flash",
            segment_duration_minimum_seconds=4,
            segment_duration_maximum_seconds=10,
            allowed_segment_durations_seconds=[4, 6, 8, 10],
            required_segment_durations_seconds=[4],
            reference_image_limit=7,
            reference_video_limit=0,
        ),
    })

    contract = build_delivery_budget_contract(brief)

    assert contract["spoken_global_max_words"] == 11
    assert contract["segments"][0]["spoken_max_words"] == 11
    assert contract["rules"][
        "segment_ceiling_is_already_capped_by_global_budget"
    ] is True


def test_director_packet_and_artifact_cannot_invent_capabilities_or_change_brief():
    brief = _brief()
    packet = build_initial_director_packet(brief)
    assert packet["project_brief"]["truth_payload"] == brief.truth_payload
    assert (
        packet["delivery_budget_contract"]
        ["spoken_global_max_words"]
        == 104
    )
    assert (
        packet["delivery_budget_contract"]
        ["display_global_max_words"]
        == 76
    )
    assert "canonical_text_sha256" not in __import__("json").dumps(
        packet["output_contract"]
    )

    program = _program()
    script = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="director-v40",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    artifact = build_directed_content_artifact(
        artifact_id="artifact-v40",
        revision=1,
        program=program,
        script=script,
    )
    validate_directed_artifact_against_brief(artifact, brief)

    invented_program = program.model_copy(
        update={
            "execution_graph": [
                DirectorCapabilityNode(
                    node_id="invented",
                    capability="media.magic",
                    input_contract="Anything",
                    output_contract="Everything",
                )
            ]
        }
    )
    invented = build_directed_content_artifact(
        artifact_id="invented",
        revision=1,
        program=invented_program,
        script=script,
    )
    with pytest.raises(ValueError, match="unregistered capability"):
        validate_directed_artifact_against_brief(invented, brief)

    changed_policy_program = program.model_copy(
        update={
            "execution_graph": [
                node.model_copy(update={"policy": {"unregistered_override": True}})
                if node.node_id == "copy"
                else node
                for node in program.execution_graph
            ]
        }
    )
    changed_policy = build_directed_content_artifact(
        artifact_id="changed-policy",
        revision=1,
        program=changed_policy_program,
        script=script,
    )
    with pytest.raises(ValueError, match="changed policy"):
        validate_directed_artifact_against_brief(changed_policy, brief)


def test_script_contract_supports_a_true_no_dialogue_video():
    script = build_script_package(
        script_id="silent-story",
        program_id="silent-program",
        locale="en-US",
        target_duration_seconds=12,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        audio_mode="sound_design",
        primary_speaker_id=None,
        lines=[],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=6,
                line_ids=[],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=6,
                line_ids=[],
            ),
        ],
    )

    assert script.audio_mode == "sound_design"
    assert script.spoken_word_count == 0
    assert script.primary_speaker_id is None

    display_script = build_script_package(
        script_id="silent-story-with-display-copy",
        program_id="silent-program",
        locale="en-US",
        target_duration_seconds=12,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        audio_mode="sound_design",
        primary_speaker_id=None,
        lines=[
            ScriptLine(
                line_id="display-1",
                speaker_id="on-screen",
                text="One clear choice.",
                beat_id="beat-1",
                purpose="display copy",
                delivery_mode="display",
            )
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=12,
                line_ids=["display-1"],
            )
        ],
    )
    assert display_script.spoken_word_count == 0
    assert display_script.lines[0].delivery_mode == "display"

    over_budget_display = build_script_package(
        script_id="silent-story-over-display-budget",
        program_id="silent-program",
        locale="en-US",
        target_duration_seconds=12,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        display_reading_rate_wpm=120,
        audio_mode="sound_design",
        primary_speaker_id=None,
        lines=[
            ScriptLine(
                line_id="display-long",
                speaker_id="on-screen",
                text=(
                    "This deliberately long screen of copy asks the viewer "
                    "to read far more words than a twelve second video can "
                    "comfortably communicate before the next scene appears."
                ),
                beat_id="beat-1",
                purpose="display copy",
                delivery_mode="display",
            )
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=12,
                line_ids=["display-long"],
            )
        ],
    )
    display_program = _program().model_copy(
        update={
            "program_id": "silent-program",
            "target_duration_seconds": 12,
            "conversion": ConversionIntent(product_required=False),
        }
    )
    display_report = preflight_script_copy(
        display_program,
        over_budget_display,
    )
    assert display_report.approved is False
    assert display_report.display_word_count > (
        display_report.display_budget_words
    )
    assert {
        issue.code for issue in display_report.issues
    } >= {
        "DISPLAY_COPY_OVER_BUDGET",
        "DISPLAY_SEGMENT_OVER_BUDGET",
    }

    invalid = script.model_dump(mode="json")
    invalid["lines"] = [{
        "line_id": "unexpected",
        "speaker_id": "narrator",
        "text": "This must not exist.",
        "beat_id": "beat-1",
        "purpose": "narration",
    }]
    with pytest.raises(ValidationError, match="non-spoken audio_mode"):
        type(script).model_validate(invalid)


def test_product_required_nonspoken_program_uses_display_copy_as_authority():
    base = _program()
    program = base.model_copy(
        update={
            "target_duration_seconds": 12,
            "conversion": base.conversion.model_copy(
                update={
                    "reveal_after_fraction": None,
                    "minimum_differentiators_in_copy": 2,
                    "protected_stake_terms": [],
                    "require_post_cta_human_agency": False,
                }
            ),
            "creative_strategy": {
                "nonverbal_conversion": {
                    "delivery": "local display copy over the product beat",
                }
            },
        }
    )
    script = build_script_package(
        script_id="silent-product-story",
        program_id=program.program_id,
        locale=program.locale,
        target_duration_seconds=12,
        edit_headroom_seconds=0,
        speech_rate_wpm=150,
        audio_mode="music_only",
        primary_speaker_id=None,
        display_reading_rate_wpm=120,
        lines=[
            ScriptLine(
                line_id="display-product",
                speaker_id="display",
                text=(
                    "MYUPONA Sleep Ease Gummies are melatonin-free with GABA."
                ),
                beat_id="product",
                purpose="product rationale",
                delivery_mode="display",
            ),
            ScriptLine(
                line_id="display-action",
                speaker_id="display",
                text="$7.99. Find them in the yellow cart.",
                beat_id="action",
                purpose="offer and action",
                delivery_mode="display",
            ),
        ],
        segments=[
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=6,
                line_ids=["display-product"],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=6,
                line_ids=["display-action"],
            ),
        ],
    )

    report = preflight_script_copy(program, script)
    assert report.approved is True
    assert report.differentiators_found == [
        "melatonin-free",
        "GABA",
    ]

    missing_offer_script = script.model_copy(
        update={
            "lines": [
                script.lines[0],
                script.lines[1].model_copy(
                    update={
                        "text": "Find them in the yellow cart.",
                    }
                ),
            ],
        },
    )
    rejected = preflight_script_copy(program, missing_offer_script)
    assert rejected.approved is False
    assert "CONFIRMED_OFFER_MISSING" in {
        issue.code for issue in rejected.issues
    }


def test_product_first_script_is_allowed_without_project_reveal_boundary():
    program = _program(reveal_after_fraction=None)
    original = script_package_from_creative_result(
        _historic_v40_result(),
        script_id="product-first-script",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    script = build_script_package(
        script_id=original.script_id,
        program_id=original.program_id,
        locale=original.locale,
        target_duration_seconds=original.target_duration_seconds,
        edit_headroom_seconds=original.edit_headroom_seconds,
        speech_rate_wpm=original.speech_rate_wpm,
        primary_speaker_id=original.primary_speaker_id,
        lines=[
            line.model_copy(
                update={
                    "text": (
                        "MYUPONA Sleep Ease Gummies are part of this "
                        "nighttime routine."
                    )
                }
            )
            if index == 0
            else line
            for index, line in enumerate(original.lines)
        ],
        segments=original.segments,
    )
    report = preflight_script_copy(program, script)

    assert "PRODUCT_REVEALED_TOO_EARLY" not in {
        issue.code for issue in report.issues
    }


def test_confirmed_product_alias_satisfies_identity_without_weakening_offer():
    program = _program().model_copy(
        update={
            "conversion": _program().conversion.model_copy(
                update={
                    "product_name": "MYUPONA Sleep Easy Gummies",
                    "product_name_aliases": [
                        "MYUPONA Sleep Ease Gummies"
                    ],
                }
            )
        }
    )
    historic = copy.deepcopy(_historic_v40_result())
    historic["complete_video_script"]["segments"][-1][
        "dialogue_lines"
    ][0]["line"] = (
        "MYUPONA Sleep Ease Gummies fit clean endings; $7.99—find "
        "them in the yellow cart below."
    )
    script = script_package_from_creative_result(
        historic,
        script_id="confirmed-alias",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=2,
    )
    report = preflight_script_copy(program, script)

    assert "PRODUCT_NOT_NAMED" not in {
        issue.code for issue in report.issues
    }
