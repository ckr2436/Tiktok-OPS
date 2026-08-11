"""Admin APIs for managing platform policies (v1)."""

from __future__ import annotations

import logging
import re
from typing import Any

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError
from app.core.policy import enforce_provider_policy
from app.data.db import get_db
from app.data.models.providers import (
    PlatformPolicy,
    PlatformProvider,
    PolicyEnforcementMode,
    PolicyMode,
)
from app.services.audit import log_event
from app.services.policy_engine import PolicyEngine
from app.services.providers import catalog as provider_catalog


logger = logging.getLogger("gmv.platform.policies")
router = APIRouter(
    prefix="/api/v1/admin/platform/policies",
    tags=["Admin / Platform Policies"],
)

_DOMAIN_PATTERN = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_ALLOWED_SCOPE_KEYS = {"bc_ids", "advertiser_ids", "store_ids", "product_ids"}
_ALLOWED_TOP_SCOPE_KEYS = {"include", "exclude"}
_SCOPE_TO_FIELD = {
    "bc_ids": "bc_id",
    "advertiser_ids": "advertiser_id",
    "store_ids": "store_id",
    "product_ids": "product_id",
}
_SORT_FIELDS = {
    "created_at": PlatformPolicy.created_at.asc(),
    "-created_at": PlatformPolicy.created_at.desc(),
    "updated_at": PlatformPolicy.updated_at.asc(),
    "-updated_at": PlatformPolicy.updated_at.desc(),
    "name": PlatformPolicy.name.asc(),
    "-name": PlatformPolicy.name.desc(),
}


class BusinessScopesResponse(BaseModel):
    include: dict[str, list[str]] = Field(default_factory=dict)
    exclude: dict[str, list[str]] = Field(default_factory=dict)


class PolicyLimitsResponse(BaseModel):
    rate_limit_rps: int | None = None
    rate_burst: int | None = None
    cooldown_seconds: int = 0
    window_cron: str | None = None
    max_concurrency: int | None = None
    max_entities_per_run: int | None = None


class PolicyResponse(BaseModel):
    id: int
    provider_key: str
    name: str
    mode: PolicyMode
    enforcement_mode: PolicyEnforcementMode
    status: str
    is_enabled: bool
    domains: list[str]
    business_scopes: BusinessScopesResponse
    limits: PolicyLimitsResponse
    description: str | None = None
    created_at: str
    updated_at: str


class PolicyPage(BaseModel):
    items: list[PolicyResponse]
    total: int
    page: int
    page_size: int


class PolicyDecisionTrace(BaseModel):
    policy_id: int
    mode: PolicyMode
    enforcement_mode: PolicyEnforcementMode
    matched: bool
    domain_match: bool
    scope_match: bool


class PolicyDryRunCandidate(BaseModel):
    domain: str | None = Field(default=None, description="Host-only value")
    bc_id: str | None = None
    advertiser_id: str | None = None
    store_id: str | None = None
    product_id: str | None = None


class PolicyDryRunRequest(BaseModel):
    resource_type: str = Field(default="admin.platform.policy.test", max_length=128)
    candidates: list[PolicyDryRunCandidate] = Field(default_factory=list)


class PolicyDryRunResponse(BaseModel):
    provider_key: str
    allowed: bool
    enforcement_mode: PolicyEnforcementMode
    reason: str | None = None
    matched_policy_ids: list[int]
    observed_policy_ids: list[int]
    limits: PolicyLimitsResponse
    trace: list[PolicyDecisionTrace]


class ErrorDetail(BaseModel):
    code: str
    message: str
    data: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ValidationCollector:
    """Collects field-level validation errors."""

    def __init__(self) -> None:
        self._fields: dict[str, str] = {}

    def add(self, field: str, message: str) -> None:
        self._fields.setdefault(field, message)

    def raise_if_errors(self) -> None:
        if self._fields:
            raise APIError(
                "VALIDATION_ERROR",
                "Validation failed.",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"fields": self._fields},
            )


def _normalize_provider_key(value: Any, errors: ValidationCollector) -> str | None:
    if value is None:
        errors.add("provider_key", "provider_key is required.")
        return None
    text = str(value).strip()
    if not text:
        errors.add("provider_key", "provider_key must not be empty.")
        return None
    return text.lower()


def _normalize_name(value: Any, errors: ValidationCollector) -> tuple[str | None, str | None]:
    if value is None:
        errors.add("name", "name is required.")
        return None, None
    text = str(value).strip()
    if not text:
        errors.add("name", "name must not be empty.")
        return None, None
    if len(text) > 128:
        errors.add("name", "name must be 128 characters or fewer.")
        return None, None
    return text, text.lower()


def _normalize_description(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_mode(value: Any, errors: ValidationCollector) -> PolicyMode | None:
    if isinstance(value, PolicyMode):
        return value
    if value is None:
        errors.add("mode", "mode is required.")
        return None
    if isinstance(value, str):
        candidate = value.strip().upper()
        if not candidate:
            errors.add("mode", "mode must not be empty.")
            return None
        try:
            return PolicyMode(candidate)
        except ValueError:
            errors.add(
                "mode",
                "mode must be one of: WHITELIST, BLACKLIST.",
            )
            return None
    errors.add("mode", "mode must be a string value.")
    return None


def _normalize_enforcement_mode(
    value: Any, errors: ValidationCollector
) -> PolicyEnforcementMode | None:
    if isinstance(value, PolicyEnforcementMode):
        return value
    if value is None:
        errors.add("enforcement_mode", "enforcement_mode is required.")
        return None
    if isinstance(value, str):
        candidate = value.strip().upper()
        if not candidate:
            errors.add("enforcement_mode", "enforcement_mode must not be empty.")
            return None
        try:
            return PolicyEnforcementMode(candidate)
        except ValueError:
            errors.add(
                "enforcement_mode",
                "enforcement_mode must be one of: ENFORCE, DRYRUN, OFF.",
            )
            return None
    errors.add("enforcement_mode", "enforcement_mode must be a string value.")
    return None


def _normalize_domains(value: Any, errors: ValidationCollector) -> list[str]:
    if value is None:
        errors.add("domains", "domains is required.")
        return []
    if not isinstance(value, list):
        errors.add("domains", "domains must be an array of hostnames.")
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(value):
        host = str(item).strip().lower()
        if not host:
            errors.add(f"domains[{idx}]", "domain must not be empty.")
            continue
        if len(host) > 255:
            errors.add(f"domains[{idx}]", "domain is too long.")
            continue
        if not _DOMAIN_PATTERN.fullmatch(host):
            errors.add(
                f"domains[{idx}]",
                "domain must be a valid host without scheme, port, or path.",
            )
            continue
        if host in seen:
            continue
        seen.add(host)
        normalized.append(host)

    if not normalized:
        errors.add("domains", "domains must contain at least one valid host.")
    return normalized


def _normalize_optional_positive_int(
    value: Any,
    errors: ValidationCollector,
    *,
    field: str,
    minimum: int = 1,
) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.add(field, f"{field} must be an integer.")
        return None
    if number < minimum:
        errors.add(field, f"{field} must be greater than or equal to {minimum}.")
        return None
    return number


def _normalize_cooldown(value: Any, errors: ValidationCollector) -> int:
    if value is None or value == "":
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.add("cooldown_seconds", "cooldown_seconds must be an integer.")
        return 0
    if number < 0:
        errors.add("cooldown_seconds", "cooldown_seconds must be zero or positive.")
        return 0
    return number


def _normalize_window_cron(value: Any, errors: ValidationCollector) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        from croniter import croniter  # local import to avoid weight at module load

        croniter(text)
    except Exception:
        errors.add("window_cron", "window_cron must be a valid cron expression.")
        return None
    return text


def _normalize_scope_bucket(
    value: Any, errors: ValidationCollector, *, path: str
) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.add(path, "must be an object containing scope arrays.")
        return {}

    normalized: dict[str, list[str]] = {}
    for key, raw in value.items():
        if key not in _ALLOWED_SCOPE_KEYS:
            errors.add(f"{path}.{key}", "unknown scope key.")
            continue
        items = raw or []
        if not isinstance(items, list):
            errors.add(f"{path}.{key}", "must be an array of identifiers.")
            continue
        seen: set[str] = set()
        result: list[str] = []
        for idx, item in enumerate(items):
            text = str(item).strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            result.append(text)
        normalized[key] = result
    return normalized


def _normalize_business_scopes(
    value: Any, errors: ValidationCollector
) -> dict[str, dict[str, list[str]]]:
    if value is None:
        return {"include": {}, "exclude": {}}
    if not isinstance(value, dict):
        errors.add("business_scopes", "business_scopes must be an object.")
        return {"include": {}, "exclude": {}}

    for key in value.keys():
        if key not in _ALLOWED_TOP_SCOPE_KEYS:
            errors.add(f"business_scopes.{key}", "unknown business_scopes key.")

    include = _normalize_scope_bucket(
        value.get("include"), errors, path="business_scopes.include"
    )
    exclude = _normalize_scope_bucket(
        value.get("exclude"), errors, path="business_scopes.exclude"
    )

    for scope_key, include_values in include.items():
        if scope_key in exclude:
            exclude[scope_key] = [
                item for item in exclude[scope_key] if item not in set(include_values)
            ]

    return {"include": include, "exclude": exclude}


def _candidate_from_business_scopes(scopes: dict[str, dict[str, list[str]]]) -> dict[str, str] | None:
    include = scopes.get("include") or {}
    candidate: dict[str, str] = {}
    for key, field in _SCOPE_TO_FIELD.items():
        values = include.get(key)
        if values:
            candidate[field] = str(values[0])
    return candidate or None


def _merge_candidates(*candidates: dict[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for candidate in candidates:
        if not candidate:
            continue
        merged.update(candidate)
    return merged or None


def _parse_bool(value: Any, errors: ValidationCollector, field: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    errors.add(field, f"{field} must be a boolean value.")
    return None


def _prepare_policy_payload(
    raw: dict[str, Any], *, errors: ValidationCollector
) -> dict[str, Any]:
    provider_key = _normalize_provider_key(raw.get("provider_key"), errors)
    name, name_normalized = _normalize_name(raw.get("name"), errors)
    mode = _normalize_mode(raw.get("mode"), errors)
    enforcement_mode = _normalize_enforcement_mode(
        raw.get("enforcement_mode"), errors
    )
    domains = _normalize_domains(raw.get("domains"), errors)
    business_scopes = _normalize_business_scopes(
        raw.get("business_scopes"), errors
    )
    is_enabled = _parse_bool(raw.get("is_enabled", True), errors, "is_enabled")
    description = _normalize_description(raw.get("description"))
    rate_limit_rps = _normalize_optional_positive_int(
        raw.get("rate_limit_rps"), errors, field="rate_limit_rps"
    )
    rate_burst = _normalize_optional_positive_int(
        raw.get("rate_burst"), errors, field="rate_burst"
    )
    cooldown_seconds = _normalize_cooldown(raw.get("cooldown_seconds"), errors)
    max_concurrency = _normalize_optional_positive_int(
        raw.get("max_concurrency"), errors, field="max_concurrency"
    )
    max_entities = _normalize_optional_positive_int(
        raw.get("max_entities_per_run"), errors, field="max_entities_per_run"
    )
    window_cron = _normalize_window_cron(raw.get("window_cron"), errors)

    errors.raise_if_errors()

    assert provider_key is not None
    assert name is not None and name_normalized is not None
    assert mode is not None
    assert enforcement_mode is not None
    assert is_enabled is not None

    return {
        "provider_key": provider_key,
        "name": name,
        "name_normalized": name_normalized,
        "mode": mode,
        "enforcement_mode": enforcement_mode,
        "domains": domains,
        "business_scopes": business_scopes,
        "is_enabled": is_enabled,
        "description": description,
        "rate_limit_rps": rate_limit_rps,
        "rate_burst": rate_burst,
        "cooldown_seconds": cooldown_seconds,
        "max_concurrency": max_concurrency,
        "max_entities_per_run": max_entities,
        "window_cron": window_cron,
    }


def _ensure_provider(db: Session, key: str) -> PlatformProvider:
    provider_catalog.sync_registry_with_session(db)
    normalized_key = key.strip().lower()
    registry_keys = {definition.key for definition in provider_catalog.iter_registry_definitions()}
    if normalized_key not in registry_keys:
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"fields": {"provider_key": "provider is not registered."}},
        )

    provider = provider_catalog.get_provider(db, normalized_key)
    if provider is None:
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"fields": {"provider_key": "provider is not configured."}},
        )

    if not bool(provider.is_enabled):
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"fields": {"provider_key": "provider is currently disabled."}},
        )

    return provider


def _policy_snapshot(policy: PlatformPolicy) -> dict[str, Any]:
    return {
        "id": int(policy.id),
        "provider_key": policy.provider_key,
        "name": policy.name,
        "mode": PolicyMode(policy.mode).value,
        "enforcement_mode": PolicyEnforcementMode(policy.enforcement_mode).value,
        "domains": list(policy.domains_json or []),
        "business_scopes": policy.business_scopes_json or {"include": {}, "exclude": {}},
        "is_enabled": bool(policy.is_enabled),
        "description": policy.description,
        "rate_limit_rps": policy.rate_limit_rps,
        "rate_burst": policy.rate_burst,
        "cooldown_seconds": policy.cooldown_seconds,
        "max_concurrency": policy.max_concurrency,
        "max_entities_per_run": policy.max_entities_per_run,
        "window_cron": policy.window_cron,
        "created_at": policy.created_at.isoformat(timespec="microseconds"),
        "updated_at": policy.updated_at.isoformat(timespec="microseconds"),
    }


def _limits_to_response(policy: PlatformPolicy) -> PolicyLimitsResponse:
    return PolicyLimitsResponse(
        rate_limit_rps=policy.rate_limit_rps,
        rate_burst=policy.rate_burst,
        cooldown_seconds=int(policy.cooldown_seconds or 0),
        window_cron=policy.window_cron,
        max_concurrency=policy.max_concurrency,
        max_entities_per_run=policy.max_entities_per_run,
    )


def _trace_to_response(entries: tuple[dict[str, Any], ...]) -> list[PolicyDecisionTrace]:
    results: list[PolicyDecisionTrace] = []
    for entry in entries:
        policy_id = int(entry.get("policy_id", 0) or 0)
        try:
            mode = PolicyMode(entry.get("mode", PolicyMode.WHITELIST.value))
        except ValueError:
            mode = PolicyMode.WHITELIST
        try:
            enforcement = PolicyEnforcementMode(entry.get("enforcement_mode", PolicyEnforcementMode.ENFORCE.value))
        except ValueError:
            enforcement = PolicyEnforcementMode.ENFORCE
        results.append(
            PolicyDecisionTrace(
                policy_id=policy_id,
                mode=mode,
                enforcement_mode=enforcement,
                matched=bool(entry.get("matched")),
                domain_match=bool(entry.get("domain_match")),
                scope_match=bool(entry.get("scope_match")),
            )
        )
    return results


def _policy_to_response(policy: PlatformPolicy) -> PolicyResponse:
    scopes = policy.business_scopes_json or {"include": {}, "exclude": {}}
    return PolicyResponse(
        id=int(policy.id),
        provider_key=policy.provider_key,
        name=policy.name,
        mode=PolicyMode(policy.mode),
        enforcement_mode=PolicyEnforcementMode(policy.enforcement_mode),
        status="ENABLED" if policy.is_enabled else "DISABLED",
        is_enabled=bool(policy.is_enabled),
        domains=list(policy.domains_json or []),
        business_scopes=BusinessScopesResponse(
            include={k: list(v) for k, v in (scopes.get("include") or {}).items()},
            exclude={k: list(v) for k, v in (scopes.get("exclude") or {}).items()},
        ),
        limits=_limits_to_response(policy),
        description=policy.description,
        created_at=policy.created_at.isoformat(timespec="microseconds"),
        updated_at=policy.updated_at.isoformat(timespec="microseconds"),
    )


def _apply_policy_filters(
    base: Select[tuple[PlatformPolicy]],
    *,
    provider_key: str | None,
    mode: PolicyMode | None,
    domain: str | None,
    name: str | None,
    status_filter: bool | None,
) -> Select[tuple[PlatformPolicy]]:
    conditions = []
    if provider_key:
        conditions.append(PlatformPolicy.provider_key == provider_key)
    if mode is not None:
        conditions.append(PlatformPolicy.mode == mode.value)
    if domain:
        like_pattern = f"%{domain.lower()}%"
        conditions.append(func.lower(PlatformPolicy.domain).like(like_pattern))
    if name:
        like_pattern = f"%{name.lower()}%"
        conditions.append(func.lower(PlatformPolicy.name).like(like_pattern))
    if status_filter is not None:
        conditions.append(PlatformPolicy.is_enabled.is_(status_filter))

    if conditions:
        base = base.where(and_(*conditions))
    return base


def _parse_mode_filter(value: str | None, errors: ValidationCollector) -> PolicyMode | None:
    if value is None or value == "":
        return None
    return _normalize_mode(value, errors)


def _parse_status_filter(value: str | None, errors: ValidationCollector) -> bool | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"enabled", "true", "1", "yes"}:
        return True
    if normalized in {"disabled", "false", "0", "no"}:
        return False
    errors.add("status", "status must be ENABLED or DISABLED.")
    return None


def _resolve_sort(value: str | None, errors: ValidationCollector):
    if not value:
        return _SORT_FIELDS["-created_at"]
    key = value if value in _SORT_FIELDS else value.lower()
    if key not in _SORT_FIELDS:
        errors.add(
            "sort",
            "sort must be one of created_at, -created_at, updated_at, -updated_at, name, -name.",
        )
        return _SORT_FIELDS["-created_at"]
    return _SORT_FIELDS[key]


def _handle_integrity_error(exc: IntegrityError) -> None:
    message = str(exc).lower()
    if "uq_platform_policy_provider_name" in message or "name_normalized" in message:
        raise APIError(
            "DUPLICATE_NAME",
            "A policy with this provider and name already exists.",
            status.HTTP_409_CONFLICT,
        ) from exc
    raise exc


def _ensure_unique_name(
    db: Session, provider_key: str, name_normalized: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(PlatformPolicy.id).where(
        PlatformPolicy.provider_key == provider_key,
        PlatformPolicy.name_normalized == name_normalized,
    )
    if exclude_id is not None:
        stmt = stmt.where(PlatformPolicy.id != exclude_id)
    existing = db.scalar(stmt)
    if existing is not None:
        raise APIError(
            "DUPLICATE_NAME",
            "A policy with this provider and name already exists.",
            status.HTTP_409_CONFLICT,
        )


def _request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id") or request.headers.get("x-requestid")


@router.get("", response_model=PolicyPage)
def list_policies(
    provider_key: str | None = Query(default=None, max_length=64),
    mode: str | None = Query(default=None),
    domain: str | None = Query(default=None, max_length=255),
    name: str | None = Query(default=None, max_length=128),
    status_param: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: str | None = Query(default="-created_at"),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyPage:
    errors = ValidationCollector()
    mode_filter = _parse_mode_filter(mode, errors)
    status_filter = _parse_status_filter(status_param, errors)
    order_by = _resolve_sort(sort, errors)
    errors.raise_if_errors()

    normalized_provider = provider_key.strip().lower() if provider_key else None
    domain_filter = domain.strip().lower() if domain else None
    name_filter = name.strip() if name else None

    base_stmt = select(PlatformPolicy)
    base_stmt = _apply_policy_filters(
        base_stmt,
        provider_key=normalized_provider,
        mode=mode_filter,
        domain=domain_filter,
        name=name_filter,
        status_filter=status_filter,
    )

    count_stmt = select(func.count()).select_from(PlatformPolicy)
    count_stmt = _apply_policy_filters(
        count_stmt,
        provider_key=normalized_provider,
        mode=mode_filter,
        domain=domain_filter,
        name=name_filter,
        status_filter=status_filter,
    )

    total = int(db.scalar(count_stmt) or 0)
    offset = (page - 1) * page_size
    items_stmt = base_stmt.order_by(order_by).offset(offset).limit(page_size)
    policies = db.scalars(items_stmt).all()

    return PolicyPage(
        items=[_policy_to_response(policy) for policy in policies],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    request: Request,
    response: Response,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    raw = await request.json()
    if not isinstance(raw, dict):
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"fields": {"body": "payload must be an object."}},
        )

    errors = ValidationCollector()
    payload = _prepare_policy_payload(raw, errors=errors)
    _ensure_provider(db, payload["provider_key"])
    _ensure_unique_name(db, payload["provider_key"], payload["name_normalized"])

    policy = PlatformPolicy(
        provider_key=payload["provider_key"],
        name=payload["name"],
        name_normalized=payload["name_normalized"],
        mode=payload["mode"].value,
        enforcement_mode=payload["enforcement_mode"].value,
        domains_json=payload["domains"],
        domain=",".join(payload["domains"]),
        business_scopes_json=payload["business_scopes"],
        is_enabled=payload["is_enabled"],
        description=payload["description"],
        created_by_user_id=int(me.id),
        updated_by_user_id=int(me.id),
        rate_limit_rps=payload["rate_limit_rps"],
        rate_burst=payload["rate_burst"],
        cooldown_seconds=payload["cooldown_seconds"],
        max_concurrency=payload["max_concurrency"],
        max_entities_per_run=payload["max_entities_per_run"],
        window_cron=payload["window_cron"],
    )
    db.add(policy)
    try:
        db.flush()
    except IntegrityError as exc:
        _handle_integrity_error(exc)

    enforce_provider_policy(
        db,
        provider_key=payload["provider_key"],
        resource_type="admin.platform.policy.write",
        me=me,
        request=request,
        response=response,
        candidate_ids=_candidate_from_business_scopes(payload["business_scopes"]),
        domain=request.headers.get("x-policy-domain"),
        audit_details={"action": "policy.create"},
    )

    db.refresh(policy)
    snapshot = _policy_snapshot(policy)
    log_event(
        db,
        action="policy.create",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        workspace_id=None,
        details={"new": snapshot},
    )
    logger.info(
        "policy.create",
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )
    return _policy_to_response(policy)


@router.put("/{policy_id}", response_model=PolicyResponse)
async def update_policy(
    policy_id: int,
    request: Request,
    response: Response,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    raw = await request.json()
    if not isinstance(raw, dict):
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"fields": {"body": "payload must be an object."}},
        )

    errors = ValidationCollector()
    payload = _prepare_policy_payload(raw, errors=errors)
    _ensure_provider(db, payload["provider_key"])
    _ensure_unique_name(
        db,
        payload["provider_key"],
        payload["name_normalized"],
        exclude_id=int(policy_id),
    )

    enforce_provider_policy(
        db,
        provider_key=payload["provider_key"],
        resource_type="admin.platform.policy.write",
        me=me,
        request=request,
        response=response,
        candidate_ids=_merge_candidates(
            _candidate_from_business_scopes(payload["business_scopes"]),
            _candidate_from_business_scopes(policy.business_scopes_json or {}),
        ),
        domain=request.headers.get("x-policy-domain"),
        audit_details={"action": "policy.update", "policy_id": int(policy_id)},
    )

    old_snapshot = _policy_snapshot(policy)

    policy.provider_key = payload["provider_key"]
    policy.name = payload["name"]
    policy.name_normalized = payload["name_normalized"]
    policy.mode = payload["mode"].value
    policy.enforcement_mode = payload["enforcement_mode"].value
    policy.domains_json = payload["domains"]
    policy.domain = ",".join(payload["domains"])
    policy.business_scopes_json = payload["business_scopes"]
    policy.is_enabled = payload["is_enabled"]
    policy.description = payload["description"]
    policy.rate_limit_rps = payload["rate_limit_rps"]
    policy.rate_burst = payload["rate_burst"]
    policy.cooldown_seconds = payload["cooldown_seconds"]
    policy.max_concurrency = payload["max_concurrency"]
    policy.max_entities_per_run = payload["max_entities_per_run"]
    policy.window_cron = payload["window_cron"]
    policy.updated_by_user_id = int(me.id)

    db.add(policy)
    try:
        db.flush()
    except IntegrityError as exc:
        _handle_integrity_error(exc)

    db.refresh(policy)
    new_snapshot = _policy_snapshot(policy)
    log_event(
        db,
        action="policy.update",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        workspace_id=None,
        details={"old": old_snapshot, "new": new_snapshot},
    )
    logger.info(
        "policy.update",
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )
    return _policy_to_response(policy)


@router.delete("/{policy_id}")
def delete_policy(
    policy_id: int,
    request: Request = None,
    response: Response = None,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    enforce_provider_policy(
        db,
        provider_key=policy.provider_key,
        resource_type="admin.platform.policy.write",
        me=me,
        request=request,
        response=response,
        candidate_ids=_candidate_from_business_scopes(policy.business_scopes_json or {}),
        domain=request.headers.get("x-policy-domain") if request else None,
        audit_details={"action": "policy.delete", "policy_id": int(policy_id)},
    )

    snapshot = _policy_snapshot(policy)
    db.delete(policy)
    db.flush()

    log_event(
        db,
        action="policy.delete",
        resource_type="platform_policy",
        resource_id=int(policy_id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        workspace_id=None,
        details={"old": snapshot},
    )
    logger.info(
        "policy.delete",
        extra={"policy_id": int(policy_id), "request_id": _request_id(request)},
    )
    return {"ok": True}


def _toggle_policy(
    policy_id: int,
    desired_state: bool,
    action_name: str,
    request: Request,
    response: Response,
    me: SessionUser,
    db: Session,
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    enforce_provider_policy(
        db,
        provider_key=policy.provider_key,
        resource_type="admin.platform.policy.toggle",
        me=me,
        request=request,
        response=response,
        candidate_ids=_candidate_from_business_scopes(policy.business_scopes_json or {}),
        domain=request.headers.get("x-policy-domain") if request else None,
        audit_details={"action": action_name, "policy_id": int(policy_id)},
    )

    if bool(policy.is_enabled) == desired_state:
        return _policy_to_response(policy)

    old_snapshot = _policy_snapshot(policy)
    policy.is_enabled = desired_state
    policy.updated_by_user_id = int(me.id)
    db.add(policy)
    db.flush()
    db.refresh(policy)
    new_snapshot = _policy_snapshot(policy)

    log_event(
        db,
        action=action_name,
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        workspace_id=None,
        details={"old": old_snapshot, "new": new_snapshot},
    )
    logger.info(
        action_name,
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )
    return _policy_to_response(policy)


@router.post("/{policy_id}/enable", response_model=PolicyResponse)
def enable_policy(
    policy_id: int,
    request: Request = None,
    response: Response = None,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    return _toggle_policy(policy_id, True, "policy.enable", request, response, me, db)


@router.post("/{policy_id}/disable", response_model=PolicyResponse)
def disable_policy(
    policy_id: int,
    request: Request = None,
    response: Response = None,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    return _toggle_policy(policy_id, False, "policy.disable", request, response, me, db)


@router.post("/{policy_id}/dry-run", response_model=PolicyDryRunResponse)
def dry_run_policy(
    policy_id: int,
    req: PolicyDryRunRequest,
    request: Request,
    response: Response,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyDryRunResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    resource_type = req.resource_type.strip() if req.resource_type else "admin.platform.policy.test"
    if not resource_type:
        resource_type = "admin.platform.policy.test"

    candidates: list[dict[str, str]] = []
    for candidate in req.candidates:
        data = {k: v for k, v in candidate.model_dump().items() if v is not None}
        if not data:
            continue
        if "domain" in data and data["domain"]:
            data["domain"] = data["domain"].strip().lower()
        candidates.append(data)  # type: ignore[arg-type]

    engine = PolicyEngine(db)
    decision = engine.evaluate_policy(
        workspace_id=me.workspace_id,
        provider_key=policy.provider_key,
        resource_type=resource_type,
        candidate_ids=candidates or None,
        now_utc=datetime.now(timezone.utc),
    )

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

    return PolicyDryRunResponse(
        provider_key=policy.provider_key,
        allowed=decision.allowed,
        enforcement_mode=decision.enforcement_mode,
        reason=decision.reason,
        matched_policy_ids=list(decision.matched_policy_ids),
        observed_policy_ids=list(decision.observed_policy_ids),
        limits=PolicyLimitsResponse(
            rate_limit_rps=limits.rate_limit_rps,
            rate_burst=limits.rate_burst,
            cooldown_seconds=limits.cooldown_seconds,
            window_cron=limits.window_cron,
            max_concurrency=limits.max_concurrency,
            max_entities_per_run=limits.max_entities_per_run,
        ),
        trace=_trace_to_response(decision.trace),
    )
