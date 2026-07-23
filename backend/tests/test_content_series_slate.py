from __future__ import annotations

import copy
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.hermes_agent.content_director import (
    apply_series_coverage_patch,
    ConversionIntent,
    CopyReviewCriterion,
    DirectorCapabilitySpec,
    DirectorSeriesBrief,
    SeriesContentFamily,
    SeriesCoverageMap,
    SeriesCoverageMapDraft,
    SeriesCoveragePatchDraft,
    SeriesCoveragePage,
    SeriesCoverageTerritory,
    SeriesDiversityRequirement,
    SeriesSlateDraft,
    build_series_coverage_packet,
    build_series_slate_packet,
    build_series_slate_page_packet,
    finalize_series_slate,
    finalize_series_coverage_map,
    materialize_series_director_briefs,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
)
from app.services.hermes_agent.content_series_runtime import (
    _parse_series_critic,
    IndependentSeriesCriticVerdict,
    SeriesCoverageCriticReview,
    SeriesPageCriticReview,
    SeriesSlateLoopResult,
    run_content_series_slate_loop,
)
from app.services.hermes_agent.content_series_store import (
    persist_approved_series_slate,
)
from app.services.hermes_agent.content_capabilities import (
    load_content_capability_manifest,
)
from app.services.hermes_agent.content_director_profile import (
    compile_universal_director_series_brief,
)
from app.services.hermes_agent.content_factory import (
    _stage_api_route,
    create_project,
    queue_stage,
)
from app.tasks.hermes_agent import content_factory_tasks
from app.tasks.hermes_agent.content_factory_tasks import (
    _configured_next_stage,
    _refresh_profile_director_brief_from_facts,
    _run_content_series_director_stage,
)
from app.data.models.hermes_agent import (
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from scripts.run_content_copy_shadow import (
    _variant_brief as _shadow_variant_brief,
)


class _FakeClient:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def create_response(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output_text": json.dumps(self.outputs.pop(0)),
            "_gmv_meta": {
                "model": "test-model",
                "request_id": f"request-{len(self.calls)}",
            },
        }, 8


def _brief() -> DirectorSeriesBrief:
    return DirectorSeriesBrief(
        series_id="generic-series",
        series_version=1,
        objective="Create three distinct short explanations.",
        platform="TikTok",
        locale="en-US",
        audience="US adults who are new to the supplied topic.",
        target_count=3,
        minimum_duration_seconds=20,
        maximum_duration_seconds=40,
        default_duration_seconds=30,
        edit_headroom_seconds=2,
        speech_rate_wpm=155,
        aspect_ratio="9:16",
        conversion=ConversionIntent(product_required=False),
        truth_payload={
            "facts": [
                "A lever changes the force-distance tradeoff.",
            ],
        },
        capability_catalog=[
            DirectorCapabilitySpec(
                capability="truth.normalize",
                input_contract="ProjectInputs",
                output_contract="TruthPacket",
            ),
            DirectorCapabilitySpec(
                capability="series.slate",
                input_contract="DirectorSeriesBrief",
                output_contract="SeriesSlate",
            ),
            DirectorCapabilitySpec(
                capability="copy.write",
                input_contract="VideoProgramSpec",
                output_contract="ScriptPackage",
            ),
        ],
        copy_review_criteria=[
            CopyReviewCriterion(
                criterion_id="clear",
                instruction="A beginner understands the supplied fact.",
                minimum_score=80,
            ),
        ],
        quality_rubric=["Each intent teaches only the supplied fact."],
        source_truth_refs=["truth:lever"],
        diversity_requirements=[
            SeriesDiversityRequirement(
                dimension_id="explanation_angle",
                instruction="Use a materially distinct explanatory angle.",
                minimum_unique_values=3,
            ),
            SeriesDiversityRequirement(
                dimension_id="visual_grammar",
                instruction="Use a materially distinct visual grammar.",
                minimum_unique_values=2,
            ),
        ],
    )


def test_universal_series_packet_does_not_force_pain_structure():
    packet = build_series_slate_packet(_brief())
    definitions = packet["output_contract"]["$defs"]
    intent = definitions["SeriesSlateIntent"]

    assert "pain_hypothesis" not in intent["properties"]
    assert "pain_hypothesis" not in intent["required"]
    assert "PainHypothesis" not in definitions
    assert packet["director_rules"][
        "describe_the_viewer_need_without_forcing_pain"
    ] is True


def _draft() -> dict:
    return {
        "schema_version": "2.0",
        "series_id": "generic-series",
        "series_version": 1,
        "intents": [
            {
                "variant_index": 1,
                "intent_id": "intent-one",
                "objective": "Explain with a playground seesaw.",
                "content_type": "animated analogy",
                "audio_mode": "spoken",
                "audience": "US adult beginners.",
                "target_duration_seconds": 30,
                "creative_strategy": {"analogy": "seesaw"},
                "differentiation": {
                    "explanation_angle": "playground balance",
                    "visual_grammar": "single continuous demonstration",
                },
                "creative_constraints": [],
                "source_truth_refs": ["truth:lever"],
            },
            {
                "variant_index": 2,
                "intent_id": "intent-two",
                "objective": "Explain with a toolbox demonstration.",
                "content_type": "hands-on explainer",
                "audio_mode": "sound_design",
                "audience": "US adult beginners.",
                "target_duration_seconds": 30,
                "creative_strategy": {"demonstration": "toolbox"},
                "differentiation": {
                    "explanation_angle": "tool effort",
                    "visual_grammar": "labeled split screen",
                },
                "creative_constraints": [],
                "source_truth_refs": ["truth:lever"],
            },
            {
                "variant_index": 3,
                "intent_id": "intent-three",
                "objective": "Explain with a distance tradeoff diagram.",
                "content_type": "motion graphic",
                "audio_mode": "music_only",
                "audience": "US adult beginners.",
                "target_duration_seconds": 30,
                "creative_strategy": {"diagram": "distance arcs"},
                "differentiation": {
                    "explanation_angle": "distance exchanged for force",
                    "visual_grammar": "diagram transformation",
                },
                "creative_constraints": [],
                "source_truth_refs": ["truth:lever"],
            },
        ],
    }


def _paged_brief(*, target_count: int) -> DirectorSeriesBrief:
    payload = _brief().model_dump(mode="json")
    payload["target_count"] = target_count
    payload["objective"] = "Create a large, paged explanation series."
    return DirectorSeriesBrief.model_validate(payload)


def _page_draft(
    *,
    start_variant_index: int,
    end_variant_index: int,
    page_index: int,
) -> dict:
    intents = []
    for variant_index in range(
        start_variant_index,
        end_variant_index + 1,
    ):
        intents.append({
            "variant_index": variant_index,
            "intent_id": f"intent-{variant_index:03d}",
            "objective": (
                f"Explain the lever tradeoff with example "
                f"{variant_index}."
            ),
            "content_type": (
                "animated analogy"
                if variant_index % 2
                else "hands-on explainer"
            ),
            "audio_mode": (
                "spoken" if variant_index % 2 else "sound_design"
            ),
            "audience": "US adult beginners.",
            "target_duration_seconds": 30,
            "creative_strategy": {
                "example_index": variant_index,
            },
            "differentiation": {
                "explanation_angle": (
                    f"angle-{((variant_index - 1) % 3) + 1}"
                ),
                "visual_grammar": (
                    "single continuous demonstration"
                    if variant_index % 2
                    else "labeled split screen"
                ),
            },
            "creative_constraints": [],
            "source_truth_refs": ["truth:lever"],
        })
    return {
        "schema_version": "2.0",
        "series_id": "generic-series",
        "series_version": 1,
        "page_index": page_index,
        "start_variant_index": start_variant_index,
        "end_variant_index": end_variant_index,
        "intents": intents,
    }


def _structured_brief(*, target_count: int = 3) -> DirectorSeriesBrief:
    return compile_universal_director_series_brief(
        series_id="structured-series",
        objective="Create distinct, concrete educational videos.",
        platform="TikTok",
        locale="en-US",
        audience="US adults new to the supplied subject.",
        target_count=target_count,
        minimum_duration_seconds=30,
        maximum_duration_seconds=30,
        product_required=False,
        brand_name=None,
        product_name=None,
        market="US",
        project_brief=None,
        structured_intent_contract_required=True,
    )


def _structured_page(
    *,
    start: int,
    end: int,
    page_index: int,
) -> dict:
    intents = []
    for variant in range(start, end + 1):
        intents.append({
            "variant_index": variant,
            "intent_id": f"structured-{variant}",
            "objective": f"Explain a concrete idea {variant}.",
            "content_type": f"format-{variant}",
            "audio_mode": (
                "spoken" if variant % 2 else "sound_design"
            ),
            "audience": "US adult beginners.",
            "target_duration_seconds": 30,
            "creative_strategy": {"approach": f"approach-{variant}"},
            "differentiation": {
                "content_type": f"type-{variant}",
                "opening_structure": f"opening-{variant}",
                "visual_grammar": f"visual-{variant}",
                "audio_strategy": (
                    "spoken" if variant % 2 else "sound-design"
                ),
            },
            "creative_constraints": [],
            "source_truth_refs": [],
            "pain_hypothesis": {
                "concrete_moment": f"Moment {variant} happens.",
                "felt_loss_or_conflict": (
                    f"The viewer loses a concrete opportunity {variant}."
                ),
                "audience_recognition": (
                    f"Beginners recognize situation {variant}."
                ),
                "claims_boundary": "Do not invent downstream outcomes.",
            },
            "conversion_hypothesis": None,
        })
    return {
        "schema_version": "2.0",
        "series_id": "structured-series",
        "series_version": 1,
        "page_index": page_index,
        "start_variant_index": start,
        "end_variant_index": end,
        "intents": intents,
    }


def _coverage_draft() -> dict:
    def territory(
        variant_index: int,
        territory_id: str,
        *,
        family_id: str,
        strategic_role: str,
        audience_state: str,
        audience_tension_or_need: str,
        viewer_value_context: str,
        response_or_action_route: str,
        anti_repetition_rule: str,
    ) -> dict:
        return {
            "variant_index": variant_index,
            "family_id": family_id,
            "territory_id": territory_id,
            "strategic_role": strategic_role,
            "audience_state": audience_state,
            "audience_tension_or_need": audience_tension_or_need,
            "viewer_value_context": viewer_value_context,
            "response_or_action_route": response_or_action_route,
            "truth_options": [],
            "anti_repetition_rule": anti_repetition_rule,
        }

    return {
        "schema_version": "1.0",
        "series_id": "structured-series",
        "series_version": 1,
        "page_size": 2,
        "families": [
            {
                "family_id": "recognition-learning",
                "strategic_job": (
                    "Help beginners recognize two different learning "
                    "failure modes."
                ),
                "audience_stage": "Problem-aware beginners.",
                "content_type_space": (
                    "Narrative explanation or practical reconstruction."
                ),
                "viewer_value_role": (
                    "Educational resolution without a product."
                ),
                "planned_variant_count": 2,
                "truth_options": [],
                "permitted_reuse": (
                    "The educational job may repeat; the human conflict and "
                    "corrective reasoning may not."
                ),
                "differentiation_mandate": (
                    "Use different failure mechanisms and evidence."
                ),
            },
            {
                "family_id": "bounded-action",
                "strategic_job": (
                    "Turn understanding into one practical choice."
                ),
                "audience_stage": "Solution-aware beginners.",
                "content_type_space": (
                    "Demonstration, comparison, or another suitable form."
                ),
                "viewer_value_role": (
                    "Resolve with a bounded non-product action."
                ),
                "planned_variant_count": 1,
                "truth_options": [],
                "permitted_reuse": (
                    "No reuse is needed for this one-episode family."
                ),
                "differentiation_mandate": (
                    "Do not restage the recognition family."
                ),
            },
        ],
        "pages": [
            {
                "page_index": 1,
                "start_variant_index": 1,
                "end_variant_index": 2,
                "territories": [
                    territory(
                        1,
                        "territory-a1",
                        family_id="recognition-learning",
                        strategic_role=(
                            "Teach through a concrete conflict."
                        ),
                        audience_state=(
                            "Beginners facing the first decision."
                        ),
                        audience_tension_or_need="A missed ordinary opportunity.",
                        viewer_value_context=(
                            "No product is required for this educational "
                            "series."
                        ),
                        response_or_action_route="Resolve with understanding.",
                        anti_repetition_rule=(
                            "Do not rename the same missed opportunity."
                        ),
                    ),
                    territory(
                        2,
                        "territory-a2",
                        family_id="recognition-learning",
                        strategic_role=(
                            "Teach through a distinct practical mistake."
                        ),
                        audience_state=(
                            "Beginners applying a new concept."
                        ),
                        audience_tension_or_need="A concrete avoidable action error.",
                        viewer_value_context=(
                            "No product is required for this educational "
                            "series."
                        ),
                        response_or_action_route="Resolve with a bounded correction.",
                        anti_repetition_rule=(
                            "Do not reuse the missed-opportunity logic."
                        ),
                    ),
                ],
                "page_uniqueness_mandate": (
                    "Use two materially different moments."
                ),
            },
            {
                "page_index": 2,
                "start_variant_index": 3,
                "end_variant_index": 3,
                "territories": [
                    territory(
                        3,
                        "territory-b",
                        family_id="bounded-action",
                        strategic_role=(
                            "Teach through a practical choice."
                        ),
                        audience_state=(
                            "Beginners comparing two actions."
                        ),
                        audience_tension_or_need="A preventable choice error.",
                        viewer_value_context=(
                            "No product is required for this educational "
                            "series."
                        ),
                        response_or_action_route="Resolve with a bounded action.",
                        anti_repetition_rule=(
                            "Do not reuse the earlier decision logic."
                        ),
                    )
                ],
                "page_uniqueness_mandate": (
                    "Use a distinct decision structure."
                ),
            },
        ],
    }


def _coverage_patch_draft(
    brief: DirectorSeriesBrief,
    *,
    base: SeriesCoverageMap | None = None,
    territory_id: str = "territory-a1-repaired",
    audience_tension_or_need: str = "A distinct concrete loss after repair.",
    response_or_action_route: str = "Resolve with a distinct bounded action.",
) -> dict:
    coverage = base or finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    territory = coverage.pages[0].territories[0].model_dump(
        mode="json"
    )
    territory.update({
        "territory_id": territory_id,
        "audience_tension_or_need": audience_tension_or_need,
        "response_or_action_route": response_or_action_route,
    })
    return {
        "schema_version": "1.0",
        "series_id": coverage.series_id,
        "series_version": coverage.series_version,
        "base_coverage_sha256": coverage.coverage_sha256,
        "family_updates": [],
        "territory_updates": [territory],
    }


def _approved_series_verdict(criteria) -> dict:
    return {
        "approved": True,
        "scores": {
            item.criterion_id: 100
            for item in criteria
        },
        "blocking_issues": [],
        "repair_scope": "slate_only",
    }


def _rejected_series_verdict(criteria, *, intent_ids: list[str]) -> dict:
    scores = {
        item.criterion_id: 100
        for item in criteria
    }
    first = criteria[0]
    scores[first.criterion_id] = max(0, first.minimum_score - 1)
    return {
        "approved": False,
        "scores": scores,
        "blocking_issues": [{
            "code": "LOCAL_INTENT_REPAIR",
            "intent_ids": intent_ids,
            "evidence": "Only the cited intent needs a more concrete plan.",
            "repair_instruction": (
                "Repair the cited intent without changing the other page."
            ),
        }],
        "repair_scope": "slate_only",
    }


def test_rejected_series_critic_must_cite_supplied_intent_ids():
    brief = _structured_brief()
    verdict = _rejected_series_verdict(
        brief.series_page_review_criteria,
        intent_ids=[],
    )

    with pytest.raises(ValueError, match="must cite at least one"):
        _parse_series_critic(
            json.dumps(verdict),
            brief=brief,
            criteria=brief.series_page_review_criteria,
            valid_intent_ids={"structured-1", "structured-2"},
        )


def test_series_slate_contract_is_project_owned_and_materializes_briefs():
    brief = _brief()
    slate = finalize_series_slate(
        SeriesSlateDraft.model_validate(_draft()),
        brief,
    )
    briefs = materialize_series_director_briefs(brief, slate)

    assert list(briefs) == ["1", "2", "3"]
    assert briefs["2"]["content_type_hint"] == "hands-on explainer"
    assert briefs["2"]["audio_mode_hint"] == "sound_design"
    assert (
        briefs["3"]["truth_payload"]["series_intent"]["differentiation"]
        ["explanation_angle"]
        == "distance exchanged for force"
    )
    assert briefs["1"]["truth_payload"]["series_objective"] == brief.objective
    assert briefs["1"]["truth_payload"]["series_audience"] == brief.audience
    assert briefs["1"]["conversion"]["product_required"] is False


def test_series_audio_delivery_policy_is_project_owned_and_fail_closed():
    brief = _brief().model_copy(
        update={"allowed_audio_modes": ["spoken"]}
    )
    packet = build_series_slate_packet(brief)
    audio_schema = (
        packet["output_contract"]["$defs"]["SeriesSlateIntent"]
        ["properties"]["audio_mode"]
    )
    assert audio_schema["const"] == "spoken"
    assert "enum" not in audio_schema

    payload = copy.deepcopy(_draft())
    with pytest.raises(ValueError, match="outside allowed_audio_modes"):
        finalize_series_slate(
            SeriesSlateDraft.model_validate(payload),
            brief,
        )


def test_paged_series_packets_preserve_operator_repair_instruction():
    instruction = (
        "Replace adjacency with a demonstrated category use case."
    )
    brief = _brief().model_copy(
        update={
            "truth_payload": {
                **_brief().truth_payload,
                "operator_stage_instruction": instruction,
            }
        }
    )
    coverage_packet = build_series_coverage_packet(
        brief,
        page_size=3,
    )
    page_packet = build_series_slate_page_packet(
        brief,
        page_index=1,
        total_pages=1,
        start_variant_index=1,
        end_variant_index=3,
        accepted_prior_intents=[],
    )

    assert coverage_packet["series_strategy_contract"][
        "operator_stage_instruction"
    ] == instruction
    assert page_packet["series_page_strategy_contract"][
        "operator_stage_instruction"
    ] == instruction


def test_legacy_series_slate_hash_ignores_only_absent_audio_authority():
    payload = copy.deepcopy(_draft())
    payload["schema_version"] = "1.0"
    for intent in payload["intents"]:
        intent.pop("audio_mode")
    draft = SeriesSlateDraft.model_validate(payload)
    slate = finalize_series_slate(draft, _brief())

    signed_intents = []
    for intent in draft.intents:
        row = intent.model_dump(mode="json")
        row.pop("audio_mode", None)
        signed_intents.append(row)
    canonical = json.dumps(
        {
            "series_id": draft.series_id,
            "series_version": draft.series_version,
            "intents": signed_intents,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert slate.schema_version == "1.0"
    assert slate.slate_sha256 == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def test_v2_series_slate_requires_structured_audio_authority():
    payload = copy.deepcopy(_draft())
    payload["intents"][1].pop("audio_mode")
    with pytest.raises(ValidationError, match="requires audio_mode"):
        SeriesSlateDraft.model_validate(payload)


def test_copy_shadow_preserves_structured_pain_and_conversion_hypotheses():
    series_brief = _structured_brief()
    page = _structured_page(
        start=1,
        end=2,
        page_index=1,
    )
    page["intents"][0]["conversion_hypothesis"] = {
        "viewer_decision_or_use_case": "Choose one bounded next action.",
        "product_relevance_bridge": (
            "The supplied option belongs in the same decision."
        ),
        "confirmed_attribute": "A confirmed attribute.",
        "reason_to_choose_or_consider": (
            "The confirmed attribute matches the stated decision."
        ),
        "bounded_human_change": "The viewer may choose one small action.",
        "prohibited_outcome_boundary": "Do not promise an outcome.",
        "semantic_route_fingerprint": "decision>attribute>bounded-action",
    }
    intent = SeriesSlateDraft.model_validate({
        "series_id": series_brief.series_id,
        "series_version": series_brief.series_version,
        "intents": page["intents"],
    }).intents[0]

    variant_brief = _shadow_variant_brief(
        series_brief=series_brief,
        intent=intent,
    )
    truth = variant_brief.truth_payload["series_intent"]

    assert truth["pain_hypothesis"] == (
        intent.pain_hypothesis.model_dump(mode="json")
    )
    assert truth["conversion_hypothesis"] == (
        intent.conversion_hypothesis.model_dump(mode="json")
    )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda payload: payload["intents"].pop(),
            "exactly target_count",
        ),
        (
            lambda payload: payload["intents"][2]["differentiation"].update(
                {"explanation_angle": "tool effort"}
            ),
            "explanation_angle has 2 unique values",
        ),
        (
            lambda payload: payload["intents"][0]["differentiation"].pop(
                "visual_grammar"
            ),
            "keys must exactly match",
        ),
    ],
)
def test_series_slate_rejects_incomplete_or_template_like_plans(
    mutator,
    message,
):
    payload = copy.deepcopy(_draft())
    mutator(payload)
    with pytest.raises(ValueError, match=message):
        finalize_series_slate(
            SeriesSlateDraft.model_validate(payload),
            _brief(),
        )


def test_series_brief_rejects_impossible_diversity_contract():
    payload = _brief().model_dump(mode="json")
    payload["diversity_requirements"][0]["minimum_unique_values"] = 4
    with pytest.raises(ValidationError, match="cannot exceed target_count"):
        DirectorSeriesBrief.model_validate(payload)


def test_enforced_series_project_starts_at_non_media_planning_stage(
    db_session,
):
    brief = _brief().model_copy(
        update={
            "capability_catalog": (
                load_content_capability_manifest().capabilities
            )
        }
    )
    project = create_project(
        db_session,
        workspace_id=1201,
        user_id=3401,
        title="Generic series",
        content_mode="educational-explainer",
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=40,
        video_language="en-US",
        content_director_mode="enforce",
        director_series_brief=brief.model_dump(mode="json"),
        director_loop_policy={
            "maximum_revisions": 1,
            "maximum_contract_repairs_per_revision": 1,
        },
        auto_run=False,
    )

    assert project.current_stage == "SERIES_DIRECTOR"
    assert project.config_json["content_mode"] == "educational-explainer"
    assert project.config_json["product_required"] is False
    assert project.config_json["director_briefs_by_variant"] == {}
    assert project.config_json["max_api_video_variants_in_flight"] == 2
    assert (
        project.state_json["video_variant_pipeline"]["mode"]
        == "bounded_api_parallel_v1"
    )
    assert _configured_next_stage(project, "FACTS") == "SERIES_DIRECTOR"
    assert (
        _stage_api_route(db_session, "SERIES_DIRECTOR")
        == "hermes:content-director"
    )


def test_creative_review_route_uses_configured_multimodal_group(
    db_session,
    monkeypatch,
):
    monkeypatch.setenv(
        "HERMES_CREATIVE_REVIEW_MODEL",
        "gmv-content-visual-inspector-v1",
    )

    assert _stage_api_route(db_session, "CREATIVE_REVIEW") == (
        "ai-routing:gmv-content-visual-inspector-v1"
    )


def test_enforced_project_compiles_scene_free_series_brief_from_normal_inputs(
    db_session,
):
    project = create_project(
        db_session,
        workspace_id=1202,
        user_id=3402,
        title="Night routine product videos",
        content_objective="Create distinct TikTok conversion videos.",
        target_audience="US adults considering a nighttime routine.",
        content_mode="product",
        brand_name="MYUPONA",
        product_name="Sleep Ease Gummies",
        market="US",
        product_brief="Use only the supplied nighttime-routine positioning.",
        confirmed_claims="Melatonin-free",
        confirmed_selling_points="GABA\nL-Theanine\nMagnesium Glycinate",
        confirmed_promotions="$7.99",
        promotion_cta="Tap the yellow cart.",
        video_count=5,
        video_duration_min_seconds=40,
        video_duration_max_seconds=40,
        video_language="en-US",
        publishing_profile={"platform": "TikTok"},
        content_director_mode="enforce",
        auto_run=False,
    )

    raw = project.config_json["director_series_brief"]
    brief = DirectorSeriesBrief.model_validate(raw)

    assert project.current_stage == "FACTS"
    assert (
        project.config_json["director_loop_policy"]
        ["maximum_series_revisions"]
            == 5
    )
    assert (
        project.config_json["director_series_brief_source"]
        == "universal_profile"
    )
    assert _configured_next_stage(project, "FACTS") == "SERIES_DIRECTOR"
    assert brief.target_count == 5
    assert brief.conversion.reveal_after_fraction is None
    assert brief.conversion.offer_text == "$7.99"
    assert brief.conversion.cta_text == "Tap the yellow cart."
    assert brief.conversion.minimum_differentiators_in_copy == 1
    assert brief.production_contract is not None
    assert brief.production_contract.model_id == "omni_flash"
    assert brief.structured_intent_contract_required is False
    assert {
        item.criterion_id for item in brief.series_page_review_criteria
    } >= {
        "intent_objective_audience_fit",
        "intent_truth_boundary",
        "requested_stakes_calibration",
        "product_conversion_feasibility",
        "bounded_product_change_feasibility",
    }
    assert {
        item.criterion_id
        for item in brief.series_global_review_criteria
    } >= {
        "semantic_intent_distinctness",
        "response_or_action_route_diversity",
    }
    assert {
        "requested_stakes_strength",
        "product_relevance_bridge",
        "reason_to_choose",
    } <= {
        item.criterion_id for item in brief.copy_review_criteria
    }
    assert brief.truth_payload["profile_id"] == "universal-short-video-v4"
    encoded = json.dumps(raw, ensure_ascii=False).lower()
    assert "mother template" in encoded
    assert "cannot by itself establish why the product category" in encoded
    assert "bedroom doorway" not in encoded
    assert "four segments" not in encoded

    _refresh_profile_director_brief_from_facts(
        project,
        product_truth={
            "source": "FACTS",
            "approved_claims": ["Melatonin-free"],
            "prohibited_claims": ["Guaranteed sleep"],
        },
    )
    refreshed = DirectorSeriesBrief.model_validate(
        project.config_json["director_series_brief"]
    )
    assert (
        refreshed.truth_payload["product_truth"]["source"]
        == "FACTS"
    )
    refreshed_text = json.dumps(
        refreshed.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert (
        "Use only the supplied nighttime-routine positioning."
        not in refreshed_text
    )


def test_universal_profile_scales_diversity_without_scene_inventory():
    brief = compile_universal_director_series_brief(
        series_id="generic-50",
        objective="Create a broad educational series.",
        platform="short-video",
        locale="en-US",
        audience="Adults new to the supplied subject.",
        target_count=50,
        minimum_duration_seconds=20,
        maximum_duration_seconds=60,
        product_required=False,
        brand_name=None,
        product_name=None,
        market="US",
        project_brief="Teach only the supplied source material.",
        additional_creative_constraints=[
            "Use concrete examples instead of generic abstractions."
        ],
        additional_copy_review_criteria=[{
            "criterion_id": "example_specificity",
            "instruction": "Every explanation uses one concrete example.",
            "minimum_score": 90,
            "blocking": True,
        }],
        additional_series_page_review_criteria=[{
            "criterion_id": "source_teachability",
            "instruction": "Each intent can teach one supplied source idea.",
            "minimum_score": 90,
            "blocking": True,
        }],
        additional_series_global_review_criteria=[{
            "criterion_id": "curriculum_progression",
            "instruction": "The series progresses without hidden prerequisites.",
            "minimum_score": 88,
            "blocking": True,
        }],
    )

    requirements = {
        item.dimension_id: item.minimum_unique_values
        for item in brief.diversity_requirements
    }
    assert requirements == {
        "content_type": 10,
        "opening_structure": 10,
        "visual_grammar": 8,
        "audio_strategy": 2,
    }
    assert brief.conversion.product_required is False
    assert "example_specificity" in {
        item.criterion_id for item in brief.copy_review_criteria
    }
    assert "source_teachability" in {
        item.criterion_id for item in brief.series_page_review_criteria
    }
    assert "curriculum_progression" in {
        item.criterion_id
        for item in brief.series_global_review_criteria
    }
    assert (
        "Use concrete examples instead of generic abstractions."
        in brief.creative_constraints
    )


def test_universal_profile_deduplicates_confirmed_attributes_case_insensitively():
    brief = compile_universal_director_series_brief(
        series_id="casefold-truth",
        objective="Create product explainers.",
        platform="short-video",
        locale="en-US",
        audience="US adults.",
        target_count=2,
        minimum_duration_seconds=20,
        maximum_duration_seconds=20,
        product_required=True,
        brand_name="Example",
        product_name="Example Product",
        market="US",
        project_brief=None,
        confirmed_claims=["Sugar free", "BLUEBERRY FLAVOR"],
        confirmed_selling_points=["sugar free", "Blueberry flavor"],
    )

    assert brief.conversion.confirmed_differentiators == [
        "sugar free",
        "Blueberry flavor",
    ]


def test_universal_profile_keeps_product_alias_out_of_differentiators():
    brief = compile_universal_director_series_brief(
        series_id="identity-alias",
        objective="Create product explainers.",
        platform="short-video",
        locale="en-US",
        audience="US adults.",
        target_count=1,
        minimum_duration_seconds=20,
        maximum_duration_seconds=20,
        product_required=True,
        brand_name="MYUPONA",
        product_name="MYUPONA SLEEP EASY GUMMIES",
        market="US",
        project_brief=None,
        confirmed_claims=[
            "MYUPONA Sleep Ease Gummies",
            "Melatonin-free",
        ],
    )

    assert brief.conversion.product_name_aliases == [
        "MYUPONA Sleep Ease Gummies"
    ]
    assert brief.conversion.confirmed_differentiators == [
        "Melatonin-free"
    ]


def test_coverage_map_reserves_model_owned_parallel_page_territories():
    brief = compile_universal_director_series_brief(
        series_id="coverage-2",
        objective="Create distinct product videos.",
        platform="TikTok",
        locale="en-US",
        audience="US adult beginners.",
        target_count=2,
        minimum_duration_seconds=40,
        maximum_duration_seconds=40,
        product_required=True,
        brand_name="MYUPONA",
        product_name="Sleep Ease Gummies",
        market="US",
        project_brief=None,
        confirmed_claims=["Melatonin-free"],
        confirmed_selling_points=["Sugar free"],
    )
    families = [
        SeriesContentFamily(
            family_id="routine-fit",
            strategic_job="Demonstrate one routine-fit decision.",
            audience_stage="Use-case aware adults.",
            content_type_space="Demonstration or narrative use case.",
            viewer_value_role="Use a confirmed format preference.",
            planned_variant_count=1,
            truth_options=["Sugar free"],
            permitted_reuse="No reuse in this one-video family.",
            differentiation_mandate=(
                "Keep the decision grounded in routine fit."
            ),
        ),
        SeriesContentFamily(
            family_id="attribute-comparison",
            strategic_job="Support one attribute comparison.",
            audience_stage="Product-aware adults.",
            content_type_space="Comparison or explanation.",
            viewer_value_role="Use a confirmed attribute criterion.",
            planned_variant_count=1,
            truth_options=["Melatonin-free"],
            permitted_reuse="No reuse in this one-video family.",
            differentiation_mandate=(
                "Do not restage the routine-fit decision."
            ),
        ),
    ]
    pages = [
        SeriesCoveragePage(
            page_index=1,
            start_variant_index=1,
            end_variant_index=1,
            territories=[
                SeriesCoverageTerritory(
                    variant_index=1,
                    family_id="routine-fit",
                    territory_id="routine-friction",
                    strategic_role="Expose a concrete routine barrier.",
                    audience_state="Adults whose evening plan keeps breaking.",
                    audience_tension_or_need="A planned pause is lost to one more task.",
                    viewer_value_context=(
                        "The person is already choosing one simple bedtime "
                        "routine format."
                    ),
                    response_or_action_route=(
                        "A simple gummy format matches the stated need for "
                        "low-friction repetition."
                    ),
                    truth_options=["Sugar free"],
                    anti_repetition_rule=(
                        "Do not reuse a task-closing story as a renamed prop."
                    ),
                )
            ],
            page_uniqueness_mandate=(
                "Use distinct human conflicts and decision logic."
            ),
        ),
        SeriesCoveragePage(
            page_index=2,
            start_variant_index=2,
            end_variant_index=2,
            territories=[
                SeriesCoverageTerritory(
                    variant_index=2,
                    family_id="attribute-comparison",
                    territory_id="ingredient-preference",
                    strategic_role="Make product selection criteria explicit.",
                    audience_state="Adults comparing nighttime routine options.",
                    audience_tension_or_need="Too many options turn one choice into homework.",
                    viewer_value_context=(
                        "The person wants a melatonin-free option."
                    ),
                    response_or_action_route=(
                        "An explicit melatonin-free preference narrows the "
                        "choice to the confirmed product attribute."
                    ),
                    truth_options=["Melatonin-free"],
                    anti_repetition_rule=(
                        "Do not repeat the low-friction routine route."
                    ),
                )
            ],
            page_uniqueness_mandate=(
                "Use selection logic rather than a boundary-setting story."
            ),
        ),
    ]
    coverage = finalize_series_coverage_map(
        SeriesCoverageMapDraft(
            series_id=brief.series_id,
            series_version=brief.series_version,
            page_size=1,
            families=families,
            pages=pages,
        ),
        brief,
        page_size=1,
    )

    assert len(coverage.pages) == 2
    assert coverage.pages[1].start_variant_index == 2
    assert len(coverage.coverage_sha256) == 64

    invalid = coverage.model_dump(mode="json")
    invalid.pop("coverage_sha256")
    invalid["pages"][1]["territories"][0][
        "truth_options"
    ] = ["Invented claim"]
    with pytest.raises(ValueError, match="invented"):
        finalize_series_coverage_map(
            SeriesCoverageMapDraft.model_validate(invalid),
            brief,
            page_size=1,
        )
    missing_territory = coverage.model_dump(mode="json")
    missing_territory.pop("coverage_sha256")
    missing_territory["pages"][1]["territories"] = []
    with pytest.raises(ValidationError):
        SeriesCoverageMapDraft.model_validate(missing_territory)


def test_coverage_families_allow_deliberate_product_reason_reuse():
    brief = compile_universal_director_series_brief(
        series_id="family-reuse-2",
        objective="Create two product videos with one truthful offer route.",
        platform="TikTok",
        locale="en-US",
        audience="US adults.",
        target_count=2,
        minimum_duration_seconds=30,
        maximum_duration_seconds=40,
        product_required=True,
        brand_name="Example",
        product_name="Example Product",
        market="US",
        project_brief=None,
        confirmed_selling_points=[
            "Confirmed format",
            "Secondary fact",
        ],
    )
    draft = SeriesCoverageMapDraft.model_validate({
        "schema_version": "1.0",
        "series_id": brief.series_id,
        "series_version": brief.series_version,
        "page_size": 2,
        "families": [{
            "family_id": "format-use-cases",
            "strategic_job": (
                "Show two different decisions where the confirmed format "
                "is relevant."
            ),
            "audience_stage": "Product-aware viewers.",
            "content_type_space": (
                "Demonstration, comparison, or another Director-chosen form."
            ),
            "viewer_value_role": (
                "Use the same truthful format reason in distinct use cases."
            ),
            "planned_variant_count": 2,
            "truth_options": ["Confirmed format"],
            "permitted_reuse": (
                "The confirmed format reason may repeat in both episodes."
            ),
            "differentiation_mandate": (
                "The viewer decision, evidence, and execution must differ."
            ),
        }],
        "pages": [{
            "page_index": 1,
            "start_variant_index": 1,
            "end_variant_index": 2,
            "territories": [
                {
                    "variant_index": 1,
                    "family_id": "format-use-cases",
                    "territory_id": "first-use-case",
                    "strategic_role": "Demonstrate an at-home choice.",
                    "audience_state": "A viewer simplifying a home routine.",
                    "audience_tension_or_need": "Preparation friction stops follow-through.",
                    "viewer_value_context": (
                        "The viewer compares formats for home use."
                    ),
                    "response_or_action_route": (
                        "The confirmed format supports the bounded choice."
                    ),
                    "truth_options": ["Confirmed format"],
                    "anti_repetition_rule": (
                        "Do not reuse the second episode's travel evidence."
                    ),
                },
                {
                    "variant_index": 2,
                    "family_id": "format-use-cases",
                    "territory_id": "second-use-case",
                    "strategic_role": "Compare a travel packing choice.",
                    "audience_state": "A viewer planning a short trip.",
                    "audience_tension_or_need": "A packing constraint removes bulky options.",
                    "viewer_value_context": (
                        "The viewer compares formats for travel packing."
                    ),
                    "response_or_action_route": (
                        "The confirmed format supports the bounded choice."
                    ),
                    "truth_options": [],
                    "anti_repetition_rule": (
                        "Do not rename the first episode's home-routine proof."
                    ),
                },
            ],
            "page_uniqueness_mandate": (
                "Reuse the truthful reason but not the decision or evidence."
            ),
        }],
    })

    coverage = finalize_series_coverage_map(
        draft,
        brief,
        page_size=2,
    )

    assert coverage.families[0].planned_variant_count == 2
    assert coverage.pages[0].territories[1].truth_options == [
        "Confirmed format"
    ]

    redundant_mismatch = draft.model_dump(mode="json")
    redundant_mismatch["families"][0]["planned_variant_count"] = 1
    redundant_mismatch["pages"][0]["territories"][1][
        "truth_options"
    ] = ["Secondary fact"]
    normalized = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(redundant_mismatch),
        brief,
        page_size=2,
    )
    assert normalized.families[0].planned_variant_count == 2
    assert normalized.families[0].truth_options == [
        "Confirmed format",
        "Secondary fact",
    ]
    assert normalized.pages[0].territories[1].truth_options == [
        "Secondary fact"
    ]
    assert {
        item.response_or_action_route
        for item in coverage.pages[0].territories
    } == {
        "The confirmed format supports the bounded choice."
    }


def test_coverage_patch_is_hash_bound_and_cannot_change_uncited_scope():
    brief = _structured_brief()
    base = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    patch = SeriesCoveragePatchDraft.model_validate(
        _coverage_patch_draft(brief, base=base)
    )

    merged = apply_series_coverage_patch(
        patch,
        base,
        brief,
        page_size=2,
        allowed_territory_ids={"territory-a1"},
    )

    assert merged.coverage_sha256 != base.coverage_sha256
    assert merged.pages[0].territories[0].territory_id == (
        "territory-a1-repaired"
    )
    assert (
        merged.pages[0].territories[1]
        == base.pages[0].territories[1]
    )
    assert merged.pages[1] == base.pages[1]

    stale = patch.model_copy(
        update={"base_coverage_sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="base hash is stale"):
        apply_series_coverage_patch(
            stale,
            base,
            brief,
            page_size=2,
            allowed_territory_ids={"territory-a1"},
        )

    uncited_payload = patch.model_dump(mode="json")
    uncited_payload["territory_updates"][0]["variant_index"] = 2
    with pytest.raises(ValueError, match="uncited variants"):
        apply_series_coverage_patch(
            SeriesCoveragePatchDraft.model_validate(uncited_payload),
            base,
            brief,
            page_size=2,
            allowed_territory_ids={"territory-a1"},
        )


def test_series_coverage_packet_is_compact_and_exposes_exact_attributes():
    brief = compile_universal_director_series_brief(
        series_id="coverage-packet-6",
        objective="Create distinct product videos.",
        platform="TikTok",
        locale="en-US",
        audience="US adult beginners.",
        target_count=6,
        minimum_duration_seconds=40,
        maximum_duration_seconds=40,
        product_required=True,
        brand_name="Example",
        product_name="Example Product",
        market="US",
        project_brief=None,
        confirmed_claims=["Attribute A"],
        confirmed_selling_points=["Attribute B"],
    )
    completed_history = [{
        "variant_index": 7,
        "content_fingerprint": {"premise": "Already delivered premise"},
        "semantic_sha256": "a" * 64,
    }]
    brief = brief.model_copy(update={
        "truth_payload": {
            **brief.truth_payload,
            "completed_content_history": completed_history,
        }
    })

    packet = build_series_coverage_packet(brief, page_size=10)

    assert "series_brief" not in packet
    contract = packet["series_strategy_contract"]
    assert contract["series_id"] == brief.series_id
    assert contract["conversion"][
        "allowed_truth_options"
    ] == brief.conversion.confirmed_differentiators
    assert "truth_payload" not in contract
    assert "capability_catalog" not in contract
    assert "copy_review_criteria" not in contract
    assert contract["completed_content_history"] == completed_history
    assert packet["strategy_rules"][
        "do_not_repeat_completed_content_history"
    ] is True
    assert "verbatim" in packet["strategy_rules"][
        "truth_option_copy_rule"
    ]
    assert packet["strategy_rules"][
        "finite_truth_or_value_reasons_may_repeat_within_a_family"
    ] is True
    assert packet["strategy_rules"][
        "do_not_invent_one_fact_or_value_premise_per_variant"
    ] is True
    assert packet["output_contract"]["properties"][
        "series_id"
    ]["const"] == brief.series_id
    assert packet["output_contract"]["properties"][
        "series_version"
    ]["const"] == brief.series_version
    assert packet["output_contract"]["properties"][
        "page_size"
    ]["const"] == 6
    assert packet["output_contract"]["$defs"][
        "SeriesContentFamily"
    ]["properties"]["truth_options"]["minItems"] == 1
    assert "minItems" not in packet["output_contract"]["$defs"][
        "SeriesCoverageTerritory"
    ]["properties"]["truth_options"]


def test_series_page_packet_has_dynamic_exact_contract_without_full_truth():
    brief = compile_universal_director_series_brief(
        series_id="page-packet-6",
        objective="Create distinct product videos.",
        platform="TikTok",
        locale="en-US",
        audience="US adult beginners.",
        target_count=6,
        minimum_duration_seconds=40,
        maximum_duration_seconds=40,
        product_required=True,
        brand_name="Example",
        product_name="Example Product",
        market="US",
        project_brief=None,
        confirmed_claims=["Attribute A"],
        confirmed_selling_points=["Attribute B"],
        structured_intent_contract_required=True,
    )
    completed_history = [{
        "variant_index": 7,
        "content_fingerprint": {"premise": "Already delivered premise"},
        "semantic_sha256": "b" * 64,
    }]
    brief = brief.model_copy(update={
        "truth_payload": {
            **brief.truth_payload,
            "completed_content_history": completed_history,
        }
    })
    packet = build_series_slate_page_packet(
        brief,
        page_index=1,
        total_pages=1,
        start_variant_index=1,
        end_variant_index=6,
        accepted_prior_intents=[],
    )

    assert "series_brief" not in packet
    strategy = packet["series_page_strategy_contract"]
    assert "truth_payload" not in strategy
    assert strategy["completed_content_history"] == completed_history
    assert packet["director_rules"][
        "do_not_repeat_completed_content_history"
    ] is True
    assert [row["dimension_id"] for row in strategy[
        "required_differentiation_dimensions"
    ]] == [
        row.dimension_id for row in brief.diversity_requirements
    ]

    definitions = packet["output_contract"]["$defs"]
    intent_properties = definitions["SeriesSlateIntent"]["properties"]
    differentiation = intent_properties["differentiation"]
    expected_dimensions = [
        row.dimension_id for row in brief.diversity_requirements
    ]
    assert differentiation["required"] == expected_dimensions
    assert differentiation["additionalProperties"] is False
    assert list(differentiation["properties"]) == expected_dimensions
    assert "pain_hypothesis" in definitions[
        "SeriesSlateIntent"
    ]["required"]
    assert "conversion_hypothesis" in definitions[
        "SeriesSlateIntent"
    ]["required"]
    assert "audio_mode" in definitions["SeriesSlateIntent"]["required"]
    assert packet["output_contract"]["properties"]["schema_version"][
        "const"
    ] == "2.0"
    assert "schema_version" in packet["output_contract"]["required"]
    assert intent_properties["pain_hypothesis"] == {
        "$ref": "#/$defs/PainHypothesis"
    }
    assert intent_properties["conversion_hypothesis"] == {
        "$ref": "#/$defs/ConversionHypothesis"
    }
    assert definitions["ConversionHypothesis"]["properties"][
        "confirmed_attribute"
    ]["enum"] == brief.conversion.confirmed_differentiators
    assert {
        "viewer_decision_or_use_case",
        "product_relevance_bridge",
        "reason_to_choose_or_consider",
    } <= set(definitions["ConversionHypothesis"]["required"])
    assert "preference_match" not in definitions[
        "ConversionHypothesis"
    ]["properties"]


@pytest.mark.asyncio
async def test_large_series_is_generated_in_bounded_resumable_pages():
    director = _FakeClient([
        _page_draft(
            start_variant_index=1,
            end_variant_index=5,
            page_index=1,
        ),
        _page_draft(
            start_variant_index=6,
            end_variant_index=10,
            page_index=2,
        ),
        _page_draft(
            start_variant_index=11,
            end_variant_index=12,
            page_index=3,
        ),
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"distinct": 95},
        "blocking_issues": [],
        "repair_scope": "slate_only",
    }])

    result = await run_content_series_slate_loop(
        brief=_paged_brief(target_count=12),
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=5,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert result.final_slate is not None
    assert len(result.final_slate.intents) == 12
    assert [
        (
            attempt.page_index,
            attempt.start_variant_index,
            attempt.end_variant_index,
        )
        for attempt in result.attempts
    ] == [
        (1, 1, 5),
        (2, 6, 10),
        (3, 11, 12),
    ]
    packets = [
        json.loads(call["input_text"])
        for call in director.calls
    ]
    assert [
        packet["page_contract"]["intent_count"]
        for packet in packets
    ] == [5, 5, 2]
    assert [
        len(packet["accepted_prior_intent_registry"])
        for packet in packets
    ] == [0, 5, 10]
    assert len(critic.calls) == 1
    critic_packet = json.loads(critic.calls[0]["input_text"])
    assert (
        critic_packet["review_rules"]
        ["verify_each_intent_can_satisfy_every_blocking_copy_criterion"]
        is True
    )
    assert (
        critic_packet["review_rules"]
        ["reject_unearned_required_conversion_transition"]
        is False
    )


@pytest.mark.asyncio
async def test_large_series_resumes_after_last_accepted_page():
    first_page = _page_draft(
        start_variant_index=1,
        end_variant_index=5,
        page_index=1,
    )
    director = _FakeClient([
        _page_draft(
            start_variant_index=6,
            end_variant_index=10,
            page_index=2,
        ),
        _page_draft(
            start_variant_index=11,
            end_variant_index=12,
            page_index=3,
        ),
    ])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"distinct": 95},
        "blocking_issues": [],
        "repair_scope": "slate_only",
    }])
    emitted: list[dict | None] = []

    result = await run_content_series_slate_loop(
        brief=_paged_brief(target_count=12),
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=5,
        ),
        director_client=director,
        critic_client=critic,
        resume_page_checkpoint={
            "schema_version": "1.0",
            "series_id": "generic-series",
            "series_version": 1,
            "revision": 1,
            "page_size": 5,
            "accepted_intents": first_page["intents"],
            "attempts": [],
            "contract_errors": [],
        },
        page_checkpoint_callback=emitted.append,
    )

    assert result.status == "approved"
    assert len(result.final_slate.intents) == 12
    assert len(director.calls) == 2
    assert [
        len(
            json.loads(call["input_text"])
            ["accepted_prior_intent_registry"]
        )
        for call in director.calls
    ] == [5, 10]
    assert [
        len(item["accepted_intents"])
        for item in emitted
        if item is not None
    ] == [10, 12]
    assert emitted[-1] is None


@pytest.mark.asyncio
async def test_structured_series_uses_coverage_page_reviews_and_compact_global_review():
    brief = _structured_brief()
    director = _FakeClient([
        _coverage_draft(),
        _structured_page(start=1, end=2, page_index=1),
        _structured_page(start=3, end=3, page_index=2),
    ])
    critic = _FakeClient([
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])
    emitted = []

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
        page_checkpoint_callback=emitted.append,
    )

    assert result.status == "approved"
    assert result.coverage_map is not None
    assert len(result.final_slate.intents) == 3
    assert len(result.page_reviews) == 2
    assert len(result.reviews) == 1
    assert len(director.calls) == 3
    assert len(critic.calls) == 4
    page_critic_packet = json.loads(
        critic.calls[1]["input_text"]
    )
    assert (
        page_critic_packet["review_rules"]
        ["do_not_require_script_lines_or_visual_artifacts"]
        is True
    )
    assert page_critic_packet["review_rules"][
        "reject_any_audio_prose_or_plan_that_contradicts_audio_mode"
    ] is True
    assert page_critic_packet["review_rules"][
        "audio_mode_semantics"
    ]["silent"].startswith("No audible sound")
    assert {
        item["criterion_id"]
        for item in page_critic_packet["review_criteria"]
    } == {
        item.criterion_id
        for item in brief.series_page_review_criteria
    }
    global_packet = json.loads(critic.calls[-1]["input_text"])
    assert "intent_fingerprints" in global_packet
    assert all(
        "audio_mode" in item
        for item in global_packet["intent_fingerprints"]
    )
    assert "series_slate" not in global_packet
    assert any(
        item is not None
        and item.get("progress", {}).get("phase")
        == "global_critic"
        for item in emitted
    )
    assert any(
        item is not None
        and len(item.get("global_reviews") or []) == 1
        for item in emitted
    )
    assert emitted[-1] is None


@pytest.mark.asyncio
async def test_coverage_semantic_repair_happens_before_page_generation():
    brief = _structured_brief()
    base_coverage = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    repaired_patch = _coverage_patch_draft(
        brief,
        base=base_coverage,
        audience_tension_or_need="A distinct loss of a concrete learning resource.",
        response_or_action_route=(
            "Resolve by choosing a different bounded learning action."
        ),
    )
    director = _FakeClient([
        _coverage_draft(),
        repaired_patch,
        _structured_page(start=1, end=2, page_index=1),
        _structured_page(start=3, end=3, page_index=2),
    ])
    critic = _FakeClient([
        _rejected_series_verdict(
            brief.series_global_review_criteria,
            intent_ids=["territory-a1"],
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])
    emitted = []

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
        page_checkpoint_callback=emitted.append,
    )

    assert result.status == "approved"
    assert len(result.coverage_reviews) == 2
    assert result.coverage_reviews[0].verdict.approved is False
    assert result.coverage_reviews[1].verdict.approved is True
    assert [
        json.loads(call["input_text"])["role"]
        for call in director.calls[:2]
    ] == [
        "content_series_strategy",
        "content_series_strategy_patch",
    ]
    repair_packet = json.loads(director.calls[1]["input_text"])
    assert repair_packet["patch_scope"]["allowed_territory_ids"] == [
        "territory-a1"
    ]
    assert repair_packet["patch_scope"][
        "base_coverage_sha256"
    ] == base_coverage.coverage_sha256
    assert repair_packet["critic_blocking_issues"][0][
        "intent_ids"
    ] == ["territory-a1"]
    rejected_checkpoints = [
        item
        for item in emitted
        if item is not None
        and item.get("progress", {}).get("phase")
        == "coverage_semantic_repair"
    ]
    assert len(rejected_checkpoints) == 1
    assert len(rejected_checkpoints[0]["coverage_reviews"]) == 1
    assert (
        rejected_checkpoints[0]["coverage_reviews"][0]
        ["verdict"]["approved"]
        is False
    )
    assert rejected_checkpoints[0]["accepted_intents"] == []


@pytest.mark.asyncio
async def test_rejected_coverage_checkpoint_resumes_exact_revision():
    brief = _structured_brief()
    base_coverage = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    first_emitted = []
    first_director = _FakeClient([_coverage_draft()])
    first_critic = _FakeClient([
        _rejected_series_verdict(
            brief.series_global_review_criteria,
            intent_ids=["territory-a1"],
        ),
    ])

    first_result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=first_director,
        critic_client=first_critic,
        page_checkpoint_callback=first_emitted.append,
    )

    assert first_result.status == "quality_pause"
    checkpoint = next(
        item
        for item in reversed(first_emitted)
        if item is not None
    )
    assert checkpoint["progress"]["phase"] == (
        "coverage_semantic_repair"
    )
    assert checkpoint["coverage_map"] is not None
    assert len(checkpoint["coverage_reviews"]) == 1

    same_budget_director = _FakeClient([])
    same_budget_critic = _FakeClient([])
    same_budget_result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=same_budget_director,
        critic_client=same_budget_critic,
        resume_page_checkpoint=checkpoint,
    )
    assert same_budget_result.status == "quality_pause"
    assert "explicitly increase" in same_budget_result.reason
    assert same_budget_director.calls == []
    assert same_budget_critic.calls == []

    repaired_patch = _coverage_patch_draft(
        brief,
        base=base_coverage,
        audience_tension_or_need="A different concrete loss of learning access.",
        response_or_action_route="Resolve with a distinct bounded action.",
    )
    second_director = _FakeClient([
        repaired_patch,
        _structured_page(start=1, end=2, page_index=1),
        _structured_page(start=3, end=3, page_index=2),
    ])
    second_critic = _FakeClient([
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])

    second_result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=second_director,
        critic_client=second_critic,
        resume_page_checkpoint=checkpoint,
    )

    assert second_result.status == "approved"
    assert len(second_director.calls) == 3
    revision_packet = json.loads(
        second_director.calls[0]["input_text"]
    )
    assert revision_packet["role"] == "content_series_strategy_patch"
    assert revision_packet["patch_scope"][
        "base_coverage_sha256"
    ] == checkpoint["coverage_map"]["coverage_sha256"]
    assert revision_packet["critic_blocking_issues"][0][
        "intent_ids"
    ] == ["territory-a1"]


@pytest.mark.asyncio
async def test_failed_coverage_revision_preserves_last_candidate_for_resume():
    brief = _structured_brief()
    base_coverage = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    first_emitted = []
    first_director = _FakeClient([
        _coverage_draft(),
        "not-an-object",
        "still-not-an-object",
    ])
    first_critic = _FakeClient([
        _rejected_series_verdict(
            brief.series_global_review_criteria,
            intent_ids=["territory-a1"],
        ),
    ])

    first_result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=first_director,
        critic_client=first_critic,
        page_checkpoint_callback=first_emitted.append,
    )

    assert first_result.status == "quality_pause"
    checkpoint = next(
        item
        for item in reversed(first_emitted)
        if item is not None
    )
    assert checkpoint["progress"]["phase"] == "coverage_contract_pause"
    assert checkpoint["progress"]["coverage_revision"] == 2
    assert checkpoint["coverage_map"] is not None
    assert checkpoint["coverage_reviews"][0]["verdict"]["approved"] is False

    repaired_patch = _coverage_patch_draft(
        brief,
        base=base_coverage,
        audience_tension_or_need="A distinct concrete loss after resuming.",
        response_or_action_route="Resolve with a different bounded action.",
    )
    second_director = _FakeClient([
        repaired_patch,
        _structured_page(start=1, end=2, page_index=1),
        _structured_page(start=3, end=3, page_index=2),
    ])
    second_critic = _FakeClient([
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])

    second_result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=2,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=second_director,
        critic_client=second_critic,
        resume_page_checkpoint=checkpoint,
    )

    assert second_result.status == "approved"
    revision_packet = json.loads(
        second_director.calls[0]["input_text"]
    )
    assert revision_packet["role"] == "content_series_strategy_patch"
    assert revision_packet["patch_scope"][
        "base_coverage_sha256"
    ] == checkpoint["coverage_map"]["coverage_sha256"]
    assert revision_packet["critic_blocking_issues"][0][
        "intent_ids"
    ] == ["territory-a1"]


@pytest.mark.asyncio
async def test_structured_series_resumes_only_a_locally_approved_page():
    brief = _structured_brief()
    coverage = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    page_one = _structured_page(
        start=1,
        end=2,
        page_index=1,
    )["intents"]
    approved_verdict = IndependentSeriesCriticVerdict.model_validate(
        _approved_series_verdict(
            brief.series_page_review_criteria
        )
    )
    approved_page_review = SeriesPageCriticReview(
        page_index=1,
        page_revision=1,
        intent_ids=["structured-1", "structured-2"],
        verdict=approved_verdict,
        latency_ms=1,
        response_sha256="a" * 64,
    )
    approved_coverage_review = SeriesCoverageCriticReview(
        revision=1,
        coverage_sha256=coverage.coverage_sha256,
        verdict=IndependentSeriesCriticVerdict.model_validate(
            _approved_series_verdict(
                brief.series_global_review_criteria
            )
        ),
        latency_ms=1,
        response_sha256="b" * 64,
    )
    director = _FakeClient([
        _structured_page(start=3, end=3, page_index=2),
    ])
    critic = _FakeClient([
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
        resume_page_checkpoint={
            "schema_version": "1.0",
            "series_id": brief.series_id,
            "series_version": brief.series_version,
            "revision": 1,
            "page_size": 2,
                "coverage_map": coverage.model_dump(mode="json"),
                "coverage_reviews": [
                    approved_coverage_review.model_dump(mode="json")
                ],
            "accepted_intents": page_one,
            "attempts": [],
            "page_reviews": [
                approved_page_review.model_dump(mode="json")
            ],
            "contract_errors": [],
        },
    )

    assert result.status == "approved"
    assert len(director.calls) == 1
    assert (
        json.loads(director.calls[0]["input_text"])
        ["page_contract"]["page_index"]
        == 2
    )


@pytest.mark.asyncio
async def test_rejected_page_resume_repairs_exact_candidate_at_next_revision():
    brief = _structured_brief()
    coverage = finalize_series_coverage_map(
        SeriesCoverageMapDraft.model_validate(_coverage_draft()),
        brief,
        page_size=2,
    )
    page_one = _structured_page(
        start=1,
        end=2,
        page_index=1,
    )["intents"]
    page_two = _structured_page(
        start=3,
        end=3,
        page_index=2,
    )["intents"]
    approved = IndependentSeriesCriticVerdict.model_validate(
        _approved_series_verdict(brief.series_page_review_criteria)
    )
    rejected = IndependentSeriesCriticVerdict.model_validate(
        _rejected_series_verdict(
            brief.series_page_review_criteria,
            intent_ids=["structured-3"],
        )
    )
    approved_page_review = SeriesPageCriticReview(
        page_index=1,
        page_revision=1,
        intent_ids=["structured-1", "structured-2"],
        verdict=approved,
        latency_ms=1,
        response_sha256="a" * 64,
    )
    rejected_page_review = SeriesPageCriticReview(
        page_index=2,
        page_revision=2,
        intent_ids=["structured-3"],
        verdict=rejected,
        latency_ms=1,
        response_sha256="b" * 64,
    )
    coverage_review = SeriesCoverageCriticReview(
        revision=1,
        coverage_sha256=coverage.coverage_sha256,
        verdict=IndependentSeriesCriticVerdict.model_validate(
            _approved_series_verdict(
                brief.series_global_review_criteria
            )
        ),
        latency_ms=1,
        response_sha256="c" * 64,
    )
    repaired_page = _structured_page(
        start=3,
        end=3,
        page_index=2,
    )
    repaired_page["intents"][0]["objective"] = (
        "Explain the cited idea with a repaired concrete route."
    )
    director = _FakeClient([repaired_page])
    critic = _FakeClient([
        _approved_series_verdict(brief.series_page_review_criteria),
        _approved_series_verdict(brief.series_global_review_criteria),
    ])

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_series_revisions=2,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
        resume_page_checkpoint={
            "schema_version": "1.0",
            "series_id": brief.series_id,
            "series_version": brief.series_version,
            "revision": 1,
            "page_size": 2,
            "coverage_map": coverage.model_dump(mode="json"),
            "coverage_reviews": [
                coverage_review.model_dump(mode="json")
            ],
            "accepted_intents": page_one,
            "page_candidates": {"2": page_two},
            "attempts": [],
            "page_reviews": [
                approved_page_review.model_dump(mode="json"),
                rejected_page_review.model_dump(mode="json"),
            ],
            "contract_errors": [],
        },
    )

    assert result.status == "approved"
    assert len(director.calls) == 1
    request = json.loads(director.calls[0]["input_text"])
    assert director.calls[0]["metadata"]["page_revision"] == 3
    assert (
        request["revision_context"]["current_page_intents"][0]
        ["intent_id"]
        == "structured-3"
    )
    assert (
        request["revision_context"]
        ["critic_blocking_issues_for_page"][0]["intent_ids"]
        == ["structured-3"]
    )


@pytest.mark.asyncio
async def test_series_critic_contract_is_repaired_without_regenerating_page():
    brief = _structured_brief()
    malformed = _approved_series_verdict(
        brief.series_page_review_criteria
    )
    malformed["scores"]["downstream_script_score"] = 100
    director = _FakeClient([
        _coverage_draft(),
        _structured_page(start=1, end=2, page_index=1),
        _structured_page(start=3, end=3, page_index=2),
    ])
    critic = _FakeClient([
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        malformed,
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(director.calls) == 3
    repair_packet = json.loads(critic.calls[2]["input_text"])
    assert repair_packet["role"] == "series_critic_contract_repair"
    assert (
        repair_packet["repair_rules"]
        ["score_exactly_supplied_criterion_ids"]
        is True
    )


@pytest.mark.asyncio
async def test_structured_series_repairs_only_the_rejected_page():
    brief = _structured_brief()
    repaired_page_one = _structured_page(
        start=1,
        end=2,
        page_index=1,
    )
    repaired_page_one["intents"][0]["objective"] = (
        "Explain a repaired, concrete idea."
    )
    director = _FakeClient([
        _coverage_draft(),
        _structured_page(start=1, end=2, page_index=1),
        repaired_page_one,
        _structured_page(start=3, end=3, page_index=2),
    ])
    critic = _FakeClient([
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        _rejected_series_verdict(
            brief.series_page_review_criteria,
            intent_ids=["structured-1"],
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [
        attempt.page_index
        for attempt in result.attempts
        if attempt.page_index is not None
    ] == [1, 1, 2]
    assert len([
        review
        for review in result.page_reviews
        if review.page_index == 1
    ]) == 2
    assert len([
        review
        for review in result.page_reviews
        if review.page_index == 2
    ]) == 1
    repair_packet = json.loads(
        director.calls[2]["input_text"]
    )
    assert (
        repair_packet["revision_context"]
        ["critic_blocking_issues_for_page"][0]["intent_ids"]
        == ["structured-1"]
    )


@pytest.mark.asyncio
async def test_global_review_regenerates_only_the_cited_page():
    brief = _structured_brief()
    repaired_page_one = _structured_page(
        start=1,
        end=2,
        page_index=1,
    )
    repaired_page_one["intents"][0]["creative_strategy"] = {
        "approach": "globally-distinct-repair"
    }
    director = _FakeClient([
        _coverage_draft(),
        _structured_page(start=1, end=2, page_index=1),
        _structured_page(start=3, end=3, page_index=2),
        repaired_page_one,
    ])
    critic = _FakeClient([
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _rejected_series_verdict(
            brief.series_global_review_criteria,
            intent_ids=["structured-1"],
        ),
        _approved_series_verdict(
            brief.series_page_review_criteria
        ),
        _approved_series_verdict(
            brief.series_global_review_criteria
        ),
    ])

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=2,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [
        attempt.page_index
        for attempt in result.attempts
        if attempt.page_index is not None
    ] == [1, 2, 1]
    assert len([
        review
        for review in result.page_reviews
        if review.page_index == 2
    ]) == 1
    assert len(result.reviews) == 2
    global_repair_packet = json.loads(
        director.calls[-1]["input_text"]
    )
    assert (
        global_repair_packet["revision_context"]
        ["critic_blocking_issues_for_page"][0]["intent_ids"]
        == ["structured-1"]
    )


@pytest.mark.asyncio
async def test_series_loop_revises_then_persists_idempotently(db_session):
    rejected = copy.deepcopy(_draft())
    approved = copy.deepcopy(_draft())
    director = _FakeClient([rejected, approved])
    critic = _FakeClient([
        {
            "approved": False,
            "scores": {"distinct": 60},
            "blocking_issues": [{
                "code": "SEMANTIC_DUPLICATE",
                "intent_ids": ["intent-one", "intent-two"],
                "evidence": "The two explanations feel too similar.",
                "repair_instruction": "Change only intent two's visual logic.",
            }],
            "repair_scope": "slate_only",
        },
        {
            "approved": True,
            "scores": {"distinct": 95},
            "blocking_issues": [],
            "repair_scope": "slate_only",
        },
    ])
    result = await run_content_series_slate_loop(
        brief=_brief(),
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
        ),
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(result.attempts) == 2
    assert len(result.reviews) == 2
    revision_packet = json.loads(director.calls[1]["input_text"])
    assert revision_packet["role"] == "content_series_revision"
    assert "blocking_issues" in revision_packet["critic_verdict"]
    assert "content_series_revision" in director.calls[1]["instructions"]

    project = SimpleNamespace(id=901, workspace_id=12, user_id=34)
    first = persist_approved_series_slate(
        db_session,
        project=project,
        brief=_brief(),
        result=result,
    )
    db_session.commit()
    second = persist_approved_series_slate(
        db_session,
        project=project,
        brief=_brief(),
        result=result,
    )
    db_session.commit()
    assert first.id == second.id

    changed = _brief().model_copy(
        update={"audience": "A different audience."}
    )
    with pytest.raises(ValueError, match="immutable project version"):
        persist_approved_series_slate(
            db_session,
            project=project,
            brief=changed,
            result=result,
        )


def test_series_stage_materializes_briefs_without_queuing_media(
    db_session,
    monkeypatch,
):
    brief = _brief().model_copy(
        update={
            "capability_catalog": (
                load_content_capability_manifest().capabilities
            )
        }
    )
    director = _FakeClient([_draft()])
    critic = _FakeClient([{
        "approved": True,
        "scores": {"distinct": 95},
        "blocking_issues": [],
        "repair_scope": "slate_only",
    }])
    approved_result = asyncio.run(
        run_content_series_slate_loop(
            brief=brief,
            policy=DirectorLoopPolicy(
                maximum_revisions=0,
                maximum_contract_repairs_per_revision=1,
            ),
            director_client=director,
            critic_client=critic,
        ),
    )

    seen_resume_checkpoints = []
    seen_policies = []

    async def _approved_loop(**kwargs):
        seen_resume_checkpoints.append(
            kwargs.get("resume_page_checkpoint")
        )
        seen_policies.append(kwargs["policy"])
        callback = kwargs["page_checkpoint_callback"]
        callback({
            "schema_version": "1.0",
            "series_id": brief.series_id,
            "series_version": brief.series_version,
            "revision": 1,
            "page_size": 10,
            "accepted_intents": [],
            "attempts": [],
            "contract_errors": [],
        })
        callback(None)
        return approved_result

    monkeypatch.setattr(
        "app.services.hermes_agent.content_series_runtime."
        "run_content_series_slate_loop",
        _approved_loop,
    )
    project = create_project(
        db_session,
        workspace_id=1202,
        user_id=3402,
        title="No-media series stage",
        content_mode="entertainment",
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=40,
        video_language="en-US",
        content_director_mode="enforce",
        director_series_brief=brief.model_dump(mode="json"),
        director_loop_policy={
            "maximum_revisions": 0,
            "maximum_contract_repairs_per_revision": 1,
        },
        auto_run=False,
    )
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="SERIES_DIRECTOR",
        attempt=1,
        status="running",
        input_json={
            "continue_workflow": False,
            "variant_index": 1,
        },
    )
    db_session.add(stage)
    db_session.commit()

    outcome = _run_content_series_director_stage(
        db_session,
        stage_row=stage,
        project=project,
        request_id="",
        delivery_run_token="",
    )
    db_session.refresh(project)
    db_session.refresh(stage)

    assert outcome["status"] == "success"
    assert stage.status == "success"
    assert project.current_stage == "DIRECTOR"
    assert len(project.config_json["director_briefs_by_variant"]) == 3
    assert (
        project.state_json["approved_series_slate"]["intent_count"]
        == 3
    )
    assert stage.output_json["evidence"]["media_authorized"] is False
    assert stage.output_json["evidence"]["coverage_reviews"] == []
    assert stage.output_json["evidence"]["page_reviews"] == []
    assert seen_resume_checkpoints == [{}]
    assert seen_policies[0].maximum_revisions == 0
    assert seen_policies[0].series_revision_limit == 5
    assert (
        "series_director_page_checkpoint"
        not in dict(stage.input_json or {})
    )
    active_media_stages = (
        db_session.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage.in_(
                ("VISUAL_PREVIEW", "FINAL_ASSETS", "VIDEO_PROMPTS")
            ),
        )
        .count()
    )
    assert active_media_stages == 0


def test_series_stage_locks_completed_history_and_queues_active_missing_variant(
    db_session,
    monkeypatch,
    tmp_path,
):
    brief = _brief().model_copy(
        update={
            "capability_catalog": (
                load_content_capability_manifest().capabilities
            )
        }
    )
    original_slate = finalize_series_slate(
        SeriesSlateDraft.model_validate(_draft()),
        brief,
    )
    existing_variant_one = materialize_series_director_briefs(
        brief,
        original_slate,
    )["1"]
    project = create_project(
        db_session,
        workspace_id=1203,
        user_id=3403,
        title="Continuation series stage",
        content_mode="education",
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=40,
        video_language="en-US",
        content_director_mode="enforce",
        director_series_brief=brief.model_dump(mode="json"),
        director_briefs_by_variant={"1": existing_variant_one},
        director_loop_policy={
            "maximum_revisions": 0,
            "maximum_contract_repairs_per_revision": 1,
        },
        auto_run=False,
    )
    video_path = tmp_path / "v01-complete.mp4"
    video_path.write_bytes(b"x" * 2048)
    db_session.add(HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name=video_path.name,
        file_path=str(video_path),
        mime_type="video/mp4",
        size_bytes=video_path.stat().st_size,
        meta_json={
            "content_factory_video_index": 1,
            "content_factory_variant_index": 1,
            "is_composed_final": True,
        },
    ))
    db_session.add(HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="CREATIVE",
        attempt=1,
        status="superseded",
        input_json={"variant_index": 1},
        output_json={
            "result": {
                "selected_concept": {
                    "title": "Already delivered lever story",
                    "logline": "A seesaw demonstrates the tradeoff.",
                },
                "complete_video_script": {
                    "opening": "A seesaw looks balanced until force moves.",
                    "resolution": "Distance changes the needed effort.",
                },
            }
        },
    ))
    pipeline = dict(project.state_json or {})
    pipeline["video_variant_pipeline"] = {
        "target_count": 3,
        "active_index": 3,
        "submitted_indices": [1],
        "completed_indices": [1],
        "failed_indices": [],
    }
    pipeline["active_variant_index"] = 3
    project.state_json = pipeline
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="SERIES_DIRECTOR",
        attempt=1,
        status="running",
        input_json={
            "continue_workflow": True,
            "variant_index": 1,
            "series_director_page_checkpoint": {
                "accepted_intents": _draft()["intents"],
            },
        },
    )
    db_session.add(stage)
    db_session.commit()

    captured: dict[str, object] = {}

    async def _approved_continuation_loop(**kwargs):
        runtime_brief = kwargs["brief"]
        captured["brief"] = runtime_brief
        captured["resume"] = kwargs.get("resume_page_checkpoint")
        continuation_draft = copy.deepcopy(_draft())
        continuation_draft["intents"] = continuation_draft["intents"][:2]
        return SeriesSlateLoopResult(
            status="approved",
            final_slate=finalize_series_slate(
                SeriesSlateDraft.model_validate(continuation_draft),
                runtime_brief,
            ),
            reason="approved continuation",
        )

    queued: dict[str, object] = {}

    def _capture_queue_stage(db, **kwargs):
        del db
        queued.update(kwargs)
        return SimpleNamespace(id=999)

    monkeypatch.setattr(
        "app.services.hermes_agent.content_series_runtime."
        "run_content_series_slate_loop",
        _approved_continuation_loop,
    )
    monkeypatch.setattr(
        "app.services.hermes_agent.content_factory.queue_stage",
        _capture_queue_stage,
    )

    outcome = _run_content_series_director_stage(
        db_session,
        stage_row=stage,
        project=project,
        request_id="",
        delivery_run_token="",
    )
    db_session.refresh(project)
    db_session.refresh(stage)

    runtime_brief = captured["brief"]
    assert runtime_brief.target_count == 2
    history = runtime_brief.truth_payload["completed_content_history"]
    assert [item["variant_index"] for item in history] == [1]
    assert captured["resume"] == {}
    assert project.config_json["director_briefs_by_variant"]["1"] == (
        existing_variant_one
    )
    assert set(project.config_json["director_briefs_by_variant"]) == {
        "1", "2", "3",
    }
    assert (
        project.config_json["director_briefs_by_variant"]["2"]
        ["truth_payload"]["series_intent"]["variant_index"]
        == 2
    )
    assert project.state_json["approved_series_slate"][
        "planned_variant_indices"
    ] == [2, 3]
    assert project.state_json["video_variant_pipeline"]["active_index"] == 3
    assert "variant 3" in str(queued["instruction"])
    assert queued["target_stage"] == "DIRECTOR"
    assert outcome["status"] == "success"


def test_series_stage_soft_limit_resumes_the_same_durable_checkpoint(
    db_session,
    monkeypatch,
):
    brief = _brief().model_copy(
        update={
            "capability_catalog": (
                load_content_capability_manifest().capabilities
            )
        }
    )
    project = create_project(
        db_session,
        workspace_id=1203,
        user_id=3403,
        title="Checkpointed series execution window",
        content_mode="entertainment",
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=40,
        video_language="en-US",
        content_director_mode="enforce",
        director_series_brief=brief.model_dump(mode="json"),
        director_loop_policy={
            "maximum_revisions": 1,
            "maximum_contract_repairs_per_revision": 1,
        },
        auto_run=False,
    )
    checkpoint = {
        "schema_version": "1.0",
        "series_id": brief.series_id,
        "series_version": brief.series_version,
        "revision": 1,
        "page_size": 10,
        "accepted_intents": [],
        "attempts": [],
        "contract_errors": [],
        "progress": {
            "phase": "page_director",
            "page_index": 1,
            "page_revision": 1,
        },
    }
    project.status = "queued"
    project.current_stage = "SERIES_DIRECTOR"
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="SERIES_DIRECTOR",
        attempt=1,
        status="queued",
        input_json={
            "continue_workflow": False,
            "variant_index": 1,
            "series_director_page_checkpoint": checkpoint,
        },
    )
    db_session.add(stage)
    db_session.commit()

    def _soft_limit(*_args, **_kwargs):
        raise content_factory_tasks.SoftTimeLimitExceeded()

    monkeypatch.setattr(
        content_factory_tasks,
        "_run_content_series_director_stage",
        _soft_limit,
    )

    outcome = content_factory_tasks.run_content_factory_stage.run(
        stage_id=int(stage.id),
        run_token=None,
    )
    db_session.expire_all()
    stage = db_session.get(HermesContentFactoryStage, int(stage.id))
    project = db_session.get(HermesContentFactoryProject, int(project.id))

    assert outcome["status"] == (
        "series_director_checkpoint_resume_scheduled"
    ), {
        "outcome": outcome,
        "stage_status": stage.status,
        "stage_error": stage.error_message,
        "project_status": project.status,
        "project_error": project.last_error,
    }
    assert outcome["checkpoint_progress"] == checkpoint["progress"]
    assert stage.status == "retrying"
    assert stage.celery_task_id is None
    assert stage.input_json["series_director_page_checkpoint"] == checkpoint
    assert stage.input_json["series_director_timeout_resume_count"] == 1
    assert stage.input_json["self_heal_action"] == (
        "resume_series_director_from_durable_checkpoint"
    )
    assert project.status == "queued"
    assert project.current_stage == "SERIES_DIRECTOR"


def test_manual_series_resume_stage_inherits_same_plan_checkpoint(
    db_session,
    monkeypatch,
):
    brief = _brief().model_copy(
        update={
            "capability_catalog": (
                load_content_capability_manifest().capabilities
            )
        }
    )
    project = create_project(
        db_session,
        workspace_id=1204,
        user_id=3404,
        title="Quality-paused series checkpoint",
        content_mode="entertainment",
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=40,
        video_language="en-US",
        content_director_mode="enforce",
        director_series_brief=brief.model_dump(mode="json"),
        director_loop_policy={
            "maximum_revisions": 1,
            "maximum_contract_repairs_per_revision": 1,
        },
        auto_run=False,
    )
    checkpoint = {
        "schema_version": "1.0",
        "series_id": brief.series_id,
        "series_version": brief.series_version,
        "revision": 2,
        "page_size": 10,
        "coverage_reviews": [],
        "accepted_intents": [],
    }
    prior = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="SERIES_DIRECTOR",
        attempt=1,
        status="failed",
        input_json={
            "series_director_page_checkpoint": checkpoint,
            "series_director_plan_signature": "same-plan",
            "completed_content_history_sha256": "same-history",
        },
    )
    project.status = "ready"
    project.current_stage = "SERIES_DIRECTOR"
    db_session.add(prior)
    db_session.commit()

    monkeypatch.setattr(
        content_factory_tasks.run_content_factory_stage,
        "apply_async",
        lambda **_kwargs: SimpleNamespace(id="series-resume-task"),
    )
    monkeypatch.setattr(
        "app.services.hermes_agent.content_factory."
        "hibernate_project_browser_slot_for_api_video",
        lambda *_args, **_kwargs: None,
    )

    successor = queue_stage(
        db_session,
        project=project,
        user_id=int(project.user_id),
        instruction="Continue the approved checkpoint.",
        target_stage="SERIES_DIRECTOR",
        continue_workflow=False,
    )

    assert successor.id != prior.id
    assert successor.input_json[
        "series_director_page_checkpoint"
    ] == checkpoint
    assert successor.input_json[
        "resumed_series_checkpoint_stage_id"
    ] == prior.id
    assert successor.input_json["series_director_plan_signature"] == (
        "same-plan"
    )
    assert successor.input_json["self_heal_action"] == (
        "resume_series_director_from_quality_checkpoint"
    )
