"""Helpers for querying persisted GMV Max campaigns."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvCampaign


_BLOCKED_SECONDARY_STATUSES = {
    "CAMPAIGN_STATUS_DELETE",
}


def _order_desc_nulls_last(col):
    return [
        case((col.is_(None), 1), else_=0).asc(),
        col.desc(),
    ]


def _allowed_operation_status_clause():
    return or_(
        GmvCampaign.operation_status.is_(None),
        GmvCampaign.operation_status != "DELETE",
    )


def _exclude_blocked_secondary_statuses():
    return or_(
        GmvCampaign.secondary_status.is_(None),
        GmvCampaign.secondary_status.notin_(tuple(_BLOCKED_SECONDARY_STATUSES)),
    )


def list_gmvmax_campaigns(
    db: Session,
    *,
    workspace_id: int,
    advertiser_id: str,
    store_id: str,
    status_filter: Optional[str] = None,
    include_deleted: bool = False,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[GmvCampaign], int]:
    query = (
        db.query(GmvCampaign)
        .filter(GmvCampaign.workspace_id == int(workspace_id))
        .filter(GmvCampaign.advertiser_id == str(advertiser_id))
        .filter(GmvCampaign.store_id == str(store_id))
    )

    if not include_deleted:
        query = query.filter(GmvCampaign.is_deleted.is_(False))
        query = query.filter(_exclude_blocked_secondary_statuses())
        query = query.filter(_allowed_operation_status_clause())

    if status_filter:
        query = query.filter(GmvCampaign.status == status_filter)
    if search:
        pattern = f"%{search}%"
        query = query.filter(GmvCampaign.name.ilike(pattern))

    total = query.count()
    offset = (page - 1) * page_size
    items = (
        query.order_by(*_order_desc_nulls_last(GmvCampaign.ext_created_time))
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return items, total


__all__ = ["list_gmvmax_campaigns"]
