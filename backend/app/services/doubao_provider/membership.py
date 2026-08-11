from __future__ import annotations

from typing import Any, Mapping


FREE_TIER = "free"
ENHANCED_TIER = "enhanced"
FREE_DURATIONS = tuple(range(4, 11))
ENHANCED_DURATIONS = tuple(range(4, 16))
MEMBERSHIP_TIERS = frozenset({FREE_TIER, ENHANCED_TIER})


def normalize_membership_tier(value: Any) -> str:
    """Normalize an operator-owned account tier and fail closed to free."""
    raw = str(value or "").strip().lower()
    if raw in {"enhanced", "member", "membership", "pro", "paid"}:
        return ENHANCED_TIER
    return FREE_TIER


def membership_tier(meta: Mapping[str, Any] | None) -> str:
    return normalize_membership_tier(
        dict(meta or {}).get("doubao_membership_tier")
    )


def allowed_durations(meta: Mapping[str, Any] | None) -> list[int]:
    return list(
        ENHANCED_DURATIONS
        if membership_tier(meta) == ENHANCED_TIER
        else FREE_DURATIONS
    )


def max_duration_seconds(meta: Mapping[str, Any] | None) -> int:
    return max(allowed_durations(meta))


def supports_duration(meta: Mapping[str, Any] | None, duration: int) -> bool:
    try:
        requested = int(duration)
    except (TypeError, ValueError):
        return False
    return requested in set(allowed_durations(meta))


def membership_payload(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(meta or {})
    tier = membership_tier(values)
    durations = allowed_durations(values)
    return {
        "tier": tier,
        "label": "加强套餐" if tier == ENHANCED_TIER else "免费账号",
        "source": str(values.get("doubao_membership_source") or "default_free"),
        "allowed_durations_seconds": durations,
        "max_duration_seconds": max(durations),
    }


def set_membership_tier(meta: Mapping[str, Any] | None, tier: str) -> dict[str, Any]:
    raw = str(tier or "").strip().lower()
    if raw not in MEMBERSHIP_TIERS:
        raise ValueError("豆包账号等级仅支持 free 或 enhanced。")
    result = dict(meta or {})
    result["doubao_membership_tier"] = raw
    result["doubao_membership_source"] = "operator"
    return result


__all__ = [
    "ENHANCED_DURATIONS",
    "ENHANCED_TIER",
    "FREE_DURATIONS",
    "FREE_TIER",
    "MEMBERSHIP_TIERS",
    "allowed_durations",
    "max_duration_seconds",
    "membership_payload",
    "membership_tier",
    "normalize_membership_tier",
    "set_membership_tier",
    "supports_duration",
]
