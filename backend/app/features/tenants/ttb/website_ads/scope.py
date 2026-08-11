from __future__ import annotations

from fastapi import HTTPException, status


def resolve_bound_advertiser_id(
    bound_advertiser_id: object,
    requested_advertiser_id: object = None,
) -> str:
    """Return the persisted advertiser and reject caller-supplied overrides."""

    bound = str(bound_advertiser_id or "").strip()
    if not bound:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "WEBSITE_ADS_ADVERTISER_REQUIRED",
                "message": "A persisted Website Ads advertiser binding is required.",
            },
        )

    requested = str(requested_advertiser_id or "").strip()
    if requested and requested != bound:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "WEBSITE_ADS_ADVERTISER_SCOPE_MISMATCH",
                "message": "advertiser_id is outside the bound Website Ads scope.",
            },
        )
    return bound
