"""Admin APIs for platform providers and policies."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.deps import SessionUser, require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.providers import (
    PlatformPolicy,
    PlatformPolicyItem,
    PlatformProvider,
    PolicyDomain,
    PolicyMode,
)
from app.data.models.workspaces import Workspace
from app.services.audit import log_event
from app.services.providers.base import registry


router = APIRouter(prefix="/api/admin", tags=["Platform / Providers"])


class ProviderUpsertRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    is_enabled: bool = True


class ProviderResponse(BaseModel):
    id: int
    key: str
    display_name: str
    is_enabled: bool
    created_at: str
    updated_at: str


def _provider_to_response(provider: PlatformProvider) -> ProviderResponse:
    return ProviderResponse(
        id=int(provider.id),
        key=provider.key,
        display_name=provider.display_name,
        is_enabled=bool(provider.is_enabled),
        created_at=provider.created_at.isoformat(timespec="microseconds"),
        updated_at=provider.updated_at.isoformat(timespec="microseconds"),
    )


@router.get("/providers", response_model=List[ProviderResponse])
def list_providers(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> List[ProviderResponse]:
    providers = db.scalars(select(PlatformProvider).order_by(PlatformProvider.key)).all()
    return [_provider_to_response(p) for p in providers]


@router.post("/providers", response_model=ProviderResponse)
def upsert_provider(
    req: ProviderUpsertRequest,
    http: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> ProviderResponse:
    normalized_key = req.key.strip().lower()
    if not normalized_key:
        raise APIError("INVALID_ARGUMENT", "Provider key must not be empty.", 400)

    try:
        provider_impl = registry.get(normalized_key)
    except KeyError:
        raise APIError("UNKNOWN_PROVIDER", f"Provider '{req.key}' is not registered.", 400)

    provider = db.scalar(select(PlatformProvider).where(PlatformProvider.key == normalized_key))
    created = False
    if provider is None:
        provider = PlatformProvider(
            key=normalized_key,
            display_name=req.display_name or provider_impl.display_name(),
            is_enabled=req.is_enabled,
        )
        created = True
    else:
        if req.display_name is not None:
            provider.display_name = req.display_name
        provider.is_enabled = req.is_enabled

    db.add(provider)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(select(PlatformProvider).where(PlatformProvider.key == normalized_key))
        if existing is None:
            raise APIError("PROVIDER_SAVE_FAILED", "Unable to save provider.", 500) from exc
        provider = existing
        created = False
        if req.display_name is not None:
            provider.display_name = req.display_name
        provider.is_enabled = req.is_enabled
        db.add(provider)
        db.flush()

    log_event(
        db,
        action="platform.provider.create" if created else "platform.provider.update",
        resource_type="platform_provider",
        resource_id=int(provider.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=None,
        details={
            "key": provider.key,
            "display_name": provider.display_name,
            "is_enabled": bool(provider.is_enabled),
        },
    )

    return _provider_to_response(provider)


class PolicyBase(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64)
    workspace_id: int | None = Field(default=None, ge=1)
    mode: PolicyMode
    is_enabled: bool = True
    description: str | None = Field(default=None, max_length=512)


class PolicyCreateRequest(PolicyBase):
    pass


class PolicyUpdateRequest(BaseModel):
    mode: PolicyMode | None = None
    is_enabled: bool | None = None
    description: str | None = Field(default=None, max_length=512)


class PolicyItemRequest(BaseModel):
    domain: PolicyDomain
    item_id: str = Field(min_length=1, max_length=128)


class PolicyItemResponse(BaseModel):
    id: int
    domain: PolicyDomain
    item_id: str


class PolicyResponse(BaseModel):
    id: int
    provider_key: str
    workspace_id: int | None
    mode: PolicyMode
    is_enabled: bool
    description: str | None
    created_by_user_id: int | None
    updated_by_user_id: int | None
    created_at: str
    updated_at: str
    items: List[PolicyItemResponse]


def _normalize_provider_key(key: str) -> str:
    return key.strip().lower()


def _ensure_provider(db: Session, key: str) -> PlatformProvider:
    provider = db.scalar(select(PlatformProvider).where(PlatformProvider.key == key))
    if provider is None:
        raise APIError("PROVIDER_NOT_FOUND", "Provider not found.", 404)
    return provider


def _ensure_workspace(db: Session, workspace_id: int | None) -> None:
    if workspace_id is None:
        return
    ws = db.get(Workspace, int(workspace_id))
    if ws is None:
        raise APIError("WORKSPACE_NOT_FOUND", "Workspace not found.", 404)


def _policy_to_response(policy: PlatformPolicy) -> PolicyResponse:
    return PolicyResponse(
        id=int(policy.id),
        provider_key=policy.provider_key,
        workspace_id=int(policy.workspace_id) if policy.workspace_id is not None else None,
        mode=PolicyMode(policy.mode),
        is_enabled=bool(policy.is_enabled),
        description=policy.description,
        created_by_user_id=int(policy.created_by_user_id) if policy.created_by_user_id is not None else None,
        updated_by_user_id=int(policy.updated_by_user_id) if policy.updated_by_user_id is not None else None,
        created_at=policy.created_at.isoformat(timespec="microseconds"),
        updated_at=policy.updated_at.isoformat(timespec="microseconds"),
        items=[
            PolicyItemResponse(id=int(item.id), domain=PolicyDomain(item.domain), item_id=item.item_id)
            for item in sorted(policy.items, key=lambda x: (x.domain, x.item_id))
        ],
    )


def _policy_query(base: Select[tuple[PlatformPolicy]]) -> Select[tuple[PlatformPolicy]]:
    return base.options(selectinload(PlatformPolicy.items)).order_by(PlatformPolicy.id)


@router.get("/policies", response_model=List[PolicyResponse])
def list_policies(
    provider_key: str | None = Query(default=None, max_length=64),
    workspace_id: int | None = Query(default=None, ge=1),
    mode: PolicyMode | None = Query(default=None),
    is_enabled: bool | None = Query(default=None),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> List[PolicyResponse]:
    stmt = _policy_query(select(PlatformPolicy))

    if provider_key:
        stmt = stmt.where(PlatformPolicy.provider_key == _normalize_provider_key(provider_key))
    if workspace_id is not None:
        stmt = stmt.where(PlatformPolicy.workspace_id == int(workspace_id))
    if mode is not None:
        stmt = stmt.where(PlatformPolicy.mode == mode.value)
    if is_enabled is not None:
        stmt = stmt.where(PlatformPolicy.is_enabled.is_(bool(is_enabled)))

    policies = db.scalars(stmt).all()
    return [_policy_to_response(p) for p in policies]


@router.post("/policies", response_model=PolicyResponse)
def create_policy(
    req: PolicyCreateRequest,
    http: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    provider_key = _normalize_provider_key(req.provider_key)
    _ensure_provider(db, provider_key)
    _ensure_workspace(db, req.workspace_id)

    policy = PlatformPolicy(
        provider_key=provider_key,
        workspace_id=int(req.workspace_id) if req.workspace_id is not None else None,
        mode=req.mode.value,
        is_enabled=req.is_enabled,
        description=req.description,
        created_by_user_id=int(me.id),
        updated_by_user_id=int(me.id),
    )
    db.add(policy)
    db.flush()

    log_event(
        db,
        action="platform.policy.create",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(policy.workspace_id) if policy.workspace_id is not None else None,
        details={
            "provider_key": policy.provider_key,
            "mode": policy.mode,
            "is_enabled": bool(policy.is_enabled),
        },
    )

    db.refresh(policy)
    return _policy_to_response(policy)


@router.patch("/policies/{policy_id}", response_model=PolicyResponse)
def update_policy(
    policy_id: int,
    req: PolicyUpdateRequest,
    http: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", 404)

    if req.mode is not None:
        policy.mode = req.mode.value
    if req.is_enabled is not None:
        policy.is_enabled = req.is_enabled
    if req.description is not None:
        policy.description = req.description

    policy.updated_by_user_id = int(me.id)
    db.add(policy)
    db.flush()

    log_event(
        db,
        action="platform.policy.update",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(policy.workspace_id) if policy.workspace_id is not None else None,
        details={
            "mode": policy.mode,
            "is_enabled": bool(policy.is_enabled),
            "description": policy.description,
        },
    )

    db.refresh(policy)
    return _policy_to_response(policy)


@router.delete("/policies/{policy_id}")
def delete_policy(
    policy_id: int,
    http: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", 404)

    log_event(
        db,
        action="platform.policy.delete",
        resource_type="platform_policy",
        resource_id=int(policy.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(policy.workspace_id) if policy.workspace_id is not None else None,
        details={"provider_key": policy.provider_key},
    )

    db.delete(policy)
    return {"ok": True}


@router.post("/policies/{policy_id}/items", response_model=PolicyItemResponse)
def add_policy_item(
    policy_id: int,
    req: PolicyItemRequest,
    http: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> PolicyItemResponse:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", 404)

    item = PlatformPolicyItem(
        policy_id=int(policy.id),
        domain=req.domain.value,
        item_id=req.item_id,
    )

    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        raise APIError("POLICY_ITEM_EXISTS", "Policy item already exists.", 409) from exc

    policy.updated_by_user_id = int(me.id)
    db.add(policy)

    log_event(
        db,
        action="platform.policy_item.create",
        resource_type="platform_policy_item",
        resource_id=int(item.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(policy.workspace_id) if policy.workspace_id is not None else None,
        details={
            "policy_id": int(policy.id),
            "domain": item.domain,
            "item_id": item.item_id,
        },
    )

    return PolicyItemResponse(id=int(item.id), domain=PolicyDomain(item.domain), item_id=item.item_id)


@router.delete("/policies/{policy_id}/items/{item_id}")
def delete_policy_item(
    policy_id: int,
    item_id: str,
    http: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    policy = db.get(PlatformPolicy, int(policy_id))
    if policy is None:
        raise APIError("POLICY_NOT_FOUND", "Policy not found.", 404)

    item = db.scalar(
        select(PlatformPolicyItem).where(
            PlatformPolicyItem.policy_id == int(policy.id),
            PlatformPolicyItem.item_id == item_id,
        )
    )
    if item is None:
        raise APIError("POLICY_ITEM_NOT_FOUND", "Policy item not found.", 404)

    log_event(
        db,
        action="platform.policy_item.delete",
        resource_type="platform_policy_item",
        resource_id=int(item.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=http.client.host if http.client else None,
        user_agent=http.headers.get("user-agent"),
        workspace_id=int(policy.workspace_id) if policy.workspace_id is not None else None,
        details={
            "policy_id": int(policy.id),
            "item_id": item.item_id,
        },
    )

    db.delete(item)
    policy.updated_by_user_id = int(me.id)
    db.add(policy)
    return {"ok": True}

