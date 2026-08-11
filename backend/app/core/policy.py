"""Shared helpers for enforcing provider policies at runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from fastapi import Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_session
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.providers import PolicyEnforcementMode
from app.services.audit import log_event
from app.services.policy_engine import PolicyDecision, PolicyEngine


def _ensure_candidate_mapping(
    candidate_ids: Mapping[str, str | None] | Sequence[Mapping[str, str | None]] | None,
    *,
    domain: str | None,
) -> Mapping[str, str | None] | Sequence[Mapping[str, str | None]] | None:
    if domain is None:
        return candidate_ids
    normalized_domain = domain.strip().lower()
    if not normalized_domain:
        return candidate_ids
    if candidate_ids is None:
        return {"domain": normalized_domain}
    if isinstance(candidate_ids, Mapping):
        merged = dict(candidate_ids)
        merged.setdefault("domain", normalized_domain)
        return merged
    enriched: list[Mapping[str, str | None]] = []
    for entry in candidate_ids:
        if isinstance(entry, Mapping):
            merged = dict(entry)
            merged.setdefault("domain", normalized_domain)
            enriched.append(merged)
        else:
            enriched.append({"value": str(entry), "domain": normalized_domain})
    return enriched


def enforce_provider_policy(
    db: Session,
    *,
    provider_key: str,
    resource_type: str,
    me: SessionUser,
    request: Request | None,
    response: Response | None,
    candidate_ids: Mapping[str, str | None] | Sequence[Mapping[str, str | None]] | None = None,
    domain: str | None = None,
    audit_details: dict | None = None,
) -> PolicyDecision:
    provider = provider_key.strip().lower()
    enriched_candidates = _ensure_candidate_mapping(candidate_ids, domain=domain)

    engine = PolicyEngine(db)
    decision = engine.evaluate_policy(
        workspace_id=me.workspace_id,
        provider_key=provider,
        resource_type=resource_type,
        candidate_ids=enriched_candidates,
        now_utc=datetime.now(timezone.utc),
    )

    if response is not None:
        response.headers["X-Policy-Decision"] = "allow" if decision.allowed else "deny"
        response.headers["X-Policy-Enforcement-Mode"] = decision.enforcement_mode.value
        if decision.reason:
            response.headers["X-Policy-Reason"] = decision.reason
        limits = decision.limits
        if limits.rate_limit_rps is not None:
            response.headers["X-Policy-Limit-RPS"] = str(limits.rate_limit_rps)
        if limits.rate_burst is not None:
            response.headers["X-Policy-Limit-Burst"] = str(limits.rate_burst)
        if limits.cooldown_seconds:
            response.headers["X-Policy-Cooldown-Seconds"] = str(limits.cooldown_seconds)
        if limits.max_concurrency is not None:
            response.headers["X-Policy-Max-Concurrency"] = str(limits.max_concurrency)
        if limits.max_entities_per_run is not None:
            response.headers["X-Policy-Max-Entities"] = str(limits.max_entities_per_run)
        if limits.window_cron:
            response.headers["X-Policy-Window"] = limits.window_cron

    if decision.allowed or decision.enforcement_mode != PolicyEnforcementMode.ENFORCE:
        return decision

    log_event(
        db,
        action="policy.enforce.deny",
        resource_type="platform_policy",
        resource_id=decision.matched_policy_ids[0] if decision.matched_policy_ids else None,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        workspace_id=int(me.workspace_id) if me.workspace_id else None,
        details={
            "provider_key": provider,
            "resource_type": resource_type,
            "reason": decision.reason,
            "matched_policy_ids": list(decision.matched_policy_ids),
            "observed_policy_ids": list(decision.observed_policy_ids),
            **(audit_details or {}),
        },
    )
    try:
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise

    raise APIError(
        "POLICY_DENIED",
        "Provider policy denied request.",
        status.HTTP_403_FORBIDDEN,
        data={
            "hint": "Review platform policy configuration.",
            "decision": {
                "allowed": decision.allowed,
                "enforcement_mode": decision.enforcement_mode.value,
                "reason": decision.reason,
                "matched_policy_ids": list(decision.matched_policy_ids),
                "observed_policy_ids": list(decision.observed_policy_ids),
                "trace": list(decision.trace),
            },
            "provider_key": provider,
            "resource_type": resource_type,
        },
    )


def require_provider_policy(
    *,
    provider_key: str | None = None,
    provider_key_param: str | None = None,
    resource_type: str,
    candidate_ids: Mapping[str, str | None] | Sequence[Mapping[str, str | None]] | None = None,
    domain_header: str | None = "x-policy-domain",
):
    """Return a dependency that enforces provider policy before proceeding."""

    async def _dependency(
        request: Request,
        response: Response,
        me: SessionUser = Depends(require_session),
        db: Session = Depends(get_db),
    ) -> PolicyDecision:
        if provider_key is not None:
            key = provider_key
        else:
            if provider_key_param is None:
                raise RuntimeError("provider_key or provider_key_param is required")
            key = request.path_params.get(provider_key_param)
            if not key:
                raise APIError("VALIDATION_ERROR", "provider key is required", status.HTTP_422_UNPROCESSABLE_CONTENT)

        domain_value = request.headers.get(domain_header) if domain_header else None
        return enforce_provider_policy(
            db,
            provider_key=key,
            resource_type=resource_type,
            me=me,
            request=request,
            response=response,
            candidate_ids=candidate_ids,
            domain=domain_value,
        )

    return _dependency


__all__ = [
    "enforce_provider_policy",
    "require_provider_policy",
]

