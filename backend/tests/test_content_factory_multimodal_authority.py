from __future__ import annotations

import json
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _source(relative: str) -> str:
    return (BACKEND / relative).read_text(encoding="utf-8")


def _function_source(source: str, function_name: str) -> str:
    start = source.index(f"def {function_name}(")
    next_function = source.find("\ndef ", start + 1)
    return source[start:] if next_function < 0 else source[start:next_function]


def test_semantic_stages_have_model_authority_without_server_authored_fallbacks():
    tasks = _source("app/tasks/hermes_agent/content_factory_tasks.py")
    api = _source("app/services/hermes_agent/content_factory_api.py")
    director = _source("app/services/hermes_agent/content_director_runtime.py")
    production = _source(
        "app/services/hermes_agent/content_production_plan_runtime.py"
    )

    banned = (
        "gmv-runtime-deterministic-allocation",
        "deterministic_server_fallback",
        "server_overrode_false_negative_creative_review",
        "auto_accepted_policy_constrained_video_prompts",
        "accepted_with_unconfirmed_constraints",
        "_generated_reference_text_dependencies",
        "_creative_reference_text_dependencies",
        "_assert_adult_only_product_story",
        "_assert_single_adult_visual_story",
    )
    active_sources = "\n".join((api, director, production))
    for marker in banned:
        assert marker not in active_sources

    # Legacy fallback approvals are mentioned only by the migration/self-heal
    # detector that invalidates them. They must never be created by live code.
    assert "deterministic_server_fallback" in tasks
    assert tasks.count("deterministic_server_fallback") == 1


def test_text_only_packets_still_use_multimodal_capable_routes():
    api = _source("app/services/hermes_agent/content_factory_api.py")

    assert 'capability = "multimodal"' in api
    assert 'capability=("multimodal" if len(user_content) > 1 else "text")' not in api
    assert '"creative_authority": "multimodal_visual_reviewer"' in api


def test_semantic_retry_classification_has_no_business_keyword_gate():
    tasks = _source("app/tasks/hermes_agent/content_factory_tasks.py")

    for function_name in (
        "_is_text_api_output_validation_failure",
        "_is_semantic_text_payload_failure",
    ):
        function = _function_source(tasks, function_name)
        assert "marker in str(error)" not in function
        assert "isinstance(error, (ValueError, ContentFactoryApiError))" in function


def test_active_director_pipeline_does_not_rejudge_model_prose_with_regexes():
    tasks = _source("app/tasks/hermes_agent/content_factory_tasks.py")
    plan = _source("app/services/hermes_agent/content_production_plan.py")
    compiler = _source(
        "app/services/hermes_agent/content_production_compiler.py"
    )
    production_runtime = _source(
        "app/services/hermes_agent/content_production_plan_runtime.py"
    )

    unbound_binding = _function_source(
        tasks, "_remove_unbound_product_visual_requirements"
    )
    assert "visual_reference_mentions_product" not in unbound_binding
    assert "server_semantic_filtering_used" in unbound_binding

    visual_validator = _function_source(plan, "_validate_visual_program")
    audio_validator = _function_source(plan, "_validate_audio_program")
    for validator in (visual_validator, audio_validator):
        assert "unbound_product_visual_depiction_evidence" not in validator
        assert "re.search" not in validator

    assert "unbound_product_visual_depiction_evidence" not in compiler
    assert "Preserve the independently reviewed Director purpose text" in compiler
    assert "physical_state_and_action_are_coherent" in production_runtime
    assert "visible_product_uses_authoritative_product_reference" in (
        production_runtime
    )


def test_producer_and_segment_normalizer_do_not_use_creative_keyword_gates():
    producer = _source("app/services/hermes_agent/content_producer.py")
    tasks = _source("app/tasks/hermes_agent/content_factory_tasks.py")
    api = _source("app/services/hermes_agent/content_factory_api.py")

    script_validator = _function_source(producer, "_validate_script_decision")
    video_normalizer = _function_source(tasks, "_normalize_video_plan")
    semantic_prompt = _function_source(
        producer,
        "_semantic_review_instructions",
    )

    assert "_validate_pacing_density" not in producer
    assert "_FAST_PACING_MARKERS" not in producer
    assert "_SPARSE_DELIVERY_MARKERS" not in producer
    assert "latest_user_message.lower" not in script_validator
    assert "whole_video_markers" not in video_normalizer
    assert "prompt contains whole-video scope" not in video_normalizer
    assert "not by matching isolated words" in semantic_prompt
    assert "_provider_direction_has_measurable_edit_grammar" not in api


def test_every_complete_producer_handoff_receives_semantic_review():
    producer = _source("app/services/hermes_agent/content_producer.py")

    assert "requires_semantic_review = bool(" in producer
    review_gate = producer.split("requires_semantic_review = bool(", 1)[1].split(
        "if requires_semantic_review:", 1
    )[0]
    assert "decision.intent_spec is not None" in review_gate
    assert "decision.proposal is not None" in review_gate
    assert "transformation_contract" not in review_gate
    assert "content_mode" not in review_gate
    assert "selected is not None" not in review_gate


def test_director_and_critic_roles_are_multimodal_capable():
    policy = json.loads(
        (REPO / "ops/hermes-content-director/routing-policy.json").read_text(
            encoding="utf-8"
        )
    )

    for role in ("director", "critic", "visual_inspector"):
        assert policy["roles"][role]["capability"] == "multimodal"
        assert policy["roles"][role]["sources"]


def test_rendered_media_quality_decisions_use_multimodal_reviewers():
    api = _source("app/services/hermes_agent/content_factory_api.py")
    tasks = _source("app/tasks/hermes_agent/content_factory_tasks.py")

    required_reviewers = (
        "review_provider_rendered_product_video_api",
        "review_provider_rendered_segment_execution_api",
        "review_spoken_copy_semantics_api",
        "review_composed_intent_fidelity_api",
    )
    for reviewer in required_reviewers:
        assert f"def {reviewer}(" in api
        assert f"{reviewer}(" in tasks
