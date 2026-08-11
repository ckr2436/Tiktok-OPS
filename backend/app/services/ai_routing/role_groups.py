from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from app.data.models.ai_routing import AiModelRoute


LEGACY_MANAGED_BY = "content_role_model_group_v1"
MANAGED_BY = "logical_role_model_group_v2"
SUPPORTED_MANAGERS = frozenset((LEGACY_MANAGED_BY, MANAGED_BY))


def _text(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def sync_role_model_group(
    db: Session,
    *,
    role: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize a configured business-role model group as normal routes.

    The gateway already supports many physical routes behind one logical model.
    This function keeps role/model policy out of the request path: operators choose
    ordered source model groups in configuration, while normal route health,
    circuit breaking and failover remain authoritative at runtime.
    """

    role_name = _text(role, field="role")
    roles = policy.get("roles")
    if not isinstance(roles, Mapping) or role_name not in roles:
        raise ValueError(f"routing policy has no role {role_name!r}")
    role_policy = roles[role_name]
    if not isinstance(role_policy, Mapping):
        raise ValueError(f"routing policy role {role_name!r} must be an object")
    logical_model_id = _text(
        role_policy.get("logical_model_id"), field="logical_model_id"
    )
    capability = str(role_policy.get("capability") or "text").strip().lower()
    if capability not in {"text", "multimodal"}:
        raise ValueError("role capability must be text or multimodal")
    sources = role_policy.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"routing policy role {role_name!r} needs sources")

    target_workload = str(role_policy.get("workload") or "default").strip().lower()
    if not target_workload or len(target_workload) > 64:
        raise ValueError("role workload must be between 1 and 64 characters")
    display_name = str(role_policy.get("display_name") or role_name).strip()[:128]
    description = str(role_policy.get("description") or "").strip()[:500]

    desired: dict[
        tuple[int, str, str, str, str],
        tuple[AiModelRoute, int, int],
    ] = {}
    for source_index, source_policy in enumerate(sources, start=1):
        if not isinstance(source_policy, Mapping):
            raise ValueError(f"sources[{source_index}] must be an object")
        source_model = _text(
            source_policy.get("logical_model_id"),
            field=f"sources[{source_index}].logical_model_id",
        )
        if source_model == logical_model_id:
            raise ValueError("role logical model cannot source itself")
        source_workload = str(source_policy.get("workload") or "default").strip().lower()
        tier_priority = int(source_policy.get("priority") or source_index * 1000)
        if tier_priority < 1 or tier_priority > 9000:
            raise ValueError("source priority must be between 1 and 9000")
        source_routes = (
            db.query(AiModelRoute)
            .filter(
                AiModelRoute.logical_model_id == source_model,
                AiModelRoute.workload == source_workload,
                AiModelRoute.capability == capability,
                AiModelRoute.adapter_type == "openai_chat_completions",
                AiModelRoute.is_verified.is_(True),
            )
            .order_by(AiModelRoute.priority.asc(), AiModelRoute.id.asc())
            .all()
        )
        if not source_routes:
            raise ValueError(
                f"no verified {capability} routes for {source_model!r}"
            )
        for source in source_routes:
            identity = (
                int(source.key_id),
                target_workload,
                str(source.provider_model_id),
                str(source.capability),
                str(source.adapter_type),
            )
            priority = min(9999, tier_priority + int(source.priority))
            current = desired.get(identity)
            if current is None or priority < current[1]:
                desired[identity] = (source, priority, tier_priority)

    existing = (
        db.query(AiModelRoute)
        .filter(AiModelRoute.logical_model_id == logical_model_id)
        .all()
    )
    existing_by_identity = {
        (
            int(route.key_id),
            str(route.workload),
            str(route.provider_model_id),
            str(route.capability),
            str(route.adapter_type),
        ): route
        for route in existing
    }
    created = 0
    updated = 0
    desired_ids: set[int] = set()
    for identity, (source, priority, tier_priority) in desired.items():
        target = existing_by_identity.get(identity)
        if target is None:
            target = AiModelRoute(
                key_id=int(source.key_id),
                provider_key=str(source.provider_key),
                workload=target_workload,
                logical_model_id=logical_model_id,
                provider_model_id=str(source.provider_model_id),
                capability=str(source.capability),
                adapter_type=str(source.adapter_type),
            )
            db.add(target)
            created += 1
        else:
            marker = dict(target.config_json or {}).get("managed_by")
            if marker not in (None, *SUPPORTED_MANAGERS):
                raise ValueError(
                    f"logical route {target.id} is not managed by {MANAGED_BY}"
                )
            updated += 1
        existing_config = dict(target.config_json or {})
        operator_priority = existing_config.get("operator_priority")
        try:
            effective_priority = int(operator_priority) if operator_priority is not None else int(priority)
        except (TypeError, ValueError):
            effective_priority = int(priority)
        target.priority = max(1, min(9999, effective_priority))
        target.is_enabled = bool(source.is_enabled)
        target.is_verified = bool(source.is_verified)
        target.health_status = str(source.health_status)
        target.consecutive_failures = int(source.consecutive_failures or 0)
        target.circuit_open_until = source.circuit_open_until
        target.last_success_at = source.last_success_at
        target.last_failure_at = source.last_failure_at
        target.last_error_class = source.last_error_class
        target.last_error_message = source.last_error_message
        target.config_json = {
            "managed_by": MANAGED_BY,
            "role": role_name,
            "display_name": display_name,
            "description": description,
            "source_route_id": int(source.id),
            "source_logical_model_id": str(source.logical_model_id),
            "source_workload": str(source.workload),
            "source_tier_priority": int(tier_priority),
            "source_route_priority": int(source.priority),
            **(
                {"operator_priority": target.priority}
                if operator_priority is not None
                else {}
            ),
        }
        db.flush()
        desired_ids.add(int(target.id))

    disabled = 0
    for target in existing:
        marker = dict(target.config_json or {}).get("managed_by")
        if marker in SUPPORTED_MANAGERS and int(target.id) not in desired_ids:
            target.is_enabled = False
            disabled += 1

    db.commit()
    return {
        "role": role_name,
        "logical_model_id": logical_model_id,
        "capability": capability,
        "workload": target_workload,
        "created": created,
        "updated": updated,
        "disabled": disabled,
        "route_count": len(desired_ids),
    }


def sync_role_policy_file(db: Session, policy_path: str | Path) -> list[dict[str, Any]]:
    path = Path(policy_path)
    policy = json.loads(path.read_text(encoding="utf-8"))
    roles = policy.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        raise ValueError(f"routing policy {path} has no roles")
    return [
        sync_role_model_group(db, role=str(role), policy=policy)
        for role in roles
    ]


def managed_role_groups(db: Session) -> list[dict[str, Any]]:
    """Return effective role routing without exposing credentials or prompts."""

    routes = (
        db.query(AiModelRoute)
        .order_by(
            AiModelRoute.logical_model_id.asc(),
            AiModelRoute.priority.asc(),
            AiModelRoute.id.asc(),
        )
        .all()
    )
    current_roles = {
        str(config.get("role") or route.logical_model_id)
        for route in routes
        for config in (dict(route.config_json or {}),)
        if config.get("managed_by") == MANAGED_BY
    }
    effective_capabilities: dict[str, set[str]] = {}
    for route in routes:
        config = dict(route.config_json or {})
        if (
            config.get("managed_by") == MANAGED_BY
            and bool(route.is_enabled)
            and bool(route.is_verified)
        ):
            role = str(config.get("role") or route.logical_model_id)
            effective_capabilities.setdefault(role, set()).add(
                str(route.capability)
            )
    groups: dict[str, dict[str, Any]] = {}
    for route in routes:
        config = dict(route.config_json or {})
        if config.get("managed_by") not in SUPPORTED_MANAGERS:
            continue
        role = str(config.get("role") or route.logical_model_id)
        # V1 rows remain disabled in the database as audit history after a
        # capability migration.  Once V2 materialization exists, they are not
        # part of the effective group and must not make a multimodal role look
        # like a text role in health/admin output.
        if role in current_roles and config.get("managed_by") != MANAGED_BY:
            continue
        if (
            effective_capabilities.get(role)
            and str(route.capability) not in effective_capabilities[role]
        ):
            continue
        group = groups.setdefault(
            role,
            {
                "role": role,
                "display_name": str(config.get("display_name") or role),
                "description": str(config.get("description") or ""),
                "logical_model_id": route.logical_model_id,
                "workload": route.workload,
                "capability": route.capability,
                "routes": [],
            },
        )
        group["routes"].append(route)
    result: list[dict[str, Any]] = []
    now = datetime.now()
    for group in groups.values():
        provider_order: list[str] = []
        route_items: list[dict[str, Any]] = []
        for route in group["routes"]:
            if route.provider_key not in provider_order:
                provider_order.append(route.provider_key)
            config = dict(route.config_json or {})
            route_items.append({
                "id": int(route.id),
                "provider_key": route.provider_key,
                "provider_model_id": route.provider_model_id,
                "source_logical_model_id": str(
                    config.get("source_logical_model_id") or route.provider_model_id
                ),
                "source_tier_priority": int(config.get("source_tier_priority") or 0),
                "priority": int(route.priority),
                "is_enabled": bool(route.is_enabled),
                "is_verified": bool(route.is_verified),
                "health_status": route.health_status,
                "latency_ema_ms": route.latency_ema_ms,
                "last_success_at": (
                    route.last_success_at.isoformat() if route.last_success_at else None
                ),
                "circuit_open_until": (
                    route.circuit_open_until.isoformat() if route.circuit_open_until else None
                ),
            })
        group["provider_order"] = provider_order
        group["routes"] = route_items
        group["eligible_route_count"] = sum(
            1
            for item in route_items
            if item["is_enabled"]
            and item["is_verified"]
            and not (
                item["circuit_open_until"]
                and datetime.fromisoformat(item["circuit_open_until"]) > now
            )
        )
        group["active_route"] = next(
            (
                item
                for item in route_items
                if item["is_enabled"]
                and item["is_verified"]
                and not (
                    item["circuit_open_until"]
                    and datetime.fromisoformat(item["circuit_open_until"]) > now
                )
            ),
            None,
        )
        result.append(group)
    return sorted(result, key=lambda item: (item["workload"] == "default", item["display_name"]))


def set_role_provider_order(
    db: Session,
    *,
    role: str,
    provider_order: Sequence[str],
) -> dict[str, Any]:
    """Persist an operator-owned provider order inside every model tier."""

    normalized = [str(value).strip().lower() for value in provider_order if str(value).strip()]
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("provider_order must contain unique provider names")
    rows = []
    for route in db.query(AiModelRoute).all():
        config = dict(route.config_json or {})
        if config.get("managed_by") in SUPPORTED_MANAGERS and config.get("role") == role:
            rows.append(route)
    if not rows:
        raise ValueError(f"unknown managed role {role!r}")
    known = {route.provider_key for route in rows}
    if not set(normalized).issubset(known):
        raise ValueError("provider_order contains a provider outside this role")
    remaining = sorted(known - set(normalized))
    effective_order = [*normalized, *remaining]
    provider_rank = {provider: index + 1 for index, provider in enumerate(effective_order)}
    by_tier: dict[int, list[AiModelRoute]] = {}
    for route in rows:
        config = dict(route.config_json or {})
        tier = int(config.get("source_tier_priority") or 1000)
        by_tier.setdefault(tier, []).append(route)
    for tier, tier_rows in by_tier.items():
        per_provider_seen: dict[str, int] = {}
        for route in sorted(tier_rows, key=lambda item: (item.provider_key, item.key_id, item.id)):
            offset = per_provider_seen.get(route.provider_key, 0)
            per_provider_seen[route.provider_key] = offset + 1
            priority = min(9999, tier + provider_rank[route.provider_key] * 10 + offset)
            config = dict(route.config_json or {})
            config["operator_priority"] = priority
            route.priority = priority
            route.config_json = config
            db.add(route)
    db.commit()
    return next(item for item in managed_role_groups(db) if item["role"] == role)


__all__ = [
    "LEGACY_MANAGED_BY",
    "MANAGED_BY",
    "SUPPORTED_MANAGERS",
    "managed_role_groups",
    "set_role_provider_order",
    "sync_role_model_group",
    "sync_role_policy_file",
]
