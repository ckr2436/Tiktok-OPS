from __future__ import annotations

from app.services.hermes_agent.content_director import (
    build_directed_content_artifact,
)
from app.services.hermes_agent.content_intent import (
    CreativeIntentManifest,
    CreativeIntentRequirement,
    RequirementExecutionMapping,
    sign_creative_intent_manifest,
)
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
    assert {
        row["storyboard_group_id"]
        for row in compiled["visual_job_ticket"]["reference_plan"]
    } == {plan.plan_id}


def test_compiler_carries_user_requirement_evidence_through_every_authority():
    base_artifact = _artifact()
    requirement = CreativeIntentRequirement(
        requirement_id="R-001",
        kind="reference_transfer",
        priority="critical",
        scope="time_window",
        start_seconds=0,
        end_seconds=3,
        intent="Open with an immediate visual interruption that stops scrolling.",
        evidence_quote="The first three seconds must keep the hook strength.",
        interpretation=(
            "The opening must create a specific visual contradiction before the "
            "viewer can infer the setup."
        ),
        observable_checks=[
            "A visually contradictory event is already readable before second 3.",
            "The opening is newly authored and does not reproduce benchmark shots.",
        ],
        creative_freedom=["Choose a new setting and interruption mechanism."],
        must_not_reuse=["benchmark composition", "benchmark wording"],
    )
    manifest = sign_creative_intent_manifest(
        CreativeIntentManifest(
            objective="Create an original fast-opening short-form product story.",
            requirements=[requirement],
        )
    )
    director_mapping = RequirementExecutionMapping(
        requirement_id="R-001",
        implementation=(
            "Make the notification trail visibly invade the home on the first beat."
        ),
        script_line_ids=["l1"],
        capability_node_ids=["copy"],
        segment_indices=[1],
        evidence_plan=[
            "The first beat shows the contradiction and the first line names it."
        ],
    )
    program = base_artifact.program.model_copy(
        update={
            "intent_manifest_sha256": manifest.manifest_sha256,
            "intent_requirements": list(manifest.requirements),
            "requirement_execution": [director_mapping],
        }
    )
    artifact = build_directed_content_artifact(
        artifact_id=base_artifact.artifact_id,
        revision=base_artifact.revision,
        parent_artifact_sha256=base_artifact.parent_artifact_sha256,
        program=program,
        script=base_artifact.script,
    )

    payload = _author_plan_payload(_spoken_plan(artifact))
    payload["visual"]["beats"][0]["requirement_ids"] = ["R-001"]
    payload["requirement_execution"] = [{
        "requirement_id": "R-001",
        "beat_ids": ["recognition"],
        "reference_ids": ["character-design"],
        "audio_cue_ids": [],
        "line_ids": ["l1"],
        "implementation_evidence": [
            "The signed recognition beat carries R-001 and begins at zero seconds."
        ],
    }]
    plan = finalize_director_production_plan_author_draft(
        DirectorProductionPlanAuthorDraft.model_validate(payload),
        artifact,
        plan_id="plan.intent-lineage",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    video = compile_production_plan_to_video_result(
        artifact,
        plan,
        variant_index=1,
        resolution="720p",
        language_label="English (US)",
    )["videos"][0]
    first_segment = video["segments"][0]
    first_beat = first_segment["timeline"][0]
    assert first_beat["environment"]
    assert first_beat["subject_action"]
    assert first_beat["motion_and_transition"]
    assert first_beat["motion_and_transition"] in first_beat["action"]
    assert first_segment["requirement_ids"] == ["R-001"]
    assert first_segment["requirement_contract"][0]["evidence_quote"] == (
        "The first three seconds must keep the hook strength."
    )
    assert first_segment["requirement_contract"][0][
        "director_implementation"
    ].startswith("Make the notification trail")
    assert first_segment["requirement_contract"][0][
        "production_implementation_evidence"
    ] == [
        "The signed recognition beat carries R-001 and begins at zero seconds."
    ]
    assert video["intent_manifest_sha256"] == manifest.manifest_sha256


def test_unbound_compiler_preserves_multimodal_director_purpose_text():
    artifact = _artifact()
    product_line = artifact.script.lines[-1].model_copy(update={
        "text": "MYUPONA is part of that moment.",
        "purpose": "Position MYUPONA as part of the protected moment.",
    })
    artifact = artifact.model_copy(update={
        "script": artifact.script.model_copy(update={
            "lines": [*artifact.script.lines[:-1], product_line],
        }),
    })
    payload = _author_plan_payload(_spoken_plan(artifact))
    plan = finalize_director_production_plan_author_draft(
        DirectorProductionPlanAuthorDraft.model_validate(payload),
        artifact,
        plan_id="plan.unbound-copy",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    compiled = compile_production_plan_for_media(
        artifact,
        plan,
        asset_registry={"asset:product:front": {"asset_id": 1}},
        product_allowed=False,
    )
    video = compile_production_plan_to_video_result(
        artifact,
        plan,
        variant_index=1,
        resolution="720p",
        language_label="English (US)",
        product_allowed=False,
    )["videos"][0]

    compiled_lines = [
        line["line"]
        for segment in compiled["complete_video_script"]["segments"]
        for line in segment["dialogue_lines"]
    ]
    assert "MYUPONA is part of that moment." in compiled_lines
    assert any(
        "Position MYUPONA" in segment["story_function"]
        for segment in compiled["shot_plan"]
    )
    assert any("Position MYUPONA" in segment["prompt"] for segment in video["segments"])
    assert any("Position MYUPONA" in segment["segment_goal"] for segment in video["segments"])
    assert any(
        "Position MYUPONA" in segment["segment_goal"]
        for segment in video["segments"]
    )


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
