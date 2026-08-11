from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.data.models.ai_routing import AiModelRoute
from app.data.models.kie_api import KieApiKey
from app.services.hermes_agent.content_director import (
    VideoProductionContract,
    production_segment_durations,
)
from app.services.hermes_agent.video_model_capabilities import (
    get_video_model_capability,
)
from app.services.ai_video.accounts import (
    effective_key_model_priority,
    effective_provider_model_capabilities,
    list_model_keys,
    normalize_video_model_id,
    provider_reference_limit,
)


VIDEO_DURATION_PLAN_SCHEMA_VERSION = "provider-model-duration-v4"
CREATIVE_FLEXIBILITY = "creative_flexibility"
CROSS_PROVIDER_PORTABLE = "cross_provider_portable"


def _positive_ints(values: Any) -> list[int]:
    result: list[int] = []
    for raw in list(values or []):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in result:
            result.append(value)
    return sorted(result)


def _route_id(db: Session, key: KieApiKey, model_id: str) -> int | None:
    row = (
        db.query(AiModelRoute)
        .filter(
            AiModelRoute.key_id == int(key.id),
            AiModelRoute.workload == "default",
            AiModelRoute.logical_model_id == normalize_video_model_id(model_id),
            AiModelRoute.capability == "video",
        )
        .order_by(AiModelRoute.id.asc())
        .first()
    )
    return int(row.id) if row is not None else None


def _route_contract(
    db: Session,
    *,
    key: KieApiKey,
    model_id: str,
) -> dict[str, Any]:
    model = get_video_model_capability(model_id)
    capabilities = effective_provider_model_capabilities(db, key, model_id)
    allowed = _positive_ints(capabilities.get("durations"))
    minimum = min(allowed) if allowed else int(model.segment_duration_minimum_seconds)
    maximum = max(allowed) if allowed else int(model.segment_duration_maximum_seconds)
    return {
        "route_id": _route_id(db, key, model_id),
        "key_id": int(key.id),
        "provider_key": str(key.provider_key),
        "priority": int(effective_key_model_priority(db, key, model_id)),
        "allowed_segment_durations_seconds": allowed,
        "segment_duration_minimum_seconds": minimum,
        "segment_duration_maximum_seconds": maximum,
        "reference_image_limit": int(
            provider_reference_limit(key.provider_key, model_id)
        ),
    }


def _production_contract(
    *,
    model_id: str,
    allowed: list[int],
    minimum: int,
    maximum: int,
    reference_image_limit: int,
    reference_video_limit: int,
) -> VideoProductionContract:
    return VideoProductionContract(
        model_id=normalize_video_model_id(model_id),
        segment_duration_minimum_seconds=float(minimum),
        segment_duration_maximum_seconds=float(maximum),
        allowed_segment_durations_seconds=[float(value) for value in allowed],
        reference_image_limit=max(0, int(reference_image_limit)),
        reference_video_limit=max(0, int(reference_video_limit)),
    )


def _legal_totals(
    contract: VideoProductionContract,
    *,
    minimum: int,
    maximum: int,
) -> list[int]:
    result: list[int] = []
    for total in range(int(minimum), int(maximum) + 1):
        try:
            production_segment_durations(contract, total)
        except ValueError:
            continue
        result.append(total)
    return result


def _supports_segments(route: dict[str, Any], segments: list[int]) -> bool:
    allowed = set(route["allowed_segment_durations_seconds"])
    if allowed:
        return all(int(value) in allowed for value in segments)
    minimum = int(route["segment_duration_minimum_seconds"])
    maximum = int(route["segment_duration_maximum_seconds"])
    return all(minimum <= int(value) <= maximum for value in segments)


def build_provider_duration_plan(
    *,
    model_id: str,
    minimum_seconds: int,
    maximum_seconds: int,
    preferred_seconds: int | None,
    preferred_segment_durations_seconds: list[int] | None = None,
    routes: list[dict[str, Any]],
    reference_video_limit: int,
    routing_strategy: str = CREATIVE_FLEXIBILITY,
) -> dict[str, Any]:
    """Choose one stable total and provider-compatible segment topology."""
    if not routes:
        raise ValueError("No enabled compatible video provider route is available")
    minimum = max(1, int(minimum_seconds))
    maximum = max(minimum, int(maximum_seconds))
    desired = (
        int(preferred_seconds)
        if preferred_seconds is not None
        else round((minimum + maximum) / 2)
    )
    desired = max(minimum, min(maximum, desired))
    ordered = sorted(
        [dict(route) for route in routes],
        key=lambda item: (
            int(item.get("priority") or 9999),
            int(item.get("key_id") or 0),
        ),
    )
    primary = ordered[0]

    requested_strategy = str(routing_strategy or CREATIVE_FLEXIBILITY).strip()
    if requested_strategy not in {
        CREATIVE_FLEXIBILITY,
        CROSS_PROVIDER_PORTABLE,
    }:
        raise ValueError(f"unknown video duration routing strategy: {requested_strategy}")
    discrete_sets = [
        set(_positive_ints(route.get("allowed_segment_durations_seconds")))
        for route in ordered
        if _positive_ints(route.get("allowed_segment_durations_seconds"))
    ]
    common_allowed = (
        sorted(set.intersection(*discrete_sets))
        if discrete_sets and len(discrete_sets) == len(ordered)
        else []
    )
    common_reference_limit = min(
        max(0, int(route.get("reference_image_limit") or 0))
        for route in ordered
    )
    if requested_strategy == CROSS_PROVIDER_PORTABLE and common_allowed:
        planning_contract = _production_contract(
            model_id=model_id,
            allowed=common_allowed,
            minimum=min(common_allowed),
            maximum=max(common_allowed),
            reference_image_limit=common_reference_limit,
            reference_video_limit=reference_video_limit,
        )
        legal = _legal_totals(
            planning_contract,
            minimum=minimum,
            maximum=maximum,
        )
        strategy = CROSS_PROVIDER_PORTABLE
    else:
        primary_allowed = _positive_ints(
            primary.get("allowed_segment_durations_seconds")
        )
        planning_contract = _production_contract(
            model_id=model_id,
            allowed=primary_allowed,
            minimum=int(primary["segment_duration_minimum_seconds"]),
            maximum=int(primary["segment_duration_maximum_seconds"]),
            reference_image_limit=int(primary["reference_image_limit"]),
            reference_video_limit=reference_video_limit,
        )
        legal = _legal_totals(
            planning_contract,
            minimum=minimum,
            maximum=maximum,
        )
        strategy = CREATIVE_FLEXIBILITY
    if not legal:
        raise ValueError(
            f"{normalize_video_model_id(model_id)} cannot compose any total "
            f"duration inside {minimum}-{maximum} seconds from enabled "
            "provider capabilities"
        )
    requested_segments = [
        int(value) for value in list(preferred_segment_durations_seconds or [])
    ]
    if any(value <= 0 for value in requested_segments):
        raise ValueError("preferred segment durations must be positive integers")
    if requested_segments:
        normalized = sum(requested_segments)
        if normalized < minimum or normalized > maximum:
            raise ValueError(
                "preferred segment durations must total inside the confirmed "
                f"{minimum}-{maximum} second range"
            )
        allowed = {
            int(round(value))
            for value in planning_contract.allowed_segment_durations_seconds
        }
        if allowed:
            unsupported = [
                value for value in requested_segments if value not in allowed
            ]
        else:
            lower = int(planning_contract.segment_duration_minimum_seconds)
            upper = int(planning_contract.segment_duration_maximum_seconds)
            unsupported = [
                value
                for value in requested_segments
                if value < lower or value > upper
            ]
        if unsupported:
            raise ValueError(
                "preferred segment durations are not supported by the "
                f"confirmed model/provider plan: {unsupported}"
            )
        segments = requested_segments
    else:
        normalized = min(legal, key=lambda value: (abs(value - desired), value))
        segments = [
            int(round(value))
            for value in production_segment_durations(
                planning_contract,
                normalized,
            )
        ]
    compatible = [
        route for route in ordered if _supports_segments(route, segments)
    ]
    if not compatible:
        raise ValueError("planned segment durations have no compatible provider route")
    # Provider capabilities describe what each clip may do; the project plan
    # additionally owns the exact ordered topology selected by the Producer.
    # Carry both in one immutable contract so every downstream Director and
    # compiler validates the same 7+7+6 (or other confirmed) sequence instead
    # of independently recomposing the same total as 10+10.
    planning_contract = VideoProductionContract.model_validate({
        **planning_contract.model_dump(mode="json"),
        "required_segment_durations_seconds": segments,
    })
    contract_payload = planning_contract.model_dump(mode="json")
    capability_payload = {
        "model_id": normalize_video_model_id(model_id),
        "routes": ordered,
        "production_contract": contract_payload,
    }
    capability_sha256 = hashlib.sha256(
        json.dumps(
            capability_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": VIDEO_DURATION_PLAN_SCHEMA_VERSION,
        "model_id": normalize_video_model_id(model_id),
        "requested_range_seconds": {"min": minimum, "max": maximum},
        "preferred_seconds": desired,
        "preferred_segment_durations_seconds": (
            requested_segments or None
        ),
        "normalized_seconds": normalized,
        "normalization_reason": (
            "preferred_segment_topology_supported"
            if requested_segments
            else (
                "preferred_duration_supported"
                if normalized == desired
                else "nearest_provider_compatible_duration"
            )
        ),
        "planning_strategy": strategy,
        "segment_durations_seconds": segments,
        "legal_total_durations_seconds": legal,
        "production_contract": contract_payload,
        "primary_route": {
            key: primary.get(key)
            for key in ("route_id", "key_id", "provider_key", "priority")
        },
        "compatible_routes": [
            {
                key: route.get(key)
                for key in ("route_id", "key_id", "provider_key", "priority")
            }
            for route in compatible
        ],
        "capability_sha256": capability_sha256,
    }


def plan_project_video_duration(
    db: Session,
    *,
    model_id: str,
    minimum_seconds: int,
    maximum_seconds: int,
    preferred_seconds: int | None,
    preferred_segment_durations_seconds: list[int] | None = None,
    reference_count: int,
    reference_video_count: int,
    aspect_ratio: str,
    reference_mode: str,
    resolution: str,
    generation_mode: str,
    routing_strategy: str = CREATIVE_FLEXIBILITY,
) -> dict[str, Any]:
    keys = list_model_keys(
        db,
        model_id=model_id,
        reference_count=max(0, int(reference_count)),
        reference_video_count=max(0, int(reference_video_count)),
        aspect_ratio=aspect_ratio,
        reference_mode=reference_mode,
        resolution=resolution,
        generation_mode=generation_mode,
        require_active=True,
    )
    routes = [
        _route_contract(db, key=key, model_id=model_id)
        for key in keys
    ]
    if not routes:
        # Project authoring must not become unavailable merely because every
        # credential is temporarily cooling down (and isolated tests may not
        # install production credentials at all). Fall back to the model's
        # conservative contract. Runtime still resolves an actual eligible
        # route for every paid segment before submission.
        model = get_video_model_capability(model_id)
        routes = [{
            "route_id": None,
            "key_id": 0,
            "provider_key": str(model.provider_key),
            "priority": 9999,
            "allowed_segment_durations_seconds": _positive_ints(
                model.allowed_segment_durations_seconds
            ),
            "segment_duration_minimum_seconds": int(
                model.segment_duration_minimum_seconds
            ),
            "segment_duration_maximum_seconds": int(
                model.segment_duration_maximum_seconds
            ),
            "reference_image_limit": int(
                model.reference_image_maximum
            ),
        }]
    return build_provider_duration_plan(
        model_id=model_id,
        minimum_seconds=minimum_seconds,
        maximum_seconds=maximum_seconds,
        preferred_seconds=preferred_seconds,
        preferred_segment_durations_seconds=(
            preferred_segment_durations_seconds
        ),
        routes=routes,
        reference_video_limit=1 if int(reference_video_count) > 0 else 0,
        routing_strategy=routing_strategy,
    )


__all__ = [
    "VIDEO_DURATION_PLAN_SCHEMA_VERSION",
    "CREATIVE_FLEXIBILITY",
    "CROSS_PROVIDER_PORTABLE",
    "build_provider_duration_plan",
    "plan_project_video_duration",
]
