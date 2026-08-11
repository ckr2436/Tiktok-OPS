"""Ordering helpers for authoritative GMV Max campaign catalog observations.

``list_synced_at`` and ``detail_synced_at`` are the existing durable ordering
columns for the two official campaign payloads.  They store the instant at
which an official read started (or a successful local mutation completed),
not the later database write time.  This distinction prevents an old response
from winning merely because it arrived last.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def normalize_catalog_observed_at(value: datetime) -> datetime:
    """Return a UTC-naive timestamp suitable for MySQL ``DATETIME(6)``."""

    if not isinstance(value, datetime):
        raise TypeError("source_observed_at must be a datetime")
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def catalog_observation_now() -> datetime:
    """Capture one authoritative observation boundary."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def latest_catalog_observed_at(row: Any) -> datetime | None:
    """Return the newest durable observation recorded on a catalog row."""

    candidates = [
        normalize_catalog_observed_at(value)
        for value in (
            getattr(row, "list_synced_at", None),
            getattr(row, "detail_synced_at", None),
        )
        if isinstance(value, datetime)
    ]
    return max(candidates) if candidates else None


def catalog_response_is_stale(row: Any, source_observed_at: datetime) -> bool:
    """Whether a response does not strictly supersede the current authority.

    Existing authority wins ties.  MySQL stores microseconds, so a sync start
    and a local mutation can legitimately collapse to the same ``DATETIME(6)``
    value even though the mutation completed later.
    """

    current = latest_catalog_observed_at(row)
    incoming = normalize_catalog_observed_at(source_observed_at)
    return current is not None and current >= incoming


def stamp_catalog_row_observation(row: Any, observed_at: datetime) -> datetime:
    """Advance both catalog payload fences after a successful remote mutation."""

    normalized = normalize_catalog_observed_at(observed_at)
    current = latest_catalog_observed_at(row)
    effective = max(current, normalized) if current is not None else normalized
    if hasattr(row, "list_synced_at"):
        row.list_synced_at = effective
    if hasattr(row, "detail_synced_at"):
        row.detail_synced_at = effective
    return effective
