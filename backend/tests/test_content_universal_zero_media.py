from __future__ import annotations

import pytest

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
from app.services.hermes_agent.content_production_compiler import (
    compile_production_plan_to_video_result,
)
from app.services.hermes_agent.content_production_plan import (
    AudioCue,
    AudioProgramAuthorDraft,
    AuthoritativeProductComposite,
    CopyDeliveryProgramAuthorDraft,
    DirectorProductionPlanAuthorDraft,
    OverlayPresentation,
    ScriptDeliveryIntent,
    SpeakerVoiceIntent,
    VisualBeat,
    VisualProgramAuthorDraft,
    VisualReferenceIntent,
    finalize_director_production_plan_author_draft,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _compact_provider_segment_prompt,
)


CASES = (
    {
        "name": "product_conversion",
        "content_type": "adult animated product conversion story",
        "objective": "Make one painful situation lead naturally to a purchase decision.",
        "audio_mode": "spoken",
        "product_required": True,
        "product_segments": {2},
        "lines": (
            "She stopped joining breakfast because every morning began exhausted.",
            "MYUPONA is a melatonin-free night-routine option at $7.99 in the yellow cart.",
        ),
    },
    {
        "name": "non_product_tutorial",
        "content_type": "screen tutorial",
        "objective": "Teach one repeatable phone-editing action.",
        "audio_mode": "spoken",
        "product_required": False,
        "product_segments": set(),
        "lines": (
            "Open the timeline and split exactly where the hand touches the frame.",
            "Remove the pause, then replay once to confirm the motion stays continuous.",
        ),
    },
    {
        "name": "documentary_story",
        "content_type": "micro documentary",
        "objective": "Tell one complete observational human story.",
        "audio_mode": "spoken",
        "product_required": False,
        "product_segments": set(),
        "lines": (
            "For thirty years, the last light in the block belonged to Mr. Chen's bakery.",
            "On closing night, the neighbors arrived before he could turn it off alone.",
        ),
    },
    {
        "name": "product_first_comparison_demo",
        "content_type": "product-first comparison demonstration",
        "objective": "Compare two visible routine formats without delaying the product.",
        "audio_mode": "spoken",
        "product_required": True,
        "product_segments": {1, 2},
        "lines": (
            "Here is MYUPONA beside a standard melatonin gummy: the package says melatonin-free.",
            "Choose the format that matches the routine you actually want to keep.",
        ),
    },
    {
        "name": "silent_visual",
        "content_type": "silent visual transformation",
        "objective": "Express one complete visual idea without sound.",
        "audio_mode": "silent",
        "product_required": False,
        "product_segments": set(),
        "lines": (
            "One crowded desk.",
            "One clear working space.",
        ),
    },
)


def _artifact(case: dict):
    spoken = case["audio_mode"] == "spoken"
    conversion = (
        ConversionIntent(
            product_required=True,
            product_name="MYUPONA",
            confirmed_differentiators=["melatonin-free"],
            minimum_differentiators_in_copy=1,
            offer_text="$7.99",
            cta_text="yellow cart",
        )
        if case["product_required"]
        else ConversionIntent(product_required=False)
    )
    program = VideoProgramSpec(
        schema_version="2.0",
        program_id=f"program.{case['name']}",
        objective=case["objective"],
        content_type=case["content_type"],
        platform="TikTok",
        locale="en-US",
        audience="US adults",
        target_duration_seconds=20,
        aspect_ratio="9:16",
        audio_mode=case["audio_mode"],
        conversion=conversion,
        execution_graph=[
            DirectorCapabilityNode(
                node_id="treatment",
                capability=(
                    "copy.write" if spoken else "nonverbal.treatment"
                ),
                input_contract="VideoProgramSpec",
                output_contract="ScriptPackage",
            )
        ],
        copy_review_criteria=[
            CopyReviewCriterion(
                criterion_id="complete",
                instruction="The accepted content must be complete and understandable.",
                minimum_score=80,
            )
        ],
    )
    delivery_mode = "spoken" if spoken else "display"
    speaker_id = "narrator" if spoken else "display"
    lines = [
        ScriptLine(
            line_id=f"line.{index}",
            speaker_id=speaker_id,
            text=text,
            beat_id=f"beat.{index}",
            purpose="opening" if index == 1 else "completion",
            delivery_mode=delivery_mode,
        )
        for index, text in enumerate(case["lines"], 1)
    ]
    script = build_script_package(
        script_id=f"script.{case['name']}",
        program_id=program.program_id,
        locale="en-US",
        target_duration_seconds=20,
        edit_headroom_seconds=1,
        speech_rate_wpm=180,
        display_reading_rate_wpm=120,
        audio_mode=case["audio_mode"],
        primary_speaker_id="narrator" if spoken else None,
        lines=lines,
        segments=[
            ScriptSegmentAllocation(
                segment_index=index,
                duration_seconds=10,
                line_ids=[f"line.{index}"],
            )
            for index in (1, 2)
        ],
    )
    return build_directed_content_artifact(
        artifact_id=f"artifact.{case['name']}",
        revision=1,
        parent_artifact_sha256=None,
        program=program,
        script=script,
    )


def _plan(case: dict, artifact):
    references = [
        VisualReferenceIntent(
            reference_id="scene",
            roles=["scene", "action"],
            purpose="Lock only the scene and key action for this treatment.",
            generation_brief=(
                "Create one reference that follows this program's selected visual form."
            ),
        )
    ]
    if case["product_required"]:
        references.append(
            VisualReferenceIntent(
                reference_id="product",
                roles=["product"],
                purpose="Use the authoritative uploaded package when the plan calls for it.",
                source_asset_refs=["asset:product"],
                generation_brief=(
                    "Create the scripted product interaction with a clear "
                    "placement surface for the exact package."
                ),
                authoritative_product_composite=AuthoritativeProductComposite(
                    placement="lower_center",
                    width_fraction=0.32,
                    entrance="fade",
                ),
            )
        )
    beats = []
    for index in (1, 2):
        reference_ids = ["scene"]
        if index in case["product_segments"]:
            reference_ids.append("product")
        beats.append(
            VisualBeat(
                beat_id=f"beat.{index}",
                start_seconds=(index - 1) * 10,
                end_seconds=index * 10,
                line_ids=[f"line.{index}"],
                purpose="opening" if index == 1 else "completion",
                environment=f"The environment selected for {case['content_type']}.",
                subject_action=f"Perform only the approved beat {index} action.",
                camera_composition=f"Composition selected for beat {index}.",
                motion_and_transition="Use one motivated transition into the next state.",
                continuity_state="Preserve the declared subject and environment.",
                reference_ids=reference_ids,
            )
        )
    spoken = case["audio_mode"] == "spoken"
    draft = DirectorProductionPlanAuthorDraft(
        schema_version="2.0",
        visual=VisualProgramAuthorDraft(
            style_language=f"A form appropriate to {case['content_type']}.",
            visual_grammar="Let the approved meaning determine shot construction.",
            product_presentation_intent=(
                "Show the uploaded package only in declared product beats."
                if case["product_required"]
                else None
            ),
            references=references,
            beats=beats,
        ),
        audio=AudioProgramAuthorDraft(
            voices=(
                [
                    SpeakerVoiceIntent(
                        speaker_id="narrator",
                        identity="One adult US narrator",
                        gender="female",
                        screen_relation="off_screen_narrator",
                        timbre="warm and clear",
                        pitch="mid-range",
                        accent="General American English",
                        delivery_direction="Natural and intelligible",
                        continuity_rule="Keep the same voice across both segments",
                    )
                ]
                if spoken
                else []
            ),
            cues=[
                AudioCue(
                    cue_id="audio",
                    start_seconds=0,
                    end_seconds=20,
                    kind="room_tone" if spoken else "silence",
                    intent=(
                        "Keep narration intelligible."
                        if spoken
                        else "Preserve complete silence."
                    ),
                )
            ],
            mix_intent=(
                "Narration leads the mix."
                if spoken
                else "No audio is present."
            ),
        ),
        copy_delivery=CopyDeliveryProgramAuthorDraft(
            deliveries=[
                ScriptDeliveryIntent(
                    line_id=f"line.{index}",
                    method=(
                        "provider_dialogue" if spoken else "local_overlay"
                    ),
                    speaker_id="narrator" if spoken else None,
                    presentation=(
                        None
                        if spoken
                        else OverlayPresentation(
                            placement=(
                                "center" if index == 1 else "lower_third"
                            ),
                            emphasis="strong",
                            background="box",
                            max_lines=2,
                        )
                    ),
                )
                for index in (1, 2)
            ]
        ),
    )
    product_assets = {"asset:product"} if case["product_required"] else set()
    return finalize_director_production_plan_author_draft(
        draft,
        artifact,
        plan_id=f"plan.{case['name']}",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs=product_assets,
        authoritative_product_asset_refs=product_assets,
    )


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_universal_showrunner_zero_media_compile(case):
    artifact = _artifact(case)
    plan = _plan(case, artifact)

    result = compile_production_plan_to_video_result(
        artifact,
        plan,
        variant_index=1,
        resolution="720p",
        language_label="English (US)",
        reference_image_limit=3,
    )

    video = result["videos"][0]
    segments = video["segments"]
    assert len(segments) == 2
    assert all(
        segment["compile_source"] == "signed_production_plan"
        for segment in segments
    )
    assert video["compiler_authority"]["director_script_sha256"] == (
        artifact.script.canonical_text_sha256
    )
    assert video["compiler_authority"]["production_plan_sha256"] == (
        plan.plan_sha256
    )
    assert [
        line["line"]
        for segment in segments
        for line in (
            segment["dialogue_lines"]
            if case["audio_mode"] == "spoken"
            else segment["display_lines"]
        )
    ] == list(case["lines"])
    assert [
        index
        for index, segment in enumerate(segments, 1)
        if segment["product_anchor_required"]
    ] == sorted(case["product_segments"])
    if case["audio_mode"] == "silent":
        assert all(not segment["dialogue_lines"] for segment in segments)
    else:
        assert all(not segment["display_lines"] for segment in segments)

    provider_prompts = [
        _compact_provider_segment_prompt(
            segment,
            resolution="720p",
            language_label="English (US)",
            requirement_contract=[],
            promotion="Runtime must not append this sentence.",
            product_required=bool(segment["product_anchor_required"]),
            audio_mode=case["audio_mode"],
        )
        for segment in segments
    ]
    assert all(
        "Runtime must not append this sentence." not in prompt
        for prompt in provider_prompts
    )
    if case["audio_mode"] == "spoken":
        assert all(
            expected in prompt
            for expected, prompt in zip(case["lines"], provider_prompts)
        )
    else:
        assert all("completely silent" in prompt for prompt in provider_prompts)
        assert all(
            line not in "\n".join(provider_prompts)
            for line in case["lines"]
        )
