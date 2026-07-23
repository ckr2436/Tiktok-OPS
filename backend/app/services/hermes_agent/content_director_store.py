from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.hermes_agent import (
    HermesContentDirectorArtifact,
    HermesContentDirectorAttempt,
    HermesContentDirectorBrief,
    HermesContentDirectorReview,
    HermesContentFactoryProject,
)
from app.services.hermes_agent.content_director import DirectorProjectBrief
from app.services.hermes_agent.content_director_runtime import DirectorLoopResult


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _assert_same_payload(
    *,
    label: str,
    existing: Any,
    expected: Any,
) -> None:
    if _canonical_json(existing) != _canonical_json(expected):
        raise ValueError(
            f"{label} identity already exists with different immutable content"
        )


def persist_content_director_loop(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    variant_index: int,
    mode: str,
    brief: DirectorProjectBrief,
    result: DirectorLoopResult,
    execution_key: str = "legacy",
) -> HermesContentDirectorBrief:
    """Persist one complete author/critic loop without committing.

    The caller owns the transaction. Replaying the same loop is idempotent;
    reusing an identity for different content fails closed.
    """
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"shadow", "enforce"}:
        raise ValueError("content director mode must be shadow or enforce")
    normalized_variant = int(variant_index)
    if normalized_variant < 1:
        raise ValueError("variant_index must be positive")
    normalized_execution_key = str(execution_key or "").strip()
    if (
        not normalized_execution_key
        or len(normalized_execution_key) > 128
    ):
        raise ValueError(
            "director execution_key must be 1-128 characters"
        )

    brief_payload = brief.model_dump(mode="json")
    brief_sha256 = _canonical_sha256(brief_payload)
    brief_row = db.execute(
        select(HermesContentDirectorBrief).where(
            HermesContentDirectorBrief.project_id == int(project.id),
            HermesContentDirectorBrief.brief_key == brief.brief_id,
            HermesContentDirectorBrief.execution_key
            == normalized_execution_key,
        )
    ).scalar_one_or_none()
    if brief_row is None:
        brief_row = HermesContentDirectorBrief(
            project_id=int(project.id),
            workspace_id=int(project.workspace_id),
            user_id=project.user_id,
            brief_key=brief.brief_id,
            execution_key=normalized_execution_key,
            variant_index=normalized_variant,
            brief_version=int(brief.brief_version),
            mode=normalized_mode,
            status="running",
            brief_sha256=brief_sha256,
            brief_json=brief_payload,
        )
        db.add(brief_row)
        db.flush()
    else:
        if (
            int(brief_row.workspace_id) != int(project.workspace_id)
            or brief_row.user_id != project.user_id
            or int(brief_row.variant_index) != normalized_variant
            or int(brief_row.brief_version) != int(brief.brief_version)
            or str(brief_row.execution_key)
            != normalized_execution_key
            or str(brief_row.mode) != normalized_mode
            or str(brief_row.brief_sha256) != brief_sha256
        ):
            raise ValueError(
                "director brief identity conflicts with project scope or version"
            )
        _assert_same_payload(
            label="director brief",
            existing=brief_row.brief_json,
            expected=brief_payload,
        )

    artifact_rows: dict[str, HermesContentDirectorArtifact] = {}
    accepted_sha256 = (
        result.final_artifact.artifact_sha256
        if result.status == "approved" and result.final_artifact is not None
        else None
    )
    for artifact in result.artifacts:
        artifact_payload = artifact.model_dump(mode="json")
        artifact_row = db.execute(
            select(HermesContentDirectorArtifact).where(
                HermesContentDirectorArtifact.brief_id == int(brief_row.id),
                HermesContentDirectorArtifact.artifact_key == artifact.artifact_id,
                HermesContentDirectorArtifact.revision == int(artifact.revision),
            )
        ).scalar_one_or_none()
        is_accepted = artifact.artifact_sha256 == accepted_sha256
        if artifact_row is None:
            artifact_row = HermesContentDirectorArtifact(
                brief_id=int(brief_row.id),
                project_id=int(project.id),
                workspace_id=int(project.workspace_id),
                user_id=project.user_id,
                variant_index=normalized_variant,
                artifact_key=artifact.artifact_id,
                revision=int(artifact.revision),
                parent_artifact_sha256=artifact.parent_artifact_sha256,
                artifact_sha256=artifact.artifact_sha256,
                accepted=is_accepted,
                artifact_json=artifact_payload,
            )
            db.add(artifact_row)
            db.flush()
        else:
            if (
                int(artifact_row.project_id) != int(project.id)
                or str(artifact_row.artifact_sha256)
                != artifact.artifact_sha256
                or artifact_row.parent_artifact_sha256
                != artifact.parent_artifact_sha256
                or bool(artifact_row.accepted) != is_accepted
            ):
                raise ValueError(
                    "director artifact identity conflicts with immutable ancestry"
                )
            _assert_same_payload(
                label="director artifact",
                existing=artifact_row.artifact_json,
                expected=artifact_payload,
            )
        artifact_rows[artifact.artifact_sha256] = artifact_row

    for attempt in result.director_attempts:
        attempt_payload = attempt.model_dump(mode="json")
        attempt_row = db.execute(
            select(HermesContentDirectorAttempt).where(
                HermesContentDirectorAttempt.brief_id == int(brief_row.id),
                HermesContentDirectorAttempt.artifact_key == attempt.artifact_id,
                HermesContentDirectorAttempt.revision == int(attempt.revision),
                HermesContentDirectorAttempt.operation == attempt.operation,
                HermesContentDirectorAttempt.contract_repair_attempt
                == int(attempt.contract_repair_attempt),
            )
        ).scalar_one_or_none()
        expected = {
            "latency_ms": int(attempt.latency_ms),
            "response_sha256": attempt.response_sha256,
            "outcome": attempt.outcome,
            "validation_error": attempt.validation_error,
            "model": attempt.model,
            "request_id": attempt.request_id,
        }
        if attempt_row is None:
            attempt_row = HermesContentDirectorAttempt(
                brief_id=int(brief_row.id),
                project_id=int(project.id),
                workspace_id=int(project.workspace_id),
                user_id=project.user_id,
                variant_index=normalized_variant,
                artifact_key=attempt.artifact_id,
                revision=int(attempt.revision),
                operation=attempt.operation,
                contract_repair_attempt=int(attempt.contract_repair_attempt),
                **expected,
            )
            db.add(attempt_row)
            db.flush()
        else:
            actual = {
                key: getattr(attempt_row, key)
                for key in expected
            }
            _assert_same_payload(
                label="director attempt",
                existing=actual,
                expected=expected,
            )

    review_round_by_artifact: dict[str, int] = {}
    for review in result.reviews:
        artifact_row = artifact_rows.get(review.artifact_sha256)
        if artifact_row is None:
            raise ValueError(
                "critic review references an artifact absent from the loop"
            )
        review_round = review_round_by_artifact.get(review.artifact_sha256, 0) + 1
        review_round_by_artifact[review.artifact_sha256] = review_round
        preflight_payload = review.preflight.model_dump(mode="json")
        verdict_payload = review.verdict.model_dump(mode="json")
        review_row = db.execute(
            select(HermesContentDirectorReview).where(
                HermesContentDirectorReview.artifact_id == int(artifact_row.id),
                HermesContentDirectorReview.review_round == review_round,
            )
        ).scalar_one_or_none()
        expected = {
            "approved": bool(review.verdict.approved),
            "preflight_json": preflight_payload,
            "verdict_json": verdict_payload,
            "critic_latency_ms": int(review.critic_latency_ms),
            "critic_response_sha256": review.critic_response_sha256,
        }
        if review_row is None:
            review_row = HermesContentDirectorReview(
                artifact_id=int(artifact_row.id),
                project_id=int(project.id),
                workspace_id=int(project.workspace_id),
                user_id=project.user_id,
                variant_index=normalized_variant,
                review_round=review_round,
                **expected,
            )
            db.add(review_row)
            db.flush()
        else:
            actual = {
                key: getattr(review_row, key)
                for key in expected
            }
            _assert_same_payload(
                label="director review",
                existing=actual,
                expected=expected,
            )

    brief_row.status = result.status
    db.flush()
    return brief_row


__all__ = ["persist_content_director_loop"]
