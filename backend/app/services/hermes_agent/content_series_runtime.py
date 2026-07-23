from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.hermes_agent.client import (
    HermesContentCriticClient,
    HermesContentDirectorClient,
    extract_output_text,
)
from app.services.hermes_agent.content_director import (
    AUDIO_MODE_SEMANTICS,
    apply_series_coverage_patch,
    DirectorSeriesBrief,
    SeriesCoverageMap,
    SeriesCoverageMapDraft,
    SeriesCoveragePatchDraft,
    SeriesCoveragePage,
    SeriesReviewCriterion,
    SeriesSlate,
    SeriesSlateDraft,
    SeriesSlateIntent,
    SeriesSlatePageDraft,
    build_series_coverage_packet,
    build_series_slate_packet,
    build_series_slate_page_packet,
    finalize_series_coverage_map,
    finalize_series_slate,
    parse_series_slate_response,
    series_slate_output_contract,
    series_slate_page_output_contract,
    validate_series_slate_page,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
)


class SeriesCriticBlockingIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128)
    intent_ids: list[str] = Field(default_factory=list, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)
    repair_instruction: str = Field(min_length=1, max_length=4000)


class IndependentSeriesCriticVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    scores: dict[str, int] = Field(min_length=1, max_length=64)
    blocking_issues: list[SeriesCriticBlockingIssue] = Field(
        default_factory=list,
        max_length=256,
    )
    repair_scope: Literal["slate_only", "series_replan"]

    @model_validator(mode="after")
    def validate_decision(self) -> "IndependentSeriesCriticVerdict":
        invalid = {
            key: value
            for key, value in self.scores.items()
            if isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 100
        }
        if invalid:
            raise ValueError(
                f"series critic scores must be integers 0-100: {invalid}"
            )
        if self.approved == bool(self.blocking_issues):
            raise ValueError(
                "approved must be true exactly when blocking_issues is empty"
            )
        return self


class SeriesDirectorAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1, le=1000)
    contract_repair_attempt: int = Field(ge=0, le=3)
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)
    outcome: Literal["accepted", "contract_rejected"]
    validation_error: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=255)
    request_id: str | None = Field(default=None, max_length=255)
    page_index: int | None = Field(default=None, ge=1, le=1000)
    start_variant_index: int | None = Field(
        default=None,
        ge=1,
        le=1000,
    )
    end_variant_index: int | None = Field(
        default=None,
        ge=1,
        le=1000,
    )


class SeriesCriticReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1, le=1000)
    slate_sha256: str = Field(min_length=64, max_length=64)
    verdict: IndependentSeriesCriticVerdict
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)


class SeriesCoverageCriticReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1, le=1000)
    coverage_sha256: str = Field(min_length=64, max_length=64)
    verdict: IndependentSeriesCriticVerdict
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)


class SeriesPageCriticReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_index: int = Field(ge=1, le=1000)
    page_revision: int = Field(ge=1, le=1000)
    intent_ids: list[str] = Field(min_length=1, max_length=1000)
    verdict: IndependentSeriesCriticVerdict
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)


class SeriesSlateLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["approved", "quality_pause"]
    final_slate: SeriesSlate | None = None
    coverage_map: SeriesCoverageMap | None = None
    attempts: list[SeriesDirectorAttempt] = Field(
        default_factory=list,
        max_length=4096,
    )
    reviews: list[SeriesCriticReview] = Field(
        default_factory=list,
        max_length=11,
    )
    coverage_reviews: list[SeriesCoverageCriticReview] = Field(
        default_factory=list,
        max_length=11,
    )
    page_reviews: list[SeriesPageCriticReview] = Field(
        default_factory=list,
        max_length=4096,
    )
    contract_errors: list[str] = Field(
        default_factory=list,
        max_length=64,
    )
    reason: str


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _response_meta(response: Any) -> dict[str, Any]:
    return (
        dict(response.get("_gmv_meta") or {})
        if isinstance(response, dict)
        else {}
    )


def _parse_series_critic(
    response_text: str,
    *,
    brief: DirectorSeriesBrief,
    criteria: list[SeriesReviewCriterion] | None = None,
    valid_intent_ids: set[str] | None = None,
) -> IndependentSeriesCriticVerdict:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 1_000_000:
        raise ValueError(
            "series critic response is empty or exceeds response limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "series critic response must be raw JSON without markdown fences"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("series critic response is not valid JSON") from exc
    verdict = IndependentSeriesCriticVerdict.model_validate(payload)
    if criteria is not None:
        expected_ids = {
            item.criterion_id for item in criteria
        }
        actual_ids = set(verdict.scores)
        if actual_ids != expected_ids:
            raise ValueError(
                "series critic score keys must exactly match supplied "
                f"criteria; missing={sorted(expected_ids - actual_ids)}, "
                f"extra={sorted(actual_ids - expected_ids)}"
            )
        below_threshold = [
            item.criterion_id
            for item in criteria
            if item.blocking
            and verdict.scores[item.criterion_id]
            < item.minimum_score
        ]
        if verdict.approved and below_threshold:
            raise ValueError(
                "series critic approved scores below blocking thresholds: "
                f"{below_threshold}"
            )
    if valid_intent_ids is not None:
        missing_ids = [
            issue.code
            for issue in verdict.blocking_issues
            if not issue.intent_ids
        ]
        if missing_ids:
            raise ValueError(
                "rejected series critic issues must cite at least one "
                "supplied intent ID; missing intent_ids for issue codes: "
                f"{missing_ids}"
            )
        unknown_ids = sorted({
            intent_id
            for issue in verdict.blocking_issues
            for intent_id in issue.intent_ids
            if intent_id not in valid_intent_ids
        })
        if unknown_ids:
            raise ValueError(
                "series critic referenced unknown intent IDs: "
                f"{unknown_ids}"
            )
    # Intent IDs are Director-authored, so the caller validates them against
    # the current slate separately. This baseline only rejects blank IDs in
    # issue references; it never imposes a naming mother template.
    del brief
    return verdict


async def _request_bounded_series_critic(
    *,
    critic: Any,
    brief: DirectorSeriesBrief,
    packet: dict[str, Any],
    instructions: str,
    metadata: dict[str, Any],
    criteria: list[SeriesReviewCriterion],
    valid_intent_ids: set[str],
    maximum_contract_repairs: int,
) -> tuple[IndependentSeriesCriticVerdict, int, str]:
    current_packet = dict(packet)
    current_instructions = instructions
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await critic.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=current_instructions,
            metadata={
                **metadata,
                "critic_contract_repair_attempt": repair_attempt,
            },
        )
        raw = extract_output_text(response)
        try:
            verdict = _parse_series_critic(
                raw,
                brief=brief,
                criteria=criteria,
                valid_intent_ids=valid_intent_ids,
            )
            return verdict, int(latency_ms), raw
        except (TypeError, ValueError) as exc:
            if repair_attempt >= maximum_contract_repairs:
                raise ValueError(
                    "series critic did not satisfy its response contract "
                    f"within the bounded repair budget: {str(exc)}"
                ) from exc
            current_packet = {
                "schema_version": "2.0",
                "role": "series_critic_contract_repair",
                "original_review_packet": packet,
                "invalid_response": raw[:1_000_000],
                "validation_error": str(exc)[:4000],
                "repair_rules": {
                    "preserve_review_judgment": True,
                    "score_exactly_supplied_criterion_ids": True,
                    "cite_only_supplied_intent_ids": True,
                    "do_not_rewrite_intents": True,
                },
                "output_contract": (
                    IndependentSeriesCriticVerdict.model_json_schema()
                ),
            }
            current_instructions = (
                "Act only as series_critic_contract_repair. Preserve the "
                "review judgment, correct only the explicit contract error, "
                "score exactly the supplied criterion IDs, cite only supplied "
                "intent IDs, and return one raw JSON verdict."
            )
    raise AssertionError("unreachable series critic repair loop")


def _build_series_coverage_patch_packet(
    *,
    brief: DirectorSeriesBrief,
    current_coverage: SeriesCoverageMap,
    blocking_issues: list[SeriesCriticBlockingIssue],
) -> tuple[dict[str, Any], set[str]]:
    territory_by_id = {
        territory.territory_id: territory
        for page in current_coverage.pages
        for territory in page.territories
    }
    cited_territory_ids = {
        territory_id
        for issue in blocking_issues
        for territory_id in issue.intent_ids
    }
    unknown = sorted(cited_territory_ids - set(territory_by_id))
    if unknown:
        raise ValueError(
            "coverage repair cites unknown territory IDs: "
            f"{unknown}"
        )
    if not cited_territory_ids:
        raise ValueError(
            "coverage repair requires at least one Critic-cited territory"
        )
    allowed_variants = sorted({
        territory_by_id[territory_id].variant_index
        for territory_id in cited_territory_ids
    })
    allowed_families = sorted({
        territory_by_id[territory_id].family_id
        for territory_id in cited_territory_ids
    })

    output_contract = SeriesCoveragePatchDraft.model_json_schema()
    properties = output_contract.get("properties", {})
    for field_name, exact_value in (
        ("series_id", current_coverage.series_id),
        ("series_version", int(current_coverage.series_version)),
        (
            "base_coverage_sha256",
            current_coverage.coverage_sha256,
        ),
    ):
        field_schema = properties.get(field_name)
        if isinstance(field_schema, dict):
            field_schema["const"] = exact_value
    definitions = output_contract.get("$defs", {})
    family_properties = definitions.get(
        "SeriesContentFamily",
        {},
    ).get("properties", {})
    territory_properties = definitions.get(
        "SeriesCoverageTerritory",
        {},
    ).get("properties", {})
    if isinstance(family_properties.get("family_id"), dict):
        family_properties["family_id"]["enum"] = allowed_families
    if isinstance(territory_properties.get("variant_index"), dict):
        territory_properties["variant_index"]["enum"] = allowed_variants
    if isinstance(territory_properties.get("family_id"), dict):
        territory_properties["family_id"]["enum"] = allowed_families
    for attribute_schema in (
        family_properties.get("truth_options"),
        territory_properties.get("truth_options"),
    ):
        if not isinstance(attribute_schema, dict):
            continue
        items = attribute_schema.get("items")
        if (
            isinstance(items, dict)
            and brief.truth_options
        ):
            items["enum"] = list(
                brief.truth_options
            )
    if brief.conversion.product_required and isinstance(
        family_properties.get("truth_options"),
        dict,
    ):
        family_properties[
            "truth_options"
        ]["minItems"] = 1
    for field_name, maximum in (
        ("family_updates", len(allowed_families)),
        ("territory_updates", len(allowed_variants)),
    ):
        field_schema = properties.get(field_name)
        if isinstance(field_schema, dict):
            field_schema["maxItems"] = maximum

    return {
        "schema_version": "1.0",
        "role": "content_series_strategy_patch",
        "current_coverage_map": current_coverage.model_dump(mode="json"),
        "critic_blocking_issues": [
            issue.model_dump(mode="json")
            for issue in blocking_issues
        ],
        "patch_scope": {
            "base_coverage_sha256": (
                current_coverage.coverage_sha256
            ),
            "allowed_territory_ids": sorted(cited_territory_ids),
            "allowed_variant_indices": allowed_variants,
            "allowed_family_ids": allowed_families,
        },
        "repair_rules": {
            "return_only_changed_objects": True,
            "variant_index_is_the_stable_territory_coordinate": True,
            "family_id_is_stable_and_must_not_be_renamed": True,
            "do_not_modify_uncited_variants_or_families": True,
            "preserve_project_truth_and_configured_action_boundary": True,
            "copy_supplied_truth_options_verbatim": True,
            "territory_attributes_may_inherit_from_family": True,
            "do_not_return_the_complete_coverage_map": True,
            "return_raw_json_only": True,
        },
        "output_contract": output_contract,
    }, cited_territory_ids


async def _request_series_coverage_patch(
    *,
    director: Any,
    brief: DirectorSeriesBrief,
    page_size: int,
    maximum_contract_repairs: int,
    revision: int,
    current_coverage: SeriesCoverageMap,
    blocking_issues: list[SeriesCriticBlockingIssue],
) -> tuple[
    SeriesCoverageMap | None,
    list[SeriesDirectorAttempt],
    list[str],
]:
    packet, allowed_territory_ids = (
        _build_series_coverage_patch_packet(
            brief=brief,
            current_coverage=current_coverage,
            blocking_issues=blocking_issues,
        )
    )
    attempts: list[SeriesDirectorAttempt] = []
    errors: list[str] = []
    current_packet = packet
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await director.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as content_series_strategy_patch. Repair exactly "
                "the Critic-cited coverage scope. Return only changed family "
                "and territory objects as one raw JSON "
                "SeriesCoveragePatchDraft. Preserve stable family IDs and "
                "variant indices. Never return the complete coverage map."
                if repair_attempt == 0
                else "Act only as content_series_strategy_patch_contract_"
                "repair. Correct only the explicit patch contract error. "
                "Keep the same base hash, cited scope, truth, and strategic "
                "repair. Return one raw JSON SeriesCoveragePatchDraft."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "operation": "series_coverage_patch",
                "coverage_revision": int(revision),
                "contract_repair_attempt": repair_attempt,
                "base_coverage_sha256": (
                    current_coverage.coverage_sha256
                ),
            },
        )
        raw = extract_output_text(response)
        meta = _response_meta(response)
        try:
            if not raw or len(raw) > 500_000:
                raise ValueError(
                    "series coverage patch response is empty or exceeds limit"
                )
            if raw.startswith("```") or raw.endswith("```"):
                raise ValueError(
                    "series coverage patch response must be raw JSON"
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "series coverage patch response is not valid JSON"
                ) from exc
            coverage = apply_series_coverage_patch(
                SeriesCoveragePatchDraft.model_validate(payload),
                current_coverage,
                brief,
                page_size=page_size,
                allowed_territory_ids=allowed_territory_ids,
            )
            attempts.append(SeriesDirectorAttempt(
                revision=revision,
                contract_repair_attempt=repair_attempt,
                latency_ms=int(latency_ms),
                response_sha256=_sha256(raw),
                outcome="accepted",
                model=str(meta.get("model") or "") or None,
                request_id=(
                    str(meta.get("request_id") or "") or None
                ),
            ))
            return coverage, attempts, errors
        except (TypeError, ValueError) as exc:
            message = f"series coverage patch: {str(exc)}"[:4000]
            errors.append(message)
            attempts.append(SeriesDirectorAttempt(
                revision=revision,
                contract_repair_attempt=repair_attempt,
                latency_ms=int(latency_ms),
                response_sha256=_sha256(raw),
                outcome="contract_rejected",
                validation_error=message,
                model=str(meta.get("model") or "") or None,
                request_id=(
                    str(meta.get("request_id") or "") or None
                ),
            ))
            if repair_attempt >= maximum_contract_repairs:
                return None, attempts, errors
            current_packet = {
                "schema_version": "1.0",
                "role": "content_series_strategy_patch_contract_repair",
                "original_contract": packet,
                "invalid_response": raw[:500_000],
                "validation_error": message,
                "repair_rules": {
                    "repair_only_the_reported_contract_error": True,
                    "preserve_base_hash_and_patch_scope": True,
                    "do_not_return_the_complete_coverage_map": True,
                    "return_raw_json_only": True,
                },
                "output_contract": packet["output_contract"],
            }
    return None, attempts, errors


async def _request_series_coverage_map(
    *,
    director: Any,
    brief: DirectorSeriesBrief,
    page_size: int,
    maximum_contract_repairs: int,
    revision: int = 1,
    current_coverage: SeriesCoverageMap | None = None,
    blocking_issues: list[SeriesCriticBlockingIssue] | None = None,
) -> tuple[
    SeriesCoverageMap | None,
    list[SeriesDirectorAttempt],
    list[str],
]:
    if current_coverage is not None and blocking_issues:
        return await _request_series_coverage_patch(
            director=director,
            brief=brief,
            page_size=page_size,
            maximum_contract_repairs=maximum_contract_repairs,
            revision=revision,
            current_coverage=current_coverage,
            blocking_issues=blocking_issues,
        )
    packet = build_series_coverage_packet(
        brief,
        page_size=page_size,
    )
    if current_coverage is not None or blocking_issues:
        packet["revision_context"] = {
            "coverage_revision": int(revision),
            "current_coverage_map": (
                current_coverage.model_dump(mode="json")
                if current_coverage is not None
                else None
            ),
            "critic_blocking_issues": [
                item.model_dump(mode="json")
                for item in list(blocking_issues or [])
            ],
            "revision_rules": {
                "repair_only_cited_family_or_episode_issues": True,
                "preserve_unaffected_territories": True,
                "preserve_unaffected_content_families": True,
                "keep_one_territory_per_variant": True,
                "keep_family_counts_equal_to_declared_assignments": True,
                "retain_at_most_one_member_of_each_unintended_duplicate_cluster": True,
            },
        }
    attempts: list[SeriesDirectorAttempt] = []
    errors: list[str] = []
    current_packet = packet
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await director.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as content_series_strategy. Return exactly one raw "
                "JSON SeriesCoverageMapDraft. First plan content families as "
                "distinct project jobs. Obey any non-empty "
                "operator_stage_instruction in series_strategy_contract as "
                "an authoritative repair constraint. Then reserve model-owned "
                "episode "
                "territories for every declared page. Families and territories "
                "are not scenes or mother templates. A finite confirmed truth "
                "set may be deliberately reused within a family; do not invent "
                "one new fact or viewer-value premise per episode. Use only "
                "the supplied truth options and do not return an integrity hash."
                if repair_attempt == 0 and revision == 1
                else "Act only as content_series_strategy_revision. Repair "
                "the supplied content-family and territory map against only "
                "the cited strategic issues. Preserve unaffected families and "
                "territories, keep family counts consistent, and keep exactly "
                "one ordered territory per variant. Intentional reuse inside "
                "a declared family is valid; renamed duplicate episodes are "
                "not. Return one raw JSON SeriesCoverageMapDraft without an "
                "integrity hash."
                if repair_attempt == 0
                else "Act only as content_series_strategy_contract_repair. "
                "Correct the invalid response to exactly one raw JSON "
                "SeriesCoverageMapDraft without changing the supplied series, "
                "page ranges, truth, or strategic meaning."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "operation": "series_coverage",
                "coverage_revision": int(revision),
                "contract_repair_attempt": repair_attempt,
            },
        )
        raw = extract_output_text(response)
        meta = _response_meta(response)
        try:
            if not raw or len(raw) > 1_000_000:
                raise ValueError(
                    "series coverage response is empty or exceeds limit"
                )
            if raw.startswith("```") or raw.endswith("```"):
                raise ValueError(
                    "series coverage response must be raw JSON"
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "series coverage response is not valid JSON"
                ) from exc
            coverage = finalize_series_coverage_map(
                SeriesCoverageMapDraft.model_validate(payload),
                brief,
                page_size=page_size,
            )
            attempts.append(
                SeriesDirectorAttempt(
                    revision=revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_sha256(raw),
                    outcome="accepted",
                    model=str(meta.get("model") or "") or None,
                    request_id=(
                        str(meta.get("request_id") or "") or None
                    ),
                )
            )
            return coverage, attempts, errors
        except (TypeError, ValueError) as exc:
            message = f"series coverage: {str(exc)}"[:4000]
            errors.append(message)
            attempts.append(
                SeriesDirectorAttempt(
                    revision=revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_sha256(raw),
                    outcome="contract_rejected",
                    validation_error=message,
                    model=str(meta.get("model") or "") or None,
                    request_id=(
                        str(meta.get("request_id") or "") or None
                    ),
                )
            )
            if repair_attempt >= maximum_contract_repairs:
                return None, attempts, errors
            current_packet = {
                "schema_version": "1.0",
                "role": "content_series_strategy_contract_repair",
                "original_contract": packet,
                "invalid_response": raw[:1_000_000],
                "validation_error": message,
                "repair_rules": {
                    "preserve_valid_strategic_meaning": True,
                    "copy_supplied_truth_options_verbatim": True,
                    "copy_family_ids_from_declared_families": True,
                    "territory_attributes_must_be_subset_of_selected_family": True,
                    "empty_territory_attributes_inherit_selected_family": True,
                    "repair_only_the_reported_contract_error": True,
                    "return_raw_json_only": True,
                },
                "output_contract": packet["output_contract"],
            }
    return None, attempts, errors


async def _review_series_coverage(
    *,
    critic: Any,
    brief: DirectorSeriesBrief,
    coverage_map: SeriesCoverageMap,
    revision: int,
    maximum_contract_repairs: int,
) -> SeriesCoverageCriticReview:
    criteria = list(brief.series_global_review_criteria)
    fingerprints = [
        {
            "intent_id": territory.territory_id,
            "variant_index": int(territory.variant_index),
            "page_index": int(page.page_index),
            "family_id": territory.family_id,
            "strategic_role": territory.strategic_role,
            "audience_state": territory.audience_state,
            "audience_tension_or_need": territory.audience_tension_or_need,
            "viewer_value_context": (
                territory.viewer_value_context
            ),
            "response_or_action_route": territory.response_or_action_route,
            "truth_options": list(
                territory.truth_options
            ),
            "anti_repetition_rule": (
                territory.anti_repetition_rule
            ),
        }
        for page in coverage_map.pages
        for territory in page.territories
    ]
    packet = {
        "schema_version": "1.0",
        "role": "independent_series_coverage_critic",
        "series_objective": brief.objective,
        "intended_audience": brief.audience,
        "engagement_or_conversion_boundary": (
            brief.conversion.model_dump(mode="json")
        ),
        "content_families": [
            family.model_dump(mode="json")
            for family in coverage_map.families
        ],
        "coverage_territory_fingerprints": fingerprints,
        "completed_content_history": list(
            dict(brief.truth_payload or {}).get(
                "completed_content_history"
            )
            or []
        ),
        "review_criteria": [
            item.model_dump(mode="json")
            for item in criteria
        ],
        "review_rules": {
            "review_strategy_before_intent_generation": True,
            "review_family_portfolio_before_episode_variation": True,
            "finite_truth_or_value_reasons_may_repeat_within_family": True,
            "do_not_demand_a_unique_fact_or_value_premise_per_variant": True,
            "reject_renamed_scenes_props_or_formats": True,
            "reject_unjustified_cross_family_duplication": True,
            "reject_duplicate_episode_logic_within_a_family": True,
            "reject_semantic_overlap_with_completed_content_history": True,
            "cite_territory_ids_in_intent_ids": True,
            "do_not_write_or_rewrite_video_intents": True,
        },
        "output_contract": (
            IndependentSeriesCriticVerdict.model_json_schema()
        ),
    }
    valid_ids = {
        territory.territory_id
        for page in coverage_map.pages
        for territory in page.territories
    }
    verdict, latency_ms, raw = await _request_bounded_series_critic(
        critic=critic,
        brief=brief,
        packet=packet,
        instructions=(
            "Act only as independent_series_coverage_critic. Review the "
            "Director-owned content families and episode territories before "
            "any video intent or script is generated. Apply only the supplied "
            "global criteria. Do not require one new fact, proof point, or "
            "viewer-value premise per episode: finite source truth may be "
            "deliberately reused inside a declared family. Reject families "
            "that perform the same strategic job without justification, and "
            "reject episodes that merely rename the same viewer moment, "
            "evidence, form, or execution. Compare every proposed territory "
            "with completed_content_history and reject renamed repeats of "
            "already delivered content. In blocking_issues.intent_ids cite "
            "exact territory IDs. Return one raw JSON verdict."
        ),
        metadata={
            "series_id": brief.series_id,
            "series_version": int(brief.series_version),
            "coverage_revision": int(revision),
        },
        criteria=criteria,
        valid_intent_ids=valid_ids,
        maximum_contract_repairs=maximum_contract_repairs,
    )
    return SeriesCoverageCriticReview(
        revision=revision,
        coverage_sha256=coverage_map.coverage_sha256,
        verdict=verdict,
        latency_ms=latency_ms,
        response_sha256=_sha256(raw),
    )


async def _request_series_slate_full(
    *,
    director: Any,
    brief: DirectorSeriesBrief,
    packet: dict[str, Any],
    revision: int,
    maximum_contract_repairs: int,
) -> tuple[
    SeriesSlate | None,
    list[SeriesDirectorAttempt],
    list[str],
]:
    current_packet = dict(packet)
    attempts: list[SeriesDirectorAttempt] = []
    errors: list[str] = []
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await director.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as content_series_director. Return exactly one raw "
                "JSON SeriesSlateDraft matching output_contract. Use only the "
                "series brief and registered capabilities. Do not use a fixed "
                "scene template and do not return an integrity hash. For product "
                "work, establish why the category or use case is relevant before "
                "using an attribute preference to explain product selection."
                if repair_attempt == 0 and revision == 1
                else "Act only as content_series_revision. Repair only the "
                "critic's blocking issues. Preserve approved intents and all "
                "project-owned fields. Return exactly one raw JSON "
                "SeriesSlateDraft without an integrity hash."
                if repair_attempt == 0
                else "Act only as content_series_contract_repair. Correct the "
                "invalid response to exactly one raw JSON SeriesSlateDraft. "
                "Preserve the supplied series brief, intent count, and "
                "creative meaning. Do not add facts or return a hash."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "revision": int(revision),
                "contract_repair_attempt": int(repair_attempt),
            },
        )
        raw = extract_output_text(response)
        meta = _response_meta(response)
        try:
            slate = parse_series_slate_response(
                raw,
                brief=brief,
                required_schema_version="2.0",
            )
            attempts.append(
                SeriesDirectorAttempt(
                    revision=revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_sha256(raw),
                    outcome="accepted",
                    model=str(meta.get("model") or "") or None,
                    request_id=str(meta.get("request_id") or "") or None,
                )
            )
            return slate, attempts, errors
        except (TypeError, ValueError) as exc:
            message = str(exc)[:4000]
            errors.append(message)
            attempts.append(
                SeriesDirectorAttempt(
                    revision=revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_sha256(raw),
                    outcome="contract_rejected",
                    validation_error=message,
                    model=str(meta.get("model") or "") or None,
                    request_id=str(meta.get("request_id") or "") or None,
                )
            )
            if repair_attempt >= maximum_contract_repairs:
                return None, attempts, errors
            current_packet = {
                "schema_version": "2.0",
                "role": "content_series_contract_repair",
                "series_brief": brief.model_dump(mode="json"),
                "invalid_response": raw[:1_000_000],
                "validation_error": message,
                "output_contract": series_slate_output_contract(
                    allowed_audio_modes=brief.allowed_audio_modes,
                ),
            }
    return None, attempts, errors


def _page_revision_context(
    packet: dict[str, Any],
    *,
    start_variant_index: int,
    end_variant_index: int,
) -> dict[str, Any] | None:
    current = dict(packet.get("current_slate") or {})
    current_intents = [
        dict(item)
        for item in list(current.get("intents") or [])
        if isinstance(item, dict)
        and start_variant_index
        <= int(item.get("variant_index") or 0)
        <= end_variant_index
    ]
    verdict = dict(packet.get("critic_verdict") or {})
    page_ids = {
        str(item.get("intent_id") or "")
        for item in current_intents
    }
    issues = [
        dict(issue)
        for issue in list(verdict.get("blocking_issues") or [])
        if isinstance(issue, dict)
        and (
            not list(issue.get("intent_ids") or [])
            or bool(page_ids & {
                str(value)
                for value in list(issue.get("intent_ids") or [])
            })
        )
    ]
    if not current_intents and not issues:
        return None
    return {
        "current_page_intents": current_intents,
        "critic_blocking_issues_for_page": issues,
        "revision_contract": dict(
            packet.get("revision_contract") or {}
        ),
    }


async def _request_structured_series_page(
    *,
    director: Any,
    critic: Any,
    brief: DirectorSeriesBrief,
    coverage_page: SeriesCoveragePage,
    total_pages: int,
    accepted_other_intents: list[SeriesSlateIntent],
    page_revision: int,
    maximum_contract_repairs: int,
    current_page_intents: list[SeriesSlateIntent] | None = None,
    blocking_issues: list[SeriesCriticBlockingIssue] | None = None,
) -> tuple[
    list[SeriesSlateIntent] | None,
    list[SeriesDirectorAttempt],
    SeriesPageCriticReview | None,
    list[str],
]:
    page_index = int(coverage_page.page_index)
    start_variant_index = int(coverage_page.start_variant_index)
    end_variant_index = int(coverage_page.end_variant_index)
    revision_context = None
    if current_page_intents or blocking_issues:
        revision_context = {
            "current_page_intents": [
                item.model_dump(mode="json")
                for item in list(current_page_intents or [])
            ],
            "critic_blocking_issues_for_page": [
                item.model_dump(mode="json")
                for item in list(blocking_issues or [])
            ],
            "revision_contract": {
                "page_revision": page_revision,
                "repair_only_supplied_issues": True,
                "preserve_unaffected_intent_meaning": True,
                "do_not_change_page_or_project_owned_fields": True,
            },
        }
    packet = build_series_slate_page_packet(
        brief,
        page_index=page_index,
        total_pages=total_pages,
        start_variant_index=start_variant_index,
        end_variant_index=end_variant_index,
        accepted_prior_intents=accepted_other_intents,
        revision_context=revision_context,
        coverage_page=coverage_page,
    )
    current_packet = packet
    attempts: list[SeriesDirectorAttempt] = []
    errors: list[str] = []
    page_intents: list[SeriesSlateIntent] | None = None
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await director.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as content_series_director_page. Create the exact "
                "declared intent range inside the reserved coverage page. "
                "Obey any non-empty operator_stage_instruction in "
                "series_page_strategy_contract as an authoritative repair "
                "constraint. "
                "Every hypothesis required by the supplied dynamic schema "
                "must be concrete and feasible; do not invent optional pain "
                "or conversion structures. For product work, establish why the "
                "category or use case is relevant before using an attribute "
                "preference to explain product selection. Return one raw JSON "
                "SeriesSlatePageDraft; do not return an integrity hash."
                if repair_attempt == 0 and page_revision == 1
                else "Act only as content_series_revision_page. Repair only "
                "the supplied page issues while preserving unaffected intent "
                "meaning and the reserved coverage. Return one raw JSON "
                "SeriesSlatePageDraft."
                if repair_attempt == 0
                else "Act only as content_series_page_contract_repair. "
                "Correct the invalid response to the exact page schema and "
                "range. Preserve all valid creative meaning and project-owned "
                "fields."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "page_index": page_index,
                "page_revision": page_revision,
                "contract_repair_attempt": repair_attempt,
            },
        )
        raw = extract_output_text(response)
        meta = _response_meta(response)
        try:
            if not raw or len(raw) > 1_000_000:
                raise ValueError(
                    "series page response is empty or exceeds limit"
                )
            if raw.startswith("```") or raw.endswith("```"):
                raise ValueError(
                    "series page response must be raw JSON"
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "series page response is not valid JSON"
                ) from exc
            page = SeriesSlatePageDraft.model_validate(payload)
            page_intents = validate_series_slate_page(
                page,
                brief,
                expected_page_index=page_index,
                expected_start_variant_index=start_variant_index,
                expected_end_variant_index=end_variant_index,
                prior_intent_ids={
                    item.intent_id
                    for item in accepted_other_intents
                },
                coverage_page=coverage_page,
                required_schema_version="2.0",
            )
            attempts.append(
                SeriesDirectorAttempt(
                    revision=page_revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_sha256(raw),
                    outcome="accepted",
                    model=str(meta.get("model") or "") or None,
                    request_id=(
                        str(meta.get("request_id") or "") or None
                    ),
                    page_index=page_index,
                    start_variant_index=start_variant_index,
                    end_variant_index=end_variant_index,
                )
            )
            break
        except (TypeError, ValueError) as exc:
            message = (
                f"page {page_index}/{total_pages}: {str(exc)}"
            )[:4000]
            errors.append(message)
            attempts.append(
                SeriesDirectorAttempt(
                    revision=page_revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_sha256(raw),
                    outcome="contract_rejected",
                    validation_error=message,
                    model=str(meta.get("model") or "") or None,
                    request_id=(
                        str(meta.get("request_id") or "") or None
                    ),
                    page_index=page_index,
                    start_variant_index=start_variant_index,
                    end_variant_index=end_variant_index,
                )
            )
            if repair_attempt >= maximum_contract_repairs:
                return None, attempts, None, errors
            current_packet = {
                "schema_version": "1.0",
                "role": "content_series_page_contract_repair",
                "original_contract": packet,
                "invalid_response": raw[:1_000_000],
                "validation_error": message,
                "repair_rules": {
                    "preserve_valid_creative_meaning": True,
                    "use_exact_dynamic_schema": True,
                    "copy_enum_values_verbatim": True,
                    "return_raw_json_only": True,
                },
                "output_contract": (
                    packet["output_contract"]
                ),
            }
    if page_intents is None:
        return None, attempts, None, errors

    criteria = list(brief.series_page_review_criteria)
    critic_packet = {
        "schema_version": "1.0",
        "role": "independent_series_page_critic",
        "series_objective": brief.objective,
        "intended_audience": brief.audience,
        "truth_payload": brief.truth_payload,
        "engagement_or_conversion_boundary": (
            brief.conversion.model_dump(mode="json")
        ),
        "reserved_coverage_page": (
            coverage_page.model_dump(mode="json")
        ),
        "page_intents": [
            item.model_dump(mode="json")
            for item in page_intents
        ],
        "completed_content_history": list(
            dict(brief.truth_payload or {}).get(
                "completed_content_history"
            )
            or []
        ),
        "review_criteria": [
            item.model_dump(mode="json")
            for item in criteria
        ],
        "review_rules": {
            "review_intent_feasibility_only": True,
            "do_not_require_script_lines_or_visual_artifacts": True,
            "configured_action_boundary_is_inherited_by_episode_brief": True,
            "do_not_require_each_intent_to_repeat_global_action_copy": True,
            "reject_only_action_boundary_contradictions_at_intent_stage": True,
            "audio_mode_is_authoritative": True,
            "audio_mode_semantics": AUDIO_MODE_SEMANTICS,
            "reject_any_audio_prose_or_plan_that_contradicts_audio_mode": True,
            "reject_semantic_overlap_with_completed_content_history": True,
            "cite_every_affected_intent_id": True,
            "intent_labels_are_not_evidence": True,
            "product_adjacency_is_not_a_relevance_bridge": True,
            "product_need_not_solve_opening_problem": True,
            "pre_reveal_selection_requirement_can_be_a_reason_to_choose": True,
            "preference_invented_inside_reveal_is_circular": True,
            "attribute_preference_cannot_create_category_need": True,
            "separate_human_agency_from_bounded_product_role": True,
            "do_not_rewrite_intents": True,
        },
        "output_contract": (
            IndependentSeriesCriticVerdict.model_json_schema()
        ),
    }
    valid_ids = {
        item.intent_id for item in page_intents
    }
    verdict, latency_ms, raw_verdict = (
        await _request_bounded_series_critic(
            critic=critic,
            brief=brief,
            packet=critic_packet,
            instructions=(
            "Act only as independent_series_page_critic. Review whether each "
            "intent plan can plausibly become a strong, truthful video. Apply "
            "only the supplied series-page criteria. Any configured engagement "
            "or conversion boundary is automatically inherited by every "
            "episode brief; do not reject an intent merely because it does "
            "not repeat global action copy, but reject contradictions. "
            "Treat each intent's audio_mode as authoritative and reject any "
            "audio-related prose, differentiation, or creative strategy that "
            "contradicts its declared semantics. Compare the page with "
            "completed_content_history and reject semantic repeats of an "
            "already delivered video. "
            "For product-required intents, inspect the exact conversion "
            "hypothesis adversarially: saying a product comes after a human "
            "decision, can fit, or is the next step is temporal adjacency, not "
            "a relevance bridge. Do not require the product to solve an unrelated "
            "opening problem; human agency may resolve that problem before the intent "
            "establishes a distinct truthful product use case. A relevant selection "
            "requirement established before the reveal can be a reason when a confirmed "
            "attribute matches it, but a preference invented only inside the reveal is "
            "circular. Keep category entry separate from product selection: a confirmed-"
            "attribute preference may explain why this product is considered among "
            "alternatives, but it cannot by itself establish why the product category or "
            "use case belongs in the story. The intent must distinguish "
            "the human action that addresses the opening problem from the "
            "truthful bounded role the product can play, while still making the "
            "transition understandable. Reject every affected intent explicitly. "
            "Do not score "
            "downstream script lines, visual plans, or media that do not "
            "exist. Return one raw JSON verdict and cite exact affected "
            "intent IDs."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "page_index": page_index,
                "page_revision": page_revision,
            },
            criteria=criteria,
            valid_intent_ids=valid_ids,
            maximum_contract_repairs=maximum_contract_repairs,
        )
    )
    review = SeriesPageCriticReview(
        page_index=page_index,
        page_revision=page_revision,
        intent_ids=sorted(valid_ids),
        verdict=verdict,
        latency_ms=int(latency_ms),
        response_sha256=_sha256(raw_verdict),
    )
    return page_intents, attempts, review, errors


def _compact_intent_fingerprint(
    intent: SeriesSlateIntent,
) -> dict[str, Any]:
    return {
        "variant_index": int(intent.variant_index),
        "intent_id": intent.intent_id,
        "objective": intent.objective,
        "content_type": intent.content_type,
        "audio_mode": intent.audio_mode,
        "audience": intent.audience,
        "differentiation": dict(intent.differentiation),
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


async def _review_compact_series(
    *,
    critic: Any,
    brief: DirectorSeriesBrief,
    coverage_map: SeriesCoverageMap,
    slate: SeriesSlate,
    revision: int,
    maximum_contract_repairs: int,
) -> SeriesCriticReview:
    criteria = list(brief.series_global_review_criteria)
    packet = {
        "schema_version": "1.0",
        "role": "independent_series_global_critic",
        "series_objective": brief.objective,
        "intended_audience": brief.audience,
        "coverage_map": coverage_map.model_dump(mode="json"),
        "intent_fingerprints": [
            _compact_intent_fingerprint(item)
            for item in slate.intents
        ],
        "completed_content_history": list(
            dict(brief.truth_payload or {}).get(
                "completed_content_history"
            )
            or []
        ),
        "review_criteria": [
            item.model_dump(mode="json")
            for item in criteria
        ],
        "review_rules": {
            "review_cross_intent_semantics_only": True,
            "evaluate_intents_inside_their_declared_content_family": True,
            "allow_deliberate_truth_and_value_reuse_within_family": True,
            "do_not_require_unique_engagement_or_conversion_logic_per_episode": True,
            "detect_renamed_duplicate_logic": True,
            "reject_semantic_overlap_with_completed_content_history": True,
            "audio_mode_is_authoritative": True,
            "audio_mode_semantics": AUDIO_MODE_SEMANTICS,
            "reject_cross_intent_or_internal_audio_contract_conflicts": True,
            "cite_exact_affected_intent_ids": True,
            "do_not_require_script_lines_or_visual_artifacts": True,
            "do_not_rewrite_intents": True,
        },
        "output_contract": (
            IndependentSeriesCriticVerdict.model_json_schema()
        ),
    }
    valid_ids = {
        item.intent_id for item in slate.intents
    }
    verdict, latency_ms, raw = (
        await _request_bounded_series_critic(
            critic=critic,
            brief=brief,
            packet=packet,
            instructions=(
            "Act only as independent_series_global_critic. Review compact "
            "intent fingerprints inside the Director-declared content-family "
            "portfolio. Review episode differentiation, family-level "
            "viewer-value coverage, balance, and truth. Do not require a unique "
                "fact, proof point, or action premise per episode; supplied "
                "truth may deliberately repeat within a family. Reject only "
                "unjustified strategic duplication or renamed episode execution. "
                "Also reject semantic overlap with completed_content_history. "
                "Treat each structured audio_mode as authoritative and reject "
                "any contradictory audio prose. "
            "Apply only the supplied global criteria. Never score script "
            "lines or visual artifacts. Return one raw JSON verdict and cite "
            "exact affected intent IDs."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "revision": int(revision),
                "slate_sha256": slate.slate_sha256,
            },
            criteria=criteria,
            valid_intent_ids=valid_ids,
            maximum_contract_repairs=maximum_contract_repairs,
        )
    )
    return SeriesCriticReview(
        revision=revision,
        slate_sha256=slate.slate_sha256,
        verdict=verdict,
        latency_ms=int(latency_ms),
        response_sha256=_sha256(raw),
    )


async def _request_series_slate_pages(
    *,
    director: Any,
    brief: DirectorSeriesBrief,
    packet: dict[str, Any],
    revision: int,
    maximum_contract_repairs: int,
    page_size: int,
    resume_checkpoint: dict[str, Any] | None = None,
    checkpoint_callback: Callable[
        [dict[str, Any] | None],
        Any,
    ] | None = None,
) -> tuple[
    SeriesSlate | None,
    list[SeriesDirectorAttempt],
    list[str],
]:
    """Generate a large slate in bounded, resumable model responses."""
    size = max(1, min(int(page_size), int(brief.target_count)))
    total_pages = (int(brief.target_count) + size - 1) // size
    accepted: list[SeriesSlateIntent] = []
    attempts: list[SeriesDirectorAttempt] = []
    errors: list[str] = []
    checkpoint = dict(resume_checkpoint or {})
    if (
        checkpoint.get("schema_version") == "1.0"
        and checkpoint.get("series_id") == brief.series_id
        and int(checkpoint.get("series_version") or 0)
        == int(brief.series_version)
        and int(checkpoint.get("revision") or 0) == int(revision)
        and int(checkpoint.get("page_size") or 0) == size
    ):
        resumed = [
            SeriesSlateIntent.model_validate(item)
            for item in list(checkpoint.get("accepted_intents") or [])
        ]
        resumed_count = len(resumed)
        if (
            resumed_count <= int(brief.target_count)
            and (
                resumed_count == int(brief.target_count)
                or resumed_count % size == 0
            )
        ):
            prior_ids: set[str] = set()
            for offset in range(0, resumed_count, size):
                chunk = resumed[offset:offset + size]
                page_index = (offset // size) + 1
                start_variant_index = offset + 1
                end_variant_index = offset + len(chunk)
                validate_series_slate_page(
                    SeriesSlatePageDraft(
                        series_id=brief.series_id,
                        series_version=brief.series_version,
                        page_index=page_index,
                        start_variant_index=start_variant_index,
                        end_variant_index=end_variant_index,
                        intents=chunk,
                    ),
                    brief,
                    expected_page_index=page_index,
                    expected_start_variant_index=start_variant_index,
                    expected_end_variant_index=end_variant_index,
                    prior_intent_ids=prior_ids,
                )
                prior_ids.update(item.intent_id for item in chunk)
            accepted = resumed
            attempts = [
                SeriesDirectorAttempt.model_validate(item)
                for item in list(checkpoint.get("attempts") or [])
            ]
            errors = [
                str(item)[:4000]
                for item in list(checkpoint.get("contract_errors") or [])
            ]

    async def emit_checkpoint() -> None:
        if checkpoint_callback is None:
            return
        payload = {
            "schema_version": "1.0",
            "series_id": brief.series_id,
            "series_version": int(brief.series_version),
            "revision": int(revision),
            "page_size": size,
            "accepted_intents": [
                item.model_dump(mode="json")
                for item in accepted
            ],
            "attempts": [
                item.model_dump(mode="json")
                for item in attempts
            ],
            "contract_errors": list(errors),
        }
        emitted = checkpoint_callback(payload)
        if inspect.isawaitable(emitted):
            await emitted

    for page_index in range(1, total_pages + 1):
        start_variant_index = ((page_index - 1) * size) + 1
        end_variant_index = min(
            page_index * size,
            int(brief.target_count),
        )
        if len(accepted) >= end_variant_index:
            continue
        revision_context = _page_revision_context(
            packet,
            start_variant_index=start_variant_index,
            end_variant_index=end_variant_index,
        )
        current_packet = build_series_slate_page_packet(
            brief,
            page_index=page_index,
            total_pages=total_pages,
            start_variant_index=start_variant_index,
            end_variant_index=end_variant_index,
            accepted_prior_intents=accepted,
            revision_context=revision_context,
        )
        page_accepted = False
        for repair_attempt in range(maximum_contract_repairs + 1):
            response, latency_ms = await director.create_response(
                input_text=json.dumps(
                    current_packet,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                instructions=(
                    "Act only as content_series_director_page. Return exactly "
                    "one raw JSON SeriesSlatePageDraft for the declared page "
                    "range. Use only the series brief and registered "
                    "capabilities. Obey any non-empty "
                    "operator_stage_instruction in "
                    "series_page_strategy_contract as an authoritative "
                    "repair constraint. "
                    "Do not repeat accepted prior intents, do "
                    "not use a fixed scene template, and do not return an "
                    "integrity hash. For product work, establish why the category "
                    "or use case is relevant before using an attribute preference "
                    "to explain product selection."
                    if repair_attempt == 0 and revision == 1
                    else "Act only as content_series_revision_page. Repair "
                    "only the supplied page-level critic issues, preserve "
                    "approved intent meaning when possible, and return exactly "
                    "one raw JSON SeriesSlatePageDraft."
                    if repair_attempt == 0
                    else "Act only as content_series_page_contract_repair. "
                    "Correct the invalid response to exactly one raw JSON "
                    "SeriesSlatePageDraft for the unchanged declared page. "
                    "Do not add facts or return a hash."
                ),
                metadata={
                    "series_id": brief.series_id,
                    "series_version": int(brief.series_version),
                    "revision": int(revision),
                    "contract_repair_attempt": int(repair_attempt),
                    "page_index": int(page_index),
                    "start_variant_index": int(start_variant_index),
                    "end_variant_index": int(end_variant_index),
                },
            )
            raw = extract_output_text(response)
            meta = _response_meta(response)
            try:
                if not raw or len(raw) > 1_000_000:
                    raise ValueError(
                        "series slate page response is empty or exceeds the "
                        "response limit"
                    )
                if raw.startswith("```") or raw.endswith("```"):
                    raise ValueError(
                        "series slate page response must be raw JSON without "
                        "markdown fences"
                    )
                try:
                    raw_page = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "series slate page response is not valid JSON"
                    ) from exc
                page = SeriesSlatePageDraft.model_validate(raw_page)
                page_intents = validate_series_slate_page(
                    page,
                    brief,
                    expected_page_index=page_index,
                    expected_start_variant_index=start_variant_index,
                    expected_end_variant_index=end_variant_index,
                    prior_intent_ids={
                        item.intent_id for item in accepted
                    },
                    required_schema_version="2.0",
                )
                attempts.append(
                    SeriesDirectorAttempt(
                        revision=revision,
                        contract_repair_attempt=repair_attempt,
                        latency_ms=int(latency_ms),
                        response_sha256=_sha256(raw),
                        outcome="accepted",
                        model=str(meta.get("model") or "") or None,
                        request_id=(
                            str(meta.get("request_id") or "") or None
                        ),
                        page_index=page_index,
                        start_variant_index=start_variant_index,
                        end_variant_index=end_variant_index,
                    )
                )
                accepted.extend(page_intents)
                page_accepted = True
                await emit_checkpoint()
                break
            except (TypeError, ValueError) as exc:
                message = (
                    f"page {page_index}/{total_pages}: {str(exc)}"
                )[:4000]
                errors.append(message)
                attempts.append(
                    SeriesDirectorAttempt(
                        revision=revision,
                        contract_repair_attempt=repair_attempt,
                        latency_ms=int(latency_ms),
                        response_sha256=_sha256(raw),
                        outcome="contract_rejected",
                        validation_error=message,
                        model=str(meta.get("model") or "") or None,
                        request_id=(
                            str(meta.get("request_id") or "") or None
                        ),
                        page_index=page_index,
                        start_variant_index=start_variant_index,
                        end_variant_index=end_variant_index,
                    )
                )
                if repair_attempt >= maximum_contract_repairs:
                    break
                current_packet = {
                    "schema_version": "2.0",
                    "role": "content_series_page_contract_repair",
                    "series_brief": brief.model_dump(mode="json"),
                    "page_contract": {
                        "page_index": page_index,
                        "total_pages": total_pages,
                        "start_variant_index": start_variant_index,
                        "end_variant_index": end_variant_index,
                    },
                    "accepted_prior_intent_registry": [
                        {
                            "variant_index": item.variant_index,
                            "intent_id": item.intent_id,
                            "objective": item.objective,
                            "content_type": item.content_type,
                            "differentiation": dict(
                                item.differentiation
                            ),
                        }
                        for item in accepted
                    ],
                    "invalid_response": raw[:1_000_000],
                    "validation_error": message,
                    "output_contract": packet["output_contract"],
                }
        if not page_accepted:
            return None, attempts, errors

    try:
        slate = finalize_series_slate(
            SeriesSlateDraft(
                schema_version="2.0",
                series_id=brief.series_id,
                series_version=brief.series_version,
                intents=accepted,
            ),
            brief,
        )
    except (TypeError, ValueError) as exc:
        errors.append(
            f"aggregate series slate validation failed: {str(exc)}"[:4000]
        )
        return None, attempts, errors
    return slate, attempts, errors


async def _request_series_slate(
    *,
    director: Any,
    brief: DirectorSeriesBrief,
    packet: dict[str, Any],
    revision: int,
    maximum_contract_repairs: int,
    page_size: int,
    resume_checkpoint: dict[str, Any] | None = None,
    checkpoint_callback: Callable[
        [dict[str, Any] | None],
        Any,
    ] | None = None,
) -> tuple[
    SeriesSlate | None,
    list[SeriesDirectorAttempt],
    list[str],
]:
    if int(brief.target_count) <= int(page_size):
        return await _request_series_slate_full(
            director=director,
            brief=brief,
            packet=packet,
            revision=revision,
            maximum_contract_repairs=maximum_contract_repairs,
        )
    return await _request_series_slate_pages(
        director=director,
        brief=brief,
        packet=packet,
        revision=revision,
        maximum_contract_repairs=maximum_contract_repairs,
        page_size=page_size,
        resume_checkpoint=resume_checkpoint,
        checkpoint_callback=checkpoint_callback,
    )


async def _run_structured_paged_series_loop(
    *,
    brief: DirectorSeriesBrief,
    policy: DirectorLoopPolicy,
    director: Any,
    critic: Any,
    resume_page_checkpoint: dict[str, Any] | None,
    page_checkpoint_callback: Callable[
        [dict[str, Any] | None],
        Any,
    ] | None,
) -> SeriesSlateLoopResult:
    """Review and repair pages locally, then compare compact fingerprints."""
    size = max(
        1,
        min(int(policy.series_page_size), int(brief.target_count)),
    )
    total_pages = (int(brief.target_count) + size - 1) // size
    checkpoint = dict(resume_page_checkpoint or {})
    attempts: list[SeriesDirectorAttempt] = []
    reviews: list[SeriesCriticReview] = []
    coverage_reviews: list[SeriesCoverageCriticReview] = []
    page_reviews: list[SeriesPageCriticReview] = []
    contract_errors: list[str] = []
    coverage_map: SeriesCoverageMap | None = None
    accepted_pages: dict[int, list[SeriesSlateIntent]] = {}
    # Keep the latest Critic-reviewed candidate for every unfinished page.
    # Without this durable state, a worker restart or quality-pause resume
    # regenerates the whole page and spends the same Director/Critic calls
    # again instead of applying the cited repair to the reviewed candidate.
    page_candidates: dict[int, list[SeriesSlateIntent]] = {}
    runtime_progress: dict[str, Any] = {
        "phase": "coverage",
    }
    resumed_coverage = False
    checkpoint_identity_matches = (
        checkpoint.get("schema_version") == "1.0"
        and checkpoint.get("series_id") == brief.series_id
        and int(checkpoint.get("series_version") or 0)
        == int(brief.series_version)
        and int(checkpoint.get("page_size") or 0) == size
    )
    if checkpoint_identity_matches:
        attempts.extend([
            SeriesDirectorAttempt.model_validate(item)
            for item in list(checkpoint.get("attempts") or [])
        ])
        coverage_reviews.extend([
            SeriesCoverageCriticReview.model_validate(item)
            for item in list(
                checkpoint.get("coverage_reviews") or []
            )
        ])
        contract_errors.extend([
            str(item)[:4000]
            for item in list(
                checkpoint.get("contract_errors") or []
            )
        ])

    raw_coverage = checkpoint.get("coverage_map")
    coverage_candidate: SeriesCoverageMap | None = None
    if checkpoint_identity_matches and isinstance(raw_coverage, dict):
        try:
            candidate = SeriesCoverageMap.model_validate(raw_coverage)
            finalize_series_coverage_map(
                SeriesCoverageMapDraft(
                    series_id=candidate.series_id,
                    series_version=candidate.series_version,
                    page_size=candidate.page_size,
                    families=candidate.families,
                    pages=candidate.pages,
                ),
                brief,
                page_size=size,
            )
            coverage_candidate = candidate
        except (TypeError, ValueError):
            coverage_candidate = None

    async def emit_coverage_checkpoint(
        candidate: SeriesCoverageMap | None,
        *,
        phase: str,
        revision: int,
        blocking_issue_codes: list[str] | None = None,
    ) -> None:
        """Persist every expensive coverage call before any page work.

        A rejected coverage strategy is useful recovery evidence. Losing it
        forces a fresh Director request and Critic review after a process
        interruption, which is both costly and semantically unsafe.
        """
        if page_checkpoint_callback is None:
            return
        payload = {
            "schema_version": "1.0",
            "series_id": brief.series_id,
            "series_version": int(brief.series_version),
            "revision": int(revision),
            "page_size": size,
            "coverage_map": (
                candidate.model_dump(mode="json")
                if candidate is not None
                else None
            ),
            "coverage_reviews": [
                item.model_dump(mode="json")
                for item in coverage_reviews
            ],
            "accepted_intents": [],
            "attempts": [
                item.model_dump(mode="json")
                for item in attempts
            ],
            "page_reviews": [],
            "global_reviews": [],
            "contract_errors": list(contract_errors),
            "progress": {
                "phase": phase,
                "coverage_revision": int(revision),
                "blocking_issue_codes": list(
                    blocking_issue_codes or []
                ),
            },
        }
        emitted = page_checkpoint_callback(payload)
        if inspect.isawaitable(emitted):
            await emitted

    matching_coverage_reviews = [
        review
        for review in coverage_reviews
        if (
            coverage_candidate is not None
            and review.coverage_sha256
            == coverage_candidate.coverage_sha256
        )
    ]
    approved_coverage_review = next(
        (
            review
            for review in reversed(matching_coverage_reviews)
            if review.verdict.approved
        ),
        None,
    )
    if approved_coverage_review is not None:
        coverage_map = coverage_candidate
        resumed_coverage = True
    else:
        coverage_revision = max(
            [
                review.revision
                for review in matching_coverage_reviews
            ]
            or [0]
        )
        latest_rejected_review = next(
            (
                review
                for review in reversed(matching_coverage_reviews)
                if not review.verdict.approved
            ),
            None,
        )
        blocking_issues = (
            list(latest_rejected_review.verdict.blocking_issues)
            if latest_rejected_review is not None
            else []
        )
        if (
            coverage_candidate is not None
            and blocking_issues
            and coverage_revision > policy.series_revision_limit
        ):
            return SeriesSlateLoopResult(
                status="quality_pause",
                coverage_map=coverage_candidate,
                attempts=attempts,
                reviews=reviews,
                coverage_reviews=coverage_reviews,
                page_reviews=page_reviews,
                contract_errors=contract_errors,
                reason=(
                    "series coverage strategy already exhausted its "
                    "configured semantic repair budget; explicitly increase "
                    "the budget before requesting another revision"
                ),
            )
        while coverage_map is None:
            if coverage_candidate is None or blocking_issues:
                coverage_revision += 1
                prior_coverage_candidate = coverage_candidate
                await emit_coverage_checkpoint(
                    coverage_candidate,
                    phase="coverage_director",
                    revision=coverage_revision,
                    blocking_issue_codes=[
                        issue.code for issue in blocking_issues
                    ],
                )
                (
                    requested_coverage_candidate,
                    coverage_attempts,
                    coverage_errors,
                ) = await _request_series_coverage_map(
                    director=director,
                    brief=brief,
                    page_size=size,
                    maximum_contract_repairs=(
                        policy
                        .maximum_contract_repairs_per_revision
                    ),
                    revision=coverage_revision,
                    current_coverage=(
                        prior_coverage_candidate
                        if blocking_issues
                        else None
                    ),
                    blocking_issues=blocking_issues,
                )
                attempts.extend(coverage_attempts)
                contract_errors.extend(coverage_errors)
                if requested_coverage_candidate is not None:
                    coverage_candidate = requested_coverage_candidate
                else:
                    # A failed revision must never erase the last valid,
                    # Critic-reviewed candidate. Keeping it in the checkpoint
                    # preserves the exact next revision and cited issues after
                    # a process restart.
                    coverage_candidate = prior_coverage_candidate
                await emit_coverage_checkpoint(
                    coverage_candidate,
                    phase=(
                        "coverage_candidate"
                        if requested_coverage_candidate is not None
                        else "coverage_contract_pause"
                    ),
                    revision=coverage_revision,
                    blocking_issue_codes=[
                        issue.code for issue in blocking_issues
                    ],
                )
                if requested_coverage_candidate is None:
                    return SeriesSlateLoopResult(
                        status="quality_pause",
                        coverage_map=coverage_candidate,
                        attempts=attempts,
                        reviews=reviews,
                        coverage_reviews=coverage_reviews,
                        page_reviews=page_reviews,
                        contract_errors=contract_errors,
                        reason=(
                            "series coverage map did not satisfy its "
                            "contract; no media stage was authorized"
                        ),
                    )
            elif coverage_revision <= 0:
                coverage_revision = 1
            await emit_coverage_checkpoint(
                coverage_candidate,
                phase="coverage_critic",
                revision=coverage_revision,
            )
            coverage_review = await _review_series_coverage(
                critic=critic,
                brief=brief,
                coverage_map=coverage_candidate,
                revision=coverage_revision,
                maximum_contract_repairs=(
                    policy.maximum_contract_repairs_per_revision
                ),
            )
            coverage_reviews.append(coverage_review)
            await emit_coverage_checkpoint(
                coverage_candidate,
                phase=(
                    "coverage_approved"
                    if coverage_review.verdict.approved
                    else "coverage_semantic_repair"
                ),
                revision=coverage_revision,
                blocking_issue_codes=[
                    issue.code
                    for issue in coverage_review.verdict.blocking_issues
                ],
            )
            if coverage_review.verdict.approved:
                coverage_map = coverage_candidate
                break
            if coverage_revision > policy.series_revision_limit:
                return SeriesSlateLoopResult(
                    status="quality_pause",
                    coverage_map=coverage_candidate,
                    attempts=attempts,
                    reviews=reviews,
                    coverage_reviews=coverage_reviews,
                    page_reviews=page_reviews,
                    contract_errors=contract_errors,
                    reason=(
                        "series coverage strategy exhausted its bounded "
                        "semantic repair budget; no intent or media stage "
                        "was authorized"
                    ),
                )
            blocking_issues = list(
                coverage_review.verdict.blocking_issues
            )

    if coverage_map is None:
        return SeriesSlateLoopResult(
            status="quality_pause",
            attempts=attempts,
            reviews=reviews,
            coverage_reviews=coverage_reviews,
            page_reviews=page_reviews,
            contract_errors=contract_errors,
            reason=(
                "series coverage map was not approved; no media stage "
                "was authorized"
            ),
        )

    if (
        checkpoint_identity_matches
        and resumed_coverage
        and isinstance(checkpoint.get("coverage_map"), dict)
    ):
        resumed_page_reviews = [
            SeriesPageCriticReview.model_validate(item)
            for item in list(checkpoint.get("page_reviews") or [])
        ]
        resumed = [
            SeriesSlateIntent.model_validate(item)
            for item in list(checkpoint.get("accepted_intents") or [])
        ]
        for page in coverage_map.pages:
            chunk = [
                item
                for item in resumed
                if page.start_variant_index
                <= item.variant_index
                <= page.end_variant_index
            ]
            expected_count = (
                page.end_variant_index
                - page.start_variant_index
                + 1
            )
            if len(chunk) != expected_count:
                continue
            approved_reviews = [
                review
                for review in resumed_page_reviews
                if review.page_index == page.page_index
                and review.verdict.approved
                and set(review.intent_ids)
                == {item.intent_id for item in chunk}
            ]
            if not approved_reviews:
                continue
            other_ids = {
                item.intent_id
                for item in resumed
                if item not in chunk
            }
            validate_series_slate_page(
                SeriesSlatePageDraft(
                    series_id=brief.series_id,
                    series_version=brief.series_version,
                    page_index=page.page_index,
                    start_variant_index=page.start_variant_index,
                    end_variant_index=page.end_variant_index,
                    intents=chunk,
                ),
                brief,
                expected_page_index=page.page_index,
                expected_start_variant_index=(
                    page.start_variant_index
                ),
                expected_end_variant_index=page.end_variant_index,
                prior_intent_ids=other_ids,
                coverage_page=page,
            )
            accepted_pages[page.page_index] = chunk
        page_reviews.extend(resumed_page_reviews)
        reviews.extend([
            SeriesCriticReview.model_validate(item)
            for item in list(checkpoint.get("global_reviews") or [])
        ])
        raw_page_candidates = checkpoint.get("page_candidates") or {}
        if isinstance(raw_page_candidates, dict):
            for page in coverage_map.pages:
                raw_candidate = raw_page_candidates.get(
                    str(page.page_index),
                    raw_page_candidates.get(page.page_index),
                )
                if not isinstance(raw_candidate, list):
                    continue
                try:
                    candidate = [
                        SeriesSlateIntent.model_validate(item)
                        for item in raw_candidate
                    ]
                    validate_series_slate_page(
                        SeriesSlatePageDraft(
                            series_id=brief.series_id,
                            series_version=brief.series_version,
                            page_index=page.page_index,
                            start_variant_index=(
                                page.start_variant_index
                            ),
                            end_variant_index=page.end_variant_index,
                            intents=candidate,
                        ),
                        brief,
                        expected_page_index=page.page_index,
                        expected_start_variant_index=(
                            page.start_variant_index
                        ),
                        expected_end_variant_index=(
                            page.end_variant_index
                        ),
                        prior_intent_ids={
                            item.intent_id
                            for accepted in accepted_pages.values()
                            for item in accepted
                        },
                        coverage_page=page,
                    )
                except (TypeError, ValueError):
                    continue
                page_candidates[page.page_index] = candidate

    def all_accepted() -> list[SeriesSlateIntent]:
        return sorted(
            [
                item
                for page_intents in accepted_pages.values()
                for item in page_intents
            ],
            key=lambda item: item.variant_index,
        )

    async def emit_checkpoint() -> None:
        if page_checkpoint_callback is None:
            return
        payload = {
            "schema_version": "1.0",
            "series_id": brief.series_id,
            "series_version": int(brief.series_version),
            "revision": 1,
            "page_size": size,
            "coverage_map": coverage_map.model_dump(mode="json"),
            "coverage_reviews": [
                item.model_dump(mode="json")
                for item in coverage_reviews
            ],
            "accepted_intents": [
                item.model_dump(mode="json")
                for item in all_accepted()
            ],
            "page_candidates": {
                str(page_index): [
                    item.model_dump(mode="json")
                    for item in candidate
                ]
                for page_index, candidate in sorted(
                    page_candidates.items()
                )
            },
            "attempts": [
                item.model_dump(mode="json")
                for item in attempts
            ],
            "page_reviews": [
                item.model_dump(mode="json")
                for item in page_reviews
            ],
            "global_reviews": [
                item.model_dump(mode="json")
                for item in reviews
            ],
            "contract_errors": list(contract_errors),
            "progress": dict(runtime_progress),
        }
        emitted = page_checkpoint_callback(payload)
        if inspect.isawaitable(emitted):
            await emitted

    await emit_checkpoint()

    async def repair_page(
        coverage_page: SeriesCoveragePage,
        *,
        initial_intents: list[SeriesSlateIntent] | None = None,
        initial_issues: list[SeriesCriticBlockingIssue] | None = None,
    ) -> bool:
        current_intents = (
            initial_intents
            if initial_intents is not None
            else page_candidates.get(coverage_page.page_index)
        )
        current_issues = list(initial_issues or [])
        first_revision = 1
        if current_intents is not None:
            current_ids = {item.intent_id for item in current_intents}
            matching_reviews = [
                review
                for review in page_reviews
                if review.page_index == coverage_page.page_index
                and set(review.intent_ids) == current_ids
                and not review.verdict.approved
            ]
            if matching_reviews:
                latest_review = max(
                    matching_reviews,
                    key=lambda item: item.page_revision,
                )
                first_revision = latest_review.page_revision + 1
                if not current_issues:
                    current_issues = list(
                        latest_review.verdict.blocking_issues
                    )
        for page_revision in range(
            first_revision,
            policy.series_revision_limit + 2,
        ):
            runtime_progress.clear()
            runtime_progress.update({
                "phase": "page_director",
                "page_index": int(coverage_page.page_index),
                "page_revision": int(page_revision),
            })
            await emit_checkpoint()
            other = [
                item
                for page_index, page_intents in accepted_pages.items()
                if page_index != coverage_page.page_index
                for item in page_intents
            ]
            (
                candidate,
                current_attempts,
                review,
                current_errors,
            ) = await _request_structured_series_page(
                director=director,
                critic=critic,
                brief=brief,
                coverage_page=coverage_page,
                total_pages=total_pages,
                accepted_other_intents=other,
                page_revision=page_revision,
                maximum_contract_repairs=(
                    policy.maximum_contract_repairs_per_revision
                ),
                current_page_intents=current_intents,
                blocking_issues=current_issues,
            )
            attempts.extend(current_attempts)
            contract_errors.extend(current_errors)
            if candidate is None or review is None:
                runtime_progress.clear()
                runtime_progress.update({
                    "phase": "page_contract_pause",
                    "page_index": int(
                        coverage_page.page_index
                    ),
                    "page_revision": int(page_revision),
                })
                await emit_checkpoint()
                return False
            page_reviews.append(review)
            current_intents = candidate
            page_candidates[coverage_page.page_index] = candidate
            current_issues = list(review.verdict.blocking_issues)
            runtime_progress.clear()
            runtime_progress.update({
                "phase": (
                    "page_approved"
                    if review.verdict.approved
                    else "page_semantic_repair"
                ),
                "page_index": int(coverage_page.page_index),
                "page_revision": int(page_revision),
                "blocking_issue_codes": [
                    issue.code
                    for issue in review.verdict.blocking_issues
                ],
            })
            await emit_checkpoint()
            if review.verdict.approved:
                accepted_pages[coverage_page.page_index] = candidate
                await emit_checkpoint()
                return True
        return False

    for coverage_page in coverage_map.pages:
        if coverage_page.page_index in accepted_pages:
            continue
        if not await repair_page(coverage_page):
            return SeriesSlateLoopResult(
                status="quality_pause",
                coverage_map=coverage_map,
                attempts=attempts,
                reviews=reviews,
                coverage_reviews=coverage_reviews,
                page_reviews=page_reviews,
                contract_errors=contract_errors,
                reason=(
                    f"series page {coverage_page.page_index} did not pass "
                    "local feasibility review within its repair bound"
                ),
            )

    for global_revision in range(
        1,
        policy.series_revision_limit + 2,
    ):
        try:
            slate = finalize_series_slate(
                SeriesSlateDraft(
                    schema_version="2.0",
                    series_id=brief.series_id,
                    series_version=brief.series_version,
                    intents=all_accepted(),
                ),
                brief,
            )
        except (TypeError, ValueError) as exc:
            contract_errors.append(
                f"aggregate series slate validation failed: {str(exc)}"
                [:4000]
            )
            return SeriesSlateLoopResult(
                status="quality_pause",
                coverage_map=coverage_map,
                attempts=attempts,
                reviews=reviews,
                coverage_reviews=coverage_reviews,
                page_reviews=page_reviews,
                contract_errors=contract_errors,
                reason=(
                    "locally approved pages failed aggregate validation"
                ),
            )
        runtime_progress.clear()
        runtime_progress.update({
            "phase": "global_critic",
            "global_revision": int(global_revision),
        })
        await emit_checkpoint()
        review = await _review_compact_series(
            critic=critic,
            brief=brief,
            coverage_map=coverage_map,
            slate=slate,
            revision=global_revision,
            maximum_contract_repairs=(
                policy.maximum_contract_repairs_per_revision
            ),
        )
        reviews.append(review)
        runtime_progress.clear()
        runtime_progress.update({
            "phase": (
                "global_approved"
                if review.verdict.approved
                else "global_semantic_repair"
            ),
            "global_revision": int(global_revision),
            "blocking_issue_codes": [
                issue.code
                for issue in review.verdict.blocking_issues
            ],
            "affected_intent_ids": sorted({
                intent_id
                for issue in review.verdict.blocking_issues
                for intent_id in issue.intent_ids
            }),
        })
        await emit_checkpoint()
        if review.verdict.approved:
            if page_checkpoint_callback is not None:
                cleared = page_checkpoint_callback(None)
                if inspect.isawaitable(cleared):
                    await cleared
            return SeriesSlateLoopResult(
                status="approved",
                final_slate=slate,
                coverage_map=coverage_map,
                attempts=attempts,
                reviews=reviews,
                coverage_reviews=coverage_reviews,
                page_reviews=page_reviews,
                contract_errors=contract_errors,
                reason=(
                    "every page passed local feasibility review and compact "
                    "global fingerprint review"
                ),
            )
        if global_revision > policy.series_revision_limit:
            return SeriesSlateLoopResult(
                status="quality_pause",
                coverage_map=coverage_map,
                attempts=attempts,
                reviews=reviews,
                coverage_reviews=coverage_reviews,
                page_reviews=page_reviews,
                contract_errors=contract_errors,
                reason=(
                    "compact global series review exhausted the bounded "
                    "local-page repair budget"
                ),
            )

        affected_ids = {
            intent_id
            for issue in review.verdict.blocking_issues
            for intent_id in issue.intent_ids
        }
        affected_pages = {
            page.page_index
            for page in coverage_map.pages
            if not affected_ids
            or any(
                item.intent_id in affected_ids
                for item in accepted_pages[page.page_index]
            )
        }
        for coverage_page in coverage_map.pages:
            if coverage_page.page_index not in affected_pages:
                continue
            current = list(
                accepted_pages[coverage_page.page_index]
            )
            current_ids = {item.intent_id for item in current}
            issues = [
                issue
                for issue in review.verdict.blocking_issues
                if not issue.intent_ids
                or bool(current_ids & set(issue.intent_ids))
            ]
            if not await repair_page(
                coverage_page,
                initial_intents=current,
                initial_issues=issues,
            ):
                return SeriesSlateLoopResult(
                    status="quality_pause",
                    coverage_map=coverage_map,
                    attempts=attempts,
                    reviews=reviews,
                    coverage_reviews=coverage_reviews,
                    page_reviews=page_reviews,
                    contract_errors=contract_errors,
                    reason=(
                        f"globally rejected page "
                        f"{coverage_page.page_index} did not pass local repair"
                    ),
                )

    raise AssertionError("unreachable structured series loop state")


async def run_content_series_slate_loop(
    *,
    brief: DirectorSeriesBrief,
    policy: DirectorLoopPolicy,
    director_client: Any | None = None,
    critic_client: Any | None = None,
    resume_page_checkpoint: dict[str, Any] | None = None,
    page_checkpoint_callback: Callable[
        [dict[str, Any] | None],
        Any,
    ] | None = None,
) -> SeriesSlateLoopResult:
    """Plan and independently review the whole series before per-video copy."""
    director = director_client or HermesContentDirectorClient()
    critic = critic_client or HermesContentCriticClient()
    if (
        brief.structured_intent_contract_required
        and brief.series_page_review_criteria
        and brief.series_global_review_criteria
    ):
        return await _run_structured_paged_series_loop(
            brief=brief,
            policy=policy,
            director=director,
            critic=critic,
            resume_page_checkpoint=resume_page_checkpoint,
            page_checkpoint_callback=page_checkpoint_callback,
        )
    packet = build_series_slate_packet(brief)
    attempts: list[SeriesDirectorAttempt] = []
    reviews: list[SeriesCriticReview] = []
    contract_errors: list[str] = []

    for revision in range(1, policy.series_revision_limit + 2):
        slate, current_attempts, current_errors = (
            await _request_series_slate(
                director=director,
                brief=brief,
                packet=packet,
                revision=revision,
                maximum_contract_repairs=(
                    policy.maximum_contract_repairs_per_revision
                ),
                page_size=policy.series_page_size,
                resume_checkpoint=resume_page_checkpoint,
                checkpoint_callback=page_checkpoint_callback,
            )
        )
        attempts.extend(current_attempts)
        contract_errors.extend(current_errors)
        if slate is None:
            return SeriesSlateLoopResult(
                status="quality_pause",
                attempts=attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "series slate did not satisfy the explicit project "
                    "contract within its bounded repair budget"
                ),
            )

        critic_packet = {
            "schema_version": "2.0",
            "role": "independent_series_critic",
            "series_brief": brief.model_dump(mode="json"),
            "series_slate": slate.model_dump(mode="json"),
            "completed_content_history": list(
                dict(brief.truth_payload or {}).get(
                    "completed_content_history"
                )
                or []
            ),
            "review_rules": {
                "score_whole_series_against_project_objective": True,
                "score_whole_series_against_intended_audience": True,
                "verify_each_intent_can_satisfy_every_blocking_copy_criterion": True,
                "score_project_quality_rubric": True,
                "score_every_diversity_requirement": True,
                "reject_semantic_duplicates": True,
                "reject_semantic_overlap_with_completed_content_history": True,
                "reject_fixed_template_repetition": True,
                "reject_unearned_required_conversion_transition": bool(
                    brief.conversion.product_required
                ),
                "audio_mode_is_authoritative": True,
                "audio_mode_semantics": AUDIO_MODE_SEMANTICS,
                "reject_any_audio_prose_or_plan_that_contradicts_audio_mode": True,
                "do_not_rewrite_intents": True,
            },
            "output_contract": (
                IndependentSeriesCriticVerdict.model_json_schema()
            ),
        }
        critic_response, latency_ms = await critic.create_response(
            input_text=json.dumps(
                critic_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as independent_series_critic. Review the exact "
                "project objective, intended audience, every blocking copy "
                "criterion, quality rubric, and diversity requirement. Reject "
                "any intent whose planned story cannot plausibly satisfy a "
                "blocking copy criterion. "
                + (
                    "Because this project explicitly requires a product, also "
                    "reject an intent whose conversion transition cannot be "
                    "earned from the preceding audience need or story logic. "
                    if brief.conversion.product_required
                    else "Do not invent a product, offer, CTA, purchase reason, "
                    "or conversion bridge for this non-product project. "
                )
                +
                "Treat structured audio_mode as authoritative and reject any "
                "contradictory audio prose or plan. "
                "Compare the proposed slate with completed_content_history and "
                "reject semantic repeats of already delivered videos. "
                "Return one raw JSON verdict. Do not rewrite the slate or "
                "relax a threshold."
            ),
            metadata={
                "series_id": brief.series_id,
                "series_version": int(brief.series_version),
                "slate_sha256": slate.slate_sha256,
                "revision": int(revision),
            },
        )
        raw_verdict = extract_output_text(critic_response)
        verdict = _parse_series_critic(
            raw_verdict,
            brief=brief,
        )
        valid_ids = {item.intent_id for item in slate.intents}
        unknown_ids = sorted({
            intent_id
            for issue in verdict.blocking_issues
            for intent_id in issue.intent_ids
            if intent_id not in valid_ids
        })
        if unknown_ids:
            raise ValueError(
                "series critic referenced unknown intent IDs: "
                f"{unknown_ids}"
            )
        reviews.append(
            SeriesCriticReview(
                revision=revision,
                slate_sha256=slate.slate_sha256,
                verdict=verdict,
                latency_ms=int(latency_ms),
                response_sha256=_sha256(raw_verdict),
            )
        )
        if verdict.approved:
            if page_checkpoint_callback is not None:
                cleared = page_checkpoint_callback(None)
                if inspect.isawaitable(cleared):
                    await cleared
            return SeriesSlateLoopResult(
                status="approved",
                final_slate=slate,
                attempts=attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "series slate passed deterministic contract validation "
                    "and independent critic review"
                ),
            )
        if revision > policy.series_revision_limit:
            return SeriesSlateLoopResult(
                status="quality_pause",
                final_slate=None,
                attempts=attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "series slate exhausted the project-owned revision budget"
                ),
            )
        if page_checkpoint_callback is not None:
            cleared = page_checkpoint_callback(None)
            if inspect.isawaitable(cleared):
                await cleared
        packet = {
            "schema_version": "2.0",
            "role": "content_series_revision",
            "series_brief": brief.model_dump(mode="json"),
            "current_slate": slate.model_dump(mode="json"),
            "critic_verdict": verdict.model_dump(mode="json"),
            "revision_contract": {
                "revision": revision + 1,
                "repair_only_blocking_issues": True,
                "preserve_approved_intents_when_possible": True,
                "do_not_change_project_owned_fields": True,
            },
            "output_contract": series_slate_output_contract(
                allowed_audio_modes=brief.allowed_audio_modes,
            ),
        }

    raise AssertionError("unreachable series slate loop state")


__all__ = [
    "IndependentSeriesCriticVerdict",
    "SeriesCriticBlockingIssue",
    "SeriesCriticReview",
    "SeriesDirectorAttempt",
    "SeriesPageCriticReview",
    "SeriesSlateLoopResult",
    "run_content_series_slate_loop",
]
