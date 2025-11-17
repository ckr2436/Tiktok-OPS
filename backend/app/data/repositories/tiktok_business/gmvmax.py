"""Helpers for querying persisted GMV Max campaigns."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import and_, case, or_
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign


_BLOCKED_SECONDARY_STATUSES = {
    "CAMPAIGN_STATUS_DISABLE",
    "CAMPAIGN_STATUS_DELETE",
}


def _order_desc_nulls_last(col):
    return [
        case((col.is_(None), 1), else_=0).asc(),
        col.desc(),
    ]


def _allowed_operation_status_clause():
    enabled = TTBGmvMaxCampaign.operation_status == "ENABLE"
    disabled = and_(
        TTBGmvMaxCampaign.operation_status == "DISABLE",
        or_(
            TTBGmvMaxCampaign.secondary_status.is_(None),
            TTBGmvMaxCampaign.secondary_status != "CAMPAIGN_STATUS_DISABLE",
        ),
    )
    return or_(enabled, disabled)


def _exclude_blocked_secondary_statuses():
    return or_(
        TTBGmvMaxCampaign.secondary_status.is_(None),
        TTBGmvMaxCampaign.secondary_status.notin_(tuple(_BLOCKED_SECONDARY_STATUSES)),
    )


def list_gmvmax_campaigns(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[TTBGmvMaxCampaign], int]:
    query = (
        db.query(TTBGmvMaxCampaign)
        .filter(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
        .filter(TTBGmvMaxCampaign.auth_id == int(auth_id))
        .filter(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))
        .filter(TTBGmvMaxCampaign.store_id == str(store_id))
        .filter(_exclude_blocked_secondary_statuses())
        .filter(_allowed_operation_status_clause())
    )

    if status_filter:
        query = query.filter(TTBGmvMaxCampaign.status == status_filter)
    if search:
        pattern = f"%{search}%"
        query = query.filter(TTBGmvMaxCampaign.name.ilike(pattern))

    total = query.count()
    offset = (page - 1) * page_size
    items = (
        query.order_by(*_order_desc_nulls_last(TTBGmvMaxCampaign.ext_created_time))
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return items, total


__all__ = ["list_gmvmax_campaigns"]
