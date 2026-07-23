"""Helpers for querying persisted GMV Max campaigns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignProduct,
    PromotionTypeEnum,
)
from app.gmvmax.services.campaign_mapper import map_gmvmax_campaign_info_to_model
from app.services.gmvmax_lifecycle import _derive_campaign_lifecycle


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

    normalized_products = {
        _normalize_identifier(product_id)
        for product_id in product_ids
        if _normalize_identifier(product_id)
    }

    if normalized_products:
        db.execute(
            delete(GmvCampaignProduct)
            .where(GmvCampaignProduct.workspace_id == campaign.workspace_id)
            .where(GmvCampaignProduct.auth_id == campaign.auth_id)
            .where(GmvCampaignProduct.store_id == (store_id or ""))
            .where(GmvCampaignProduct.item_group_id.in_(normalized_products))
        )

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

    lifecycle_status, _ = _derive_campaign_lifecycle(None, "CAMPAIGN_STATUS_DELETE")

    stmt = (
        update(GmvCampaign)
        .where(GmvCampaign.workspace_id == workspace_id)
        .where(GmvCampaign.auth_id == auth_id)
        .where(GmvCampaign.advertiser_id == str(advertiser_id))
        .where(GmvCampaign.store_id == str(store_id))
        .where(GmvCampaign.campaign_id.in_(ids))
        .where(GmvCampaign.is_deleted.is_(False))
        .values(
            status=lifecycle_status,
            lifecycle_status=lifecycle_status,
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
        .values(operation_status="DISABLE")
    )
    db.execute(product_cleanup)

    return int(result.rowcount or 0)


def _order_desc_nulls_last(col):
    return [
        case((col.is_(None), 1), else_=0).asc(),
        col.desc(),
    ]


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
        query = query.filter(GmvCampaign.lifecycle_status != "DELETED")

    if status_filter:
        query = query.filter(GmvCampaign.lifecycle_status == status_filter)
    if search:
        pattern = f"%{search}%"
        query = query.filter(GmvCampaign.name.ilike(pattern))

    total = query.count()
    offset = (page - 1) * page_size
    items = (
        query.order_by(
            *_order_desc_nulls_last(GmvCampaign.ext_created_time),
            GmvCampaign.id.desc(),
        )
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return items, total


__all__ = [
    "list_campaign_ids_for_scope",
    "mark_campaigns_deleted_for_scope",
    "list_gmvmax_campaigns",
]
