from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


ROLLOUT_GATE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class VariantRolloutGate:
    """One operator-released batch of video variants.

    The gate is deliberately an execution-control contract, not a creative
    template.  It can release any set of variant indices for any project and
    therefore lets production prove a small batch before widening the
    pipeline without teaching the worker about canaries, genres, products, or
    campaign-specific numbering.
    """

    batch_id: str
    authorized_variant_indices: tuple[int, ...]
    pause_when_complete: bool


def parse_variant_rollout_gate(
    config: dict[str, Any] | None,
    *,
    target_count: int,
) -> VariantRolloutGate | None:
    raw = dict((config or {}).get("variant_rollout_gate") or {})
    if not bool(raw.get("enabled", False)):
        return None

    if str(raw.get("schema_version") or ROLLOUT_GATE_SCHEMA_VERSION) != ROLLOUT_GATE_SCHEMA_VERSION:
        raise ValueError("CONTENT_ROLLOUT_GATE_SCHEMA_UNSUPPORTED")

    target = max(1, int(target_count))
    indices: set[int] = set()
    for value in list(raw.get("authorized_variant_indices") or []):
        try:
            index = int(value)
        except (TypeError, ValueError):
            raise ValueError("CONTENT_ROLLOUT_GATE_VARIANT_INVALID") from None
        if index < 1 or index > target:
            raise ValueError("CONTENT_ROLLOUT_GATE_VARIANT_OUT_OF_RANGE")
        indices.add(index)
    if not indices:
        raise ValueError("CONTENT_ROLLOUT_GATE_AUTHORIZED_VARIANTS_REQUIRED")

    batch_id = str(raw.get("batch_id") or "").strip()
    if not batch_id:
        raise ValueError("CONTENT_ROLLOUT_GATE_BATCH_ID_REQUIRED")

    return VariantRolloutGate(
        batch_id=batch_id[:128],
        authorized_variant_indices=tuple(sorted(indices)),
        pause_when_complete=bool(raw.get("pause_when_complete", True)),
    )


def rollout_variant_authorized(
    config: dict[str, Any] | None,
    *,
    target_count: int,
    variant_index: int,
) -> bool:
    gate = parse_variant_rollout_gate(config, target_count=target_count)
    if gate is None:
        return True
    return int(variant_index) in set(gate.authorized_variant_indices)


def rollout_checkpoint_reached(
    config: dict[str, Any] | None,
    *,
    target_count: int,
    completed_variant_indices: Iterable[int],
) -> VariantRolloutGate | None:
    gate = parse_variant_rollout_gate(config, target_count=target_count)
    if gate is None or not gate.pause_when_complete:
        return None
    completed = {
        int(value)
        for value in completed_variant_indices
        if str(value).strip().isdigit()
    }
    if set(gate.authorized_variant_indices).issubset(completed):
        return gate
    return None


__all__ = [
    "ROLLOUT_GATE_SCHEMA_VERSION",
    "VariantRolloutGate",
    "parse_variant_rollout_gate",
    "rollout_checkpoint_reached",
    "rollout_variant_authorized",
]
