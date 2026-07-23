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
)
from app.services.hermes_agent.content_production_plan import (
    ProductionPlanReviewCriterion,
)


_PROFILE_PATH = Path(__file__).with_name(
    "content_director_profile.v2.json"
)


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
    for value in values:
        match = re.search(
            r"(?:[$€£]\s*\d+(?:\.\d{1,2})?"
            r"|\d+(?:\.\d{1,2})?\s*(?:USD|EUR|GBP))",
            str(value or ""),
            flags=re.IGNORECASE,
        )
        if match:
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
    aspect_ratio: str | None = None,
    confirmed_claims: Any = None,
    confirmed_selling_points: Any = None,
    confirmed_promotions: Any = None,
    promotion_cta: str | None = None,
    allow_promotional_cta: bool = True,
    creative_copy_contract: dict[str, Any] | None = None,
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
    headroom = min(
        selected_profile.edit_headroom_seconds,
        max(0.0, duration_min - 0.5),
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
        "creative_cast_policy": dict(creative_cast_policy or {}),
        "product_presentation_policy": dict(
            product_presentation_policy or {}
        ),
        "product_truth": dict(product_truth or {}),
    }
    required_voiceover = copy_contract.get("required_verbatim_voiceover")
    if isinstance(required_voiceover, str) and required_voiceover.strip():
        # Lift the immutable user script to the generic runtime contract.  The
        # Director still owns segment allocation and visuals, but cannot
        # rewrite, omit or reorder the approved spoken words.
        truth_payload["required_verbatim_voiceover"] = required_voiceover.strip()
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
    capability_catalog = list(
        load_content_capability_manifest().capabilities
    )
    production_contract = None
    if str(video_model or "").strip():
        from app.services.hermes_agent.video_model_capabilities import (
            build_video_production_contract,
        )

        production_contract = build_video_production_contract(
            model_id=str(video_model),
            reference_image_limit=video_reference_limit,
            allow_reference_video=allow_reference_video,
        )
    return DirectorSeriesBrief(
        series_id=series_id,
        series_version=1,
        objective=str(objective or "").strip(),
        platform=str(platform or "").strip(),
        locale=str(locale or "").strip(),
        audience=str(audience or "").strip(),
        target_count=count,
        minimum_duration_seconds=duration_min,
        maximum_duration_seconds=duration_max,
        default_duration_seconds=(
            duration_min + duration_max
        ) / 2.0,
        edit_headroom_seconds=headroom,
        speech_rate_wpm=selected_profile.speech_rate_wpm,
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
        quality_rubric=list(selected_profile.quality_rubric),
        source_truth_refs=_source_truth_refs(truth_payload),
        diversity_requirements=diversity_requirements,
    )


def default_director_loop_policy() -> dict[str, int]:
    return dict(load_universal_director_profile().default_loop_policy)


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
        video_model=str(config.get("video_model") or "omni_flash"),
        video_reference_limit=int(
            config.get("video_reference_limit") or 7
        ),
        allow_reference_video=bool(
            config.get("allow_reference_video", False)
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
    config["director_series_brief"] = compiled.model_dump(mode="json")
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
