from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


AUTHENTICATED = "authenticated"
AUTH_REQUIRED = "auth_required"
AUTH_UNKNOWN = "unknown"
NETWORK_REACHABLE = "reachable"
NETWORK_REGION_RESTRICTED = "region_restricted"
NETWORK_UNREACHABLE = "unreachable"
NETWORK_UNKNOWN = "unknown"

AUTH_PROBE_INTERVAL = timedelta(hours=6)
AUTH_PROBE_RETRY_INTERVAL = timedelta(minutes=15)
AUTH_FRESHNESS = timedelta(hours=8)


def parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def authentication_state(meta: dict[str, Any]) -> str:
    state = str(meta.get("doubao_auth_state") or AUTH_UNKNOWN).strip().lower()
    return state if state in {AUTHENTICATED, AUTH_REQUIRED, AUTH_UNKNOWN} else AUTH_UNKNOWN


def authentication_is_fresh(
    meta: dict[str, Any], *, now: datetime | None = None
) -> bool:
    if authentication_state(meta) != AUTHENTICATED:
        return False
    checked_at = parse_datetime(meta.get("doubao_auth_checked_at"))
    current = now or datetime.now()
    return bool(checked_at and checked_at + AUTH_FRESHNESS > current)


def mark_auth_probe_result(
    meta: dict[str, Any],
    *,
    state: str,
    network_state: str,
    error_code: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now()
    result = dict(meta)
    normalized_state = (
        state if state in {AUTHENTICATED, AUTH_REQUIRED, AUTH_UNKNOWN} else AUTH_UNKNOWN
    )
    normalized_network = (
        network_state
        if network_state
        in {
            NETWORK_REACHABLE,
            NETWORK_REGION_RESTRICTED,
            NETWORK_UNREACHABLE,
            NETWORK_UNKNOWN,
        }
        else NETWORK_UNKNOWN
    )
    result.update(
        {
            "doubao_auth_state": normalized_state,
            "doubao_auth_checked_at": current.isoformat(),
            "doubao_auth_error": str(error_code or "")[:64] or None,
            "doubao_network_state": normalized_network,
            "doubao_network_checked_at": current.isoformat(),
            "doubao_last_auth_probe_at": current.isoformat(),
            "doubao_next_auth_probe_at": (
                current
                + (
                    AUTH_PROBE_INTERVAL
                    if normalized_state == AUTHENTICATED
                    else AUTH_PROBE_RETRY_INTERVAL
                )
            ).isoformat(),
        }
    )
    return result


def mark_authenticated(
    meta: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    return mark_auth_probe_result(
        meta,
        state=AUTHENTICATED,
        network_state=NETWORK_REACHABLE,
        now=now,
    )


__all__ = [
    "AUTHENTICATED",
    "AUTH_FRESHNESS",
    "AUTH_PROBE_INTERVAL",
    "AUTH_PROBE_RETRY_INTERVAL",
    "AUTH_REQUIRED",
    "AUTH_UNKNOWN",
    "NETWORK_REACHABLE",
    "NETWORK_REGION_RESTRICTED",
    "NETWORK_UNKNOWN",
    "NETWORK_UNREACHABLE",
    "authentication_is_fresh",
    "authentication_state",
    "mark_auth_probe_result",
    "mark_authenticated",
    "parse_datetime",
]
