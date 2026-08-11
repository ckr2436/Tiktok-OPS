from __future__ import annotations

import hashlib
import json
import re
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
    DirectorAuthorDraftPayload,
    DirectorProjectBrief,
    DirectorScriptSegmentDraft,
    IndependentCopyCriticVerdict,
    ScriptSegmentAllocation,
    build_delivery_budget_contract,
    build_director_revision_packet,
    build_independent_copy_critic_packet,
    build_initial_director_packet,
    director_author_project_brief,
    director_author_output_contract,
    finalize_director_author_draft,
    parse_director_author_draft_response,
    parse_independent_copy_critic_response,
    preflight_script_copy,
    build_script_package,
    _brief_segment_durations,
    _required_verbatim_voiceover_blocks,
    _words,
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
    contract_repair_attempt: int = Field(ge=0, le=8)
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
    delivery_mode: Literal["strict_approved", "best_available"] = (
        "strict_approved"
    )
    reason: str


def _best_available_copy_artifact(
    brief: DirectorBrief,
    artifacts: list[DirectedContentArtifact],
    reviews: list[DirectorReviewRecord],
) -> DirectedContentArtifact | None:
    """Choose the strongest safe candidate when only soft criteria remain."""
    artifact_by_hash = {item.artifact_sha256: item for item in artifacts}
    critical = [
        item
        for item in brief.copy_review_criteria
        if item.blocking and item.priority == "critical"
    ]
    if not critical:
        return None
    candidates: list[tuple[float, DirectedContentArtifact]] = []
    for review in reviews:
        artifact = artifact_by_hash.get(review.artifact_sha256)
        if artifact is None or not review.preflight.approved:
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
        candidates.append((mean, artifact))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _raw_json_sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _model_request_idempotency_key(
    namespace: str | None,
    *,
    role: str,
    revision: int,
    repair_attempt: int,
    input_text: str,
    instructions: str,
) -> str | None:
    """Fence one logical model request without replaying a later recovery.

    The generic Hermes client hashes only the request body when no explicit
    key is supplied.  That is correct for a broker redelivery of one stage,
    but wrong for a newly-created self-heal stage: an identical repair packet
    must be evaluated again instead of receiving the cached rejected answer.
    """

    normalized = str(namespace or "").strip()
    if not normalized:
        return None
    digest = hashlib.sha256(
        json.dumps(
            {
                "namespace": normalized,
                "role": str(role),
                "revision": int(revision),
                "repair_attempt": int(repair_attempt),
                "input_sha256": _raw_json_sha256(input_text),
                "instructions_sha256": _raw_json_sha256(instructions),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"hermes-content-{role[:24]}-{digest}"


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


class _LockedVoiceoverAllocationPatch(BaseModel):
    """Compact author-owned allocation for runtime-locked spoken lines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_indices: list[int] = Field(min_length=1, max_length=1000)


def _runtime_verified_locked_allocation(
    brief: DirectorProjectBrief,
    *,
    expected_line_count: int,
    segment_count: int,
) -> list[int] | None:
    raw = dict(brief.truth_payload or {}).get(
        "locked_voiceover_feasible_allocation"
    )
    values: list[Any] = []
    if isinstance(raw, dict) and abs(
        float(raw.get("speech_rate_wpm") or 0.0)
        - float(brief.speech_rate_wpm)
    ) <= 0.05:
        values = list(raw.get("segment_indices") or [])
    if not values:
        # Compatibility for materialized briefs created before the compiler
        # persisted its proof.  This is a deterministic ordered partition,
        # not model-authored creative work, so derive it locally at execution.
        from app.services.hermes_agent.content_director_profile import (
            _ordered_locked_allocation,
        )

        blocks = _required_verbatim_voiceover_blocks(brief.truth_payload)
        if blocks is None or len(blocks) != expected_line_count:
            return None
        word_counts = [
            len(
                re.findall(
                    r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?|[\u3400-\u9fff]",
                    str(block),
                )
            )
            for block in blocks
        ]
        derived = _ordered_locked_allocation(
            block_word_counts=word_counts,
            segment_durations=_brief_segment_durations(brief),
            speech_rate_wpm=float(brief.speech_rate_wpm),
        )
        values = list(derived or [])
    if len(values) != expected_line_count:
        return None
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > segment_count
        for value in values
    ):
        return None
    indices = [int(value) for value in values]
    if indices != sorted(indices):
        return None
    return indices


def _locked_voiceover_allocation_candidate(
    response_text: str,
    *,
    brief: DirectorProjectBrief,
    validation_error: str,
) -> DirectorAuthorDraftPayload | None:
    """Recover a valid author draft whose only defect is empty allocation.

    Cross-array referential integrity cannot be fully expressed by ordinary
    JSON Schema. Some models correctly return every immutable spoken line but
    leave the segment ``line_ids`` arrays empty. Preserve that otherwise valid
    creative work and request only an ordered segment-index vector instead of
    paying the model to reproduce the complete nested artifact.
    """

    blocks = _required_verbatim_voiceover_blocks(brief.truth_payload)
    if blocks is None or "segment allocation must contain" not in str(
        validation_error or ""
    ):
        return None
    raw = str(response_text or "").strip()
    try:
        draft = DirectorAuthorDraftPayload.model_validate(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    spoken_lines = [
        line
        for line in draft.script.lines
        if line.delivery_mode == "spoken"
    ]
    if len(spoken_lines) != len(blocks):
        return None
    if len(draft.script.lines) != len(spoken_lines):
        return None
    if len(draft.script.segments) != len(_brief_segment_durations(brief)):
        return None
    return draft


def _locked_voiceover_allocation_packet(
    draft: DirectorAuthorDraftPayload,
    *,
    brief: DirectorProjectBrief,
    validation_error: str,
) -> dict[str, Any]:
    blocks = _required_verbatim_voiceover_blocks(brief.truth_payload)
    if blocks is None:
        raise ValueError("locked voiceover allocation requires source blocks")
    spoken_lines = [
        line
        for line in draft.script.lines
        if line.delivery_mode == "spoken"
    ]
    segment_count = len(_brief_segment_durations(brief))
    output_contract = _LockedVoiceoverAllocationPatch.model_json_schema()
    indices_schema = output_contract["properties"]["segment_indices"]
    indices_schema["minItems"] = len(spoken_lines)
    indices_schema["maxItems"] = len(spoken_lines)
    indices_schema["items"] = {
        "type": "integer",
        "minimum": 1,
        "maximum": segment_count,
    }
    verified_baseline = _runtime_verified_locked_allocation(
        brief,
        expected_line_count=len(spoken_lines),
        segment_count=segment_count,
    )
    return {
        "schema_version": "1.0",
        "role": "content_director_locked_voiceover_allocation_patch",
        "validation_error": validation_error,
        "immutable_spoken_lines": [
            {
                "position": position,
                "line_id": line.line_id,
                "text": block,
            }
            for position, (line, block) in enumerate(
                zip(spoken_lines, blocks, strict=True),
                1,
            )
        ],
        "segment_count": segment_count,
        "delivery_budget_contract": build_delivery_budget_contract(brief),
        "runtime_verified_feasible_segment_indices": verified_baseline,
        "repair_rules": {
            "return_one_segment_index_per_spoken_line": True,
            "preserve_input_line_order": True,
            "segment_indices_must_be_nondecreasing": True,
            "allocate_every_line_exactly_once": True,
            "fit_every_segment_spoken_word_limit": True,
            "do_not_rewrite_copy_or_return_any_other_fields": True,
        },
        "output_contract": output_contract,
    }


def _apply_locked_voiceover_allocation_patch(
    draft: DirectorAuthorDraftPayload,
    response_text: str,
    *,
    brief: DirectorProjectBrief,
    artifact_id: str,
    revision: int,
    parent_artifact_sha256: str | None,
) -> DirectedContentArtifact:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 100_000:
        raise ValueError(
            "director locked-voiceover allocation patch is empty or exceeds limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "director locked-voiceover allocation patch must be raw JSON"
        )
    try:
        patch = _LockedVoiceoverAllocationPatch.model_validate(
            json.loads(raw)
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(
            "director locked-voiceover allocation patch is not valid contract JSON"
        ) from exc

    spoken_lines = [
        line
        for line in draft.script.lines
        if line.delivery_mode == "spoken"
    ]
    indices = list(patch.segment_indices)
    segment_count = len(_brief_segment_durations(brief))
    if len(indices) != len(spoken_lines):
        raise ValueError(
            "director locked-voiceover allocation patch length mismatch: "
            f"expected={len(spoken_lines)}, actual={len(indices)}"
        )
    if any(index < 1 or index > segment_count for index in indices):
        raise ValueError(
            "director locked-voiceover allocation patch uses an unknown segment"
        )
    if indices != sorted(indices):
        raise ValueError(
            "director locked-voiceover allocation patch reordered source lines"
        )
    line_ids_by_segment = {
        segment_index: []
        for segment_index in range(1, segment_count + 1)
    }
    for line, segment_index in zip(spoken_lines, indices, strict=True):
        line_ids_by_segment[segment_index].append(line.line_id)
    repaired_script = draft.script.model_copy(
        update={
            "segments": [
                DirectorScriptSegmentDraft(
                    segment_index=segment_index,
                    line_ids=line_ids_by_segment[segment_index],
                )
                for segment_index in range(1, segment_count + 1)
            ]
        }
    )
    return finalize_director_author_draft(
        draft.model_copy(update={"script": repaired_script}),
        brief,
        artifact_id=artifact_id,
        revision=revision,
        parent_artifact_sha256=parent_artifact_sha256,
    )


def _locked_voiceover_artifact_allocation_packet(
    artifact: DirectedContentArtifact,
    *,
    brief: DirectorProjectBrief,
    validation_error: str,
) -> dict[str, Any]:
    blocks = _required_verbatim_voiceover_blocks(brief.truth_payload)
    spoken_lines = [
        line
        for line in artifact.script.lines
        if line.delivery_mode == "spoken"
    ]
    if blocks is None or len(spoken_lines) != len(blocks):
        raise ValueError(
            "locked voiceover artifact allocation requires exact source lines"
        )
    segment_count = len(artifact.script.segments)
    output_contract = _LockedVoiceoverAllocationPatch.model_json_schema()
    indices_schema = output_contract["properties"]["segment_indices"]
    indices_schema["minItems"] = len(spoken_lines)
    indices_schema["maxItems"] = len(spoken_lines)
    indices_schema["items"] = {
        "type": "integer",
        "minimum": 1,
        "maximum": segment_count,
    }
    current_segment_by_line_id = {
        line_id: segment.segment_index
        for segment in artifact.script.segments
        for line_id in segment.line_ids
    }
    verified_baseline = _runtime_verified_locked_allocation(
        brief,
        expected_line_count=len(spoken_lines),
        segment_count=segment_count,
    )
    return {
        "schema_version": "1.0",
        "role": "content_director_locked_voiceover_allocation_patch",
        "validation_error": validation_error,
        "immutable_spoken_lines": [
            {
                "position": position,
                "line_id": line.line_id,
                "text": block,
                "current_segment_index": current_segment_by_line_id.get(
                    line.line_id
                ),
            }
            for position, (line, block) in enumerate(
                zip(spoken_lines, blocks, strict=True),
                1,
            )
        ],
        "segment_count": segment_count,
        "delivery_budget_contract": build_delivery_budget_contract(brief),
        "runtime_verified_feasible_segment_indices": verified_baseline,
        "repair_rules": {
            "return_one_segment_index_per_spoken_line": True,
            "preserve_input_line_order": True,
            "segment_indices_must_be_nondecreasing": True,
            "allocate_every_line_exactly_once": True,
            "fit_every_segment_spoken_word_limit": True,
            "repair_the_cited_overloaded_segments": True,
            "do_not_rewrite_copy_or_return_any_other_fields": True,
        },
        "output_contract": output_contract,
    }


def _apply_locked_voiceover_artifact_allocation_patch(
    artifact: DirectedContentArtifact,
    response_text: str,
    *,
    brief: DirectorProjectBrief,
) -> DirectedContentArtifact:
    raw = str(response_text or "").strip()
    if not raw or len(raw) > 100_000:
        raise ValueError(
            "director locked-voiceover allocation patch is empty or exceeds limit"
        )
    if raw.startswith("```") or raw.endswith("```"):
        raise ValueError(
            "director locked-voiceover allocation patch must be raw JSON"
        )
    try:
        patch = _LockedVoiceoverAllocationPatch.model_validate(
            json.loads(raw)
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(
            "director locked-voiceover allocation patch is not valid contract JSON"
        ) from exc
    spoken_lines = [
        line
        for line in artifact.script.lines
        if line.delivery_mode == "spoken"
    ]
    indices = list(patch.segment_indices)
    segment_count = len(artifact.script.segments)
    if len(indices) != len(spoken_lines):
        raise ValueError(
            "director locked-voiceover allocation patch length mismatch: "
            f"expected={len(spoken_lines)}, actual={len(indices)}"
        )
    if any(index < 1 or index > segment_count for index in indices):
        raise ValueError(
            "director locked-voiceover allocation patch uses an unknown segment"
        )
    if indices != sorted(indices):
        raise ValueError(
            "director locked-voiceover allocation patch reordered source lines"
        )
    line_ids_by_segment = {
        segment_index: []
        for segment_index in range(1, segment_count + 1)
    }
    for line, segment_index in zip(spoken_lines, indices, strict=True):
        line_ids_by_segment[segment_index].append(line.line_id)
    segments = [
        ScriptSegmentAllocation(
            segment_index=segment.segment_index,
            duration_seconds=segment.duration_seconds,
            line_ids=line_ids_by_segment[segment.segment_index],
        )
        for segment in artifact.script.segments
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
        lines=list(artifact.script.lines),
        segments=segments,
    )
    repaired = build_directed_content_artifact(
        artifact_id=artifact.artifact_id,
        revision=artifact.revision,
        parent_artifact_sha256=artifact.parent_artifact_sha256,
        program=artifact.program,
        script=script,
    )
    validate_directed_artifact_against_brief(repaired, brief)
    return repaired


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


def _proportional_word_ceilings(
    line_ids: list[str],
    *,
    budget: int,
    current_counts: dict[str, int],
) -> dict[str, int]:
    """Allocate an exact numeric ceiling without authoring any copy."""

    ordered = list(line_ids)
    if not ordered:
        return {}
    bounded_budget = max(0, int(budget))
    minimum = 1 if bounded_budget >= len(ordered) else 0
    ceilings = {line_id: minimum for line_id in ordered}
    remaining = bounded_budget - sum(ceilings.values())
    if remaining <= 0:
        return ceilings
    weights = {
        line_id: max(1, int(current_counts.get(line_id, 1)) - minimum)
        for line_id in ordered
    }
    total_weight = max(1, sum(weights.values()))
    raw_shares = {
        line_id: remaining * weights[line_id] / total_weight
        for line_id in ordered
    }
    for line_id in ordered:
        ceilings[line_id] += int(raw_shares[line_id])
    leftover = bounded_budget - sum(ceilings.values())
    for line_id in sorted(
        ordered,
        key=lambda item: (
            raw_shares[item] - int(raw_shares[item]),
            current_counts.get(item, 0),
            item,
        ),
        reverse=True,
    )[:leftover]:
        ceilings[line_id] += 1
    return ceilings


def _delivery_line_word_ceilings(
    artifact: DirectedContentArtifact,
) -> dict[str, int]:
    """Translate signed global/segment timing into per-line numeric targets."""

    lines_by_id = {line.line_id: line for line in artifact.script.lines}
    current_counts = {
        line.line_id: len(_words(line.text)) for line in artifact.script.lines
    }
    ceilings: dict[str, int] = {}
    for mode in ("spoken", "display"):
        mode_line_ids: list[str] = []
        for segment in artifact.script.segments:
            line_ids = [
                line_id
                for line_id in segment.line_ids
                if lines_by_id[line_id].delivery_mode == mode
            ]
            if not line_ids:
                continue
            mode_line_ids.extend(line_ids)
            rate = (
                artifact.script.speech_rate_wpm
                if mode == "spoken"
                else artifact.script.display_reading_rate_wpm
            )
            segment_budget = int(
                float(segment.duration_seconds) * float(rate) / 60.0
            )
            if mode == "spoken":
                segment_budget += max(1, int(segment_budget * 0.05))
            ceilings.update(_proportional_word_ceilings(
                line_ids,
                budget=segment_budget,
                current_counts=current_counts,
            ))

        global_budget = (
            artifact.script.spoken_budget_words
            if mode == "spoken"
            else artifact.script.display_budget_words
        )
        overflow = sum(ceilings.get(line_id, 0) for line_id in mode_line_ids) - int(
            global_budget
        )
        while overflow > 0:
            reducible = [
                line_id
                for line_id in mode_line_ids
                if ceilings.get(line_id, 0) > 1
            ]
            if not reducible:
                break
            line_id = max(
                reducible,
                key=lambda item: (
                    ceilings[item],
                    current_counts.get(item, 0),
                    item,
                ),
            )
            ceilings[line_id] -= 1
            overflow -= 1
    return ceilings


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
    line_word_ceilings = _delivery_line_word_ceilings(artifact)
    budget_contract = build_delivery_budget_contract(brief)
    segment_contracts = {
        int(item["segment_index"]): item
        for item in list(budget_contract.get("segments") or [])
    }
    segment_by_line_id = {
        line_id: int(segment.segment_index)
        for segment in artifact.script.segments
        for line_id in segment.line_ids
    }

    violation_targets: list[dict[str, Any]] = []
    for issue in issues:
        code = str(issue.code or "")
        current_words: int | None = None
        maximum_words: int | None = None
        segment_index: int | None = None
        if code == "SPOKEN_COPY_OVER_BUDGET":
            current_words = int(artifact.script.spoken_word_count)
            maximum_words = int(artifact.script.spoken_budget_words)
        elif code == "DISPLAY_COPY_OVER_BUDGET":
            current_words = int(artifact.script.display_word_count)
            maximum_words = int(artifact.script.display_budget_words)
        elif code in {
            "SPOKEN_SEGMENT_OVER_BUDGET",
            "DISPLAY_SEGMENT_OVER_BUDGET",
        }:
            candidate_segments = {
                segment_by_line_id.get(line_id)
                for line_id in issue.line_ids
            } - {None}
            if len(candidate_segments) == 1:
                segment_index = int(next(iter(candidate_segments)))
                segment = next(
                    item
                    for item in artifact.script.segments
                    if int(item.segment_index) == segment_index
                )
                mode = (
                    "spoken"
                    if code == "SPOKEN_SEGMENT_OVER_BUDGET"
                    else "display"
                )
                current_words = sum(
                    len(_words(lines_by_id[line_id].text))
                    for line_id in segment.line_ids
                    if lines_by_id[line_id].delivery_mode == mode
                )
                maximum_words = int(
                    segment_contracts.get(segment_index, {}).get(
                        f"{mode}_max_words",
                        0,
                    )
                )
                if mode == "spoken":
                    maximum_words += max(
                        1,
                        int(maximum_words * 0.05),
                    )
        violation_targets.append({
            "code": code,
            "segment_index": segment_index,
            "current_words": current_words,
            "maximum_words": maximum_words,
            "minimum_words_to_remove": (
                max(0, int(current_words) - int(maximum_words))
                if current_words is not None and maximum_words is not None
                else None
            ),
            "line_ids": list(issue.line_ids),
        })
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
                "current_word_count": len(
                    _words(lines_by_id[line_id].text)
                ),
                "maximum_word_count": int(
                    line_word_ceilings.get(
                        line_id,
                        len(_words(lines_by_id[line_id].text)),
                    )
                ),
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
        "exact_reduction_targets": violation_targets,
        "delivery_budget_contract": budget_contract,
        "conversion_authority": brief.conversion.model_dump(mode="json"),
        "repair_rules": {
            "return_only_line_replacements": True,
            "replace_only_editable_line_ids": True,
            "omit_lines_that_do_not_need_changes": True,
            "every_returned_replacement_must_have_fewer_words": True,
            "every_returned_replacement_must_fit_maximum_word_count": True,
            "return_at_least_one_actual_shortening": True,
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
    changed_replacements = {
        line_id: text
        for line_id, text in replacements.items()
        if original_lines.get(line_id) != text
    }
    if not changed_replacements:
        raise ValueError(
            "director delivery-budget patch did not shorten any cited line"
        )
    shorter_replacements = {
        line_id: text
        for line_id, text in changed_replacements.items()
        if len(_words(text)) < len(_words(original_lines.get(line_id, "")))
    }
    if not shorter_replacements:
        raise ValueError(
            "director delivery-budget patch did not reduce any cited line "
            "word count"
        )
    lines = [
        line.model_copy(
            update={
                # A patch can contain useful shortenings plus same-length
                # paraphrases. Keep the useful arithmetic work and ignore the
                # latter so one no-op line cannot discard the whole batch.
                "text": shorter_replacements.get(line.line_id, line.text)
            }
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
        "purpose fields, or author intent. Apply every non-null review_method rule. "
        "The registered review_criteria are the exclusive scoring contract. Never "
        "smuggle an unregistered product, category-entry, conversion, offer, CTA, or "
        "reason-to-choose requirement into a generic coherence or comprehension "
        "criterion. Product-bridge rules apply only when review_criteria explicitly "
        "contains product_relevance_bridge or reason_to_choose. For every "
        "review_criteria criterion_id, first provide criterion_evidence with valid "
        "line_ids, exact verbatim quotes from those lines, and a threshold rationale; "
        "then assign its score. A setup is not automatically a human consequence, "
        "and relabeling the setup does not prove what the person concretely lost. "
        "When the registered criteria require product evaluation, temporal adjacency "
        "is not a product bridge, and a preference invented only "
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
        "Respect the signed requirement surface in project_brief: visual action, "
        "camera, performance, escalation, and payoff belong to later production "
        "and multimodal media review, not copy review. Calibrate stakes in the "
        "words only when a registered criterion and signed spoken or displayed "
        "copy requirement explicitly assign stakes to those words. Never convert "
        "a visual-intensity requirement into demanded pain, loss, or consequence "
        "in the script. If the exact quotes do not prove a blocking copy minimum, "
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
        "object matching output_contract. Use only criterion IDs listed in "
        "valid_criterion_ids and only line IDs listed in valid_script_lines. "
        "Quotes must be exact substrings of their cited immutable lines. If "
        "the immutable script repeats identical copy in different line IDs, "
        "it is valid to repeat that exact quote while citing both real lines. "
        "Correct the concrete prior_invalid_response according to "
        "prior_validation_error; do not guess a fresh coordinate system. Do "
        "not use markdown, commentary, preambles, or trailing text."
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
        "Requirement mappings may cite only IDs listed in "
        "valid_coordinates_from_invalid_response: script_line_ids belong in "
        "script_line_ids, capability_node_ids belong in capability_node_ids, "
        "and registered capability names are not node IDs. "
        "one raw "
        "JSON DirectorAuthorDraftPayload. Do not use markdown, add facts, change "
        "creative intent, or return hashes."
    )


def _author_contract_repair_coordinates(
    response_text: str,
    *,
    brief: DirectorProjectBrief,
) -> dict[str, Any] | None:
    """Expose exact author-owned IDs so contract repair does not guess.

    Cross-reference validation happens after the author payload itself is
    parsed. A model can therefore return structurally valid JSON while citing
    a series variant ID or a capability name where the current single-video
    line/node ID is required. Preserve the creative response, but enumerate
    its real coordinate vocabulary in the next model repair packet.
    """

    raw = str(response_text or "").strip()
    if not raw or raw.startswith("```") or raw.endswith("```"):
        return None
    try:
        draft = DirectorAuthorDraftPayload.model_validate_json(raw)
    except (TypeError, ValueError):
        return None
    return {
        "script_line_ids": [line.line_id for line in draft.script.lines],
        "capability_node_ids": [
            node.node_id for node in draft.program.execution_graph
        ],
        "segment_indices": [
            int(segment.segment_index) for segment in draft.script.segments
        ],
        "registered_capability_names": [
            item.capability for item in brief.capability_catalog
        ],
    }


def _delivery_budget_patch_instructions() -> str:
    return (
        "Act only as content_director_delivery_budget_patch. The supplied "
        "artifact already passed its full schema; shorten only the cited "
        "editable line text enough to satisfy every exact word ceiling and "
        "minimum_words_to_remove target. Every returned replacement must "
        "contain fewer counted words than its current_word_count; omit lines "
        "that do not need changes, but return at least one real shortening. "
        "Each replacement must be at or below its maximum_word_count; rewrite "
        "all editable lines whose current_word_count exceeds that maximum. "
        "Preserve the story meaning, confirmed product reason, product name, "
        "offer, authorized CTA, line order, and complete natural American "
        "sentences. Do not add facts or outcomes. Return exactly one raw JSON "
        "object matching output_contract and no markdown."
    )


def _locked_voiceover_allocation_patch_instructions() -> str:
    return (
        "Act only as content_director_locked_voiceover_allocation_patch. "
        "Return one nondecreasing segment index for every immutable spoken "
        "line, in the supplied order, while fitting each exact segment word "
        "budget. When runtime_verified_feasible_segment_indices is present, "
        "it is a mechanically verified valid reference and may be returned "
        "unchanged. Return exactly one raw JSON object matching output_contract. "
        "Do not rewrite or repeat any copy, IDs, metadata, or project fields."
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
    request_idempotency_namespace: str | None = None,
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
    locked_allocation_draft: DirectorAuthorDraftPayload | None = None
    locked_allocation_artifact: DirectedContentArtifact | None = None
    # A valid author draft can still need a tiny arithmetic-only copy patch.
    # Keep that focused model repair independent from the broader schema
    # repair budget so one malformed author response cannot consume the only
    # chance to remove a few words.  A partial shortening can leave one word
    # over budget and the next model turn can legitimately return a no-op
    # paraphrase.  Keep two additional focused turns for that exact-fit case;
    # the loop remains strictly bounded and every call is still audited.
    maximum_delivery_patch_attempt = min(
        8,
        maximum_contract_repairs + (6 if maximum_contract_repairs > 0 else 0),
    )
    for repair_attempt in range(maximum_delivery_patch_attempt + 1):
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
            idempotency_key=_model_request_idempotency_key(
                request_idempotency_namespace,
                role="director",
                revision=revision,
                repair_attempt=repair_attempt,
                input_text=input_text,
                instructions=current_instructions,
            ),
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
            if locked_allocation_artifact is not None:
                artifact = (
                    _apply_locked_voiceover_artifact_allocation_patch(
                        locked_allocation_artifact,
                        raw_response,
                        brief=brief,
                    )
                )
            elif locked_allocation_draft is not None:
                artifact = _apply_locked_voiceover_allocation_patch(
                    locked_allocation_draft,
                    raw_response,
                    brief=brief,
                    artifact_id=artifact_id,
                    revision=revision,
                    parent_artifact_sha256=parent_artifact_sha256,
                )
            elif delivery_patch_base is not None:
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
            if (
                locked_allocation_artifact is None
                and isinstance(artifact, DirectedContentArtifact)
                and "deterministic delivery budget" in error_text
                and _required_verbatim_voiceover_blocks(
                    brief.truth_payload
                ) is not None
            ):
                locked_allocation_artifact = artifact
            if locked_allocation_draft is None:
                locked_allocation_draft = (
                    _locked_voiceover_allocation_candidate(
                        raw_response,
                        brief=brief,
                        validation_error=error_text,
                    )
                )
            if (
                "deterministic delivery budget" in error_text
                and isinstance(artifact, DirectedContentArtifact)
                and _required_verbatim_voiceover_blocks(
                    brief.truth_payload
                ) is None
            ):
                # Keep a valid partial shortening as the base for the next
                # focused patch. Replaying every retry against the original
                # over-budget artifact makes a model repeatedly remove the
                # same first word while the remaining segment overflow never
                # changes.
                delivery_patch_base = artifact
            active_repair_limit = (
                maximum_delivery_patch_attempt
                if delivery_patch_base is not None
                else maximum_contract_repairs
            )
            if repair_attempt >= active_repair_limit:
                return None, contract_errors, attempts
            if locked_allocation_artifact is not None:
                current_packet = (
                    _locked_voiceover_artifact_allocation_packet(
                        locked_allocation_artifact,
                        brief=brief,
                        validation_error=error_text,
                    )
                )
                current_instructions = (
                    _locked_voiceover_allocation_patch_instructions()
                )
                continue
            if locked_allocation_draft is not None:
                current_packet = _locked_voiceover_allocation_packet(
                    locked_allocation_draft,
                    brief=brief,
                    validation_error=error_text,
                )
                current_instructions = (
                    _locked_voiceover_allocation_patch_instructions()
                )
                continue
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
                repair_coordinates = _author_contract_repair_coordinates(
                    raw_response,
                    brief=brief,
                )
                current_packet = {
                    "schema_version": "2.0",
                    "role": "content_director_contract_repair",
                    "project_brief": director_author_project_brief(brief),
                    "invalid_response": raw_response[:1_000_000],
                    "validation_error": error_text,
                    "valid_coordinates_from_invalid_response": (
                        repair_coordinates
                    ),
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
                        "requirement_script_line_ids_must_come_from_valid_coordinates": True,
                        "requirement_capability_node_ids_must_come_from_valid_coordinates": True,
                        "never_use_variant_ids_as_script_line_ids": True,
                        "never_use_capability_names_as_capability_node_ids": True,
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
    pending_review_artifact: DirectedContentArtifact | None = None,
    allow_fresh_revision_from_accepted_ancestor: bool = False,
    request_idempotency_namespace: str | None = None,
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
    if pending_review_artifact is not None and any(
        item is not None for item in resume_items
    ):
        raise ValueError(
            "a pending-review artifact cannot include rejected resume evidence"
        )
    if pending_review_artifact is not None and allow_fresh_revision_from_accepted_ancestor:
        raise ValueError(
            "a pending-review artifact cannot start a fresh accepted revision"
        )
    if (
        start_revision > 1
        and not allow_fresh_revision_from_accepted_ancestor
        and any(item is None for item in resume_items)
        and pending_review_artifact is None
    ):
        raise ValueError(
            "resumed director work requires the rejected artifact, deterministic "
            "preflight, and independent critic verdict"
        )
    if (
        start_revision > 1
        and allow_fresh_revision_from_accepted_ancestor
        and any(item is not None for item in resume_items)
    ):
        raise ValueError(
            "a fresh revision from an accepted ancestor cannot include rejected "
            "resume evidence"
        )
    if start_revision == 1 and allow_fresh_revision_from_accepted_ancestor:
        raise ValueError(
            "a fresh revision from an accepted ancestor requires revision ancestry"
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

    if pending_review_artifact is not None:
        if pending_review_artifact.artifact_id != artifact_id:
            raise ValueError(
                "pending-review artifact identity does not match artifact_id"
            )
        if pending_review_artifact.artifact_sha256 != parent_artifact_sha256:
            raise ValueError(
                "pending-review artifact hash does not match parent_artifact_sha256"
            )
        if int(pending_review_artifact.revision) + 1 != start_revision:
            raise ValueError(
                "pending-review artifact is not the immediate immutable ancestor"
            )

    if pending_review_artifact is not None:
        # The author response was durably accepted, but the independent critic
        # never produced a contract-valid verdict (for example, after a worker
        # restart or exhausted critic contract repair).  Resume at the missing
        # review boundary instead of silently rewriting or blindly rejecting
        # the immutable author artifact.
        artifact = pending_review_artifact
        contract_errors: list[str] = []
        director_attempts: list[DirectorAttemptRecord] = []
    elif resume_artifact is not None:
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
    if pending_review_artifact is None:
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
            request_idempotency_namespace=request_idempotency_namespace,
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
                        "prior_invalid_response": raw_verdict,
                        "prior_response_sha256": _raw_json_sha256(raw_verdict),
                        "repair_attempt": critic_contract_repair_attempt,
                        "valid_criterion_ids": [
                            str(item.get("criterion_id") or "")
                            for item in list(
                                critic_packet.get("review_criteria") or []
                            )
                            if isinstance(item, dict)
                            and str(item.get("criterion_id") or "").strip()
                        ],
                        "valid_script_lines": [
                            {
                                "line_id": line.line_id,
                                "text": line.text,
                            }
                            for line in artifact.script.lines
                        ],
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
                idempotency_key=_model_request_idempotency_key(
                    request_idempotency_namespace,
                    role="critic",
                    revision=int(artifact.revision),
                    repair_attempt=critic_contract_repair_attempt,
                    input_text=request_text,
                    instructions=request_instructions,
                ),
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
            best_available = _best_available_copy_artifact(
                brief,
                artifacts,
                reviews,
            )
            if best_available is not None:
                return DirectorLoopResult(
                    status="approved",
                    final_artifact=best_available,
                    artifacts=artifacts,
                    director_attempts=director_attempts,
                    reviews=reviews,
                    contract_errors=contract_errors,
                    delivery_mode="best_available",
                    reason=(
                        "bounded copy loop converged to the highest-scoring "
                        "candidate; deterministic and critical criteria passed"
                    ),
                )
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
            request_idempotency_namespace=request_idempotency_namespace,
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
