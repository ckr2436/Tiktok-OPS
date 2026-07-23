from __future__ import annotations

from app.services.hermes_agent.content_production_compiler import (
    compile_production_plan_for_media,
    compile_production_plan_to_video_result,
)
from app.services.hermes_agent.content_production_plan import (
    finalize_director_production_plan_author_draft,
    DirectorProductionPlanAuthorDraft,
)

from test_content_production_plan import (
    _artifact,
    _author_plan_payload,
    _silent_plan,
    _spoken_plan,
)


def test_compiler_preserves_script_and_variable_reference_count():
    artifact = _artifact()
    payload = _author_plan_payload(_spoken_plan(artifact))
    payload["visual"]["references"].append({
        "reference_id": "ref.extra",
        "roles": ["scene"],
        "purpose": "Preserve the end location.",
        "source_asset_refs": [],
        "generation_brief": "One quiet end-location continuity frame.",
    })
    payload["visual"]["beats"][-1]["reference_ids"].append("ref.extra")
    plan = finalize_director_production_plan_author_draft(
        DirectorProductionPlanAuthorDraft.model_validate(payload),
        artifact,
        plan_id="plan.compiler",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    compiled = compile_production_plan_for_media(
        artifact,
        plan,
        asset_registry={"asset:product:front": {"asset_id": 1}},
    )

    assert compiled["visual_job_ticket"]["reference_image_count"] == 3
    expected = [line.text for line in artifact.script.lines]
    actual = [
        line["line"]
        for segment in compiled["complete_video_script"]["segments"]
        for line in segment["dialogue_lines"]
    ]
    assert actual == expected
    assert compiled["director_lock"]["script_sha256"] == (
        artifact.script.canonical_text_sha256
    )
    voice = compiled["voice_bible"]["speakers"][0]
    assert voice["gender"] == "female"
    assert voice["screen_relation"] == "off_screen_narrator"
    assert voice["timbre"] == "warm and grounded"
    assert compiled["production_plan_lock"]["plan_sha256"] == plan.plan_sha256


def test_finalizer_rejects_source_only_reference_before_media_spend():
    artifact = _artifact()
    payload = _author_plan_payload(_spoken_plan(artifact))
    payload["visual"]["references"][0]["generation_brief"] = None
    payload["visual"]["references"][0]["source_asset_refs"] = [
        "asset:product:front"
    ]
    try:
        finalize_director_production_plan_author_draft(
            DirectorProductionPlanAuthorDraft.model_validate(payload),
            artifact,
            plan_id="plan.source-only",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )
    except ValueError as exc:
        assert "every visual reference requires a generation brief" in str(exc)
    else:
        raise AssertionError("source-only Production Plan was accepted")


def test_segment_compiler_preserves_exact_copy_and_plan_owned_product_beats():
    artifact = _artifact()
    plan = finalize_director_production_plan_author_draft(
        DirectorProductionPlanAuthorDraft.model_validate(
            _author_plan_payload(_spoken_plan(artifact))
        ),
        artifact,
        plan_id="plan.segment-compiler",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    result = compile_production_plan_to_video_result(
        artifact,
        plan,
        variant_index=7,
        resolution="720p",
        language_label="English (US)",
    )
    assert all(
        segment["visual_style"] == plan.visual.style_language
        for segment in result["videos"][0]["segments"]
    )
    assert result["videos"][0]["visual_style"] == plan.visual.style_language

    video = result["videos"][0]
    assert video["segments"][0]["voice_lock"][0]["gender"] == "female"
    assert video["segments"][0]["voice_lock"][0][
        "screen_relation"
    ] == "off_screen_narrator"
    assert video["variant_index"] == 7
    assert video["compiler_authority"]["production_plan_sha256"] == (
        plan.plan_sha256
    )
    assert [
        line["line"]
        for segment in video["segments"]
        for line in segment["dialogue_lines"]
    ] == [line.text for line in artifact.script.lines]
    assert [
        segment["product_anchor_required"]
        for segment in video["segments"]
    ] == [False, True, True, True]
    assert all(
        0 <= beat["start_seconds"] < beat["end_seconds"] <= 10
        for segment in video["segments"]
        for beat in segment["timeline"]
    )


def test_compiler_exposes_director_owned_independent_segment_dependencies():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    independent_beats = [
        beat.model_copy(update={"continuity_dependency": "independent"})
        for beat in draft.visual.beats
    ]
    draft = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={"beats": independent_beats}),
    })
    plan = finalize_director_production_plan_author_draft(
        DirectorProductionPlanAuthorDraft.model_validate(
            _author_plan_payload(draft)
        ),
        artifact,
        plan_id="plan.independent-segments",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    result = compile_production_plan_to_video_result(
        artifact,
        plan,
        variant_index=1,
        resolution="720p",
        language_label="English (US)",
    )

    assert {
        segment["continuity_dependency"]
        for segment in result["videos"][0]["segments"]
    } == {"independent"}


def test_segment_compiler_keeps_silent_display_copy_out_of_dialogue():
    artifact = _artifact(silent=True)
    plan = finalize_director_production_plan_author_draft(
        DirectorProductionPlanAuthorDraft.model_validate(
            _author_plan_payload(_silent_plan(artifact))
        ),
        artifact,
        plan_id="plan.silent-segment-compiler",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs=set(),
        authoritative_product_asset_refs=set(),
    )

    result = compile_production_plan_to_video_result(
        artifact,
        plan,
        variant_index=2,
        resolution="720p",
        language_label="English (US)",
    )

    segments = result["videos"][0]["segments"]
    assert all(segment["dialogue_lines"] == [] for segment in segments)
    assert [
        line["line"]
        for segment in segments
        for line in segment["display_lines"]
    ] == [line.text for line in artifact.script.lines]
    assert all(not segment["product_anchor_required"] for segment in segments)
