from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.hermes_agent.content_capabilities import (
    load_content_capability_manifest,
)
from app.services.hermes_agent.content_director import (
    ConversionIntent,
    CopyReviewCriterion,
    DirectorSeriesBrief,
    SeriesDiversityRequirement,
    SeriesReviewCriterion,
    VideoProductionContract,
    production_segment_durations,
)
from app.services.hermes_agent.content_production_plan import (
    ProductionPlanReviewCriterion,
)


_PROFILE_PATH = Path(__file__).with_name(
    "content_director_profile.v2.json"
)

_LOCKED_WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?"
)


def _locked_voiceover_blocks(value: str) -> list[str]:
    """Preserve operator-authored line breaks as immutable timing units.

    A blank-line paragraph may intentionally contain several short VO beats.
    Treating that whole paragraph as one indivisible unit can make a valid
    script impossible to allocate across provider clips.  Splitting only at
    authored newlines preserves every word and punctuation mark while giving
    the deterministic runtime the same pause coordinates the user supplied.
    """

    return [
        block.strip()
        for block in re.split(r"\r?\n", str(value or "").strip())
        if block.strip()
    ]


def _ordered_locked_allocation(
    *,
    block_word_counts: list[int],
    segment_durations: list[float],
    speech_rate_wpm: float,
) -> list[int] | None:
    """Return one order-preserving allocation that fits every segment.

    A whole-video word budget is insufficient for immutable copy: blank-line
    blocks are indivisible author coordinates, so their ordered partition can
    still be impossible even when total capacity is large enough.  Mirror the
    deterministic preflight tolerance here and prove that the compiled rate
    has at least one legal allocation before a model call is authorized.
    """

    counts = [int(value) for value in block_word_counts]
    durations = [float(value) for value in segment_durations]
    if not counts or not durations:
        return None
    limits: list[int] = []
    for duration in durations:
        target = int(duration * float(speech_rate_wpm) / 60.0)
        tolerance = max(1, int(math.floor(float(target) * 0.05)))
        limits.append(target + tolerance)
    prefix = [0]
    for count in counts:
        prefix.append(prefix[-1] + count)
    paths: dict[int, list[int]] = {0: []}
    require_nonempty_segments = len(counts) >= len(limits)
    for segment_index, limit in enumerate(limits, 1):
        remaining_segments = len(limits) - segment_index
        next_paths: dict[int, list[int]] = {}
        for start, cuts in paths.items():
            for end in range(start, len(counts) + 1):
                if prefix[end] - prefix[start] > limit:
                    break
                # Prefer a useful spoken beat in every segment whenever there
                # are at least as many immutable blocks as segments.
                if require_nonempty_segments and end == start:
                    continue
                if (
                    require_nonempty_segments
                    and len(counts) - end < remaining_segments
                ):
                    continue
                next_paths.setdefault(end, [*cuts, end])
        paths = next_paths
    cuts = paths.get(len(counts))
    if cuts is None:
        return None
    result: list[int] = []
    start = 0
    for segment_index, end in enumerate(cuts, 1):
        result.extend([segment_index] * (end - start))
        start = end
    return result


def _minimum_feasible_locked_rate(
    *,
    block_word_counts: list[int],
    segment_durations: list[float],
    usable_seconds: float,
    baseline_rate_wpm: float,
    maximum_rate_wpm: float,
) -> tuple[float, list[int]] | None:
    total_words = sum(block_word_counts)
    first_rate = max(
        int(math.ceil(float(baseline_rate_wpm))),
        int(math.ceil(total_words * 60.0 / max(0.5, usable_seconds))),
    )
    for rate in range(first_rate, int(math.floor(maximum_rate_wpm)) + 1):
        if int(usable_seconds * rate / 60.0) < total_words:
            continue
        allocation = _ordered_locked_allocation(
            block_word_counts=block_word_counts,
            segment_durations=segment_durations,
            speech_rate_wpm=float(rate),
        )
        if allocation is not None:
            return float(rate), allocation
    return None


def _effective_edit_headroom_seconds(
    *,
    configured_headroom_seconds: float,
    minimum_duration_seconds: float,
) -> float:
    """Scale a general editing reserve to the actual short-video duration.

    The universal profile's two-second reserve is useful for ordinary
    ten-second-or-longer videos, but applying it unchanged to a four-second
    hook leaves only half of the requested duration for speech.  That can make
    an otherwise feasible Producer seed impossible before the Director is
    allowed to judge or rewrite it.  Use the same 84 percent usable-duration
    target as the creative compiler and keep at least half a second available
    for visual-only editing.
    """

    duration = max(0.0, float(minimum_duration_seconds))
    if duration <= 0.5:
        return 0.0
    target_usable_seconds = min(
        duration,
        max(0.5, float(round(duration * 0.84))),
    )
    return min(
        max(0.0, float(configured_headroom_seconds)),
        max(0.0, duration - target_usable_seconds),
    )


def _editable_seed_speech_rate_wpm(
    *,
    copy_contract: dict[str, Any],
    default_duration_seconds: float,
    edit_headroom_seconds: float,
    baseline_rate_wpm: float,
    maximum_rate_wpm: float,
) -> float:
    """Return a feasible narration rate for editable Producer seed copy.

    Editable seed copy is not immutable, but it is still evidence of the
    user's requested spoken density.  Ignoring it can create an impossible
    contract where the deterministic budget forces a five-word rewrite while
    the independent Critic correctly requires the supplied eleven-word hook.
    This function adjusts only delivery capacity; the Director remains free to
    rewrite the seed and the Critic remains the semantic authority.
    """

    if str(copy_contract.get("copy_authority") or "").strip() != (
        "producer_draft_editable"
    ):
        return float(baseline_rate_wpm)
    single = copy_contract.get("director_seed_voiceover")
    if isinstance(single, dict) and str(single.get("text") or "").strip():
        # A materialized per-variant brief owns one editable seed.  Sibling
        # seeds remain in durable series truth for auditability but must not
        # increase this deliverable's narration rate.
        seeds = [dict(single)]
    else:
        seeds = [
            dict(item)
            for item in list(
                copy_contract.get("director_seed_voiceovers") or []
            )
            if isinstance(item, dict)
            and str(item.get("text") or "").strip()
        ]
    required_rate = float(baseline_rate_wpm)
    for seed in seeds:
        word_count = len(_LOCKED_WORD_RE.findall(str(seed.get("text") or "")))
        if word_count <= 0:
            continue
        try:
            duration = float(
                seed.get("target_duration_seconds")
                or default_duration_seconds
            )
        except (TypeError, ValueError):
            duration = float(default_duration_seconds)
        usable_seconds = max(
            0.5,
            duration - min(edit_headroom_seconds, max(0.0, duration - 0.5)),
        )
        required_rate = max(
            required_rate,
            float(math.ceil(word_count * 60.0 / usable_seconds)),
        )
    return min(float(maximum_rate_wpm), required_rate)


class DirectorDiversityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    instruction: str = Field(min_length=1, max_length=2000)
    minimum_unique: int = Field(ge=1, le=1000)
    target_fraction: float = Field(gt=0, le=1)
    maximum_unique: int = Field(ge=1, le=1000)
    product_only: bool = False


class UniversalDirectorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    profile_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    speech_rate_wpm: float = Field(gt=0, le=400)
    maximum_locked_script_speech_rate_wpm: float = Field(gt=0, le=400)
    display_reading_rate_wpm: float = Field(gt=0, le=400)
    edit_headroom_seconds: float = Field(ge=0, le=600)
    aspect_ratio: str = Field(min_length=3, max_length=32)
    creative_constraints: list[str] = Field(
        min_length=1,
        max_length=128,
    )
    copy_review_criteria: list[CopyReviewCriterion] = Field(
        min_length=1,
        max_length=64,
    )
    series_page_review_criteria: list[SeriesReviewCriterion] = Field(
        min_length=1,
        max_length=64,
    )
    product_series_page_review_criteria: list[
        SeriesReviewCriterion
    ] = Field(default_factory=list, max_length=64)
    series_global_review_criteria: list[SeriesReviewCriterion] = Field(
        min_length=1,
        max_length=64,
    )
    structured_intent_contract_required: bool
    product_copy_review_criteria: list[CopyReviewCriterion] = Field(
        default_factory=list,
        max_length=64,
    )
    production_plan_review_criteria: list[
        ProductionPlanReviewCriterion
    ] = Field(min_length=1, max_length=64)
    quality_rubric: list[str] = Field(min_length=1, max_length=64)
    diversity_dimensions: list[DirectorDiversityProfile] = Field(
        min_length=1,
        max_length=64,
    )
    default_loop_policy: dict[str, int]


@lru_cache(maxsize=1)
def load_universal_director_profile() -> UniversalDirectorProfile:
    profile = UniversalDirectorProfile.model_validate_json(
        _PROFILE_PATH.read_text(encoding="utf-8")
    )
    criterion_ids = [
        item.criterion_id
        for item in (
            profile.copy_review_criteria
            + profile.product_copy_review_criteria
        )
    ]
    if len(criterion_ids) != len(set(criterion_ids)):
        raise ValueError(
            "director profile contains duplicate review criterion IDs"
        )
    for label, series_criteria in (
        (
            "series_page_review_criteria",
            profile.series_page_review_criteria,
        ),
        (
            "series_global_review_criteria",
            profile.series_global_review_criteria,
        ),
        (
            "product_series_page_review_criteria",
            profile.product_series_page_review_criteria,
        ),
    ):
        series_ids = [
            item.criterion_id for item in series_criteria
        ]
        if len(series_ids) != len(set(series_ids)):
            raise ValueError(
                f"director profile contains duplicate {label} IDs"
            )
    combined_page_ids = [
        item.criterion_id
        for item in (
            profile.series_page_review_criteria
            + profile.product_series_page_review_criteria
        )
    ]
    if len(combined_page_ids) != len(set(combined_page_ids)):
        raise ValueError(
            "director profile contains overlapping generic/product series "
            "page criterion IDs"
        )
    production_ids = [
        item.criterion_id
        for item in profile.production_plan_review_criteria
    ]
    if len(production_ids) != len(set(production_ids)):
        raise ValueError(
            "director profile contains duplicate production plan criterion IDs"
        )
    dimension_ids = [
        item.dimension_id for item in profile.diversity_dimensions
    ]
    if len(dimension_ids) != len(set(dimension_ids)):
        raise ValueError(
            "director profile contains duplicate diversity dimension IDs"
        )
    return profile


def _confirmed_entries(value: Any, *, limit: int = 32) -> list[str]:
    if isinstance(value, list):
        candidates = value
    else:
        candidates = re.split(
            r"(?:\r?\n|[;；])",
            str(value or ""),
        )
    rows: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = re.sub(
            r"^\s*(?:[-*•]|\d+[.)])\s*",
            "",
            str(candidate or ""),
        ).strip()
        fingerprint = re.sub(
            r"\s+",
            " ",
            normalized,
        ).casefold()
        if normalized and fingerprint not in seen:
            rows.append(normalized[:500])
            seen.add(fingerprint)
        if len(rows) >= limit:
            break
    return rows


def _product_truth_confirmed_differentiators(
    product_truth: dict[str, Any] | None,
) -> list[str]:
    """Extract only explicitly approved or label-confirmed product facts.

    Product-library projects do not duplicate their attributes into campaign
    fields.  The Director contract must therefore read the normalized FACTS
    envelope instead of compiling an impossible empty attribute allow-list.
    The paths below are semantic facts contracts, not product-specific data.
    """
    root = dict(product_truth or {})
    envelope = dict(root.get("facts_envelope") or {})
    result = dict(envelope.get("result") or root.get("result") or {})
    passport = dict(result.get("product_passport") or {})
    candidates: list[Any] = [
        root.get("approved_claims"),
        root.get("confirmed_claims"),
        root.get("confirmed_selling_points"),
        result.get("approved_claims"),
        result.get("confirmed_claims"),
        result.get("confirmed_selling_points"),
        passport.get("label_confirmed_facts"),
    ]
    rows: list[str] = []
    for value in candidates:
        rows.extend(_confirmed_entries(value, limit=64))
    return _confirmed_entries(rows, limit=32)


def _confirmed_product_aliases(
    product_truth: dict[str, Any] | None,
    *,
    brand_name: str,
) -> list[str]:
    """Extract only product identities confirmed by the FACTS envelope.

    Product-library display names are not always identical to the name on the
    authoritative package.  The FACTS result already contains the approved
    product claim, passport name, and product-truth handoff, so those are the
    safe alias sources. A spoken shorthand may omit the trailing product form
    only when that form itself is explicitly confirmed in the product passport
    and the remaining alias still contains the brand plus a distinctive token.
    """

    payload = dict(product_truth or {})
    nested = payload.get("facts_envelope")
    if isinstance(nested, dict):
        payload = dict(nested)
    result = payload.get("result")
    result = dict(result) if isinstance(result, dict) else {}
    passport = result.get("product_passport")
    passport = dict(passport) if isinstance(passport, dict) else {}
    confirmed_product_form = re.sub(
        r"\s+",
        " ",
        str(passport.get("product_form") or ""),
    ).strip()
    handoff = result.get("product_truth_handoff")
    handoff = dict(handoff) if isinstance(handoff, dict) else {}
    candidates: list[str] = [
        *_confirmed_entries(result.get("approved_claims")),
        str(handoff.get("PRODUCT") or "").strip(),
    ]
    passport_name = str(passport.get("product_name") or "").strip()
    if passport_name:
        candidates.append(
            passport_name
            if brand_name.casefold() in passport_name.casefold()
            else " ".join(part for part in (brand_name, passport_name) if part)
        )

    aliases: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip()
        fingerprint = normalized.casefold()
        if (
            not normalized
            or not brand_name
            or brand_name.casefold() not in fingerprint
            or fingerprint in seen
        ):
            return
        aliases.append(normalized[:500])
        seen.add(fingerprint)

    for candidate in candidates:
        add(candidate)
        words = str(candidate or "").split()
        form_words = confirmed_product_form.split()
        if (
            form_words
            and len(words) > len(form_words) + 1
            and [word.casefold() for word in words[-len(form_words):]]
            == [word.casefold() for word in form_words]
        ):
            shortened_words = words[:-len(form_words)]
            shortened = " ".join(shortened_words).strip()
            distinctive = [
                word
                for word in shortened_words
                if word.casefold() != brand_name.casefold()
                and len(re.sub(r"[^A-Za-z0-9]", "", word)) >= 4
            ]
            if distinctive:
                add(shortened)
    return aliases[:32]


def _source_truth_refs(truth_payload: dict[str, Any]) -> list[str]:
    return [
        f"truth_payload.{key}"
        for key, value in truth_payload.items()
        if value not in (None, "", [], {})
    ]


def _confirmed_offer_token(*values: Any) -> str | None:
    """Return the complete current offer, not only its currency token.

    ``confirmed_promotions`` is already the project-owned, user-confirmed
    commercial fact selected by the Producer boundary.  Reducing
    ``$14.99 shipped`` to ``$14.99`` makes downstream critics correctly treat
    the immutable word ``shipped`` as unsupported.  Preserve the bounded
    confirmed phrase while still requiring a recognizable price token; legacy
    CTA prose without a structured current offer continues to fall back to the
    extracted currency token.
    """
    for index, value in enumerate(values):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        match = re.search(
            r"(?:[$€£]\s*\d+(?:\.\d{1,2})?"
            r"|\d+(?:\.\d{1,2})?\s*(?:USD|EUR|GBP))",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            # The second argument is the structured confirmed promotion in
            # all current callers.  Keep it intact so qualifiers such as
            # shipping inclusion, bundle quantity, or cadence remain part of
            # the same audited offer.  The first legacy CTA argument may
            # contain unrelated prose, so it remains token-only.
            if index > 0:
                return text[:500]
            return re.sub(r"\s+", "", match.group(0))
    return None


def _confirmed_cta_sentence(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？])\s+", text)
        if item.strip()
    ]
    return (sentences[-1] if sentences else text)[:500] or None


def _diversity_requirements(
    profile: UniversalDirectorProfile,
    *,
    target_count: int,
    product_required: bool,
) -> list[SeriesDiversityRequirement]:
    rows: list[SeriesDiversityRequirement] = []
    for dimension in profile.diversity_dimensions:
        if dimension.product_only and not product_required:
            continue
        desired = max(
            dimension.minimum_unique,
            math.ceil(target_count * dimension.target_fraction),
        )
        rows.append(
            SeriesDiversityRequirement(
                dimension_id=dimension.dimension_id,
                instruction=dimension.instruction,
                minimum_unique_values=min(
                    target_count,
                    dimension.maximum_unique,
                    desired,
                ),
            )
        )
    return rows


def compile_universal_director_series_brief(
    *,
    series_id: str,
    objective: str,
    platform: str,
    locale: str,
    audience: str,
    target_count: int,
    minimum_duration_seconds: float,
    maximum_duration_seconds: float,
    product_required: bool,
    brand_name: str | None,
    product_name: str | None,
    market: str,
    project_brief: str | None,
    video_model: str | None = None,
    video_reference_limit: int | None = None,
    allow_reference_video: bool = False,
    production_contract_override: dict[str, Any] | VideoProductionContract | None = None,
    aspect_ratio: str | None = None,
    confirmed_claims: Any = None,
    confirmed_selling_points: Any = None,
    confirmed_promotions: Any = None,
    promotion_cta: str | None = None,
    allow_promotional_cta: bool = True,
    creative_copy_contract: dict[str, Any] | None = None,
    producer_intent_spec: dict[str, Any] | None = None,
    creative_cast_policy: dict[str, Any] | None = None,
    product_presentation_policy: dict[str, Any] | None = None,
    product_truth: dict[str, Any] | None = None,
    additional_creative_constraints: list[str] | None = None,
    additional_copy_review_criteria: list[
        dict[str, Any] | CopyReviewCriterion
    ] | None = None,
    additional_series_page_review_criteria: list[
        dict[str, Any] | SeriesReviewCriterion
    ] | None = None,
    additional_series_global_review_criteria: list[
        dict[str, Any] | SeriesReviewCriterion
    ] | None = None,
    diversity_requirements_override: list[
        dict[str, Any] | SeriesDiversityRequirement
    ] | None = None,
    structured_intent_contract_required: bool | None = None,
    profile: UniversalDirectorProfile | None = None,
) -> DirectorSeriesBrief:
    """Compile ordinary project settings into a scene-free Director contract.

    The compiler owns policy and truth boundaries only. It intentionally does
    not choose a story, character, hook, segment count, reveal position,
    visual style, or ending. ``project_brief`` is accepted for migration
    compatibility but is deliberately not forwarded: legacy projects mixed
    product facts with campaign mother-template instructions. Normalized
    ``product_truth`` and explicit project fields are the only Director truth.
    """
    del project_brief
    selected_profile = profile or load_universal_director_profile()
    selected_aspect_ratio = str(
        aspect_ratio or selected_profile.aspect_ratio
    ).strip()
    if selected_aspect_ratio not in {"9:16", "16:9", "1:1"}:
        raise ValueError("aspect_ratio must be one of 9:16, 16:9, or 1:1")
    count = max(1, min(1000, int(target_count)))
    duration_min = float(minimum_duration_seconds)
    duration_max = float(maximum_duration_seconds)
    if duration_min > duration_max:
        raise ValueError(
            "minimum_duration_seconds cannot exceed maximum_duration_seconds"
        )
    headroom = _effective_edit_headroom_seconds(
        configured_headroom_seconds=(
            selected_profile.edit_headroom_seconds
        ),
        minimum_duration_seconds=duration_min,
    )
    production_contract: VideoProductionContract | None = None
    if production_contract_override is not None:
        production_contract = (
            production_contract_override
            if isinstance(production_contract_override, VideoProductionContract)
            else VideoProductionContract.model_validate(
                production_contract_override
            )
        )
        if (
            str(video_model or "").strip()
            and production_contract.model_id
            != str(video_model).strip().lower().replace("-", "_")
        ):
            raise ValueError(
                "production contract model_id must match video_model"
            )
    elif str(video_model or "").strip():
        from app.services.hermes_agent.video_model_capabilities import (
            build_video_production_contract,
        )

        production_contract = build_video_production_contract(
            model_id=str(video_model),
            reference_image_limit=video_reference_limit,
            allow_reference_video=allow_reference_video,
        )
    brand = str(brand_name or "").strip()
    product = str(product_name or "").strip()
    identity = (
        product
        if brand and product.casefold().startswith(brand.casefold())
        else " ".join(part for part in (brand, product) if part)
    )
    product_aliases: list[str] = []
    alias_fingerprints: set[str] = {identity.casefold()} if identity else set()
    for item in [
        *_confirmed_entries(confirmed_claims),
        *_confirmed_product_aliases(
            product_truth,
            brand_name=brand,
        ),
    ]:
        fingerprint = item.casefold()
        if (
            brand
            and brand.casefold() in fingerprint
            and fingerprint not in alias_fingerprints
        ):
            product_aliases.append(item)
            alias_fingerprints.add(fingerprint)
        if len(product_aliases) >= 32:
            break
    identity_labels = {
        value.casefold()
        for value in (
            identity,
            product,
            *product_aliases,
        )
        if str(value).strip()
    }
    differentiators = _confirmed_entries([
        *_confirmed_entries(confirmed_selling_points),
        *_confirmed_entries(confirmed_claims),
        *_product_truth_confirmed_differentiators(product_truth),
    ])
    differentiators = [
        item
        for item in differentiators
        if item.casefold() not in identity_labels
    ][:32]
    offer = _confirmed_offer_token(
        promotion_cta,
        confirmed_promotions,
    )
    cta = (
        _confirmed_cta_sentence(promotion_cta)
        if allow_promotional_cta
        else None
    )
    copy_contract = dict(creative_copy_contract or {})
    normalized_producer_intent = dict(producer_intent_spec or {})
    from app.services.hermes_agent.content_change_contract import (
        transformation_contract_constraint,
        transformation_contract_from_mapping,
    )

    intent_manifest = dict(
        normalized_producer_intent.get("intent_manifest") or {}
    )
    transformation_contract = transformation_contract_from_mapping(
        intent_manifest.get("transformation_contract")
        or normalized_producer_intent.get("transformation_contract")
    )
    truth_payload = {
        "profile_id": selected_profile.profile_id,
        "market": str(market or "").strip(),
        "brand_name": str(brand_name or "").strip() or None,
        "product_name": str(product_name or "").strip() or None,
        "confirmed_claims": _confirmed_entries(confirmed_claims),
        "confirmed_selling_points": differentiators,
        "confirmed_promotions": offer,
        "confirmed_promotion_policy": (
            str(confirmed_promotions or "").strip() or None
        ),
        "promotion_cta": cta,
        "creative_copy_contract": copy_contract,
        "producer_intent_spec": normalized_producer_intent,
        "creative_cast_policy": dict(creative_cast_policy or {}),
        "product_presentation_policy": dict(
            product_presentation_policy or {}
        ),
        "product_truth": dict(product_truth or {}),
    }
    required_voiceover = copy_contract.get("required_verbatim_voiceover")
    required_voiceovers = [
        dict(item)
        for item in list(copy_contract.get("required_verbatim_voiceovers") or [])
        if isinstance(item, dict)
        and int(item.get("deliverable_ordinal") or 0) > 0
        and str(item.get("text") or "").strip()
    ]
    if required_voiceovers:
        truth_payload["deliverable_script_manifest"] = [
            {
                "deliverable_ordinal": int(item["deliverable_ordinal"]),
                "label": str(item.get("label") or "")[:255],
                "objective": str(item.get("objective") or "")[:1000],
                "sha256": str(item.get("sha256") or "")[:64],
                "target_duration_seconds": item.get("target_duration_seconds"),
                "must_preserve": list(item.get("must_preserve") or [])[:32],
                "differentiation": list(item.get("differentiation") or [])[:32],
            }
            for item in required_voiceovers
        ]
    locked_script_visual_variants = False
    effective_speech_rate_wpm = selected_profile.speech_rate_wpm
    effective_speech_rate_wpm = _editable_seed_speech_rate_wpm(
        copy_contract=copy_contract,
        default_duration_seconds=(duration_min + duration_max) / 2.0,
        edit_headroom_seconds=headroom,
        baseline_rate_wpm=effective_speech_rate_wpm,
        maximum_rate_wpm=max(
            selected_profile.speech_rate_wpm,
            selected_profile.maximum_locked_script_speech_rate_wpm,
        ),
    )
    effective_duration_min = duration_min
    if isinstance(required_voiceover, str) and required_voiceover.strip():
        # Lift the immutable user script to the generic runtime contract.  The
        # Director still owns segment allocation and visuals, but cannot
        # rewrite, omit or reorder the approved spoken words.
        truth_payload["required_verbatim_voiceover"] = required_voiceover.strip()
        locked_blocks = _locked_voiceover_blocks(required_voiceover)
        locked_lines = [
            {
                "line_id": f"LOCKED-VO-{index:03d}",
                "delivery_mode": "spoken",
                "text": block,
            }
            for index, block in enumerate(locked_blocks, 1)
        ]
        # The line-addressable artifact is deterministic and travels with the
        # immutable string.  Critics and downstream compilers can now prove
        # lossless coverage without asking the model to invent line IDs.
        truth_payload["required_verbatim_voiceover_lines"] = locked_lines
        reuse_mode = str(copy_contract.get("script_reuse_mode") or "").strip()
        if reuse_mode == "single" and count > 1:
            raise ValueError(
                "LOCKED_SCRIPT_REUSE_POLICY_REQUIRED: one locked script with "
                "multiple outputs must explicitly use same_copy_visual_variants "
                "or provide distinct_per_deliverable scripts"
            )
        locked_script_visual_variants = (
            reuse_mode == "same_copy_visual_variants"
        )
        # Compatibility only for already-created projects whose old producer
        # contract predates explicit intent semantics. New projects always
        # persist script_reuse_mode and never derive it from target count.
        if not reuse_mode and count > 1:
            locked_script_visual_variants = True
        if locked_script_visual_variants:
            truth_payload["locked_script_variant_policy"] = {
                "mode": "same_copy_visual_variants",
                "copy_reuse_required": True,
                "semantic_copy_diversity_required": False,
                "conversion_route_diversity_required": False,
                "visual_execution_diversity_required": True,
                "personal_first_person_routine_copy": (
                    "A first-person description supplied and locked by the "
                    "operator is not a label Directions claim. Preserve it "
                    "exactly, never generalize it into viewer instructions, "
                    "and still reject explicit prohibited claims or direct "
                    "contradictions of confirmed product facts."
                ),
            }
        block_word_counts = [
            len(_LOCKED_WORD_RE.findall(block))
            for block in locked_blocks
        ]
        locked_word_count = sum(block_word_counts)
        if locked_word_count:
            maximum_rate = max(
                selected_profile.speech_rate_wpm,
                selected_profile.maximum_locked_script_speech_rate_wpm,
            )
            required_total_seconds = (
                locked_word_count * 60.0 / maximum_rate
            ) + headroom
            if required_total_seconds > duration_max + 1e-9:
                raise ValueError(
                    "LOCKED_SCRIPT_DURATION_BUDGET_EXCEEDED: immutable "
                    f"voiceover has {locked_word_count} words and needs at "
                    f"least {math.ceil(required_total_seconds)} seconds at "
                    f"the configured maximum {maximum_rate:g} WPM, but the "
                    f"project maximum is {duration_max:g} seconds"
                )
            # A duration range may include values too short for the locked
            # script. Narrow only the compiled Director contract so the model
            # cannot choose an impossible target while preserving the user's
            # configured maximum duration.
            effective_duration_min = min(
                duration_max,
                max(duration_min, required_total_seconds),
            )
            usable_minimum_seconds = max(
                0.5,
                effective_duration_min - headroom,
            )
            required_rate = math.ceil(
                locked_word_count * 60.0 / usable_minimum_seconds
            )
            effective_speech_rate_wpm = min(
                maximum_rate,
                max(selected_profile.speech_rate_wpm, required_rate),
            )
            # When the duration is fixed, prove that the ordered immutable
            # blocks also fit the registered provider segment topology.  A
            # global 394-word budget can still be impossible to partition
            # into twelve ten-second clips at the same nominal rate.
            if (
                production_contract is not None
                and abs(effective_duration_min - duration_max) <= 0.05
            ):
                segment_durations = production_segment_durations(
                    production_contract,
                    duration_max,
                )
                feasible = _minimum_feasible_locked_rate(
                    block_word_counts=block_word_counts,
                    segment_durations=segment_durations,
                    usable_seconds=max(0.5, duration_max - headroom),
                    baseline_rate_wpm=effective_speech_rate_wpm,
                    maximum_rate_wpm=maximum_rate,
                )
                if feasible is None:
                    raise ValueError(
                        "LOCKED_SCRIPT_SEGMENT_BUDGET_EXCEEDED: immutable "
                        f"voiceover has {len(locked_blocks)} ordered blocks "
                        f"and cannot fit {len(segment_durations)} registered "
                        f"provider segments at or below {maximum_rate:g} WPM"
                    )
                effective_speech_rate_wpm = feasible[0]
                truth_payload["locked_voiceover_feasible_allocation"] = {
                    "speech_rate_wpm": feasible[0],
                    "segment_indices": feasible[1],
                    "segment_durations_seconds": segment_durations,
                    "authority": "runtime_verified_reference_for_director",
                }
    truth_payload = {
        key: value
        for key, value in truth_payload.items()
        if value not in (None, "", [], {})
    }
    conversion = ConversionIntent(
        product_required=product_required,
        product_name=identity or None,
        product_name_aliases=product_aliases,
        reveal_after_fraction=None,
        confirmed_differentiators=differentiators,
        minimum_differentiators_in_copy=(
            1 if product_required and differentiators else 0
        ),
        offer_text=offer,
        cta_text=cta,
        require_post_cta_human_agency=bool(
            copy_contract.get("require_post_cta_agency_ending")
        ),
        post_cta_agency_terms=[
            str(item).strip()
            for item in list(
                copy_contract.get("post_cta_agency_terms") or []
            )
            if str(item).strip()
        ],
    )
    review_criteria = list(selected_profile.copy_review_criteria)
    if product_required:
        review_criteria.extend(
            selected_profile.product_copy_review_criteria
        )
    review_by_id = {
        item.criterion_id: item for item in review_criteria
    }
    for raw in list(additional_copy_review_criteria or []):
        criterion = (
            raw
            if isinstance(raw, CopyReviewCriterion)
            else CopyReviewCriterion.model_validate(raw)
        )
        review_by_id[criterion.criterion_id] = criterion
    if transformation_contract is not None:
        review_by_id["source_change_boundary_fidelity"] = CopyReviewCriterion(
            criterion_id="source_change_boundary_fidelity",
            instruction=(
                "Review only the audience-facing copy and conversion arc owned "
                "by this Director artifact against "
                "truth_payload.source_transformation_contract. Score 100 when "
                "the protected copy or semantic structure is preserved, every "
                "copy change is authorized, no source dialogue is silently "
                "copied when semantic regeneration is required, and no textual "
                "excluded_source_artifact is introduced. Do not require the "
                "spoken or displayed copy to state or prove media provenance, "
                "new asset generation, cast identity, shot provenance, timing "
                "placement, or the absence of source pixels and audio; those "
                "non-copy obligations are enforced by the production contract "
                "and final media originality audit. Internal coherence is not "
                "evidence of user authorization."
            ),
            minimum_score=100,
            blocking=True,
        )
    review_criteria = list(review_by_id.values())
    page_review_by_id = {
        item.criterion_id: item
        for item in [
            *selected_profile.series_page_review_criteria,
            *(
                selected_profile.product_series_page_review_criteria
                if product_required
                else []
            ),
        ]
    }
    for raw in list(additional_series_page_review_criteria or []):
        criterion = (
            raw
            if isinstance(raw, SeriesReviewCriterion)
            else SeriesReviewCriterion.model_validate(raw)
        )
        page_review_by_id[criterion.criterion_id] = criterion
    global_review_by_id = {
        item.criterion_id: item
        for item in selected_profile.series_global_review_criteria
    }
    if locked_script_visual_variants:
        # One approved script rendered through several visual executions is a
        # legitimate project type.  Semantic-copy and conversion-route
        # diversity are impossible by design and must not be treated as model
        # quality failures. Truth review remains blocking.
        for criterion_id in (
            "semantic_intent_distinctness",
            "response_or_action_route_diversity",
            "series_coverage_balance",
        ):
            global_review_by_id.pop(criterion_id, None)
    for raw in list(additional_series_global_review_criteria or []):
        criterion = (
            raw
            if isinstance(raw, SeriesReviewCriterion)
            else SeriesReviewCriterion.model_validate(raw)
        )
        global_review_by_id[criterion.criterion_id] = criterion
    creative_constraints = list(dict.fromkeys([
        *selected_profile.creative_constraints,
        *[
            str(item).strip()
            for item in list(additional_creative_constraints or [])
            if str(item).strip()
        ],
    ]))
    if transformation_contract is not None:
        truth_payload["source_transformation_contract"] = (
            transformation_contract.model_dump(mode="json")
        )
        creative_constraints = list(dict.fromkeys([
            *creative_constraints,
            transformation_contract_constraint(transformation_contract),
        ]))
        global_review_by_id["source_change_boundary_fidelity"] = (
            SeriesReviewCriterion(
                criterion_id="source_change_boundary_fidelity",
                instruction=(
                    "Compare every planned intent against the complete "
                    "source_transformation_contract. Reject any plan that "
                    "rewrites, replaces, omits, reorders, or slows a protected "
                    "element, or that uses an execution strategy unable to "
                    "prove the requested fidelity. When transfer_mode is "
                    "semantic_structure and source_media_reuse is forbidden, "
                    "also reject plans that preserve source pixels, actors, "
                    "voices, audio, captions, platform UI, watermarks, or any "
                    "declared excluded_source_artifact instead of generating a "
                    "new execution of the protected structure. Also reject a "
                    "plan that carries over any distinctive source premise, "
                    "signature metaphor, character role, setting, prop, action "
                    "sequence, dialogue conceit, or product-transition device "
                    "unless that exact narrative element appears in "
                    "protected_requirements. Hook intensity, visual pacing, "
                    "shot density, tension curve, reveal timing, and conversion "
                    "order are abstract structures, not permission to reuse the "
                    "source story identity."
                ),
                minimum_score=100,
                blocking=True,
            )
        )
    diversity_requirements = (
        [
            raw
            if isinstance(raw, SeriesDiversityRequirement)
            else SeriesDiversityRequirement.model_validate(raw)
            for raw in diversity_requirements_override
        ]
        if diversity_requirements_override
        else _diversity_requirements(
            selected_profile,
            target_count=count,
            product_required=product_required,
        )
    )
    if locked_script_visual_variants:
        diversity_requirements = [
            item
            for item in diversity_requirements
            if item.dimension_id
            not in {"audio_strategy", "conversion_architecture"}
        ]
        creative_constraints = list(dict.fromkeys([
            *creative_constraints,
            (
                "This project requires multiple visual executions of one "
                "immutable script. Preserve every locked line and conversion "
                "beat exactly; vary only project-compatible staging, visual "
                "grammar, camera, action, and edit execution."
            ),
        ]))
    capability_catalog = list(
        load_content_capability_manifest().capabilities
    )
    return DirectorSeriesBrief(
        series_id=series_id,
        series_version=1,
        objective=str(objective or "").strip(),
        platform=str(platform or "").strip(),
        locale=str(locale or "").strip(),
        audience=str(audience or "").strip(),
        target_count=count,
        minimum_duration_seconds=effective_duration_min,
        maximum_duration_seconds=duration_max,
        default_duration_seconds=(
            effective_duration_min + duration_max
        ) / 2.0,
        edit_headroom_seconds=headroom,
        speech_rate_wpm=effective_speech_rate_wpm,
        display_reading_rate_wpm=(
            selected_profile.display_reading_rate_wpm
        ),
        aspect_ratio=selected_aspect_ratio,
        production_contract=production_contract,
        conversion=conversion,
        truth_payload=truth_payload,
        truth_options=differentiators,
        creative_constraints=creative_constraints,
        capability_catalog=capability_catalog,
        copy_review_criteria=review_criteria,
        series_page_review_criteria=list(page_review_by_id.values()),
        series_global_review_criteria=list(global_review_by_id.values()),
        structured_intent_contract_required=(
            selected_profile.structured_intent_contract_required
            if structured_intent_contract_required is None
            else bool(structured_intent_contract_required)
        ),
        quality_rubric=(
            [
                item
                for item in selected_profile.quality_rubric
                if "fixed campaign formula" not in item.casefold()
            ]
            + ([
                "The same immutable copy is intentionally reused; each "
                "variant must provide a materially different visual execution "
                "without changing, dropping, duplicating, or reordering it."
            ] if locked_script_visual_variants else [])
        ),
        source_truth_refs=_source_truth_refs(truth_payload),
        diversity_requirements=diversity_requirements,
    )


def default_director_loop_policy(
    *,
    target_count: int = 1,
    has_reference_transfer: bool = False,
    has_locked_script: bool = False,
    product_required: bool = False,
) -> dict[str, int]:
    from app.services.hermes_agent.content_autonomy import (
        derive_director_loop_policy,
    )

    shipped_floor = dict(load_universal_director_profile().default_loop_policy)
    derived = derive_director_loop_policy(
        target_count=target_count,
        has_reference_transfer=has_reference_transfer,
        has_locked_script=has_locked_script,
        product_required=product_required,
    )
    return {
        key: max(int(shipped_floor.get(key) or 0), int(value))
        for key, value in derived.items()
    }


def refresh_project_director_brief_from_facts(
    project: Any,
    *,
    product_truth: dict[str, Any],
) -> bool:
    """Compile the project-owned universal Director brief from FACTS truth.

    This belongs in the lightweight Director profile layer so zero-media
    planning tools do not need to import the complete Celery task graph.
    Returns ``True`` when the project configuration was refreshed.
    """
    config = dict(project.config_json or {})
    if (
        str(config.get("content_director_mode") or "").strip().lower()
        != "enforce"
        or str(
            config.get("director_series_brief_source") or ""
        ).strip()
        != "universal_profile"
    ):
        return False
    previous_series_brief = dict(
        config.get("director_series_brief") or {}
    )
    previous_series_id = str(
        previous_series_brief.get("series_id") or ""
    ).strip()
    previous_series_version = max(
        1,
        int(previous_series_brief.get("series_version") or 1),
    )
    publishing = dict(config.get("publishing_profile") or {})
    compiled = compile_universal_director_series_brief(
        series_id=f"{project.project_key}.series",
        objective=(
            str(config.get("content_objective") or "").strip()
            or project.title
        ),
        platform=(
            str(publishing.get("platform") or "").strip()
            or "short-video"
        ),
        locale=str(config.get("video_language") or "en-US"),
        audience=(
            str(config.get("target_audience") or "").strip()
            or (
                "Use only audience details explicitly supplied in project "
                f"truth for market {project.market}."
            )
        ),
        target_count=int(config.get("video_count") or 1),
        minimum_duration_seconds=float(
            config.get("video_duration_min_seconds") or 10
        ),
        maximum_duration_seconds=float(
            config.get("video_duration_max_seconds") or 10
        ),
        video_model=str(config.get("video_model") or ""),
        video_reference_limit=int(
            config.get("video_reference_limit") or 7
        ),
        allow_reference_video=bool(
            config.get("allow_reference_video", False)
        ),
        production_contract_override=(
            dict(
                dict(config.get("video_duration_plan") or {}).get(
                    "production_contract"
                )
                or {}
            )
            or None
        ),
        product_required=bool(config.get("product_required", True)),
        brand_name=config.get("brand_name"),
        product_name=project.product_name,
        market=project.market,
        project_brief=None,
        confirmed_claims=config.get("confirmed_claims"),
        confirmed_selling_points=config.get(
            "confirmed_selling_points"
        ),
        confirmed_promotions=config.get("confirmed_promotions"),
        promotion_cta=config.get("promotion_cta"),
        allow_promotional_cta=bool(
            config.get("allow_promotional_cta", True)
        ),
        creative_copy_contract=dict(
            config.get("creative_copy_contract") or {}
        ),
        producer_intent_spec=dict(
            config.get("producer_intent_spec") or {}
        ),
        creative_cast_policy=dict(
            config.get("creative_cast_policy") or {}
        ),
        product_presentation_policy=dict(
            config.get("product_presentation_policy") or {}
        ),
        product_truth=dict(product_truth or {}),
        additional_creative_constraints=list(
            config.get("director_creative_constraints") or []
        ),
        additional_copy_review_criteria=list(
            config.get("director_copy_review_criteria") or []
        ),
        additional_series_page_review_criteria=list(
            config.get("director_series_page_review_criteria") or []
        ),
        additional_series_global_review_criteria=list(
            config.get("director_series_global_review_criteria") or []
        ),
        diversity_requirements_override=list(
            config.get("director_diversity_requirements") or []
        ),
        structured_intent_contract_required=(
            config.get("director_structured_intent_contract_required")
        ),
    )
    refreshed_series_brief = compiled.model_dump(mode="json")
    # FACTS refreshes product truth; it does not create a new immutable
    # Director series identity. A SERIES_DIRECTOR restart may already have
    # allocated max(persisted_version)+1. Recompiling the universal profile
    # defaults to version 1, and silently writing that default back makes the
    # next approved slate collide with the prior immutable audit row. Preserve
    # the project-owned version whenever we are refreshing the same series.
    if (
        previous_series_id
        and previous_series_id
        == str(refreshed_series_brief.get("series_id") or "").strip()
    ):
        refreshed_series_brief["series_version"] = (
            previous_series_version
        )
    config["director_series_brief"] = refreshed_series_brief
    config["director_loop_policy"] = (
        dict(config.get("director_loop_policy") or {})
        or default_director_loop_policy()
    )
    project.config_json = config
    return True


__all__ = [
    "DirectorDiversityProfile",
    "UniversalDirectorProfile",
    "compile_universal_director_series_brief",
    "default_director_loop_policy",
    "load_universal_director_profile",
    "refresh_project_director_brief_from_facts",
]
