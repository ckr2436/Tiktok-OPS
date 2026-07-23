from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.data.models.ai_routing import AiModelRoute


MANAGED_BY = "content_role_model_group_v1"


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
    """Materialize a configured content-role model group as normal routes.

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

    desired: dict[tuple[int, str, str, str, str], tuple[AiModelRoute, int]] = {}
    for source_index, source_policy in enumerate(sources, start=1):
        if not isinstance(source_policy, Mapping):
            raise ValueError(f"sources[{source_index}] must be an object")
        source_model = _text(
            source_policy.get("logical_model_id"),
            field=f"sources[{source_index}].logical_model_id",
        )
        if source_model == logical_model_id:
            raise ValueError("role logical model cannot source itself")
        tier_priority = int(source_policy.get("priority") or source_index * 1000)
        if tier_priority < 1 or tier_priority > 9000:
            raise ValueError("source priority must be between 1 and 9000")
        source_routes = (
            db.query(AiModelRoute)
            .filter(
                AiModelRoute.logical_model_id == source_model,
                AiModelRoute.workload == "default",
                AiModelRoute.capability == capability,
                AiModelRoute.adapter_type == "openai_chat_completions",
                AiModelRoute.is_verified.is_(True),
            )
            .order_by(AiModelRoute.priority.asc(), AiModelRoute.id.asc())
            .all()
        )
        if not source_routes:
            raise ValueError(f"no verified text routes for {source_model!r}")
        for source in source_routes:
            identity = (
                int(source.key_id),
                str(source.workload),
                str(source.provider_model_id),
                str(source.capability),
                str(source.adapter_type),
            )
            priority = min(9999, tier_priority + int(source.priority))
            current = desired.get(identity)
            if current is None or priority < current[1]:
                desired[identity] = (source, priority)

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
    for identity, (source, priority) in desired.items():
        target = existing_by_identity.get(identity)
        if target is None:
            target = AiModelRoute(
                key_id=int(source.key_id),
                provider_key=str(source.provider_key),
                workload=str(source.workload),
                logical_model_id=logical_model_id,
                provider_model_id=str(source.provider_model_id),
                capability=str(source.capability),
                adapter_type=str(source.adapter_type),
            )
            db.add(target)
            created += 1
        else:
            marker = dict(target.config_json or {}).get("managed_by")
            if marker not in (None, MANAGED_BY):
                raise ValueError(
                    f"logical route {target.id} is not managed by {MANAGED_BY}"
                )
            updated += 1
        target.priority = int(priority)
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
            "source_route_id": int(source.id),
            "source_logical_model_id": str(source.logical_model_id),
        }
        db.flush()
        desired_ids.add(int(target.id))

    disabled = 0
    for target in existing:
        marker = dict(target.config_json or {}).get("managed_by")
        if marker == MANAGED_BY and int(target.id) not in desired_ids:
            target.is_enabled = False
            disabled += 1

    db.commit()
    return {
        "role": role_name,
        "logical_model_id": logical_model_id,
        "capability": capability,
        "created": created,
        "updated": updated,
        "disabled": disabled,
        "route_count": len(desired_ids),
    }


__all__ = ["MANAGED_BY", "sync_role_model_group"]
