"""Deterministic policy evaluation utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import Iterable, Mapping, Sequence

from croniter import croniter
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.data.models.providers import (
    PlatformPolicy,
    PolicyEnforcementMode,
    PolicyMode,
)


logger = logging.getLogger("gmv.policy.engine")


@dataclass(frozen=True, slots=True)
class PolicyLimits:
    rate_limit_rps: int | None = None
    rate_burst: int | None = None
    cooldown_seconds: int = 0
    window_cron: str | None = None
    max_concurrency: int | None = None
    max_entities_per_run: int | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    enforcement_mode: PolicyEnforcementMode
    reason: str | None
    matched_policy_ids: tuple[int, ...]
    observed_policy_ids: tuple[int, ...]
    limits: PolicyLimits


CandidateMapping = Mapping[str, str | None]


def _normalize_candidates(candidate_ids: Mapping[str, str | None] | Sequence[Mapping[str, str | None]] | None) -> list[CandidateMapping]:
    if candidate_ids is None:
        return [{}]
    if isinstance(candidate_ids, Mapping):
        return [candidate_ids]
    normalized: list[CandidateMapping] = []
    for idx, item in enumerate(candidate_ids):
        if not isinstance(item, Mapping):
            normalized.append({"id": str(item)})
        else:
            normalized.append(item)
    return normalized or [{}]


def _compile_product_patterns(patterns: Iterable[str]) -> list[tuple[str, re.Pattern[str] | None]]:
    compiled: list[tuple[str, re.Pattern[str] | None]] = []
    for raw in patterns:
        if raw is None:
            continue
        pattern = raw.strip()
        if not pattern:
            continue
        regex: re.Pattern[str] | None = None
        if pattern.startswith("^") or any(ch in pattern for ch in {"*", "?", "[", ")", "|", "+", "$"}):
            try:
                regex = re.compile(pattern)
            except re.error:
                regex = None
        compiled.append((pattern, regex))
    return compiled


def _matches_scope(policy: PlatformPolicy, candidate: CandidateMapping) -> bool:
    def _contains(values: list[str] | None, key: str, *, ignore_case: bool = False) -> bool:
        if not values:
            return True
        value = candidate.get(key)
        if value is None:
            return False
        target = value.strip()
        if ignore_case:
            target = target.upper()
            normalized = {str(v).strip().upper() for v in values if v is not None}
        else:
            normalized = {str(v).strip() for v in values if v is not None}
        return target in normalized

    if not _contains(policy.scope_bc_ids_json, "bc_id"):
        return False
    if not _contains(policy.scope_advertiser_ids_json, "advertiser_id"):
        return False
    if not _contains(policy.scope_shop_ids_json, "shop_id"):
        return False
    if not _contains(policy.scope_region_codes_json, "region_code", ignore_case=True):
        return False

    if policy.scope_product_id_patterns_json:
        product_id = candidate.get("product_id")
        if product_id is None:
            return False
        product_id = str(product_id)
        compiled = getattr(policy, "__compiled_product_patterns", None)
        if compiled is None:
            compiled = _compile_product_patterns(policy.scope_product_id_patterns_json)
            setattr(policy, "__compiled_product_patterns", compiled)
        matched = False
        for raw, regex in compiled:
            if regex is not None:
                if regex.search(product_id):
                    matched = True
                    break
            else:
                if product_id.startswith(raw):
                    matched = True
                    break
        if not matched:
            return False

    return True


def _merge_limits(policies: Iterable[PlatformPolicy]) -> PolicyLimits:
    rps_values: list[int] = []
    burst_values: list[int] = []
    cooldown_values: list[int] = []
    max_concurrency_values: list[int] = []
    max_entities_values: list[int] = []
    windows: list[str] = []

    for policy in policies:
        if policy.rate_limit_rps is not None and policy.rate_limit_rps > 0:
            rps_values.append(int(policy.rate_limit_rps))
        if policy.rate_burst is not None and policy.rate_burst > 0:
            burst_values.append(int(policy.rate_burst))
        if policy.cooldown_seconds is not None:
            cooldown_values.append(int(policy.cooldown_seconds))
        if policy.max_concurrency is not None and policy.max_concurrency > 0:
            max_concurrency_values.append(int(policy.max_concurrency))
        if policy.max_entities_per_run is not None and policy.max_entities_per_run > 0:
            max_entities_values.append(int(policy.max_entities_per_run))
        if policy.window_cron:
            windows.append(policy.window_cron.strip())

    return PolicyLimits(
        rate_limit_rps=min(rps_values) if rps_values else None,
        rate_burst=min(burst_values) if burst_values else None,
        cooldown_seconds=max(cooldown_values) if cooldown_values else 0,
        window_cron=windows[0] if windows else None,
        max_concurrency=min(max_concurrency_values) if max_concurrency_values else None,
        max_entities_per_run=min(max_entities_values) if max_entities_values else None,
    )


class PolicyEngine:
    """Evaluate platform policies deterministically."""

    def __init__(self, db: Session):
        self._db = db

    def evaluate_policy(
        self,
        workspace_id: int | None,
        provider_key: str,
        resource_type: str,
        candidate_ids: Mapping[str, str | None] | Sequence[Mapping[str, str | None]] | None,
        now_utc: datetime,
    ) -> PolicyDecision:
        normalized_candidates = _normalize_candidates(candidate_ids)

        stmt = (
            select(PlatformPolicy)
            .where(
                PlatformPolicy.provider_key == provider_key.strip().lower(),
                PlatformPolicy.is_enabled.is_(True),
                or_(
                    PlatformPolicy.workspace_id.is_(None),
                    PlatformPolicy.workspace_id == (int(workspace_id) if workspace_id is not None else None),
                ),
            )
            .order_by(PlatformPolicy.workspace_id.isnot(None).desc(), PlatformPolicy.id)
        )
        policies = self._db.scalars(stmt).all()

        enforce_policies: list[PlatformPolicy] = []
        observe_policies: list[PlatformPolicy] = []

        enforce_blacklist_hits: list[int] = []
        observe_blacklist_hits: list[int] = []
        enforce_whitelist_hits = [False] * len(normalized_candidates)
        observe_whitelist_hits = [False] * len(normalized_candidates)

        deny_reason: str | None = None
        observed_reason: str | None = None

        def _matches_policy(policy: PlatformPolicy, candidate: CandidateMapping) -> bool:
            if policy.domain and policy.domain not in {"*", resource_type, resource_type.lower()}:
                return False
            return _matches_scope(policy, candidate)

        for policy in policies:
            target = policy.enforcement_mode
            collection = enforce_policies if target == PolicyEnforcementMode.ENFORCE.value else observe_policies
            collection.append(policy)

            for idx, candidate in enumerate(normalized_candidates):
                if not _matches_policy(policy, candidate):
                    continue
                if policy.mode == PolicyMode.BLACKLIST.value:
                    if target == PolicyEnforcementMode.ENFORCE.value:
                        enforce_blacklist_hits.append(int(policy.id))
                        deny_reason = "Matched blacklist policy"
                    else:
                        observe_blacklist_hits.append(int(policy.id))
                        observed_reason = "Matched blacklist policy in observe mode"
                else:
                    if target == PolicyEnforcementMode.ENFORCE.value:
                        enforce_whitelist_hits[idx] = True
                    else:
                        observe_whitelist_hits[idx] = True

        if enforce_blacklist_hits:
            decision_mode = PolicyEnforcementMode.ENFORCE
            limits = _merge_limits(enforce_policies or policies)
            logger.info(
                "policy.decision",
                extra={
                    "workspace_id": workspace_id,
                    "provider": provider_key,
                    "resource_type": resource_type,
                    "decision": "deny",
                    "reason": deny_reason,
                    "policy_ids": enforce_blacklist_hits,
                },
            )
            return PolicyDecision(
                allowed=False,
                enforcement_mode=decision_mode,
                reason=deny_reason,
                matched_policy_ids=tuple(enforce_blacklist_hits),
                observed_policy_ids=tuple(observe_blacklist_hits),
                limits=limits,
            )

        has_enforce_whitelist = any(
            p.mode == PolicyMode.WHITELIST.value and p in enforce_policies for p in policies
        )
        if has_enforce_whitelist and not all(enforce_whitelist_hits):
            decision_mode = PolicyEnforcementMode.ENFORCE
            limits = _merge_limits(enforce_policies or policies)
            deny_reason = "Outside enforce whitelist"
            logger.info(
                "policy.decision",
                extra={
                    "workspace_id": workspace_id,
                    "provider": provider_key,
                    "resource_type": resource_type,
                    "decision": "deny",
                    "reason": deny_reason,
                },
            )
            return PolicyDecision(
                allowed=False,
                enforcement_mode=decision_mode,
                reason=deny_reason,
                matched_policy_ids=tuple(int(p.id) for p in policies if p in enforce_policies),
                observed_policy_ids=tuple(observe_blacklist_hits),
                limits=limits,
            )

        window_violations: list[int] = []
        for policy in enforce_policies:
            if not policy.window_cron:
                continue
            expr = policy.window_cron.strip()
            try:
                if not croniter.match(expr, now_utc):
                    window_violations.append(int(policy.id))
            except (ValueError, KeyError):
                logger.warning("Invalid cron expression on policy %s", policy.id)
                window_violations.append(int(policy.id))

        if window_violations:
            limits = _merge_limits(enforce_policies or policies)
            reason = "Outside enforce window"
            logger.info(
                "policy.decision",
                extra={
                    "workspace_id": workspace_id,
                    "provider": provider_key,
                    "resource_type": resource_type,
                    "decision": "deny",
                    "reason": reason,
                    "policy_ids": window_violations,
                },
            )
            return PolicyDecision(
                allowed=False,
                enforcement_mode=PolicyEnforcementMode.ENFORCE,
                reason=reason,
                matched_policy_ids=tuple(window_violations),
                observed_policy_ids=tuple(observe_blacklist_hits),
                limits=limits,
            )

        decision_mode = PolicyEnforcementMode.ENFORCE
        reason = None

        observe_window_violations: list[int] = []
        for policy in observe_policies:
            if not policy.window_cron:
                continue
            expr = policy.window_cron.strip()
            try:
                if not croniter.match(expr, now_utc):
                    observe_window_violations.append(int(policy.id))
            except (ValueError, KeyError):
                observe_window_violations.append(int(policy.id))

        if observe_blacklist_hits or (any(p.mode == PolicyMode.WHITELIST.value for p in observe_policies) and not all(
            a or b for a, b in zip(enforce_whitelist_hits, observe_whitelist_hits)
        )) or observe_window_violations:
            decision_mode = PolicyEnforcementMode.OBSERVE
            reason = observed_reason or "Observed policy restriction"

        limits_source = enforce_policies if enforce_policies else observe_policies or policies
        limits = _merge_limits(limits_source)

        logger.info(
            "policy.decision",
            extra={
                "workspace_id": workspace_id,
                "provider": provider_key,
                "resource_type": resource_type,
                "decision": "allow" if decision_mode == PolicyEnforcementMode.ENFORCE else "observe",
                "policy_ids": [int(p.id) for p in limits_source],
                "reason": reason,
            },
        )

        return PolicyDecision(
            allowed=True,
            enforcement_mode=decision_mode,
            reason=reason,
            matched_policy_ids=tuple(int(p.id) for p in limits_source),
            observed_policy_ids=tuple(observe_blacklist_hits + observe_window_violations),
            limits=limits,
        )


__all__ = ["PolicyEngine", "PolicyDecision", "PolicyLimits"]
