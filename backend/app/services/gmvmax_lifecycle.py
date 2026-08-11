"""Lifecycle mapping utilities for GMV Max campaigns."""

from __future__ import annotations

from typing import Tuple


def _derive_campaign_lifecycle(
    operation_status: str | None,
    secondary_status: str | None,
) -> Tuple[str, bool]:
    """
    Map TikTok GMV Max status to our local lifecycle + is_deleted.

    - secondary_status == 'CAMPAIGN_STATUS_DELETE'      -> ('DELETED', True)
    - secondary_status == 'CAMPAIGN_STATUS_ENABLE'      -> ('ACTIVE', False)
    - all other non-null secondary_status values        -> ('INACTIVE', False)
    - secondary_status is None                          -> ('UNKNOWN', False)
    """

    if secondary_status == "CAMPAIGN_STATUS_DELETE":
        return "DELETED", True
    if secondary_status == "CAMPAIGN_STATUS_ENABLE":
        return "ACTIVE", False
    if secondary_status:
        return "INACTIVE", False
    return "UNKNOWN", False


__all__ = ["_derive_campaign_lifecycle"]
