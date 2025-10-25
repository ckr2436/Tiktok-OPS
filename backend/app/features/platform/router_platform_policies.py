"""Admin APIs for managing platform policies."""

from __future__ import annotations

import logging
import re
from typing import Any, Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, BeforeValidator
from pydantic_core import PydanticCustomError
from sqlalchemy import Select, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.providers import PlatformPolicy, PlatformProvider, PolicyMode
from app.services.audit import log_event
from app.services.providers import catalog as provider_catalog


logger = logging.getLogger("gmv.platform.policies")
router = APIRouter(prefix="/api/admin/platform/policies", tags=["Platform"])

_DOMAIN_PATTERN = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_ALLOWED_POLICY_MODE_VALUES = [mode.value for mode in PolicyMode]


class ProviderOption(BaseModel):
    key: str = Field(description="Provider registry key")
    name: str = Field(description="Display name")
    is_enabled: bool = Field(description="Whether provider is active")


class PolicyResponse(BaseModel):
    id: int
    provider_key: str
    mode: PolicyMode
    domain: str
    is_enabled: bool
    description: str | None
    created_at: str
    updated_at: str


class PolicyPage(BaseModel):
    items: list[PolicyResponse]
    total: int
    page: int
    page_size: int


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable error code")
    message: str = Field(description="Human readable error message")
    data: dict[str, Any] | None = Field(default=None, description="Optional structured data")


class ErrorResponse(BaseModel):
    error: ErrorDetail


def _coerce_policy_mode(value: Any) -> PolicyMode:
    if isinstance(value, PolicyMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().upper()
        if not normalized:
            raise PydanticCustomError(
                "policy_invalid_mode",
                "Policy mode is required.",
                {"allowed": _ALLOWED_POLICY_MODE_VALUES},
            )
        try:
            return PolicyMode(normalized)
        except ValueError as exc:  # pragma: no cover - defensive
            raise PydanticCustomError(
                "policy_invalid_mode",
                "Invalid policy mode.",
                {"allowed": _ALLOWED_POLICY_MODE_VALUES},
            ) from exc
    raise PydanticCustomError(
        "policy_invalid_mode",
        "Invalid policy mode type.",
        {"allowed": _ALLOWED_POLICY_MODE_VALUES},
    )


def _coerce_optional_policy_mode(value: Any) -> PolicyMode | None:
    if value is None:
        return None
    return _coerce_policy_mode(value)


def _parse_mode_filter(value: Any) -> PolicyMode | None:
    if value is None:
        return None
    if isinstance(value, PolicyMode):
        return value
    if isinstance(value, str):
        normalized = value.strip().upper()
        if not normalized:
            return None
        try:
            return PolicyMode(normalized)
        except ValueError as exc:  # pragma: no cover - defensive
            raise APIError(
                "POLICY_INVALID_MODE",
                "Invalid policy mode. Allowed values: WHITELIST, BLACKLIST.",
                status.HTTP_400_BAD_REQUEST,
            ) from exc
    raise APIError(
        "POLICY_INVALID_MODE",
        "Invalid policy mode. Allowed values: WHITELIST, BLACKLIST.",
        status.HTTP_400_BAD_REQUEST,
    )


PolicyModeInput = Annotated[PolicyMode, BeforeValidator(_coerce_policy_mode)]
PolicyModeOptionalInput = Annotated[PolicyMode | None, BeforeValidator(_coerce_optional_policy_mode)]


class PolicyCreateRequest(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64)
    mode: PolicyModeInput
    domain: str = Field(min_length=1, max_length=255)
    is_enabled: bool = True
    description: str | None = Field(default=None, max_length=512)


class PolicyUpdateRequest(BaseModel):
    mode: PolicyModeOptionalInput = None
    domain: str | None = Field(default=None, min_length=1, max_length=255)
    is_enabled: bool | None = None
    description: str | None = Field(default=None, max_length=512)


class PolicyToggleRequest(BaseModel):
    is_enabled: bool


def _normalize_provider_key(key: str) -> str:
    return key.strip().lower()


def _normalize_domain(domain: str) -> str:
    normalized = domain.strip().lower()
    if not normalized:
        raise APIError("INVALID_DOMAIN", "Domain must not be empty.", status.HTTP_400_BAD_REQUEST)
    if len(normalized) > 255:
        raise APIError("INVALID_DOMAIN", "Domain is too long.", status.HTTP_400_BAD_REQUEST)
    if not _DOMAIN_PATTERN.match(normalized):
        raise APIError(
            "INVALID_DOMAIN",
            "Domain must be a valid hostname (supporting optional wildcard prefix).",
            status.HTTP_400_BAD_REQUEST,
        )
    return normalized


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _ensure_provider(db: Session, key: str) -> PlatformProvider:
    provider_catalog.sync_registry_with_session(db)
    normalized = _normalize_provider_key(key)

    registry_keys = {definition.key for definition in provider_catalog.iter_registry_definitions()}
    if normalized not in registry_keys:
        raise APIError(
            "PROVIDER_NOT_FOUND",
            f"Provider '{key}' is not registered.",
            status.HTTP_400_BAD_REQUEST,
        )

    provider = provider_catalog.get_provider(db, normalized)
    if provider is None:
        raise APIError(
            "PROVIDER_NOT_CONFIGURED",
            f"Provider '{key}' is not configured in the platform.",
            status.HTTP_400_BAD_REQUEST,
        )

    if not bool(provider.is_enabled):
        raise APIError(
            "PROVIDER_DISABLED",
            f"Provider '{key}' is currently disabled.",
            status.HTTP_400_BAD_REQUEST,
        )

    return provider


def _policy_to_response(policy: PlatformPolicy) -> PolicyResponse:
    return PolicyResponse(
        id=int(policy.id),
        provider_key=policy.provider_key,
        mode=PolicyMode(policy.mode),
        domain=str(policy.domain or ""),
        is_enabled=bool(policy.is_enabled),
        description=policy.description,
        created_at=policy.created_at.isoformat(timespec="microseconds"),
        updated_at=policy.updated_at.isoformat(timespec="microseconds"),
    )


def _policy_snapshot(policy: PlatformPolicy) -> dict[str, Any]:
    return {
        "id": int(policy.id),
        "provider_key": policy.provider_key,
        "mode": PolicyMode(policy.mode).value,
        "domain": policy.domain,
        "is_enabled": bool(policy.is_enabled),
        "description": policy.description,
        "created_at": policy.created_at.isoformat(timespec="microseconds"),
        "updated_at": policy.updated_at.isoformat(timespec="microseconds"),
    }


def _request_id(request: Request) -> str | None:
    return request.headers.get("x-request-id") or request.headers.get("x-requestid")


def _apply_policy_filters(
    base: Select[tuple[PlatformPolicy]],
    *,
    provider_key: str | None,
    mode: PolicyMode | None,
    domain: str | None,
    enabled: bool | None,
) -> Select[tuple[PlatformPolicy]]:
    conditions = []
    if provider_key:
        conditions.append(PlatformPolicy.provider_key == provider_key)
    if mode is not None:
        conditions.append(PlatformPolicy.mode == mode.value)
    if domain:
        like_pattern = f"%{domain.lower()}%"
        conditions.append(func.lower(PlatformPolicy.domain).like(like_pattern))
    if enabled is not None:
        conditions.append(PlatformPolicy.is_enabled.is_(enabled))

    if conditions:
        base = base.where(and_(*conditions))
    return base


def _parse_enabled_filter(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "enabled", "yes"}:
        return True
    if normalized in {"false", "0", "disabled", "no"}:
        return False
    raise APIError("INVALID_ENABLED_FILTER", "Invalid enabled filter value.")


@router.get(
    "/providers",
    response_model=list[ProviderOption],
    summary="List registered platform policy providers",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "User is not a platform administrator.",
        }
    },
)
def list_providers(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[ProviderOption]:
    provider_catalog.sync_registry_with_session(db)
    providers = provider_catalog.list_configured_providers(db)
    return [
        ProviderOption(
            key=p.key,
            name=p.display_name,
            is_enabled=bool(p.is_enabled),
        )
        for p in providers
    ]


@router.get(
    "",
    response_model=PolicyPage,
    summary="List platform policies",
)
def list_policies(
    provider_key: str | None = Query(default=None, max_length=64),
    mode: str | None = Query(default=None),
    domain: str | None = Query(default=None, max_length=255),
    enabled: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyPage:
    normalized_key = _normalize_provider_key(provider_key) if provider_key else None
    mode_filter = _parse_mode_filter(mode)
    enabled_filter = _parse_enabled_filter(enabled)
    domain_filter = domain.strip().lower() if domain else None

    base_stmt = select(PlatformPolicy).options(joinedload(PlatformPolicy.provider))
    base_stmt = _apply_policy_filters(
        base_stmt,
        provider_key=normalized_key,
        mode=mode_filter,
        domain=domain_filter,
        enabled=enabled_filter,
    )

    count_stmt = _apply_policy_filters(
        select(func.count()).select_from(PlatformPolicy),
        provider_key=normalized_key,
        mode=mode_filter,
        domain=domain_filter,
        enabled=enabled_filter,
    )
    total = int(db.scalar(count_stmt) or 0)

    offset = (page - 1) * page_size
    items_stmt = base_stmt.order_by(PlatformPolicy.updated_at.desc()).offset(offset).limit(page_size)
    policies = db.scalars(items_stmt).all()

    return PolicyPage(
        items=[_policy_to_response(policy) for policy in policies],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=PolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a platform policy",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponse,
            "description": "Validation failed (unknown provider, invalid domain, duplicate policy, etc.)",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "User is not a platform administrator.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "A policy with the same provider, mode and domain already exists.",
        },
    },
)
def create_policy(
    req: PolicyCreateRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    provider_key = _normalize_provider_key(req.provider_key)
    _ensure_provider(db, provider_key)
    domain = _normalize_domain(req.domain)

    policy = PlatformPolicy(
        provider_key=provider_key,
        mode=req.mode.value,
        domain=domain,
        is_enabled=req.is_enabled,
        description=_normalize_description(req.description),
        created_by_user_id=int(me.id),
        updated_by_user_id=int(me.id),
    )
    db.add(policy)
    try:
        db.flush()
    except IntegrityError as exc:  # pragma: no cover - handled in tests
        raise APIError(
            "POLICY_EXISTS",
            "A policy with the same provider, mode and domain already exists.",
            status.HTTP_409_CONFLICT,
        ) from exc

    db.refresh(policy)
    new_snapshot = _policy_snapshot(policy)
    log_event(
        db,
        action="policy.create",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        workspace_id=None,
        details={"old": None, "new": new_snapshot},
    )
    logger.info(
        "policy.create",
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )
    return _policy_to_response(policy)


@router.patch(
    "/{policy_id}",
    response_model=PolicyResponse,
    summary="Update a platform policy",
)
def update_policy(
    policy_id: int,
    req: PolicyUpdateRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    old_snapshot = _policy_snapshot(policy)

    if req.mode is not None:
        policy.mode = req.mode.value
    if req.domain is not None:
        policy.domain = _normalize_domain(req.domain)
    if req.is_enabled is not None:
        policy.is_enabled = req.is_enabled
    if req.description is not None:
        policy.description = _normalize_description(req.description)

    policy.updated_by_user_id = int(me.id)
    db.add(policy)
    try:
        db.flush()
    except IntegrityError as exc:
        raise APIError(
            "POLICY_EXISTS",
            "A policy with the same provider, mode and domain already exists.",
            status.HTTP_409_CONFLICT,
        ) from exc

    db.refresh(policy)
    new_snapshot = _policy_snapshot(policy)

    log_event(
        db,
        action="policy.update",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        workspace_id=None,
        details={"old": old_snapshot, "new": new_snapshot},
    )
    logger.info(
        "policy.update",
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )
    return _policy_to_response(policy)


@router.post(
    "/{policy_id}/toggle",
    response_model=PolicyResponse,
    summary="Toggle policy enabled state",
)
def toggle_policy(
    policy_id: int,
    req: PolicyToggleRequest,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    if bool(policy.is_enabled) == bool(req.is_enabled):
        return _policy_to_response(policy)

    old_snapshot = _policy_snapshot(policy)
    policy.is_enabled = req.is_enabled
    policy.updated_by_user_id = int(me.id)
    db.add(policy)
    db.flush()
    db.refresh(policy)
    new_snapshot = _policy_snapshot(policy)

    log_event(
        db,
        action="policy.toggle",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        workspace_id=None,
        details={"old": old_snapshot, "new": new_snapshot},
    )
    logger.info(
        "policy.toggle",
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )
    return _policy_to_response(policy)


@router.delete(
    "/{policy_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a platform policy",
)
def delete_policy(
    policy_id: int,
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", status.HTTP_404_NOT_FOUND)

    snapshot = _policy_snapshot(policy)
    log_event(
        db,
        action="policy.delete",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id) if me.workspace_id else None,
        actor_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        workspace_id=None,
        details={"old": snapshot, "new": None},
    )
    logger.info(
        "policy.delete",
        extra={"policy_id": int(policy.id), "request_id": _request_id(request)},
    )

    db.delete(policy)
    db.flush()
    return {"ok": True}
