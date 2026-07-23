from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.hermes_agent.client import (
    HermesContentCriticClient,
    HermesContentDirectorClient,
    extract_output_text,
)
from app.services.hermes_agent.content_director import (
    DirectedContentArtifact,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
)
from app.services.hermes_agent.content_production_plan import (
    DirectedProductionPlan,
    DirectorProductionPlanAuthorDraft,
    ProductionPlanReviewCriterion,
    build_director_production_plan_packet,
    finalize_director_production_plan_author_draft,
)


_GENERATED_REFERENCE_TEXT_DEPENDENCY = re.compile(
    r"\b(?:"
    r"handwritten\s+(?:word|words|text|note|message)|"
    r"(?:note|reminder)\b.{0,80}\b(?:reads|says|wording)|"
    r"(?:screen|ui|message|email)\b.{0,80}\b(?:exact|readable|legible|wording)|"
    r"(?:readable|legible)\s+(?:word|words|text|note|message|copy)|"
    r"(?:showing|displaying)\s+(?:the\s+)?(?:word|words|text|name)|"
    r"named\s+sender"
    r")\b",
    flags=re.IGNORECASE,
)

_GENERATED_REFERENCE_WRITING_ACTION = re.compile(
    r"\b(?:write|writes|writing|wrote|inscribe|inscribes)\s+"
    r"(?!a\b|an\b|the\b|something\b|briefly\b|quietly\b)"
    r".{2,120}?\s+(?:on|onto|across)\s+(?:a|an|the)\s+"
    r"(?:note|paper|card|label|screen|message|reminder)\b",
    flags=re.IGNORECASE,
)

_NEGATED_GENERATED_COPY = re.compile(
    r"\b(?:no|not|without|avoid|exclude|do\s+not\s+include)\s+"
    r"(?:any\s+)?(?:readable|legible|visible|exact)?\s*"
    r"(?:screen|ui|note|message|email|handwritten)?\s*"
    r"(?:text|copy|wording|letters|words)\b",
    flags=re.IGNORECASE,
)

_NONREQUIRED_GENERATED_COPY = re.compile(
    r"\b(?:not\s+(?:being\s+)?required|rather\s+than\s+(?:being\s+)?required)\s+"
    r"(?:as\s+)?(?:generated\s+)?(?:readable|legible|visible|exact)?\s*"
    r"(?:screen\s+)?(?:text|copy|wording|letters|words)\b",
    flags=re.IGNORECASE,
)


def _generated_copy_match_evidence(value: str) -> str | None:
    """Return bounded evidence for an impossible image-copy dependency."""
    scrubbed = _NEGATED_GENERATED_COPY.sub("", str(value or ""))
    scrubbed = _NONREQUIRED_GENERATED_COPY.sub("", scrubbed)
    match = (
        _GENERATED_REFERENCE_TEXT_DEPENDENCY.search(scrubbed)
        or _GENERATED_REFERENCE_WRITING_ACTION.search(scrubbed)
    )
    if match is None:
        return None
    start = max(0, match.start() - 80)
    end = min(len(scrubbed), match.end() + 80)
    return " ".join(scrubbed[start:end].split())[:320]


def _strip_authoritative_product_copy_claims(value: str) -> str:
    """Remove only copy authority supplied by uploaded product pixels."""
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", str(value or "")):
        lowered = sentence.lower()
        authoritative = any(marker in lowered for marker in (
            "authoritative",
            "uploaded package",
            "uploaded product",
            "source pixels",
            "local composite",
            "composited locally",
            "supplied only",
        ))
        product = "package" in lowered or "product" in lowered
        copy_or_pixels = any(marker in lowered for marker in (
            "pixel",
            "label",
            "copy",
            "text",
            "brand",
            "composit",
        ))
        if authoritative and product and copy_or_pixels:
            continue
        kept.append(sentence)
    return " ".join(kept)


def _generated_reference_text_dependencies(
    plan: DirectedProductionPlan,
) -> list[str]:
    """Reject image-generation contracts that require exact small copy.

    Generated references are scene/character/action authority. Exact viewer
    copy stays in the signed delivery mode (spoken voice or local overlay).
    Requiring a model-rendered note, sender name, or UI phrase creates a
    predictable image-review loop and spends quota on an impossible contract.
    """
    return sorted(
        detail["reference_id"]
        for detail in _generated_reference_text_dependency_details(plan)
    )


def _generated_reference_text_dependency_details(
    plan: DirectedProductionPlan,
) -> list[dict[str, Any]]:
    """Explain exactly which Director-owned fields require generated copy."""
    beat_fields_by_reference: dict[str, list[tuple[str, str]]] = {}
    for beat in plan.visual.beats:
        for field_name in (
            "environment",
            "subject_action",
            "continuity_state",
            "camera_composition",
            "motion_and_transition",
        ):
            value = str(getattr(beat, field_name, "") or "")
            if not value:
                continue
            for reference_id in beat.reference_ids:
                beat_fields_by_reference.setdefault(reference_id, []).append(
                    (f"visual.beats[{beat.beat_id}].{field_name}", value)
                )

    details: list[dict[str, Any]] = []
    for reference in plan.visual.references:
        fields = [
            ("purpose", str(reference.purpose or "")),
            ("generation_brief", str(reference.generation_brief or "")),
            *beat_fields_by_reference.get(reference.reference_id, []),
        ]
        is_authoritative_product = bool(
            "product" in reference.roles
            and reference.source_asset_refs
        )
        matches: list[dict[str, str]] = []
        for field_name, raw_value in fields:
            value = (
                _strip_authoritative_product_copy_claims(raw_value)
                if is_authoritative_product
                else raw_value
            )
            evidence = _generated_copy_match_evidence(value)
            if evidence:
                matches.append({
                    "field": field_name,
                    "evidence": evidence,
                })
        if not matches:
            combined = " ".join(value for _, value in fields)
            if is_authoritative_product:
                combined = _strip_authoritative_product_copy_claims(combined)
            evidence = _generated_copy_match_evidence(combined)
            if evidence:
                matches.append({
                    "field": "combined_visual_contract",
                    "evidence": evidence,
                })
        if matches:
            details.append({
                "reference_id": reference.reference_id,
                "matches": matches,
            })
    return details


def _generated_copy_repair_instruction(
    artifact: DirectedContentArtifact,
) -> str:
    audio_mode = str(artifact.program.audio_mode or "")
    if audio_mode == "spoken":
        return (
            "Replace readable generated copy with a text-free physical "
            "state or action. Preserve every exact viewer-facing fact in "
            "the immutable spoken delivery through provider_dialogue or "
            "local_voiceover; do not create local_overlay for spoken copy."
        )
    return (
        "Replace readable generated copy with a text-free physical state "
        "or action. Preserve exact viewer-facing copy only through the "
        "line's declared local_overlay delivery; never ask an image or "
        "video model to render it."
    )


class ProductionPlanCriticIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=128)
    beat_ids: list[str] = Field(default_factory=list, max_length=1000)
    line_ids: list[str] = Field(default_factory=list, max_length=1000)
    reference_ids: list[str] = Field(default_factory=list, max_length=1000)
    audio_cue_ids: list[str] = Field(default_factory=list, max_length=1000)
    evidence: str = Field(min_length=1, max_length=4000)
    repair_instruction: str = Field(min_length=1, max_length=4000)


class IndependentProductionPlanCriticVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    scores: dict[str, int] = Field(min_length=1, max_length=64)
    blocking_issues: list[ProductionPlanCriticIssue] = Field(
        default_factory=list,
        max_length=256,
    )
    repair_scope: Literal["plan_only", "director_replan"]

    @model_validator(mode="after")
    def validate_decision(self) -> "IndependentProductionPlanCriticVerdict":
        invalid = {
            key: value
            for key, value in self.scores.items()
            if isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 100
        }
        if invalid:
            raise ValueError(
                "production plan scores must be integers 0-100: "
                f"{invalid}"
            )
        if self.approved == bool(self.blocking_issues):
            raise ValueError(
                "approved must be true exactly when blocking_issues is empty"
            )
        return self


class ProductionPlanAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["initial", "revision", "contract_repair"]
    plan_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=1000)
    contract_repair_attempt: int = Field(ge=0, le=3)
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)
    outcome: Literal["accepted", "contract_rejected"]
    validation_error: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=255)
    request_id: str | None = Field(default=None, max_length=255)


class ProductionPlanCriticAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_sha256: str = Field(min_length=64, max_length=64)
    revision: int = Field(ge=1, le=1000)
    contract_repair_attempt: int = Field(ge=0, le=3)
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)
    outcome: Literal["accepted", "contract_rejected"]
    validation_error: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=255)
    request_id: str | None = Field(default=None, max_length=255)


class ProductionPlanReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_sha256: str = Field(min_length=64, max_length=64)
    verdict: IndependentProductionPlanCriticVerdict
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)


class ProductionPlanLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["approved", "quality_pause"]
    final_plan: DirectedProductionPlan | None = None
    plans: list[DirectedProductionPlan] = Field(
        default_factory=list,
        max_length=11,
    )
    attempts: list[ProductionPlanAttempt] = Field(
        default_factory=list,
        max_length=64,
    )
    critic_attempts: list[ProductionPlanCriticAttempt] = Field(
        default_factory=list,
        max_length=64,
    )
    reviews: list[ProductionPlanReview] = Field(
        default_factory=list,
        max_length=11,
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


def _draft_schema_for_artifact(
    artifact: DirectedContentArtifact,
    *,
    capability_catalog: list[dict[str, Any]],
    authorized_asset_refs: list[str],
    authoritative_product_asset_refs: list[str],
) -> dict[str, Any]:
    return build_director_production_plan_packet(
        artifact,
        capability_catalog=capability_catalog,
        authorized_asset_refs=authorized_asset_refs,
        authoritative_product_asset_refs=(
            authoritative_product_asset_refs
        ),
    )["output_contract"]


async def _request_plan(
    *,
    director: Any,
    packet: dict[str, Any],
    artifact: DirectedContentArtifact,
    plan_id: str,
    revision: int,
    parent_plan_sha256: str | None,
    maximum_contract_repairs: int,
    capability_catalog: list[dict[str, Any]],
    authorized_asset_refs: list[str],
    authoritative_product_asset_refs: list[str],
) -> tuple[
    DirectedProductionPlan | None,
    list[ProductionPlanAttempt],
    list[str],
]:
    attempts: list[ProductionPlanAttempt] = []
    errors: list[str] = []
    persistent_contract_requirements = {
        "every_reference_has_generation_brief": (
            "Every visual reference, including a source-guided or product "
            "reference, must have a non-empty generation_brief for the scene "
            "that will be generated."
        ),
        "generated_scenes_do_not_own_exact_copy": (
            "Generated reference scenes and linked visual beats must not "
            "require an image model to render exact note, handwriting, UI, "
            "screen, label, sender, or message copy. Use a text-free physical "
            "state or action; the immutable spoken line carries the fact, and "
            "local_overlay is available only when the approved line is "
            "display copy."
        ),
        "spoken_lines_are_audible": (
            "Every approved spoken line must use local_voiceover or "
            "provider_dialogue with its approved speaker_id; never use "
            "local_overlay for spoken copy."
        ),
        "spoken_voice_authority_is_explicit": (
            "For every spoken speaker choose one explicit gender, one "
            "screen_relation, timbre, pitch, and accent, then keep that exact "
            "voice authority for the complete video. An off_screen_narrator "
            "may have a different gender from a visible silent character. An "
            "on_screen_character or character_voiceover belongs to that "
            "character and must match the character's gender presentation."
        ),
        "asset_authority_is_preserved": (
            "The uploaded product image is the authoritative visual identity "
            "reference for package shape, cap, colors, brand, and primary "
            "label. Image and video models render that product naturally in "
            "the scripted scene. Never turn the source packshot or its white "
            "background into a pasted card, overlay, or full-frame hold."
        ),
    }
    current_packet = dict(packet)
    current_packet["persistent_contract_requirements"] = (
        persistent_contract_requirements
    )
    output_contract = _draft_schema_for_artifact(
        artifact,
        capability_catalog=capability_catalog,
        authorized_asset_refs=authorized_asset_refs,
        authoritative_product_asset_refs=(
            authoritative_product_asset_refs
        ),
    )
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await director.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as content_director_production_plan. Direct the "
                "approved immutable script as one complete video. Choose the "
                "visual beat count, visual grammar, reference intents, voice "
                "and sound design, and line delivery method from the project "
                "and capability packet. Visual beats are meaning intervals, "
                "not provider transport segments. The runtime deterministically "
                "compiles feasible copy start/end intervals from the immutable "
                "line_delivery_contract; do not return copy delivery times. "
                "For every local_overlay, explicitly choose a presentation "
                "whose placement, emphasis, background, and maximum line count "
                "fit that exact story beat. Do not use one default placement "
                "for the whole video. "
                "For each spoken speaker explicitly choose gender, "
                "screen_relation, timbre, pitch, and accent. Use "
                "off_screen_narrator when an independent narrator speaks over "
                "a silent visible character; use on_screen_character for "
                "visible lip-synced speech; use character_voiceover when the "
                "voice belongs to the visible protagonist without requiring "
                "lip sync. A character's own dialogue or first-person "
                "voiceover must match that character's gender presentation. "
                "Keep the chosen voice authority unchanged across every "
                "transport segment. "
                "Return exactly one raw JSON "
                "DirectorProductionPlanAuthorDraft. Do not copy runtime-owned "
                "identity, duration, aspect ratio, audio mode, or hashes."
                if repair_attempt == 0 and revision == 1
                else "Act only as content_director_production_plan_revision. "
                "Repair every supplied blocking issue while preserving the "
                "approved script, unaffected plan decisions, asset authority, "
                "and capability boundary. Return exactly one raw JSON "
                "DirectorProductionPlanAuthorDraft without runtime-owned "
                "fields, hashes, or markdown."
                if repair_attempt == 0
                else "Act only as content_director_production_plan_contract_"
                "repair. Correct every accumulated contract error without changing "
                "the approved script, cited semantic repair, asset authority, "
                "capabilities, or any previously satisfied contract requirement. "
                "Return one raw JSON "
                "DirectorProductionPlanAuthorDraft without runtime-owned "
                "fields, hashes, or markdown."
            ),
            metadata={
                "program_id": artifact.program.program_id,
                "director_artifact_sha256": artifact.artifact_sha256,
                "plan_id": plan_id,
                "revision": int(revision),
                "contract_repair_attempt": repair_attempt,
                "operation": "production_plan",
            },
        )
        raw = extract_output_text(response)
        meta = _response_meta(response)
        operation: Literal["initial", "revision", "contract_repair"] = (
            "contract_repair"
            if repair_attempt > 0
            else ("initial" if revision == 1 else "revision")
        )
        try:
            if not raw or len(raw) > 1_000_000:
                raise ValueError(
                    "production plan response is empty or exceeds limit"
                )
            if raw.startswith("```") or raw.endswith("```"):
                raise ValueError(
                    "production plan response must be raw JSON"
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "production plan response is not valid JSON"
                ) from exc
            plan = finalize_director_production_plan_author_draft(
                DirectorProductionPlanAuthorDraft.model_validate(payload),
                artifact,
                plan_id=plan_id,
                revision=revision,
                parent_plan_sha256=parent_plan_sha256,
                authorized_asset_refs=set(authorized_asset_refs),
                authoritative_product_asset_refs=set(
                    authoritative_product_asset_refs
                ),
            )
            generated_text_dependencies = (
                _generated_reference_text_dependencies(plan)
            )
            if generated_text_dependencies:
                dependency_details = (
                    _generated_reference_text_dependency_details(plan)
                )
                raise ValueError(
                    "PRODUCTION_PLAN_GENERATED_REFERENCE_TEXT_DEPENDENCY: "
                    f"references {generated_text_dependencies} require exact "
                    "visible small copy from an image model; offending "
                    "fields are "
                    f"{json.dumps(dependency_details, ensure_ascii=False)}. "
                    f"{_generated_copy_repair_instruction(artifact)}"
                )
            attempts.append(ProductionPlanAttempt(
                operation=operation,
                plan_id=plan_id,
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
            return plan, attempts, errors
        except (TypeError, ValueError) as exc:
            message = str(exc)[:4000]
            errors.append(message)
            attempts.append(ProductionPlanAttempt(
                operation=operation,
                plan_id=plan_id,
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
                "schema_version": "2.0",
                "role": "content_director_production_plan_contract_repair",
                "approved_director_artifact": artifact.model_dump(
                    mode="json"
                ),
                "original_request": current_packet.get(
                    "original_request",
                    current_packet,
                ),
                "invalid_response": raw[:1_000_000],
                "validation_error": message,
                "accumulated_validation_errors": list(dict.fromkeys(errors)),
                "persistent_contract_requirements": (
                    persistent_contract_requirements
                ),
                "asset_authority": {
                    "authorized_asset_refs": authorized_asset_refs,
                    "authoritative_product_asset_refs": (
                        authoritative_product_asset_refs
                    ),
                },
                "repair_rules": {
                    "repair_all_accumulated_contract_errors": True,
                    "preserve_approved_script_verbatim": True,
                    "preserve_semantic_revision_request": True,
                    "do_not_regress_previously_valid_fields": True,
                    "generated_references_and_video_must_be_text_free": True,
                    "copy_delivery_mode": str(
                        artifact.program.audio_mode or ""
                    ),
                    "spoken_copy_must_not_become_local_overlay": (
                        artifact.program.audio_mode == "spoken"
                    ),
                    "do_not_return_hashes": True,
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
    raise AssertionError("unreachable production plan repair loop")


def _critic_packet(
    *,
    artifact: DirectedContentArtifact,
    plan: DirectedProductionPlan,
    criteria: list[ProductionPlanReviewCriterion],
) -> dict[str, Any]:
    output_contract = (
        IndependentProductionPlanCriticVerdict.model_json_schema()
    )
    score_properties = {
        item.criterion_id: {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": item.instruction,
        }
        for item in criteria
    }
    output_contract["properties"]["scores"] = {
        "type": "object",
        "properties": score_properties,
        "required": list(score_properties),
        "additionalProperties": False,
        "minProperties": len(score_properties),
        "maxProperties": len(score_properties),
    }
    return {
        "schema_version": "2.0",
        "role": "independent_production_plan_critic",
        "approved_director_artifact": artifact.model_dump(mode="json"),
        "production_plan": plan.model_dump(mode="json"),
        "review_criteria": [
            item.model_dump(mode="json") for item in criteria
        ],
        "deterministic_preflight": {
            "identity_timing_and_hash_valid": True,
            "script_line_coverage_exact": True,
            "display_copy_is_locally_rendered": True,
            "source_assets_are_authorized": True,
            "product_assets_are_authoritative": True,
            "structured_audio_mode_is_immutable": True,
        },
        "review_rules": {
            "judge_visual_expression_of_the_locked_copy": True,
            "judge_audio_and_voice_continuity": True,
            "judge_whether_reference_intents_are_sufficient_not_excessive": True,
            "do_not_rewrite_the_script": True,
            "do_not_demand_a_fixed_beat_count_or_story_template": True,
            "creative_strategy_prose_cannot_override_structured_audio_mode": True,
            "production_plan_audio_cue_conflict_is_plan_only": True,
            "director_replan_only_for_an_immutable_artifact_conflict": True,
            "cite_audio_cue_ids_for_every_audio_cue_finding": True,
            "cite_only_supplied_beat_line_and_reference_ids": True,
            "return_raw_json_only": True,
        },
        "output_contract": output_contract,
    }


def _parse_critic(
    raw: str,
    *,
    plan: DirectedProductionPlan,
    criteria: list[ProductionPlanReviewCriterion],
) -> IndependentProductionPlanCriticVerdict:
    if not raw or len(raw) > 500_000:
        raise ValueError(
            "production plan critic response is empty or exceeds limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "production plan critic response must be raw JSON"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "production plan critic response is not valid JSON"
        ) from exc
    verdict = IndependentProductionPlanCriticVerdict.model_validate(payload)
    expected_criteria = {item.criterion_id for item in criteria}
    if set(verdict.scores) != expected_criteria:
        raise ValueError(
            "production plan critic score keys must exactly match criteria"
        )
    below = [
        item.criterion_id
        for item in criteria
        if item.blocking
        and verdict.scores[item.criterion_id] < item.minimum_score
    ]
    if verdict.approved and below:
        raise ValueError(
            "production plan critic approved below thresholds: "
            f"{below}"
        )
    valid_beats = {beat.beat_id for beat in plan.visual.beats}
    valid_lines = {
        delivery.line_id for delivery in plan.copy_delivery.deliveries
    }
    valid_references = {
        reference.reference_id for reference in plan.visual.references
    }
    valid_audio_cues = {cue.cue_id for cue in plan.audio.cues}
    for issue in verdict.blocking_issues:
        if not (
            issue.beat_ids
            or issue.line_ids
            or issue.reference_ids
            or issue.audio_cue_ids
        ):
            raise ValueError(
                "production plan issue must cite a concrete plan object"
            )
        if set(issue.beat_ids) - valid_beats:
            raise ValueError("production plan critic cited unknown beat IDs")
        if set(issue.line_ids) - valid_lines:
            raise ValueError("production plan critic cited unknown line IDs")
        if set(issue.reference_ids) - valid_references:
            raise ValueError(
                "production plan critic cited unknown reference IDs"
            )
        if set(issue.audio_cue_ids) - valid_audio_cues:
            raise ValueError(
                "production plan critic cited unknown audio cue IDs"
            )
    if verdict.repair_scope == "director_replan":
        # Audio cues are authored only by the production plan; the approved
        # Director artifact has no cue collection.  A critic occasionally
        # described a cue/beat mismatch accurately but then routed it back to
        # DIRECTOR, where immutable-copy ancestry correctly refused to rewrite
        # an already accepted artifact.  Reject that contradictory verdict as
        # a critic-contract error so the bounded contract-repair call changes
        # only the scope to ``plan_only`` and the plan repairs itself without
        # operator intervention or media spend.
        cue_ids_mentioned_in_evidence = {
            cue_id
            for issue in verdict.blocking_issues
            for cue_id in valid_audio_cues
            if cue_id in " ".join((
                issue.code,
                issue.evidence,
                issue.repair_instruction,
            ))
        }
        cited_plan_audio_cues = {
            cue_id
            for issue in verdict.blocking_issues
            for cue_id in issue.audio_cue_ids
        }
        if cue_ids_mentioned_in_evidence or cited_plan_audio_cues:
            cue_ids = sorted(
                cue_ids_mentioned_in_evidence | cited_plan_audio_cues
            )
            raise ValueError(
                "production-plan audio cue findings must use repair_scope "
                f"plan_only; cited plan-owned cue IDs: {cue_ids}"
            )
    return verdict


async def _request_critic(
    *,
    critic: Any,
    artifact: DirectedContentArtifact,
    plan: DirectedProductionPlan,
    criteria: list[ProductionPlanReviewCriterion],
    maximum_contract_repairs: int,
) -> tuple[
    IndependentProductionPlanCriticVerdict | None,
    list[ProductionPlanCriticAttempt],
    str,
    list[str],
]:
    packet = _critic_packet(
        artifact=artifact,
        plan=plan,
        criteria=criteria,
    )
    current_packet = packet
    errors: list[str] = []
    attempts: list[ProductionPlanCriticAttempt] = []
    raw = ""
    for repair_attempt in range(maximum_contract_repairs + 1):
        response, latency_ms = await critic.create_response(
            input_text=json.dumps(
                current_packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            instructions=(
                "Act only as independent_production_plan_critic. Score every "
                "supplied criterion against the locked script and production "
                "plan. Cite concrete beat, line, or reference IDs for every "
                "blocking issue, including audio cue IDs for cue findings. "
                "A conflict introduced by production_plan.audio.cues is "
                "plan_only; use director_replan only for a contradiction "
                "inside the immutable approved Director artifact. Do not "
                "rewrite the script or demand a fixed "
                "template. Return one raw JSON verdict."
                if repair_attempt == 0
                else "Act only as production_plan_critic_contract_repair. "
                "Preserve the review judgment and correct only the explicit "
                "contract error. Return one raw JSON verdict."
            ),
            metadata={
                "program_id": artifact.program.program_id,
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "revision": plan.revision,
                "contract_repair_attempt": repair_attempt,
                "operation": "production_plan_review",
            },
        )
        raw = extract_output_text(response)
        meta = _response_meta(response)
        try:
            verdict = _parse_critic(
                raw,
                plan=plan,
                criteria=criteria,
            )
            attempts.append(ProductionPlanCriticAttempt(
                plan_sha256=plan.plan_sha256,
                revision=plan.revision,
                contract_repair_attempt=repair_attempt,
                latency_ms=int(latency_ms),
                response_sha256=_sha256(raw),
                outcome="accepted",
                model=str(meta.get("model") or "") or None,
                request_id=(
                    str(meta.get("request_id") or "") or None
                ),
            ))
            return verdict, attempts, raw, errors
        except (TypeError, ValueError) as exc:
            message = str(exc)[:4000]
            errors.append(message)
            attempts.append(ProductionPlanCriticAttempt(
                plan_sha256=plan.plan_sha256,
                revision=plan.revision,
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
                return None, attempts, raw, errors
            current_packet = {
                "schema_version": "1.0",
                "role": "production_plan_critic_contract_repair",
                "original_request": packet,
                "invalid_response": raw[:500_000],
                "validation_error": message,
                "repair_rules": {
                    "preserve_review_judgment": True,
                    "score_exactly_supplied_criterion_ids": True,
                    "cite_only_supplied_plan_object_ids": True,
                    "plan_audio_cue_findings_must_be_plan_only": True,
                    "do_not_rewrite_the_script_or_plan": True,
                    "return_raw_json_only": True,
                },
                "output_contract": packet["output_contract"],
            }
    raise AssertionError("unreachable production plan critic loop")


async def run_content_production_plan_loop(
    *,
    artifact: DirectedContentArtifact,
    plan_id: str,
    policy: DirectorLoopPolicy,
    review_criteria: list[ProductionPlanReviewCriterion],
    capability_catalog: list[dict[str, Any]],
    authorized_asset_refs: list[str],
    authoritative_product_asset_refs: list[str],
    current_plan: DirectedProductionPlan | None = None,
    external_repair_brief: str | None = None,
    director_client: Any | None = None,
    critic_client: Any | None = None,
) -> ProductionPlanLoopResult:
    """Direct and independently review one media-free production plan."""

    if (
        artifact.program.schema_version != "2.0"
        or artifact.program.audio_mode is None
    ):
        return ProductionPlanLoopResult(
            status="quality_pause",
            reason=(
                "approved copy artifact predates structured program audio "
                "authority and requires a zero-media Director v2 replan"
            ),
        )
    if not review_criteria:
        raise ValueError("production plan review criteria are required")
    if len({item.criterion_id for item in review_criteria}) != len(
        review_criteria
    ):
        raise ValueError("production plan review criterion IDs must be unique")
    director = director_client or HermesContentDirectorClient()
    critic = critic_client or HermesContentCriticClient()
    if current_plan is not None:
        if (
            current_plan.visual.director_artifact_sha256
            != artifact.artifact_sha256
        ):
            raise ValueError(
                "current production plan belongs to another Director artifact"
            )
        if not str(external_repair_brief or "").strip():
            raise ValueError(
                "an external repair brief is required when revising a current plan"
            )
        initial_packet = {
            "schema_version": "2.0",
            "role": "content_director_production_plan_external_revision",
            "approved_director_artifact": artifact.model_dump(mode="json"),
            "current_plan": current_plan.model_dump(mode="json"),
            "external_repair_brief": str(external_repair_brief).strip()[:4000],
            "revision_rules": {
                "repair_only_the_evidenced_visual_problem": True,
                "preserve_every_script_line_verbatim": True,
                "preserve_uncited_plan_decisions": True,
                "preserve_asset_authority": True,
                "do_not_add_a_story_template": True,
                "do_not_return_hashes": True,
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
            "output_contract": _draft_schema_for_artifact(
                artifact,
                capability_catalog=capability_catalog,
                authorized_asset_refs=authorized_asset_refs,
                authoritative_product_asset_refs=(
                    authoritative_product_asset_refs
                ),
            ),
        }
        initial_revision = int(current_plan.revision) + 1
        initial_parent_sha256 = current_plan.plan_sha256
    else:
        initial_packet = build_director_production_plan_packet(
            artifact,
            capability_catalog=capability_catalog,
            authorized_asset_refs=authorized_asset_refs,
            authoritative_product_asset_refs=(
                authoritative_product_asset_refs
            ),
        )
        initial_revision = 1
        initial_parent_sha256 = None
    plan, attempts, contract_errors = await _request_plan(
        director=director,
        packet=initial_packet,
        artifact=artifact,
        plan_id=plan_id,
        revision=initial_revision,
        parent_plan_sha256=initial_parent_sha256,
        maximum_contract_repairs=(
            policy.maximum_contract_repairs_per_revision
        ),
        capability_catalog=capability_catalog,
        authorized_asset_refs=authorized_asset_refs,
        authoritative_product_asset_refs=(
            authoritative_product_asset_refs
        ),
    )
    if plan is None:
        return ProductionPlanLoopResult(
            status="quality_pause",
            attempts=attempts,
            contract_errors=contract_errors,
            reason=(
                "production plan did not satisfy the deterministic contract; "
                "no media was authorized"
            ),
        )

    plans = [item for item in (current_plan, plan) if item is not None]
    reviews: list[ProductionPlanReview] = []
    critic_attempts: list[ProductionPlanCriticAttempt] = []
    while True:
        (
            verdict,
            current_critic_attempts,
            raw,
            critic_errors,
        ) = await _request_critic(
            critic=critic,
            artifact=artifact,
            plan=plan,
            criteria=review_criteria,
            maximum_contract_repairs=(
                policy.maximum_contract_repairs_per_revision
            ),
        )
        critic_attempts.extend(current_critic_attempts)
        contract_errors.extend(critic_errors)
        if verdict is None:
            return ProductionPlanLoopResult(
                status="quality_pause",
                final_plan=plan,
                plans=plans,
                attempts=attempts,
                critic_attempts=critic_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "production plan Critic did not return a valid bounded "
                    "verdict; no media was authorized"
                ),
            )
        reviews.append(ProductionPlanReview(
            plan_sha256=plan.plan_sha256,
            verdict=verdict,
            latency_ms=sum(
                attempt.latency_ms
                for attempt in current_critic_attempts
            ),
            response_sha256=_sha256(raw),
        ))
        if verdict.approved:
            return ProductionPlanLoopResult(
                status="approved",
                final_plan=plan,
                plans=plans,
                attempts=attempts,
                critic_attempts=critic_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "production plan passed deterministic validation and "
                    "independent semantic review"
                ),
            )
        if verdict.repair_scope == "director_replan":
            return ProductionPlanLoopResult(
                status="quality_pause",
                final_plan=plan,
                plans=plans,
                attempts=attempts,
                critic_attempts=critic_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "the approved copy artifact requires a Director replan; "
                    "the production-plan stage cannot rewrite it"
                ),
            )
        if plan.revision - 1 >= policy.maximum_revisions:
            return ProductionPlanLoopResult(
                status="quality_pause",
                final_plan=plan,
                plans=plans,
                attempts=attempts,
                critic_attempts=critic_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "production plan did not pass within the project-owned "
                    "revision bound; no media was authorized"
                ),
            )
        revision_packet = {
            "schema_version": "2.0",
            "role": "content_director_production_plan_revision",
            "approved_director_artifact": artifact.model_dump(mode="json"),
            "current_plan": plan.model_dump(mode="json"),
            "critic_blocking_issues": [
                issue.model_dump(mode="json")
                for issue in verdict.blocking_issues
            ],
            "revision_rules": {
                "repair_every_cited_issue": True,
                "preserve_uncited_plan_decisions": True,
                "preserve_every_script_line_verbatim": True,
                "preserve_asset_authority": True,
                "do_not_add_a_story_template": True,
                "do_not_return_hashes": True,
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
            "output_contract": _draft_schema_for_artifact(
                artifact,
                capability_catalog=capability_catalog,
                authorized_asset_refs=authorized_asset_refs,
                authoritative_product_asset_refs=(
                    authoritative_product_asset_refs
                ),
            ),
        }
        next_plan, revision_attempts, revision_errors = await _request_plan(
            director=director,
            packet=revision_packet,
            artifact=artifact,
            plan_id=plan_id,
            revision=plan.revision + 1,
            parent_plan_sha256=plan.plan_sha256,
            maximum_contract_repairs=(
                policy.maximum_contract_repairs_per_revision
            ),
            capability_catalog=capability_catalog,
            authorized_asset_refs=authorized_asset_refs,
            authoritative_product_asset_refs=(
                authoritative_product_asset_refs
            ),
        )
        attempts.extend(revision_attempts)
        contract_errors.extend(revision_errors)
        if next_plan is None:
            return ProductionPlanLoopResult(
                status="quality_pause",
                final_plan=plan,
                plans=plans,
                attempts=attempts,
                critic_attempts=critic_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "production plan revision failed its deterministic "
                    "contract; the last reviewed plan was preserved"
                ),
            )
        plans.append(next_plan)
        plan = next_plan


__all__ = [
    "IndependentProductionPlanCriticVerdict",
    "ProductionPlanAttempt",
    "ProductionPlanCriticAttempt",
    "ProductionPlanCriticIssue",
    "ProductionPlanLoopResult",
    "ProductionPlanReview",
    "run_content_production_plan_loop",
]
