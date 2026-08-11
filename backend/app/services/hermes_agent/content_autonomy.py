from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal


AUTONOMY_POLICY_VERSION = "content-autonomy-v1"

RecoveryStrategy = Literal[
    "targeted_repair",
    "structural_replan",
    "alternate_candidate",
]


def derive_director_loop_policy(
    *,
    target_count: int,
    has_reference_transfer: bool = False,
    has_locked_script: bool = False,
    product_required: bool = False,
) -> dict[str, int]:
    """Return a project-sized zero-media review budget.

    This is an execution budget, not a creative rubric.  Larger series and
    projects with benchmark transfer, immutable copy, or product truth have a
    larger chance of needing one additional semantic pass.  The result stays
    bounded so unattended work cannot hot-loop or spend media while copy is
    unsettled.
    """

    count = max(1, min(50, int(target_count or 1)))
    complexity = sum(
        int(value)
        for value in (
            has_reference_transfer,
            has_locked_script,
            product_required,
        )
    )
    per_video_revisions = min(6, 2 + min(2, complexity))
    series_revisions = min(
        6,
        2 + int(math.ceil(math.log2(max(1, count)))) // 2 + int(
            has_reference_transfer
        ),
    )
    contract_repairs = 2 if complexity else 1
    return {
        "maximum_revisions": per_video_revisions,
        "maximum_series_revisions": series_revisions,
        "maximum_contract_repairs_per_revision": contract_repairs,
        "series_page_size": 10,
    }


def derive_quality_recovery_limit(
    config: dict[str, Any] | None,
    *,
    pause_reason: str,
) -> int:
    """Size one recovery epoch from durable project complexity.

    The epoch is deliberately finite.  Exhaustion starts a cooled new epoch
    with a different strategy; it is not an operator boundary.
    """

    values = dict(config or {})
    explicit = values.get("automatic_quality_recovery_limit")
    if explicit is not None:
        try:
            return max(2, min(12, int(explicit)))
        except (TypeError, ValueError):
            pass
    try:
        target_count = max(
            1,
            min(50, int(values.get("video_count") or 1)),
        )
    except (TypeError, ValueError):
        target_count = 1
    base = 3 + int(target_count >= 4) + int(target_count >= 12)
    if str(pause_reason or "").strip().lower() in {
        "creative_visual_replan_exhausted",
        "final_video_quality_gate",
    }:
        base += 1
    return min(8, base)


def recovery_strategy(
    *,
    attempt_count: int,
    no_progress_cycles: int = 0,
) -> RecoveryStrategy:
    """Escalate the scope instead of replaying the same repair."""

    attempt = max(1, int(attempt_count or 1))
    stagnant = max(0, int(no_progress_cycles or 0))
    if stagnant >= 2 or attempt % 3 == 0:
        return "alternate_candidate"
    if stagnant >= 1 or attempt % 3 == 2:
        return "structural_replan"
    return "targeted_repair"


def quality_snapshot(
    *,
    scores: dict[str, Any] | None,
    issue_codes: list[str] | None,
) -> dict[str, Any]:
    normalized_scores: dict[str, int] = {}
    for key, value in dict(scores or {}).items():
        normalized_key = str(key).strip()
        if not normalized_key or isinstance(value, bool):
            continue
        try:
            normalized_scores[normalized_key] = max(
                0,
                min(100, int(value)),
            )
        except (TypeError, ValueError):
            continue
    normalized_issues = sorted(
        {
            str(value).strip()[:128]
            for value in list(issue_codes or [])
            if str(value).strip()
        }
    )
    mean_score = (
        round(sum(normalized_scores.values()) / len(normalized_scores), 3)
        if normalized_scores
        else None
    )
    canonical = json.dumps(
        {"scores": normalized_scores, "issues": normalized_issues},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "scores": normalized_scores,
        "issue_codes": normalized_issues,
        "mean_score": mean_score,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "policy_version": AUTONOMY_POLICY_VERSION,
    }


def convergence_state(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    prior = dict(previous or {})
    latest = dict(current or {})
    prior_score = prior.get("mean_score")
    current_score = latest.get("mean_score")
    score_delta = (
        round(float(current_score) - float(prior_score), 3)
        if prior_score is not None and current_score is not None
        else None
    )
    same_issue = bool(
        prior.get("fingerprint")
        and prior.get("fingerprint") == latest.get("fingerprint")
    )
    improved = bool(score_delta is not None and score_delta >= 1.0)
    no_progress = bool(same_issue or (score_delta is not None and score_delta < 1.0))
    return {
        "score_delta": score_delta,
        "same_issue_fingerprint": same_issue,
        "improved": improved,
        "no_progress": no_progress,
        "policy_version": AUTONOMY_POLICY_VERSION,
    }


def is_external_operator_blocker(
    *,
    fault_class: str,
    missing_authority: bool = False,
    irreconcilable_user_choice: bool = False,
    manual_pause: bool = False,
) -> bool:
    """Only authority the software cannot manufacture may require a person."""

    if manual_pause or missing_authority or irreconcilable_user_choice:
        return True
    return str(fault_class or "").strip().upper() in {
        "BROWSER_LOGIN_REQUIRED",
        "CAPTCHA_REQUIRED",
        "OAUTH_REQUIRED",
        "MISSING_AUTHORITATIVE_PRODUCT_FACT",
        "MISSING_AUTHORITATIVE_PRODUCT_ASSET",
        "IRRECONCILABLE_USER_REQUIREMENTS",
    }


__all__ = [
    "AUTONOMY_POLICY_VERSION",
    "convergence_state",
    "derive_director_loop_policy",
    "derive_quality_recovery_limit",
    "is_external_operator_blocker",
    "quality_snapshot",
    "recovery_strategy",
]
