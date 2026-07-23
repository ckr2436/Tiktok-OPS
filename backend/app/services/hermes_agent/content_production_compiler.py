from __future__ import annotations

from typing import Any

from app.services.hermes_agent.content_director import DirectedContentArtifact
from app.services.hermes_agent.content_production_plan import (
    DirectedProductionPlan,
)


_REFERENCE_ROLE_MAP = {
    "character": "character_anchor",
    "scene": "scene_anchor",
    "action": "action_anchor",
    "first_frame": "action_anchor",
    # The original uploaded package remains a separate authority.  A
    # generated reference carrying this role is an interaction/composition
    # guide, never a replacement package image.
    "product": "action_anchor",
}


def _segment_windows(
    artifact: DirectedContentArtifact,
) -> list[tuple[int, float, float, list[str]]]:
    rows: list[tuple[int, float, float, list[str]]] = []
    start = 0.0
    for segment in artifact.script.segments:
        end = start + float(segment.duration_seconds)
        rows.append((
            int(segment.segment_index),
            start,
            end,
            list(segment.line_ids),
        ))
        start = end
    return rows


def _overlaps(left_start: float, left_end: float, right_start: float, right_end: float) -> bool:
    return min(left_end, right_end) - max(left_start, right_start) > 0.01


def compile_production_plan_for_media(
    artifact: DirectedContentArtifact,
    plan: DirectedProductionPlan,
    *,
    asset_registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compile one signed plan into the existing media-stage data shape.

    This is a deterministic transport adapter, not another creative stage.
    It cannot choose a story, rewrite copy, add references, or change timing.
    """

    if plan.visual.director_artifact_sha256 != artifact.artifact_sha256:
        raise ValueError("production plan belongs to another Director artifact")
    line_by_id = {line.line_id: line for line in artifact.script.lines}
    reference_by_id = {
        item.reference_id: item for item in plan.visual.references
    }
    windows = _segment_windows(artifact)

    reference_plan: list[dict[str, Any]] = []
    for index, reference in enumerate(plan.visual.references, 1):
        if not str(reference.generation_brief or "").strip():
            raise ValueError(
                "production plan contains a source-only visual reference; "
                "re-author the plan before media execution"
            )
        citing_beats = [
            beat
            for beat in plan.visual.beats
            if reference.reference_id in beat.reference_ids
        ]
        segment_indices = [
            segment_index
            for segment_index, start, end, _line_ids in windows
            if any(
                _overlaps(beat.start_seconds, beat.end_seconds, start, end)
                for beat in citing_beats
            )
        ]
        roles = list(dict.fromkeys(
            _REFERENCE_ROLE_MAP[role] for role in reference.roles
        )) or ["action_anchor"]
        unknown_assets = sorted(
            set(reference.source_asset_refs) - set(asset_registry)
        )
        if unknown_assets:
            raise ValueError(
                "production plan compiler received unknown source assets: "
                f"{unknown_assets}"
            )
        description = str(
            reference.generation_brief or reference.purpose
        ).strip()
        reference_plan.append({
            "index": index,
            "reference_id": reference.reference_id,
            "segment": segment_indices[0] if segment_indices else 1,
            "segments": segment_indices,
            "description": description,
            "purpose": reference.purpose,
            "roles": roles,
            "source_asset_refs": list(reference.source_asset_refs),
            "generation_mode": "generate",
            "requires_product_reference": "product" in reference.roles,
        })

    shot_plan: list[dict[str, Any]] = []
    script_segments: list[dict[str, Any]] = []
    for segment_index, start, end, line_ids in windows:
        beats = [
            beat
            for beat in plan.visual.beats
            if _overlaps(beat.start_seconds, beat.end_seconds, start, end)
        ]
        dialogue = [
            {
                "line_id": line_by_id[line_id].line_id,
                "speaker_id": line_by_id[line_id].speaker_id,
                "speaker": line_by_id[line_id].speaker_id,
                "line": line_by_id[line_id].text,
                "delivery_mode": line_by_id[line_id].delivery_mode,
            }
            for line_id in line_ids
        ]
        purposes = list(dict.fromkeys(
            line_by_id[line_id].purpose for line_id in line_ids
        ))
        visual_state = " ".join(
            (
                f"{beat.environment}. {beat.subject_action}. "
                f"{beat.camera_composition}. {beat.continuity_state}."
            )
            for beat in beats
        ).strip()
        transition = " ".join(
            beat.motion_and_transition for beat in beats
        ).strip()
        reference_ids = list(dict.fromkeys(
            reference_id
            for beat in beats
            for reference_id in beat.reference_ids
        ))
        shot_plan.append({
            "segment": segment_index,
            "segment_index": segment_index,
            "start_seconds": start,
            "end_seconds": end,
            "duration_seconds": round(end - start, 2),
            "story_function": " / ".join(purposes),
            "visual_state": visual_state,
            "motion_and_transition": transition,
            "reference_ids": reference_ids,
            "director_line_ids": line_ids,
        })
        script_segments.append({
            "segment_index": segment_index,
            "duration_seconds": round(end - start, 2),
            "story_function": " / ".join(purposes),
            "dialogue_lines": dialogue,
            "director_line_ids": line_ids,
            "visual_state": visual_state,
            "motion_and_transition": transition,
            "reference_ids": reference_ids,
        })

    voices = {
        voice.speaker_id: {
            "identity": voice.identity,
            "gender": voice.gender,
            "screen_relation": voice.screen_relation,
            "timbre": voice.timbre,
            "pitch": voice.pitch,
            "accent": voice.accent,
            "delivery_direction": voice.delivery_direction,
            "continuity_rule": voice.continuity_rule,
        }
        for voice in plan.audio.voices
    }
    legacy_speakers = [
        {
            "speaker_id": voice.speaker_id,
            "name": voice.identity,
            "identity": voice.identity,
            "gender": voice.gender,
            "screen_relation": voice.screen_relation,
            "timbre": voice.timbre,
            "pitch": voice.pitch,
            "accent": voice.accent,
            "delivery": voice.delivery_direction,
            "delivery_direction": voice.delivery_direction,
            "continuity_rule": voice.continuity_rule,
            "speech_rate": float(artifact.script.speech_rate_wpm),
            "speech_rate_unit": (
                "characters_per_minute"
                if str(artifact.program.locale).lower().startswith("zh")
                else "words_per_minute"
            ),
        }
        for voice in plan.audio.voices
    ]
    conversion = artifact.program.conversion
    selected_concept = {
        "concept_id": artifact.program.program_id,
        "name": artifact.program.content_type,
        "content_type": artifact.program.content_type,
        "objective": artifact.program.objective,
        "creative_strategy": dict(artifact.program.creative_strategy),
    }
    return {
        "schema_version": "production-plan-compiler-v1",
        "concepts": [selected_concept],
        "selected_concept": selected_concept,
        "visual_job_ticket": {
            "source": "directed_production_plan",
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "visual_style": plan.visual.style_language,
            "visual_grammar": plan.visual.visual_grammar,
            "product_presentation_intent": (
                plan.visual.product_presentation_intent
            ),
            "reference_image_count": len(reference_plan),
            "final_reference_count": len(reference_plan),
            "reference_plan": reference_plan,
        },
        "continuity_rules": {
            "source": "directed_production_plan",
            "visual_style": plan.visual.style_language,
            "visual_grammar": plan.visual.visual_grammar,
            "reference_intents": {
                reference_id: reference.model_dump(mode="json")
                for reference_id, reference in reference_by_id.items()
            },
            "audio_mode": plan.audio.audio_mode,
            "audio_mix_intent": plan.audio.mix_intent,
        },
        "shot_plan": shot_plan,
        "cta_options": (
            [conversion.cta_text] if conversion.cta_text else []
        ),
        "complete_video_script": {
            "script_id": artifact.script.script_id,
            "program_id": artifact.script.program_id,
            "audio_mode": artifact.script.audio_mode,
            "duration_seconds": float(
                artifact.script.target_duration_seconds
            ),
            "target_edit_duration_seconds": float(
                artifact.script.target_duration_seconds
                - artifact.script.edit_headroom_seconds
            ),
            "speech_rate_wpm": float(artifact.script.speech_rate_wpm),
            "display_reading_rate_wpm": float(
                artifact.script.display_reading_rate_wpm
            ),
            "primary_speaker_id": artifact.script.primary_speaker_id,
            "canonical_text_sha256": (
                artifact.script.canonical_text_sha256
            ),
            "segments": script_segments,
        },
        "voice_bible": {
            "audio_mode": plan.audio.audio_mode,
            "primary_speaker_id": artifact.script.primary_speaker_id,
            "speakers": legacy_speakers,
            "voices": voices,
            "mix_intent": plan.audio.mix_intent,
            "cues": [item.model_dump(mode="json") for item in plan.audio.cues],
        },
        "director_lock": {
            "artifact_id": artifact.artifact_id,
            "artifact_revision": int(artifact.revision),
            "artifact_sha256": artifact.artifact_sha256,
            "script_id": artifact.script.script_id,
            "script_sha256": artifact.script.canonical_text_sha256,
            "audio_mode": artifact.script.audio_mode,
            "line_ids": [line.line_id for line in artifact.script.lines],
            "segment_line_ids": [
                {
                    "segment_index": int(item.segment_index),
                    "line_ids": list(item.line_ids),
                }
                for item in artifact.script.segments
            ],
        },
        "production_plan_lock": {
            "plan_id": plan.plan_id,
            "plan_revision": int(plan.revision),
            "plan_sha256": plan.plan_sha256,
            "director_artifact_sha256": artifact.artifact_sha256,
        },
    }


def compile_production_plan_to_video_result(
    artifact: DirectedContentArtifact,
    plan: DirectedProductionPlan,
    *,
    variant_index: int,
    resolution: str,
    language_label: str,
    reference_image_limit: int | None = None,
) -> dict[str, Any]:
    """Compile the signed plan into segment execution data without an LLM.

    The returned shape is deliberately compatible with the historical
    ``VIDEO_PROMPTS`` storage boundary so existing provider submission,
    download, composition, and recovery code can migrate without keeping a
    second creative authority.  Every semantic value comes from the accepted
    Director artifact or Production Plan; this compiler only clips global
    intervals into provider-local segment coordinates and binds reference
    indices.
    """

    if plan.visual.director_artifact_sha256 != artifact.artifact_sha256:
        raise ValueError("production plan belongs to another Director artifact")

    line_by_id = {line.line_id: line for line in artifact.script.lines}
    reference_by_id = {
        reference.reference_id: (index, reference)
        for index, reference in enumerate(plan.visual.references, 1)
    }
    delivery_by_id = {
        delivery.line_id: delivery for delivery in plan.copy_delivery.deliveries
    }
    voice_by_id = {
        voice.speaker_id: voice for voice in plan.audio.voices
    }
    reference_limit = (
        max(0, int(reference_image_limit))
        if reference_image_limit is not None
        else None
    )

    segments: list[dict[str, Any]] = []
    segment_start = 0.0
    for allocation in artifact.script.segments:
        segment_index = int(allocation.segment_index)
        segment_duration = float(allocation.duration_seconds)
        segment_end = segment_start + segment_duration
        beats = [
            beat
            for beat in plan.visual.beats
            if _overlaps(
                float(beat.start_seconds),
                float(beat.end_seconds),
                segment_start,
                segment_end,
            )
        ]
        if not beats:
            raise ValueError(
                "production plan has no visual beat for segment "
                f"{segment_index}"
            )

        reference_ids = list(dict.fromkeys(
            reference_id
            for beat in beats
            for reference_id in beat.reference_ids
        ))
        missing_references = [
            reference_id
            for reference_id in reference_ids
            if reference_id not in reference_by_id
        ]
        if missing_references:
            raise ValueError(
                "production plan segment cites unknown references: "
                f"{missing_references}"
            )
        reference_indices = [
            int(reference_by_id[reference_id][0])
            for reference_id in reference_ids
        ]
        product_anchor_required = any(
            "product" in reference_by_id[reference_id][1].roles
            for reference_id in reference_ids
        )
        required_provider_images = len(reference_indices) + (
            1 if product_anchor_required else 0
        )
        if (
            reference_limit is not None
            and required_provider_images > reference_limit
        ):
            raise ValueError(
                "production plan segment exceeds provider reference limit: "
                f"segment={segment_index} required={required_provider_images} "
                f"limit={reference_limit}"
            )

        timeline: list[dict[str, Any]] = []
        for beat in beats:
            local_start = max(float(beat.start_seconds), segment_start) - segment_start
            local_end = min(float(beat.end_seconds), segment_end) - segment_start
            if local_end - local_start <= 0.01:
                continue
            timeline.append({
                "start_seconds": round(local_start, 2),
                "end_seconds": round(local_end, 2),
                "action": " ".join(
                    value.strip()
                    for value in (
                        beat.environment,
                        beat.subject_action,
                        beat.motion_and_transition,
                    )
                    if str(value or "").strip()
                ),
                "camera": str(beat.camera_composition).strip(),
                "dialogue_key": ",".join(
                    line_id
                    for line_id in beat.line_ids
                    if line_id in allocation.line_ids
                ),
            })

        dialogue_lines: list[dict[str, Any]] = []
        display_lines: list[dict[str, Any]] = []
        for line_id in allocation.line_ids:
            line = line_by_id[line_id]
            delivery = delivery_by_id[line_id]
            row = {
                "line_id": line.line_id,
                "speaker_id": line.speaker_id,
                "speaker": line.speaker_id,
                "line": line.text,
                "delivery_mode": line.delivery_mode,
                "delivery_method": delivery.method,
                "start_seconds": round(
                    float(delivery.start_seconds) - segment_start,
                    2,
                ),
                "end_seconds": round(
                    float(delivery.end_seconds) - segment_start,
                    2,
                ),
                "overlay_presentation": (
                    delivery.presentation.model_dump(mode="json")
                    if delivery.presentation is not None
                    else None
                ),
            }
            if line.delivery_mode == "spoken":
                dialogue_lines.append(row)
            else:
                display_lines.append(row)

        purposes = list(dict.fromkeys(
            line_by_id[line_id].purpose for line_id in allocation.line_ids
        ))
        environments = list(dict.fromkeys(
            str(beat.environment).strip() for beat in beats
        ))
        actions = list(dict.fromkeys(
            str(beat.subject_action).strip() for beat in beats
        ))
        transitions = list(dict.fromkeys(
            str(beat.motion_and_transition).strip() for beat in beats
        ))
        cameras = list(dict.fromkeys(
            str(beat.camera_composition).strip() for beat in beats
        ))
        prompt = "\n".join([
            f"Segment {segment_index}: {' / '.join(purposes)}",
            "Environment: " + " ".join(environments),
            "Action: " + " ".join(actions),
            "Motion and transition: " + " ".join(transitions),
        ])
        voice_lock = [
            {
                "speaker_id": speaker_id,
                "name": voice_by_id[speaker_id].identity,
                "identity": voice_by_id[speaker_id].identity,
                "gender": voice_by_id[speaker_id].gender,
                "screen_relation": (
                    voice_by_id[speaker_id].screen_relation
                ),
                "timbre": voice_by_id[speaker_id].timbre,
                "pitch": voice_by_id[speaker_id].pitch,
                "accent": voice_by_id[speaker_id].accent,
                "delivery": voice_by_id[speaker_id].delivery_direction,
                "delivery_direction": (
                    voice_by_id[speaker_id].delivery_direction
                ),
                "continuity_rule": voice_by_id[speaker_id].continuity_rule,
                "speech_rate": float(artifact.script.speech_rate_wpm),
                "speech_rate_unit": (
                    "characters_per_minute"
                    if str(artifact.program.locale).lower().startswith("zh")
                    else "words_per_minute"
                ),
            }
            for speaker_id in list(dict.fromkeys(
                line_by_id[line_id].speaker_id
                for line_id in allocation.line_ids
                if line_by_id[line_id].delivery_mode == "spoken"
            ))
            if speaker_id in voice_by_id
        ]
        continuity_dependency = (
            "previous_segment"
            if segment_index > 1 and any(
                beat.continuity_dependency == "previous_segment"
                for beat in beats
            )
            else "independent"
        )
        segments.append({
            "segment_index": segment_index,
            "duration_seconds": segment_duration,
            "segment_goal": " / ".join(purposes),
            "prompt": prompt,
            # Visual style is a signed whole-video decision.  Every provider
            # call is an isolated segment, so omitting it here silently lets
            # later compilation replace an animation contract with a provider
            # default such as photorealism.
            "visual_style": plan.visual.style_language,
            "visual_grammar": plan.visual.visual_grammar,
            "timeline": timeline,
            "pacing": plan.visual.visual_grammar,
            "camera_direction": " ".join(cameras),
            "dialogue_lines": dialogue_lines,
            "display_lines": display_lines,
            "voice_lock": voice_lock,
            "continuity_note": str(beats[-1].continuity_state).strip(),
            "continuity_dependency": continuity_dependency,
            "negative_prompt": "",
            "reference_ids": reference_ids,
            "reference_indices": reference_indices,
            "product_anchor_required": product_anchor_required,
            # Product presence is generated by the provider from the uploaded
            # package identity anchor.  Historical placement hints are not
            # compiled into local pixel-composite instructions.
            "authoritative_product_composites": [],
            "product_render_mode": "provider_reference",
            "audio_mode": plan.audio.audio_mode,
            "compile_source": "signed_production_plan",
        })
        segment_start = segment_end

    return {
        "videos": [{
            "video_id": (
                f"{artifact.program.program_id}.variant-{int(variant_index):03d}"
            ),
            "variant_index": int(variant_index),
            "version_name": f"v{int(variant_index):02d}",
            "story_arc": artifact.program.objective,
            "duration_seconds": float(
                artifact.script.target_duration_seconds
            ),
            "format": f"{artifact.program.platform} {artifact.program.aspect_ratio}",
            "resolution": str(resolution),
            "language": str(language_label),
            "audio_mode": artifact.script.audio_mode,
            "visual_style": plan.visual.style_language,
            "visual_grammar": plan.visual.visual_grammar,
            "segments": segments,
            "compiler_authority": {
                "director_artifact_sha256": artifact.artifact_sha256,
                "director_script_sha256": (
                    artifact.script.canonical_text_sha256
                ),
                "production_plan_sha256": plan.plan_sha256,
            },
        }]
    }


__all__ = [
    "compile_production_plan_for_media",
    "compile_production_plan_to_video_result",
]
