# app/features/platform/kie_ai/routes.py
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_admin, SessionUser
from app.celery_app import celery_app
from app.data.db import get_db
from app.data.models.ai_routing import AiModelRoute
from app.services.ai_routing.discovery import (
    AiModelDiscoveryError,
    discover_models_for_key,
    discover_models_for_provider,
    ensure_builtin_routes,
)
from app.services.ai_routing.overview import model_catalog_page, route_catalog_page, routing_overview
from app.services.ai_routing.router import AiGatewayError, probe_route
from app.services.audit import log_event
from app.services.kie_api.accounts import (
    BANDIANWA_PROVIDER_KEY,
    list_keys,
    get_key_by_id,
    create_kie_key,
    update_kie_key,
    deactivate_kie_key,
    key_model_priorities,
    key_scopes,
    normalize_provider_key,
    normalize_video_model_id,
    provider_catalog,
    provider_model_settings_catalog,
    request_hermes_provider_sync,
    set_provider_model_enabled,
    video_model_routing_catalog,
)

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/api-keys",
    tags=["Platform / API Keys"],
)


class KieKeyOut(BaseModel):
    id: int
    name: str
    provider_key: str
    is_active: bool
    is_default: bool
    scopes: list[str] = Field(default_factory=list)
    model_priorities: dict[str, int] = Field(default_factory=dict)
    provider_label: str = ""
    capabilities: list[str] = Field(default_factory=list)
    hermes_managed: bool = False
    supports_model_discovery: bool = False

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_key(cls, key):
        provider = next(
            (item for item in provider_catalog() if item["id"] == normalize_provider_key(key.provider_key)),
            None,
        ) or {}
        return cls(
            id=int(key.id),
            name=key.name,
            provider_key=key.provider_key,
            is_active=bool(key.is_active),
            is_default=bool(key.is_default),
            scopes=key_scopes(key),
            model_priorities=key_model_priorities(key),
            provider_label=str(provider.get("label") or key.provider_key),
            capabilities=list(provider.get("capabilities") or []),
            hermes_managed=bool(provider.get("hermes_managed")),
            supports_model_discovery=bool(provider.get("supports_model_discovery")),
        )


class KieKeyCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field(..., min_length=1)
    provider_key: str = Field(BANDIANWA_PROVIDER_KEY, min_length=1, max_length=64)
    is_default: bool = Field(False)


class KieKeyUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    api_key: Optional[str] = Field(None, min_length=1)
    provider_key: Optional[str] = Field(None, min_length=1, max_length=64)
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


class ProviderModelSwitchIn(BaseModel):
    is_enabled: bool


class AiRouteUpdateIn(BaseModel):
    priority: int | None = Field(None, ge=1, le=9999)
    is_enabled: bool | None = None
    logical_model_id: str | None = Field(None, min_length=1, max_length=191)
    workload: str | None = Field(None, min_length=1, max_length=64)


class AiRouteProbeIn(BaseModel):
    enable_on_success: bool = False


@router.get("/keys", response_model=List[KieKeyOut])
def list_kie_keys(
    provider_key: Optional[str] = Query(None),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：列出所有 KIE API keys。
    """
    keys = list_keys(db, provider_key=provider_key)
    return [KieKeyOut.from_key(key) for key in keys]


@router.get("/models")
def list_video_models(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return video_model_routing_catalog(db)


@router.get("/providers")
def list_ai_providers(
    _: SessionUser = Depends(require_platform_admin),
) -> list[dict[str, Any]]:
    return provider_catalog()


@router.get("/routing-overview")
def get_routing_overview(
    include_details: bool = Query(True),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return routing_overview(db, include_details=bool(include_details))


@router.get("/catalog-models")
def list_discovered_models(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=191),
    provider_key: str | None = Query(None, max_length=32),
    capability: str | None = Query(None, max_length=32),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return model_catalog_page(
        db,
        page=page,
        page_size=page_size,
        search=search,
        provider_key=provider_key,
        capability=capability,
    )


@router.get("/routes")
def list_ai_routes(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=191),
    provider_key: str | None = Query(None, max_length=32),
    workload: str | None = Query(None, max_length=64),
    capability: str | None = Query(None, max_length=32),
    enabled: bool | None = Query(None),
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return route_catalog_page(
        db,
        page=page,
        page_size=page_size,
        search=search,
        provider_key=provider_key,
        workload=workload,
        capability=capability,
        enabled=enabled,
    )


@router.post("/keys/{key_id}/discover")
async def discover_key_models(
    key_id: int,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    key = get_key_by_id(db, key_id=int(key_id))
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")
    try:
        result = await discover_models_for_key(db, key=key)
    except AiModelDiscoveryError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.commit()
    result.pop("_model_ids", None)
    log_event(
        db,
        action="ai_provider.models.discover",
        resource_type="kie_api_key",
        resource_id=int(key.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        details={"provider_key": key.provider_key, "discovered": int(result.get("discovered") or 0)},
    )
    db.commit()
    return {**result, "ok": True}


@router.post("/discover-all")
async def discover_all_provider_models(
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_builtin_routes(db)
    db.commit()
    results: list[dict[str, Any]] = []
    for provider in provider_catalog():
        if not provider.get("auto_discovery"):
            continue
        try:
            item = await discover_models_for_provider(
                db,
                provider_key=str(provider["id"]),
                automatic_only=True,
            )
            db.commit()
        except AiModelDiscoveryError as exc:
            db.rollback()
            item = {"provider_key": provider["id"], "ok": False, "error": str(exc)}
        else:
            item = {**item, "ok": not bool(item.get("skipped"))}
        results.append(item)
    log_event(
        db,
        action="ai_provider.models.discover_all",
        resource_type="ai_provider",
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        details={"providers": [{"provider_key": item.get("provider_key"), "ok": item.get("ok")} for item in results]},
    )
    db.commit()
    return {"items": results}


@router.patch("/routes/{route_id}")
def update_ai_route(
    route_id: int,
    payload: AiRouteUpdateIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    route = db.get(AiModelRoute, int(route_id))
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    if payload.is_enabled is True and not route.is_verified:
        raise HTTPException(status_code=409, detail="Route must pass a probe before it can be enabled")
    if payload.priority is not None:
        route.priority = int(payload.priority)
    if payload.is_enabled is not None:
        route.is_enabled = bool(payload.is_enabled)
    if payload.logical_model_id is not None:
        route.logical_model_id = payload.logical_model_id.strip()
    if payload.workload is not None:
        route.workload = payload.workload.strip().lower().replace(" ", "_")
    db.add(route)
    log_event(
        db,
        action="ai_route.update",
        resource_type="ai_model_route",
        resource_id=int(route.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        details={
            "priority": int(route.priority),
            "is_enabled": bool(route.is_enabled),
            "logical_model_id": route.logical_model_id,
            "workload": route.workload,
        },
    )
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=409, detail="Route identity conflicts with an existing route") from exc
    return next(item for item in routing_overview(db)["routes"] if item["id"] == int(route.id))


@router.post("/routes/{route_id}/probe")
async def probe_ai_route(
    route_id: int,
    payload: AiRouteProbeIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = await probe_route(
            db,
            route_id=int(route_id),
            enable_on_success=bool(payload.enable_on_success),
        )
    except AiGatewayError as exc:
        raise HTTPException(status_code=int(exc.status_code or 502), detail=str(exc)) from exc
    log_event(
        db,
        action="ai_route.probe",
        resource_type="ai_model_route",
        resource_id=int(route_id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        details={"ok": True, "enabled": bool(result.get("enabled"))},
    )
    db.commit()
    return result


@router.post("/routes/{route_id}/reset-circuit")
def reset_ai_route_circuit(
    route_id: int,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    route = db.get(AiModelRoute, int(route_id))
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    route.circuit_open_until = None
    route.consecutive_failures = 0
    route.health_status = "UNKNOWN" if route.last_success_at is None else "HEALTHY"
    route.last_error_class = None
    route.last_error_message = None
    db.add(route)
    log_event(
        db,
        action="ai_route.circuit.reset",
        resource_type="ai_model_route",
        resource_id=int(route.id),
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        details={"health_status": route.health_status},
    )
    db.commit()
    return next(item for item in routing_overview(db)["routes"] if item["id"] == int(route.id))


@router.get("/provider-models")
def list_provider_model_switches(
    _: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return provider_model_settings_catalog(db)


@router.patch("/provider-models/{provider_key}/{model_id}")
def update_provider_model_switch(
    provider_key: str,
    model_id: str,
    payload: ProviderModelSwitchIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    provider = _require_known_provider(provider_key)
    try:
        set_provider_model_enabled(
            db,
            provider_key=provider,
            model_id=model_id,
            is_enabled=payload.is_enabled,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized_model = normalize_video_model_id(model_id)
    matching_routes = (
        db.query(AiModelRoute)
        .filter(
            AiModelRoute.provider_key == provider,
            AiModelRoute.logical_model_id == normalized_model,
            AiModelRoute.capability == "video",
            AiModelRoute.adapter_type == "legacy_video",
        )
        .all()
    )
    for route in matching_routes:
        route.is_enabled = bool(payload.is_enabled)
        db.add(route)
    db.commit()
    return next(
        item
        for item in provider_model_settings_catalog(db)
        if item["provider_key"] == provider and item["model_id"] == normalize_video_model_id(model_id)
    )


def _require_known_provider(provider_key: str) -> str:
    provider = normalize_provider_key(provider_key)
    if provider not in {item["id"] for item in provider_catalog()}:
        raise HTTPException(status_code=400, detail=f"Unsupported AI provider: {provider}")
    return provider


def _queue_discovery_if_supported(key: Any) -> None:
    provider = next(
        (item for item in provider_catalog() if item["id"] == normalize_provider_key(key.provider_key)),
        {},
    )
    if not key.is_active or not provider.get("auto_discovery"):
        return
    try:
        celery_app.send_task(
            "ai_provider.discover_key",
            kwargs={"key_id": int(key.id)},
            queue="gmv.tasks.default",
        )
    except Exception:
        # Key persistence remains authoritative. The administrator can run the
        # synchronous discovery action if the broker is temporarily down.
        return


@router.post("/keys", response_model=KieKeyOut, status_code=status.HTTP_201_CREATED)
def create_key(
    payload: KieKeyCreateIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：创建 KIE API key。
    """
    try:
        provider = _require_known_provider(payload.provider_key)
        key = create_kie_key(
            db,
            name=payload.name,
            api_key_plaintext=payload.api_key,
            provider_key=provider,
            is_default=payload.is_default,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
        )
        ensure_builtin_routes(db, [key])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(key)
    request_hermes_provider_sync(key.provider_key)
    _queue_discovery_if_supported(key)
    return KieKeyOut.from_key(key)


@router.patch("/keys/{key_id}", response_model=KieKeyOut)
def update_key(
    key_id: int,
    payload: KieKeyUpdateIn,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：更新 KIE API key（名称 / 启用 / 默认 / key 本身）。
    """
    key = get_key_by_id(db, key_id=key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")
    if (
        payload.provider_key is not None
        and normalize_provider_key(payload.provider_key) != normalize_provider_key(key.provider_key)
    ):
        raise HTTPException(
            status_code=409,
            detail="A saved credential cannot be moved to another provider; create a new Key instead",
        )

    try:
        provider = _require_known_provider(payload.provider_key) if payload.provider_key else None
        key = update_kie_key(
            db,
            key=key,
            name=payload.name,
            api_key_plaintext=payload.api_key,
            provider_key=provider,
            is_active=payload.is_active,
            is_default=payload.is_default,
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
        )
        ensure_builtin_routes(db, [key])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(key)
    request_hermes_provider_sync(key.provider_key)
    if payload.api_key is not None or payload.is_active is True:
        _queue_discovery_if_supported(key)
    return KieKeyOut.from_key(key)


@router.delete("/keys/{key_id}", response_model=KieKeyOut)
def deactivate_key(
    key_id: int,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    平台管理员：停用某个 KIE API key（不物理删除）。
    """
    key = get_key_by_id(db, key_id=key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")

    key = deactivate_kie_key(
        db,
        key=key,
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
    )
    db.commit()
    db.refresh(key)
    request_hermes_provider_sync(key.provider_key)
    return KieKeyOut.from_key(key)
