from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating


def _find_cached_instance(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    creative_id: str,
    promotion_type: str,
) -> TTBGmvMaxCreativeHeating | None:
    for obj in db.identity_map.values():
        if not isinstance(obj, TTBGmvMaxCreativeHeating):
            continue
        if (
            obj.workspace_id == workspace_id
            and obj.auth_id == auth_id
            and obj.campaign_id == campaign_id
            and obj.creative_id == creative_id
            and getattr(obj.promotion_type, "value", str(obj.promotion_type)) == promotion_type
        ):
            return obj
    for obj in list(db.new):
        if not isinstance(obj, TTBGmvMaxCreativeHeating):
            continue
        if (
            obj.workspace_id == workspace_id
            and obj.auth_id == auth_id
            and obj.campaign_id == campaign_id
            and obj.creative_id == creative_id
            and getattr(obj.promotion_type, "value", str(obj.promotion_type)) == promotion_type
        ):
            return obj
    return None


async def upsert_creative_heating(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    creative_id: str,
    advertiser_id: str,
    promotion_type: str,
    mode: str | None = None,
    target_daily_budget: Any | None = None,
    budget_delta: Any | None = None,
    currency: str | None = None,
    max_duration_minutes: int | None = None,
    note: str | None = None,
    creative_name: str | None = None,
    product_id: str | None = None,
    item_id: str | None = None,
    evaluation_window_minutes: int | None = None,
    min_clicks: int | None = None,
    min_ctr: Any | None = None,
    min_gross_revenue: Any | None = None,
    auto_stop_enabled: bool | None = None,
) -> TTBGmvMaxCreativeHeating:
    campaign_key = str(campaign_id)
    creative_key = str(creative_id)
    advertiser_key = str(advertiser_id)
    promotion_type_key = str(promotion_type)

    instance = _find_cached_instance(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_key,
        creative_id=creative_key,
        promotion_type=promotion_type_key,
    )
    if instance is None:
        stmt: Select[TTBGmvMaxCreativeHeating] = (
            select(TTBGmvMaxCreativeHeating)
            .where(TTBGmvMaxCreativeHeating.workspace_id == workspace_id)
            .where(TTBGmvMaxCreativeHeating.auth_id == auth_id)
            .where(TTBGmvMaxCreativeHeating.campaign_id == campaign_key)
            .where(TTBGmvMaxCreativeHeating.creative_id == creative_key)
            .where(TTBGmvMaxCreativeHeating.promotion_type == promotion_type_key)
        )
        instance = db.execute(stmt).scalars().first()
    is_new = instance is None
    if instance is None:
        instance = TTBGmvMaxCreativeHeating(
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_key,
            creative_id=creative_key,
            advertiser_id=advertiser_key,
            promotion_type=promotion_type_key,
        )
        db.add(instance)
    else:
        if str(instance.advertiser_id) != advertiser_key:
            raise ValueError(
                "creative heating config belongs to a different advertiser scope"
            )
        instance.campaign_id = campaign_key
        instance.creative_id = creative_key
        instance.promotion_type = promotion_type_key

    # Partial actions (especially STOP) must not erase the identifiers and
    # reusable boost settings persisted by the original start action.  Only
    # overwrite optional fields that were actually supplied.
    for field_name, field_value in (
        ("mode", mode),
        ("target_daily_budget", target_daily_budget),
        ("budget_delta", budget_delta),
        ("currency", currency),
        ("max_duration_minutes", max_duration_minutes),
        ("note", note),
        ("creative_name", creative_name),
        ("product_id", product_id),
        ("item_group_id", product_id),
        ("item_id", item_id),
    ):
        if is_new or field_value is not None:
            setattr(instance, field_name, field_value)

    if evaluation_window_minutes is not None:
        instance.evaluation_window_minutes = int(evaluation_window_minutes)
    elif instance.evaluation_window_minutes is None:
        instance.evaluation_window_minutes = 60

    if min_clicks is not None:
        instance.min_clicks = int(min_clicks)

    if min_ctr is not None:
        instance.min_ctr = min_ctr

    if min_gross_revenue is not None:
        instance.min_gross_revenue = min_gross_revenue

    if auto_stop_enabled is not None:
        instance.auto_stop_enabled = bool(auto_stop_enabled)
    elif instance.auto_stop_enabled is None:
        instance.auto_stop_enabled = True

    if instance.is_heating_active is None:
        instance.is_heating_active = False

    return instance


async def update_heating_action_result(
    db: Session,
    *,
    heating_id: int,
    status: str,
    action_type: str,
    action_time: datetime,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> TTBGmvMaxCreativeHeating:
    instance = db.get(TTBGmvMaxCreativeHeating, heating_id)
    if instance is None:
        raise ValueError(f"heating config {heating_id} not found")

    instance.status = status
    instance.last_action_type = action_type
    instance.last_action_at = action_time
    instance.last_action_request = dict(request_payload) if request_payload else None
    instance.last_action_response = dict(response_payload) if response_payload else None
    instance.last_error = error_message

    normalized_type = (action_type or "").upper()
    normalized_status = (status or "").upper()
    if normalized_type == "APPLY_BOOST" and normalized_status == "APPLIED":
        instance.is_heating_active = True
    elif normalized_type in {
        "STOP_CREATIVE",
        "STOP_BOOST",
        "STOP_HEATING",
        "REMOVE_CREATIVE",
        "EXCLUDE_CREATIVE",
    }:
        if normalized_status in {"APPLIED", "CANCELLED"}:
            instance.is_heating_active = False
        if normalized_status in {"APPLIED", "EXCLUDED"}:
            instance.is_heating_active = False
            instance.auto_stop_enabled = False
    elif normalized_type in {"ADD_BACK_CREATIVE", "RESTORE_CREATIVE"}:
        if normalized_status in {"APPLIED", "AVAILABLE", "IDLE"}:
            instance.is_heating_active = False
            instance.auto_stop_enabled = True

    db.add(instance)
    return instance


async def update_heating_evaluation(
    db: Session,
    *,
    heating_id: int,
    evaluated_at: datetime,
    evaluation_result: str,
    is_heating_active: bool | None = None,
) -> TTBGmvMaxCreativeHeating:
    instance = db.get(TTBGmvMaxCreativeHeating, heating_id)
    if instance is None:
        raise ValueError(f"heating config {heating_id} not found")

    instance.last_evaluated_at = evaluated_at
    instance.last_evaluation_result = evaluation_result
    if is_heating_active is not None:
        instance.is_heating_active = bool(is_heating_active)

    db.add(instance)
    return instance


def _apply_required_filters(
    *,
    query: Select[TTBGmvMaxCreativeHeating],
    workspace_id: int,
    provider: str,
    auth_id: int,
) -> Select[TTBGmvMaxCreativeHeating]:
    del provider
    return (
        query.where(TTBGmvMaxCreativeHeating.workspace_id == workspace_id)
        .where(TTBGmvMaxCreativeHeating.auth_id == auth_id)
    )


async def list_heating_configs(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str | None = None,
    status: str | None = None,
    creative_ids: Iterable[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[TTBGmvMaxCreativeHeating]:
    db.flush()
    query: Select[TTBGmvMaxCreativeHeating] = select(TTBGmvMaxCreativeHeating)
    query = _apply_required_filters(
        query=query, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )

    if campaign_id is not None:
        query = query.where(TTBGmvMaxCreativeHeating.campaign_id == str(campaign_id))

    if status is not None:
        query = query.where(TTBGmvMaxCreativeHeating.status == str(status))

    if creative_ids:
        query = query.where(
            TTBGmvMaxCreativeHeating.creative_id.in_([str(cid) for cid in creative_ids])
        )

    query = query.order_by(
        TTBGmvMaxCreativeHeating.created_at.desc(),
        TTBGmvMaxCreativeHeating.id.desc(),
    )
    if limit is not None:
        query = query.limit(int(limit))
    if offset:
        query = query.offset(int(offset))

    return list(db.execute(query).scalars().all())


async def list_active_heating_configs(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str | None = None,
) -> list[TTBGmvMaxCreativeHeating]:
    db.flush()
    query: Select[TTBGmvMaxCreativeHeating] = select(TTBGmvMaxCreativeHeating)
    query = _apply_required_filters(
        query=query, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )
    query = query.where(TTBGmvMaxCreativeHeating.auto_stop_enabled.is_(True))
    query = query.where(TTBGmvMaxCreativeHeating.is_heating_active.is_(True))

    if campaign_id is not None:
        query = query.where(TTBGmvMaxCreativeHeating.campaign_id == str(campaign_id))

    query = query.order_by(
        TTBGmvMaxCreativeHeating.created_at.asc(),
        TTBGmvMaxCreativeHeating.id.asc(),
    )
    return list(db.execute(query).scalars().all())


async def get_heating_for_creative(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    creative_id: str,
) -> TTBGmvMaxCreativeHeating | None:
    db.flush()
    query: Select[TTBGmvMaxCreativeHeating] = select(TTBGmvMaxCreativeHeating)
    query = _apply_required_filters(
        query=query, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )
    query = (
        query.where(TTBGmvMaxCreativeHeating.campaign_id == str(campaign_id))
        .where(TTBGmvMaxCreativeHeating.creative_id == str(creative_id))
        .limit(1)
    )

    return db.execute(query).scalars().first()
