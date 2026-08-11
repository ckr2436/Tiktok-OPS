from __future__ import annotations

import pytest

from app.services.hermes_agent.content_intent import (
    CreativeIntentManifest,
    CreativeIntentRequirement,
    RequirementExecutionMapping,
    creative_intent_manifest_sha256,
    requirement_review_packet,
    sign_creative_intent_manifest,
    validate_requirement_execution_coverage,
)
from app.services.hermes_agent.content_production_plan import (
    ProductionRequirementMapping,
)


def _manifest() -> CreativeIntentManifest:
    return CreativeIntentManifest(
        objective="Create an original TikTok variation without losing the reference hook's stopping power.",
        requirements=[
            CreativeIntentRequirement(
                requirement_id="R-001",
                kind="reference_transfer",
                priority="critical",
                scope="time_window",
                start_seconds=0,
                end_seconds=3,
                intent="Transfer the reference opening's immediate surprise and conflict mechanism.",
                evidence_quote="画面节奏，钩子参考原视频，而不是直接复制",
                interpretation="Invent a new visual premise whose first three seconds create an equally immediate contradiction and unanswered question.",
                observable_checks=[
                    "A new visual contradiction is understandable without captions by second three.",
                    "The opening creates an unanswered question that motivates the next beat.",
                ],
                creative_freedom=["actors", "setting", "props", "metaphor"],
                must_not_reuse=["source actors", "source wording", "source pixels"],
            ),
            CreativeIntentRequirement(
                requirement_id="R-002",
                kind="differentiation",
                priority="high",
                scope="project",
                intent="The new video must be an AI-authored creative reconstruction.",
                evidence_quote="肯定还是要AI重新生成",
                interpretation="Create a distinct story and imagery while preserving only the authorized effectiveness mechanism.",
                observable_checks=["No source pixels, wording, actors, or signature scene are reused."],
            ),
        ],
    )


def test_intent_manifest_is_signed_and_tamper_evident() -> None:
    signed = sign_creative_intent_manifest(_manifest())
    assert signed.manifest_sha256 == creative_intent_manifest_sha256(signed)

    payload = signed.model_dump(mode="json")
    payload["requirements"][0]["interpretation"] = "A weaker generic opening."
    with pytest.raises(ValueError, match="manifest_sha256"):
        CreativeIntentManifest.model_validate(payload)


def test_critical_requirements_cannot_disappear_between_ai_roles() -> None:
    manifest = sign_creative_intent_manifest(_manifest())
    with pytest.raises(ValueError, match="lack execution mappings"):
        validate_requirement_execution_coverage(
            manifest,
            [],
            valid_script_line_ids={"L-001"},
            valid_capability_node_ids={"C-001"},
            valid_segment_indices={1},
        )

    mappings = [
        RequirementExecutionMapping(
            requirement_id="R-001",
            implementation="Open on a new impossible bedside contradiction, then reveal its cause.",
            script_line_ids=["L-001"],
            capability_node_ids=["C-001"],
            segment_indices=[1],
            evidence_plan=["The contradiction reads in the first contact-sheet frame."],
        ),
        RequirementExecutionMapping(
            requirement_id="R-002",
            implementation="Use a newly authored character, setting, action, and wording.",
            script_line_ids=["L-001"],
            capability_node_ids=["C-001"],
            segment_indices=[1],
            evidence_plan=["The final frames contain no copied source elements."],
        ),
    ]
    validate_requirement_execution_coverage(
        manifest,
        mappings,
        valid_script_line_ids={"L-001"},
        valid_capability_node_ids={"C-001"},
        valid_segment_indices={1},
    )


def test_requirement_coordinates_and_observable_checks_reach_media_contract() -> None:
    manifest = sign_creative_intent_manifest(_manifest())
    packet = requirement_review_packet(manifest, ["R-001"])
    assert packet[0]["observable_checks"] == manifest.requirements[0].observable_checks
    assert packet[0]["must_not_reuse"] == manifest.requirements[0].must_not_reuse

    production = ProductionRequirementMapping(
        requirement_id="R-001",
        beat_ids=["B-001"],
        reference_ids=["REF-001"],
        audio_cue_ids=["A-001"],
        line_ids=["L-001"],
        implementation_evidence=[
            "B-001 carries the visible contradiction from 0.0 to 3.0 seconds."
        ],
    )
    assert production.requirement_id == "R-001"
    assert production.beat_ids == ["B-001"]


def test_unknown_director_coordinates_fail_closed() -> None:
    manifest = sign_creative_intent_manifest(_manifest())
    mappings = [
        RequirementExecutionMapping(
            requirement_id=requirement.requirement_id,
            implementation="Concrete implementation",
            script_line_ids=["L-missing"],
            capability_node_ids=["C-001"],
            segment_indices=[1],
            evidence_plan=["Visible evidence"],
        )
        for requirement in manifest.requirements
    ]
    with pytest.raises(ValueError, match="unknown script lines"):
        validate_requirement_execution_coverage(
            manifest,
            mappings,
            valid_script_line_ids={"L-001"},
            valid_capability_node_ids={"C-001"},
            valid_segment_indices={1},
        )
