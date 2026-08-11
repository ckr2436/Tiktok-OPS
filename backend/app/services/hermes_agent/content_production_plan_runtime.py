from __future__ import annotations

import hashlib
import json
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
    build_runtime_line_delivery_contract,
    finalize_director_production_plan_author_draft,
    normalize_production_plan_author_payload,
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
    delivery_mode: Literal["strict_approved", "best_available"] = (
        "strict_approved"
    )
    reason: str


def _best_available_production_plan(
    criteria: list[ProductionPlanReviewCriterion],
    plans: list[DirectedProductionPlan],
    reviews: list[ProductionPlanReview],
) -> DirectedProductionPlan | None:
    """Return the strongest plan only when all critical criteria pass."""
    plan_by_hash = {item.plan_sha256: item for item in plans}
    critical = [
        item
        for item in criteria
        if item.blocking and item.priority == "critical"
    ]
    if not critical:
        return None
    candidates: list[tuple[float, DirectedProductionPlan]] = []
    for review in reviews:
        plan = plan_by_hash.get(review.plan_sha256)
        if plan is None:
            continue
        scores = dict(review.verdict.scores or {})
        if any(
            int(scores.get(item.criterion_id, -1)) < int(item.minimum_score)
            for item in critical
        ):
            continue
        mean = (
            sum(int(value) for value in scores.values()) / len(scores)
            if scores
            else 0.0
        )
        candidates.append((mean, plan))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _response_meta(response: Any) -> dict[str, Any]:
    return (
        dict(response.get("_gmv_meta") or {})
        if isinstance(response, dict)
        else {}
    )


def _creative_energy_authority(
    artifact: DirectedContentArtifact,
) -> dict[str, Any]:
    """Expose user-owned creative intensity without prescribing a template.

    Truth and provider policies are hard boundaries, but they are not a reason
    to neutralize a strong hook.  Director revisions and the independent
    Critic receive the same semantic requirements so a provider-safe medium
    change must replace a hook with an equally effective original device
    instead of falling back to a generic establishing shot.
    """
    relevant_kinds = {
        "objective",
        "preservation",
        "differentiation",
        "reference_transfer",
        "visual",
        "acceptance",
        "conversion",
    }
    requirements = [
        requirement.model_dump(mode="json")
        for requirement in artifact.program.intent_requirements
        if requirement.kind in relevant_kinds
    ]
    return {
        "creative_strategy": dict(
            artifact.program.creative_strategy or {}
        ),
        "signed_requirements": requirements,
        "authority_rule": (
            "Preserve the user-requested intensity, contradiction, surprise, "
            "emotion, escalation, cut energy and conversion pressure whenever "
            "they are present in the signed requirements. Product truth and "
            "provider safety constrain expression, not dramatic strength. If "
            "a device is infeasible or forbidden, invent an original provider-"
            "safe replacement with equal observable attention strength; never "
            "silently downgrade it to a calm generic establishing shot. For "
            "short-form attention or conversion work, a sequence with no "
            "experiential change of state, genre-appropriate tension/question/"
            "contrast, or memorable payoff/highlight is itself a flattening "
            "unless the signed intent explicitly chooses meditative minimalism. "
            "The model chooses the mechanism; do not impose a reusable plot."
        ),
    }


def _draft_schema_for_artifact(
    artifact: DirectedContentArtifact,
    *,
    capability_catalog: list[dict[str, Any]],
    authorized_asset_refs: list[str],
    authoritative_product_asset_refs: list[str],
    video_generation_mode: Literal[
        "text_to_video", "image_to_video", "video_to_video"
    ],
) -> dict[str, Any]:
    return build_director_production_plan_packet(
        artifact,
        capability_catalog=capability_catalog,
        authorized_asset_refs=authorized_asset_refs,
        authoritative_product_asset_refs=(
            authoritative_product_asset_refs
        ),
        video_generation_mode=video_generation_mode,
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
    video_generation_mode: Literal[
        "text_to_video", "image_to_video", "video_to_video"
    ],
    idempotency_namespace: str | None = None,
) -> tuple[
    DirectedProductionPlan | None,
    list[ProductionPlanAttempt],
    list[str],
]:
    attempts: list[ProductionPlanAttempt] = []
    errors: list[str] = []
    text_to_video = video_generation_mode == "text_to_video"
    persistent_contract_requirements = {
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
        "physical_state_and_action_are_coherent": (
            "Review every visual beat semantically for physically possible "
            "state transitions. For example, a sealed or closed package cannot "
            "release contents until an explicit opening action occurs. Reject "
            "and repair contradictions instead of relying on a server keyword "
            "detector."
        ),
        "continuity_dependency_matches_the_authored_action": (
            "Judge each beat's continuity_dependency against its complete "
            "environment, action, motion, camera, and continuity state. An "
            "independent segment must be self-contained; a previous_segment "
            "beat may depend on its signed predecessor."
        ),
    }
    audio_execution_policy: dict[str, Any] = {}
    visual_execution_policy: dict[str, Any] = {}
    for capability in capability_catalog:
        if str(capability.get("capability") or "") == "visual.plan":
            visual_execution_policy = dict(
                capability.get("policy") or {}
            )
        if str(capability.get("capability") or "") != "audio.design":
            continue
        audio_execution_policy = dict(capability.get("policy") or {})
    if (
        bool(audio_execution_policy.get("supports_native_spoken_audio"))
        and str(
            audio_execution_policy.get("preferred_spoken_delivery") or ""
        ).strip().lower() == "provider_dialogue"
    ):
        persistent_contract_requirements[
            "native_provider_speech_is_the_default"
        ] = (
            "The selected video model supports native expressive spoken audio. "
            "Use provider_dialogue by default so performance, emotion, action, "
            "and lip-sync or character voiceover are generated together. Use "
            "local_voiceover only when the user explicitly requested post-dubbing "
            "or when a concrete declared provider limitation makes native speech "
            "unavailable; never choose it merely as a convenience."
        )
    if artifact.program.conversion.product_required is False:
        persistent_contract_requirements[
            "unbound_product_words_are_audio_only"
        ] = (
            "This project does not bind a product. Preserve every locked "
            "script line verbatim, including user-supplied product words, but "
            "keep those words in narration only. Do not depict a package, "
            "product, generic or unbranded substitute, contents, serving, "
            "flavor-colored units, or product interaction in any visual "
            "reference, visual beat, or product presentation intent."
        )
    else:
        persistent_contract_requirements[
            "visible_product_uses_authoritative_product_reference"
        ] = (
            "Whenever a visual beat depicts, handles, reveals, or interacts "
            "with the bound product, cite a product-role reference backed by "
            "the authoritative uploaded product asset. Judge the complete beat "
            "semantically; do not infer product visibility from isolated words."
        )
    if text_to_video:
        persistent_contract_requirements.update({
            "zero_reference_media_contract": (
                "This is text-to-video. Visual references are optional semantic "
                "shot-planning metadata only. The runtime will not generate, "
                "upload, extract, or attach any reference image, product anchor, "
                "continuity frame, first/last frame, or reference video."
            ),
            "self_contained_text_prompts": (
                "Every visual beat must be independently expressible as a "
                "self-contained text-to-video prompt."
            ),
        })
    else:
        persistent_contract_requirements.update({
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
            "asset_authority_is_preserved": (
            "The uploaded product image is the authoritative visual identity "
            "reference for package shape, cap, colors, brand, and primary "
            "label. Image and video models render that product naturally in "
            "the scripted scene. Never turn the source packshot or its white "
            "background into a pasted card, overlay, or full-frame hold."
            ),
        })
        if visual_execution_policy:
            persistent_contract_requirements[
                "selected_provider_visual_capability_is_resolved_before_media"
            ] = (
                "The visual style_language and every reference generation_brief "
                "must satisfy the selected visual.plan provider policy in this "
                "packet. Resolve medium, human-face-reference mode, reference "
                "limit, and hard rules now; do not leave a contradiction for "
                "the image generator or final pixel reviewer."
            )
        persistent_contract_requirements[
            "evidence_surface_is_not_confused_with_reference_pixels"
        ] = (
            "Plan each requirement on the surface that can actually prove it: "
            "reference pixels lock identity, scene, product and an animatable "
            "action state; video pixels prove motion and temporal change; "
            "provider dialogue or local voiceover proves spoken copy; "
            "local_overlay proves exact readable display copy. Never demand "
            "local-overlay wording or future motion inside one reference still."
        )
    current_packet = dict(packet)
    # Initial authoring, Critic-directed revision, external visual repair and
    # contract repair must all see the same provider capability authority.
    # Revision packets historically omitted it, allowing a corrected animated
    # plan to regress to photorealism on the next model turn.
    current_packet["production_capabilities"] = list(capability_catalog)
    current_packet["creative_energy_authority"] = (
        _creative_energy_authority(artifact)
    )
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
        video_generation_mode=video_generation_mode,
    )
    for repair_attempt in range(maximum_contract_repairs + 1):
        input_text = json.dumps(
            current_packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instructions = (
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
            "Before returning, compare visual.style_language and every "
            "reference.generation_brief with production_capabilities, and "
            "resolve every provider-medium or human-reference conflict. "
            "Choose a coherent reference topology once: every row must have "
            "one distinct role in chronological order and must be observable "
            "as a still without asking it to prove later motion or local copy. "
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
        )
        idempotency_key = None
        if str(idempotency_namespace or "").strip():
            digest = hashlib.sha256(
                "\n".join((
                    str(idempotency_namespace).strip(),
                    "content_director_production_plan",
                    plan_id,
                    str(int(revision)),
                    str(int(repair_attempt)),
                    instructions,
                    input_text,
                )).encode("utf-8")
            ).hexdigest()
            idempotency_key = f"gmv-content-plan-{digest}"
        response, latency_ms = await director.create_response(
            input_text=input_text,
            instructions=instructions,
            idempotency_key=idempotency_key,
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
            normalized_payload = normalize_production_plan_author_payload(
                payload
            )
            plan = finalize_director_production_plan_author_draft(
                DirectorProductionPlanAuthorDraft.model_validate(
                    normalized_payload
                ),
                artifact,
                plan_id=plan_id,
                revision=revision,
                parent_plan_sha256=parent_plan_sha256,
                authorized_asset_refs=set(authorized_asset_refs),
                authoritative_product_asset_refs=set(
                    authoritative_product_asset_refs
                ),
                require_visual_references=not text_to_video,
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
                "production_capabilities": list(capability_catalog),
                "creative_energy_authority": (
                    _creative_energy_authority(artifact)
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
    capability_catalog: list[dict[str, Any]],
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
    runtime_line_delivery_contract = build_runtime_line_delivery_contract(
        artifact
    )
    return {
        "schema_version": "2.0",
        "role": "independent_production_plan_critic",
        "approved_director_artifact": artifact.model_dump(mode="json"),
        "production_plan": plan.model_dump(mode="json"),
        # The author already sees this catalog.  The independent Critic must
        # receive the same authority or it can approve photoreal human
        # references for a provider that accepts stylized animation only,
        # leaving the expensive pixel reviewer to discover the contradiction.
        "production_capabilities": list(capability_catalog),
        "creative_energy_authority": _creative_energy_authority(artifact),
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
            "runtime_line_delivery_contract_valid": True,
            "whole_script_word_budget_valid": True,
        },
        "runtime_line_delivery_contract": runtime_line_delivery_contract,
        "runtime_delivery_authority": {
            "word_count_authority": "runtime_word_count",
            "minimum_duration_authority": "minimum_delivery_seconds",
            "compiled_interval_authority": (
                "runtime_compiled_interval_seconds"
            ),
            "internal_splice_mode": (
                "runtime_compiled_continuation"
                if artifact.script.schema_version == "2.1"
                else "complete_sentence"
            ),
            "instruction": (
                "These timing facts were produced by the same deterministic "
                "tokenizer and pacing policy that approved the locked script. "
                "Do not retokenize contractions or punctuation, recompute WPM, "
                "or reject a line whose minimum_delivery_seconds fits its "
                "runtime_compiled_interval_seconds. In continuation mode, "
                "adjacent transport clips are one uninterrupted performance."
            ),
        },
        "review_rules": {
            "judge_visual_expression_of_the_locked_copy": True,
            "judge_audio_and_voice_continuity": True,
            "judge_whether_reference_intents_are_sufficient_not_excessive": True,
            "judge_every_reference_against_selected_provider_capabilities_before_media": True,
            "style_language_and_every_generation_brief_must_share_one_provider_safe_medium": True,
            "provider_safety_must_not_flatten_signed_creative_energy": True,
            "infeasible_hook_devices_require_equal_strength_original_replacements": True,
            "reference_topology_count_roles_and_order_must_be_coherent_before_media": True,
            "benchmark_evidence_cannot_override_signed_must_not_reuse_or_excluded_source_artifacts": True,
            "observable_checks_and_transformation_success_checks_are_the_only_mandatory_benchmark_transfer_evidence": True,
            "literal_must_not_reuse_lists_bound_source_exclusions": True,
            "narrative_interpretation_cannot_expand_an_exclusion_beyond_"
            "its_literal_list": True,
            "approved_director_generic_setting_is_authorized_unless_"
            "literally_excluded": True,
            "do_not_require_local_overlay_copy_inside_generated_reference_pixels": True,
            "evidence_must_be_judged_on_its_declared_surface_reference_pixels_video_pixels_audio_or_local_overlay": True,
            "reject_unnecessary_previous_segment_dependencies_that_serialize_otherwise_independent_beats": True,
            "shared_identity_location_style_or_mood_is_not_by_itself_a_previous_frame_dependency": True,
            "do_not_rewrite_the_script": True,
            "do_not_demand_a_fixed_beat_count_or_story_template": True,
            "creative_strategy_prose_cannot_override_structured_audio_mode": True,
            "production_plan_audio_cue_conflict_is_plan_only": True,
            "director_replan_only_for_an_immutable_artifact_conflict": True,
            "runtime_delivery_facts_are_not_reviewable_estimates": True,
            "do_not_retokenize_or_recalculate_approved_copy_timing": True,
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
    capability_catalog: list[dict[str, Any]],
    maximum_contract_repairs: int,
    idempotency_namespace: str | None = None,
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
        capability_catalog=capability_catalog,
    )
    current_packet = packet
    errors: list[str] = []
    attempts: list[ProductionPlanCriticAttempt] = []
    raw = ""
    for repair_attempt in range(maximum_contract_repairs + 1):
        input_text = json.dumps(
            current_packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instructions = (
            "Act only as independent_production_plan_critic. Score every "
            "supplied criterion against the locked script and production "
            "plan. Cite concrete beat, line, or reference IDs for every "
            "blocking issue, including audio cue IDs for cue findings. "
            "A conflict introduced by production_plan.audio.cues is "
            "plan_only; use director_replan only for a contradiction "
            "inside the immutable approved Director artifact. Do not "
            "broaden a source-exclusion narrative beyond the concrete "
            "must_not_reuse items supplied by its signed requirement. A "
            "generic setting explicitly declared by the approved Director "
            "artifact remains authorized unless that generic setting itself "
            "is literally listed as excluded; continue to reject the exact "
            "source actors, props, event sequence, dialogue, composition, UI, "
            "watermark, pixels, or other concrete excluded artifacts. Do not "
            "rewrite the script or demand a fixed "
            "template. Return one raw JSON verdict."
            if repair_attempt == 0
            else "Act only as production_plan_critic_contract_repair. "
            "Preserve the review judgment and correct only the explicit "
            "contract error. Return one raw JSON verdict."
        )
        idempotency_key = None
        if str(idempotency_namespace or "").strip():
            digest = hashlib.sha256(
                "\n".join((
                    str(idempotency_namespace).strip(),
                    "independent_production_plan_critic",
                    plan.plan_id,
                    plan.plan_sha256,
                    str(int(plan.revision)),
                    str(int(repair_attempt)),
                    instructions,
                    input_text,
                )).encode("utf-8")
            ).hexdigest()
            idempotency_key = f"gmv-content-plan-critic-{digest}"
        response, latency_ms = await critic.create_response(
            input_text=input_text,
            instructions=instructions,
            idempotency_key=idempotency_key,
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
    video_generation_mode: Literal[
        "text_to_video", "image_to_video", "video_to_video"
    ] = "image_to_video",
    current_plan: DirectedProductionPlan | None = None,
    external_repair_brief: str | None = None,
    recovery_idempotency_namespace: str | None = None,
    recovery_contract_errors: list[str] | None = None,
    director_client: Any | None = None,
    critic_client: Any | None = None,
) -> ProductionPlanLoopResult:
    """Direct and independently review one media-free production plan."""

    if video_generation_mode not in {
        "text_to_video",
        "image_to_video",
        "video_to_video",
    }:
        raise ValueError("unsupported video_generation_mode")

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
                video_generation_mode=video_generation_mode,
            ),
            "video_generation_mode": video_generation_mode,
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
            video_generation_mode=video_generation_mode,
        )
        initial_revision = 1
        initial_parent_sha256 = None
    normalized_recovery_errors = [
        str(value).strip()[:4000]
        for value in list(recovery_contract_errors or [])[:16]
        if str(value).strip()
    ]
    if str(recovery_idempotency_namespace or "").strip():
        initial_packet["bounded_recovery_context"] = {
            "idempotency_namespace": str(
                recovery_idempotency_namespace
            ).strip()[:255],
            "prior_contract_errors": normalized_recovery_errors,
            "repair_rules": {
                "produce_a_new_candidate_for_this_recovery_generation": True,
                "repair_every_prior_contract_error": True,
                "preserve_the_approved_script_verbatim": True,
            },
        }
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
        video_generation_mode=video_generation_mode,
        idempotency_namespace=recovery_idempotency_namespace,
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
    # ``revision`` is a durable, project-wide lineage number.  It must not be
    # used as this invocation's retry budget: an externally requested repair
    # can legitimately start from an old revision whose ordinal already
    # exceeds ``maximum_revisions``.  Give every bounded loop the configured
    # number of Critic-directed repairs while preserving the monotonic lineage.
    critic_revisions_in_run = 0
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
            capability_catalog=capability_catalog,
            maximum_contract_repairs=(
                policy.maximum_contract_repairs_per_revision
            ),
            idempotency_namespace=recovery_idempotency_namespace,
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
        if critic_revisions_in_run >= policy.maximum_revisions:
            best_available = _best_available_production_plan(
                review_criteria,
                plans,
                reviews,
            )
            if best_available is not None:
                return ProductionPlanLoopResult(
                    status="approved",
                    final_plan=best_available,
                    plans=plans,
                    attempts=attempts,
                    critic_attempts=critic_attempts,
                    reviews=reviews,
                    contract_errors=contract_errors,
                    delivery_mode="best_available",
                    reason=(
                        "bounded production loop converged to the highest-scoring "
                        "plan; every critical criterion passed"
                    ),
                )
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
                video_generation_mode=video_generation_mode,
            ),
            "video_generation_mode": video_generation_mode,
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
            video_generation_mode=video_generation_mode,
            idempotency_namespace=recovery_idempotency_namespace,
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
        critic_revisions_in_run += 1


__all__ = [
    "IndependentProductionPlanCriticVerdict",
    "ProductionPlanAttempt",
    "ProductionPlanCriticAttempt",
    "ProductionPlanCriticIssue",
    "ProductionPlanLoopResult",
    "ProductionPlanReview",
    "run_content_production_plan_loop",
]
