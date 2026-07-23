from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.hermes_agent.client import (
    HermesContentCriticClient,
    HermesContentDirectorClient,
    extract_output_text,
)
from app.services.hermes_agent.content_director import (
    build_directed_content_artifact,
    CopyPreflightReport,
    DirectedContentArtifact,
    DirectorProjectBrief,
    IndependentCopyCriticVerdict,
    build_delivery_budget_contract,
    build_director_revision_packet,
    build_independent_copy_critic_packet,
    build_initial_director_packet,
    director_author_output_contract,
    parse_director_author_draft_response,
    parse_independent_copy_critic_response,
    preflight_script_copy,
    build_script_package,
    _required_verbatim_voiceover_blocks,
    validate_directed_artifact_against_brief,
)


class DirectorLoopPolicy(BaseModel):
    """Project-owned bound for copy work before a durable quality pause."""

    model_config = ConfigDict(extra="forbid")

    maximum_revisions: int = Field(ge=0, le=10)
    maximum_series_revisions: int | None = Field(
        default=None,
        ge=0,
        le=10,
    )
    maximum_contract_repairs_per_revision: int = Field(ge=0, le=3)
    series_page_size: int = Field(default=10, ge=1, le=100)

    @property
    def series_revision_limit(self) -> int:
        """Use a dedicated series budget when the project supplies one."""

        if self.maximum_series_revisions is not None:
            return int(self.maximum_series_revisions)
        return int(self.maximum_revisions)


class DirectorReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_sha256: str = Field(min_length=64, max_length=64)
    preflight: CopyPreflightReport
    verdict: IndependentCopyCriticVerdict
    critic_latency_ms: int = Field(ge=0)
    critic_response_sha256: str = Field(min_length=64, max_length=64)


class DirectorAttemptRecord(BaseModel):
    """One auditable model response in the authoring side of the loop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["initial", "revision", "contract_repair"]
    artifact_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=1000)
    contract_repair_attempt: int = Field(ge=0, le=3)
    latency_ms: int = Field(ge=0)
    response_sha256: str = Field(min_length=64, max_length=64)
    outcome: Literal["accepted", "contract_rejected"]
    validation_error: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=255)
    request_id: str | None = Field(default=None, max_length=255)


class DirectorLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["approved", "quality_pause"]
    final_artifact: DirectedContentArtifact | None = None
    artifacts: list[DirectedContentArtifact] = Field(default_factory=list, max_length=11)
    director_attempts: list[DirectorAttemptRecord] = Field(default_factory=list, max_length=64)
    reviews: list[DirectorReviewRecord] = Field(default_factory=list, max_length=11)
    contract_errors: list[str] = Field(default_factory=list, max_length=64)
    reason: str


def _raw_json_sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


_AUTHOR_DELIVERY_BUDGET_CODES = frozenset({
    "SPOKEN_COPY_OVER_BUDGET",
    "SPOKEN_SEGMENT_OVER_BUDGET",
    "DISPLAY_COPY_OVER_BUDGET",
    "DISPLAY_SEGMENT_OVER_BUDGET",
})


class _DeliveryBudgetLineReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)


class _DeliveryBudgetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_replacements: list[_DeliveryBudgetLineReplacement] = Field(
        min_length=1,
        max_length=128,
    )


def _author_delivery_budget_issues(
    artifact: DirectedContentArtifact,
) -> list[Any]:
    report = preflight_script_copy(artifact.program, artifact.script)
    return [
        issue
        for issue in report.issues
        if issue.code in _AUTHOR_DELIVERY_BUDGET_CODES
    ]


def _author_delivery_budget_error(
    artifact: DirectedContentArtifact,
) -> str | None:
    """Return a repairable author-contract error before paying for critique.

    Reading/speaking capacity is deterministic and already supplied to the
    author. Sending known-over-budget copy to the semantic critic wastes a
    request and a revision. Keep semantic and conversion findings in the
    independent critic; repair only arithmetic delivery violations here.
    """

    violations = _author_delivery_budget_issues(artifact)
    if not violations:
        return None
    details = "; ".join(
        f"{issue.code}: {issue.message}"
        for issue in violations
    )
    return (
        "director audience-facing copy violates the supplied deterministic "
        f"delivery budget; preserve meaning and repair exactly: {details}"
    )


def _delivery_budget_patch_packet(
    artifact: DirectedContentArtifact,
    *,
    brief: DirectorProjectBrief,
    validation_error: str,
    invalid_patch_response: str | None = None,
) -> dict[str, Any]:
    """Build a compact copy-only repair request for arithmetic overflow.

    The base artifact already passed the full author schema. Asking the model
    to reproduce that large nested object just to remove a few words creates
    avoidable malformed JSON and can regress unrelated creative decisions.
    Only cited line text may change; the runtime merges and revalidates it.
    """

    issues = _author_delivery_budget_issues(artifact)
    allowed_ids = {
        line_id
        for issue in issues
        for line_id in issue.line_ids
    }
    lines_by_id = {
        line.line_id: line
        for line in artifact.script.lines
    }
    return {
        "schema_version": "1.0",
        "role": "content_director_delivery_budget_patch",
        "validation_error": validation_error,
        "invalid_patch_response": (
            str(invalid_patch_response or "")[:100_000] or None
        ),
        "editable_lines": [
            {
                "line_id": line_id,
                "text": lines_by_id[line_id].text,
                "delivery_mode": lines_by_id[line_id].delivery_mode,
            }
            for line_id in sorted(allowed_ids)
            if line_id in lines_by_id
        ],
        "surrounding_canonical_lines": [
            {
                "line_id": line.line_id,
                "text": line.text,
                "delivery_mode": line.delivery_mode,
            }
            for line in artifact.script.lines
        ],
        "budget_violations": [
            issue.model_dump(mode="json")
            for issue in issues
        ],
        "delivery_budget_contract": build_delivery_budget_contract(brief),
        "conversion_authority": brief.conversion.model_dump(mode="json"),
        "repair_rules": {
            "return_only_line_replacements": True,
            "replace_only_editable_line_ids": True,
            "shorten_without_changing_meaning": True,
            "preserve_product_offer_cta_and_confirmed_reason": True,
            "preserve_complete_natural_american_sentences": True,
            "do_not_add_facts_or_outcomes": True,
            "do_not_change_line_order_or_segment_allocation": True,
            "self_count_against_every_cited_budget": True,
        },
        "output_contract": _DeliveryBudgetPatch.model_json_schema(),
    }


def _apply_delivery_budget_patch(
    artifact: DirectedContentArtifact,
    response_text: str,
    *,
    brief: DirectorProjectBrief,
) -> DirectedContentArtifact:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 100_000:
        raise ValueError(
            "director delivery-budget patch is empty or exceeds limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "director delivery-budget patch must be raw JSON"
        )
    try:
        patch = _DeliveryBudgetPatch.model_validate(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(
            "director delivery-budget patch is not valid contract JSON"
        ) from exc

    editable_ids = {
        line_id
        for issue in _author_delivery_budget_issues(artifact)
        for line_id in issue.line_ids
    }
    replacements = {
        item.line_id: item.text.strip()
        for item in patch.line_replacements
    }
    if len(replacements) != len(patch.line_replacements):
        raise ValueError(
            "director delivery-budget patch repeats a line ID"
        )
    unknown = sorted(set(replacements) - editable_ids)
    if unknown:
        raise ValueError(
            "director delivery-budget patch changed non-editable lines: "
            f"{unknown}"
        )
    original_lines = {
        line.line_id: line.text
        for line in artifact.script.lines
    }
    unchanged = sorted(
        line_id
        for line_id, text in replacements.items()
        if original_lines.get(line_id) == text
    )
    if unchanged:
        raise ValueError(
            "director delivery-budget patch did not shorten cited lines: "
            f"{unchanged}"
        )
    lines = [
        line.model_copy(
            update={"text": replacements.get(line.line_id, line.text)}
        )
        for line in artifact.script.lines
    ]
    script = build_script_package(
        schema_version=artifact.script.schema_version,
        script_id=artifact.script.script_id,
        program_id=artifact.script.program_id,
        locale=artifact.script.locale,
        target_duration_seconds=artifact.script.target_duration_seconds,
        edit_headroom_seconds=artifact.script.edit_headroom_seconds,
        speech_rate_wpm=artifact.script.speech_rate_wpm,
        display_reading_rate_wpm=(
            artifact.script.display_reading_rate_wpm
        ),
        audio_mode=artifact.script.audio_mode,
        primary_speaker_id=artifact.script.primary_speaker_id,
        lines=lines,
        segments=list(artifact.script.segments),
    )
    repaired = build_directed_content_artifact(
        artifact_id=artifact.artifact_id,
        revision=artifact.revision,
        parent_artifact_sha256=artifact.parent_artifact_sha256,
        program=artifact.program,
        script=script,
    )
    validate_directed_artifact_against_brief(repaired, brief)
    delivery_error = _author_delivery_budget_error(repaired)
    if delivery_error is not None:
        raise ValueError(delivery_error)
    return repaired


def _failed_blocking_criterion_ids(
    brief: DirectorProjectBrief,
    verdict: IndependentCopyCriticVerdict,
) -> set[str]:
    return {
        criterion.criterion_id
        for criterion in brief.copy_review_criteria
        if (
            criterion.blocking
            and verdict.scores.get(criterion.criterion_id, -1)
            < criterion.minimum_score
        )
    }


def _director_instructions() -> str:
    return (
        "Act only as the content_director role in the supplied packet. "
        "Return exactly one raw JSON object matching output_contract. "
        "Do not use markdown. Do not return or invent integrity hashes. "
        "Use only project_brief truth and registered capabilities."
    )


def _revision_instructions() -> str:
    return (
        "Act only as content_director_revision. Repair the explicit current "
        "artifact within revision_contract.repair_scope. Return exactly one raw "
        "JSON DirectorAuthorDraftPayload and no markdown. Return only fields "
        "owned by the author; the runtime materializes project truth, policy, "
        "timing, and hashes. Use only supplied truth. Implement every blocking "
        "repair_instruction concretely, meet every must_improve threshold, and "
        "do not regress must_preserve criteria. Self-check the draft against every "
        "listed project criterion before returning. Do not return integrity hashes."
    )


def _critic_instructions() -> str:
    return (
        "Act only as an adversarial independent_copy_critic. Judge only the exact "
        "words the audience hears or sees; never infer quality from strategy labels, "
        "purpose fields, or author intent. Apply every review_method rule. For every "
        "review_criteria criterion_id, first provide criterion_evidence with valid "
        "line_ids, exact verbatim quotes from those lines, and a threshold rationale; "
        "then assign its score. A setup is not automatically a human consequence, "
        "and relabeling the setup does not prove what the person concretely lost. "
        "temporal adjacency is not a product bridge, and a preference invented only "
        "inside the product reveal is not a reason to choose. For a delayed reveal, "
        "the selection need must be completed in an earlier audience-facing line or "
        "beat; wording earlier in the same reveal line does not count. A bridge may "
        "truthfully show human agency resolving part of the opening problem, but the "
        "product use case must continue the audience's established causal or psychological "
        "decision path; never require the product to solve an unrelated opening problem. "
        "Do not accept a new use case that is inserted only as an additional desire. "
        "Keep category entry separate from product selection: a confirmed-attribute "
        "preference may explain why this product is considered among alternatives, but "
        "it cannot by itself explain why that product category or use case suddenly "
        "belongs in the story. "
        "Calibrate stakes to the whole-series objective stored in project_brief.truth_payload "
        "as well as the episode objective: when sharp or high-stakes pain is explicitly "
        "requested, routine friction alone cannot pass. If the exact "
        "quotes do not prove a blocking minimum, "
        "fail it. Return exactly one raw JSON object with approved, scores, "
        "criterion_evidence, blocking_issues containing code, line_ids, evidence, "
        "and repair_instruction, plus repair_scope. Use copy_only only when the "
        "existing premise and conversion logic are sound and exact wording alone "
        "can fix the issue. Use director_replan when the story premise, beat logic, "
        "or problem-to-product relationship must change to earn the criterion. "
        "Match output_contract exactly. "
        "Do not use markdown and do not rewrite the script."
    )


def _critic_contract_repair_instructions() -> str:
    return (
        "Act only as an adversarial independent_copy_critic performing a "
        "contract-format repair. Re-evaluate the exact immutable script in "
        "review_packet under the same criteria; do not rewrite it and do not "
        "relax, omit, or rename any criterion. Return exactly one raw JSON "
        "object matching output_contract. Do not use markdown, commentary, "
        "preambles, or trailing text."
    )


def _contract_repair_instructions() -> str:
    return (
        "Act only as content_director_contract_repair. Correct the supplied "
        "invalid_response so it validates against the author-only "
        "output_contract. Do not return project-owned conversion, review "
        "criteria, capability contracts or policies, truth refs, duration, "
        "locale, audience, provider timing, or hashes; the runtime materializes "
        "those fields. For non-spoken audio, every retained line must use "
        "delivery_mode=display and primary_speaker_id must be null. Obey the "
        "configured global and per-segment spoken/display delivery budgets "
        "and the registered production segment plan exactly. Return exactly "
        "one raw "
        "JSON DirectorAuthorDraftPayload. Do not use markdown, add facts, change "
        "creative intent, or return hashes."
    )


def _delivery_budget_patch_instructions() -> str:
    return (
        "Act only as content_director_delivery_budget_patch. The supplied "
        "artifact already passed its full schema; shorten only the cited "
        "editable line text enough to satisfy every exact word ceiling. "
        "Preserve the story meaning, confirmed product reason, product name, "
        "offer, authorized CTA, line order, and complete natural American "
        "sentences. Do not add facts or outcomes. Return exactly one raw JSON "
        "object matching output_contract and no markdown."
    )


async def _request_directed_artifact(
    *,
    director: Any,
    packet: dict[str, Any],
    instructions: str,
    metadata: dict[str, Any],
    brief: DirectorProjectBrief,
    artifact_id: str,
    revision: int,
    parent_artifact_sha256: str | None,
    maximum_contract_repairs: int,
) -> tuple[
    DirectedContentArtifact | None,
    list[str],
    list[DirectorAttemptRecord],
]:
    current_packet = dict(packet)
    current_instructions = instructions
    contract_errors: list[str] = []
    attempts: list[DirectorAttemptRecord] = []
    delivery_patch_base: DirectedContentArtifact | None = None
    for repair_attempt in range(maximum_contract_repairs + 1):
        input_text = json.dumps(
            current_packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        response, latency_ms = await director.create_response(
            input_text=input_text,
            instructions=current_instructions,
            metadata={
                **metadata,
                "contract_repair_attempt": repair_attempt,
            },
        )
        raw_response = extract_output_text(response)
        response_meta = (
            dict(response.get("_gmv_meta") or {})
            if isinstance(response, dict)
            else {}
        )
        operation: Literal["initial", "revision", "contract_repair"] = (
            "contract_repair"
            if repair_attempt > 0
            else ("initial" if revision == 1 else "revision")
        )
        artifact: DirectedContentArtifact | None = None
        try:
            if delivery_patch_base is not None:
                artifact = _apply_delivery_budget_patch(
                    delivery_patch_base,
                    raw_response,
                    brief=brief,
                )
            else:
                artifact = parse_director_author_draft_response(
                    raw_response,
                    brief=brief,
                    artifact_id=artifact_id,
                    revision=revision,
                    parent_artifact_sha256=parent_artifact_sha256,
                )
            validate_directed_artifact_against_brief(artifact, brief)
            delivery_error = _author_delivery_budget_error(artifact)
            if delivery_error is not None:
                raise ValueError(delivery_error)
            attempts.append(
                DirectorAttemptRecord(
                    operation=operation,
                    artifact_id=artifact_id,
                    revision=revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_raw_json_sha256(raw_response),
                    outcome="accepted",
                    model=str(response_meta.get("model") or "") or None,
                    request_id=str(response_meta.get("request_id") or "") or None,
                )
            )
            return artifact, contract_errors, attempts
        except (TypeError, ValueError) as exc:
            error_text = str(exc)[:4000]
            contract_errors.append(error_text)
            attempts.append(
                DirectorAttemptRecord(
                    operation=operation,
                    artifact_id=artifact_id,
                    revision=revision,
                    contract_repair_attempt=repair_attempt,
                    latency_ms=int(latency_ms),
                    response_sha256=_raw_json_sha256(raw_response),
                    outcome="contract_rejected",
                    validation_error=error_text,
                    model=str(response_meta.get("model") or "") or None,
                    request_id=str(response_meta.get("request_id") or "") or None,
                )
            )
            if repair_attempt >= maximum_contract_repairs:
                return None, contract_errors, attempts
            if (
                delivery_patch_base is None
                and "deterministic delivery budget" in error_text
                and isinstance(artifact, DirectedContentArtifact)
                and _required_verbatim_voiceover_blocks(
                    brief.truth_payload
                ) is None
            ):
                delivery_patch_base = artifact
            if delivery_patch_base is not None:
                current_packet = _delivery_budget_patch_packet(
                    delivery_patch_base,
                    brief=brief,
                    validation_error=error_text,
                    invalid_patch_response=(
                        raw_response if repair_attempt > 0 else None
                    ),
                )
                current_instructions = (
                    _delivery_budget_patch_instructions()
                )
            else:
                current_packet = {
                    "schema_version": "2.0",
                    "role": "content_director_contract_repair",
                    "project_brief": brief.model_dump(mode="json"),
                    "invalid_response": raw_response[:1_000_000],
                    "validation_error": error_text,
                    "runtime_owned_fields": [
                        "project objective, platform, locale, audience, duration, and aspect ratio",
                        "conversion intent and source truth",
                        "copy review criteria and quality rubric",
                        "capability contracts and policies",
                        "script locale, duration, edit headroom, and delivery rates",
                        "provider segment durations and integrity hashes",
                    ],
                    "repair_rules": {
                        "change_only_fields_required_by_validation_error": True,
                        "preserve_all_valid_story_copy": True,
                        "non_spoken_lines_must_be_display_only": True,
                        "return_only_author_owned_fields": True,
                    },
                    "delivery_budget_contract": (
                        build_delivery_budget_contract(brief)
                    ),
                    "expected_identity": {
                        "artifact_id": artifact_id,
                        "revision": revision,
                        "parent_artifact_sha256": parent_artifact_sha256,
                    },
                    "output_contract": director_author_output_contract(brief),
                }
                current_instructions = _contract_repair_instructions()
    return None, contract_errors, attempts


async def run_content_director_copy_loop(
    *,
    brief: DirectorProjectBrief,
    artifact_id: str,
    policy: DirectorLoopPolicy,
    director_client: Any | None = None,
    critic_client: Any | None = None,
    initial_revision: int = 1,
    parent_artifact_sha256: str | None = None,
    resume_artifact: DirectedContentArtifact | None = None,
    resume_preflight: CopyPreflightReport | None = None,
    resume_verdict: IndependentCopyCriticVerdict | None = None,
) -> DirectorLoopResult:
    """Create and independently review immutable copy before any media spend."""
    director = director_client or HermesContentDirectorClient()
    critic = critic_client or HermesContentCriticClient()
    start_revision = int(initial_revision)
    if start_revision < 1 or start_revision > 1000:
        raise ValueError("initial_revision must be between 1 and 1000")
    if start_revision > 1 and not str(parent_artifact_sha256 or "").strip():
        raise ValueError("resumed director ancestry requires parent_artifact_sha256")
    resume_items = (resume_artifact, resume_preflight, resume_verdict)
    if start_revision > 1 and any(item is None for item in resume_items):
        raise ValueError(
            "resumed director work requires the rejected artifact, deterministic "
            "preflight, and independent critic verdict"
        )
    if start_revision == 1 and any(item is not None for item in resume_items):
        raise ValueError("new director work cannot include resume review evidence")
    if resume_artifact is not None:
        if resume_artifact.artifact_id != artifact_id:
            raise ValueError("resume artifact identity does not match artifact_id")
        if resume_artifact.artifact_sha256 != parent_artifact_sha256:
            raise ValueError(
                "resume artifact hash does not match parent_artifact_sha256"
            )
        if int(resume_artifact.revision) + 1 != start_revision:
            raise ValueError(
                "resume artifact revision is not the immediate immutable ancestor"
            )
        if bool(resume_preflight.approved and resume_verdict.approved):
            raise ValueError("an approved artifact cannot be resumed as rejected work")

    if resume_artifact is not None:
        initial_packet = build_director_revision_packet(
            resume_artifact,
            brief=brief,
            preflight=resume_preflight,
            verdict=resume_verdict,
        )
        initial_packet["resume_context"] = {
            "operation": "quality_pause_replan",
            "revision": start_revision,
            "parent_artifact_sha256": parent_artifact_sha256,
            "instruction": (
                "Continue from the supplied immutable rejected artifact and its "
                "independent review. Resolve every cited blocking issue, preserve "
                "passing criteria, and do not replay the rejected ancestor."
            ),
        }
        initial_instructions = _revision_instructions()
    else:
        initial_packet = build_initial_director_packet(brief)
        initial_instructions = _director_instructions()
    artifact, contract_errors, director_attempts = await _request_directed_artifact(
        director=director,
        packet=initial_packet,
        instructions=initial_instructions,
        metadata={
            "brief_id": brief.brief_id,
            "artifact_id": artifact_id,
            "revision": start_revision,
        },
        brief=brief,
        artifact_id=artifact_id,
        revision=start_revision,
        parent_artifact_sha256=parent_artifact_sha256,
        maximum_contract_repairs=policy.maximum_contract_repairs_per_revision,
    )
    if artifact is None:
        return DirectorLoopResult(
            status="quality_pause",
            final_artifact=None,
            artifacts=[],
            director_attempts=director_attempts,
            reviews=[],
            contract_errors=contract_errors,
            reason=(
                "director draft did not satisfy the explicit contract within "
                "the project-owned repair bound; no media stage was authorized"
            ),
        )

    artifacts: list[DirectedContentArtifact] = [artifact]
    reviews: list[DirectorReviewRecord] = []

    while True:
        preflight = preflight_script_copy(artifact.program, artifact.script)
        critic_packet = build_independent_copy_critic_packet(
            artifact.program,
            artifact.script,
            preflight,
            brief=brief,
        )
        critic_text = json.dumps(
            critic_packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        verdict: IndependentCopyCriticVerdict | None = None
        raw_verdict = ""
        critic_latency_ms = 0
        for critic_contract_repair_attempt in range(
            policy.maximum_contract_repairs_per_revision + 1
        ):
            if critic_contract_repair_attempt == 0:
                request_text = critic_text
                request_instructions = _critic_instructions()
            else:
                request_text = json.dumps(
                    {
                        "role": "independent_copy_critic_contract_repair",
                        "review_packet": critic_packet,
                        "prior_validation_error": critic_contract_error,
                        "prior_response_sha256": _raw_json_sha256(raw_verdict),
                        "repair_attempt": critic_contract_repair_attempt,
                        "output_contract": IndependentCopyCriticVerdict.model_json_schema(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                request_instructions = _critic_contract_repair_instructions()
            critic_response, attempt_latency_ms = await critic.create_response(
                input_text=request_text,
                instructions=request_instructions,
                metadata={
                    "brief_id": brief.brief_id,
                    "artifact_id": artifact.artifact_id,
                    "artifact_sha256": artifact.artifact_sha256,
                    "revision": artifact.revision,
                    "critic_contract_repair_attempt": (
                        critic_contract_repair_attempt
                    ),
                },
            )
            critic_latency_ms += int(attempt_latency_ms)
            raw_verdict = extract_output_text(critic_response)
            try:
                verdict = parse_independent_copy_critic_response(
                    raw_verdict,
                    packet=critic_packet,
                    script=artifact.script,
                    preflight=preflight,
                )
                break
            except (TypeError, ValueError) as exc:
                critic_contract_error = str(exc)[:4000]
                contract_errors.append(
                    "critic contract rejected "
                    f"attempt={critic_contract_repair_attempt} "
                    f"response_sha256={_raw_json_sha256(raw_verdict)}: "
                    f"{critic_contract_error}"
                )
        if verdict is None:
            return DirectorLoopResult(
                status="quality_pause",
                final_artifact=artifact,
                artifacts=artifacts,
                director_attempts=director_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "independent critic did not satisfy its explicit response "
                    "contract within the project-owned repair bound; no media "
                    "stage was authorized"
                ),
            )
        reviews.append(
            DirectorReviewRecord(
                artifact_sha256=artifact.artifact_sha256,
                preflight=preflight,
                verdict=verdict,
                critic_latency_ms=int(critic_latency_ms),
                critic_response_sha256=_raw_json_sha256(raw_verdict),
            )
        )

        if preflight.approved and verdict.approved:
            return DirectorLoopResult(
                status="approved",
                final_artifact=artifact,
                artifacts=artifacts,
                director_attempts=director_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason="copy passed deterministic preflight and independent critic",
            )

        revisions_used = len(artifacts) - 1
        if revisions_used >= policy.maximum_revisions:
            return DirectorLoopResult(
                status="quality_pause",
                final_artifact=artifact,
                artifacts=artifacts,
                director_attempts=director_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "copy did not pass within the project-owned revision bound; "
                    "no media stage was authorized"
                ),
            )

        current_failed = _failed_blocking_criterion_ids(brief, verdict)
        previously_failed = {
            criterion_id
            for prior_review in reviews[:-1]
            for criterion_id in _failed_blocking_criterion_ids(
                brief,
                prior_review.verdict,
            )
        }
        repeated_copy_failure = bool(current_failed & previously_failed)
        runtime_replan = (
            verdict.repair_scope == "copy_only" and repeated_copy_failure
        )
        revision_packet = build_director_revision_packet(
            artifact,
            brief=brief,
            preflight=preflight,
            verdict=verdict,
            repair_scope_override=(
                "director_replan" if runtime_replan else None
            ),
            repair_scope_override_reason=(
                "A blocking criterion remained below threshold after a copy-only "
                "revision. Broaden the next attempt to story and conversion replanning "
                "instead of repeating surface wording changes."
                if runtime_replan else None
            ),
        )
        revision_packet["output_contract"] = (
            director_author_output_contract(brief)
        )
        (
            next_artifact,
            revision_contract_errors,
            revision_attempts,
        ) = await _request_directed_artifact(
            director=director,
            packet=revision_packet,
            instructions=_revision_instructions(),
            metadata={
                "brief_id": brief.brief_id,
                "artifact_id": artifact.artifact_id,
                "parent_artifact_sha256": artifact.artifact_sha256,
                "revision": artifact.revision + 1,
            },
            brief=brief,
            artifact_id=artifact.artifact_id,
            revision=artifact.revision + 1,
            parent_artifact_sha256=artifact.artifact_sha256,
            maximum_contract_repairs=policy.maximum_contract_repairs_per_revision,
        )
        contract_errors.extend(revision_contract_errors)
        director_attempts.extend(revision_attempts)
        if next_artifact is None:
            return DirectorLoopResult(
                status="quality_pause",
                final_artifact=artifact,
                artifacts=artifacts,
                director_attempts=director_attempts,
                reviews=reviews,
                contract_errors=contract_errors,
                reason=(
                    "director revision did not satisfy the explicit contract "
                    "within the project-owned repair bound; no media stage was authorized"
                ),
            )
        artifact = next_artifact
        artifacts.append(artifact)


__all__ = [
    "DirectorLoopPolicy",
    "DirectorLoopResult",
    "DirectorAttemptRecord",
    "DirectorReviewRecord",
    "run_content_director_copy_loop",
]
