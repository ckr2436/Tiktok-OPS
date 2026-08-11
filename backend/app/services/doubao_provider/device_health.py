from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from app.data.models.hermes_agent import HermesBrowserBridge


DEFAULT_AGENT_HEARTBEAT_TTL_SECONDS = 90
DEFAULT_DEVICE_CIRCUIT_SECONDS = 60


def _bounded_env_seconds(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def parse_local_datetime(value: Any) -> datetime | None:
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


def physical_agent_device_id(row: HermesBrowserBridge) -> str:
    meta = dict(row.meta_json or {})
    return str(
        meta.get("agent_device_id")
        or str(row.device_id or "").split("::slot:", 1)[0]
    ).strip()


def agent_heartbeat_at(row: HermesBrowserBridge) -> datetime | None:
    return parse_local_datetime(
        dict(row.meta_json or {}).get("agent_last_heartbeat_at")
    )


def agent_is_online(
    row: HermesBrowserBridge,
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> bool:
    observed_at = agent_heartbeat_at(row)
    if observed_at is None:
        return False
    current = now or datetime.now().astimezone().replace(tzinfo=None)
    ttl = (
        max(10, int(ttl_seconds))
        if ttl_seconds is not None
        else _bounded_env_seconds(
            "HERMES_BRIDGE_TTL_SECONDS",
            DEFAULT_AGENT_HEARTBEAT_TTL_SECONDS,
            minimum=30,
            maximum=600,
        )
    )
    return observed_at >= current - timedelta(seconds=ttl)


def device_circuit_is_open(
    row: HermesBrowserBridge, *, now: datetime | None = None
) -> bool:
    until = parse_local_datetime(
        dict(row.meta_json or {}).get("doubao_device_circuit_until")
    )
    current = now or datetime.now().astimezone().replace(tzinfo=None)
    return bool(until is not None and until > current)


def device_is_available(
    row: HermesBrowserBridge, *, now: datetime | None = None
) -> bool:
    return agent_is_online(row, now=now) and not device_circuit_is_open(
        row, now=now
    )


def device_circuit_seconds() -> int:
    return _bounded_env_seconds(
        "DOUBAO_DEVICE_CIRCUIT_SECONDS",
        DEFAULT_DEVICE_CIRCUIT_SECONDS,
        minimum=15,
        maximum=300,
    )


__all__ = [
    "DEFAULT_AGENT_HEARTBEAT_TTL_SECONDS",
    "DEFAULT_DEVICE_CIRCUIT_SECONDS",
    "agent_heartbeat_at",
    "agent_is_online",
    "device_circuit_is_open",
    "device_circuit_seconds",
    "device_is_available",
    "parse_local_datetime",
    "physical_agent_device_id",
]
