"""Admin APIs for managing platform policies (v1)."""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.providers import (
    PlatformPolicy,
    PlatformProvider,
    PolicyEnforcementMode,
    PolicyMode,
)
from app.services.audit import log_event
from app.services.providers import catalog as provider_catalog


logger = logging.getLogger("gmv.platform.policies")
router = APIRouter(
    prefix="/api/v1/admin/platform/policies",
    tags=["Admin / Platform Policies"],
)

_DOMAIN_PATTERN = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_ALLOWED_SCOPE_KEYS = {"bc_ids", "advertiser_ids", "shop_ids", "product_ids"}
_ALLOWED_TOP_SCOPE_KEYS = {"include", "exclude"}
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
    description: str | None = None
    created_at: str
    updated_at: str


class PolicyPage(BaseModel):
    items: list[PolicyResponse]
    total: int
    page: int
    page_size: int


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
                status.HTTP_422_UNPROCESSABLE_ENTITY,
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
    }


def _ensure_provider(db: Session, key: str) -> PlatformProvider:
    provider_catalog.sync_registry_with_session(db)
    normalized_key = key.strip().lower()
    registry_keys = {definition.key for definition in provider_catalog.iter_registry_definitions()}
    if normalized_key not in registry_keys:
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"fields": {"provider_key": "provider is not registered."}},
        )

    provider = provider_catalog.get_provider(db, normalized_key)
    if provider is None:
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"fields": {"provider_key": "provider is not configured."}},
        )

    if not bool(provider.is_enabled):
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
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
        "created_at": policy.created_at.isoformat(timespec="microseconds"),
        "updated_at": policy.updated_at.isoformat(timespec="microseconds"),
    }


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
def create_policy(
    req: dict[str, Any] = Body(...),
    request: Request = None,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    if not isinstance(req, dict):
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"fields": {"body": "payload must be an object."}},
        )

    errors = ValidationCollector()
    payload = _prepare_policy_payload(req, errors=errors)
    _ensure_provider(db, payload["provider_key"])

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
    )
    db.add(policy)
    try:
        db.flush()
    except IntegrityError as exc:
        _handle_integrity_error(exc)

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
def update_policy(
    policy_id: int,
    req: dict[str, Any] = Body(...),
    request: Request = None,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    if not isinstance(req, dict):
        raise APIError(
            "VALIDATION_ERROR",
            "Validation failed.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"fields": {"body": "payload must be an object."}},
        )

    errors = ValidationCollector()
    payload = _prepare_policy_payload(req, errors=errors)
    _ensure_provider(db, payload["provider_key"])

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
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

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
    me: SessionUser,
    db: Session,
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

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
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    return _toggle_policy(policy_id, True, "policy.enable", request, me, db)


@router.post("/{policy_id}/disable", response_model=PolicyResponse)
def disable_policy(
    policy_id: int,
    request: Request = None,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    return _toggle_policy(policy_id, False, "policy.disable", request, me, db)
