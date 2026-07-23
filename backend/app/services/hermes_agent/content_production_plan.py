from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.hermes_agent.content_director import (
    AUDIO_MODE_SEMANTICS,
    DirectedContentArtifact,
)


_TIMING_TOLERANCE_SECONDS = 0.05
_SPOKEN_PACING_TOLERANCE_FRACTION = 0.05
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")


class AuthoritativeProductComposite(BaseModel):
    """Placement contract for exact source pixels inside a generated scene."""

    model_config = ConfigDict(extra="forbid")

    placement: Literal[
        "upper_center",
        "center",
        "lower_center",
        "lower_left",
        "lower_right",
    ]
    width_fraction: float = Field(ge=0.12, le=0.70)
    entrance: Literal["cut", "fade"] = "fade"


class VisualReferenceIntent(BaseModel):
    """A semantic reference request, not a provider upload list."""

    model_config = ConfigDict(extra="forbid")

    reference_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    roles: list[
        Literal[
            "character",
            "scene",
            "action",
            "product",
            "first_frame",
        ]
    ] = Field(min_length=1, max_length=5)
    purpose: str = Field(min_length=1, max_length=1000)
    source_asset_refs: list[str] = Field(
        default_factory=list,
        max_length=64,
    )
    generation_brief: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )
    # Historical plans may carry a deterministic placement hint.  It is no
    # longer an instruction to paste package pixels into generated media: the
    # authoritative uploaded package is supplied to the image/video model as
    # a visual identity reference and the model renders it naturally in-scene.
    authoritative_product_composite: AuthoritativeProductComposite | None = None

    @model_validator(mode="after")
    def validate_reference_intent(self) -> "VisualReferenceIntent":
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("visual reference roles must be unique")
        if len(self.source_asset_refs) != len(set(self.source_asset_refs)):
            raise ValueError("visual source asset refs must be unique")
        if not self.source_asset_refs and not self.generation_brief:
            raise ValueError(
                "visual reference needs a source asset or generation brief"
            )
        if (
            self.authoritative_product_composite is not None
            and "product" not in self.roles
        ):
            raise ValueError(
                "authoritative product composite requires the product role"
            )
        return self


class VisualBeat(BaseModel):
    """One Director-owned meaning interval; shot count remains model-owned."""

    model_config = ConfigDict(extra="forbid")

    beat_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    start_seconds: float = Field(ge=0, le=3600)
    end_seconds: float = Field(gt=0, le=3600)
    line_ids: list[str] = Field(default_factory=list, max_length=1000)
    purpose: str = Field(min_length=1, max_length=1000)
    environment: str = Field(min_length=1, max_length=2000)
    subject_action: str = Field(min_length=1, max_length=4000)
    camera_composition: str = Field(min_length=1, max_length=2000)
    motion_and_transition: str = Field(min_length=1, max_length=2000)
    continuity_state: str = Field(min_length=1, max_length=2000)
    continuity_dependency: Literal["previous_segment", "independent"] = (
        "previous_segment"
    )
    reference_ids: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_interval(self) -> "VisualBeat":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("visual beat end must be after start")
        if len(self.line_ids) != len(set(self.line_ids)):
            raise ValueError("visual beat line IDs must be unique")
        if len(self.reference_ids) != len(set(self.reference_ids)):
            raise ValueError("visual beat reference IDs must be unique")
        return self


class VisualProgramDraft(BaseModel):
    """Provider-independent visual direction for the approved script."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    program_id: str = Field(min_length=1, max_length=128)
    director_artifact_sha256: str = Field(min_length=64, max_length=64)
    target_duration_seconds: float = Field(gt=0, le=3600)
    aspect_ratio: str = Field(min_length=3, max_length=32)
    style_language: str = Field(min_length=1, max_length=4000)
    visual_grammar: str = Field(min_length=1, max_length=4000)
    product_presentation_intent: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )
    references: list[VisualReferenceIntent] = Field(
        default_factory=list,
        max_length=1000,
    )
    beats: list[VisualBeat] = Field(min_length=1, max_length=1000)


class SpeakerVoiceIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker_id: str = Field(min_length=1, max_length=128)
    identity: str = Field(min_length=1, max_length=1000)
    # Defaults keep already-signed historical plans readable.  The author
    # contract and finalizer below require every newly-authored spoken plan to
    # make these choices explicitly, so an isolated provider call never has to
    # guess a speaker's sex or relationship to the person on screen.
    gender: Literal["female", "male", "androgynous", "unspecified"] = (
        "unspecified"
    )
    screen_relation: Literal[
        "off_screen_narrator",
        "on_screen_character",
        "character_voiceover",
        "unspecified",
    ] = "unspecified"
    timbre: str = Field(default="", max_length=500)
    pitch: str = Field(default="", max_length=200)
    accent: str = Field(default="", max_length=300)
    delivery_direction: str = Field(min_length=1, max_length=2000)
    continuity_rule: str = Field(min_length=1, max_length=1000)


class AudioCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cue_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    start_seconds: float = Field(ge=0, le=3600)
    end_seconds: float = Field(gt=0, le=3600)
    kind: Literal["music", "sound_effect", "room_tone", "silence"]
    intent: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_interval(self) -> "AudioCue":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("audio cue end must be after start")
        return self


class AudioProgramDraft(BaseModel):
    """Voice identity and sound design without provider-specific fields."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    program_id: str = Field(min_length=1, max_length=128)
    director_artifact_sha256: str = Field(min_length=64, max_length=64)
    target_duration_seconds: float = Field(gt=0, le=3600)
    audio_mode: Literal["spoken", "silent", "music_only", "sound_design"]
    voices: list[SpeakerVoiceIntent] = Field(
        default_factory=list,
        max_length=128,
    )
    cues: list[AudioCue] = Field(default_factory=list, max_length=1000)
    mix_intent: str = Field(min_length=1, max_length=4000)


class OverlayPresentation(BaseModel):
    """Director-owned readable placement for one visible copy line."""

    model_config = ConfigDict(extra="forbid")

    placement: Literal[
        "top_safe",
        "center",
        "lower_third",
        "bottom_safe",
    ]
    emphasis: Literal["quiet", "standard", "strong"] = "standard"
    background: Literal["none", "shadow", "box"] = "box"
    max_lines: int = Field(default=2, ge=1, le=4)


class TimedScriptDelivery(BaseModel):
    """Exact delivery ownership for one immutable script line."""

    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(min_length=1, max_length=64)
    start_seconds: float = Field(ge=0, le=3600)
    end_seconds: float = Field(gt=0, le=3600)
    method: Literal[
        "local_voiceover",
        "provider_dialogue",
        "local_overlay",
    ]
    speaker_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Optional only so historical signed plans remain readable. New plans are
    # required to supply this field for every local_overlay delivery.
    presentation: OverlayPresentation | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "TimedScriptDelivery":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("script delivery end must be after start")
        return self


class ScriptDeliveryIntent(BaseModel):
    """Director-owned delivery method; runtime owns the exact interval."""

    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(min_length=1, max_length=64)
    method: Literal[
        "local_voiceover",
        "provider_dialogue",
        "local_overlay",
    ]
    speaker_id: str | None = Field(default=None, min_length=1, max_length=128)
    presentation: OverlayPresentation | None = None


class CopyDeliveryProgramDraft(BaseModel):
    """A lossless, timed delivery plan for every approved script line."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    program_id: str = Field(min_length=1, max_length=128)
    director_artifact_sha256: str = Field(min_length=64, max_length=64)
    target_duration_seconds: float = Field(gt=0, le=3600)
    deliveries: list[TimedScriptDelivery] = Field(
        min_length=1,
        max_length=1000,
    )


class DirectorProductionPlanDraft(BaseModel):
    """The Director's complete visual, audio, and copy-delivery decision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "2.0"] = "1.0"
    visual: VisualProgramDraft
    audio: AudioProgramDraft
    copy_delivery: CopyDeliveryProgramDraft


class VisualProgramAuthorDraft(BaseModel):
    """Director-owned visual decisions without copied runtime identity."""

    model_config = ConfigDict(extra="forbid")

    style_language: str = Field(min_length=1, max_length=4000)
    visual_grammar: str = Field(min_length=1, max_length=4000)
    product_presentation_intent: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )
    references: list[VisualReferenceIntent] = Field(
        default_factory=list,
        max_length=1000,
    )
    beats: list[VisualBeat] = Field(min_length=1, max_length=1000)


class AudioProgramAuthorDraft(BaseModel):
    """Director-owned voice and sound choices under runtime audio authority."""

    model_config = ConfigDict(extra="forbid")

    voices: list[SpeakerVoiceIntent] = Field(
        default_factory=list,
        max_length=128,
    )
    cues: list[AudioCue] = Field(default_factory=list, max_length=1000)
    mix_intent: str = Field(min_length=1, max_length=4000)


class CopyDeliveryProgramAuthorDraft(BaseModel):
    """Director-owned lossless delivery choices without runtime timing."""

    model_config = ConfigDict(extra="forbid")

    deliveries: list[ScriptDeliveryIntent] = Field(
        min_length=1,
        max_length=1000,
    )


class DirectorProductionPlanAuthorDraft(BaseModel):
    """V2 author response; runtime materializes all immutable fields."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    visual: VisualProgramAuthorDraft
    audio: AudioProgramAuthorDraft
    copy_delivery: CopyDeliveryProgramAuthorDraft


class ProductionPlanReviewCriterion(BaseModel):
    """One project-owned semantic standard for the production plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    instruction: str = Field(min_length=1, max_length=2000)
    minimum_score: int = Field(ge=0, le=100)
    blocking: bool = True


class DirectedProductionPlan(DirectorProductionPlanDraft):
    """Runtime-signed production plan bound to one approved copy artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=1000)
    parent_plan_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    plan_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_plan_hash(self) -> "DirectedProductionPlan":
        if self.revision == 1 and self.parent_plan_sha256 is not None:
            raise ValueError("production plan revision 1 cannot have a parent")
        if self.revision > 1 and self.parent_plan_sha256 is None:
            raise ValueError(
                "production plan revisions after one require a parent"
            )
        if self.plan_sha256 != production_plan_sha256(
            self,
            plan_id=self.plan_id,
            revision=self.revision,
            parent_plan_sha256=self.parent_plan_sha256,
        ):
            raise ValueError("plan_sha256 does not match production plan")
        return self


def _close(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= _TIMING_TOLERANCE_SECONDS


def _words(value: str) -> list[str]:
    return [match.group(0) for match in _WORD_RE.finditer(str(value or ""))]


def _minimum_line_delivery_seconds(
    line: Any,
    artifact: DirectedContentArtifact,
) -> float:
    rate = (
        artifact.script.speech_rate_wpm
        if line.delivery_mode == "spoken"
        else artifact.script.display_reading_rate_wpm
    )
    seconds = len(_words(line.text)) * 60.0 / float(rate)
    if line.delivery_mode == "spoken":
        # Director preflight permits a five-percent per-segment pacing
        # variance while retaining the strict whole-video word budget.  The
        # production-plan compiler must consume that same accepted contract;
        # otherwise a 26-word ten-second beat approved at 150 WPM is rejected
        # one stage later even though it needs only 156 WPM.  This does not
        # create accumulated slack because every delivery remains inside its
        # immutable provider segment and the global copy budget is unchanged.
        seconds /= 1.0 + _SPOKEN_PACING_TOLERANCE_FRACTION
    return seconds


def _compiled_copy_delivery_windows(
    artifact: DirectedContentArtifact,
) -> dict[str, tuple[float, float]]:
    """Compile provider-segment-safe line windows from immutable copy budgets."""

    line_by_id = {line.line_id: line for line in artifact.script.lines}
    windows: dict[str, tuple[float, float]] = {}
    segment_start = 0.0
    for segment in artifact.script.segments:
        segment_end = segment_start + float(segment.duration_seconds)
        lines = [line_by_id[line_id] for line_id in segment.line_ids]
        requirements = [
            _minimum_line_delivery_seconds(line, artifact)
            for line in lines
        ]
        required_total = sum(requirements)
        available = float(segment.duration_seconds)
        if required_total > available + _TIMING_TOLERANCE_SECONDS:
            raise ValueError(
                "approved script cannot fit registered transport segment "
                f"{segment.segment_index}"
            )
        shared_slack = (
            max(0.0, available - required_total) / len(lines)
            if lines
            else 0.0
        )
        cursor = segment_start
        for position, (line, required) in enumerate(
            zip(lines, requirements, strict=True)
        ):
            end = (
                segment_end
                if position == len(lines) - 1
                else cursor + required + shared_slack
            )
            windows[line.line_id] = (cursor, end)
            cursor = end
        segment_start = segment_end
    return windows


def _compile_copy_delivery_timeline(
    draft: CopyDeliveryProgramAuthorDraft,
    artifact: DirectedContentArtifact,
) -> list[TimedScriptDelivery]:
    """Keep Director transport choices while runtime owns feasible timing."""

    expected = [line.line_id for line in artifact.script.lines]
    actual = [delivery.line_id for delivery in draft.deliveries]
    if actual != expected:
        raise ValueError(
            "copy delivery intents must cover every approved script line "
            "exactly once and in order"
        )
    windows = _compiled_copy_delivery_windows(artifact)
    return [
        TimedScriptDelivery(
            line_id=delivery.line_id,
            start_seconds=windows[delivery.line_id][0],
            end_seconds=windows[delivery.line_id][1],
            method=delivery.method,
            speaker_id=delivery.speaker_id,
            presentation=delivery.presentation,
        )
        for delivery in draft.deliveries
    ]


def _canonical_plan_payload(
    value: DirectorProductionPlanDraft | DirectedProductionPlan,
    *,
    plan_id: str,
    revision: int,
    parent_plan_sha256: str | None,
) -> dict[str, Any]:
    payload = {
        "plan_id": plan_id,
        "revision": int(revision),
        "parent_plan_sha256": parent_plan_sha256,
        "plan": value.model_dump(
            mode="json",
            exclude={
                "plan_id",
                "revision",
                "parent_plan_sha256",
                "plan_sha256",
            },
        ),
    }
    return _canonicalize_plan_hash_numbers(payload)


def _canonicalize_plan_hash_numbers(value: Any) -> Any:
    """Stabilize signed plans across JSON database round trips.

    MySQL JSON stores IEEE-754 numbers but may emit an equivalent float with
    fewer trailing digits (for example ``24.523809523809526`` becomes
    ``24.52380952380953``).  Timing validation already uses a 50 ms tolerance;
    hashing raw binary-float spellings made a freshly approved plan fail its
    signature before media generation.  Nanosecond precision is far tighter
    than any provider or editing boundary and keeps substantive changes fully
    detectable while making the signature storage-stable.
    """

    if isinstance(value, float):
        normalized = round(value, 9)
        return 0.0 if normalized == 0 else normalized
    if isinstance(value, dict):
        return {
            key: _canonicalize_plan_hash_numbers(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _canonicalize_plan_hash_numbers(item)
            for item in value
        ]
    return value


def production_plan_sha256(
    value: DirectorProductionPlanDraft | DirectedProductionPlan,
    *,
    plan_id: str,
    revision: int,
    parent_plan_sha256: str | None,
) -> str:
    canonical = json.dumps(
        _canonical_plan_payload(
            value,
            plan_id=plan_id,
            revision=revision,
            parent_plan_sha256=parent_plan_sha256,
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_common_identity(
    artifact: DirectedContentArtifact,
    draft: DirectorProductionPlanDraft,
) -> None:
    program_id = artifact.program.program_id
    artifact_sha256 = artifact.artifact_sha256
    duration = artifact.script.target_duration_seconds
    for name, component in (
        ("visual", draft.visual),
        ("audio", draft.audio),
        ("copy_delivery", draft.copy_delivery),
    ):
        if component.program_id != program_id:
            raise ValueError(f"{name} changed approved program_id")
        if component.director_artifact_sha256 != artifact_sha256:
            raise ValueError(
                f"{name} belongs to another Director artifact"
            )
        if not _close(component.target_duration_seconds, duration):
            raise ValueError(
                f"{name} changed approved target duration"
            )
    if draft.visual.aspect_ratio != artifact.program.aspect_ratio:
        raise ValueError("visual program changed approved aspect ratio")
    if draft.audio.audio_mode != artifact.script.audio_mode:
        raise ValueError("audio program changed approved audio mode")


def _validate_visual_program(
    artifact: DirectedContentArtifact,
    visual: VisualProgramDraft,
    *,
    authorized_asset_refs: set[str],
    authoritative_product_asset_refs: set[str],
) -> None:
    reference_ids = [item.reference_id for item in visual.references]
    if len(reference_ids) != len(set(reference_ids)):
        raise ValueError("visual reference IDs must be unique")
    reference_by_id = {
        item.reference_id: item for item in visual.references
    }
    for reference in visual.references:
        if not str(reference.generation_brief or "").strip():
            raise ValueError(
                "every visual reference requires a generation brief; "
                "uploaded source assets are guidance or pixel authority, "
                "not generated scene references"
            )
        unauthorized = sorted(
            set(reference.source_asset_refs) - authorized_asset_refs
        )
        if unauthorized:
            raise ValueError(
                "visual reference cites unauthorized source assets: "
                f"{unauthorized}"
            )
        if "product" in reference.roles:
            if not reference.source_asset_refs:
                raise ValueError(
                    "product reference requires an authoritative source asset"
                )
            untrusted_product_refs = sorted(
                set(reference.source_asset_refs)
                - authoritative_product_asset_refs
            )
            if untrusted_product_refs:
                raise ValueError(
                    "product reference must use authoritative product assets: "
                    f"{untrusted_product_refs}"
                )

    beat_ids = [beat.beat_id for beat in visual.beats]
    if len(beat_ids) != len(set(beat_ids)):
        raise ValueError("visual beat IDs must be unique")
    if not _close(visual.beats[0].start_seconds, 0):
        raise ValueError("visual beats must begin at zero")
    previous_end = 0.0
    cited_reference_ids: set[str] = set()
    product_reference_ids = {
        reference.reference_id
        for reference in visual.references
        if "product" in reference.roles
    }
    product_reveal_at = (
        float(artifact.script.target_duration_seconds)
        * float(artifact.program.conversion.reveal_after_fraction)
        if artifact.program.conversion.reveal_after_fraction is not None
        else None
    )
    for beat in visual.beats:
        if not _close(beat.start_seconds, previous_end):
            raise ValueError(
                "visual beats must form one contiguous ordered timeline"
            )
        unknown_references = sorted(
            set(beat.reference_ids) - set(reference_by_id)
        )
        if unknown_references:
            raise ValueError(
                f"visual beat {beat.beat_id} cites unknown references: "
                f"{unknown_references}"
            )
        early_product_references = sorted(
            set(beat.reference_ids) & product_reference_ids
        )
        if (
            early_product_references
            and product_reveal_at is not None
            and beat.start_seconds
            < product_reveal_at - _TIMING_TOLERANCE_SECONDS
        ):
            raise ValueError(
                "product visual references cannot be attached before the "
                "project-owned product reveal boundary; "
                f"beat={beat.beat_id}, references={early_product_references}, "
                f"reveal_at={product_reveal_at:g}"
            )
        cited_reference_ids.update(beat.reference_ids)
        action_text = " ".join(
            (
                str(beat.subject_action or ""),
                str(beat.motion_and_transition or ""),
                str(beat.continuity_state or ""),
            )
        ).casefold()
        sealed_package = bool(re.search(
            r"\b(?:sealed|unopened|closed)\s+"
            r"(?:package|container|bottle|pouch|jar|box|bag)\b"
            r"|\b(?:cap|lid)\s+remains\s+closed\b"
            r"|(?:密封|未开封|保持关闭).{0,8}(?:包装|容器|瓶|袋|盒|盖)",
            action_text,
        ))
        contents_leave_package = bool(
            re.search(
                r"\b(?:pours?|dispenses?|removes?|takes?\s+out)\b"
                r"[^.。!?]{0,100}\b"
                r"(?:contents?|units?|servings?|pieces?|doses?|product)\b",
                action_text,
            )
            or re.search(
                r"\b(?:contents?|units?|servings?|pieces?|doses?)\b"
                r"[^.。!?]{0,60}\b"
                r"(?:spill|fall|emerge|leave|exit)s?\b",
                action_text,
            )
            or re.search(
                r"(?:倒出|取出|掉出|洒出|流出).{0,30}"
                r"(?:内容物|产品|份量|颗粒)"
                r"|(?:内容物|产品|份量|颗粒).{0,30}"
                r"(?:掉出|洒出|流出|离开)",
                action_text,
            )
        )
        opens_package = bool(re.search(
            r"\b(?:opens?|unseals?|unwraps?)\b[^.。!?]{0,60}\b"
            r"(?:package|container|bottle|pouch|jar|box|bag)\b"
            r"|\b(?:removes?|unscrews?|twists?\s+off)\b"
            r"[^.。!?]{0,40}\b(?:cap|lid)\b"
            r"|(?:打开|拧开|揭开|拆封).{0,12}(?:包装|容器|瓶|袋|盒|盖)",
            action_text,
        ))
        if sealed_package and contents_leave_package and not opens_package:
            raise ValueError(
                "visual beat contains an impossible package-state action: "
                f"beat={beat.beat_id}; a sealed or closed package cannot "
                "release contents without an explicit opening action"
            )
        if beat.continuity_dependency == "independent" and any(
            marker in action_text
            for marker in (
                "exact previous frame",
                "continues directly",
                "same continuous action",
                "picks up where",
                "immediately after the previous",
            )
        ):
            raise ValueError(
                "visual beat declares independent transport but its action "
                f"requires the previous segment: beat={beat.beat_id}"
            )
        previous_end = beat.end_seconds
    uncited_references = sorted(set(reference_by_id) - cited_reference_ids)
    if uncited_references:
        raise ValueError(
            "every visual reference must be cited by at least one timed visual "
            f"beat; uncited={uncited_references}"
        )
    if not _close(previous_end, artifact.script.target_duration_seconds):
        raise ValueError("visual beats must end at target duration")

    expected_line_ids = [line.line_id for line in artifact.script.lines]
    visual_line_ids = [
        line_id
        for beat in visual.beats
        for line_id in beat.line_ids
    ]
    if visual_line_ids != expected_line_ids:
        raise ValueError(
            "visual beats must cover every approved script line exactly once "
            "in order"
        )


def _validate_audio_program(
    artifact: DirectedContentArtifact,
    audio: AudioProgramDraft,
    copy_delivery: CopyDeliveryProgramDraft,
) -> None:
    voice_ids = [voice.speaker_id for voice in audio.voices]
    if len(voice_ids) != len(set(voice_ids)):
        raise ValueError("audio speaker voice IDs must be unique")
    cue_ids = [cue.cue_id for cue in audio.cues]
    if len(cue_ids) != len(set(cue_ids)):
        raise ValueError("audio cue IDs must be unique")
    duration = artifact.script.target_duration_seconds
    for cue in audio.cues:
        if cue.end_seconds > duration + _TIMING_TOLERANCE_SECONDS:
            raise ValueError("audio cue exceeds target duration")
    if audio.audio_mode == "silent":
        audible = [
            cue.cue_id for cue in audio.cues if cue.kind != "silence"
        ]
        if audible:
            raise ValueError(
                "silent audio mode cannot declare audible cues: "
                f"{audible}"
            )
    if audio.audio_mode == "music_only":
        non_music = [
            cue.cue_id
            for cue in audio.cues
            if cue.kind not in {"music", "silence"}
        ]
        if non_music:
            raise ValueError(
                "music_only audio mode cannot declare sound effects or "
                f"room tone: {non_music}"
            )

    expected_line_ids = [line.line_id for line in artifact.script.lines]
    delivered_line_ids = [
        delivery.line_id for delivery in copy_delivery.deliveries
    ]
    if delivered_line_ids != expected_line_ids:
        counts = Counter(delivered_line_ids)
        duplicates = sorted(
            line_id for line_id, count in counts.items() if count > 1
        )
        missing = sorted(set(expected_line_ids) - set(delivered_line_ids))
        unknown = sorted(set(delivered_line_ids) - set(expected_line_ids))
        raise ValueError(
            "copy delivery must cover every approved script line exactly once "
            f"in order; missing={missing}, duplicates={duplicates}, "
            f"unknown={unknown}"
        )

    line_by_id = {line.line_id: line for line in artifact.script.lines}
    previous_start = -1.0
    spoken_speakers: set[str] = set()
    for delivery in copy_delivery.deliveries:
        line = line_by_id[delivery.line_id]
        if delivery.start_seconds + _TIMING_TOLERANCE_SECONDS < previous_start:
            raise ValueError("copy deliveries must be ordered by start time")
        if delivery.end_seconds > duration + _TIMING_TOLERANCE_SECONDS:
            raise ValueError("copy delivery exceeds target duration")
        previous_start = delivery.start_seconds
        available_seconds = delivery.end_seconds - delivery.start_seconds
        if line.delivery_mode == "display":
            if delivery.method != "local_overlay":
                raise ValueError(
                    "display copy must use deterministic local overlay"
                )
            if delivery.speaker_id is not None:
                raise ValueError("display copy cannot declare an audio speaker")
            if delivery.presentation is None:
                raise ValueError(
                    "display copy requires a Director-owned overlay presentation"
                )
        else:
            if delivery.method == "local_overlay":
                raise ValueError(
                    "spoken copy requires voiceover or provider dialogue"
                )
            if delivery.presentation is not None:
                raise ValueError(
                    "spoken copy cannot declare an overlay presentation"
                )
            if delivery.speaker_id != line.speaker_id:
                raise ValueError(
                    "spoken copy delivery changed the approved speaker"
                )
            spoken_speakers.add(line.speaker_id)
        if (
            available_seconds + _TIMING_TOLERANCE_SECONDS
            < _minimum_line_delivery_seconds(line, artifact)
        ):
            raise ValueError(
                f"copy delivery interval cannot fit line {line.line_id}"
            )

    if artifact.script.audio_mode == "spoken":
        missing_voices = sorted(spoken_speakers - set(voice_ids))
        if missing_voices:
            raise ValueError(
                "audio program has no continuity voice for speakers: "
                f"{missing_voices}"
            )
    elif audio.voices:
        raise ValueError("non-spoken audio mode cannot declare voices")


def finalize_director_production_plan(
    draft: DirectorProductionPlanDraft,
    artifact: DirectedContentArtifact,
    *,
    plan_id: str,
    revision: int,
    parent_plan_sha256: str | None,
    authorized_asset_refs: set[str],
    authoritative_product_asset_refs: set[str],
) -> DirectedProductionPlan:
    """Validate and sign a provider-independent Director production plan."""

    validation_errors: list[str] = []
    validators = [
        lambda: _validate_common_identity(artifact, draft),
        lambda: _validate_visual_program(
            artifact,
            draft.visual,
            authorized_asset_refs=set(authorized_asset_refs),
            authoritative_product_asset_refs=set(
                authoritative_product_asset_refs
            ),
        ),
        lambda: _validate_audio_program(
            artifact,
            draft.audio,
            draft.copy_delivery,
        ),
    ]
    for validator in validators:
        try:
            validator()
        except ValueError as exc:
            validation_errors.append(str(exc))
    if validation_errors:
        raise ValueError(
            "production plan contract violations: "
            + " | ".join(validation_errors)
        )
    if revision == 1 and parent_plan_sha256 is not None:
        raise ValueError("production plan revision 1 cannot have a parent")
    if revision > 1 and parent_plan_sha256 is None:
        raise ValueError(
            "production plan revisions after one require a parent"
        )
    return DirectedProductionPlan(
        **draft.model_dump(mode="python"),
        plan_id=plan_id,
        revision=revision,
        parent_plan_sha256=parent_plan_sha256,
        plan_sha256=production_plan_sha256(
            draft,
            plan_id=plan_id,
            revision=revision,
            parent_plan_sha256=parent_plan_sha256,
        ),
    )


def finalize_director_production_plan_author_draft(
    draft: DirectorProductionPlanAuthorDraft,
    artifact: DirectedContentArtifact,
    *,
    plan_id: str,
    revision: int,
    parent_plan_sha256: str | None,
    authorized_asset_refs: set[str],
    authoritative_product_asset_refs: set[str],
) -> DirectedProductionPlan:
    """Inject immutable copy identity into an author-only production plan."""

    if artifact.program.audio_mode is None:
        raise ValueError(
            "production planning requires structured audio authority"
        )
    if artifact.program.audio_mode == "spoken":
        incomplete_voices = [
            voice.speaker_id
            for voice in draft.audio.voices
            if (
                voice.gender == "unspecified"
                or voice.screen_relation == "unspecified"
                or not voice.timbre.strip()
                or not voice.pitch.strip()
                or not voice.accent.strip()
            )
        ]
        if incomplete_voices:
            raise ValueError(
                "every spoken voice requires explicit gender, screen_relation, "
                "timbre, pitch, and accent; incomplete speakers="
                f"{sorted(incomplete_voices)}"
            )
    runtime_draft = DirectorProductionPlanDraft(
        schema_version="2.0",
        visual=VisualProgramDraft(
            schema_version="1.0",
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=(
                artifact.script.target_duration_seconds
            ),
            aspect_ratio=artifact.program.aspect_ratio,
            style_language=draft.visual.style_language,
            visual_grammar=draft.visual.visual_grammar,
            product_presentation_intent=(
                draft.visual.product_presentation_intent
            ),
            references=list(draft.visual.references),
            beats=list(draft.visual.beats),
        ),
        audio=AudioProgramDraft(
            schema_version="1.0",
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=(
                artifact.script.target_duration_seconds
            ),
            audio_mode=artifact.program.audio_mode,
            voices=list(draft.audio.voices),
            cues=list(draft.audio.cues),
            mix_intent=draft.audio.mix_intent,
        ),
        copy_delivery=CopyDeliveryProgramDraft(
            schema_version="1.0",
            program_id=artifact.program.program_id,
            director_artifact_sha256=artifact.artifact_sha256,
            target_duration_seconds=(
                artifact.script.target_duration_seconds
            ),
            deliveries=_compile_copy_delivery_timeline(
                draft.copy_delivery,
                artifact,
            ),
        ),
    )
    return finalize_director_production_plan(
        runtime_draft,
        artifact,
        plan_id=plan_id,
        revision=revision,
        parent_plan_sha256=parent_plan_sha256,
        authorized_asset_refs=authorized_asset_refs,
        authoritative_product_asset_refs=(
            authoritative_product_asset_refs
        ),
    )


def production_plan_author_output_contract(
    artifact: DirectedContentArtifact,
    *,
    authorized_asset_refs: list[str],
) -> dict[str, Any]:
    """Return a script-bound schema containing creative decisions only."""

    output_contract = DirectorProductionPlanAuthorDraft.model_json_schema()
    root_properties = output_contract.get("properties", {})
    version_schema = root_properties.get("schema_version")
    if isinstance(version_schema, dict):
        version_schema["const"] = "2.0"
        version_schema["default"] = "2.0"
    root_required = list(output_contract.get("required") or [])
    if "schema_version" not in root_required:
        root_required.append("schema_version")
    output_contract["required"] = root_required

    definitions = output_contract.get("$defs", {})
    line_ids = [line.line_id for line in artifact.script.lines]
    visual_line_ids = (
        definitions.get("VisualBeat", {})
        .get("properties", {})
        .get("line_ids")
    )
    if isinstance(visual_line_ids, dict):
        items = visual_line_ids.get("items")
        if isinstance(items, dict):
            items["enum"] = line_ids
    delivery_line_id = (
        definitions.get("ScriptDeliveryIntent", {})
        .get("properties", {})
        .get("line_id")
    )
    if isinstance(delivery_line_id, dict):
        delivery_line_id["enum"] = line_ids

    source_asset_refs = (
        definitions.get("VisualReferenceIntent", {})
        .get("properties", {})
        .get("source_asset_refs")
    )
    if isinstance(source_asset_refs, dict):
        items = source_asset_refs.get("items")
        if isinstance(items, dict):
            items["enum"] = list(authorized_asset_refs)

    audio_mode = artifact.program.audio_mode
    allowed_cue_kinds = {
        "spoken": ["music", "sound_effect", "room_tone", "silence"],
        "silent": ["silence"],
        "music_only": ["music", "silence"],
        "sound_design": ["sound_effect", "room_tone", "silence"],
    }.get(str(audio_mode), [])
    cue_kind = (
        definitions.get("AudioCue", {})
        .get("properties", {})
        .get("kind")
    )
    if isinstance(cue_kind, dict):
        cue_kind["enum"] = allowed_cue_kinds

    voices_schema = (
        definitions.get("AudioProgramAuthorDraft", {})
        .get("properties", {})
        .get("voices")
    )
    spoken_speaker_ids = list(dict.fromkeys(
        line.speaker_id
        for line in artifact.script.lines
        if line.delivery_mode == "spoken"
    ))
    if isinstance(voices_schema, dict):
        if audio_mode == "spoken":
            voices_schema["minItems"] = len(spoken_speaker_ids)
            voices_schema["maxItems"] = len(spoken_speaker_ids)
        else:
            voices_schema["maxItems"] = 0
    speaker_id_schema = (
        definitions.get("SpeakerVoiceIntent", {})
        .get("properties", {})
        .get("speaker_id")
    )
    if isinstance(speaker_id_schema, dict):
        speaker_id_schema["enum"] = spoken_speaker_ids
    voice_definition = definitions.get("SpeakerVoiceIntent", {})
    if isinstance(voice_definition, dict):
        required_voice_fields = list(voice_definition.get("required") or [])
        for field_name in (
            "speaker_id",
            "identity",
            "gender",
            "screen_relation",
            "timbre",
            "pitch",
            "accent",
            "delivery_direction",
            "continuity_rule",
        ):
            if field_name not in required_voice_fields:
                required_voice_fields.append(field_name)
        voice_definition["required"] = required_voice_fields
    visual_beat_definition = definitions.get("VisualBeat", {})
    if isinstance(visual_beat_definition, dict):
        required_beat_fields = list(
            visual_beat_definition.get("required") or []
        )
        if "continuity_dependency" not in required_beat_fields:
            required_beat_fields.append("continuity_dependency")
        visual_beat_definition["required"] = required_beat_fields
    return output_contract


def build_director_production_plan_packet(
    artifact: DirectedContentArtifact,
    *,
    capability_catalog: list[dict[str, Any]],
    authorized_asset_refs: list[str],
    authoritative_product_asset_refs: list[str],
) -> dict[str, Any]:
    """Build a strict planning packet without prescribing a story template."""

    output_contract = production_plan_author_output_contract(
        artifact,
        authorized_asset_refs=authorized_asset_refs,
    )
    definitions = output_contract.get("$defs", {})
    line_ids = [line.line_id for line in artifact.script.lines]
    for definition_name, field_name in (
        ("VisualBeat", "line_ids"),
        ("ScriptDeliveryIntent", "line_id"),
    ):
        field_schema = definitions.get(definition_name, {}).get(
            "properties",
            {},
        ).get(field_name)
        if not isinstance(field_schema, dict):
            continue
        if field_name == "line_ids":
            items = field_schema.get("items")
            if isinstance(items, dict):
                items["enum"] = line_ids
        else:
            field_schema["enum"] = line_ids
    source_asset_schema = definitions.get(
        "VisualReferenceIntent",
        {},
    ).get("properties", {}).get("source_asset_refs")
    if isinstance(source_asset_schema, dict):
        items = source_asset_schema.get("items")
        if isinstance(items, dict):
            items["enum"] = list(authorized_asset_refs)

    segment_windows: dict[str, dict[str, Any]] = {}
    segment_start = 0.0
    for segment in artifact.script.segments:
        segment_end = segment_start + float(segment.duration_seconds)
        for line_id in segment.line_ids:
            segment_windows[line_id] = {
                "segment_index": int(segment.segment_index),
                "window_start_seconds": segment_start,
                "window_end_seconds": segment_end,
            }
        segment_start = segment_end
    line_delivery_contract = []
    compiled_windows = _compiled_copy_delivery_windows(artifact)
    for line in artifact.script.lines:
        word_count = len(_words(line.text))
        line_delivery_contract.append({
            "line_id": line.line_id,
            "speaker_id": line.speaker_id,
            "delivery_mode": line.delivery_mode,
            "text": line.text,
            "word_count": word_count,
            "minimum_delivery_seconds": round(
                _minimum_line_delivery_seconds(line, artifact),
                2,
            ),
            "registered_transport_window": segment_windows[line.line_id],
            "runtime_compiled_interval_seconds": {
                "start_seconds": compiled_windows[line.line_id][0],
                "end_seconds": compiled_windows[line.line_id][1],
            },
        })

    return {
        "schema_version": "2.0",
        "role": "content_director_production_plan",
        "approved_director_artifact": artifact.model_dump(mode="json"),
        "immutable_line_sequence": [
            line.line_id for line in artifact.script.lines
        ],
        "line_delivery_contract": line_delivery_contract,
        "production_capabilities": list(capability_catalog),
        "asset_authority": {
            "authorized_asset_refs": list(authorized_asset_refs),
            "authoritative_product_asset_refs": list(
                authoritative_product_asset_refs
            ),
        },
        "planning_rules": {
            "choose_visual_beat_count_from_the_approved_program": True,
            "do_not_copy_a_server_story_template": True,
            "visual_beats_are_independent_of_provider_segment_count": True,
            "each_visual_beat_declares_whether_its_transport_segment_needs_the_previous_segment_final_frame": True,
            "independent_is_allowed_only_when_character_scene_action_and_product_state_do_not_depend_on_the_immediately_previous_segment": True,
            "visual_timeline_starts_at_zero": True,
            "visual_beats_are_contiguous_without_gaps_or_overlaps": True,
            "visual_timeline_ends_at_target_duration": True,
            "cover_every_script_line_once_in_order": True,
            "line_ids_are_single_owner_markers_not_repeated_context": True,
            "meet_each_line_minimum_delivery_seconds": True,
            "runtime_compiles_exact_copy_delivery_intervals": True,
            "prefer_the_registered_transport_window_for_each_line": True,
            "critical_display_copy_uses_local_overlay": True,
            "every_visual_reference_is_a_new_generated_scene_reference": True,
            "every_visual_reference_is_cited_by_a_timed_visual_beat": True,
            "source_assets_are_guidance_or_pixel_authority_not_reference_rows": True,
            "every_local_overlay_has_director_owned_presentation": True,
            "audio_mode_semantics": {
                **AUDIO_MODE_SEMANTICS,
            },
            "product_references_use_authoritative_assets": True,
            "product_references_use_uploaded_package_as_visual_authority": True,
            "product_is_rendered_naturally_in_scene_by_media_model": True,
            "product_reference_not_before_reveal_seconds": (
                round(
                    float(artifact.script.target_duration_seconds)
                    * float(
                        artifact.program.conversion.reveal_after_fraction
                    ),
                    3,
                )
                if artifact.program.conversion.reveal_after_fraction
                is not None
                else None
            ),
            "provider_upload_selection_happens_after_planning": True,
            "return_raw_json_only": True,
        },
        "runtime_owned_fields": [
            "program_id",
            "director_artifact_sha256",
            "target_duration_seconds",
            "aspect_ratio",
            "audio_mode",
            "integrity hashes",
        ],
        "output_contract": output_contract,
    }


__all__ = [
    "AudioCue",
    "AudioProgramDraft",
    "AuthoritativeProductComposite",
    "CopyDeliveryProgramDraft",
    "DirectedProductionPlan",
    "DirectorProductionPlanDraft",
    "DirectorProductionPlanAuthorDraft",
    "VisualProgramAuthorDraft",
    "AudioProgramAuthorDraft",
    "CopyDeliveryProgramAuthorDraft",
    "ProductionPlanReviewCriterion",
    "ScriptDeliveryIntent",
    "SpeakerVoiceIntent",
    "TimedScriptDelivery",
    "OverlayPresentation",
    "VisualBeat",
    "VisualProgramDraft",
    "VisualReferenceIntent",
    "build_director_production_plan_packet",
    "finalize_director_production_plan",
    "finalize_director_production_plan_author_draft",
    "production_plan_author_output_contract",
    "production_plan_sha256",
]
