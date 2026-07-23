from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

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
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
)
from app.services.hermes_agent.content_director_profile import (
    load_universal_director_profile,
)
from app.services.hermes_agent.content_production_plan import (
    AudioCue,
    AudioProgramDraft,
    AuthoritativeProductComposite,
    CopyDeliveryProgramDraft,
    DirectedProductionPlan,
    DirectorProductionPlanAuthorDraft,
    DirectorProductionPlanDraft,
    OverlayPresentation,
    ProductionPlanReviewCriterion,
    SpeakerVoiceIntent,
    TimedScriptDelivery,
    VisualBeat,
    VisualProgramDraft,
    VisualReferenceIntent,
    build_director_production_plan_packet,
    finalize_director_production_plan,
    finalize_director_production_plan_author_draft,
)
from app.services.hermes_agent.content_production_plan_runtime import (
    _generated_reference_text_dependency_details,
    _generated_reference_text_dependencies,
    run_content_production_plan_loop,
)


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create_response(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        text = response if isinstance(response, str) else json.dumps(response)
        return {"output_text": text}, 7


def _plan_criteria() -> list[ProductionPlanReviewCriterion]:
    return [
        ProductionPlanReviewCriterion(
            criterion_id="visual_copy_alignment",
            instruction="Visual meaning must express the locked copy.",
            minimum_score=85,
        ),
        ProductionPlanReviewCriterion(
            criterion_id="continuity_and_delivery",
            instruction="Visual, voice, and delivery continuity must hold.",
            minimum_score=85,
        ),
    ]


def _approved_plan_verdict() -> dict:
    return {
        "approved": True,
        "scores": {
            "visual_copy_alignment": 95,
            "continuity_and_delivery": 94,
        },
        "blocking_issues": [],
        "repair_scope": "plan_only",
    }


def _rejected_plan_verdict(*, repair_scope: str = "plan_only") -> dict:
    return {
        "approved": False,
        "scores": {
            "visual_copy_alignment": 80,
            "continuity_and_delivery": 92,
        },
        "blocking_issues": [
            {
                "code": "VISUAL_ACTION_TOO_ABSTRACT",
                "beat_ids": ["recognition"],
                "line_ids": ["l1"],
                "reference_ids": ["character-design"],
                "evidence": "The opening action does not yet show the line.",
                "repair_instruction": (
                    "Make the cited opening action visibly carry the inbox."
                ),
            }
        ],
        "repair_scope": repair_scope,
    }


def _artifact(*, silent: bool = False):
    program = VideoProgramSpec(
        schema_version="2.0",
        program_id="program-silent" if silent else "program-spoken",
        objective=(
            "Explain one idea without dialogue."
            if silent
            else "Tell one complete product story."
        ),
        content_type=(
            "silent animated explainer"
            if silent
            else "animated product story"
        ),
        platform="TikTok",
        locale="en-US",
        audience="US adults.",
        target_duration_seconds=20 if silent else 40,
        aspect_ratio="9:16",
        audio_mode="silent" if silent else "spoken",
        conversion=(
            ConversionIntent(product_required=False)
            if silent
            else ConversionIntent(
                product_required=True,
                product_name="Example Product",
                confirmed_differentiators=["Confirmed format"],
                minimum_differentiators_in_copy=1,
                offer_text="$7.99",
                cta_text="yellow cart",
            )
        ),
        execution_graph=[
            DirectorCapabilityNode(
                node_id="copy",
                capability="copy.write",
                input_contract="VideoProgramSpec",
                output_contract="ScriptPackage",
            ),
        ],
        copy_review_criteria=[
            CopyReviewCriterion(
                criterion_id="complete",
                instruction="The complete video is understandable.",
                minimum_score=80,
            ),
        ],
    )
    if silent:
        lines = [
            ScriptLine(
                line_id="d1",
                speaker_id="display",
                text="One clear idea.",
                beat_id="idea",
                purpose="explain",
                delivery_mode="display",
            ),
            ScriptLine(
                line_id="d2",
                speaker_id="display",
                text="One visible conclusion.",
                beat_id="conclusion",
                purpose="complete",
                delivery_mode="display",
            ),
        ]
        segments = [
            ScriptSegmentAllocation(
                segment_index=1,
                duration_seconds=10,
                line_ids=["d1"],
            ),
            ScriptSegmentAllocation(
                segment_index=2,
                duration_seconds=10,
                line_ids=["d2"],
            ),
        ]
    else:
        lines = [
            ScriptLine(
                line_id="l1",
                speaker_id="narrator",
                text="The inbox followed her home.",
                beat_id="recognition",
                purpose="recognition",
            ),
            ScriptLine(
                line_id="l2",
                speaker_id="narrator",
                text="She finally closed it.",
                beat_id="decision",
                purpose="human decision",
            ),
            ScriptLine(
                line_id="l3",
                speaker_id="narrator",
                text="The confirmed format fit that small routine.",
                beat_id="bridge",
                purpose="product relevance",
            ),
            ScriptLine(
                line_id="l4",
                speaker_id="narrator",
                text="It is $7.99 in the yellow cart.",
                beat_id="action",
                purpose="offer and action",
            ),
        ]
        segments = [
            ScriptSegmentAllocation(
                segment_index=index,
                duration_seconds=10,
                line_ids=[f"l{index}"],
            )
            for index in range(1, 5)
        ]
    script = build_script_package(
        script_id=f"script-{program.program_id}",
        program_id=program.program_id,
        locale="en-US",
        target_duration_seconds=program.target_duration_seconds,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        display_reading_rate_wpm=120,
        audio_mode="silent" if silent else "spoken",
        primary_speaker_id=None if silent else "narrator",
        lines=lines,
        segments=segments,
    )
    return build_directed_content_artifact(
        artifact_id=f"artifact-{program.program_id}",
        revision=1,
        parent_artifact_sha256=None,
        program=program,
        script=script,
    )


def _spoken_plan(artifact) -> DirectorProductionPlanDraft:
    return DirectorProductionPlanDraft(
        visual=VisualProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=40,
            aspect_ratio="9:16",
            style_language="Adult 2D animation with restrained expressions.",
            visual_grammar="Three meaning beats with changing composition.",
            product_presentation_intent=(
                "Place the authoritative package naturally in the decision."
            ),
            references=[
                VisualReferenceIntent(
                    reference_id="product-anchor",
                    roles=["product"],
                    purpose="Preserve the exact uploaded package.",
                    source_asset_refs=["asset:product:front"],
                    generation_brief=(
                        "Create the scripted product-interaction composition "
                        "with a clear placement surface for the exact package."
                    ),
                    authoritative_product_composite=(
                        AuthoritativeProductComposite(
                            placement="lower_center",
                            width_fraction=0.32,
                            entrance="fade",
                        )
                    ),
                ),
                VisualReferenceIntent(
                    reference_id="character-design",
                    roles=["character", "scene"],
                    purpose="Keep one adult character and room consistent.",
                    generation_brief="Create one reusable adult character and room.",
                ),
            ],
            beats=[
                VisualBeat(
                    beat_id="recognition",
                    start_seconds=0,
                    end_seconds=12,
                    line_ids=["l1"],
                    purpose="Make the opening situation recognizable.",
                    environment="A quiet apartment after work.",
                    subject_action="Notifications trail the character indoors.",
                    camera_composition="Wide view compressing around the character.",
                    motion_and_transition="Notifications collect, then the frame settles.",
                    continuity_state="Same character, clothes, room, and evening.",
                    reference_ids=["character-design"],
                ),
                VisualBeat(
                    beat_id="decision-and-bridge",
                    start_seconds=12,
                    end_seconds=28,
                    line_ids=["l2", "l3"],
                    purpose="Show the human decision before product relevance.",
                    environment="The same apartment and desk.",
                    subject_action="She closes the inbox, then reaches for the package.",
                    camera_composition="Medium action view followed by product insert.",
                    motion_and_transition="One continuous reach motivates the insert.",
                    continuity_state="Preserve hand, desk, package, and screen position.",
                    reference_ids=["character-design", "product-anchor"],
                ),
                VisualBeat(
                    beat_id="offer-and-action",
                    start_seconds=28,
                    end_seconds=40,
                    line_ids=["l4"],
                    purpose="Complete the same decision with a readable action.",
                    environment="The room is quieter but remains the same room.",
                    subject_action="The package rests beside the closed laptop.",
                    camera_composition="Stable product-and-context composition.",
                    motion_and_transition="Slow settle into the final state.",
                    continuity_state="No package redesign or scene reset.",
                    reference_ids=["character-design", "product-anchor"],
                ),
            ],
        ),
        audio=AudioProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=40,
            audio_mode="spoken",
            voices=[
                SpeakerVoiceIntent(
                    speaker_id="narrator",
                    identity="One adult US narrator.",
                    gender="female",
                    screen_relation="off_screen_narrator",
                    timbre="warm and grounded",
                    pitch="medium-low",
                    accent="US English",
                    delivery_direction="Natural, direct, and unhurried.",
                    continuity_rule="Keep one voice across all lines.",
                ),
            ],
            cues=[
                AudioCue(
                    cue_id="bed",
                    start_seconds=0,
                    end_seconds=40,
                    kind="room_tone",
                    intent="Subtle apartment ambience under clear narration.",
                ),
            ],
            mix_intent="Narration remains intelligible above restrained ambience.",
        ),
        copy_delivery=CopyDeliveryProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=40,
            deliveries=[
                TimedScriptDelivery(
                    line_id="l1",
                    start_seconds=0,
                    end_seconds=7,
                    method="local_voiceover",
                    speaker_id="narrator",
                ),
                TimedScriptDelivery(
                    line_id="l2",
                    start_seconds=12,
                    end_seconds=17,
                    method="local_voiceover",
                    speaker_id="narrator",
                ),
                TimedScriptDelivery(
                    line_id="l3",
                    start_seconds=18,
                    end_seconds=25,
                    method="local_voiceover",
                    speaker_id="narrator",
                ),
                TimedScriptDelivery(
                    line_id="l4",
                    start_seconds=30,
                    end_seconds=37,
                    method="local_voiceover",
                    speaker_id="narrator",
                ),
            ],
        ),
    )


def _silent_plan(artifact) -> DirectorProductionPlanDraft:
    return DirectorProductionPlanDraft(
        visual=VisualProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=20,
            aspect_ratio="9:16",
            style_language="Simple diagram animation.",
            visual_grammar="One continuous transformation.",
            references=[
                VisualReferenceIntent(
                    reference_id="diagram",
                    roles=["scene", "action"],
                    purpose="Show one transformation clearly.",
                    generation_brief="Create a simple reusable diagram scene.",
                ),
            ],
            beats=[
                VisualBeat(
                    beat_id="complete-explanation",
                    start_seconds=0,
                    end_seconds=20,
                    line_ids=["d1", "d2"],
                    purpose="Explain and conclude one idea.",
                    environment="A clean diagram field.",
                    subject_action="One object changes state once.",
                    camera_composition="Fixed legible composition.",
                    motion_and_transition="Continuous motion without cuts.",
                    continuity_state="Preserve scale and labels.",
                    reference_ids=["diagram"],
                ),
            ],
        ),
        audio=AudioProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=20,
            audio_mode="silent",
            voices=[],
            cues=[
                AudioCue(
                    cue_id="silence",
                    start_seconds=0,
                    end_seconds=20,
                    kind="silence",
                    intent="No audio is required.",
                ),
            ],
            mix_intent="Preserve silence.",
        ),
        copy_delivery=CopyDeliveryProgramDraft(
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=20,
            deliveries=[
                TimedScriptDelivery(
                    line_id="d1",
                    start_seconds=1,
                    end_seconds=7,
                    method="local_overlay",
                    presentation=OverlayPresentation(
                        placement="center",
                        emphasis="strong",
                        background="box",
                        max_lines=2,
                    ),
                ),
                TimedScriptDelivery(
                    line_id="d2",
                    start_seconds=11,
                    end_seconds=18,
                    method="local_overlay",
                    presentation=OverlayPresentation(
                        placement="lower_third",
                        emphasis="standard",
                        background="box",
                        max_lines=2,
                    ),
                ),
            ],
        ),
    )


@pytest.mark.parametrize(
    "action",
    [
        "A sealed bottle pours out two product units onto the desk.",
        "未开封的瓶保持关闭，却从瓶中倒出两份产品颗粒。",
    ],
)
def test_plan_rejects_impossible_sealed_package_action_before_media_spend(action):
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    beats = list(draft.visual.beats)
    beats[-1] = beats[-1].model_copy(update={
        "subject_action": action,
    })
    broken = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={"beats": beats}),
    })

    with pytest.raises(ValueError, match="impossible package-state action"):
        finalize_director_production_plan_author_draft(
            DirectorProductionPlanAuthorDraft.model_validate(
                _author_plan_payload(broken)
            ),
            artifact,
            plan_id="plan.impossible-package-state",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )


def _author_plan_payload(
    plan: DirectorProductionPlanDraft,
) -> dict:
    excluded = {
        "schema_version",
        "program_id",
        "director_artifact_sha256",
        "target_duration_seconds",
    }
    payload = {
        "schema_version": "2.0",
        "visual": plan.visual.model_dump(
            mode="json",
            exclude={*excluded, "aspect_ratio"},
        ),
        "audio": plan.audio.model_dump(
            mode="json",
            exclude={*excluded, "audio_mode"},
        ),
        "copy_delivery": plan.copy_delivery.model_dump(
            mode="json",
            exclude=excluded,
        ),
    }
    for delivery in payload["copy_delivery"]["deliveries"]:
        delivery.pop("start_seconds", None)
        delivery.pop("end_seconds", None)
    return payload


def test_visual_beats_are_not_provider_transport_segments():
    artifact = _artifact()
    plan = finalize_director_production_plan(
        _spoken_plan(artifact),
        artifact,
        plan_id="plan-spoken",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert len(artifact.script.segments) == 4
    assert len(plan.visual.beats) == 3
    assert plan.copy_delivery.deliveries[-1].line_id == "l4"
    assert DirectedProductionPlan.model_validate(
        plan.model_dump(mode="json")
    ).plan_sha256 == plan.plan_sha256


def test_silent_video_uses_local_display_copy_without_voice():
    artifact = _artifact(silent=True)
    plan = finalize_director_production_plan(
        _silent_plan(artifact),
        artifact,
        plan_id="plan-silent",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs=set(),
        authoritative_product_asset_refs=set(),
    )

    assert plan.audio.audio_mode == "silent"
    assert plan.audio.voices == []
    assert {item.method for item in plan.copy_delivery.deliveries} == {
        "local_overlay"
    }


def test_silent_video_rejects_audible_sound_cues():
    artifact = _artifact(silent=True)
    draft = _silent_plan(artifact)
    damaged = draft.model_copy(update={
        "audio": draft.audio.model_copy(update={
            "cues": [
                AudioCue(
                    cue_id="audible-room",
                    start_seconds=0,
                    end_seconds=20,
                    kind="room_tone",
                    intent="This is audible and contradicts silent mode.",
                )
            ]
        })
    })

    with pytest.raises(ValueError, match="silent audio mode"):
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-silent-audio-violation",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs=set(),
            authoritative_product_asset_refs=set(),
        )


def test_production_plan_rejects_dropped_script_line():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    damaged_beats = [beat.model_copy(deep=True) for beat in draft.visual.beats]
    damaged_beats[-1] = damaged_beats[-1].model_copy(
        update={"line_ids": []}
    )
    damaged = draft.model_copy(
        update={
            "visual": draft.visual.model_copy(
                update={"beats": damaged_beats}
            )
        }
    )

    with pytest.raises(ValueError, match="every approved script line"):
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-dropped-line",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )


def test_production_plan_accepts_director_spoken_pacing_tolerance():
    base = _artifact()
    lines = [line.model_copy(deep=True) for line in base.script.lines]
    lines[2] = lines[2].model_copy(update={
        "text": (
            "After closing my laptop, I put one simple melatonin free gummy "
            "by my bed so my routine stays small and does not become another "
            "task tonight."
        )
    })
    script = build_script_package(
        script_id=base.script.script_id,
        program_id=base.script.program_id,
        locale=base.script.locale,
        target_duration_seconds=base.script.target_duration_seconds,
        edit_headroom_seconds=base.script.edit_headroom_seconds,
        speech_rate_wpm=base.script.speech_rate_wpm,
        display_reading_rate_wpm=base.script.display_reading_rate_wpm,
        audio_mode=base.script.audio_mode,
        primary_speaker_id=base.script.primary_speaker_id,
        lines=lines,
        segments=base.script.segments,
    )
    artifact = build_directed_content_artifact(
        artifact_id=base.artifact_id,
        revision=base.revision,
        parent_artifact_sha256=base.parent_artifact_sha256,
        program=base.program,
        script=script,
    )

    packet = build_director_production_plan_packet(
        artifact,
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
    )

    segment_three = [
        delivery for delivery in packet["line_delivery_contract"]
        if delivery["line_id"] == "l3"
    ][0]
    assert segment_three["minimum_delivery_seconds"] == 9.9
    assert segment_three["runtime_compiled_interval_seconds"] == {
        "start_seconds": 20.0,
        "end_seconds": 30.0,
    }


def test_production_plan_rejects_non_authoritative_product_reference():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    references = [
        item.model_copy(
            update={"source_asset_refs": ["asset:character:one"]}
        )
        if "product" in item.roles
        else item
        for item in draft.visual.references
    ]
    damaged = draft.model_copy(
        update={
            "visual": draft.visual.model_copy(
                update={"references": references}
            )
        }
    )

    with pytest.raises(ValueError, match="authoritative product assets"):
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-product-authority",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={
                "asset:product:front",
                "asset:character:one",
            },
            authoritative_product_asset_refs={"asset:product:front"},
        )


def test_production_plan_rejects_uncited_visual_reference():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    unused_reference = VisualReferenceIntent(
        reference_id="unused-bedside-scene",
        roles=["scene"],
        purpose="A bedside scene that no timed beat actually uses.",
        generation_brief="Create an unused bedside scene.",
    )
    damaged = draft.model_copy(
        update={
            "visual": draft.visual.model_copy(
                update={
                    "references": [
                        *draft.visual.references,
                        unused_reference,
                    ]
                }
            )
        }
    )

    with pytest.raises(ValueError, match="every visual reference must be cited"):
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-uncited-reference",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )


def test_production_plan_rejects_product_reference_before_reveal_boundary():
    base_artifact = _artifact()
    program = base_artifact.program.model_copy(
        update={
            "conversion": base_artifact.program.conversion.model_copy(
                update={"reveal_after_fraction": 0.25}
            )
        }
    )
    artifact = build_directed_content_artifact(
        artifact_id=base_artifact.artifact_id,
        revision=base_artifact.revision,
        parent_artifact_sha256=base_artifact.parent_artifact_sha256,
        program=program,
        script=base_artifact.script,
    )
    draft = _spoken_plan(artifact)
    product_reference = next(
        item
        for item in draft.visual.references
        if "product" in item.roles
    )
    beats = [item.model_copy(deep=True) for item in draft.visual.beats]
    beats[0] = beats[0].model_copy(
        update={
            "reference_ids": [
                *beats[0].reference_ids,
                product_reference.reference_id,
            ]
        }
    )
    damaged = draft.model_copy(
        update={
            "visual": draft.visual.model_copy(
                update={"beats": beats}
            )
        }
    )

    with pytest.raises(
        ValueError,
        match="product visual references cannot be attached before",
    ):
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-early-product-reference",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )


def test_display_copy_cannot_be_delegated_to_provider_dialogue():
    artifact = _artifact(silent=True)
    draft = _silent_plan(artifact)
    deliveries = [item.model_copy(deep=True) for item in draft.copy_delivery.deliveries]
    deliveries[0] = deliveries[0].model_copy(
        update={
            "method": "provider_dialogue",
            "speaker_id": "display",
        }
    )
    damaged = draft.model_copy(
        update={
            "copy_delivery": draft.copy_delivery.model_copy(
                update={"deliveries": deliveries}
            )
        }
    )

    with pytest.raises(ValueError, match="deterministic local overlay"):
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-display-delivery",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs=set(),
            authoritative_product_asset_refs=set(),
        )


def test_production_plan_reports_visual_and_delivery_errors_together():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    beats = [beat.model_copy(deep=True) for beat in draft.visual.beats]
    beats[-1] = beats[-1].model_copy(update={"line_ids": []})
    deliveries = [
        delivery.model_copy(deep=True)
        for delivery in draft.copy_delivery.deliveries
    ]
    deliveries[0] = deliveries[0].model_copy(
        update={"end_seconds": 0.5}
    )
    damaged = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={"beats": beats}),
        "copy_delivery": draft.copy_delivery.model_copy(
            update={"deliveries": deliveries}
        ),
    })

    with pytest.raises(ValueError) as exc_info:
        finalize_director_production_plan(
            damaged,
            artifact,
            plan_id="plan-aggregate-errors",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )
    message = str(exc_info.value)
    assert "every approved script line" in message
    assert "cannot fit line l1" in message


def test_production_plan_packet_binds_identity_but_not_beat_count():
    artifact = _artifact()
    packet = build_director_production_plan_packet(
        artifact,
        capability_catalog=[
            {
                "capability": "visual.reference.generate",
                "input_contract": "VisualProgram",
                "output_contract": "ReferenceAssetSet",
            },
        ],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
    )
    definitions = packet["output_contract"]["$defs"]

    assert packet["role"] == "content_director_production_plan"
    assert packet["immutable_line_sequence"] == [
        "l1",
        "l2",
        "l3",
        "l4",
    ]
    assert packet["line_delivery_contract"][0][
        "registered_transport_window"
    ] == {
        "segment_index": 1,
        "window_start_seconds": 0.0,
        "window_end_seconds": 10.0,
    }
    assert packet["line_delivery_contract"][0][
        "minimum_delivery_seconds"
    ] > 0
    serialized_contract = json.dumps(
        packet["output_contract"],
        sort_keys=True,
    )
    for forbidden in (
        '"program_id"',
        '"director_artifact_sha256"',
        '"target_duration_seconds"',
        '"aspect_ratio"',
        '"audio_mode"',
    ):
        assert forbidden not in serialized_contract
    assert definitions["ScriptDeliveryIntent"]["properties"][
        "line_id"
    ]["enum"] == ["l1", "l2", "l3", "l4"]
    voice_schema = definitions["SpeakerVoiceIntent"]
    for field_name in (
        "gender",
        "screen_relation",
        "timbre",
        "pitch",
        "accent",
    ):
        assert field_name in voice_schema["required"]
    assert "start_seconds" not in definitions[
        "ScriptDeliveryIntent"
    ]["properties"]
    assert "end_seconds" not in definitions[
        "ScriptDeliveryIntent"
    ]["properties"]
    beats_schema = definitions["VisualProgramAuthorDraft"]["properties"][
        "beats"
    ]
    assert beats_schema["minItems"] == 1
    assert beats_schema["maxItems"] == 1000
    assert beats_schema["minItems"] != beats_schema["maxItems"]
    assert packet["planning_rules"][
        "visual_beats_are_independent_of_provider_segment_count"
    ] is True
    assert packet["planning_rules"][
        "visual_beats_are_contiguous_without_gaps_or_overlaps"
    ] is True


def test_author_plan_rejects_implicit_voice_gender_or_screen_relation():
    artifact = _artifact()
    payload = _author_plan_payload(_spoken_plan(artifact))
    voice = payload["audio"]["voices"][0]
    voice.pop("gender")
    voice.pop("screen_relation")

    with pytest.raises(ValueError, match="explicit gender") as exc_info:
        finalize_director_production_plan_author_draft(
            DirectorProductionPlanAuthorDraft.model_validate(payload),
            artifact,
            plan_id="plan-voice-authority",
            revision=1,
            parent_plan_sha256=None,
            authorized_asset_refs={"asset:product:front"},
            authoritative_product_asset_refs={"asset:product:front"},
        )
    assert "narrator" in str(exc_info.value)


def test_author_only_production_plan_materializes_runtime_identity():
    artifact = _artifact()
    author = DirectorProductionPlanAuthorDraft.model_validate(
        _author_plan_payload(_spoken_plan(artifact))
    )
    plan = finalize_director_production_plan_author_draft(
        author,
        artifact,
        plan_id="author-plan",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert plan.schema_version == "2.0"
    assert plan.visual.program_id == artifact.program.program_id
    assert plan.visual.director_artifact_sha256 == artifact.artifact_sha256
    assert plan.visual.target_duration_seconds == (
        artifact.script.target_duration_seconds
    )
    assert plan.visual.aspect_ratio == artifact.program.aspect_ratio
    assert plan.audio.audio_mode == artifact.program.audio_mode
    assert plan.copy_delivery.program_id == artifact.program.program_id


def test_author_only_plan_runtime_compiles_copy_timing_deterministically():
    artifact = _artifact()
    payload = _author_plan_payload(_spoken_plan(artifact))
    assert all(
        "start_seconds" not in delivery and "end_seconds" not in delivery
        for delivery in payload["copy_delivery"]["deliveries"]
    )
    author = DirectorProductionPlanAuthorDraft.model_validate(payload)

    plan = finalize_director_production_plan_author_draft(
        author,
        artifact,
        plan_id="author-plan-compiled-timing",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    by_id = {
        delivery.line_id: delivery
        for delivery in plan.copy_delivery.deliveries
    }
    assert by_id["l1"].start_seconds == 0
    assert by_id["l1"].end_seconds == 10
    assert by_id["l4"].start_seconds == 30
    assert by_id["l4"].end_seconds == 40


def test_delivery_validation_uses_one_tolerant_duration_formula():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    deliveries = list(draft.copy_delivery.deliveries)
    # l3 has seven words and needs exactly 2.8 seconds at 150 WPM.  Binary
    # subtraction produces 2.799999..., which must not lose a whole word by
    # integer truncation.
    deliveries[2] = deliveries[2].model_copy(update={
        "start_seconds": 27.200000000000003,
        "end_seconds": 30.0,
    })
    boundary_plan = draft.model_copy(update={
        "copy_delivery": draft.copy_delivery.model_copy(
            update={"deliveries": deliveries}
        ),
    })

    plan = finalize_director_production_plan(
        boundary_plan,
        artifact,
        plan_id="float-boundary",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert plan.copy_delivery.deliveries[2].line_id == "l3"


def test_runtime_signed_plan_survives_json_float_reformatting():
    artifact = _artifact()
    plan = finalize_director_production_plan(
        _spoken_plan(artifact),
        artifact,
        plan_id="plan-json-float-roundtrip",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )
    payload = plan.model_dump(mode="json")
    original = float(payload["audio"]["cues"][0]["end_seconds"])
    payload["audio"]["cues"][0]["end_seconds"] = (
        original + 4e-15
    )

    restored = DirectedProductionPlan.model_validate(payload)

    assert restored.plan_sha256 == plan.plan_sha256


def test_runtime_signed_plan_rejects_hash_tampering():
    artifact = _artifact()
    plan = finalize_director_production_plan(
        _spoken_plan(artifact),
        artifact,
        plan_id="plan-hash",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )
    payload = plan.model_dump(mode="json")
    payload["visual"]["style_language"] = "A different unreviewed style."

    with pytest.raises(ValidationError, match="plan_sha256"):
        DirectedProductionPlan.model_validate(payload)


def test_production_plan_detects_impossible_generated_small_copy():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    references = list(draft.visual.references)
    references[1] = references[1].model_copy(update={
        "generation_brief": (
            "Show a handwritten note that reads Laptop closed at nine."
        ),
    })
    draft = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={"references": references}),
    })
    plan = finalize_director_production_plan(
        draft,
        artifact,
        plan_id="plan-generated-text",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert _generated_reference_text_dependencies(plan) == [
        "character-design"
    ]
    assert _generated_reference_text_dependency_details(plan) == [
        {
            "reference_id": "character-design",
            "matches": [
                {
                    "field": "generation_brief",
                    "evidence": (
                        "Show a handwritten note that reads Laptop closed "
                        "at nine."
                    ),
                }
            ],
        }
    ]


def test_production_plan_allows_explicitly_nonrequired_generated_copy():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    references = list(draft.visual.references)
    references[1] = references[1].model_copy(update={
        "generation_brief": (
            "Show an alarm clock with abstract numerals rather than required "
            "as generated readable text."
        ),
    })
    draft = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={"references": references}),
    })
    plan = finalize_director_production_plan(
        draft,
        artifact,
        plan_id="plan-no-generated-text-dependency",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert _generated_reference_text_dependency_details(plan) == []


def test_production_plan_detects_exact_writing_action_in_linked_beat():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    references = list(draft.visual.references)
    target = references[1]
    references[1] = target.model_copy(update={
        "generation_brief": (
            "Same adult at a warm home desk with a blank paper note."
        ),
    })
    beats = list(draft.visual.beats)
    linked_index = next(
        index
        for index, beat in enumerate(beats)
        if target.reference_id in beat.reference_ids
    )
    beats[linked_index] = beats[linked_index].model_copy(update={
        "subject_action": (
            "The adult writes Laptop closed at nine on the paper note and "
            "places it beside the keyboard."
        ),
    })
    draft = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={
            "references": references,
            "beats": beats,
        }),
    })
    plan = finalize_director_production_plan_author_draft(
        draft,
        artifact,
        plan_id="plan-generated-action-text",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert target.reference_id in _generated_reference_text_dependencies(plan)


def test_production_plan_allows_generic_typing_with_unreadable_screen():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    references = list(draft.visual.references)
    target = references[1]
    references[1] = target.model_copy(update={
        "purpose": "Show the late email catch-up pattern.",
        "generation_brief": (
            "Same adult typing at a laptop with generic inbox rows; no "
            "readable screen text, sender names, brands, or UI copy."
        ),
    })
    beats = list(draft.visual.beats)
    linked_index = next(
        index
        for index, beat in enumerate(beats)
        if target.reference_id in beat.reference_ids
    )
    beats[linked_index] = beats[linked_index].model_copy(update={
        "subject_action": (
            "The adult types briefly, stops, and takes both hands away "
            "from the keyboard."
        ),
    })
    draft = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={
            "references": references,
            "beats": beats,
        }),
    })
    plan = finalize_director_production_plan_author_draft(
        draft,
        artifact,
        plan_id="plan-generic-screen-action",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert target.reference_id not in _generated_reference_text_dependencies(
        plan
    )


def test_product_reference_allows_copy_from_authoritative_local_composite():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    references = list(draft.visual.references)
    target_index = next(
        index
        for index, reference in enumerate(references)
        if "product" in reference.roles
    )
    target = references[target_index]
    references[target_index] = target.model_copy(update={
        "generation_brief": (
            "Use a broad empty tabletop. Exact package pixels and all package "
            "labeling are supplied only by the authoritative local composite."
        ),
    })
    draft = draft.model_copy(update={
        "visual": draft.visual.model_copy(update={"references": references}),
    })
    plan = finalize_director_production_plan_author_draft(
        draft,
        artifact,
        plan_id="plan-authoritative-product-copy",
        revision=1,
        parent_plan_sha256=None,
        authorized_asset_refs={"asset:product:front"},
        authoritative_product_asset_refs={"asset:product:front"},
    )

    assert target.reference_id not in _generated_reference_text_dependencies(
        plan
    )


def test_universal_profile_owns_production_plan_review_thresholds():
    profile = load_universal_director_profile()

    assert {
        item.criterion_id
        for item in profile.production_plan_review_criteria
    } == {
        "visual_script_alignment",
        "visual_continuity_and_reference_efficiency",
        "audio_and_copy_delivery_fit",
        "production_plan_truth_boundary",
    }
    assert next(
        item
        for item in profile.production_plan_review_criteria
        if item.criterion_id == "production_plan_truth_boundary"
    ).minimum_score == 100


@pytest.mark.asyncio
async def test_production_plan_loop_approves_without_media_work():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    director = _FakeClient([_author_plan_payload(draft)])
    critic = _FakeClient([_approved_plan_verdict()])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-loop-approved",
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[
            {
                "capability": "visual.reference.generate",
                "input_contract": "VisualProgram",
                "output_contract": "ReferenceAssetSet",
            }
        ],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert len(result.plans) == 1
    assert len(result.reviews) == 1
    assert len(result.critic_attempts) == 1
    assert json.loads(director.calls[0]["input_text"])["role"] == (
        "content_director_production_plan"
    )
    assert json.loads(critic.calls[0]["input_text"])["role"] == (
        "independent_production_plan_critic"
    )
    critic_contract = json.loads(
        critic.calls[0]["input_text"]
    )["output_contract"]
    scores = critic_contract["properties"]["scores"]
    assert scores["required"] == [
        item.criterion_id for item in _plan_criteria()
    ]
    assert scores["additionalProperties"] is False


@pytest.mark.asyncio
async def test_production_plan_loop_rejects_legacy_audio_authority():
    current = _artifact()
    legacy_program = current.program.model_copy(update={
        "schema_version": "1.0",
        "audio_mode": None,
    })
    legacy = build_directed_content_artifact(
        artifact_id="legacy-v1-artifact",
        revision=1,
        parent_artifact_sha256=None,
        program=legacy_program,
        script=current.script,
    )
    director = _FakeClient([])
    critic = _FakeClient([])

    result = await run_content_production_plan_loop(
        artifact=legacy,
        plan_id="legacy-plan",
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "quality_pause"
    assert "v2 replan" in result.reason
    assert director.calls == []
    assert critic.calls == []


@pytest.mark.asyncio
async def test_production_plan_loop_revises_plan_without_rewriting_copy():
    artifact = _artifact()
    initial = _spoken_plan(artifact)
    revised = initial.model_copy(
        update={
            "visual": initial.visual.model_copy(
                update={
                    "style_language": (
                        "Adult 2D animation with a concrete notification trail."
                    )
                }
            )
        }
    )
    director = _FakeClient([
        _author_plan_payload(initial),
        _author_plan_payload(revised),
    ])
    critic = _FakeClient([
        _rejected_plan_verdict(),
        _approved_plan_verdict(),
    ])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-loop-revision",
        policy=DirectorLoopPolicy(
            maximum_revisions=1,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [plan.revision for plan in result.plans] == [1, 2]
    assert result.plans[1].parent_plan_sha256 == (
        result.plans[0].plan_sha256
    )
    assert [
        line.text for line in artifact.script.lines
    ] == [
        "The inbox followed her home.",
        "She finally closed it.",
        "The confirmed format fit that small routine.",
        "It is $7.99 in the yellow cart.",
    ]
    revision_packet = json.loads(director.calls[1]["input_text"])
    assert revision_packet["role"] == (
        "content_director_production_plan_revision"
    )
    assert revision_packet["critic_blocking_issues"][0][
        "beat_ids"
    ] == ["recognition"]


@pytest.mark.asyncio
async def test_production_plan_loop_repairs_contract_before_review():
    artifact = _artifact()
    draft = _spoken_plan(artifact)
    director = _FakeClient([
        "not-json",
        _author_plan_payload(draft),
    ])
    critic = _FakeClient([_approved_plan_verdict()])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-loop-contract-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [attempt.outcome for attempt in result.attempts] == [
        "contract_rejected",
        "accepted",
    ]
    assert json.loads(director.calls[1]["input_text"])["role"] == (
        "content_director_production_plan_contract_repair"
    )
    initial_packet = json.loads(director.calls[0]["input_text"])
    repair_packet = json.loads(director.calls[1]["input_text"])
    assert initial_packet["persistent_contract_requirements"][
        "spoken_lines_are_audible"
    ]
    assert repair_packet["accumulated_validation_errors"] == [
        "production plan response is not valid JSON"
    ]
    assert repair_packet["repair_rules"][
        "do_not_regress_previously_valid_fields"
    ] is True


@pytest.mark.asyncio
async def test_spoken_plan_copy_repair_never_requests_local_overlay():
    artifact = _artifact()
    invalid = _spoken_plan(artifact)
    references = list(invalid.visual.references)
    references[1] = references[1].model_copy(update={
        "generation_brief": (
            "Show a handwritten note that reads Laptop closed at nine."
        ),
    })
    invalid = invalid.model_copy(update={
        "visual": invalid.visual.model_copy(update={
            "references": references,
        }),
    })
    valid = _spoken_plan(artifact)
    director = _FakeClient([
        _author_plan_payload(invalid),
        _author_plan_payload(valid),
    ])
    critic = _FakeClient([_approved_plan_verdict()])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-spoken-copy-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    repair_packet = json.loads(director.calls[1]["input_text"])
    assert repair_packet["repair_rules"]["copy_delivery_mode"] == "spoken"
    assert repair_packet["repair_rules"][
        "spoken_copy_must_not_become_local_overlay"
    ] is True
    validation_error = repair_packet["validation_error"]
    assert "provider_dialogue or local_voiceover" in validation_error
    assert "do not create local_overlay for spoken copy" in validation_error
    assert "generation_brief" in validation_error


@pytest.mark.asyncio
async def test_production_plan_loop_audits_critic_contract_repair():
    artifact = _artifact()
    director = _FakeClient([
        _author_plan_payload(_spoken_plan(artifact))
    ])
    invalid_verdict = _approved_plan_verdict()
    invalid_verdict["scores"] = {"wrong_key": 100}
    critic = _FakeClient([invalid_verdict, _approved_plan_verdict()])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-loop-critic-contract-repair",
        policy=DirectorLoopPolicy(
            maximum_revisions=0,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [attempt.outcome for attempt in result.critic_attempts] == [
        "contract_rejected",
        "accepted",
    ]
    assert result.reviews[0].latency_ms == 14
    assert "score keys" in result.contract_errors[-1]


@pytest.mark.asyncio
async def test_production_plan_loop_stops_when_copy_needs_replan():
    artifact = _artifact()
    director = _FakeClient([
        _author_plan_payload(_spoken_plan(artifact))
    ])
    critic = _FakeClient([
        _rejected_plan_verdict(repair_scope="director_replan")
    ])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-loop-director-replan",
        policy=DirectorLoopPolicy(
            maximum_revisions=3,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "quality_pause"
    assert "requires a Director replan" in result.reason
    assert len(director.calls) == 1


@pytest.mark.asyncio
async def test_plan_owned_audio_conflict_repairs_inside_production_plan():
    artifact = _artifact()
    initial_plan = _spoken_plan(artifact)
    repaired_plan = _spoken_plan(artifact)
    director = _FakeClient([
        _author_plan_payload(initial_plan),
        _author_plan_payload(repaired_plan),
    ])
    invalid_scope = _rejected_plan_verdict(
        repair_scope="director_replan"
    )
    invalid_scope["blocking_issues"][0].update({
        "code": "AUDIO_VISUAL_ACTION_CONFLICT",
        "audio_cue_ids": ["bed"],
        "evidence": (
            "Production-plan cue bed conflicts with the cited recognition "
            "beat action."
        ),
        "repair_instruction": (
            "Revise cue bed so it matches the existing visual action."
        ),
    })
    corrected_scope = json.loads(json.dumps(invalid_scope))
    corrected_scope["repair_scope"] = "plan_only"
    critic = _FakeClient([
        invalid_scope,
        corrected_scope,
        _approved_plan_verdict(),
    ])

    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id="plan-loop-audio-conflict",
        policy=DirectorLoopPolicy(
            maximum_revisions=2,
            maximum_contract_repairs_per_revision=1,
            series_page_size=10,
        ),
        review_criteria=_plan_criteria(),
        capability_catalog=[],
        authorized_asset_refs=["asset:product:front"],
        authoritative_product_asset_refs=["asset:product:front"],
        director_client=director,
        critic_client=critic,
    )

    assert result.status == "approved"
    assert [item.outcome for item in result.critic_attempts] == [
        "contract_rejected",
        "accepted",
        "accepted",
    ]
    assert "must use repair_scope plan_only" in result.contract_errors[0]
    assert len(director.calls) == 2
