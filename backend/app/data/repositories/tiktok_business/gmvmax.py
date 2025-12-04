"""Helpers for querying persisted GMV Max campaigns and snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignProduct,
    GmvCampaignSyncSnapshot,
    PromotionTypeEnum,
)


def _normalize_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_field(payload: Mapping[str, Any] | None, *keys: str) -> Any:
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _extract_field_from_sources(keys: Sequence[str], *sources: Mapping[str, Any] | None) -> Any:
    for source in sources:
        value = _extract_field(source, *keys)
        if value is not None:
            return value
    return None


def _normalize_promotion_type(value: Any, *, fallback: PromotionTypeEnum | None = None) -> PromotionTypeEnum:
    normalized = (_normalize_identifier(value) or "PRODUCT").upper()
    if normalized.startswith("LIVE"):
        return PromotionTypeEnum.LIVE
    if normalized == "LIVE":
        return PromotionTypeEnum.LIVE
    if fallback is not None:
        return fallback
    return PromotionTypeEnum.PRODUCT


def _normalize_status_value(value: Any) -> str | None:
    normalized = _normalize_identifier(value)
    if normalized is None:
        return None
    upper = normalized.upper()
    if upper == "ON":
        return "ACTIVE"
    if upper == "OFF":
        return "PAUSED"
    return upper


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed
        except ValueError:
            return None
    return None


def _to_decimal(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_cents(value: Any) -> int | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    return int(decimal_value * 100)


def _collect_product_ids_from_value(source: Any, target: set[str]) -> None:
    if source is None:
        return
    if isinstance(source, (list, tuple, set)):
        for item in source:
            _collect_product_ids_from_value(item, target)
        return
    if isinstance(source, Mapping):
        for key in (
            "item_group_id",
            "itemGroupId",
            "spu_id",
            "spuId",
            "product_id",
            "productId",
            "item_id",
            "itemId",
            "id",
        ):
            normalized = _normalize_identifier(source.get(key))
            if normalized:
                target.add(normalized)
        for nested_key in (
            "item_group_ids",
            "itemGroupIds",
            "item_groups",
            "itemGroupList",
            "item_group_list",
            "item_list",
            "itemList",
            "item_ids",
            "itemIds",
            "item_id_list",
            "itemIdList",
            "product_ids",
            "productIds",
            "product_list",
            "productList",
            "products",
            "items",
        ):
            nested_value = source.get(nested_key)
            if nested_value is not None and nested_value is not source:
                _collect_product_ids_from_value(nested_value, target)
        return
    normalized = _normalize_identifier(source)
    if normalized:
        target.add(normalized)


def _extract_item_group_ids_from_payload(payload: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    collected: set[str] = set()
    for key in (
        "item_group_ids",
        "itemGroupIds",
        "item_groups",
        "itemGroupList",
        "item_group_list",
        "item_list",
        "itemList",
        "item_ids",
        "itemIds",
        "item_id",
        "itemId",
        "product_ids",
        "productIds",
        "product_list",
        "productList",
        "products",
    ):
        value = payload.get(key)
        if value is not None:
            _collect_product_ids_from_value(value, collected)

    nested_campaign = payload.get("campaign")
    if isinstance(nested_campaign, Mapping):
        _collect_product_ids_from_value(nested_campaign, collected)

    sessions = payload.get("sessions") or payload.get("session_list")
    if isinstance(sessions, Mapping):
        _collect_product_ids_from_value(sessions, collected)
    elif isinstance(sessions, (list, tuple, set)):
        for session in sessions:
            if isinstance(session, Mapping):
                _collect_product_ids_from_value(session, collected)

    return sorted(collected)


def _sync_campaign_product_assignments(
    db: Session,
    *,
    campaign: GmvCampaign,
    product_ids: Sequence[str],
    store_id_hint: str | None,
    operation_status: Any,
    promotion_type: PromotionTypeEnum,
) -> None:
    normalized_status = _normalize_status_value(operation_status)
    store_id = _normalize_identifier(store_id_hint)

    campaign_pk = getattr(campaign, "id", None)
    if campaign_pk is None:
        db.flush([campaign])
        campaign_pk = getattr(campaign, "id", None)
    if campaign_pk is None:
        return

    existing_rows = (
        db.query(GmvCampaignProduct)
        .filter(GmvCampaignProduct.campaign_pk == int(campaign_pk))
        .all()
    )
    existing_map = {(row.item_group_id, row.store_id): row for row in existing_rows}

    for product_id in product_ids:
        normalized = _normalize_identifier(product_id)
        if not normalized:
            continue
        key = (normalized, store_id or "")
        row = existing_map.get(key)
        if row is None:
            row = GmvCampaignProduct(
                workspace_id=campaign.workspace_id,
                auth_id=campaign.auth_id,
                campaign_pk=int(campaign_pk),
                campaign_id=campaign.campaign_id,
                item_group_id=normalized,
                store_id=store_id or "",
                promotion_type=promotion_type,
            )
            db.add(row)
        if normalized_status:
            row.operation_status = normalized_status
        row.store_id = store_id or ""

    db.flush()


def create_campaign_snapshot(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    promotion_type: PromotionTypeEnum | None,
    snapshot_type: str,
    payload_json: Mapping[str, Any] | None,
    source: str | None,
    raw_request_id: str | None,
    synced_at: datetime,
) -> None:
    row = {
        "workspace_id": workspace_id,
        "auth_id": auth_id,
        "advertiser_id": str(advertiser_id),
        "store_id": str(store_id or ""),
        "campaign_id": str(campaign_id),
        "promotion_type": promotion_type,
        "snapshot_type": snapshot_type,
        "payload_json": payload_json,
        "source": source,
        "raw_request_id": raw_request_id,
        "synced_at": synced_at,
        "is_deleted": False,
        "deleted_at": None,
    }

    bind = db.get_bind()
    if bind and bind.dialect.name == "sqlite":
        stmt = sqlite_insert(GmvCampaignSyncSnapshot).values(row)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GmvCampaignSyncSnapshot.workspace_id,
                GmvCampaignSyncSnapshot.auth_id,
                GmvCampaignSyncSnapshot.advertiser_id,
                GmvCampaignSyncSnapshot.store_id,
                GmvCampaignSyncSnapshot.campaign_id,
                GmvCampaignSyncSnapshot.snapshot_type,
                GmvCampaignSyncSnapshot.synced_at,
            ],
            set_={
                "promotion_type": stmt.excluded.promotion_type,
                "payload_json": stmt.excluded.payload_json,
                "source": stmt.excluded.source,
                "raw_request_id": stmt.excluded.raw_request_id,
                "is_deleted": stmt.excluded.is_deleted,
                "deleted_at": stmt.excluded.deleted_at,
            },
        )
    else:
        stmt = mysql_insert(GmvCampaignSyncSnapshot).values(row)
        stmt = stmt.on_duplicate_key_update(
            promotion_type=stmt.inserted.promotion_type,
            payload_json=stmt.inserted.payload_json,
            source=stmt.inserted.source,
            raw_request_id=stmt.inserted.raw_request_id,
            is_deleted=stmt.inserted.is_deleted,
            deleted_at=stmt.inserted.deleted_at,
        )
    db.execute(stmt)


def list_campaign_snapshots_for_scope(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    promotion_type: PromotionTypeEnum | None,
    snapshot_type: str,
    synced_at: datetime,
) -> list[GmvCampaignSyncSnapshot]:
    query = (
        db.query(GmvCampaignSyncSnapshot)
        .filter(GmvCampaignSyncSnapshot.workspace_id == int(workspace_id))
        .filter(GmvCampaignSyncSnapshot.auth_id == int(auth_id))
        .filter(GmvCampaignSyncSnapshot.advertiser_id == str(advertiser_id))
        .filter(GmvCampaignSyncSnapshot.store_id == str(store_id))
        .filter(GmvCampaignSyncSnapshot.snapshot_type == snapshot_type)
        .filter(GmvCampaignSyncSnapshot.synced_at == synced_at)
    )
    if promotion_type is not None:
        query = query.filter(GmvCampaignSyncSnapshot.promotion_type == promotion_type)
    return query.all()


def upsert_campaign_from_snapshot(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    payload_json: Mapping[str, Any] | None,
    campaign_id: str,
    promotion_type: PromotionTypeEnum | None,
    allow_revive: bool = True,
) -> GmvCampaign:
    if not campaign_id:
        raise ValueError("campaign_id is required for upsert")

    stmt = (
        select(GmvCampaign)
        .where(GmvCampaign.workspace_id == workspace_id)
        .where(GmvCampaign.auth_id == auth_id)
        .where(GmvCampaign.campaign_id == str(campaign_id))
    )
    existing = db.execute(stmt).scalars().first()

    resolved_promotion_type = _normalize_promotion_type(
        promotion_type,
        fallback=_normalize_promotion_type(
            _extract_field_from_sources(
                (
                    "gmv_max_promotion_type",
                    "promotion_type",
                    "shopping_ads_type",
                ),
                payload_json or {},
            )
        ),
    )

    instance = existing or GmvCampaign(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        campaign_id=str(campaign_id),
        promotion_type=resolved_promotion_type,
    )
    if existing is None:
        db.add(instance)

    instance.auth_id = auth_id
    instance.advertiser_id = str(advertiser_id)
    instance.promotion_type = resolved_promotion_type
    if allow_revive and instance.is_deleted:
        instance.is_deleted = False
        instance.deleted_at = None

    name_value = _extract_field_from_sources(("campaign_name", "name"), payload_json or {})
    instance.name = name_value

    normalized_store = _normalize_identifier(store_id)
    if not normalized_store:
        normalized_store = _normalize_identifier(
            _extract_field_from_sources(("store_id", "shop_id"), payload_json or {})
        )
    instance.store_id = normalized_store or ""

    status_value = _extract_field_from_sources(("status", "campaign_status"), payload_json or {})
    instance.status = _normalize_status_value(status_value)
    instance.operation_status = _extract_field(payload_json, "operation_status")
    instance.secondary_status = _extract_field(payload_json, "secondary_status")

    instance.shopping_ads_type = _extract_field_from_sources(("shopping_ads_type",), payload_json or {})
    instance.optimization_goal = _extract_field_from_sources(("optimization_goal",), payload_json or {})
    instance.bid_type = _extract_field_from_sources(("bid_type",), payload_json or {})
    instance.target_roi_budget = _to_decimal(
        _extract_field_from_sources(("target_roi_budget",), payload_json or {})
    )
    instance.max_delivery_budget = _to_decimal(
        _extract_field_from_sources(("max_delivery_budget",), payload_json or {})
    )

    roas_value = _extract_field_from_sources(("roas_bid", "roi_target"), payload_json or {})
    instance.roas_bid = _to_decimal(roas_value)

    budget_cents_value = _extract_field_from_sources(("daily_budget_cents",), payload_json or {})
    if budget_cents_value is not None:
        instance.daily_budget_cents = _to_int(budget_cents_value)
    else:
        budget_value = _extract_field_from_sources(("daily_budget", "budget"), payload_json or {})
        instance.daily_budget_cents = _to_cents(budget_value)

    currency_value = _extract_field_from_sources(("currency", "budget_currency"), payload_json or {})
    instance.currency = str(currency_value) if currency_value is not None else None

    schedule_type = _extract_field_from_sources(("schedule_type",), payload_json or {})
    instance.schedule_type = schedule_type
    instance.schedule_start_time = _parse_datetime(
        _extract_field_from_sources(("schedule_start_time", "start_time"), payload_json or {})
    )
    instance.schedule_end_time = _parse_datetime(
        _extract_field_from_sources(("schedule_end_time", "end_time"), payload_json or {})
    )

    created_time = _extract_field_from_sources(
        ("create_time", "created_time", "ext_created_time"), payload_json or {}
    )
    updated_time = _extract_field_from_sources(
        ("update_time", "updated_time", "ext_updated_time"), payload_json or {}
    )
    instance.ext_created_time = _parse_datetime(created_time)
    instance.ext_updated_time = _parse_datetime(updated_time)

    if isinstance(payload_json, Mapping):
        instance.raw_json = dict(payload_json)

    product_ids = _extract_item_group_ids_from_payload(payload_json)
    _sync_campaign_product_assignments(
        db,
        campaign=instance,
        product_ids=product_ids,
        store_id_hint=instance.store_id,
        operation_status=_extract_field(payload_json, "operation_status"),
        promotion_type=resolved_promotion_type,
    )

    db.flush()
    return instance


def list_campaign_ids_for_scope(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    promotion_type: PromotionTypeEnum | None,
    include_deleted: bool = False,
) -> set[str]:
    query = (
        db.query(GmvCampaign.campaign_id)
        .filter(GmvCampaign.workspace_id == workspace_id)
        .filter(GmvCampaign.auth_id == auth_id)
        .filter(GmvCampaign.advertiser_id == str(advertiser_id))
        .filter(GmvCampaign.store_id == str(store_id))
    )
    if promotion_type is not None:
        query = query.filter(GmvCampaign.promotion_type == promotion_type)
    if not include_deleted:
        query = query.filter(GmvCampaign.is_deleted.is_(False))

    values = query.distinct().all()
    results: set[str] = set()

    for value in values:
        if isinstance(value, tuple):
            value = value[0]
        if value is not None:
            results.add(str(value))

    return results


def mark_campaigns_deleted_for_scope(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_ids: Iterable[str],
) -> int:
    ids = {str(cid) for cid in campaign_ids if cid}
    if not ids:
        return 0

    stmt = (
        update(GmvCampaign)
        .where(GmvCampaign.workspace_id == workspace_id)
        .where(GmvCampaign.auth_id == auth_id)
        .where(GmvCampaign.advertiser_id == str(advertiser_id))
        .where(GmvCampaign.store_id == str(store_id))
        .where(GmvCampaign.campaign_id.in_(ids))
        .where(GmvCampaign.is_deleted.is_(False))
        .values(
            status="DELETE",
            operation_status="DELETE",
            secondary_status="CAMPAIGN_STATUS_DELETE",
            is_deleted=True,
            deleted_at=datetime.now(timezone.utc),
        )
    )
    result = db.execute(stmt)

    product_cleanup = (
        update(GmvCampaignProduct)
        .where(GmvCampaignProduct.workspace_id == workspace_id)
        .where(GmvCampaignProduct.auth_id == auth_id)
        .where(GmvCampaignProduct.store_id == str(store_id))
        .where(GmvCampaignProduct.campaign_id.in_(ids))
        .values(operation_status="DELETE")
    )
    db.execute(product_cleanup)

    return int(result.rowcount or 0)


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


__all__ = [
    "create_campaign_snapshot",
    "list_campaign_snapshots_for_scope",
    "upsert_campaign_from_snapshot",
    "list_campaign_ids_for_scope",
    "mark_campaigns_deleted_for_scope",
    "list_gmvmax_campaigns",
]
