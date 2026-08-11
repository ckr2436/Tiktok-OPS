"""Canonical TikTok Business GMV Max creative delivery statuses.

TikTok's public ``/gmv_max/report/get/`` response currently uses the
``creative_delivery_status`` metric.  ``NOT_DELIVERYING`` is the spelling in
the official API contract and live response, even though older local code and
fixtures sometimes used more natural variants.

Unknown non-empty values are deliberately preserved.  TikTok can add enum
members before our deployment is updated, and collapsing such a value into a
local candidate status would hide an upstream contract change.
"""

from __future__ import annotations

from typing import Any


OFFICIAL_CREATIVE_DELIVERY_STATUSES = frozenset(
    {
        "IN_QUEUE",
        "LEARNING",
        "DELIVERING",
        "NOT_DELIVERYING",
        "AUTHORIZATION_NEEDED",
        "EXCLUDED",
        "UNAVAILABLE",
        "REJECTED",
        "NOT_ACTIVE",
    }
)

_LEGACY_STATUS_ALIASES = {
    "NOT_DELIVERING": "NOT_DELIVERYING",
    "NOT_DELIVERED": "NOT_DELIVERYING",
}


def canonicalize_creative_delivery_status(value: Any) -> str | None:
    """Return the official enum spelling while preserving future values."""

    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    return _LEGACY_STATUS_ALIASES.get(normalized, normalized)
