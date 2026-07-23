from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’)]*$")
_GENERIC_TOKENS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "below", "but",
    "by", "for", "from", "had", "has", "have", "he", "her", "hers", "him",
    "his", "i", "in", "into", "is", "it", "its", "my", "of", "on", "or",
    "our", "she", "so", "that", "the", "their", "them", "they", "this", "to",
    "was", "we", "were", "with", "you", "your",
}

AudioMode = Literal[
    "spoken",
    "silent",
    "music_only",
    "sound_design",
]

AUDIO_MODE_SEMANTICS = {
    "spoken": "At least one spoken line; sound design and music are optional.",
    "silent": "No audible sound of any kind, including voice, music, room tone, or sound effects.",
    "music_only": "Music is allowed; voice, room tone, and sound effects are forbidden.",
    "sound_design": "Non-voice sound design is allowed; spoken voice is forbidden.",
}


def _words(text: str) -> list[str]:
    return [match.group(0).lower().replace("’", "'") for match in _WORD_RE.finditer(text or "")]


def _required_verbatim_voiceover_blocks(
    truth_payload: dict[str, Any] | None,
) -> list[str] | None:
    """Return an explicitly user-locked voiceover as ordered text blocks.

    A project may ask the Director to design the story, segment allocation,
    and visual program around copy that the user has already approved.  That
    copy is source truth, not model-editable prose.  Blank lines are the
    stable block boundary because sentence splitting can corrupt abbreviations
    and decimal price expressions supplied by the user.
    """

    payload = dict(truth_payload or {})
    if "required_verbatim_voiceover" not in payload:
        return None
    raw = payload.get("required_verbatim_voiceover")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "required_verbatim_voiceover must be a non-empty string"
        )
    blocks = [
        block.strip()
        for block in re.split(r"(?:\r?\n)[ \t]*(?:\r?\n)+", raw.strip())
        if block.strip()
    ]
    if not blocks:
        raise ValueError(
            "required_verbatim_voiceover must contain audience-facing copy"
        )
    return blocks


def _materialize_required_verbatim_voiceover(
    lines: list["ScriptLine"],
    *,
    truth_payload: dict[str, Any] | None,
) -> list["ScriptLine"]:
    """Replace model prose with the user-authoritative voiceover, losslessly.

    The model still owns line metadata and segment allocation.  The runtime
    owns the exact words whenever the project supplies the explicit lock.
    Failing on a block-count mismatch is intentional: silently merging or
    dropping lines would make timing and segment ownership ambiguous.
    """

    blocks = _required_verbatim_voiceover_blocks(truth_payload)
    if blocks is None:
        return list(lines)
    spoken_indices = [
        index
        for index, line in enumerate(lines)
        if line.delivery_mode == "spoken"
    ]
    if len(spoken_indices) != len(blocks):
        raise ValueError(
            "director spoken line count does not match the immutable "
            "required_verbatim_voiceover blocks: "
            f"expected={len(blocks)}, actual={len(spoken_indices)}"
        )
    materialized = list(lines)
    for index, block in zip(spoken_indices, blocks, strict=True):
        materialized[index] = materialized[index].model_copy(
            update={"text": block}
        )
    return materialized


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token
        for token in _words(text)
        if len(token) >= 4 and token not in _GENERIC_TOKENS
    }


class DirectorCapabilityNode(BaseModel):
    """One declarative node in a director-selected execution graph.

    ``capability`` is a registry key, not a Python function name.  This lets a
    director choose a legal workflow without inventing code or depending on a
    fixed maternal template.
    """

    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    capability: str = Field(min_length=1, max_length=128)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    required: bool = True
    input_contract: str = Field(min_length=1, max_length=128)
    output_contract: str = Field(min_length=1, max_length=128)
    policy: dict[str, Any] = Field(default_factory=dict)


class DirectorCapabilitySpec(BaseModel):
    """One runtime-registered capability the director is allowed to select."""

    capability: str = Field(min_length=1, max_length=128)
    input_contract: str = Field(min_length=1, max_length=128)
    output_contract: str = Field(min_length=1, max_length=128)
    policy: dict[str, Any] = Field(default_factory=dict)


class VideoProductionContract(BaseModel):
    """Provider timing/reference limits supplied by a capability registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(min_length=1, max_length=128)
    segment_duration_minimum_seconds: float = Field(gt=0, le=300)
    segment_duration_maximum_seconds: float = Field(gt=0, le=300)
    allowed_segment_durations_seconds: list[float] = Field(
        default_factory=list,
        max_length=300,
    )
    reference_image_limit: int = Field(ge=0, le=64)
    reference_video_limit: int = Field(ge=0, le=8)

    @model_validator(mode="after")
    def validate_timing_contract(self) -> "VideoProductionContract":
        if (
            self.segment_duration_minimum_seconds
            > self.segment_duration_maximum_seconds
        ):
            raise ValueError(
                "segment duration minimum cannot exceed maximum"
            )
        invalid = [
            value
            for value in self.allowed_segment_durations_seconds
            if not (
                self.segment_duration_minimum_seconds
                <= value
                <= self.segment_duration_maximum_seconds
            )
        ]
        if invalid:
            raise ValueError(
                "allowed segment durations are outside the declared range: "
                f"{invalid}"
            )
        if len(self.allowed_segment_durations_seconds) != len({
            round(value, 4)
            for value in self.allowed_segment_durations_seconds
        }):
            raise ValueError(
                "allowed segment durations must be unique"
            )
        return self


def production_segment_durations(
    contract: VideoProductionContract,
    total_duration_seconds: float,
) -> list[float]:
    """Return the deterministic legal segment plan for one complete video."""
    total = float(total_duration_seconds)
    if total <= 0:
        raise ValueError("total_duration_seconds must be positive")
    allowed = sorted(
        {
            int(round(float(value) * 100))
            for value in contract.allowed_segment_durations_seconds
        },
        reverse=True,
    )
    target = int(round(total * 100))
    if allowed:
        best: list[list[int] | None] = [None] * (target + 1)
        best[0] = []
        for current in range(target + 1):
            if best[current] is None:
                continue
            for value in allowed:
                next_value = current + value
                if next_value > target:
                    continue
                candidate = [*best[current], value]
                existing = best[next_value]
                if (
                    existing is None
                    or len(candidate) < len(existing)
                    or (
                        len(candidate) == len(existing)
                        and candidate > existing
                    )
                ):
                    best[next_value] = candidate
        if best[target] is None:
            raise ValueError(
                f"{contract.model_id} cannot compose total duration "
                f"{total:g} from allowed segment durations"
            )
        return [value / 100.0 for value in best[target]]

    maximum = float(contract.segment_duration_maximum_seconds)
    minimum = float(contract.segment_duration_minimum_seconds)
    # Continuous-duration providers need enough segments that every segment
    # stays at or below the advertised maximum. Floor division incorrectly
    # planned 40 seconds as two 20-second clips for a 15-second provider.
    count = max(1, math.ceil((total - 1e-9) / maximum))
    while count * minimum > total + 0.05:
        count -= 1
        if count <= 0:
            raise ValueError(
                f"{contract.model_id} cannot compose total duration "
                f"{total:g} inside its segment range"
            )
    base = total / count
    if not minimum - 0.05 <= base <= maximum + 0.05:
        raise ValueError(
            f"{contract.model_id} cannot compose total duration "
            f"{total:g} inside its segment range"
        )
    values = [round(base, 2) for _ in range(count)]
    values[-1] = round(total - sum(values[:-1]), 2)
    if any(
        value < minimum - 0.05 or value > maximum + 0.05
        for value in values
    ):
        raise ValueError(
            f"{contract.model_id} produced an illegal segment plan"
        )
    return values


class ConversionIntent(BaseModel):
    """Project-owned conversion policy, independent from any product or brand."""

    product_required: bool = False
    product_name: str | None = Field(default=None, max_length=255)
    product_name_aliases: list[str] = Field(
        default_factory=list,
        max_length=32,
    )
    # ``None`` leaves reveal architecture to the Director. A fixed halfway
    # reveal is a campaign template, not a universal content invariant:
    # demonstrations, unboxings and product-first explainers may legitimately
    # identify the product in their opening beat.
    reveal_after_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )
    confirmed_differentiators: list[str] = Field(default_factory=list, max_length=32)
    minimum_differentiators_in_copy: int = Field(default=0, ge=0, le=8)
    expected_human_change: str | None = Field(default=None, max_length=1000)
    outcome_boundary: str | None = Field(default=None, max_length=1000)
    offer_text: str | None = Field(default=None, max_length=500)
    cta_text: str | None = Field(default=None, max_length=500)
    protected_stake_terms: list[str] = Field(default_factory=list, max_length=32)
    require_post_cta_human_agency: bool = False
    post_cta_agency_terms: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_product_policy(self) -> "ConversionIntent":
        if self.product_required and not str(self.product_name or "").strip():
            raise ValueError("product_name is required when product_required=true")
        if self.minimum_differentiators_in_copy > len(self.confirmed_differentiators):
            raise ValueError(
                "minimum_differentiators_in_copy cannot exceed confirmed_differentiators"
            )
        if self.require_post_cta_human_agency and not self.post_cta_agency_terms:
            raise ValueError(
                "post_cta_agency_terms are required when a human-agency ending is required"
            )
        return self


class CopyReviewCriterion(BaseModel):
    """One project-owned copy standard; the runtime supplies no content rubric."""

    criterion_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    instruction: str = Field(min_length=1, max_length=2000)
    minimum_score: int = Field(ge=0, le=100)
    blocking: bool = True


class SeriesReviewCriterion(BaseModel):
    """One project-owned standard for an intent plan, not a final script."""

    criterion_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    instruction: str = Field(min_length=1, max_length=2000)
    minimum_score: int = Field(ge=0, le=100)
    blocking: bool = True


class DirectorProjectBrief(BaseModel):
    """Project-owned truth and constraints for any video type."""

    model_config = ConfigDict(extra="forbid")

    brief_id: str = Field(min_length=1, max_length=128)
    brief_version: int = Field(default=1, ge=1, le=1000)
    objective: str = Field(min_length=1, max_length=255)
    content_type_hint: str | None = Field(default=None, max_length=128)
    audio_mode_hint: AudioMode | None = None
    platform: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=2, max_length=32)
    audience: str = Field(min_length=1, max_length=1000)
    target_duration_seconds: float = Field(gt=0, le=3600)
    edit_headroom_seconds: float = Field(ge=0, le=600)
    speech_rate_wpm: float = Field(gt=0, le=400)
    display_reading_rate_wpm: float = Field(default=120, gt=0, le=400)
    aspect_ratio: str = Field(min_length=3, max_length=32)
    production_contract: VideoProductionContract | None = None
    conversion: ConversionIntent = Field(default_factory=ConversionIntent)
    truth_payload: dict[str, Any] = Field(default_factory=dict)
    truth_options: list[str] = Field(default_factory=list, max_length=128)
    creative_constraints: list[str] = Field(default_factory=list, max_length=128)
    capability_catalog: list[DirectorCapabilitySpec] = Field(
        min_length=1,
        max_length=128,
    )
    copy_review_criteria: list[CopyReviewCriterion] = Field(
        min_length=1,
        max_length=64,
    )
    quality_rubric: list[str] = Field(default_factory=list, max_length=64)
    source_truth_refs: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_project_owned_catalogs(self) -> "DirectorProjectBrief":
        capabilities = [item.capability for item in self.capability_catalog]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("capability_catalog capability values must be unique")
        criteria = [item.criterion_id for item in self.copy_review_criteria]
        if len(criteria) != len(set(criteria)):
            raise ValueError("copy_review_criteria criterion_id values must be unique")
        return self


class SeriesDiversityRequirement(BaseModel):
    """One project-owned axis used to keep a generated series distinct."""

    model_config = ConfigDict(extra="forbid")

    dimension_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    instruction: str = Field(min_length=1, max_length=2000)
    minimum_unique_values: int = Field(ge=1, le=1000)


class DirectorSeriesBrief(BaseModel):
    """Project-owned contract for a whole series, without scene templates."""

    model_config = ConfigDict(extra="forbid")

    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(default=1, ge=1, le=1000)
    objective: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=2, max_length=32)
    audience: str = Field(min_length=1, max_length=1000)
    target_count: int = Field(ge=1, le=1000)
    minimum_duration_seconds: float = Field(gt=0, le=3600)
    maximum_duration_seconds: float = Field(gt=0, le=3600)
    default_duration_seconds: float = Field(gt=0, le=3600)
    edit_headroom_seconds: float = Field(ge=0, le=600)
    speech_rate_wpm: float = Field(gt=0, le=400)
    display_reading_rate_wpm: float = Field(default=120, gt=0, le=400)
    allowed_audio_modes: list[AudioMode] = Field(
        default_factory=lambda: [
            "spoken",
            "silent",
            "music_only",
            "sound_design",
        ],
        min_length=1,
        max_length=4,
    )
    aspect_ratio: str = Field(min_length=3, max_length=32)
    production_contract: VideoProductionContract | None = None
    conversion: ConversionIntent = Field(default_factory=ConversionIntent)
    truth_payload: dict[str, Any] = Field(default_factory=dict)
    truth_options: list[str] = Field(default_factory=list, max_length=128)
    creative_constraints: list[str] = Field(default_factory=list, max_length=128)
    capability_catalog: list[DirectorCapabilitySpec] = Field(
        min_length=1,
        max_length=128,
    )
    copy_review_criteria: list[CopyReviewCriterion] = Field(
        min_length=1,
        max_length=64,
    )
    series_page_review_criteria: list[SeriesReviewCriterion] = Field(
        default_factory=list,
        max_length=64,
    )
    series_global_review_criteria: list[SeriesReviewCriterion] = Field(
        default_factory=list,
        max_length=64,
    )
    structured_intent_contract_required: bool = False
    quality_rubric: list[str] = Field(default_factory=list, max_length=64)
    source_truth_refs: list[str] = Field(default_factory=list, max_length=256)
    diversity_requirements: list[SeriesDiversityRequirement] = Field(
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_series_contract(self) -> "DirectorSeriesBrief":
        if self.minimum_duration_seconds > self.maximum_duration_seconds:
            raise ValueError(
                "minimum_duration_seconds cannot exceed "
                "maximum_duration_seconds"
            )
        if not (
            self.minimum_duration_seconds
            <= self.default_duration_seconds
            <= self.maximum_duration_seconds
        ):
            raise ValueError(
                "default_duration_seconds must be inside the duration range"
            )
        if self.edit_headroom_seconds >= self.minimum_duration_seconds:
            raise ValueError(
                "edit_headroom_seconds must be smaller than the minimum "
                "duration"
            )
        if len(self.allowed_audio_modes) != len(
            set(self.allowed_audio_modes)
        ):
            raise ValueError("allowed_audio_modes values must be unique")
        capabilities = [
            item.capability for item in self.capability_catalog
        ]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError(
                "capability_catalog capability values must be unique"
            )
        criteria = [
            item.criterion_id for item in self.copy_review_criteria
        ]
        if len(criteria) != len(set(criteria)):
            raise ValueError(
                "copy_review_criteria criterion_id values must be unique"
            )
        for label, review_criteria in (
            (
                "series_page_review_criteria",
                self.series_page_review_criteria,
            ),
            (
                "series_global_review_criteria",
                self.series_global_review_criteria,
            ),
        ):
            criterion_ids = [
                item.criterion_id for item in review_criteria
            ]
            if len(criterion_ids) != len(set(criterion_ids)):
                raise ValueError(
                    f"{label} criterion_id values must be unique"
                )
        dimensions = [
            item.dimension_id for item in self.diversity_requirements
        ]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError(
                "diversity requirement dimension_id values must be unique"
            )
        impossible = [
            item.dimension_id
            for item in self.diversity_requirements
            if item.minimum_unique_values > self.target_count
        ]
        if impossible:
            raise ValueError(
                "minimum_unique_values cannot exceed target_count: "
                f"{impossible}"
            )
        return self


class PainHypothesis(BaseModel):
    """Explicit project-requested pain logic, never a universal template."""

    model_config = ConfigDict(extra="forbid")

    concrete_moment: str = Field(min_length=1, max_length=1000)
    felt_loss_or_conflict: str = Field(min_length=1, max_length=1000)
    audience_recognition: str = Field(min_length=1, max_length=1000)
    claims_boundary: str = Field(min_length=1, max_length=1000)


class ConversionHypothesis(BaseModel):
    """Intent-level reason the product belongs in the same decision."""

    model_config = ConfigDict(extra="forbid")

    viewer_decision_or_use_case: str = Field(
        min_length=1,
        max_length=1000,
    )
    product_relevance_bridge: str = Field(
        min_length=1,
        max_length=1000,
    )
    confirmed_attribute: str = Field(min_length=1, max_length=500)
    reason_to_choose_or_consider: str = Field(
        min_length=1,
        max_length=1000,
    )
    bounded_human_change: str = Field(min_length=1, max_length=1000)
    prohibited_outcome_boundary: str = Field(
        min_length=1,
        max_length=1000,
    )
    semantic_route_fingerprint: str = Field(
        min_length=1,
        max_length=255,
    )


class SeriesContentFamily(BaseModel):
    """A Director-owned content job, never a scene or copy template.

    A finite source-truth set cannot support one genuinely new fact or value
    proposition per episode. Families make deliberate reuse explicit while
    still requiring meaningful variation in audience moment, evidence, form,
    and execution. Commerce is only one possible project objective.
    """

    model_config = ConfigDict(extra="forbid")

    family_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    strategic_job: str = Field(min_length=1, max_length=1000)
    audience_stage: str = Field(min_length=1, max_length=500)
    content_type_space: str = Field(min_length=1, max_length=1000)
    viewer_value_role: str = Field(min_length=1, max_length=1000)
    planned_variant_count: int = Field(ge=1, le=1000)
    truth_options: list[str] = Field(
        default_factory=list,
        max_length=32,
    )
    permitted_reuse: str = Field(min_length=1, max_length=1000)
    differentiation_mandate: str = Field(
        min_length=1,
        max_length=1000,
    )


class SeriesCoverageTerritory(BaseModel):
    """One model-owned strategic territory, never a prewritten scene.

    The generic fields can express education, entertainment, reporting,
    instruction, brand building, or conversion. Product and pain semantics
    belong only in projects whose objective and conversion contract require
    them.
    """

    model_config = ConfigDict(extra="forbid")

    variant_index: int = Field(ge=1, le=1000)
    family_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    territory_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    strategic_role: str = Field(min_length=1, max_length=500)
    audience_state: str = Field(min_length=1, max_length=500)
    audience_tension_or_need: str = Field(min_length=1, max_length=500)
    viewer_value_context: str = Field(min_length=1, max_length=1000)
    response_or_action_route: str = Field(min_length=1, max_length=500)
    truth_options: list[str] = Field(
        default_factory=list,
        max_length=32,
    )
    anti_repetition_rule: str = Field(min_length=1, max_length=1000)


class SeriesCoveragePage(BaseModel):
    """Reserved strategy space for one independently repairable page."""

    model_config = ConfigDict(extra="forbid")

    page_index: int = Field(ge=1, le=1000)
    start_variant_index: int = Field(ge=1, le=1000)
    end_variant_index: int = Field(ge=1, le=1000)
    territories: list[SeriesCoverageTerritory] = Field(
        min_length=1,
        max_length=100,
    )
    page_uniqueness_mandate: str = Field(min_length=1, max_length=1000)


class SeriesCoverageMapDraft(BaseModel):
    """Compact Director-authored reservation map for parallel page work."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(ge=1, le=1000)
    page_size: int = Field(ge=1, le=100)
    families: list[SeriesContentFamily] = Field(
        min_length=1,
        max_length=1000,
    )
    pages: list[SeriesCoveragePage] = Field(
        min_length=1,
        max_length=1000,
    )


class SeriesCoveragePatchDraft(BaseModel):
    """A bounded semantic repair against one signed coverage map.

    Variant indices and family IDs are stable patch coordinates. The model
    returns only affected objects; the runtime owns the atomic merge, global
    truth validation, normalized family counts, and replacement hash.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(ge=1, le=1000)
    base_coverage_sha256: str = Field(min_length=64, max_length=64)
    family_updates: list[SeriesContentFamily] = Field(
        default_factory=list,
        max_length=1000,
    )
    territory_updates: list[SeriesCoverageTerritory] = Field(
        default_factory=list,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_nonempty_patch(self) -> "SeriesCoveragePatchDraft":
        if not self.family_updates and not self.territory_updates:
            raise ValueError(
                "coverage patch must update at least one family or territory"
            )
        family_ids = [item.family_id for item in self.family_updates]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("coverage patch family updates must be unique")
        variant_indices = [
            item.variant_index for item in self.territory_updates
        ]
        if len(variant_indices) != len(set(variant_indices)):
            raise ValueError(
                "coverage patch territory variant indices must be unique"
            )
        return self


class SeriesCoverageMap(BaseModel):
    """Runtime-signed coverage map used by every subsequent page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(ge=1, le=1000)
    page_size: int = Field(ge=1, le=100)
    families: list[SeriesContentFamily] = Field(
        min_length=1,
        max_length=1000,
    )
    pages: list[SeriesCoveragePage] = Field(
        min_length=1,
        max_length=1000,
    )
    coverage_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_hash(self) -> "SeriesCoverageMap":
        if self.coverage_sha256 != series_coverage_sha256(
            series_id=self.series_id,
            series_version=self.series_version,
            page_size=self.page_size,
            families=self.families,
            pages=self.pages,
        ):
            raise ValueError(
                "coverage_sha256 does not match the coverage map"
            )
        return self


class SeriesSlateIntent(BaseModel):
    """One Director-authored intent; concrete scenes remain model-owned."""

    model_config = ConfigDict(extra="forbid")

    variant_index: int = Field(ge=1, le=1000)
    intent_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    objective: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=128)
    audio_mode: AudioMode | None = None
    audience: str = Field(min_length=1, max_length=1000)
    target_duration_seconds: float = Field(gt=0, le=3600)
    creative_strategy: dict[str, Any] = Field(default_factory=dict)
    differentiation: dict[str, str] = Field(min_length=1, max_length=64)
    creative_constraints: list[str] = Field(
        default_factory=list,
        max_length=128,
    )
    source_truth_refs: list[str] = Field(
        default_factory=list,
        max_length=256,
    )
    pain_hypothesis: PainHypothesis | None = None
    conversion_hypothesis: ConversionHypothesis | None = None


class SeriesSlateDraft(BaseModel):
    """Strict raw series response; runtime computes the integrity hash."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "2.0"] = "1.0"
    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(ge=1, le=1000)
    intents: list[SeriesSlateIntent] = Field(
        min_length=1,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_structured_audio(self) -> "SeriesSlateDraft":
        if self.schema_version == "2.0" and any(
            intent.audio_mode is None for intent in self.intents
        ):
            raise ValueError(
                "SeriesSlateDraft v2 requires audio_mode for every intent"
            )
        return self


class SeriesSlatePageDraft(BaseModel):
    """One bounded, resumable page of a large series slate.

    The page carries global variant ordinals.  It is never a deliverable on
    its own; the runtime validates and combines every page before computing
    the immutable whole-series hash.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "2.0"] = "1.0"
    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(ge=1, le=1000)
    page_index: int = Field(ge=1, le=1000)
    start_variant_index: int = Field(ge=1, le=1000)
    end_variant_index: int = Field(ge=1, le=1000)
    intents: list[SeriesSlateIntent] = Field(
        min_length=1,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_page_range(self) -> "SeriesSlatePageDraft":
        if self.schema_version == "2.0" and any(
            intent.audio_mode is None for intent in self.intents
        ):
            raise ValueError(
                "SeriesSlatePageDraft v2 requires audio_mode for every intent"
            )
        if self.start_variant_index > self.end_variant_index:
            raise ValueError(
                "start_variant_index cannot exceed end_variant_index"
            )
        expected = list(
            range(self.start_variant_index, self.end_variant_index + 1)
        )
        actual = [item.variant_index for item in self.intents]
        if actual != expected:
            raise ValueError(
                "page intent variant_index values must match the declared "
                "contiguous page range"
            )
        return self


class SeriesSlate(BaseModel):
    """Runtime-signed, immutable project-level slate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "2.0"] = "1.0"
    series_id: str = Field(min_length=1, max_length=128)
    series_version: int = Field(ge=1, le=1000)
    intents: list[SeriesSlateIntent] = Field(
        min_length=1,
        max_length=1000,
    )
    slate_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_hash(self) -> "SeriesSlate":
        if self.schema_version == "2.0" and any(
            intent.audio_mode is None for intent in self.intents
        ):
            raise ValueError(
                "SeriesSlate v2 requires audio_mode for every intent"
            )
        if self.slate_sha256 != series_slate_sha256(
            series_id=self.series_id,
            series_version=self.series_version,
            intents=self.intents,
        ):
            raise ValueError("slate_sha256 does not match the series slate")
        return self


def series_coverage_sha256(
    *,
    series_id: str,
    series_version: int,
    page_size: int,
    families: list[SeriesContentFamily],
    pages: list[SeriesCoveragePage],
) -> str:
    canonical = json.dumps(
        {
            "series_id": series_id,
            "series_version": int(series_version),
            "page_size": int(page_size),
            "families": [
                family.model_dump(mode="json")
                for family in families
            ],
            "pages": [
                page.model_dump(mode="json")
                for page in pages
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def finalize_series_coverage_map(
    draft: SeriesCoverageMapDraft,
    brief: DirectorSeriesBrief,
    *,
    page_size: int,
) -> SeriesCoverageMap:
    size = max(1, min(int(page_size), int(brief.target_count)))
    total_pages = (int(brief.target_count) + size - 1) // size
    if draft.series_id != brief.series_id:
        raise ValueError("coverage director changed series_id")
    if draft.series_version != brief.series_version:
        raise ValueError("coverage director changed series_version")
    if int(draft.page_size) != size:
        raise ValueError("coverage director changed page_size")
    if [page.page_index for page in draft.pages] != list(
        range(1, total_pages + 1)
    ):
        raise ValueError(
            "coverage pages must be contiguous and match total pages"
        )

    family_by_id = {
        family.family_id: family
        for family in draft.families
    }
    if len(family_by_id) != len(draft.families):
        raise ValueError("coverage family_id values must be unique")

    territory_ids: list[str] = []
    semantic_keys: list[tuple[str, str, str]] = []
    actual_family_counts: Counter[str] = Counter()
    territory_attributes_by_family: dict[str, list[str]] = defaultdict(
        list
    )
    allowed_truth_options = {
        str(value).strip().casefold()
        for value in brief.truth_options
        if str(value).strip()
    }
    for page in draft.pages:
        expected_start = ((page.page_index - 1) * size) + 1
        expected_end = min(
            page.page_index * size,
            int(brief.target_count),
        )
        if (
            page.start_variant_index != expected_start
            or page.end_variant_index != expected_end
        ):
            raise ValueError(
                f"coverage page {page.page_index} changed its variant range"
            )
        expected_variant_indices = list(
            range(expected_start, expected_end + 1)
        )
        actual_variant_indices = [
            territory.variant_index
            for territory in page.territories
        ]
        if actual_variant_indices != expected_variant_indices:
            raise ValueError(
                f"coverage page {page.page_index} must reserve exactly one "
                "ordered strategic territory for every variant"
        )
        for territory in page.territories:
            territory_ids.append(territory.territory_id)
            actual_family_counts[territory.family_id] += 1
            family = family_by_id.get(territory.family_id)
            if family is None:
                raise ValueError(
                    f"coverage territory {territory.territory_id} cites "
                    "an unknown content family"
                )
            semantic_keys.append((
                re.sub(
                    r"\s+",
                    " ",
                    territory.audience_state.strip().casefold(),
                ),
                re.sub(
                    r"\s+",
                    " ",
                    territory.audience_tension_or_need.strip().casefold(),
                ),
                re.sub(
                    r"\s+",
                    " ",
                    territory.response_or_action_route.strip().casefold(),
                ),
            ))
            invalid_attributes = [
                value
                for value in territory.truth_options
                if str(value).strip().casefold()
                not in allowed_truth_options
            ]
            if invalid_attributes:
                raise ValueError(
                    f"coverage territory {territory.territory_id} invented "
                    "a truth option outside the project contract"
                )
            for value in territory.truth_options:
                if value not in territory_attributes_by_family[
                    territory.family_id
                ]:
                    territory_attributes_by_family[
                        territory.family_id
                    ].append(value)
    for family in draft.families:
        invalid_family_attributes = [
            value
            for value in family.truth_options
            if str(value).strip().casefold()
            not in allowed_truth_options
        ]
        if invalid_family_attributes:
            raise ValueError(
                f"coverage family {family.family_id} invented confirmed "
                "truth options"
            )
        normalized_family_attributes = list(dict.fromkeys([
            *family.truth_options,
            *territory_attributes_by_family[family.family_id],
        ]))
        if (
            brief.conversion.product_required
            and not normalized_family_attributes
        ):
            raise ValueError(
                f"coverage family {family.family_id} must reserve at least "
                "one confirmed product differentiator"
            )
        if actual_family_counts[family.family_id] <= 0:
            raise ValueError(
                f"coverage family {family.family_id} has no assigned "
                "territories"
            )
    if len(territory_ids) != len(set(territory_ids)):
        raise ValueError("coverage territory_id values must be unique")
    if len(semantic_keys) != len(set(semantic_keys)):
        raise ValueError(
            "coverage map contains an exact repeated strategic territory"
        )
    normalized_families = [
        family.model_copy(
            update={
                "planned_variant_count": int(
                    actual_family_counts[family.family_id]
                ),
                "truth_options": list(dict.fromkeys([
                    *family.truth_options,
                    *territory_attributes_by_family[family.family_id],
                ])),
            }
        )
        for family in draft.families
    ]
    normalized_family_by_id = {
        family.family_id: family
        for family in normalized_families
    }
    normalized_pages = [
        page.model_copy(
            update={
                "territories": [
                    territory.model_copy(
                        update={
                            "truth_options": list(
                                territory.truth_options
                                or normalized_family_by_id[
                                    territory.family_id
                                ].truth_options
                            )
                        }
                    )
                    for territory in page.territories
                ]
            }
        )
        for page in draft.pages
    ]
    return SeriesCoverageMap(
        series_id=draft.series_id,
        series_version=draft.series_version,
        page_size=size,
        families=normalized_families,
        pages=normalized_pages,
        coverage_sha256=series_coverage_sha256(
            series_id=draft.series_id,
            series_version=draft.series_version,
            page_size=size,
            families=normalized_families,
            pages=normalized_pages,
        ),
    )


def apply_series_coverage_patch(
    patch: SeriesCoveragePatchDraft,
    base: SeriesCoverageMap,
    brief: DirectorSeriesBrief,
    *,
    page_size: int,
    allowed_territory_ids: set[str],
) -> SeriesCoverageMap:
    """Atomically merge a Critic-scoped coverage repair.

    Only families containing cited territories and the cited variant indices
    themselves may change. Everything else is copied from the signed base map
    before the complete deterministic coverage validator runs again.
    """
    if patch.series_id != base.series_id:
        raise ValueError("coverage patch changed series_id")
    if patch.series_version != base.series_version:
        raise ValueError("coverage patch changed series_version")
    if patch.base_coverage_sha256 != base.coverage_sha256:
        raise ValueError("coverage patch base hash is stale")
    if base.series_id != brief.series_id:
        raise ValueError("coverage patch base belongs to another series")

    base_territory_by_id = {
        territory.territory_id: territory
        for page in base.pages
        for territory in page.territories
    }
    unknown_scope = sorted(
        set(allowed_territory_ids) - set(base_territory_by_id)
    )
    if unknown_scope:
        raise ValueError(
            "coverage patch scope cites unknown territories: "
            f"{unknown_scope}"
        )
    if not allowed_territory_ids:
        raise ValueError("coverage patch has no Critic-cited scope")
    allowed_variants = {
        base_territory_by_id[territory_id].variant_index
        for territory_id in allowed_territory_ids
    }
    allowed_families = {
        base_territory_by_id[territory_id].family_id
        for territory_id in allowed_territory_ids
    }

    base_family_by_id = {
        family.family_id: family for family in base.families
    }
    invalid_family_updates = sorted(
        {
            family.family_id for family in patch.family_updates
        }
        - allowed_families
    )
    if invalid_family_updates:
        raise ValueError(
            "coverage patch changed uncited families: "
            f"{invalid_family_updates}"
        )
    missing_family_updates = sorted(
        {
            family.family_id for family in patch.family_updates
        }
        - set(base_family_by_id)
    )
    if missing_family_updates:
        raise ValueError(
            "coverage patch invented family IDs: "
            f"{missing_family_updates}"
        )
    invalid_variant_updates = sorted(
        {
            territory.variant_index
            for territory in patch.territory_updates
        }
        - allowed_variants
    )
    if invalid_variant_updates:
        raise ValueError(
            "coverage patch changed uncited variants: "
            f"{invalid_variant_updates}"
        )
    invalid_assignment_updates = sorted({
        territory.family_id
        for territory in patch.territory_updates
        if territory.family_id not in allowed_families
    })
    if invalid_assignment_updates:
        raise ValueError(
            "coverage patch moved a cited territory into an uncited family: "
            f"{invalid_assignment_updates}"
        )

    family_update_by_id = {
        family.family_id: family for family in patch.family_updates
    }
    territory_update_by_variant = {
        territory.variant_index: territory
        for territory in patch.territory_updates
    }
    merged_families = [
        family_update_by_id.get(family.family_id, family)
        for family in base.families
    ]
    merged_pages = [
        page.model_copy(
            update={
                "territories": [
                    territory_update_by_variant.get(
                        territory.variant_index,
                        territory,
                    )
                    for territory in page.territories
                ]
            }
        )
        for page in base.pages
    ]
    return finalize_series_coverage_map(
        SeriesCoverageMapDraft(
            series_id=base.series_id,
            series_version=base.series_version,
            page_size=base.page_size,
            families=merged_families,
            pages=merged_pages,
        ),
        brief,
        page_size=page_size,
    )


def build_series_coverage_packet(
    brief: DirectorSeriesBrief,
    *,
    page_size: int,
) -> dict[str, Any]:
    size = max(1, min(int(page_size), int(brief.target_count)))
    total_pages = (int(brief.target_count) + size - 1) // size
    page_ranges = [
        {
            "page_index": page_index,
            "start_variant_index": ((page_index - 1) * size) + 1,
            "end_variant_index": min(
                page_index * size,
                int(brief.target_count),
            ),
        }
        for page_index in range(1, total_pages + 1)
    ]
    # Coverage planning only needs strategic constraints. Sending the complete
    # full truth envelope and downstream production schemas hide the small
    # authoritative vocabulary that the model must copy exactly. Keep this
    # boundary compact; page and episode stages receive their richer packets.
    strategy_contract = {
        "series_id": brief.series_id,
        "series_version": int(brief.series_version),
        "objective": brief.objective,
        "platform": brief.platform,
        "locale": brief.locale,
        "audience": brief.audience,
        "target_count": int(brief.target_count),
        "duration_range_seconds": {
            "minimum": float(brief.minimum_duration_seconds),
            "maximum": float(brief.maximum_duration_seconds),
            "default": float(brief.default_duration_seconds),
        },
        "conversion": {
            "product_required": bool(brief.conversion.product_required),
            "product_name": brief.conversion.product_name,
            "allowed_truth_options": list(
                brief.truth_options
            ),
            "expected_human_change": (
                brief.conversion.expected_human_change
            ),
            "outcome_boundary": brief.conversion.outcome_boundary,
            "offer_text": brief.conversion.offer_text,
            "cta_text": brief.conversion.cta_text,
            "protected_stake_terms": list(
                brief.conversion.protected_stake_terms
            ),
        },
        "creative_constraints": list(brief.creative_constraints),
        "diversity_requirements": [
            item.model_dump(mode="json")
            for item in brief.diversity_requirements
        ],
        "source_truth_refs": list(brief.source_truth_refs),
        # Operator repair instructions are stage-owned policy, not product
        # truth.  Keep this single scoped field in both bounded planning
        # packets; omitting it made coverage/page Directors silently continue
        # with the old creative behavior after an explicit quality restart.
        "operator_stage_instruction": str(
            dict(brief.truth_payload or {}).get(
                "operator_stage_instruction"
            )
            or ""
        ).strip()
        or None,
        "completed_content_history": list(
            dict(brief.truth_payload or {}).get(
                "completed_content_history"
            )
            or []
        ),
    }
    output_contract = SeriesCoverageMapDraft.model_json_schema()
    for definition_name in (
        "SeriesContentFamily",
        "SeriesCoverageTerritory",
    ):
        attribute_schema = (
            output_contract.get("$defs", {})
            .get(definition_name, {})
            .get("properties", {})
            .get("truth_options", {})
            .get("items")
        )
        if (
            isinstance(attribute_schema, dict)
            and brief.truth_options
        ):
            attribute_schema["enum"] = list(
                brief.truth_options
            )
    if brief.conversion.product_required:
        collection_schema = (
            output_contract.get("$defs", {})
            .get("SeriesContentFamily", {})
            .get("properties", {})
            .get("truth_options")
        )
        if isinstance(collection_schema, dict):
            collection_schema["minItems"] = 1
    pages_schema = output_contract.get("properties", {}).get(
        "pages"
    )
    if isinstance(pages_schema, dict):
        pages_schema["minItems"] = total_pages
        pages_schema["maxItems"] = total_pages
    coverage_properties = output_contract.get("properties", {})
    for field_name, exact_value in (
        ("series_id", brief.series_id),
        ("series_version", int(brief.series_version)),
        ("page_size", int(size)),
    ):
        field_schema = coverage_properties.get(field_name)
        if isinstance(field_schema, dict):
            field_schema["const"] = exact_value
    return {
        "schema_version": "1.0",
        "role": "content_series_strategy",
        "series_strategy_contract": strategy_contract,
        "page_ranges": page_ranges,
        "strategy_rules": {
            "plan_content_families_before_episode_territories": True,
            "families_are_project_jobs_not_copy_templates": True,
            "territories_are_reasoning_spaces_not_scene_templates": True,
            "finite_truth_or_value_reasons_may_repeat_within_a_family": True,
            "do_not_invent_one_fact_or_value_premise_per_variant": True,
            "family_counts_must_sum_to_target_count": True,
            "vary_viewer_moment_evidence_content_form_and_execution_within_family": True,
            "reserve_non_overlapping_audience_need_and_execution_logic": True,
            "reserve_exactly_one_ordered_territory_per_variant": True,
            "truth_option_copy_rule": (
                "Each truth_options item must be copied "
                "verbatim from allowed_truth_options. Put one "
                "allowed list element in each item; never paraphrase, combine, "
                "split, or invent an attribute."
            ),
            "family_assignment_copy_rule": (
                "Every territory.family_id must copy exactly one family_id "
                "declared in this same response. Every territory attribute "
                "must also appear in that selected family's "
                "truth_options."
            ),
            "territory_truth_inheritance_rule": (
                "Put the exact supplied truth options on each content "
                "family. A territory may leave truth_options "
                "empty to inherit its selected family's options; only include "
                "territory options when intentionally narrowing that family "
                "list."
            ),
            "let_page_directors_choose_characters_scenes_and_copy": True,
            "do_not_repeat_completed_content_history": True,
            "return": "SeriesCoverageMapDraft only",
        },
        "output_contract": output_contract,
    }


def series_slate_sha256(
    *,
    series_id: str,
    series_version: int,
    intents: list[SeriesSlateIntent],
) -> str:
    serialized_intents: list[dict[str, Any]] = []
    for item in intents:
        payload = item.model_dump(mode="json")
        # audio_mode did not exist in the signed v1 contract. Omitting only
        # this absent field keeps persisted v1 slate hashes readable while v2
        # signs the Director's explicit choice.
        if payload.get("audio_mode") is None:
            payload.pop("audio_mode", None)
        serialized_intents.append(payload)
    canonical = json.dumps(
        {
            "series_id": series_id,
            "series_version": int(series_version),
            "intents": serialized_intents,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_series_intents(
    brief: DirectorSeriesBrief,
    intents: list[SeriesSlateIntent],
    *,
    expected_indices: list[int],
    prior_intent_ids: set[str] | None = None,
) -> None:
    indices = [item.variant_index for item in intents]
    if indices != expected_indices:
        raise ValueError(
            "series slate variant_index values must be contiguous and ordered"
        )
    intent_ids = [item.intent_id for item in intents]
    if len(intent_ids) != len(set(intent_ids)):
        raise ValueError("series slate intent_id values must be unique")
    collisions = sorted(set(intent_ids) & set(prior_intent_ids or set()))
    if collisions:
        raise ValueError(
            "series slate page reused prior intent_id values: "
            f"{collisions}"
        )
    allowed_truth_refs = set(brief.source_truth_refs)
    required_dimensions = {
        item.dimension_id: item
        for item in brief.diversity_requirements
    }
    confirmed_attributes = {
        str(item).strip().casefold()
        for item in brief.conversion.confirmed_differentiators
        if str(item).strip()
    }
    allowed_audio_modes = set(brief.allowed_audio_modes)
    for intent in intents:
        # Schema v1 slates predate structured audio authority and legitimately
        # carry None.  Current v2 draft/page validators require a real mode;
        # constrain only explicit modes here so legacy audit hashes remain
        # readable without weakening new production contracts.
        if (
            intent.audio_mode is not None
            and intent.audio_mode not in allowed_audio_modes
        ):
            raise ValueError(
                f"intent {intent.intent_id} audio_mode "
                f"{intent.audio_mode!r} is outside allowed_audio_modes"
            )
        if not (
            brief.minimum_duration_seconds
            <= intent.target_duration_seconds
            <= brief.maximum_duration_seconds
        ):
            raise ValueError(
                f"intent {intent.intent_id} duration is outside project range"
            )
        keys = set(intent.differentiation)
        if keys != set(required_dimensions):
            raise ValueError(
                f"intent {intent.intent_id} differentiation keys must exactly "
                "match project diversity dimensions"
            )
        if any(
            not str(value or "").strip()
            for value in intent.differentiation.values()
        ):
            raise ValueError(
                f"intent {intent.intent_id} has an empty differentiation value"
            )
        if (
            allowed_truth_refs
            and not set(intent.source_truth_refs) <= allowed_truth_refs
        ):
            raise ValueError(
                f"intent {intent.intent_id} invented a source truth reference"
            )
        if brief.structured_intent_contract_required:
            if intent.pain_hypothesis is None:
                raise ValueError(
                    f"intent {intent.intent_id} is missing pain_hypothesis"
                )
            if (
                brief.conversion.product_required
                and intent.conversion_hypothesis is None
            ):
                raise ValueError(
                    f"intent {intent.intent_id} is missing "
                    "conversion_hypothesis"
                )
        if intent.conversion_hypothesis is not None:
            confirmed_attribute = (
                intent.conversion_hypothesis.confirmed_attribute
                .strip()
                .casefold()
            )
            if (
                brief.conversion.product_required
                and confirmed_attribute not in confirmed_attributes
            ):
                raise ValueError(
                    f"intent {intent.intent_id} conversion_hypothesis "
                    "must cite one exact confirmed product attribute"
                )
        if brief.production_contract is not None:
            production_segment_durations(
                brief.production_contract,
                intent.target_duration_seconds,
            )


def validate_series_slate_page(
    draft: SeriesSlatePageDraft,
    brief: DirectorSeriesBrief,
    *,
    expected_page_index: int,
    expected_start_variant_index: int,
    expected_end_variant_index: int,
    prior_intent_ids: set[str] | None = None,
    coverage_page: SeriesCoveragePage | None = None,
    required_schema_version: Literal["1.0", "2.0"] | None = None,
) -> list[SeriesSlateIntent]:
    """Validate one page without pretending it is the complete series."""
    if (
        required_schema_version is not None
        and draft.schema_version != required_schema_version
    ):
        raise ValueError(
            "series slate page schema_version must be "
            f"{required_schema_version}"
        )
    if draft.series_id != brief.series_id:
        raise ValueError("director changed series_id")
    if draft.series_version != brief.series_version:
        raise ValueError("director changed series_version")
    if draft.page_index != expected_page_index:
        raise ValueError("director changed page_index")
    if draft.start_variant_index != expected_start_variant_index:
        raise ValueError("director changed start_variant_index")
    if draft.end_variant_index != expected_end_variant_index:
        raise ValueError("director changed end_variant_index")
    expected_indices = list(
        range(
            expected_start_variant_index,
            expected_end_variant_index + 1,
        )
    )
    _validate_series_intents(
        brief,
        draft.intents,
        expected_indices=expected_indices,
        prior_intent_ids=prior_intent_ids,
    )
    if coverage_page is not None:
        if (
            coverage_page.page_index != expected_page_index
            or coverage_page.start_variant_index
            != expected_start_variant_index
            or coverage_page.end_variant_index
            != expected_end_variant_index
        ):
            raise ValueError(
                "coverage page does not match the declared page range"
            )
        territory_by_variant = {
            territory.variant_index: territory
            for territory in coverage_page.territories
        }
        for intent in draft.intents:
            if intent.conversion_hypothesis is None:
                continue
            territory = territory_by_variant.get(
                intent.variant_index
            )
            if territory is None:
                raise ValueError(
                    f"intent {intent.intent_id} has no reserved "
                    "strategic territory"
                )
            allowed_attributes = {
                str(value).strip().casefold()
                for value in territory.truth_options
                if str(value).strip()
            }
            selected_attribute = str(
                intent.conversion_hypothesis.confirmed_attribute
            ).strip().casefold()
            if (
                allowed_attributes
                and selected_attribute not in allowed_attributes
            ):
                raise ValueError(
                    f"intent {intent.intent_id} selected a confirmed "
                    "attribute outside its reserved territory"
                )
    return list(draft.intents)


def finalize_series_slate(
    draft: SeriesSlateDraft,
    brief: DirectorSeriesBrief,
) -> SeriesSlate:
    if draft.series_id != brief.series_id:
        raise ValueError("director changed series_id")
    if draft.series_version != brief.series_version:
        raise ValueError("director changed series_version")
    if len(draft.intents) != brief.target_count:
        raise ValueError(
            "series slate must contain exactly target_count intents"
        )
    _validate_series_intents(
        brief,
        draft.intents,
        expected_indices=list(range(1, brief.target_count + 1)),
    )
    required_dimensions = {
        item.dimension_id: item
        for item in brief.diversity_requirements
    }
    for dimension_id, requirement in required_dimensions.items():
        values = {
            re.sub(
                r"\s+",
                " ",
                intent.differentiation[dimension_id].strip().casefold(),
            )
            for intent in draft.intents
        }
        if len(values) < requirement.minimum_unique_values:
            raise ValueError(
                f"series slate dimension {dimension_id} has {len(values)} "
                "unique values but requires "
                f"{requirement.minimum_unique_values}"
            )
    return SeriesSlate(
        schema_version=draft.schema_version,
        series_id=draft.series_id,
        series_version=draft.series_version,
        intents=draft.intents,
        slate_sha256=series_slate_sha256(
            series_id=draft.series_id,
            series_version=draft.series_version,
            intents=draft.intents,
        ),
    )


def parse_series_slate_response(
    response_text: str,
    *,
    brief: DirectorSeriesBrief,
    required_schema_version: Literal["1.0", "2.0"] | None = None,
) -> SeriesSlate:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 2_000_000:
        raise ValueError(
            "series slate response is empty or exceeds the response limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "series slate response must be raw JSON without markdown fences"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("series slate response is not valid JSON") from exc
    draft = SeriesSlateDraft.model_validate(payload)
    if (
        required_schema_version is not None
        and draft.schema_version != required_schema_version
    ):
        raise ValueError(
            "series slate schema_version must be "
            f"{required_schema_version}"
        )
    return finalize_series_slate(draft, brief)


def build_series_slate_packet(
    brief: DirectorSeriesBrief,
) -> dict[str, Any]:
    output_contract = series_slate_output_contract(
        allowed_audio_modes=brief.allowed_audio_modes,
    )
    _remove_unrequested_pain_contract(output_contract, brief)
    return {
        "schema_version": "2.0",
        "role": "content_series_director",
        "series_brief": brief.model_dump(mode="json"),
        "director_rules": {
            "use_only_supplied_truth": True,
            "select_only_registered_capabilities": True,
            "do_not_use_server_scene_templates": True,
            "satisfy_every_project_diversity_dimension": True,
            "plan_exactly_target_count_intents": True,
            "describe_the_viewer_need_without_forcing_pain": True,
            "return": (
                "SeriesSlateDraft only; runtime computes slate_sha256"
            ),
        },
        "output_contract": output_contract,
    }


def _require_current_series_audio_contract(
    output_contract: dict[str, Any],
    *,
    allowed_audio_modes: list[AudioMode] | None = None,
) -> dict[str, Any]:
    properties = output_contract.get("properties", {})
    schema_version = properties.get("schema_version")
    if isinstance(schema_version, dict):
        schema_version["const"] = "2.0"
        schema_version.pop("enum", None)
        schema_version["default"] = "2.0"
    root_required = list(output_contract.get("required") or [])
    if "schema_version" not in root_required:
        root_required.append("schema_version")
    output_contract["required"] = root_required
    intent_schema = (
        output_contract.get("$defs", {}).get("SeriesSlateIntent", {})
    )
    audio_mode_schema = (
        intent_schema.get("properties", {}).get("audio_mode")
    )
    if isinstance(audio_mode_schema, dict):
        audio_mode_schema["description"] = (
            "Authoritative audio contract. Every audio-related free-text "
            "field must obey these semantics: "
            + json.dumps(
                AUDIO_MODE_SEMANTICS,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        allowed = list(
            allowed_audio_modes
            or ["spoken", "silent", "music_only", "sound_design"]
        )
        if len(allowed) == 1:
            audio_mode_schema["const"] = allowed[0]
            audio_mode_schema.pop("enum", None)
        else:
            audio_mode_schema["enum"] = allowed
            audio_mode_schema.pop("const", None)
    required = list(intent_schema.get("required") or [])
    if "audio_mode" not in required:
        required.append("audio_mode")
    intent_schema["required"] = required
    return output_contract


def series_slate_output_contract(
    *,
    allowed_audio_modes: list[AudioMode] | None = None,
) -> dict[str, Any]:
    """Current monolithic series contract with explicit audio authority."""

    return _require_current_series_audio_contract(
        SeriesSlateDraft.model_json_schema(),
        allowed_audio_modes=allowed_audio_modes,
    )


def series_slate_page_output_contract(
    *,
    allowed_audio_modes: list[AudioMode] | None = None,
) -> dict[str, Any]:
    """Current page contract with explicit audio authority."""

    return _require_current_series_audio_contract(
        SeriesSlatePageDraft.model_json_schema(),
        allowed_audio_modes=allowed_audio_modes,
    )


def _remove_unrequested_pain_contract(
    output_contract: dict[str, Any],
    brief: DirectorSeriesBrief,
) -> None:
    """Keep pain as an explicit opt-in instead of a system-wide story shape."""

    if brief.structured_intent_contract_required:
        return
    definitions = output_contract.get("$defs", {})
    intent_schema = definitions.get("SeriesSlateIntent", {})
    properties = intent_schema.get("properties", {})
    if isinstance(properties, dict):
        properties.pop("pain_hypothesis", None)
    required = list(intent_schema.get("required") or [])
    intent_schema["required"] = [
        field for field in required if field != "pain_hypothesis"
    ]
    definitions.pop("PainHypothesis", None)


def build_series_slate_page_packet(
    brief: DirectorSeriesBrief,
    *,
    page_index: int,
    total_pages: int,
    start_variant_index: int,
    end_variant_index: int,
    accepted_prior_intents: list[SeriesSlateIntent],
    revision_context: dict[str, Any] | None = None,
    coverage_page: SeriesCoveragePage | None = None,
) -> dict[str, Any]:
    """Build a bounded page request with cumulative diversity evidence."""
    unique_values: dict[str, set[str]] = {
        item.dimension_id: set()
        for item in brief.diversity_requirements
    }
    for intent in accepted_prior_intents:
        for dimension_id, value in intent.differentiation.items():
            if dimension_id in unique_values:
                unique_values[dimension_id].add(
                    re.sub(r"\s+", " ", str(value).strip().casefold())
                )
    remaining = {
        item.dimension_id: max(
            0,
            int(item.minimum_unique_values)
            - len(unique_values[item.dimension_id]),
        )
        for item in brief.diversity_requirements
    }
    prior_registry = [
        {
            "variant_index": int(item.variant_index),
            "intent_id": item.intent_id,
            "content_type": item.content_type,
            "differentiation": dict(item.differentiation),
            "pain_fingerprint": (
                {
                    "concrete_moment": (
                        item.pain_hypothesis.concrete_moment
                    ),
                    "felt_loss_or_conflict": (
                        item.pain_hypothesis.felt_loss_or_conflict
                    ),
                }
                if item.pain_hypothesis is not None
                else None
            ),
            "conversion_fingerprint": (
                {
                    "confirmed_attribute": (
                        item.conversion_hypothesis.confirmed_attribute
                    ),
                    "semantic_route_fingerprint": (
                        item.conversion_hypothesis
                        .semantic_route_fingerprint
                    ),
                }
                if item.conversion_hypothesis is not None
                else None
            ),
        }
        for item in accepted_prior_intents
    ]
    required_dimensions = [
        {
            "dimension_id": item.dimension_id,
            "instruction": item.instruction,
            "minimum_unique_values": int(
                item.minimum_unique_values
            ),
        }
        for item in brief.diversity_requirements
    ]
    page_strategy_contract = {
        "series_id": brief.series_id,
        "series_version": int(brief.series_version),
        "objective": brief.objective,
        "platform": brief.platform,
        "locale": brief.locale,
        "audience": brief.audience,
        "allowed_audio_modes": list(brief.allowed_audio_modes),
        "duration_range_seconds": {
            "minimum": float(brief.minimum_duration_seconds),
            "maximum": float(brief.maximum_duration_seconds),
            "default": float(brief.default_duration_seconds),
        },
        "conversion": brief.conversion.model_dump(mode="json"),
        "truth_options": list(brief.truth_options),
        "creative_constraints": list(brief.creative_constraints),
        "available_capabilities": [
            item.model_dump(mode="json")
            for item in brief.capability_catalog
        ],
        "required_differentiation_dimensions": required_dimensions,
        "structured_intent_contract_required": bool(
            brief.structured_intent_contract_required
        ),
        "source_truth_refs": list(brief.source_truth_refs),
        "operator_stage_instruction": str(
            dict(brief.truth_payload or {}).get(
                "operator_stage_instruction"
            )
            or ""
        ).strip()
        or None,
        "completed_content_history": list(
            dict(brief.truth_payload or {}).get(
                "completed_content_history"
            )
            or []
        ),
    }
    output_contract = series_slate_page_output_contract(
        allowed_audio_modes=brief.allowed_audio_modes,
    )
    _remove_unrequested_pain_contract(output_contract, brief)
    intent_schema = (
        output_contract.get("$defs", {})
        .get("SeriesSlateIntent", {})
    )
    intent_properties = intent_schema.get("properties", {})
    dimension_ids = [
        item.dimension_id
        for item in brief.diversity_requirements
    ]
    intent_properties["differentiation"] = {
        "type": "object",
        "properties": {
            item.dimension_id: {
                "type": "string",
                "minLength": 1,
                "maxLength": 2000,
                "description": item.instruction,
            }
            for item in brief.diversity_requirements
        },
        "required": dimension_ids,
        "additionalProperties": False,
        "minProperties": len(dimension_ids),
        "maxProperties": len(dimension_ids),
    }
    required_intent_fields = list(intent_schema.get("required") or [])
    if brief.structured_intent_contract_required:
        intent_properties["pain_hypothesis"] = {
            "$ref": "#/$defs/PainHypothesis"
        }
        if "pain_hypothesis" not in required_intent_fields:
            required_intent_fields.append("pain_hypothesis")
        if brief.conversion.product_required:
            intent_properties["conversion_hypothesis"] = {
                "$ref": "#/$defs/ConversionHypothesis"
            }
            if "conversion_hypothesis" not in required_intent_fields:
                required_intent_fields.append(
                    "conversion_hypothesis"
                )
    intent_schema["required"] = required_intent_fields
    duration_schema = intent_properties.get(
        "target_duration_seconds"
    )
    if isinstance(duration_schema, dict):
        duration_schema["minimum"] = float(
            brief.minimum_duration_seconds
        )
        duration_schema["maximum"] = float(
            brief.maximum_duration_seconds
        )
    if brief.source_truth_refs:
        truth_ref_schema = intent_properties.get("source_truth_refs")
        if isinstance(truth_ref_schema, dict):
            items_schema = truth_ref_schema.get("items")
            if isinstance(items_schema, dict):
                items_schema["enum"] = list(brief.source_truth_refs)
    if brief.conversion.confirmed_differentiators:
        conversion_schema = (
            output_contract.get("$defs", {})
            .get("ConversionHypothesis", {})
            .get("properties", {})
            .get("confirmed_attribute")
        )
        if isinstance(conversion_schema, dict):
            conversion_schema["enum"] = list(
                brief.conversion.confirmed_differentiators
            )
    page_properties = output_contract.get("properties", {})
    for field_name, exact_value in (
        ("series_id", brief.series_id),
        ("series_version", int(brief.series_version)),
        ("page_index", int(page_index)),
        ("start_variant_index", int(start_variant_index)),
        ("end_variant_index", int(end_variant_index)),
    ):
        field_schema = page_properties.get(field_name)
        if isinstance(field_schema, dict):
            field_schema["const"] = exact_value
    intents_schema = page_properties.get("intents")
    if isinstance(intents_schema, dict):
        exact_intent_count = (
            int(end_variant_index)
            - int(start_variant_index)
            + 1
        )
        intents_schema["minItems"] = exact_intent_count
        intents_schema["maxItems"] = exact_intent_count
    packet: dict[str, Any] = {
        "schema_version": "2.0",
        "role": "content_series_director_page",
        "series_page_strategy_contract": page_strategy_contract,
        "page_contract": {
            "page_index": int(page_index),
            "total_pages": int(total_pages),
            "start_variant_index": int(start_variant_index),
            "end_variant_index": int(end_variant_index),
            "intent_count": (
                int(end_variant_index) - int(start_variant_index) + 1
            ),
        },
        "accepted_prior_intent_registry": prior_registry,
        "remaining_minimum_unique_values": remaining,
        "director_rules": {
            "use_only_supplied_truth": True,
            "select_only_registered_capabilities": True,
            "do_not_use_server_scene_templates": True,
            "do_not_repeat_prior_intents": True,
            "do_not_repeat_completed_content_history": True,
            "satisfy_declared_page_range_exactly": True,
            "reserve_enough_diversity_for_remaining_pages": True,
            "obey_reserved_coverage_without_copying_a_template": True,
            "use_exact_differentiation_object_shape": True,
            "copy_confirmed_attribute_from_schema_enum": True,
            "audio_mode_is_authoritative": True,
            "audio_mode_semantics": AUDIO_MODE_SEMANTICS,
            "all_audio_prose_must_match_audio_mode": True,
            "describe_the_viewer_need_without_forcing_pain": True,
            "return": "SeriesSlatePageDraft only",
        },
        "output_contract": output_contract,
    }
    if revision_context:
        packet["revision_context"] = dict(revision_context)
    if coverage_page is not None:
        packet["reserved_coverage_page"] = (
            coverage_page.model_dump(mode="json")
        )
        packet["variant_territory_assignments"] = {
            str(territory.variant_index): {
                "territory_id": territory.territory_id,
                "truth_options": list(
                    territory.truth_options
                ),
                "audience_tension_or_need": territory.audience_tension_or_need,
                "viewer_value_context": (
                    territory.viewer_value_context
                ),
                "response_or_action_route": territory.response_or_action_route,
                "anti_repetition_rule": (
                    territory.anti_repetition_rule
                ),
            }
            for territory in coverage_page.territories
        }
    return packet


def materialize_series_director_briefs(
    brief: DirectorSeriesBrief,
    slate: SeriesSlate,
) -> dict[str, dict[str, Any]]:
    """Create explicit per-variant briefs without inventing creative defaults."""
    finalized = finalize_series_slate(
        SeriesSlateDraft(
            schema_version=slate.schema_version,
            series_id=slate.series_id,
            series_version=slate.series_version,
            intents=slate.intents,
        ),
        brief,
    )
    if finalized.slate_sha256 != slate.slate_sha256:
        raise ValueError("series slate changed during brief materialization")
    rows: dict[str, dict[str, Any]] = {}
    for intent in slate.intents:
        intent_truth = {
            "series_id": slate.series_id,
            "series_version": slate.series_version,
            "slate_sha256": slate.slate_sha256,
            "intent_id": intent.intent_id,
            "variant_index": int(intent.variant_index),
            "audio_mode": intent.audio_mode,
            "creative_strategy": intent.creative_strategy,
            "differentiation": intent.differentiation,
            "pain_hypothesis": (
                intent.pain_hypothesis.model_dump(mode="json")
                if intent.pain_hypothesis is not None
                else None
            ),
            "conversion_hypothesis": (
                intent.conversion_hypothesis.model_dump(mode="json")
                if intent.conversion_hypothesis is not None
                else None
            ),
        }
        project_brief = DirectorProjectBrief(
            brief_id=(
                f"{brief.series_id}.variant-"
                f"{int(intent.variant_index):03d}.v{brief.series_version}"
            ),
            brief_version=brief.series_version,
            objective=intent.objective,
            content_type_hint=intent.content_type,
            audio_mode_hint=intent.audio_mode,
            platform=brief.platform,
            locale=brief.locale,
            audience=intent.audience,
            target_duration_seconds=intent.target_duration_seconds,
            edit_headroom_seconds=brief.edit_headroom_seconds,
            speech_rate_wpm=brief.speech_rate_wpm,
            display_reading_rate_wpm=brief.display_reading_rate_wpm,
            aspect_ratio=brief.aspect_ratio,
            production_contract=brief.production_contract,
            conversion=brief.conversion,
            truth_payload={
                **brief.truth_payload,
                # Preserve the whole-series mandate beside the per-episode
                # intent.  A narrow episode device must still deliver the
                # requested campaign intensity and audience job; otherwise a
                # Director can optimize a prop or routine while silently
                # dropping the reason the series exists.
                "series_objective": brief.objective,
                "series_audience": brief.audience,
                "series_intent": intent_truth,
            },
            truth_options=list(brief.truth_options),
            creative_constraints=list(dict.fromkeys(
                [
                    *brief.creative_constraints,
                    *intent.creative_constraints,
                ]
            )),
            capability_catalog=brief.capability_catalog,
            copy_review_criteria=brief.copy_review_criteria,
            quality_rubric=brief.quality_rubric,
            source_truth_refs=list(dict.fromkeys(
                [
                    *brief.source_truth_refs,
                    *intent.source_truth_refs,
                ]
            )),
        )
        rows[str(int(intent.variant_index))] = (
            project_brief.model_dump(mode="json")
        )
    return rows


class VideoProgramSpec(BaseModel):
    """Director-authored program for any supported video type."""

    schema_version: Literal["1.0", "2.0"] = "1.0"
    program_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=2, max_length=32)
    audience: str = Field(min_length=1, max_length=1000)
    target_duration_seconds: float = Field(gt=0, le=3600)
    aspect_ratio: str = Field(min_length=3, max_length=32)
    audio_mode: Literal[
        "spoken",
        "silent",
        "music_only",
        "sound_design",
    ] | None = None
    creative_strategy: dict[str, Any] = Field(default_factory=dict)
    conversion: ConversionIntent = Field(default_factory=ConversionIntent)
    execution_graph: list[DirectorCapabilityNode] = Field(min_length=1, max_length=64)
    copy_review_criteria: list[CopyReviewCriterion] = Field(
        min_length=1,
        max_length=64,
    )
    quality_rubric: list[str] = Field(default_factory=list, max_length=64)
    source_truth_refs: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_execution_graph(self) -> "VideoProgramSpec":
        if self.schema_version == "2.0" and self.audio_mode is None:
            raise ValueError(
                "VideoProgramSpec v2 requires an explicit audio_mode"
            )
        criterion_ids = [
            criterion.criterion_id
            for criterion in self.copy_review_criteria
        ]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("copy_review_criteria criterion_id values must be unique")
        nodes = {node.node_id: node for node in self.execution_graph}
        if len(nodes) != len(self.execution_graph):
            raise ValueError("execution_graph node_id values must be unique")
        for node in self.execution_graph:
            missing = [dependency for dependency in node.depends_on if dependency not in nodes]
            if missing:
                raise ValueError(
                    f"execution_graph node {node.node_id} has missing dependencies: {missing}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError("execution_graph must be acyclic")
            visiting.add(node_id)
            for dependency in nodes[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in nodes:
            visit(node_id)
        return self


class ScriptLine(BaseModel):
    line_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    speaker_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    beat_id: str = Field(min_length=1, max_length=64)
    purpose: str = Field(min_length=1, max_length=128)
    delivery_mode: Literal["spoken", "display"] = "spoken"


class ScriptSegmentAllocation(BaseModel):
    segment_index: int = Field(ge=1, le=1000)
    duration_seconds: float = Field(gt=0, le=300)
    line_ids: list[str] = Field(default_factory=list, max_length=128)


class ScriptPackage(BaseModel):
    """Immutable full script plus a lossless segment allocation."""

    model_config = ConfigDict(validate_assignment=True)

    schema_version: str = "1.0"
    script_id: str = Field(min_length=1, max_length=128)
    program_id: str = Field(min_length=1, max_length=128)
    locale: str = Field(min_length=2, max_length=32)
    target_duration_seconds: float = Field(gt=0, le=3600)
    edit_headroom_seconds: float = Field(default=0, ge=0, le=600)
    speech_rate_wpm: float = Field(gt=0, le=400)
    display_reading_rate_wpm: float = Field(default=120, gt=0, le=400)
    audio_mode: Literal["spoken", "silent", "music_only", "sound_design"] = "spoken"
    primary_speaker_id: str | None = Field(default=None, min_length=1, max_length=128)
    lines: list[ScriptLine] = Field(default_factory=list, max_length=1000)
    segments: list[ScriptSegmentAllocation] = Field(min_length=1, max_length=1000)
    canonical_text_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_lossless_allocation(self) -> "ScriptPackage":
        line_ids = [line.line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("script line_id values must be unique")
        spoken_lines = [
            line for line in self.lines
            if line.delivery_mode == "spoken"
        ]
        if self.audio_mode == "spoken":
            if not spoken_lines:
                raise ValueError(
                    "spoken audio_mode requires at least one spoken script line"
                )
            if self.primary_speaker_id not in {
                line.speaker_id for line in spoken_lines
            }:
                raise ValueError(
                    "primary_speaker_id must own at least one spoken script line"
                )
        elif spoken_lines or self.primary_speaker_id is not None:
            raise ValueError(
                "non-spoken audio_mode requires display-only lines and no "
                "primary speaker"
            )

        segment_indices = [segment.segment_index for segment in self.segments]
        if segment_indices != list(range(1, len(segment_indices) + 1)):
            raise ValueError("segment_index values must be contiguous and ordered from 1")

        allocated = [
            line_id
            for segment in self.segments
            for line_id in segment.line_ids
        ]
        if allocated != line_ids:
            missing = sorted(set(line_ids) - set(allocated))
            duplicates = sorted(
                line_id for line_id, count in Counter(allocated).items() if count > 1
            )
            unknown = sorted(set(allocated) - set(line_ids))
            raise ValueError(
                "segment allocation must contain every canonical line exactly once "
                f"in order; missing={missing}, duplicates={duplicates}, unknown={unknown}"
            )
        if abs(sum(segment.duration_seconds for segment in self.segments) - self.target_duration_seconds) > 0.05:
            raise ValueError("segment durations must sum to target_duration_seconds")
        digest = script_text_sha256(self.lines)
        if digest != self.canonical_text_sha256:
            raise ValueError("canonical_text_sha256 does not match ordered script lines")
        return self

    @property
    def spoken_word_count(self) -> int:
        return sum(
            len(_words(line.text))
            for line in self.lines
            if line.delivery_mode == "spoken"
        )

    @property
    def spoken_budget_words(self) -> int:
        usable_seconds = max(0.0, self.target_duration_seconds - self.edit_headroom_seconds)
        return int(usable_seconds * self.speech_rate_wpm / 60.0)

    @property
    def display_word_count(self) -> int:
        return sum(
            len(_words(line.text))
            for line in self.lines
            if line.delivery_mode == "display"
        )

    @property
    def display_budget_words(self) -> int:
        usable_seconds = max(
            0.0,
            self.target_duration_seconds - self.edit_headroom_seconds,
        )
        return int(
            usable_seconds * self.display_reading_rate_wpm / 60.0
        )


class ScriptDraft(BaseModel):
    """Model-authored fields; integrity hashes are always computed by runtime."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    script_id: str = Field(min_length=1, max_length=128)
    program_id: str = Field(min_length=1, max_length=128)
    locale: str = Field(min_length=2, max_length=32)
    target_duration_seconds: float = Field(gt=0, le=3600)
    edit_headroom_seconds: float = Field(default=0, ge=0, le=600)
    speech_rate_wpm: float = Field(gt=0, le=400)
    display_reading_rate_wpm: float = Field(default=120, gt=0, le=400)
    audio_mode: Literal["spoken", "silent", "music_only", "sound_design"] = "spoken"
    primary_speaker_id: str | None = Field(default=None, min_length=1, max_length=128)
    lines: list[ScriptLine] = Field(default_factory=list, max_length=1000)
    segments: list[ScriptSegmentAllocation] = Field(min_length=1, max_length=1000)

    def finalize(self) -> ScriptPackage:
        return build_script_package(
            script_id=self.script_id,
            program_id=self.program_id,
            locale=self.locale,
            target_duration_seconds=self.target_duration_seconds,
            edit_headroom_seconds=self.edit_headroom_seconds,
            speech_rate_wpm=self.speech_rate_wpm,
            display_reading_rate_wpm=self.display_reading_rate_wpm,
            audio_mode=self.audio_mode,
            primary_speaker_id=self.primary_speaker_id,
            lines=self.lines,
            segments=self.segments,
        )


class DirectorDraftPayload(BaseModel):
    """Strict raw JSON returned by the isolated director."""

    model_config = ConfigDict(extra="forbid")

    program: VideoProgramSpec
    script: ScriptDraft


class DirectorCapabilitySelection(BaseModel):
    """Author-owned selection of one registered runtime capability.

    Contracts and policies deliberately do not appear here.  The runtime
    resolves them from ``DirectorProjectBrief.capability_catalog`` so a model
    response cannot weaken or accidentally mutate the deployed contract.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    capability: str = Field(min_length=1, max_length=128)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    required: bool = True


class DirectorProgramAuthorDraft(BaseModel):
    """Only the creative program decisions owned by the Director."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    program_id: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=128)
    audio_mode: Literal[
        "spoken",
        "silent",
        "music_only",
        "sound_design",
    ]
    creative_strategy: dict[str, Any] = Field(default_factory=dict)
    execution_graph: list[DirectorCapabilitySelection] = Field(
        min_length=1,
        max_length=64,
    )


class DirectorScriptSegmentDraft(BaseModel):
    """Lossless line allocation without runtime-owned provider timing."""

    model_config = ConfigDict(extra="forbid")

    segment_index: int = Field(ge=1, le=1000)
    line_ids: list[str] = Field(default_factory=list, max_length=128)


class DirectorScriptAuthorDraft(BaseModel):
    """Script authorship without locale, timing, or delivery-rate echoes."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    script_id: str = Field(min_length=1, max_length=128)
    program_id: str = Field(min_length=1, max_length=128)
    audio_mode: Literal["spoken", "silent", "music_only", "sound_design"]
    primary_speaker_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    lines: list[ScriptLine] = Field(default_factory=list, max_length=1000)
    segments: list[DirectorScriptSegmentDraft] = Field(
        min_length=1,
        max_length=1000,
    )


class DirectorAuthorDraftPayload(BaseModel):
    """Current author-only response contract for the isolated Director."""

    model_config = ConfigDict(extra="forbid")

    program: DirectorProgramAuthorDraft
    script: DirectorScriptAuthorDraft

    @model_validator(mode="after")
    def validate_author_identity(self) -> "DirectorAuthorDraftPayload":
        if self.script.program_id != self.program.program_id:
            raise ValueError(
                "author script belongs to a different program"
            )
        if self.script.audio_mode != self.program.audio_mode:
            raise ValueError(
                "author program and script audio modes must match"
            )
        return self


def director_draft_output_contract() -> dict[str, Any]:
    """Return the current authoring contract while preserving v1 reads."""

    output_contract = DirectorDraftPayload.model_json_schema()
    definitions = output_contract.get("$defs", {})
    program_schema = definitions.get("VideoProgramSpec", {})
    program_properties = program_schema.get("properties", {})
    if isinstance(program_properties.get("schema_version"), dict):
        program_properties["schema_version"]["const"] = "2.0"
    required_program = list(program_schema.get("required") or [])
    if "audio_mode" not in required_program:
        required_program.append("audio_mode")
    program_schema["required"] = required_program
    script_schema = definitions.get("ScriptDraft", {})
    required_script = list(script_schema.get("required") or [])
    if "audio_mode" not in required_script:
        required_script.append("audio_mode")
    script_schema["required"] = required_script
    return output_contract


def _brief_segment_durations(
    brief: DirectorProjectBrief,
) -> list[float]:
    if brief.production_contract is None:
        return [round(float(brief.target_duration_seconds), 2)]
    return production_segment_durations(
        brief.production_contract,
        brief.target_duration_seconds,
    )


def director_author_output_contract(
    brief: DirectorProjectBrief,
) -> dict[str, Any]:
    """Return a brief-bound schema containing author-owned fields only."""

    output_contract = DirectorAuthorDraftPayload.model_json_schema()
    definitions = output_contract.get("$defs", {})
    program_schema = definitions.get(
        "DirectorProgramAuthorDraft",
        {},
    )
    program_properties = program_schema.get("properties", {})
    if brief.content_type_hint and isinstance(
        program_properties.get("content_type"),
        dict,
    ):
        program_properties["content_type"]["const"] = (
            brief.content_type_hint
        )
    if brief.audio_mode_hint:
        program_audio = program_properties.get("audio_mode")
        if isinstance(program_audio, dict):
            program_audio["const"] = brief.audio_mode_hint
        script_audio = (
            definitions.get("DirectorScriptAuthorDraft", {})
            .get("properties", {})
            .get("audio_mode")
        )
        if isinstance(script_audio, dict):
            script_audio["const"] = brief.audio_mode_hint

    capability_schema = (
        definitions.get("DirectorCapabilitySelection", {})
        .get("properties", {})
        .get("capability")
    )
    if isinstance(capability_schema, dict):
        capability_schema["enum"] = [
            item.capability for item in brief.capability_catalog
        ]

    script_segments_schema = (
        definitions.get("DirectorScriptAuthorDraft", {})
        .get("properties", {})
        .get("segments")
    )
    if isinstance(script_segments_schema, dict):
        segment_count = len(_brief_segment_durations(brief))
        script_segments_schema["minItems"] = segment_count
        script_segments_schema["maxItems"] = segment_count
    return output_contract


class DirectedContentArtifact(BaseModel):
    """Immutable, runtime-signed director artifact with an explicit ancestry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=1000)
    parent_artifact_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    program: VideoProgramSpec
    script: ScriptPackage
    artifact_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> "DirectedContentArtifact":
        if self.script.program_id != self.program.program_id:
            raise ValueError("artifact script belongs to a different program")
        if self.script.locale.lower() != self.program.locale.lower():
            raise ValueError("artifact script locale does not match program")
        if (
            self.program.audio_mode is not None
            and self.script.audio_mode != self.program.audio_mode
        ):
            raise ValueError(
                "artifact script audio mode does not match program"
            )
        if self.revision == 1 and self.parent_artifact_sha256 is not None:
            raise ValueError("revision 1 cannot have a parent artifact")
        if self.revision > 1 and self.parent_artifact_sha256 is None:
            raise ValueError("revision greater than 1 requires a parent artifact")
        if self.artifact_sha256 != directed_artifact_sha256(
            artifact_id=self.artifact_id,
            revision=self.revision,
            parent_artifact_sha256=self.parent_artifact_sha256,
            program=self.program,
            script=self.script,
        ):
            raise ValueError("artifact_sha256 does not match the director artifact")
        return self


class CopyPreflightIssue(BaseModel):
    code: str
    message: str
    blocking: bool = True
    line_ids: list[str] = Field(default_factory=list)


class CopyPreflightReport(BaseModel):
    approved: bool
    issues: list[CopyPreflightIssue]
    spoken_word_count: int
    spoken_budget_words: int
    display_word_count: int = 0
    display_budget_words: int = 0
    differentiators_found: list[str] = Field(default_factory=list)
    critic_required: bool = True


class CopyCriticBlockingIssue(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    line_ids: list[str] = Field(default_factory=list, max_length=128)
    evidence: str = Field(min_length=1, max_length=4000)
    repair_instruction: str = Field(min_length=1, max_length=4000)


class CopyCriticCriterionEvidence(BaseModel):
    """Auditable proof for one score, grounded only in the final script."""

    line_ids: list[str] = Field(min_length=1, max_length=128)
    quotes: list[str] = Field(min_length=1, max_length=32)
    rationale: str = Field(min_length=20, max_length=4000)

    @model_validator(mode="after")
    def validate_quotes(self) -> "CopyCriticCriterionEvidence":
        cleaned = [str(quote or "").strip() for quote in self.quotes]
        if any(len(quote) < 8 for quote in cleaned):
            raise ValueError("critic evidence quotes must contain at least 8 characters")
        if len(set(self.line_ids)) != len(self.line_ids):
            raise ValueError("critic evidence line_ids must be unique")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("critic evidence quotes must be unique")
        self.quotes = cleaned
        return self


class IndependentCopyCriticVerdict(BaseModel):
    approved: bool
    scores: dict[str, int] = Field(min_length=1, max_length=64)
    criterion_evidence: dict[str, CopyCriticCriterionEvidence] = Field(
        min_length=1,
        max_length=64,
    )
    blocking_issues: list[CopyCriticBlockingIssue] = Field(
        default_factory=list,
        max_length=128,
    )
    repair_scope: Literal["copy_only", "director_replan"]

    @model_validator(mode="after")
    def validate_decision_consistency(self) -> "IndependentCopyCriticVerdict":
        invalid_scores = {
            dimension: score
            for dimension, score in self.scores.items()
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100
        }
        if invalid_scores:
            raise ValueError(f"critic scores must be integers from 0 to 100: {invalid_scores}")
        if self.approved == bool(self.blocking_issues):
            raise ValueError(
                "approved must be true exactly when blocking_issues is empty"
            )
        return self


def script_text_sha256(lines: list[ScriptLine]) -> str:
    canonical = "\n".join(
        f"{line.line_id}\t{line.speaker_id}\t{line.text.strip()}"
        for line in lines
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def directed_artifact_sha256(
    *,
    artifact_id: str,
    revision: int,
    parent_artifact_sha256: str | None,
    program: VideoProgramSpec,
    script: ScriptPackage,
) -> str:
    program_payload = program.model_dump(mode="json")
    # Version-one artifacts predate the structured program audio authority.
    # Excluding the absent field preserves every existing signed hash while
    # version two includes it as part of the immutable program contract.
    if program.schema_version == "1.0" and program.audio_mode is None:
        program_payload.pop("audio_mode", None)
    canonical = json.dumps(
        {
            "artifact_id": artifact_id,
            "revision": int(revision),
            "parent_artifact_sha256": parent_artifact_sha256,
            "program": program_payload,
            "script": script.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_directed_content_artifact(
    *,
    artifact_id: str,
    revision: int,
    program: VideoProgramSpec,
    script: ScriptPackage,
    parent_artifact_sha256: str | None = None,
) -> DirectedContentArtifact:
    return DirectedContentArtifact(
        artifact_id=artifact_id,
        revision=revision,
        parent_artifact_sha256=parent_artifact_sha256,
        program=program,
        script=script,
        artifact_sha256=directed_artifact_sha256(
            artifact_id=artifact_id,
            revision=revision,
            parent_artifact_sha256=parent_artifact_sha256,
            program=program,
            script=script,
        ),
    )


def parse_director_draft_response(
    response_text: str,
    *,
    artifact_id: str,
    revision: int,
    parent_artifact_sha256: str | None = None,
) -> DirectedContentArtifact:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 1_000_000:
        raise ValueError("director response is empty or exceeds the response limit")
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError("director response must be raw JSON without markdown fences")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("director response is not valid JSON") from exc
    draft = DirectorDraftPayload.model_validate(payload)
    script = draft.script.finalize()
    return build_directed_content_artifact(
        artifact_id=artifact_id,
        revision=revision,
        parent_artifact_sha256=parent_artifact_sha256,
        program=draft.program,
        script=script,
    )


def build_delivery_budget_contract(
    brief: DirectorProjectBrief,
    *,
    segment_durations: list[float] | None = None,
) -> dict[str, Any]:
    """Expand timing policy into exact, model-readable word ceilings."""
    usable_seconds = max(
        0.0,
        brief.target_duration_seconds - brief.edit_headroom_seconds,
    )
    durations = (
        [
            float(value)
            for value in segment_durations
        ]
        if segment_durations is not None
        else (
            production_segment_durations(
                brief.production_contract,
                brief.target_duration_seconds,
            )
            if brief.production_contract is not None
            else []
        )
    )
    return {
        "spoken_global_max_words": int(
            usable_seconds * brief.speech_rate_wpm / 60.0
        ),
        "display_global_max_words": int(
            usable_seconds
            * brief.display_reading_rate_wpm
            / 60.0
        ),
        "segments": [
            {
                "segment_index": index,
                "duration_seconds": duration,
                "spoken_max_words": int(
                    duration * brief.speech_rate_wpm / 60.0
                ),
                "display_max_words": int(
                    duration
                    * brief.display_reading_rate_wpm
                    / 60.0
                ),
            }
            for index, duration in enumerate(durations, 1)
        ],
        "rules": {
            "count_each_line_in_its_delivery_mode": True,
            "every_segment_must_fit_its_own_budget": True,
            "global_budget_does_not_override_segment_budget": True,
            "self_count_before_returning": True,
        },
    }


def build_initial_director_packet(brief: DirectorProjectBrief) -> dict[str, Any]:
    verbatim_blocks = _required_verbatim_voiceover_blocks(
        brief.truth_payload
    )
    return {
        "schema_version": "1.0",
        "role": "content_director",
        "project_brief": brief.model_dump(mode="json"),
        "delivery_budget_contract": build_delivery_budget_contract(
            brief
        ),
        "director_rules": {
            "use_only_supplied_truth": True,
            "select_only_registered_capabilities": True,
            "return_only_author_owned_fields": True,
            "runtime_materializes_project_truth_and_capability_policies": True,
            "write_complete_script_before_media": True,
            "obey_spoken_and_display_reading_budgets": True,
            "program_and_script_audio_mode_must_match": True,
            "audio_mode_semantics": {
                **AUDIO_MODE_SEMANTICS,
            },
            "creative_strategy_audio_prose_cannot_contradict_audio_mode": True,
            "required_verbatim_voiceover": (
                {
                    "runtime_locked": True,
                    "required_spoken_line_count": len(verbatim_blocks),
                    "director_owns_words": False,
                    "director_owns_line_metadata_and_segment_allocation": True,
                    "instruction": (
                        "Create exactly one spoken script line for each ordered "
                        "blank-line-delimited source block. The runtime will "
                        "materialize the exact source words; do not combine, "
                        "split, add, or remove spoken lines."
                    ),
                }
                if verbatim_blocks is not None
                else None
            ),
            "return": (
                "DirectorAuthorDraftPayload only; runtime materializes "
                "project-owned fields, provider timing, and hashes"
            ),
        },
        "runtime_owned_fields": [
            "objective",
            "platform",
            "locale",
            "audience",
            "target_duration_seconds",
            "aspect_ratio",
            "conversion",
            "capability input_contract/output_contract/policy",
            "copy_review_criteria",
            "quality_rubric",
            "source_truth_refs",
            "script locale/duration/edit_headroom/delivery_rates",
            "provider segment durations",
            "integrity hashes",
        ],
        "output_contract": director_author_output_contract(brief),
    }


def validate_directed_artifact_against_brief(
    artifact: DirectedContentArtifact,
    brief: DirectorProjectBrief,
) -> None:
    program = artifact.program
    exact_fields = {
        "objective": (program.objective, brief.objective),
        "platform": (program.platform, brief.platform),
        "locale": (program.locale, brief.locale),
        "audience": (program.audience, brief.audience),
        "target_duration_seconds": (
            program.target_duration_seconds,
            brief.target_duration_seconds,
        ),
        "aspect_ratio": (program.aspect_ratio, brief.aspect_ratio),
        "script_edit_headroom_seconds": (
            artifact.script.edit_headroom_seconds,
            brief.edit_headroom_seconds,
        ),
        "script_speech_rate_wpm": (
            artifact.script.speech_rate_wpm,
            brief.speech_rate_wpm,
        ),
        "script_display_reading_rate_wpm": (
            artifact.script.display_reading_rate_wpm,
            brief.display_reading_rate_wpm,
        ),
    }
    mismatches = [
        field
        for field, (actual, expected) in exact_fields.items()
        if actual != expected
    ]
    if mismatches:
        raise ValueError(f"director changed project-owned fields: {mismatches}")
    if program.conversion.model_dump(mode="json") != brief.conversion.model_dump(mode="json"):
        raise ValueError("director changed the project-owned conversion intent")
    if (
        [row.model_dump(mode="json") for row in program.copy_review_criteria]
        != [row.model_dump(mode="json") for row in brief.copy_review_criteria]
    ):
        raise ValueError("director changed project-owned copy review criteria")
    if program.quality_rubric != brief.quality_rubric:
        raise ValueError("director changed the project-owned quality rubric")
    if program.source_truth_refs != brief.source_truth_refs:
        raise ValueError("director dropped or changed source truth references")
    if program.schema_version == "2.0" and (
        program.audio_mode != artifact.script.audio_mode
    ):
        raise ValueError(
            "director program and script audio mode must match"
        )
    if brief.content_type_hint and program.content_type != brief.content_type_hint:
        raise ValueError("director changed the requested content type")
    if brief.audio_mode_hint and (
        program.audio_mode != brief.audio_mode_hint
        or artifact.script.audio_mode != brief.audio_mode_hint
    ):
        raise ValueError("director changed the requested audio mode")
    if brief.production_contract is not None:
        expected_segment_durations = production_segment_durations(
            brief.production_contract,
            artifact.script.target_duration_seconds,
        )
        actual_segment_durations = [
            round(float(item.duration_seconds), 2)
            for item in artifact.script.segments
        ]
        if actual_segment_durations != expected_segment_durations:
            raise ValueError(
                "director script segment durations do not match the "
                "registered video production contract: "
                f"expected={expected_segment_durations}, "
                f"actual={actual_segment_durations}"
            )

    catalog = {
        row.capability: row
        for row in brief.capability_catalog
    }
    for node in program.execution_graph:
        spec = catalog.get(node.capability)
        if spec is None:
            raise ValueError(
                f"director selected unregistered capability: {node.capability}"
            )
        if (
            node.input_contract != spec.input_contract
            or node.output_contract != spec.output_contract
        ):
            raise ValueError(
                f"director changed contracts for capability: {node.capability}"
            )
        if node.policy != spec.policy:
            raise ValueError(
                f"director changed policy for capability: {node.capability}"
            )


def build_script_package(
    *,
    schema_version: str = "1.0",
    script_id: str,
    program_id: str,
    locale: str,
    target_duration_seconds: float,
    edit_headroom_seconds: float,
    speech_rate_wpm: float,
    audio_mode: Literal["spoken", "silent", "music_only", "sound_design"] = "spoken",
    primary_speaker_id: str | None,
    lines: list[ScriptLine],
    segments: list[ScriptSegmentAllocation],
    display_reading_rate_wpm: float = 120,
) -> ScriptPackage:
    return ScriptPackage(
        schema_version=schema_version,
        script_id=script_id,
        program_id=program_id,
        locale=locale,
        target_duration_seconds=target_duration_seconds,
        edit_headroom_seconds=edit_headroom_seconds,
        speech_rate_wpm=speech_rate_wpm,
        display_reading_rate_wpm=display_reading_rate_wpm,
        audio_mode=audio_mode,
        primary_speaker_id=primary_speaker_id,
        lines=lines,
        segments=segments,
        canonical_text_sha256=script_text_sha256(lines),
    )


def finalize_director_author_draft(
    draft: DirectorAuthorDraftPayload,
    brief: DirectorProjectBrief,
    *,
    artifact_id: str,
    revision: int,
    parent_artifact_sha256: str | None = None,
) -> DirectedContentArtifact:
    """Materialize one author response with runtime-owned project truth.

    The Director chooses creative strategy, content form, audio mode, script,
    and capability topology.  The runtime injects objective, audience,
    locale, duration, conversion truth, review criteria, registered capability
    contracts/policies, provider timing, and all integrity hashes.
    """

    if (
        brief.content_type_hint
        and draft.program.content_type != brief.content_type_hint
    ):
        raise ValueError("director changed the requested content type")
    if brief.audio_mode_hint and (
        draft.program.audio_mode != brief.audio_mode_hint
        or draft.script.audio_mode != brief.audio_mode_hint
    ):
        raise ValueError("director changed the requested audio mode")

    catalog = {
        item.capability: item
        for item in brief.capability_catalog
    }
    selected_nodes: list[DirectorCapabilityNode] = []
    selected_ids: set[str] = set()
    for selection in draft.program.execution_graph:
        if selection.node_id in selected_ids:
            raise ValueError(
                "author execution graph node IDs must be unique"
            )
        selected_ids.add(selection.node_id)
        spec = catalog.get(selection.capability)
        if spec is None:
            raise ValueError(
                "director selected unregistered capability: "
                f"{selection.capability}"
            )
        selected_nodes.append(
            DirectorCapabilityNode(
                node_id=selection.node_id,
                capability=selection.capability,
                depends_on=list(selection.depends_on),
                required=selection.required,
                input_contract=spec.input_contract,
                output_contract=spec.output_contract,
                policy=dict(spec.policy),
            )
        )

    program = VideoProgramSpec(
        schema_version="2.0",
        program_id=draft.program.program_id,
        objective=brief.objective,
        content_type=draft.program.content_type,
        platform=brief.platform,
        locale=brief.locale,
        audience=brief.audience,
        target_duration_seconds=brief.target_duration_seconds,
        aspect_ratio=brief.aspect_ratio,
        audio_mode=draft.program.audio_mode,
        creative_strategy=dict(draft.program.creative_strategy),
        conversion=brief.conversion,
        execution_graph=selected_nodes,
        copy_review_criteria=brief.copy_review_criteria,
        quality_rubric=list(brief.quality_rubric),
        source_truth_refs=list(brief.source_truth_refs),
    )

    expected_durations = _brief_segment_durations(brief)
    if len(draft.script.segments) != len(expected_durations):
        raise ValueError(
            "author script segment count does not match the registered "
            "production plan: "
            f"expected={len(expected_durations)}, "
            f"actual={len(draft.script.segments)}"
        )
    expected_indices = list(range(1, len(expected_durations) + 1))
    actual_indices = [
        item.segment_index for item in draft.script.segments
    ]
    if actual_indices != expected_indices:
        raise ValueError(
            "author script segment indices must be contiguous and ordered "
            f"from 1: expected={expected_indices}, actual={actual_indices}"
        )
    segments = [
        ScriptSegmentAllocation(
            segment_index=index,
            duration_seconds=duration,
            line_ids=list(author_segment.line_ids),
        )
        for index, (duration, author_segment) in enumerate(
            zip(expected_durations, draft.script.segments, strict=True),
            1,
        )
    ]
    script_lines = _materialize_required_verbatim_voiceover(
        list(draft.script.lines),
        truth_payload=brief.truth_payload,
    )
    script = build_script_package(
        schema_version="2.0",
        script_id=draft.script.script_id,
        program_id=draft.script.program_id,
        locale=brief.locale,
        target_duration_seconds=brief.target_duration_seconds,
        edit_headroom_seconds=brief.edit_headroom_seconds,
        speech_rate_wpm=brief.speech_rate_wpm,
        display_reading_rate_wpm=brief.display_reading_rate_wpm,
        audio_mode=draft.script.audio_mode,
        primary_speaker_id=draft.script.primary_speaker_id,
        lines=script_lines,
        segments=segments,
    )
    artifact = build_directed_content_artifact(
        artifact_id=artifact_id,
        revision=revision,
        parent_artifact_sha256=parent_artifact_sha256,
        program=program,
        script=script,
    )
    validate_directed_artifact_against_brief(artifact, brief)
    return artifact


def parse_director_author_draft_response(
    response_text: str,
    *,
    brief: DirectorProjectBrief,
    artifact_id: str,
    revision: int,
    parent_artifact_sha256: str | None = None,
) -> DirectedContentArtifact:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 1_000_000:
        raise ValueError(
            "director response is empty or exceeds the response limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "director response must be raw JSON without markdown fences"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("director response is not valid JSON") from exc
    return finalize_director_author_draft(
        DirectorAuthorDraftPayload.model_validate(payload),
        brief,
        artifact_id=artifact_id,
        revision=revision,
        parent_artifact_sha256=parent_artifact_sha256,
    )


def script_package_from_creative_result(
    result: dict[str, Any],
    *,
    script_id: str,
    program_id: str,
    locale: str,
    edit_headroom_seconds: float = 0,
) -> ScriptPackage:
    """Adapt the deployed CREATIVE result without changing a single word."""
    script = dict(result.get("complete_video_script") or {})
    voice_bible = dict(result.get("voice_bible") or {})
    primary = str(voice_bible.get("primary_speaker_id") or "").strip()
    speakers = {
        str(dict(row).get("speaker_id") or dict(row).get("name") or ""): dict(row)
        for row in list(voice_bible.get("speakers") or [])
        if isinstance(row, dict)
    }
    speech_rate = float(
        dict(speakers.get(primary) or {}).get("speech_rate")
        or 150
    )
    lines: list[ScriptLine] = []
    segments: list[ScriptSegmentAllocation] = []
    for segment_position, raw_segment in enumerate(list(script.get("segments") or []), 1):
        segment = dict(raw_segment or {})
        segment_index = int(segment.get("segment_index") or segment_position)
        allocated: list[str] = []
        for line_position, raw_line in enumerate(list(segment.get("dialogue_lines") or []), 1):
            dialogue = dict(raw_line or {})
            line_id = f"s{segment_index:03d}.l{line_position:03d}"
            speaker_id = str(
                dialogue.get("speaker_id")
                or dialogue.get("speaker")
                or primary
            ).strip()
            lines.append(
                ScriptLine(
                    line_id=line_id,
                    speaker_id=speaker_id,
                    text=str(dialogue.get("line") or "").strip(),
                    beat_id=f"segment-{segment_index}",
                    purpose=str(segment.get("story_function") or "spoken_copy"),
                )
            )
            allocated.append(line_id)
        segments.append(
            ScriptSegmentAllocation(
                segment_index=segment_index,
                duration_seconds=float(segment.get("duration_seconds") or 0),
                line_ids=allocated,
            )
        )
    if not primary and lines:
        primary = lines[0].speaker_id
    return build_script_package(
        script_id=script_id,
        program_id=program_id,
        locale=locale,
        target_duration_seconds=float(script.get("duration_seconds") or 0),
        edit_headroom_seconds=edit_headroom_seconds,
        speech_rate_wpm=speech_rate,
        audio_mode="spoken",
        primary_speaker_id=primary,
        lines=lines,
        segments=segments,
    )


def preflight_script_copy(
    program: VideoProgramSpec,
    script: ScriptPackage,
) -> CopyPreflightReport:
    """Run deterministic, project-driven checks before an independent critic.

    This gate proves structural completeness and confirmed product rationale.
    It intentionally does not claim to judge natural American phrasing,
    emotional truth, or cultural fit; those remain mandatory work for the
    isolated model critic represented by ``critic_required=true``.
    """
    issues: list[CopyPreflightIssue] = []
    if script.program_id != program.program_id:
        issues.append(
            CopyPreflightIssue(
                code="PROGRAM_ID_MISMATCH",
                message="Script belongs to a different director program.",
            )
        )
    if script.locale.lower() != program.locale.lower():
        issues.append(
            CopyPreflightIssue(
                code="LOCALE_MISMATCH",
                message="Script locale does not match the director program.",
            )
        )
    if abs(script.target_duration_seconds - program.target_duration_seconds) > 0.05:
        issues.append(
            CopyPreflightIssue(
                code="DURATION_MISMATCH",
                message="Script duration does not match the director program.",
            )
        )
    if script.spoken_word_count > script.spoken_budget_words:
        issues.append(
            CopyPreflightIssue(
                code="SPOKEN_COPY_OVER_BUDGET",
                message=(
                    f"Spoken copy has {script.spoken_word_count} words but the "
                    f"configured budget is {script.spoken_budget_words}."
                ),
                line_ids=[line.line_id for line in script.lines],
            )
        )
    lines_by_id = {line.line_id: line for line in script.lines}
    for segment in script.segments:
        segment_spoken_lines = [
            lines_by_id[line_id]
            for line_id in segment.line_ids
            if lines_by_id[line_id].delivery_mode == "spoken"
        ]
        segment_words = sum(
            len(_words(line.text))
            for line in segment_spoken_lines
        )
        segment_budget = int(
            float(segment.duration_seconds)
            * script.speech_rate_wpm
            / 60.0
        )
        # The project rate is a natural delivery target, not a frame-exact
        # metronome.  A single extra word in a ten-second spoken beat is only
        # a few percent of pacing variance and is routinely absorbed by
        # prosody.  Keep the exact whole-video budget as the hard ceiling, but
        # allow a small proportional per-segment tolerance so the author is
        # not billed repeatedly to remove one word from otherwise valid copy.
        # Dense beats remain blocked, and slack cannot accumulate because the
        # global spoken budget above is still strict.
        segment_tolerance = max(
            1,
            int(math.floor(float(segment_budget) * 0.05)),
        )
        segment_delivery_limit = segment_budget + segment_tolerance
        if segment_words > segment_delivery_limit:
            issues.append(
                CopyPreflightIssue(
                    code="SPOKEN_SEGMENT_OVER_BUDGET",
                    message=(
                        f"Segment {segment.segment_index} speaks "
                        f"{segment_words} words but its configured delivery "
                        f"limit is {segment_delivery_limit} "
                        f"(target {segment_budget} plus "
                        f"{segment_tolerance} word pacing tolerance)."
                    ),
                    line_ids=[
                        line.line_id
                        for line in segment_spoken_lines
                    ],
                )
            )
    display_lines = [
        line
        for line in script.lines
        if line.delivery_mode == "display"
    ]
    if script.display_word_count > script.display_budget_words:
        issues.append(
            CopyPreflightIssue(
                code="DISPLAY_COPY_OVER_BUDGET",
                message=(
                    f"Displayed copy has {script.display_word_count} words "
                    f"but the configured reading budget is "
                    f"{script.display_budget_words}."
                ),
                line_ids=[line.line_id for line in display_lines],
            )
        )
    for segment in script.segments:
        segment_display_lines = [
            lines_by_id[line_id]
            for line_id in segment.line_ids
            if lines_by_id[line_id].delivery_mode == "display"
        ]
        segment_words = sum(
            len(_words(line.text))
            for line in segment_display_lines
        )
        segment_budget = int(
            float(segment.duration_seconds)
            * script.display_reading_rate_wpm
            / 60.0
        )
        if segment_words > segment_budget:
            issues.append(
                CopyPreflightIssue(
                    code="DISPLAY_SEGMENT_OVER_BUDGET",
                    message=(
                        f"Segment {segment.segment_index} displays "
                        f"{segment_words} words but its configured reading "
                        f"budget is {segment_budget}."
                    ),
                    line_ids=[
                        line.line_id
                        for line in segment_display_lines
                    ],
                )
            )
    fragments = [
        line.line_id
        for line in script.lines
        if not _SENTENCE_END_RE.search(line.text.strip())
    ]
    if fragments:
        issues.append(
            CopyPreflightIssue(
                code="INCOMPLETE_SPOKEN_SENTENCE",
                message="Every allocated spoken line must end as a complete sentence.",
                line_ids=fragments,
            )
        )

    conversion = program.conversion
    differentiators_found: list[str] = []
    if conversion.product_required:
        # Script lines are the canonical audience-facing copy for every audio
        # mode. In non-spoken work they are rendered as local display copy, so
        # requiring the exact product, offer, CTA, and differentiators to be
        # duplicated inside free-form creative_strategy creates two competing
        # authorities. Validate the canonical lines here; the independently
        # reviewed production plan owns their later visual delivery.
        reveal_threshold = (
            max(
                1,
                int(
                    len(script.segments)
                    * conversion.reveal_after_fraction
                    + 0.999999
                ),
            )
            if conversion.reveal_after_fraction is not None
            else None
        )
        product_token_sets = [
            tokens
            for tokens in (
                _meaningful_tokens(str(identity or ""))
                for identity in (
                    conversion.product_name,
                    *conversion.product_name_aliases,
                )
            )
            if tokens
        ]
        product_tokens = set().union(*product_token_sets)
        product_line_positions = [
            (index, line)
            for index, line in enumerate(script.lines)
            if product_token_sets
            and any(
                tokens <= _meaningful_tokens(line.text)
                for tokens in product_token_sets
            )
        ]
        if not product_line_positions:
            issues.append(
                CopyPreflightIssue(
                    code="PRODUCT_NOT_NAMED",
                    message="Product-required script never names the configured product.",
                )
            )
        else:
            line_to_segment = {
                line_id: segment.segment_index
                for segment in script.segments
                for line_id in segment.line_ids
            }
            early = (
                [
                    line.line_id
                    for _, line in product_line_positions
                    if line_to_segment.get(line.line_id, 0)
                    < reveal_threshold
                ]
                if reveal_threshold is not None
                else []
            )
            if early:
                issues.append(
                    CopyPreflightIssue(
                        code="PRODUCT_REVEALED_TOO_EARLY",
                        message=(
                            "Product appears before the director-configured reveal "
                            f"boundary at segment {reveal_threshold}."
                        ),
                        line_ids=early,
                    )
                )

        all_copy = "\n".join(line.text for line in script.lines).lower()
        for differentiator in conversion.confirmed_differentiators:
            normalized = str(differentiator or "").strip()
            if normalized and normalized.lower() in all_copy:
                differentiators_found.append(normalized)
        if (
            len(differentiators_found)
            < conversion.minimum_differentiators_in_copy
        ):
            issues.append(
                CopyPreflightIssue(
                    code="WHY_CHOOSE_PRODUCT_MISSING",
                    message=(
                        "Copy does not state enough confirmed reasons to choose "
                        f"the product; found {len(differentiators_found)}, requires "
                        f"{conversion.minimum_differentiators_in_copy}."
                    ),
                )
            )

        offer_tokens = _meaningful_tokens(
            str(conversion.offer_text or "")
        )
        offer_text = re.sub(
            r"\s+",
            "",
            str(conversion.offer_text or "").casefold(),
        )
        normalized_script_text = re.sub(
            r"\s+",
            "",
            "\n".join(line.text for line in script.lines).casefold(),
        )
        if offer_text and offer_text not in normalized_script_text:
            issues.append(
                CopyPreflightIssue(
                    code="CONFIRMED_OFFER_MISSING",
                    message="Configured offer is missing from audience-facing copy.",
                )
            )
        cta_tokens = _meaningful_tokens(str(conversion.cta_text or ""))
        if cta_tokens and not any(
            cta_tokens <= _meaningful_tokens(line.text)
            for line in script.lines
        ):
            issues.append(
                CopyPreflightIssue(
                    code="CONFIRMED_CTA_MISSING",
                    message="Configured CTA is missing from audience-facing copy.",
                )
            )

        protected = {
            token
            for term in conversion.protected_stake_terms
            for token in _meaningful_tokens(term)
        }
        product_or_offer_tokens = (
            product_tokens | offer_tokens | cta_tokens
        )
        causal_lines: list[str] = []
        for line in script.lines:
            tokens = _meaningful_tokens(line.text)
            if (
                tokens & product_or_offer_tokens
                and tokens & protected
            ):
                causal_lines.append(line.line_id)
        if causal_lines:
            issues.append(
                CopyPreflightIssue(
                    code="PRODUCT_CAUSAL_STAKE_COLLISION",
                    message=(
                        "A product/offer sentence also claims or invokes the protected "
                        "opening stake. Separate the product rationale from the human "
                        "decision so the product is not written as preventing or "
                        "repairing the loss."
                    ),
                    line_ids=causal_lines,
                )
            )

        if conversion.require_post_cta_human_agency:
            cta_positions = [
                index
                for index, line in enumerate(script.lines)
                if cta_tokens and cta_tokens <= _meaningful_tokens(line.text)
            ]
            last_cta = max(cta_positions) if cta_positions else len(script.lines)
            agency_tokens = {
                token
                for term in conversion.post_cta_agency_terms
                for token in _meaningful_tokens(term)
            }
            if not any(
                _meaningful_tokens(line.text) & agency_tokens
                for line in script.lines[last_cta + 1 :]
            ):
                issues.append(
                    CopyPreflightIssue(
                        code="POST_CTA_HUMAN_AGENCY_MISSING",
                        message=(
                            "The configured CTA must be followed by a separate "
                            "human-agency ending."
                        ),
                    )
                )

    return CopyPreflightReport(
        approved=not any(issue.blocking for issue in issues),
        issues=issues,
        spoken_word_count=script.spoken_word_count,
        spoken_budget_words=script.spoken_budget_words,
        display_word_count=script.display_word_count,
        display_budget_words=script.display_budget_words,
        differentiators_found=differentiators_found,
        critic_required=True,
    )


def build_independent_copy_critic_packet(
    program: VideoProgramSpec,
    script: ScriptPackage,
    preflight: CopyPreflightReport,
    *,
    brief: DirectorProjectBrief | None = None,
) -> dict[str, Any]:
    """Build a stateless reviewer packet; no author conversation is included."""
    verbatim_blocks = _required_verbatim_voiceover_blocks(
        brief.truth_payload if brief is not None else None
    )
    return {
        "schema_version": "1.0",
        "role": "independent_copy_critic",
        "store": False,
        "project_brief": (
            brief.model_dump(mode="json")
            if brief is not None
            else None
        ),
        "program": program.model_dump(mode="json"),
        "script": script.model_dump(mode="json"),
        "deterministic_preflight": preflight.model_dump(mode="json"),
        "audio_authority": {
            "program_audio_mode": program.audio_mode,
            "script_audio_mode": script.audio_mode,
            "rules": {
                "program_and_script_must_match": True,
                "creative_strategy_prose_cannot_contradict_mode": True,
                "silent_has_no_audible_sound": True,
                "music_only_has_no_voice_or_sound_effects": True,
                "sound_design_has_no_spoken_copy": True,
            },
        },
        "review_criteria": [
            criterion.model_dump(mode="json")
            for criterion in program.copy_review_criteria
        ],
        "review_method": {
            "immutable_user_copy_authority": (
                "The exact spoken copy is user-supplied and runtime-locked. "
                "Verify fidelity, truth, safety, compliance, delivery, and the "
                "criteria that the words can actually prove. Do not demand a "
                "rewrite for stylistic escalation, and do not judge ungenerated "
                "visual execution from the copy."
                if verbatim_blocks is not None
                else None
            ),
            "score_only_the_final_copy": (
                "Judge the exact script text. Do not award credit for program "
                "labels, creative_strategy, line purpose labels, or intended meaning "
                "that the audience never hears or sees."
            ),
            "prove_every_score": (
                "For every criterion_id, cite one or more exact script quotes and "
                "explain why those words meet or miss that criterion's threshold."
            ),
            "setup_is_not_consequence": (
                "A count, workload fact, interruption, symptom, or abstract metaphor "
                "may establish the setup but does not by itself establish a meaningful "
                "human stake, consequence, or loss when the criterion asks for one."
            ),
            "requested_intensity_is_binding": (
                "Read the whole-series objective and audience in project_brief."
                "truth_payload together with the episode objective and pain hypothesis. "
                "When they explicitly request sharp, deep, urgent, painful, or high-stakes "
                "communication, generic friction or an unfinished routine cannot receive "
                "a passing stakes score. Quote the exact downstream human loss, identity "
                "conflict, relationship or professional cost, repeated consequence, "
                "missed life moment, or fear of no longer feeling like oneself. For a "
                "neutral objective, do not manufacture pain."
            ),
            "consequence_is_not_a_restatement": (
                "Repeating or relabeling the setup as more instances of the same setup "
                "does not prove a downstream human consequence. When a criterion asks "
                "for felt loss or stakes, the exact copy must show what concretely "
                "changed, disappeared, was missed, or became harder for the person."
            ),
            "adjacency_is_not_a_bridge": (
                "A product appearing after the problem, or transition words such as "
                "then, so, next, or next step, do not by themselves prove relevance. "
                "The product does not need to solve the opening problem and human agency "
                "may resolve part of it, but the audience's established decision path "
                "must continue into the product use case. Do not approve a newly invented "
                "or unrelated preference after the opening problem has been closed. The "
                "exact copy must make the shared causal or psychological progression and "
                "the product's bounded role understandable."
            ),
            "new_use_case_must_be_established": (
                "A distinct product-relevant use case cannot be inserted merely by "
                "announcing an additional desire. The exact progression must make clear "
                "why that question, decision, comparison, demonstration, or use case is "
                "now the audience's relevant next consideration."
            ),
            "category_entry_precedes_attribute_selection": (
                "A confirmed-attribute preference may explain why this product is "
                "considered among alternatives, but it cannot create the need for the "
                "product category or use case. In delayed-reveal work, exact earlier "
                "copy must first make that category-level decision relevant."
            ),
            "preference_is_not_a_reason": (
                "A relevant preference or selection requirement established before the "
                "product reveal can be a valid decision basis when a confirmed attribute "
                "clearly matches it. In story-led or delayed-reveal work it must be a "
                "completed earlier audience-facing line or beat; wording earlier in the "
                "same reveal sentence or line does not count. Merely asking whether the "
                "viewer wants the matching attribute, or saying the speaker wanted, "
                "chose, liked, or added it during reveal, is circular. Product-first "
                "demonstrations and comparisons may establish their basis in the opening."
            ),
            "fail_closed": (
                "If exact quoted copy cannot prove a blocking criterion at its minimum "
                "score, score it below threshold and return a concrete blocking issue."
            ),
        },
        "decision_contract": {
            "approved": "boolean",
            "scores": "object with 0-100 per review_criteria criterion_id",
            "criterion_evidence": (
                "object keyed by every criterion_id; each value must contain valid "
                "script line_ids, exact quotes from those lines, and threshold rationale"
            ),
            "blocking_issues": "array with line_ids, evidence, and repair instruction",
            "repair_scope": (
                "required top-level field: copy_only or director_replan"
            ),
        },
        "output_contract": (
            IndependentCopyCriticVerdict.model_json_schema()
        ),
    }


def parse_independent_copy_critic_response(
    response_text: str,
    *,
    packet: dict[str, Any],
    script: ScriptPackage,
    preflight: CopyPreflightReport,
) -> IndependentCopyCriticVerdict:
    """Fail closed when an isolated critic violates its explicit contract."""
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 200_000:
        raise ValueError("critic response is empty or exceeds the response limit")
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError("critic response must be raw JSON without markdown fences")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("critic response is not valid JSON") from exc
    verdict = IndependentCopyCriticVerdict.model_validate(payload)

    raw_criteria = list(packet.get("review_criteria") or [])
    expected = {
        str(dict(criterion).get("criterion_id") or "")
        for criterion in raw_criteria
        if isinstance(criterion, dict)
    }
    actual = set(verdict.scores)
    if actual != expected:
        raise ValueError(
            "critic score dimensions do not match the request; "
            f"missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )
    evidence_dimensions = set(verdict.criterion_evidence)
    if evidence_dimensions != expected:
        raise ValueError(
            "critic evidence dimensions do not match the request; "
            f"missing={sorted(expected - evidence_dimensions)}, "
            f"unknown={sorted(evidence_dimensions - expected)}"
        )
    known_line_ids = {line.line_id for line in script.lines}
    unknown_line_ids = sorted(
        {
            line_id
            for issue in verdict.blocking_issues
            for line_id in issue.line_ids
            if line_id not in known_line_ids
        }
    )
    if unknown_line_ids:
        raise ValueError(f"critic cited unknown script line_ids: {unknown_line_ids}")
    script_lines = {line.line_id: line.text for line in script.lines}
    for criterion_id, evidence in verdict.criterion_evidence.items():
        unknown_evidence_line_ids = sorted(
            line_id for line_id in evidence.line_ids if line_id not in known_line_ids
        )
        if unknown_evidence_line_ids:
            raise ValueError(
                "critic evidence cited unknown script line_ids for "
                f"{criterion_id}: {unknown_evidence_line_ids}"
            )
        cited_texts = [script_lines[line_id] for line_id in evidence.line_ids]
        for quote in evidence.quotes:
            if not any(quote.casefold() in text.casefold() for text in cited_texts):
                raise ValueError(
                    "critic evidence quote is not present in its cited script lines "
                    f"for {criterion_id}: {quote!r}"
                )
    if verdict.approved and "reason_to_choose" in expected:
        # For delayed-reveal stories, a critic cannot manufacture a decision
        # basis from words earlier in the same product line.  Require its own
        # evidence to cite at least one completed audience-facing line before
        # the first product identity. Product-first demonstrations and
        # comparisons deliberately skip this ordering rule because their
        # opening format may establish the basis while showing the product.
        program_payload = dict(packet.get("program") or {})
        conversion_payload = dict(program_payload.get("conversion") or {})
        product_identities = [
            conversion_payload.get("product_name"),
            *list(conversion_payload.get("product_name_aliases") or []),
        ]
        product_token_sets = [
            tokens
            for tokens in (
                _meaningful_tokens(str(identity or ""))
                for identity in product_identities
            )
            if tokens
        ]
        first_product_line_index = next(
            (
                index
                for index, line in enumerate(script.lines)
                if any(
                    tokens <= _meaningful_tokens(line.text)
                    for tokens in product_token_sets
                )
            ),
            None,
        )
        if first_product_line_index is not None and first_product_line_index > 0:
            earlier_line_ids = {
                line.line_id
                for line in script.lines[:first_product_line_index]
            }
            reason_evidence_ids = set(
                verdict.criterion_evidence["reason_to_choose"].line_ids
            )
            if not reason_evidence_ids & earlier_line_ids:
                raise ValueError(
                    "approved delayed-reveal reason_to_choose evidence must cite "
                    "a completed audience-facing line before the product reveal; "
                    "the reveal line cannot invent its own selection basis"
                )
    if not preflight.approved and verdict.approved:
        raise ValueError(
            "critic cannot approve copy that failed deterministic preflight"
        )
    below_blocking_threshold = sorted(
        str(criterion.get("criterion_id") or "")
        for criterion in raw_criteria
        if bool(criterion.get("blocking", True))
        and verdict.scores.get(str(criterion.get("criterion_id") or ""), -1)
        < int(criterion.get("minimum_score") or 0)
    )
    if verdict.approved and below_blocking_threshold:
        raise ValueError(
            "critic approved scores below project-owned blocking thresholds: "
            f"{below_blocking_threshold}"
        )
    return verdict


def build_director_revision_packet(
    artifact: DirectedContentArtifact,
    *,
    brief: DirectorProjectBrief,
    preflight: CopyPreflightReport,
    verdict: IndependentCopyCriticVerdict,
    repair_scope_override: Literal["copy_only", "director_replan"] | None = None,
    repair_scope_override_reason: str | None = None,
) -> dict[str, Any]:
    """Explicit revision input; no hidden author conversation is required."""
    if preflight.approved and verdict.approved:
        raise ValueError("an approved artifact does not require revision")
    must_improve = []
    must_preserve = []
    for criterion in brief.copy_review_criteria:
        score = int(verdict.scores.get(criterion.criterion_id, 0))
        record = {
            "criterion_id": criterion.criterion_id,
            "instruction": criterion.instruction,
            "current_score": score,
            "minimum_score": criterion.minimum_score,
            "blocking": criterion.blocking,
        }
        if criterion.blocking and score < criterion.minimum_score:
            must_improve.append(record)
        else:
            must_preserve.append(record)
    effective_repair_scope = repair_scope_override or verdict.repair_scope
    return {
        "schema_version": "1.0",
        "role": "content_director_revision",
        "project_brief": brief.model_dump(mode="json"),
        "current_artifact": artifact.model_dump(mode="json"),
        "deterministic_preflight": preflight.model_dump(mode="json"),
        "independent_critic_verdict": verdict.model_dump(mode="json"),
        "delivery_budget_contract": build_delivery_budget_contract(
            brief,
            segment_durations=[
                segment.duration_seconds
                for segment in artifact.script.segments
            ],
        ),
        "revision_contract": {
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision + 1,
            "parent_artifact_sha256": artifact.artifact_sha256,
            "repair_scope": effective_repair_scope,
            "critic_requested_repair_scope": verdict.repair_scope,
            "runtime_repair_scope_override_reason": (
                str(repair_scope_override_reason or "").strip() or None
            ),
            "must_improve": must_improve,
            "must_preserve": must_preserve,
            "acceptance_rule": (
                "Resolve every blocking issue, meet every blocking minimum_score, "
                "and do not regress criteria already at or above threshold."
            ),
            "return": (
                "DirectorAuthorDraftPayload only; runtime materializes all "
                "project-owned fields, provider timing, and hashes"
            ),
        },
        "runtime_owned_fields": [
            "project truth and conversion",
            "review criteria and quality thresholds",
            "capability contracts and policies",
            "locale, duration, aspect ratio, and delivery rates",
            "provider segment durations and integrity hashes",
        ],
    }


def run_content_director_shadow_preflight(
    *,
    project_config: dict[str, Any] | None,
    creative_result: dict[str, Any],
    stage_id: int,
) -> dict[str, Any] | None:
    """Evaluate a creative result without changing the workflow decision.

    Shadow mode is opt-in and project-owned.  It records what the future gate
    *would* decide, but it cannot change ``PASS``, choose a successor, submit a
    provider task, or open a browser.  This makes it safe to collect precision
    data before enforcement.
    """
    config = dict(project_config or {})
    if str(config.get("content_director_mode") or "").strip().lower() != "shadow":
        return None
    raw_program = config.get("director_program_spec")
    if not isinstance(raw_program, dict):
        return {
            "mode": "shadow",
            "stage_id": int(stage_id),
            "evaluated": False,
            "would_block": False,
            "configuration_error": "director_program_spec is missing",
        }
    try:
        program = VideoProgramSpec.model_validate(raw_program)
        script = script_package_from_creative_result(
            creative_result,
            script_id=f"creative-stage-{int(stage_id)}",
            program_id=program.program_id,
            locale=program.locale,
            edit_headroom_seconds=float(
                config.get("director_edit_headroom_seconds") or 0
            ),
        )
        report = preflight_script_copy(program, script)
    except Exception as exc:  # noqa: BLE001
        return {
            "mode": "shadow",
            "stage_id": int(stage_id),
            "evaluated": False,
            "would_block": False,
            "configuration_error": str(exc)[:1000],
        }
    return {
        "mode": "shadow",
        "stage_id": int(stage_id),
        "evaluated": True,
        "would_block": not report.approved,
        "enforced": False,
        "script_id": script.script_id,
        "script_sha256": script.canonical_text_sha256,
        "spoken_word_count": report.spoken_word_count,
        "spoken_budget_words": report.spoken_budget_words,
        "display_word_count": report.display_word_count,
        "display_budget_words": report.display_budget_words,
        "differentiators_found": report.differentiators_found,
        "issue_codes": [issue.code for issue in report.issues],
        "issues": [issue.model_dump(mode="json") for issue in report.issues],
        "critic_required": report.critic_required,
    }


__all__ = [
    "ConversionIntent",
    "CopyCriticBlockingIssue",
    "CopyCriticCriterionEvidence",
    "CopyPreflightIssue",
    "CopyPreflightReport",
    "CopyReviewCriterion",
    "DirectorCapabilityNode",
    "DirectorCapabilitySelection",
    "DirectorCapabilitySpec",
    "DirectorAuthorDraftPayload",
    "DirectedContentArtifact",
    "DirectorDraftPayload",
    "DirectorProgramAuthorDraft",
    "DirectorProjectBrief",
    "DirectorScriptAuthorDraft",
    "DirectorScriptSegmentDraft",
    "IndependentCopyCriticVerdict",
    "PainHypothesis",
    "ConversionHypothesis",
    "SeriesContentFamily",
    "SeriesCoverageMap",
    "SeriesCoverageMapDraft",
    "SeriesCoveragePatchDraft",
    "SeriesCoveragePage",
    "SeriesCoverageTerritory",
    "SeriesReviewCriterion",
    "SeriesSlateIntent",
    "AudioMode",
    "ScriptLine",
    "ScriptPackage",
    "ScriptDraft",
    "ScriptSegmentAllocation",
    "VideoProgramSpec",
    "build_delivery_budget_contract",
    "director_author_output_contract",
    "director_draft_output_contract",
    "build_series_coverage_packet",
    "series_slate_output_contract",
    "series_slate_page_output_contract",
    "build_independent_copy_critic_packet",
    "build_directed_content_artifact",
    "build_director_revision_packet",
    "build_initial_director_packet",
    "build_script_package",
    "finalize_director_author_draft",
    "finalize_series_coverage_map",
    "apply_series_coverage_patch",
    "preflight_script_copy",
    "parse_independent_copy_critic_response",
    "parse_director_draft_response",
    "parse_director_author_draft_response",
    "run_content_director_shadow_preflight",
    "script_package_from_creative_result",
    "script_text_sha256",
    "validate_directed_artifact_against_brief",
]
