from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
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

def _load_unbound_product_visual_terms() -> str:
    """Load category-neutral visual terms from versioned policy data."""

    path = Path(__file__).with_name("content_product_visual_terms.v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    terms = [
        str(value).strip()
        for value in list(payload.get("unbound_visual_regex_terms") or [])
        if str(value).strip()
    ]
    if not terms:
        raise RuntimeError(f"empty unbound product visual term policy: {path}")
    return "(?:" + "|".join(terms) + ")"


# A product-unbound project may preserve product words supplied in immutable
# narration, but those words are not visual authority. Detect product-form
# depictions using versioned category policy rather than campaign vocabulary.
_UNBOUND_PRODUCT_FORM = _load_unbound_product_visual_terms()
_UNBOUND_PRODUCT_VISUAL_TERM = re.compile(
    rf"\b{_UNBOUND_PRODUCT_FORM}\b",
    flags=re.IGNORECASE,
)
_UNBOUND_BRAND_VISUAL_TERM = re.compile(
    r"\b(?i:show|depict|feature|position|place|reveal|present|display|"
    r"introduce|highlight|center|focus\s+on)\s+(?:the\s+)?"
    r"(?:brand\s+)?[A-Z][A-Z0-9&._-]{2,}\b"
)
_UNBOUND_PRODUCT_VISUAL_EXCLUSION = re.compile(
    r"\bpre[- ]product\b|"
    r"\b(?:no|without|exclude(?:s|d|ing)?|omit(?:s|ted|ting)?|avoid(?:s|ed|ing)?)\b"
    r"(?:(?!\b(?:but|however|except|instead|only)\b)[^.;!?]){0,120}"
    rf"\b{_UNBOUND_PRODUCT_FORM}\b|"
    r"\b(?:do\s+not|does\s+not|don't|doesn't|never)\s+"
    r"(?:show|depict|display|include|add|create|generate|feature|visualize|"
    r"reveal|present|introduce)\b"
    r"(?:(?!\b(?:but|however|except|instead|only)\b|[.;!?]).){0,100}"
    rf"\b{_UNBOUND_PRODUCT_FORM}\b|"
    r"\b(?:product|package|packaging)\s*(?:words?|mentions?)?\s+"
    r"(?:remain|stay|are|is|must\s+remain)\b(?:(?![.;!?]).){0,80}"
    r"\b(?:narration|voiceover|spoken\s+copy|audio)[- ]only\b|"
    r"\b(?:non[- ]?products?|products?[- ]free)\b|"
    rf"\b{_UNBOUND_PRODUCT_FORM}\b"
    r"(?:(?![.;!?]).){0,100}\b(?:narration|voiceover|spoken\s+copy|audio)"
    r"[- ]only\b|"
    r"\b(?:rather\s+than|instead\s+of)\b(?:(?![.;!?]).){0,120}"
    rf"\b{_UNBOUND_PRODUCT_FORM}\b|"
    rf"\b(?:{_UNBOUND_PRODUCT_FORM}|"
    r"product\s+identity)\b(?:(?![.;!?]).){0,50}"
    r"\b(?:is|are|remains?|stays?)\s+(?:fully\s+)?"
    r"(?:absent|missing|hidden|off[- ]screen|out\s+of\s+frame|not\s+visible)\b|"
    rf"\b(?:{_UNBOUND_PRODUCT_FORM}|"
    r"product\s+identity)\b(?:(?![.;!?]).){0,50}"
    r"\b(?:has|have|had)\s+not\s+(?:yet\s+)?"
    r"(?:appeared|entered|been\s+(?:shown|revealed|displayed|introduced))\b"
    r"(?:\s+yet)?|"
    rf"\bbefore\b(?:(?![.;!?]).){{0,80}}\b(?:{_UNBOUND_PRODUCT_FORM}|product\s+(?:identity|visual))\b"
    r"(?:(?![.;!?]).){0,40}\b(?:appears?|enters?|is\s+shown|is\s+revealed)\b|"
    r"\bbefore\b(?:(?![.;!?]).){0,80}\b(?:showing|displaying|depicting|"
    r"featuring|introducing|revealing)\b(?:(?![.;!?]).){0,40}"
    rf"\b(?:any\s+|the\s+)?(?:{_UNBOUND_PRODUCT_FORM}|product\s+(?:identity|visual))\b|"
    r"\b(?:next|following|later|subsequent)\s+"
    r"(?:beat|shot|scene|segment)\b(?:(?![.;!?]).){0,100}"
    rf"\b(?:{_UNBOUND_PRODUCT_FORM}|product\s+(?:identity|visual|reveal))\b|"
    r"\b(?:free|clear)\s+of\b(?:(?![.;!?]).){0,50}"
    rf"\b{_UNBOUND_PRODUCT_FORM}\b|"
    r"\u4e0d\u5c55\u793a\u4ea7\u54c1|\u4e0d\u8981\u51fa\u73b0\u4ea7\u54c1|"
    r"\u4ea7\u54c1\u4ec5\u4fdd\u7559\u5728\u53e3\u64ad|\u65e0\u4ea7\u54c1",
    flags=re.IGNORECASE,
)
_UNBOUND_PRODUCT_NEGATIVE_SCOPE_START = re.compile(
    r"\b(?:no|without|avoid(?:s|ed|ing)?|exclude(?:s|d|ing)?|"
    r"omit(?:s|ted|ting)?|rather\s+than|instead\s+of)\b|"
    r"\b(?:do\s+not|does\s+not|don't|doesn't|never)\s+"
    r"(?:show|depict|display|include|add|create|generate|feature|visualize|"
    r"reveal|present|introduce)\b|"
    r"(?:\u65e0|\u4e0d(?:\u8981|\u518d)?(?:\u5c55\u793a|\u51fa\u73b0|\u663e\u793a|\u5305\u542b|\u52a0\u5165|\u751f\u6210|\u5448\u73b0|\u9732\u51fa)?|"
    r"\u907f\u514d|\u6392\u9664|\u7701\u7565)(?:\u4efb\u4f55|\u771f\u5b9e|\u53ef\u89c1)?",
    flags=re.IGNORECASE,
)
_UNBOUND_PRODUCT_POSITIVE_VISUAL_VERB = re.compile(
    r"\b(?:show|depict|display|include|add|create|generate|feature|"
    r"visualize|position|place|reveal|present|introduce|highlight|center)\b",
    flags=re.IGNORECASE,
)


def _unbound_product_term_is_excluded(
    clause: str,
    match: re.Match[str],
    exclusion_spans: list[tuple[int, int]],
) -> bool:
    if any(
        start <= match.start() and match.end() <= end
        for start, end in exclusion_spans
    ):
        return True

    prefix = clause[: match.start()]
    suffix = clause[match.end():]
    if re.search(r"\bnon[- ]?$", prefix, flags=re.IGNORECASE):
        return True
    if re.search(
        r"(?:\u65e0|\u4e0d(?:\u8981|\u518d)?(?:\u5c55\u793a|\u51fa\u73b0|\u663e\u793a|\u5305\u542b|\u52a0\u5165|\u751f\u6210|\u5448\u73b0|\u9732\u51fa)?|"
        r"\u907f\u514d|\u6392\u9664|\u7701\u7565)(?:\u4efb\u4f55|\u771f\u5b9e|\u53ef\u89c1)?$",
        prefix[-32:],
    ):
        return True
    if re.match(r"[- ]free\b", suffix, flags=re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:narration|voiceover|spoken\s+copy|audio)[- ]only\b",
        suffix[:100],
        flags=re.IGNORECASE,
    ):
        return True

    starts = list(_UNBOUND_PRODUCT_NEGATIVE_SCOPE_START.finditer(prefix))
    if not starts:
        return False
    scoped_prefix = prefix[starts[-1].end():]
    if re.search(
        r"\b(?:but|however|except)\b",
        scoped_prefix,
        flags=re.IGNORECASE,
    ):
        return False
    return _UNBOUND_PRODUCT_POSITIVE_VISUAL_VERB.search(scoped_prefix) is None


def unbound_product_visual_depiction_evidence(value: Any) -> str | None:
    """Return evidence when product-free media positively depicts a product.

    Negative policy clauses remain legal. Splitting at sentence and semicolon
    Boundaries are deliberate: one clause may exclude packaging while the next
    positively depicts an unverified loose product form.
    """

    text = " ".join(str(value or "").split())
    if not text:
        return None
    for clause in re.split(r"(?<=[.!?])\s+|[;|\n]+", text):
        clause = clause.strip()
        if not clause:
            continue
        generic_matches = list(_UNBOUND_PRODUCT_VISUAL_TERM.finditer(clause))
        brand_match = _UNBOUND_BRAND_VISUAL_TERM.search(clause)
        if not generic_matches and brand_match is None:
            continue
        exclusion_spans = [
            match.span()
            for match in _UNBOUND_PRODUCT_VISUAL_EXCLUSION.finditer(clause)
        ]
        positive_generic_match = next((
            match
            for match in generic_matches
            if not _unbound_product_term_is_excluded(
                clause,
                match,
                exclusion_spans,
            )
        ), None)
        positive_brand_match = brand_match
        if brand_match is not None:
            prefix = clause[: brand_match.start()]
            if re.search(
                r"\b(?:no|not|never|without|avoid|exclude|omit)\b"
                r"[^.;!?]{0,60}$",
                prefix,
                flags=re.IGNORECASE,
            ):
                positive_brand_match = None
        if positive_generic_match is not None or positive_brand_match is not None:
            return clause[:320]
    return None


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
    requirement_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_interval(self) -> "VisualBeat":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("visual beat end must be after start")
        if len(self.line_ids) != len(set(self.line_ids)):
            raise ValueError("visual beat line IDs must be unique")
        if len(self.reference_ids) != len(set(self.reference_ids)):
            raise ValueError("visual beat reference IDs must be unique")
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("visual beat requirement IDs must be unique")
        return self


class ProductionRequirementMapping(BaseModel):
    """Where the production plan makes a Director intent observable."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(pattern=r"^R-[0-9]{3}$")
    beat_ids: list[str] = Field(default_factory=list, max_length=1000)
    reference_ids: list[str] = Field(default_factory=list, max_length=1000)
    audio_cue_ids: list[str] = Field(default_factory=list, max_length=1000)
    line_ids: list[str] = Field(default_factory=list, max_length=1000)
    implementation_evidence: list[str] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _coordinates(self) -> "ProductionRequirementMapping":
        for field_name in (
            "beat_ids",
            "reference_ids",
            "audio_cue_ids",
            "line_ids",
        ):
            values = list(getattr(self, field_name))
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if not any((self.beat_ids, self.reference_ids, self.audio_cue_ids, self.line_ids)):
            raise ValueError("production requirement mapping needs a plan coordinate")
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
    requirement_execution: list[ProductionRequirementMapping] = Field(
        default_factory=list,
        max_length=128,
    )


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
    requirement_execution: list[ProductionRequirementMapping] = Field(
        default_factory=list,
        max_length=128,
    )


_AUTHOR_PLACEMENT_ALIASES = {
    "upper_third": "top_safe",
    "top_third": "top_safe",
    "top": "top_safe",
    "bottom_third": "bottom_safe",
    "bottom": "bottom_safe",
}


def normalize_production_plan_author_payload(value: Any) -> Any:
    """Normalize harmless schema spelling without rewriting creative intent.

    Model providers occasionally emit a human label where the signed JSON
    schema requires a machine identifier (for example ``ref product reveal``)
    or use a common placement synonym such as ``upper_third``.  Rejecting and
    regenerating the entire creative plan for those two transport-level
    spellings wastes model calls and can regress otherwise complete output.

    The normalization is deliberately narrow: it changes no script, timing,
    action, camera, product statement, or requirement evidence.  Reference ID
    rewrites are applied consistently to every beat and requirement mapping;
    unknown placement values continue to fail closed in Pydantic.
    """

    if not isinstance(value, dict):
        return value
    payload = copy.deepcopy(value)
    visual = payload.get("visual")
    if not isinstance(visual, dict):
        return payload

    references = visual.get("references")
    id_map: dict[str, str] = {}
    used_ids: set[str] = set()
    if isinstance(references, list):
        for index, reference in enumerate(references, start=1):
            if not isinstance(reference, dict):
                continue
            original = str(reference.get("reference_id") or "").strip()
            normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "_", original)
            normalized = normalized.strip("_.-") or f"reference_{index}"
            candidate = normalized[:128]
            suffix = 2
            while candidate in used_ids:
                suffix_text = f"_{suffix}"
                candidate = f"{normalized[:128-len(suffix_text)]}{suffix_text}"
                suffix += 1
            used_ids.add(candidate)
            reference["reference_id"] = candidate
            if original:
                id_map[original] = candidate

    def rewrite_reference_ids(container: Any) -> None:
        if not isinstance(container, list):
            return
        for item in container:
            if not isinstance(item, dict):
                continue
            raw_ids = item.get("reference_ids")
            if not isinstance(raw_ids, list):
                continue
            item["reference_ids"] = [
                id_map.get(str(reference_id).strip(), str(reference_id).strip())
                for reference_id in raw_ids
            ]

    rewrite_reference_ids(visual.get("beats"))
    rewrite_reference_ids(payload.get("requirement_execution"))

    # A requirement mapping is the model's explicit semantic claim that the
    # cited beat executes that requirement.  Mirror that already-authored
    # relationship onto the beat's reverse index instead of spending another
    # model call asking it to duplicate the same identifier.  Unknown beat IDs
    # and unknown requirement IDs still fail in the normal validators.
    beats = visual.get("beats")
    beat_by_id = {
        str(beat.get("beat_id") or "").strip(): beat
        for beat in beats
        if isinstance(beat, dict)
        and str(beat.get("beat_id") or "").strip()
    } if isinstance(beats, list) else {}
    requirement_execution = payload.get("requirement_execution")
    if isinstance(requirement_execution, list):
        for mapping in requirement_execution:
            if not isinstance(mapping, dict):
                continue
            requirement_id = str(
                mapping.get("requirement_id") or ""
            ).strip()
            if not requirement_id:
                continue
            for beat_id in list(mapping.get("beat_ids") or []):
                beat = beat_by_id.get(str(beat_id or "").strip())
                if beat is None:
                    continue
                requirement_ids = [
                    str(value).strip()
                    for value in list(beat.get("requirement_ids") or [])
                    if str(value).strip()
                ]
                if requirement_id not in requirement_ids:
                    requirement_ids.append(requirement_id)
                beat["requirement_ids"] = requirement_ids

    copy_delivery = payload.get("copy_delivery")
    deliveries = (
        copy_delivery.get("deliveries")
        if isinstance(copy_delivery, dict)
        else None
    )
    if isinstance(deliveries, list):
        for delivery in deliveries:
            if not isinstance(delivery, dict):
                continue
            presentation = delivery.get("presentation")
            if not isinstance(presentation, dict):
                continue
            placement = str(presentation.get("placement") or "").strip().lower()
            if placement in _AUTHOR_PLACEMENT_ALIASES:
                presentation["placement"] = _AUTHOR_PLACEMENT_ALIASES[placement]
    return payload


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
    priority: Literal["critical", "high", "normal"] = "high"


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
    *,
    segment_duration_seconds: float | None = None,
) -> float:
    rate = (
        artifact.script.speech_rate_wpm
        if line.delivery_mode == "spoken"
        else artifact.script.display_reading_rate_wpm
    )
    seconds = len(_words(line.text)) * 60.0 / float(rate)
    if line.delivery_mode == "spoken":
        # Director preflight permits five percent rounded to at least one
        # whole word per provider segment.  On a four-second tail segment that
        # one-word floor is intentionally more than five percent.  Consume
        # that exact accepted contract here; a fixed 1.05 divisor made the
        # next stage reject copy the Director had already proved feasible.
        tolerance_fraction = _SPOKEN_PACING_TOLERANCE_FRACTION
        if segment_duration_seconds is not None:
            base_words = int(
                float(segment_duration_seconds) * float(rate) / 60.0
            )
            if base_words > 0:
                tolerance_words = max(
                    1,
                    int(math.floor(base_words * _SPOKEN_PACING_TOLERANCE_FRACTION)),
                )
                tolerance_fraction = tolerance_words / float(base_words)
        seconds /= 1.0 + tolerance_fraction
    return seconds


def _compiled_copy_delivery_windows(
    artifact: DirectedContentArtifact,
) -> dict[str, tuple[float, float]]:
    """Compile provider-segment-safe line windows from immutable copy budgets.

    Spoken copy and deterministic display overlays are independent delivery
    lanes.  They may intentionally run at the same time (for example, a
    narrator speaks while a short emphasis caption is visible).  Director
    preflight budgets those lanes independently, so the production compiler
    must not serialize their reading times and reject an already-approved
    script one stage later.
    """

    line_by_id = {line.line_id: line for line in artifact.script.lines}
    windows: dict[str, tuple[float, float]] = {}
    segment_start = 0.0
    for segment in artifact.script.segments:
        segment_end = segment_start + float(segment.duration_seconds)
        lines = [line_by_id[line_id] for line_id in segment.line_ids]
        available = float(segment.duration_seconds)
        for delivery_mode in ("spoken", "display"):
            lane_lines = [
                line
                for line in lines
                if line.delivery_mode == delivery_mode
            ]
            if not lane_lines:
                continue
            requirements = [
                _minimum_line_delivery_seconds(
                    line,
                    artifact,
                    segment_duration_seconds=float(segment.duration_seconds),
                )
                for line in lane_lines
            ]
            required_total = sum(requirements)
            if required_total > available + _TIMING_TOLERANCE_SECONDS:
                raise ValueError(
                    "PRODUCTION_PLAN_COPY_TRANSPORT_CONTRACT_INVALID: "
                    "approved script cannot fit registered transport segment "
                    f"{segment.segment_index} {delivery_mode} lane"
                )
            shared_slack = max(0.0, available - required_total) / len(
                lane_lines
            )
            cursor = segment_start
            for position, (line, required) in enumerate(
                zip(lane_lines, requirements, strict=True)
            ):
                end = (
                    segment_end
                    if position == len(lane_lines) - 1
                    else cursor + required + shared_slack
                )
                windows[line.line_id] = (cursor, end)
                cursor = end
        segment_start = segment_end
    return windows


def build_runtime_line_delivery_contract(
    artifact: DirectedContentArtifact,
) -> list[dict[str, Any]]:
    """Expose the exact timing facts used by the runtime compiler.

    Both the production-plan author and its independent critic must reason
    from the same tokenizer, pacing tolerance and compiled intervals.  If a
    model independently splits contractions or punctuation, it can otherwise
    reject copy that the deterministic Director gate already proved fits.
    """
    segment_windows: dict[str, dict[str, Any]] = {}
    segment_duration_by_line_id: dict[str, float] = {}
    segment_start = 0.0
    for segment in artifact.script.segments:
        segment_end = segment_start + float(segment.duration_seconds)
        for line_id in segment.line_ids:
            segment_duration_by_line_id[line_id] = float(
                segment.duration_seconds
            )
            segment_windows[line_id] = {
                "segment_index": int(segment.segment_index),
                "window_start_seconds": segment_start,
                "window_end_seconds": segment_end,
            }
        segment_start = segment_end
    compiled_windows = _compiled_copy_delivery_windows(artifact)
    contract: list[dict[str, Any]] = []
    for line in artifact.script.lines:
        compiled_start, compiled_end = compiled_windows[line.line_id]
        contract.append({
            "line_id": line.line_id,
            "speaker_id": line.speaker_id,
            "delivery_mode": line.delivery_mode,
            "text": line.text,
            # Keep the original author-packet key for compatibility while
            # naming the same value explicitly for critic authority.
            "word_count": len(_words(line.text)),
            "runtime_word_count": len(_words(line.text)),
            "minimum_delivery_seconds": round(
                _minimum_line_delivery_seconds(
                    line,
                    artifact,
                    segment_duration_seconds=segment_duration_by_line_id[
                        line.line_id
                    ],
                ),
                2,
            ),
            "registered_transport_window": segment_windows[line.line_id],
            "runtime_compiled_interval_seconds": {
                "start_seconds": compiled_start,
                "end_seconds": compiled_end,
            },
            "runtime_compiled_duration_seconds": round(
                compiled_end - compiled_start,
                3,
            ),
        })
    return contract


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
    require_visual_references: bool,
) -> None:
    if require_visual_references and not visual.references:
        raise ValueError(
            "image-to-video production requires at least one visual reference"
        )
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
    segment_duration_by_line_id = {
        line_id: float(segment.duration_seconds)
        for segment in artifact.script.segments
        for line_id in segment.line_ids
    }
    # Spoken audio and deterministic display overlays are independent lanes.
    # The immutable deliveries remain in approved script order, while their
    # compiled intervals may overlap or move backwards globally when the next
    # script row belongs to the other lane.  Enforce chronology inside each
    # lane instead of imposing a contradictory global sort requirement.
    previous_start_by_lane = {
        "spoken": -1.0,
        "display": -1.0,
    }
    spoken_speakers: set[str] = set()
    for delivery in copy_delivery.deliveries:
        line = line_by_id[delivery.line_id]
        lane = str(line.delivery_mode)
        if (
            delivery.start_seconds + _TIMING_TOLERANCE_SECONDS
            < previous_start_by_lane[lane]
        ):
            raise ValueError(
                "copy deliveries must be ordered by start time within each "
                f"delivery lane: {lane}"
            )
        if delivery.end_seconds > duration + _TIMING_TOLERANCE_SECONDS:
            raise ValueError("copy delivery exceeds target duration")
        previous_start_by_lane[lane] = delivery.start_seconds
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
            < _minimum_line_delivery_seconds(
                line,
                artifact,
                segment_duration_seconds=segment_duration_by_line_id[
                    line.line_id
                ],
            )
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


def _validate_requirement_execution_plan(
    artifact: DirectedContentArtifact,
    draft: DirectorProductionPlanDraft,
) -> None:
    expected_ids = {
        item.requirement_id
        for item in artifact.program.requirement_execution
    }
    mappings = list(draft.requirement_execution)
    actual_ids = [item.requirement_id for item in mappings]
    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("production requirement mappings must be unique")
    if set(actual_ids) != expected_ids:
        raise ValueError(
            "production plan must map every Director requirement exactly once: "
            f"expected={sorted(expected_ids)} actual={sorted(set(actual_ids))}"
        )
    valid_beats = {item.beat_id for item in draft.visual.beats}
    valid_references = {item.reference_id for item in draft.visual.references}
    valid_cues = {item.cue_id for item in draft.audio.cues}
    valid_lines = {item.line_id for item in artifact.script.lines}
    beat_requirement_ids = {
        requirement_id
        for beat in draft.visual.beats
        for requirement_id in beat.requirement_ids
    }
    unknown_beat_requirements = sorted(beat_requirement_ids - expected_ids)
    if unknown_beat_requirements:
        raise ValueError(
            "visual beats cite unknown requirement IDs: "
            f"{unknown_beat_requirements}"
        )
    for mapping in mappings:
        coordinates = (
            ("beat", set(mapping.beat_ids), valid_beats),
            ("reference", set(mapping.reference_ids), valid_references),
            ("audio cue", set(mapping.audio_cue_ids), valid_cues),
            ("line", set(mapping.line_ids), valid_lines),
        )
        for label, selected, valid in coordinates:
            unknown = sorted(selected - valid)
            if unknown:
                raise ValueError(
                    f"requirement {mapping.requirement_id} cites unknown "
                    f"{label} IDs: {unknown}"
                )
        if mapping.beat_ids and mapping.requirement_id not in beat_requirement_ids:
            raise ValueError(
                f"requirement {mapping.requirement_id} maps visual beats but "
                "those beats do not carry the requirement ID"
            )


def finalize_director_production_plan(
    draft: DirectorProductionPlanDraft,
    artifact: DirectedContentArtifact,
    *,
    plan_id: str,
    revision: int,
    parent_plan_sha256: str | None,
    authorized_asset_refs: set[str],
    authoritative_product_asset_refs: set[str],
    require_visual_references: bool = True,
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
            require_visual_references=require_visual_references,
        ),
        lambda: _validate_audio_program(
            artifact,
            draft.audio,
            draft.copy_delivery,
        ),
        lambda: _validate_requirement_execution_plan(artifact, draft),
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
    require_visual_references: bool = True,
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
    # The accepted Director artifact already owns every immutable script line
    # and its feasible runtime delivery window.  Production authors choose
    # visual meaning intervals, but a duplicated or omitted line_id must not
    # erase approved copy or consume another model retry.  Bind each line to
    # the visual interval with the greatest temporal overlap; the independent
    # critic still judges whether that interval actually expresses the line.
    line_windows = _compiled_copy_delivery_windows(artifact)
    materialized_beats: list[VisualBeat] = []
    line_ids_by_beat: list[list[str]] = [
        [] for _ in draft.visual.beats
    ]
    minimum_beat_index = 0
    for line in artifact.script.lines:
        line_start, line_end = line_windows[line.line_id]
        overlaps = [
            max(
                0.0,
                min(float(beat.end_seconds), line_end)
                - max(float(beat.start_seconds), line_start),
            )
            for beat in draft.visual.beats
        ]
        # Spoken copy and display overlays occupy independent timing lanes.
        # Their compiled windows can overlap, and a model revision may split
        # the visual intervals at slightly different boundaries.  Choosing
        # each line's globally best overlap independently can therefore move
        # a later immutable line into an earlier beat and fail the exact-order
        # contract even though no copy is missing.  Restrict each assignment
        # to the current or a later beat so the runtime-owned script sequence
        # remains lossless while still choosing the strongest available
        # temporal overlap.
        best_index = max(
            range(minimum_beat_index, len(draft.visual.beats)),
            key=lambda index: (overlaps[index], -index),
        )
        line_ids_by_beat[best_index].append(line.line_id)
        minimum_beat_index = best_index
    for index, beat in enumerate(draft.visual.beats):
        materialized_beats.append(beat.model_copy(update={
            "line_ids": line_ids_by_beat[index],
        }))

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
            beats=materialized_beats,
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
        requirement_execution=list(draft.requirement_execution),
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
        require_visual_references=require_visual_references,
    )


def production_plan_author_output_contract(
    artifact: DirectedContentArtifact,
    *,
    authorized_asset_refs: list[str],
    require_visual_references: bool = True,
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
    references_schema = (
        definitions.get("VisualProgramAuthorDraft", {})
        .get("properties", {})
        .get("references")
    )
    if isinstance(references_schema, dict):
        references_schema["minItems"] = (
            1 if require_visual_references else 0
        )
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
        voice_properties = voice_definition.get("properties", {})
        gender_schema = voice_properties.get("gender")
        if isinstance(gender_schema, dict):
            gender_schema["enum"] = [
                "female",
                "male",
                "androgynous",
            ]
        relation_schema = voice_properties.get("screen_relation")
        if isinstance(relation_schema, dict):
            relation_schema["enum"] = [
                "off_screen_narrator",
                "on_screen_character",
                "character_voiceover",
            ]
        for field_name in ("timbre", "pitch", "accent"):
            field_schema = voice_properties.get(field_name)
            if isinstance(field_schema, dict):
                field_schema["minLength"] = 1
    visual_beat_definition = definitions.get("VisualBeat", {})
    if isinstance(visual_beat_definition, dict):
        required_beat_fields = list(
            visual_beat_definition.get("required") or []
        )
        if "continuity_dependency" not in required_beat_fields:
            required_beat_fields.append("continuity_dependency")
        visual_beat_definition["required"] = required_beat_fields
    requirement_ids = [
        item.requirement_id
        for item in artifact.program.requirement_execution
    ]
    beat_requirement_schema = (
        definitions.get("VisualBeat", {})
        .get("properties", {})
        .get("requirement_ids")
    )
    if isinstance(beat_requirement_schema, dict):
        items = beat_requirement_schema.get("items")
        if isinstance(items, dict):
            items["enum"] = requirement_ids
    production_mappings_schema = root_properties.get(
        "requirement_execution"
    )
    if isinstance(production_mappings_schema, dict):
        production_mappings_schema["minItems"] = len(requirement_ids)
        production_mappings_schema["maxItems"] = len(requirement_ids)
    production_requirement_id = (
        definitions.get("ProductionRequirementMapping", {})
        .get("properties", {})
        .get("requirement_id")
    )
    if isinstance(production_requirement_id, dict):
        production_requirement_id["enum"] = requirement_ids
    return output_contract


def build_director_production_plan_packet(
    artifact: DirectedContentArtifact,
    *,
    capability_catalog: list[dict[str, Any]],
    authorized_asset_refs: list[str],
    authoritative_product_asset_refs: list[str],
    video_generation_mode: Literal[
        "text_to_video", "image_to_video", "video_to_video"
    ] = "image_to_video",
) -> dict[str, Any]:
    """Build a strict planning packet without prescribing a story template."""

    output_contract = production_plan_author_output_contract(
        artifact,
        authorized_asset_refs=authorized_asset_refs,
        require_visual_references=(
            video_generation_mode != "text_to_video"
        ),
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

    line_delivery_contract = build_runtime_line_delivery_contract(artifact)

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
            "video_generation_mode": video_generation_mode,
            "visual_references_required": (
                video_generation_mode != "text_to_video"
            ),
            "visual_references_are_semantic_only": (
                video_generation_mode == "text_to_video"
            ),
            "runtime_must_not_generate_or_attach_reference_media": (
                video_generation_mode == "text_to_video"
            ),
            "choose_visual_beat_count_from_the_approved_program": True,
            "map_every_director_requirement_to_concrete_plan_objects": True,
            "copy_requirement_ids_onto_the_visual_beats_that_execute_them": True,
            "requirement_evidence_must_be_observable_not_generic_intent_prose": True,
            "do_not_copy_a_server_story_template": True,
            "visual_beats_are_independent_of_provider_segment_count": True,
            "each_visual_beat_declares_whether_its_transport_segment_needs_the_previous_segment_final_frame": True,
            "independent_is_allowed_only_when_character_scene_action_and_product_state_do_not_depend_on_the_immediately_previous_segment": True,
            "previous_segment_is_reserved_for_literal_action_or_product_state_continuation": True,
            "shared_character_location_wardrobe_style_or_mood_alone_does_not_require_previous_segment": True,
            "prefer_independent_transport_for_parallel_execution_when_signed_references_fully_define_the_beat": True,
            "visual_timeline_starts_at_zero": True,
            "visual_beats_are_contiguous_without_gaps_or_overlaps": True,
            "visual_timeline_ends_at_target_duration": True,
            "cover_every_script_line_once_in_order": True,
            "line_ids_are_single_owner_markers_not_repeated_context": True,
            "meet_each_line_minimum_delivery_seconds": True,
            "runtime_compiles_exact_copy_delivery_intervals": True,
            "prefer_the_registered_transport_window_for_each_line": True,
            "critical_display_copy_uses_local_overlay": True,
            "every_visual_reference_is_a_new_generated_scene_reference": (
                video_generation_mode != "text_to_video"
            ),
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
    "build_runtime_line_delivery_contract",
    "finalize_director_production_plan",
    "finalize_director_production_plan_author_draft",
    "production_plan_author_output_contract",
    "production_plan_sha256",
]
