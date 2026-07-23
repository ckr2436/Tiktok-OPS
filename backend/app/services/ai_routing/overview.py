from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.data.models.ai_routing import AiModelRoute, AiProviderModel, AiRouteAttempt
from app.data.models.kie_api import KieApiKey
from app.services.kie_api.accounts import normalize_provider_key, provider_catalog


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _circuit_active(route: AiModelRoute, now: datetime) -> bool:
    return bool(route.circuit_open_until and route.circuit_open_until > now)


def _serialize_model(model: AiProviderModel) -> dict[str, Any]:
    return {
        "id": int(model.id),
        "provider_key": model.provider_key,
        "provider_model_id": model.provider_model_id,
        "display_name": model.display_name,
        "capabilities": list(model.capabilities_json or []),
        "endpoint_modes": list(model.endpoint_modes_json or []),
        "lifecycle_status": model.lifecycle_status,
        "is_available": bool(model.is_available),
        "first_seen_at": _iso(model.first_seen_at),
        "last_seen_at": _iso(model.last_seen_at),
        "last_verified_at": _iso(model.last_verified_at),
    }


def _serialize_route(route: AiModelRoute, key: KieApiKey, now: datetime) -> dict[str, Any]:
    circuit_active = _circuit_active(route, now)
    effective_health = (
        "CIRCUIT_OPEN"
        if circuit_active
        else "HALF_OPEN"
        if route.health_status == "CIRCUIT_OPEN"
        else route.health_status
    )
    return {
        "id": int(route.id),
        "key_id": int(route.key_id),
        "key_name": key.name,
        "key_active": bool(key.is_active),
        "provider_key": route.provider_key,
        "workload": route.workload,
        "logical_model_id": route.logical_model_id,
        "provider_model_id": route.provider_model_id,
        "capability": route.capability,
        "adapter_type": route.adapter_type,
        "priority": int(route.priority),
        "is_enabled": bool(route.is_enabled),
        "is_verified": bool(route.is_verified),
        "is_eligible": bool(
            key.is_active and route.is_enabled and route.is_verified and not circuit_active
        ),
        "health_status": effective_health,
        "consecutive_failures": int(route.consecutive_failures or 0),
        "total_successes": int(route.total_successes or 0),
        "total_failures": int(route.total_failures or 0),
        "latency_ema_ms": route.latency_ema_ms,
        "circuit_open_until": _iso(route.circuit_open_until),
        "last_success_at": _iso(route.last_success_at),
        "last_failure_at": _iso(route.last_failure_at),
        "last_error_class": route.last_error_class,
        "last_error_message": route.last_error_message,
    }


def model_catalog_page(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    provider_key: str | None = None,
    capability: str | None = None,
) -> dict[str, Any]:
    current_page = max(1, int(page))
    size = max(1, min(200, int(page_size)))
    query = db.query(AiProviderModel)
    if provider_key:
        query = query.filter(AiProviderModel.provider_key == normalize_provider_key(provider_key))
    term = str(search or "").strip()
    if term:
        pattern = f"%{term}%"
        query = query.filter(or_(
            AiProviderModel.provider_model_id.ilike(pattern),
            AiProviderModel.display_name.ilike(pattern),
            AiProviderModel.provider_key.ilike(pattern),
        ))
    query = query.order_by(AiProviderModel.provider_key.asc(), AiProviderModel.provider_model_id.asc())
    requested_capability = str(capability or "").strip().lower()
    if requested_capability:
        filtered = [
            row for row in query.all()
            if requested_capability in set(row.capabilities_json or [])
        ]
        total = len(filtered)
        rows = filtered[(current_page - 1) * size:current_page * size]
    else:
        total = int(query.count())
        rows = query.offset((current_page - 1) * size).limit(size).all()
    return {
        "items": [_serialize_model(row) for row in rows],
        "page": current_page,
        "page_size": size,
        "total": total,
    }


def route_catalog_page(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    provider_key: str | None = None,
    workload: str | None = None,
    capability: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    current_page = max(1, int(page))
    size = max(1, min(200, int(page_size)))
    query = db.query(AiModelRoute, KieApiKey).join(KieApiKey, KieApiKey.id == AiModelRoute.key_id)
    if provider_key:
        query = query.filter(AiModelRoute.provider_key == normalize_provider_key(provider_key))
    if workload:
        query = query.filter(AiModelRoute.workload == str(workload).strip())
    if capability:
        query = query.filter(AiModelRoute.capability == str(capability).strip().lower())
    if enabled is not None:
        query = query.filter(AiModelRoute.is_enabled.is_(bool(enabled)))
    term = str(search or "").strip()
    if term:
        pattern = f"%{term}%"
        query = query.filter(or_(
            AiModelRoute.logical_model_id.ilike(pattern),
            AiModelRoute.provider_model_id.ilike(pattern),
            AiModelRoute.provider_key.ilike(pattern),
            KieApiKey.name.ilike(pattern),
        ))
    total = int(query.count())
    rows = (
        query.order_by(
            AiModelRoute.is_enabled.desc(),
            AiModelRoute.logical_model_id.asc(),
            AiModelRoute.capability.asc(),
            AiModelRoute.workload.asc(),
            AiModelRoute.priority.asc(),
            AiModelRoute.id.asc(),
        )
        .offset((current_page - 1) * size)
        .limit(size)
        .all()
    )
    now = datetime.now()
    return {
        "items": [_serialize_route(route, key, now) for route, key in rows],
        "page": current_page,
        "page_size": size,
        "total": total,
    }


def routing_overview(db: Session, *, include_details: bool = True) -> dict[str, Any]:
    now = datetime.now()
    keys = db.query(KieApiKey).order_by(KieApiKey.id.asc()).all()
    models = (
        db.query(AiProviderModel)
        .order_by(AiProviderModel.provider_key.asc(), AiProviderModel.provider_model_id.asc())
        .all()
    )
    routes = (
        db.query(AiModelRoute, KieApiKey)
        .join(KieApiKey, KieApiKey.id == AiModelRoute.key_id)
        .order_by(
            AiModelRoute.logical_model_id.asc(),
            AiModelRoute.capability.asc(),
            AiModelRoute.workload.asc(),
            AiModelRoute.priority.asc(),
            AiModelRoute.id.asc(),
        )
        .all()
    )
    key_by_provider: dict[str, list[KieApiKey]] = {}
    for key in keys:
        key_by_provider.setdefault(normalize_provider_key(key.provider_key), []).append(key)
    model_by_provider: dict[str, list[AiProviderModel]] = {}
    for model in models:
        model_by_provider.setdefault(model.provider_key, []).append(model)
    route_by_provider: dict[str, list[AiModelRoute]] = {}
    for route, _key in routes:
        route_by_provider.setdefault(route.provider_key, []).append(route)
    providers: list[dict[str, Any]] = []
    for provider in provider_catalog():
        provider_key = str(provider["id"])
        provider_keys = key_by_provider.get(provider_key, [])
        provider_models = model_by_provider.get(provider_key, [])
        provider_routes = route_by_provider.get(provider_key, [])
        healthy = [
            route for route in provider_routes
            if route.is_enabled
            and route.is_verified
            and not _circuit_active(route, now)
            and route.health_status in {"HEALTHY", "UNKNOWN", "DEGRADED"}
        ]
        latest_seen = max((model.last_seen_at for model in provider_models), default=None)
        providers.append({
            **provider,
            "key_count": len(provider_keys),
            "active_key_count": sum(1 for key in provider_keys if key.is_active),
            "discovered_model_count": len(provider_models),
            "available_model_count": sum(1 for model in provider_models if model.is_available),
            "route_count": len(provider_routes),
            "healthy_route_count": len(healthy),
            "circuit_open_count": sum(1 for route in provider_routes if _circuit_active(route, now)),
            "last_discovered_at": _iso(latest_seen),
        })
    serialized_routes = [_serialize_route(route, key, now) for route, key in routes]
    recent_attempts = (
        db.query(AiRouteAttempt, AiModelRoute)
        .join(AiModelRoute, AiModelRoute.id == AiRouteAttempt.route_id)
        .order_by(AiRouteAttempt.id.desc())
        .limit(50)
        .all()
    )
    attempts = [{
        "id": int(attempt.id),
        "request_id": attempt.request_id,
        "route_id": int(attempt.route_id),
        "provider_key": route.provider_key,
        "logical_model_id": route.logical_model_id,
        "provider_model_id": route.provider_model_id,
        "status": attempt.status,
        "error_class": attempt.error_class,
        "upstream_status_code": attempt.upstream_status_code,
        "latency_ms": attempt.latency_ms,
        "created_at": _iso(attempt.created_at),
        "completed_at": _iso(attempt.completed_at),
    } for attempt, route in recent_attempts]
    logical_models = sorted({
        (route.logical_model_id, route.capability, route.workload)
        for route, _key in routes
    })
    key_health: list[dict[str, Any]] = []
    route_items_by_key: dict[int, list[dict[str, Any]]] = {}
    for item in serialized_routes:
        route_items_by_key.setdefault(int(item["key_id"]), []).append(item)
    for key in keys:
        items = route_items_by_key.get(int(key.id), [])
        if not key.is_active:
            health = "DISABLED"
        elif any(item["is_eligible"] and item["health_status"] == "HEALTHY" for item in items):
            health = "HEALTHY"
        elif any(item["health_status"] == "CIRCUIT_OPEN" for item in items):
            health = "CIRCUIT_OPEN"
        elif any(item["health_status"] == "HALF_OPEN" for item in items):
            health = "HALF_OPEN"
        elif any(item["is_eligible"] for item in items):
            health = "DEGRADED"
        else:
            health = "UNKNOWN" if items else "UNCONFIGURED"
        key_health.append({
            "key_id": int(key.id),
            "health_status": health,
            "route_count": len(items),
            "eligible_route_count": sum(1 for item in items if item["is_eligible"]),
        })
    result = {
        "summary": {
            "providers": len(providers),
            "keys": len(keys),
            "active_keys": sum(1 for key in keys if key.is_active),
            "discovered_models": len(models),
            "available_models": sum(1 for model in models if model.is_available),
            "routes": len(routes),
            "enabled_routes": sum(1 for route, _key in routes if route.is_enabled),
            "eligible_routes": sum(1 for item in serialized_routes if item["is_eligible"]),
            "open_circuits": sum(1 for item in serialized_routes if item["health_status"] == "CIRCUIT_OPEN"),
        },
        "providers": providers,
        "key_health": key_health,
        "recent_attempts": attempts,
        "generated_at": now.isoformat(),
    }
    if include_details:
        result["models"] = [_serialize_model(model) for model in models]
        result["routes"] = serialized_routes
        result["logical_models"] = [
            {"logical_model_id": model, "capability": capability, "workload": workload}
            for model, capability, workload in logical_models
        ]
    return result


def attempt_retention_cleanup(db: Session, *, keep_days: int = 30) -> int:
    cutoff = datetime.now().timestamp() - max(1, int(keep_days)) * 86400
    cutoff_dt = datetime.fromtimestamp(cutoff)
    deleted = (
        db.query(AiRouteAttempt)
        .filter(AiRouteAttempt.created_at < cutoff_dt)
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


__all__ = [
    "attempt_retention_cleanup",
    "model_catalog_page",
    "route_catalog_page",
    "routing_overview",
]
