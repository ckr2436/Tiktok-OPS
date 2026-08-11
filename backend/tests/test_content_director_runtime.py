from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.services.hermes_agent.client import (
    HermesContentCriticClient,
    HermesContentDirectorClient,
)
from app.services.hermes_agent.content_director import (
    ConversionIntent,
    CopyReviewCriterion,
    DirectorCapabilitySpec,
    DirectorProjectBrief,
    IndependentCopyCriticVerdict,
    VideoProductionContract,
    parse_director_author_draft_response,
    preflight_script_copy,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
    _apply_delivery_budget_patch,
    _author_contract_repair_coordinates,
    _model_request_idempotency_key,
    run_content_director_copy_loop,
)
from app.services.hermes_agent.content_director_profile import (
    compile_universal_director_series_brief,
    default_director_loop_policy,
)


class _FakeClient:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def create_response(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        return {
            "output_text": output if isinstance(output, str) else json.dumps(output)
        }, 7


def test_content_roles_use_dedicated_enable_flags(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_AGENT_ENABLED", False)
    monkeypatch.setattr(
        settings,
        "HERMES_CONTENT_DIRECTOR_AGENT_ENABLED",
        True,
    )
    monkeypatch.setattr(
        settings,
        "HERMES_CONTENT_CRITIC_AGENT_ENABLED",
        True,
    )

    assert HermesContentDirectorClient().enabled is True
    assert HermesContentCriticClient().enabled is True


def test_universal_profile_finishes_copy_before_spending_media():
    policy = default_director_loop_policy()

    assert policy["maximum_revisions"] == 2
    assert policy["maximum_series_revisions"] == 2
    assert policy["maximum_contract_repairs_per_revision"] >= 1


def test_model_request_idempotency_is_stable_per_stage_but_fresh_for_recovery():
    shared = {
        "role": "director",
        "revision": 1,
        "repair_attempt": 0,
        "input_text": '{"same":"packet"}',
        "instructions": "same instructions",
    }
    first = _model_request_idempotency_key("project:stage:10", **shared)
    redelivery = _model_request_idempotency_key("project:stage:10", **shared)
    recovery = _model_request_idempotency_key("project:stage:11", **shared)

    assert first == redelivery
    assert first != recovery


def test_universal_profile_lifts_user_locked_script_into_runtime_truth():
    exact = "3:07 a.m. again?\n\nSearch MYUPONA on TikTok Shop."
    brief = compile_universal_director_series_brief(
        series_id="producer-script.series",
        objective="Produce the supplied conversion script.",
        platform="tiktok",
        locale="en-US",
        audience="US adults",
        target_count=3,
        minimum_duration_seconds=40,
        maximum_duration_seconds=40,
        product_required=False,
        brand_name=None,
        product_name=None,
        market="US",
        project_brief=None,
        creative_copy_contract={"required_verbatim_voiceover": exact},
        product_truth={},
    )

    assert brief.truth_payload["required_verbatim_voiceover"] == exact
    assert brief.truth_payload["required_verbatim_voiceover_lines"] == [
        {
            "line_id": "LOCKED-VO-001",
            "delivery_mode": "spoken",
            "text": "3:07 a.m. again?",
        },
        {
            "line_id": "LOCKED-VO-002",
            "delivery_mode": "spoken",
            "text": "Search MYUPONA on TikTok Shop.",
        },
    ]
    assert brief.truth_payload["locked_script_variant_policy"]["mode"] == (
        "same_copy_visual_variants"
    )
    assert {
        item.criterion_id for item in brief.series_global_review_criteria
    } == {"series_truth_boundary"}
    assert "audio_strategy" not in {
        item.dimension_id for item in brief.diversity_requirements
    }
    assert any(
        "same immutable copy is intentionally reused" in item.casefold()
        for item in brief.quality_rubric
    )


def test_explicit_script_policy_never_derives_visual_variants_from_count():
    exact = "One supplied script."
    with pytest.raises(ValueError, match="REUSE_POLICY_REQUIRED"):
        compile_universal_director_series_brief(
            series_id="explicit-script-policy.series",
            objective="Create three independently directed outputs.",
            platform="tiktok",
            locale="en-US",
            audience="US adults",
            target_count=3,
            minimum_duration_seconds=40,
            maximum_duration_seconds=40,
            product_required=False,
            brand_name=None,
            product_name=None,
            market="US",
            project_brief=None,
            creative_copy_contract={
                "required_verbatim_voiceover": exact,
                "script_reuse_mode": "single",
            },
            product_truth={},
        )


def test_distinct_deliverable_scripts_are_exposed_as_hash_bound_manifest_only():
    scripts = [
        {
            "deliverable_ordinal": 1,
            "label": "First",
            "objective": "Tell the first story",
            "text": "First exact script.",
            "sha256": "a" * 64,
            "target_duration_seconds": 40,
        },
        {
            "deliverable_ordinal": 2,
            "label": "Second",
            "objective": "Tell the second story",
            "text": "Second exact script.",
            "sha256": "b" * 64,
            "target_duration_seconds": 40,
        },
    ]
    brief = compile_universal_director_series_brief(
        series_id="distinct-scripts.series",
        objective="Produce two supplied scripts independently.",
        platform="tiktok",
        locale="en-US",
        audience="US adults",
        target_count=2,
        minimum_duration_seconds=40,
        maximum_duration_seconds=40,
        product_required=False,
        brand_name=None,
        product_name=None,
        market="US",
        project_brief=None,
        creative_copy_contract={
            "required_verbatim_voiceovers": scripts,
            "script_reuse_mode": "distinct_per_deliverable",
        },
        product_truth={},
    )

    assert brief.truth_payload["deliverable_script_manifest"][0]["sha256"] == "a" * 64
    assert "required_verbatim_voiceover" not in brief.truth_payload
    assert "text" not in brief.truth_payload["deliverable_script_manifest"][0]


def _brief() -> DirectorProjectBrief:
    return DirectorProjectBrief(
        brief_id="generic-explainer",
        objective="Explain one supplied concept clearly.",
        content_type_hint="animated explainer",
        platform="YouTube Shorts",
        locale="en-US",
        audience="Adult beginners.",
        target_duration_seconds=20,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        aspect_ratio="9:16",
        conversion=ConversionIntent(product_required=False),
        truth_payload={"concept": "A lever changes the force-distance tradeoff."},
        capability_catalog=[
            DirectorCapabilitySpec(
                capability="truth.normalize",
                input_contract="ProjectInputs",
                output_contract="TruthPacket",
            ),
            DirectorCapabilitySpec(
                capability="copy.write",
                input_contract="VideoProgramSpec",
                output_contract="ScriptPackage",
            ),
            DirectorCapabilitySpec(
                capability="copy.review",
                input_contract="ScriptPackage",
                output_contract="CopyReview",
            ),
        ],
        copy_review_criteria=[
            CopyReviewCriterion(
                criterion_id="clear",
                instruction="A beginner understands the supplied concept.",
                minimum_score=80,
            ),
        ],
        source_truth_refs=["truth:lever"],
    )


def _draft(line: str) -> dict:
    return {
        "program": {
            "schema_version": "2.0",
            "program_id": "lever-program",
            "content_type": "animated explainer",
            "audio_mode": "spoken",
            "creative_strategy": {
                "format": "plain-language visual explanation",
            },
            "execution_graph": [
                {
                    "node_id": "facts",
                    "capability": "truth.normalize",
                    "depends_on": [],
                },
                {
                    "node_id": "copy",
                    "capability": "copy.write",
                    "depends_on": ["facts"],
                },
                {
                    "node_id": "critic",
                    "capability": "copy.review",
                    "depends_on": ["copy"],
                },
            ],
        },
        "script": {
            "schema_version": "2.0",
            "script_id": "lever-script",
            "program_id": "lever-program",
            "audio_mode": "spoken",
            "primary_speaker_id": "narrator",
            "lines": [{
                "line_id": "l1",
                "speaker_id": "narrator",
                "text": line,
                "beat_id": "explain",
                "purpose": "explanation",
            }],
            "segments": [{
                "segment_index": 1,
                "line_ids": ["l1"],
            }],
        },
    }


def _clear_evidence(line: str, rationale: str) -> dict:
    return {
        "clear": {
            "line_ids": ["l1"],
            "quotes": [line],
            "rationale": rationale,
        }
    }


def test_contract_repair_coordinates_separate_lines_nodes_and_capabilities():
    draft = _draft("A lever changes the tradeoff.")
    draft["program"]["requirement_execution"] = [{
        "requirement_id": "R-001",
        "implementation": "Explain the supplied fact.",
        "script_line_ids": ["v4.l1"],
        "capability_node_ids": ["copy.write"],
        "segment_indices": [1],
        "evidence_plan": ["The spoken line explains the tradeoff."],
    }]

    coordinates = _author_contract_repair_coordinates(
        json.dumps(draft),
        brief=_brief(),
    )

    assert coordinates == {
        "script_line_ids": ["l1"],
        "capability_node_ids": ["facts", "copy", "critic"],
        "segment_indices": [1],
        "registered_capability_names": [
            "truth.normalize",
            "copy.write",
            "copy.review",
        ],
    }


def test_delivery_patch_keeps_shortening_and_ignores_same_length_paraphrase():
    brief = _brief().model_copy(update={
        "target_duration_seconds": 4,
        "edit_headroom_seconds": 0,
        "speech_rate_wpm": 60,
    })
    draft = _draft("A lever changes.")
    draft["script"]["lines"].append({
        "line_id": "l2",
        "speaker_id": "narrator",
        "text": "Force trades distance.",
        "beat_id": "explain",
        "purpose": "explanation",
    })
    draft["script"]["segments"][0]["line_ids"].append("l2")
    artifact = parse_director_author_draft_response(
        json.dumps(draft),
        brief=brief,
        artifact_id="mixed-budget-patch",
        revision=1,
        parent_artifact_sha256=None,
    )

    repaired = _apply_delivery_budget_patch(
        artifact,
        json.dumps({"line_replacements": [
            {"line_id": "l1", "text": "Levers change."},
            {"line_id": "l2", "text": "Distance trades force."},
        ]}),
        brief=brief,
    )

    assert [line.text for line in repaired.script.lines] == [
        "Levers change.",
        "Force trades distance.",
    ]


@pytest.mark.asyncio
async def test_user_locked_voiceover_is_materialized_before_critic():
    exact = "3:07 a.m. again?"
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": exact,
            },
        }
    )
    director = _FakeClient([_draft("Three in the morning again?")])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            exact,
            "The runtime-locked source line is present verbatim.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="locked-copy",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=0,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert result.final_artifact is not None
    assert result.final_artifact.script.lines[0].text == exact
    critic_packet = json.loads(critic.calls[0]["input_text"])
    assert critic_packet["script"]["lines"][0]["text"] == exact
    assert (
        critic_packet["review_method"]["immutable_user_copy_authority"]
        is not None
    )
    assert "Apply only the criterion IDs" in (
        critic_packet["review_method"][
            "registered_criteria_are_exclusive"
        ]
    )
    assert critic_packet["review_method"]["adjacency_is_not_a_bridge"] is None
    assert critic_packet["review_method"]["preference_is_not_a_reason"] is None


@pytest.mark.asyncio
async def test_explicit_restart_can_start_fresh_revision_from_accepted_ancestor():
    line = "A lever trades force for distance."
    director = _FakeClient([_draft(line)])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            line,
            "The new audited revision explains the supplied concept.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="accepted-ancestor-restart",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=0,
        ),
        director_client=director,
        critic_client=critic,
        initial_revision=2,
        parent_artifact_sha256="a" * 64,
        allow_fresh_revision_from_accepted_ancestor=True,
    )

    assert result.status == "approved"
    assert result.final_artifact is not None
    assert result.final_artifact.revision == 2
    assert result.final_artifact.parent_artifact_sha256 == "a" * 64
    assert len(director.calls) == 1


@pytest.mark.asyncio
async def test_user_locked_voiceover_fails_closed_on_block_count_mismatch():
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": "First block.\n\nSecond block.",
            },
        }
    )
    director = _FakeClient([_draft("Only one model line.")])
    critic = _FakeClient([])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="locked-copy-count-mismatch",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=0,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "quality_pause"
    assert len(critic.calls) == 0
    assert "expected=2, actual=1" in result.contract_errors[0]


@pytest.mark.asyncio
async def test_locked_voiceover_repairs_only_missing_segment_allocation():
    exact_blocks = ["First immutable block.", "Second immutable block."]
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": "\n\n".join(exact_blocks),
            },
        }
    )
    incomplete = _draft("Model placeholder one.")
    incomplete["script"]["lines"] = [
        {
            "line_id": "l1",
            "speaker_id": "narrator",
            "text": "Model placeholder one.",
            "beat_id": "explain",
            "purpose": "explanation",
        },
        {
            "line_id": "l2",
            "speaker_id": "narrator",
            "text": "Model placeholder two.",
            "beat_id": "explain",
            "purpose": "explanation",
        },
    ]
    incomplete["script"]["segments"][0]["line_ids"] = []
    director = _FakeClient([
        incomplete,
        {"segment_indices": [1, 1]},
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            exact_blocks[0],
            "The immutable line is clear and allocated.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="locked-copy-allocation-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(director.calls) == 2
    assert result.director_attempts[-1].model is None
    assert result.final_artifact is not None
    assert [
        line.text for line in result.final_artifact.script.lines
    ] == exact_blocks
    assert result.final_artifact.script.segments[0].line_ids == [
        "l1",
        "l2",
    ]


@pytest.mark.asyncio
async def test_locked_voiceover_repairs_only_overloaded_segment_allocation():
    exact_blocks = [
        "One two three four five six seven eight nine.",
        "Ten eleven twelve thirteen fourteen fifteen sixteen seventeen.",
        "Eighteen nineteen twenty twentyone twentytwo twentythree.",
        "Twentyfour twentyfive twentysix twentyseven twentyeight twentynine.",
    ]
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": "\n\n".join(exact_blocks),
            },
            "production_contract": VideoProductionContract(
                model_id="two-ten-second-segments",
                segment_duration_minimum_seconds=10,
                segment_duration_maximum_seconds=10,
                allowed_segment_durations_seconds=[10],
                reference_image_limit=1,
                reference_video_limit=0,
            ),
        }
    )
    overloaded = _draft("placeholder")
    overloaded["script"]["lines"] = [
        {
            "line_id": f"l{position}",
            "speaker_id": "narrator",
            "text": f"Model placeholder {position}.",
            "beat_id": "explain",
            "purpose": "explanation",
        }
        for position in range(1, 5)
    ]
    overloaded["script"]["segments"] = [
        {"segment_index": 1, "line_ids": []},
        {
            "segment_index": 2,
            "line_ids": ["l1", "l2", "l3", "l4"],
        },
    ]
    director = _FakeClient([
        overloaded,
        {"segment_indices": [1, 1, 2, 2]},
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            exact_blocks[0],
            "The immutable line is clear and fits its segment.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="locked-copy-overloaded-allocation-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(director.calls) == 2
    assert result.director_attempts[-1].model is None
    assert result.final_artifact is not None
    allocated_line_ids = [
        line_id
        for segment in result.final_artifact.script.segments
        for line_id in segment.line_ids
    ]
    assert allocated_line_ids == ["l1", "l2", "l3", "l4"]
    assert all(
        segment.line_ids
        for segment in result.final_artifact.script.segments
    )


@pytest.mark.asyncio
async def test_locked_voiceover_chains_format_then_allocation_repair():
    exact_blocks = [
        "One two three four five six seven eight nine.",
        "Ten eleven twelve thirteen fourteen fifteen sixteen seventeen.",
        "Eighteen nineteen twenty twentyone twentytwo twentythree.",
        "Twentyfour twentyfive twentysix twentyseven twentyeight twentynine.",
    ]
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                "required_verbatim_voiceover": "\n\n".join(exact_blocks),
                "locked_voiceover_feasible_allocation": {
                    "speech_rate_wpm": 150,
                    "segment_indices": [1, 1, 2, 2],
                    "segment_durations_seconds": [10, 10],
                    "authority": "runtime_verified_reference_for_director",
                },
            },
            "production_contract": VideoProductionContract(
                model_id="two-ten-second-segments",
                segment_duration_minimum_seconds=10,
                segment_duration_maximum_seconds=10,
                allowed_segment_durations_seconds=[10],
                reference_image_limit=1,
                reference_video_limit=0,
            ),
        }
    )
    overloaded = _draft("placeholder")
    overloaded["script"]["lines"] = [
        {
            "line_id": f"l{position}",
            "speaker_id": "narrator",
            "text": f"Model placeholder {position}.",
            "beat_id": "explain",
            "purpose": "explanation",
        }
        for position in range(1, 5)
    ]
    overloaded["script"]["segments"] = [
        {"segment_index": 1, "line_ids": []},
        {
            "segment_index": 2,
            "line_ids": ["l1", "l2", "l3", "l4"],
        },
    ]
    director = _FakeClient([
        "not valid json",
        overloaded,
        {"segment_indices": [1, 1, 2, 2]},
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            exact_blocks[0],
            "The immutable line is clear and fits its segment.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="locked-copy-chained-contract-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=2,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(director.calls) == 3
    assert result.director_attempts[-1].model is None
    assert result.final_artifact is not None
    assert result.final_artifact.script.segments[0].line_ids == [
        "l1",
        "l2",
    ]


@pytest.mark.asyncio
async def test_copy_loop_revises_explicit_version_then_approves():
    director = _FakeClient([
        _draft("A lever changes the tradeoff"),
        _draft("A lever trades force for distance."),
    ])
    critic = _FakeClient([
        {
            "approved": False,
            "scores": {"clear": 30},
            "criterion_evidence": _clear_evidence(
                "A lever changes the tradeoff",
                "The quoted line names a change but does not explain it to a beginner.",
            ),
            "blocking_issues": [{
                "code": "INCOMPLETE",
                "line_ids": ["l1"],
                "evidence": "The sentence is incomplete.",
                "repair_instruction": "Finish the supplied explanation.",
            }],
            "repair_scope": "copy_only",
        },
        {
            "approved": True,
            "scores": {"clear": 95},
            "criterion_evidence": _clear_evidence(
                "A lever trades force for distance.",
                "The quoted line states both sides of the tradeoff in plain language.",
            ),
            "blocking_issues": [],
            "repair_scope": "copy_only",
        },
    ])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(result.artifacts) == 2
    assert [attempt.operation for attempt in result.director_attempts] == [
        "initial",
        "revision",
    ]
    assert all(
        attempt.outcome == "accepted"
        for attempt in result.director_attempts
    )
    assert len(result.reviews) == 2
    assert (
        result.artifacts[1].parent_artifact_sha256
        == result.artifacts[0].artifact_sha256
    )
    assert len(director.calls) == 2
    assert len(critic.calls) == 2
    assert "conversation" not in director.calls[1]
    critic_packet = json.loads(critic.calls[0]["input_text"])
    assert (
        critic_packet["project_brief"]["truth_payload"]
        == _brief().truth_payload
    )


@pytest.mark.asyncio
async def test_invalid_critic_json_gets_one_bounded_contract_repair():
    line = "A lever trades force for distance."
    director = _FakeClient([_draft(line)])
    critic = _FakeClient([
        "I reviewed the script, but this is not JSON.",
        {
            "approved": True,
            "scores": {"clear": 95},
            "criterion_evidence": _clear_evidence(
                line,
                "The exact line explains both sides of the tradeoff.",
            ),
            "blocking_issues": [],
            "repair_scope": "copy_only",
        },
    ])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-critic-contract-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(critic.calls) == 2
    repair_packet = json.loads(critic.calls[1]["input_text"])
    assert repair_packet["role"] == "independent_copy_critic_contract_repair"
    assert repair_packet["repair_attempt"] == 1
    assert "not valid JSON" in repair_packet["prior_validation_error"]
    assert repair_packet["prior_invalid_response"] == (
        "I reviewed the script, but this is not JSON."
    )
    assert repair_packet["valid_criterion_ids"] == ["clear"]
    assert repair_packet["valid_script_lines"] == [
        {"line_id": "l1", "text": line}
    ]
    assert result.contract_errors[0].startswith(
        "critic contract rejected attempt=0 response_sha256="
    )


@pytest.mark.asyncio
async def test_exhausted_critic_contract_repairs_quality_pause_before_media():
    line = "A lever trades force for distance."
    director = _FakeClient([_draft(line)])
    critic = _FakeClient(["not json", "still not json"])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-critic-contract-pause",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "quality_pause"
    assert result.final_artifact is not None
    assert len(result.reviews) == 0
    assert len(result.contract_errors) == 2
    assert len(critic.calls) == 2
    assert "no media stage was authorized" in result.reason


@pytest.mark.asyncio
async def test_delivery_budget_is_contract_repaired_before_critic_call():
    brief = _brief().model_copy(
        update={
            "target_duration_seconds": 4,
            "edit_headroom_seconds": 0,
            "speech_rate_wpm": 60,
        }
    )
    repaired_line = "Levers trade force."
    director = _FakeClient([
        _draft("A lever changes the tradeoff."),
        {
            "line_replacements": [{
                "line_id": "l1",
                "text": repaired_line,
            }],
        },
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            repaired_line,
            "The repaired line fits the supplied delivery budget.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="lever-budget-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [attempt.operation for attempt in result.director_attempts] == [
        "initial",
        "contract_repair",
    ]
    assert [attempt.outcome for attempt in result.director_attempts] == [
        "contract_rejected",
        "accepted",
    ]
    assert len(critic.calls) == 1
    repair_packet = json.loads(director.calls[1]["input_text"])
    assert repair_packet["role"] == (
        "content_director_delivery_budget_patch"
    )
    assert repair_packet["editable_lines"] == [{
        "line_id": "l1",
        "text": "A lever changes the tradeoff.",
        "delivery_mode": "spoken",
        "current_word_count": 5,
        "maximum_word_count": 4,
    }]
    assert "DISPLAY_SEGMENT_OVER_BUDGET" not in str(
        repair_packet["validation_error"]
    )
    assert "SPOKEN_COPY_OVER_BUDGET" in str(
        repair_packet["validation_error"]
    )
    assert repair_packet["exact_reduction_targets"][0][
        "minimum_words_to_remove"
    ] == 1


@pytest.mark.asyncio
async def test_delivery_budget_noop_patch_gets_one_focused_retry():
    brief = _brief().model_copy(
        update={
            "target_duration_seconds": 4,
            "edit_headroom_seconds": 0,
            "speech_rate_wpm": 60,
        }
    )
    original_line = "A lever changes the tradeoff."
    repaired_line = "Levers trade force."
    director = _FakeClient([
        _draft(original_line),
        {
            "line_replacements": [{
                "line_id": "l1",
                "text": original_line,
            }],
        },
        {
            "line_replacements": [{
                "line_id": "l1",
                "text": repaired_line,
            }],
        },
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            repaired_line,
            "The focused retry fits the supplied delivery budget.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="lever-budget-noop-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [attempt.outcome for attempt in result.director_attempts] == [
        "contract_rejected",
        "contract_rejected",
        "accepted",
    ]
    assert "did not shorten any cited line" in result.contract_errors[-1]
    retry_packet = json.loads(director.calls[2]["input_text"])
    assert retry_packet["invalid_patch_response"] is not None
    assert retry_packet["exact_reduction_targets"][0][
        "minimum_words_to_remove"
    ] == 1


@pytest.mark.asyncio
async def test_delivery_budget_one_word_overflow_survives_two_noop_patches():
    brief = _brief().model_copy(
        update={
            "target_duration_seconds": 4,
            "edit_headroom_seconds": 0,
            "speech_rate_wpm": 60,
        }
    )
    original_line = "A lever changes the tradeoff."
    repaired_line = "Levers change tradeoffs."
    director = _FakeClient([
        _draft(original_line),
        {"line_replacements": [{
            "line_id": "l1",
            "text": original_line,
        }]},
        {"line_replacements": [{
            "line_id": "l1",
            "text": "A lever alters the tradeoff.",
        }]},
        {"line_replacements": [{
            "line_id": "l1",
            "text": repaired_line,
        }]},
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            repaired_line,
            "The bounded exact-fit repair is natural and complete.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="lever-budget-two-noops-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [attempt.outcome for attempt in result.director_attempts] == [
        "contract_rejected",
        "contract_rejected",
        "contract_rejected",
        "accepted",
    ]
    assert len(critic.calls) == 1


@pytest.mark.asyncio
async def test_delivery_budget_partial_patch_becomes_next_retry_base():
    brief = _brief().model_copy(
        update={
            "target_duration_seconds": 4,
            "edit_headroom_seconds": 0,
            "speech_rate_wpm": 60,
        }
    )
    original_line = "A lever changes the force distance tradeoff."
    partial_line = "A lever changes the force tradeoff."
    repaired_line = "Levers trade force."
    director = _FakeClient([
        _draft(original_line),
        {
            "line_replacements": [{
                "line_id": "l1",
                "text": partial_line,
            }],
        },
        {
            "line_replacements": [{
                "line_id": "l1",
                "text": repaired_line,
            }],
        },
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            repaired_line,
            "The accumulated repair now fits the delivery budget.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="lever-budget-partial-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=2,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    second_patch_packet = json.loads(director.calls[2]["input_text"])
    assert second_patch_packet["editable_lines"] == [{
        "line_id": "l1",
        "text": partial_line,
        "delivery_mode": "spoken",
        "current_word_count": 6,
        "maximum_word_count": 4,
    }]
    assert second_patch_packet["exact_reduction_targets"][0][
        "minimum_words_to_remove"
    ] == 2


@pytest.mark.asyncio
async def test_delivery_budget_can_accumulate_two_specialized_repairs():
    brief = _brief().model_copy(
        update={
            "target_duration_seconds": 4,
            "edit_headroom_seconds": 0,
            "speech_rate_wpm": 60,
        }
    )
    director = _FakeClient([
        _draft("A lever changes the force and distance tradeoff."),
        {"line_replacements": [{
            "line_id": "l1",
            "text": "A lever changes force and distance tradeoff.",
        }]},
        {"line_replacements": [{
            "line_id": "l1",
            "text": "A lever changes force tradeoffs.",
        }]},
        {"line_replacements": [{
            "line_id": "l1",
            "text": "Levers trade force.",
        }]},
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            "Levers trade force.",
            "The accumulated specialized repairs fit the final budget.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id="lever-budget-multi-patch-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(result.director_attempts) == 4
    assert result.director_attempts[-1].contract_repair_attempt == 3


@pytest.mark.asyncio
async def test_quality_pause_resume_continues_immutable_revision_ancestry():
    director = _FakeClient([
        _draft("A lever changes the tradeoff"),
        _draft("A lever trades force for distance."),
    ])
    critic = _FakeClient([
        {
            "approved": False,
            "scores": {"clear": 30},
            "criterion_evidence": _clear_evidence(
                "A lever changes the tradeoff",
                "The line still does not explain both sides of the tradeoff.",
            ),
            "blocking_issues": [{
                "code": "INCOMPLETE",
                "line_ids": ["l1"],
                "evidence": "The sentence is incomplete.",
                "repair_instruction": "Explain both sides.",
            }],
            "repair_scope": "director_replan",
        },
        {
            "approved": True,
            "scores": {"clear": 95},
            "criterion_evidence": _clear_evidence(
                "A lever trades force for distance.",
                "The line explains both sides directly.",
            ),
            "blocking_issues": [],
            "repair_scope": "copy_only",
        },
    ])
    prior_artifact = parse_director_author_draft_response(
        json.dumps(_draft("A lever changes the tradeoff")),
        brief=_brief(),
        artifact_id="lever-artifact",
        revision=3,
        parent_artifact_sha256="e" * 64,
    )
    prior_preflight = preflight_script_copy(
        prior_artifact.program,
        prior_artifact.script,
    )
    prior_verdict = IndependentCopyCriticVerdict.model_validate({
        "approved": False,
        "scores": {"clear": 30},
        "criterion_evidence": _clear_evidence(
            "A lever changes the tradeoff",
            "The line still does not explain both sides of the tradeoff.",
        ),
        "blocking_issues": [{
            "code": "INCOMPLETE",
            "line_ids": ["l1"],
            "evidence": "The sentence is incomplete.",
            "repair_instruction": "Explain both sides.",
        }],
        "repair_scope": "director_replan",
    })
    prior_sha = prior_artifact.artifact_sha256

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
        initial_revision=4,
        parent_artifact_sha256=prior_sha,
        resume_artifact=prior_artifact,
        resume_preflight=prior_preflight,
        resume_verdict=prior_verdict,
    )

    assert result.status == "approved"
    assert [artifact.revision for artifact in result.artifacts] == [4, 5]
    assert result.artifacts[0].parent_artifact_sha256 == prior_sha
    assert result.artifacts[1].parent_artifact_sha256 == result.artifacts[0].artifact_sha256
    resume_packet = json.loads(director.calls[0]["input_text"])
    assert resume_packet["resume_context"]["operation"] == "quality_pause_replan"
    assert resume_packet["resume_context"]["revision"] == 4
    assert resume_packet["current_artifact"]["artifact_sha256"] == prior_sha
    assert (
        resume_packet["independent_critic_verdict"]["blocking_issues"][0]["code"]
        == "INCOMPLETE"
    )


@pytest.mark.asyncio
async def test_quality_pause_resume_rejects_blind_ancestry_retry():
    with pytest.raises(ValueError, match="requires the rejected artifact"):
        await run_content_director_copy_loop(
            brief=_brief(),
            artifact_id="lever-artifact",
            policy=DirectorLoopPolicy(
                maximum_revisions=0,
                maximum_contract_repairs_per_revision=0,
            ),
            director_client=_FakeClient([]),
            critic_client=_FakeClient([]),
            initial_revision=4,
            parent_artifact_sha256="f" * 64,
        )


@pytest.mark.asyncio
async def test_quality_pause_resume_retries_missing_critic_without_rewriting_artifact():
    artifact = parse_director_author_draft_response(
        json.dumps(_draft("A lever trades force for distance.")),
        brief=_brief(),
        artifact_id="lever-artifact",
        revision=3,
        parent_artifact_sha256="e" * 64,
    )
    director = _FakeClient([])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            "A lever trades force for distance.",
            "The exact line explains both sides of the tradeoff.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
        initial_revision=4,
        parent_artifact_sha256=artifact.artifact_sha256,
        pending_review_artifact=artifact,
    )

    assert result.status == "approved"
    assert result.final_artifact == artifact
    assert [item.revision for item in result.artifacts] == [3]
    assert len(result.reviews) == 1
    assert director.calls == []
    assert len(critic.calls) == 1


@pytest.mark.asyncio
async def test_copy_loop_quality_pauses_without_requesting_media_or_revision():
    director = _FakeClient([_draft("A lever changes the tradeoff")])
    critic = _FakeClient([{
        "approved": False,
        "scores": {"clear": 20},
        "criterion_evidence": _clear_evidence(
            "A lever changes the tradeoff",
            "The quoted line does not identify either side of the claimed tradeoff.",
        ),
        "blocking_issues": [{
            "code": "INCOMPLETE",
            "line_ids": ["l1"],
            "evidence": "The sentence is incomplete.",
            "repair_instruction": "Finish the explanation.",
        }],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-artifact",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "quality_pause"
    assert len(director.calls) == 1
    assert len(critic.calls) == 1
    assert "no media stage was authorized" in result.reason


@pytest.mark.asyncio
async def test_repeated_copy_failure_escalates_to_director_replan():
    director = _FakeClient([
        _draft("A lever changes the tradeoff"),
        _draft("A lever changes how much force you use"),
        _draft("A lever trades force for distance."),
    ])
    critic = _FakeClient([
        {
            "approved": False,
            "scores": {"clear": 30},
            "criterion_evidence": _clear_evidence(
                "A lever changes the tradeoff",
                "The quote does not name either side of the tradeoff for a beginner.",
            ),
            "blocking_issues": [{
                "code": "INCOMPLETE",
                "line_ids": ["l1"],
                "evidence": "The sentence does not identify the tradeoff.",
                "repair_instruction": "Explain both sides of the tradeoff.",
            }],
            "repair_scope": "copy_only",
        },
        {
            "approved": False,
            "scores": {"clear": 50},
            "criterion_evidence": _clear_evidence(
                "A lever changes how much force you use",
                "The quote mentions force but still omits the distance side of the tradeoff.",
            ),
            "blocking_issues": [{
                "code": "STILL_INCOMPLETE",
                "line_ids": ["l1"],
                "evidence": "The revised sentence still omits distance.",
                "repair_instruction": "Replan the explanation around both sides.",
            }],
            "repair_scope": "copy_only",
        },
        {
            "approved": True,
            "scores": {"clear": 95},
            "criterion_evidence": _clear_evidence(
                "A lever trades force for distance.",
                "The quote names both sides of the tradeoff directly for a beginner.",
            ),
            "blocking_issues": [],
            "repair_scope": "copy_only",
        },
    ])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-replan",
        policy=DirectorLoopPolicy(
            maximum_revisions=2,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    second_revision_packet = json.loads(director.calls[2]["input_text"])
    contract = second_revision_packet["revision_contract"]
    assert contract["critic_requested_repair_scope"] == "copy_only"
    assert contract["repair_scope"] == "director_replan"
    assert "remained below threshold" in (
        contract["runtime_repair_scope_override_reason"]
    )


@pytest.mark.asyncio
async def test_invalid_director_contract_is_repaired_without_media():
    invalid = _draft("A lever trades force for distance.")
    invalid["script"]["segments"][0]["segment_index"] = 2
    director = _FakeClient([
        invalid,
        _draft("A lever trades force for distance."),
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"clear": 95},
        "criterion_evidence": _clear_evidence(
            "A lever trades force for distance.",
            "The quoted line states the force-distance tradeoff directly and clearly.",
        ),
        "blocking_issues": [],
        "repair_scope": "copy_only",
    }])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-contract-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(result.contract_errors) == 1
    assert "segment indices" in result.contract_errors[0]
    assert [attempt.operation for attempt in result.director_attempts] == [
        "initial",
        "contract_repair",
    ]
    assert [attempt.outcome for attempt in result.director_attempts] == [
        "contract_rejected",
        "accepted",
    ]
    assert len(director.calls) == 2
    repair_packet = json.loads(director.calls[1]["input_text"])
    assert "validation_error" in repair_packet
    assert (
        repair_packet["repair_rules"]
        ["return_only_author_owned_fields"]
        is True
    )
    assert "immutable_program_fields" not in repair_packet
    assert "immutable_script_fields" not in repair_packet
    assert "conversion" not in json.dumps(
        repair_packet["output_contract"],
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_exhausted_contract_repairs_quality_pause_before_critic():
    invalid = _draft("A lever trades force for distance.")
    invalid["script"]["segments"][0]["segment_index"] = 2
    director = _FakeClient([invalid])
    critic = _FakeClient([])

    result = await run_content_director_copy_loop(
        brief=_brief(),
        artifact_id="lever-contract-pause",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=0,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "quality_pause"
    assert result.final_artifact is None
    assert len(result.contract_errors) == 1
    assert result.director_attempts[0].outcome == "contract_rejected"
    assert result.director_attempts[0].validation_error
    assert len(critic.calls) == 0
    assert "no media stage was authorized" in result.reason
