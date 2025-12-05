from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.data.models.gmv_restructured import GmvCampaign, PromotionTypeEnum


def _as_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _as_naive_utc(value)

    text = str(value).strip()
    if not text:
        return None

    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return _as_naive_utc(datetime.strptime(text.replace("Z", "+00:00"), pattern))
        except ValueError:
            continue

    try:
        return _as_naive_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _normalize_promotion_type(value: Any, fallback: PromotionTypeEnum | None = None) -> PromotionTypeEnum:
    if isinstance(value, PromotionTypeEnum):
        return value
    try:
        normalized = str(value).strip().upper()
        if normalized:
            return PromotionTypeEnum(normalized)
    except Exception:
        pass
    return fallback or PromotionTypeEnum.PRODUCT


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text.upper() if text else None


def _to_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        quantized = Decimal(str(value)).scaleb(2).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return int(quantized)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        quantized = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return int(quantized)


def _to_decimal(value: Any, *, quantize: Decimal | None = None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
        if quantize is not None:
            parsed = parsed.quantize(quantize, rounding=ROUND_HALF_UP)
        return parsed
    except (InvalidOperation, ValueError):
        return None


def map_gmvmax_campaign_info_to_model(
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    info: dict[str, Any],
    campaign_id: str | None = None,
    status_value: str | None = None,
    store_id_hint: str | None = None,
    currency_fallback: str | None = None,
    promotion_type_override: PromotionTypeEnum | None = None,
    synced_at: datetime | None = None,
    existing: GmvCampaign | None = None,
) -> GmvCampaign:
    """Map TikTok GMV Max campaign info payloads into a ``GmvCampaign``.

    The function applies the final field semantics agreed for ``gmv_campaigns``
    and centralizes conversions (promotion_type, budget to cents, schedule
    parsing, etc.).  ``synced_at`` is used as a fallback for remote timestamps
    when the API omits them.
    """

    payload = dict(info or {})
    resolved_synced = _as_naive_utc(synced_at) or datetime.now(timezone.utc).replace(tzinfo=None)
    campaign_identifier = campaign_id or payload.get("campaign_id") or payload.get("id")
    if not campaign_identifier:
        raise ValueError("campaign_id is required to map campaign info")

    promotion_raw = payload.get("shopping_ads_type")
    promotion_type = _normalize_promotion_type(promotion_raw, fallback=promotion_type_override)

    instance = existing or GmvCampaign(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        campaign_id=str(campaign_identifier),
        promotion_type=promotion_type,
    )
    if existing is None:
        instance.is_deleted = False
        instance.deleted_at = None
    instance.workspace_id = workspace_id
    instance.auth_id = auth_id
    instance.advertiser_id = str(advertiser_id)
    instance.campaign_id = str(campaign_identifier)
    instance.promotion_type = promotion_type
    instance.shopping_ads_type = promotion_raw if promotion_raw is not None else instance.shopping_ads_type

    instance.name = payload.get("campaign_name") or payload.get("name")
    instance.status = _normalize_status(status_value or payload.get("status") or payload.get("campaign_status"))
    instance.operation_status = payload.get("operation_status")
    instance.optimization_goal = payload.get("optimization_goal")
    instance.bid_type = payload.get("deep_bid_type") or payload.get("bid_type")
    instance.roas_bid = _to_decimal(payload.get("roas_bid"), quantize=Decimal("0.0001"))

    budget_cents = _to_int(payload.get("daily_budget_cents"))
    if budget_cents is None:
        budget_cents = _to_cents(payload.get("daily_budget"))
    if budget_cents is None:
        budget_cents = _to_cents(payload.get("budget"))
    if budget_cents is not None:
        instance.daily_budget_cents = budget_cents
    currency_value = payload.get("currency") or payload.get("budget_currency") or currency_fallback
    instance.currency = str(currency_value) if currency_value is not None else instance.currency

    instance.schedule_type = payload.get("schedule_type")
    instance.schedule_start_time = _parse_datetime(payload.get("schedule_start_time")) or instance.schedule_start_time
    instance.schedule_end_time = _parse_datetime(payload.get("schedule_end_time")) or instance.schedule_end_time

    store_value = store_id_hint or payload.get("store_id") or payload.get("shop_id") or ""
    instance.store_id = str(store_value or "")

    created_time = _parse_datetime(
        payload.get("ext_created_time") or payload.get("create_time") or payload.get("created_time")
    )
    updated_time = _parse_datetime(
        payload.get("ext_updated_time") or payload.get("update_time") or payload.get("updated_time")
    )
    instance.ext_created_time = created_time or instance.ext_created_time or resolved_synced
    instance.ext_updated_time = updated_time or resolved_synced

    instance.raw_json = payload
    instance.is_deleted = False
    instance.deleted_at = None

    return instance


__all__ = ["map_gmvmax_campaign_info_to_model"]
