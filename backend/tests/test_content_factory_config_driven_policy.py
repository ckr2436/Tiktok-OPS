from types import SimpleNamespace
from pathlib import Path

from app.tasks.hermes_agent.content_factory_tasks import (
    _creative_cast_policy,
    _creative_copy_contract,
    _editor_guidance_hashtag_list,
    _product_presentation_policy,
    _product_conversion_points,
    _promotion_line_for_speech,
)
from app.features.tenants.hermes_agent.schemas import ContentFactoryProjectUpdate
from app.services.hermes_agent.content_factory import (
    _control_transition_checkpoint_clear_keys,
)


def _project(**config):
    return SimpleNamespace(
        id=901,
        product_id=33,
        product_name="NOVA Evening Tea",
        product_brief="",
        config_json={
            "content_mode": "product",
            "product_required": True,
            "video_model": "omni_flash",
            **config,
        },
        state_json={},
    )


def test_targeted_plan_repair_keeps_paid_visual_sibling_checkpoint():
    assert _control_transition_checkpoint_clear_keys(
        production_plan_external_repair=True,
    ) == ("pending_visual_api_resume",)
    assert "pending_visual_partial_repair" in (
        _control_transition_checkpoint_clear_keys(
            production_plan_external_repair=False,
        )
    )


def test_live_content_factory_has_no_retired_creative_stage_or_single_duration_fallback():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "app/services/hermes_agent/content_factory.py",
        root / "app/services/hermes_agent/direct_browser.py",
        root / "app/tasks/hermes_agent/content_factory_tasks.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert '"CREATIVE"' not in combined
    assert "CONTENT_LEGACY_CREATIVE_REMOVED" not in combined
    assert "RETIRED_CONTENT_FACTORY_STAGES" not in combined
    assert 'config.get("video_duration_seconds")' not in combined


def test_conversion_points_come_only_from_confirmed_project_fields():
    project = _project(
        confirmed_selling_points="Caffeine-free; blueberry flavor",
        confirmed_claims="No guaranteed outcomes",
    )

    assert _product_conversion_points(project, "en-US") == [
        "Caffeine-free",
        "blueberry flavor",
    ]


def test_product_name_does_not_trigger_campaign_specific_claim_fallback():
    project = _project()

    assert _product_conversion_points(project, "en-US") == []


def test_hashtags_use_publishing_profile_and_current_brand():
    project = _project(
        brand_name="NOVA",
        publishing_profile={"hashtags": ["EveningTea", "#WindDown"]},
    )

    assert _editor_guidance_hashtag_list(project, "en-US") == [
        "#NOVA",
        "#EveningTea",
        "#WindDown",
    ]


def test_price_without_configured_storefront_action_does_not_invent_one():
    assert _promotion_line_for_speech("NOVA Evening Tea is $12.50", "en-US") == (
        "NOVA Evening Tea is $12.50."
    )


def test_update_schema_accepts_structured_copy_and_publishing_profiles():
    payload = ContentFactoryProjectUpdate(
        title="NOVA launch",
        publishing_profile={
            "platform": "short_video",
            "hashtags": ["NOVA", "EveningTea"],
        },
        creative_copy_contract={
            "product_reveal_segment": 3,
            "conversion_segment": 3,
            "tieback_source_segments": [1],
            "require_causal_product_bridge": True,
        },
        creative_cast_policy={
            "allow_minor_story_characters": True,
            "minimum_product_actor_age": 25,
            "max_spoken_voices": 1,
        },
        product_presentation_policy={
            "authority_mode": "uploaded_source_only",
            "forbidden_interaction_categories": [
                "consume_product",
                "minor_product_interaction",
            ],
        },
    )

    assert payload.publishing_profile.hashtags == ["NOVA", "EveningTea"]
    assert payload.creative_copy_contract.product_reveal_segment == 3
    assert payload.creative_cast_policy.allow_minor_story_characters is True
    assert payload.product_presentation_policy.authority_mode == (
        "uploaded_source_only"
    )


def test_update_schema_accepts_board_generation_and_model_fallback_chain():
    payload = ContentFactoryProjectUpdate(
        title="Board-rendered launch",
        visual_reference_generation_mode="board",
        visual_image_model_chain=[
            "gpt-image-2.0",
            "nano_banana_pro",
        ],
    )

    assert payload.visual_reference_generation_mode == "board"
    assert payload.visual_image_model_chain == [
        "gpt-image-2.0",
        "nano_banana_pro",
    ]


def test_cast_and_product_presentation_rules_are_project_owned():
    project = _project(
        creative_cast_policy={
            "allow_minor_story_characters": True,
            "minimum_product_actor_age": 25,
            "max_spoken_voices": 1,
            "instructions": ["A child may appear only in the story beats."],
        },
        product_presentation_policy={
            "authority_mode": "uploaded_source_only",
            "forbidden_interaction_categories": ["consume_product"],
            "presentation_instructions": [
                "Place the authoritative package on the configured surface.",
            ],
        },
    )

    assert _creative_cast_policy(project) == {
        "allow_minor_story_characters": True,
        "minimum_product_actor_age": 25,
        "max_spoken_voices": 1,
        "instructions": ["A child may appear only in the story beats."],
    }
    assert _product_presentation_policy(project) == {
        "authority_mode": "uploaded_source_only",
        "forbidden_interaction_categories": ["consume_product"],
        "presentation_instructions": [
            "Place the authoritative package on the configured surface.",
        ],
    }


def test_copy_contract_uses_project_configured_tieback_sources():
    project = _project(
        video_duration_min_seconds=30,
        video_duration_max_seconds=30,
        creative_copy_contract={
            "product_reveal_segment": 3,
            "conversion_segment": 3,
            "tieback_source_segments": [1],
        },
    )

    contract = _creative_copy_contract(
        project,
        {
            "confirmed_promotions": "Current price is $12.50",
            "promotion_cta": "Find NOVA Evening Tea for $12.50 in the configured storefront.",
        },
    )

    assert contract["product_reveal_segment"] == 3
    assert contract["conversion_segment"] == 3
    assert contract["tieback_source_segments"] == [1]
