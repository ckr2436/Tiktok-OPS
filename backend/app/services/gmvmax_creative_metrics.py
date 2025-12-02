from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.data.models.ttb_gmvmax_creative_metrics_10min import (
    TTBGmvMaxCreativeMetrics10Min,
)
from app.providers.tiktok_business.gmvmax_client import TikTokBusinessGMVMaxClient
from app.services.gmvmax_spec import GMVMaxReportLevel
from app.services.ttb_gmvmax import (
    get_item_group_ids_for_campaign,
    fetch_gmvmax_report_by_level,
)

logger = logging.getLogger("gmv.services.gmvmax.creative_metrics")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):  # noqa: BLE001
        return None


def _to_cents(value: Any) -> int | None:
    dec = _to_decimal(value)
    if dec is None:
        return None
    try:
        return int((dec * 100).to_integral_value())
    except Exception:  # noqa: BLE001
        return None


def _floor_snapshot(now: datetime | None = None) -> datetime:
    current = now or datetime.utcnow()
    floored_minute = (current.minute // 10) * 10
    return current.replace(minute=floored_minute, second=0, microsecond=0)


def _serialize_payload(metrics: Mapping[str, Any], dimensions: Mapping[str, Any]) -> dict[str, Any]:
    payload = {"metrics": dict(metrics), "dimensions": dict(dimensions)}
    return payload


async def sync_creative_metrics_10min_for_campaign(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    campaign: TTBGmvMaxCampaign,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    item_group_ids = get_item_group_ids_for_campaign(session, campaign=campaign)
    if not item_group_ids:
        logger.warning(
            "gmvmax creative metrics missing item groups",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "campaign_id": campaign.campaign_id,
                "store_id": campaign.store_id,
            },
        )
        return {"rows": 0}

    entries = await fetch_gmvmax_report_by_level(
        client,
        advertiser_id=advertiser_id,
        store_id=str(campaign.store_id or ""),
        campaign_id=str(campaign.campaign_id),
        campaign_ids=[str(campaign.campaign_id)],
        level=GMVMaxReportLevel.CREATIVE.value,
        start_date=start_date,
        end_date=end_date,
        item_group_ids=item_group_ids,
    )

    if not entries:
        return {"rows": 0}

    snapshot_at = _floor_snapshot()
    rows_written = 0
    for entry in entries:
        metrics = entry.get("metrics") or {}
        dimensions = entry.get("dimensions") or {}
        creative_id = str(dimensions.get("shop_content_id") or dimensions.get("creative_id") or "").strip()
        if not creative_id:
            continue

        stat_time_day_raw = dimensions.get("stat_time_day") or dimensions.get("date")
        try:
            stat_time_day = (
                stat_time_day_raw if isinstance(stat_time_day_raw, date) else date.fromisoformat(str(stat_time_day_raw))
            )
        except Exception:  # noqa: BLE001
            logger.warning("gmvmax creative metrics missing stat_time_day", extra={"dimensions": dimensions})
            continue

        payload = _serialize_payload(metrics, dimensions)
        insert_stmt = insert(TTBGmvMaxCreativeMetrics10Min).values(
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            campaign_id=str(campaign.campaign_id),
            store_id=str(campaign.store_id or ""),
            product_id=str(dimensions.get("product_id") or dimensions.get("item_group_id") or ""),
            creative_id=creative_id,
            stat_time_day=stat_time_day,
            snapshot_at=snapshot_at,
            product_impressions=_to_int(metrics.get("product_impressions")),
            product_clicks=_to_int(metrics.get("product_clicks")),
            product_click_rate=_to_decimal(metrics.get("product_click_rate")),
            ad_click_rate=_to_decimal(metrics.get("ad_click_rate")),
            ad_conversion_rate=_to_decimal(metrics.get("ad_conversion_rate")),
            ad_video_view_rate_p25=_to_decimal(metrics.get("ad_video_view_rate_p25")),
            ad_video_view_rate_p50=_to_decimal(metrics.get("ad_video_view_rate_p50")),
            ad_video_view_rate_p75=_to_decimal(metrics.get("ad_video_view_rate_p75")),
            ad_video_view_rate_p100=_to_decimal(metrics.get("ad_video_view_rate_p100")),
            ad_video_view_rate_2s=_to_decimal(metrics.get("ad_video_view_rate_2s")),
            ad_video_view_rate_6s=_to_decimal(metrics.get("ad_video_view_rate_6s")),
            impressions=_to_int(metrics.get("impressions")),
            clicks=_to_int(metrics.get("clicks")),
            orders=_to_int(metrics.get("orders")),
            cost_cents=_to_cents(metrics.get("cost")),
            net_cost_cents=_to_cents(metrics.get("net_cost")),
            cost_per_order_cents=_to_cents(metrics.get("cost_per_order")),
            gross_revenue_cents=_to_cents(metrics.get("gross_revenue")),
            roi=_to_decimal(metrics.get("roi")),
            creative_delivery_status=metrics.get("creative_delivery_status"),
            raw_metrics=payload,
        )

        update_stmt = insert_stmt.on_duplicate_key_update(
            product_impressions=insert_stmt.inserted.product_impressions,
            product_clicks=insert_stmt.inserted.product_clicks,
            product_click_rate=insert_stmt.inserted.product_click_rate,
            ad_click_rate=insert_stmt.inserted.ad_click_rate,
            ad_conversion_rate=insert_stmt.inserted.ad_conversion_rate,
            ad_video_view_rate_p25=insert_stmt.inserted.ad_video_view_rate_p25,
            ad_video_view_rate_p50=insert_stmt.inserted.ad_video_view_rate_p50,
            ad_video_view_rate_p75=insert_stmt.inserted.ad_video_view_rate_p75,
            ad_video_view_rate_p100=insert_stmt.inserted.ad_video_view_rate_p100,
            ad_video_view_rate_2s=insert_stmt.inserted.ad_video_view_rate_2s,
            ad_video_view_rate_6s=insert_stmt.inserted.ad_video_view_rate_6s,
            impressions=insert_stmt.inserted.impressions,
            clicks=insert_stmt.inserted.clicks,
            orders=insert_stmt.inserted.orders,
            cost_cents=insert_stmt.inserted.cost_cents,
            net_cost_cents=insert_stmt.inserted.net_cost_cents,
            cost_per_order_cents=insert_stmt.inserted.cost_per_order_cents,
            gross_revenue_cents=insert_stmt.inserted.gross_revenue_cents,
            roi=insert_stmt.inserted.roi,
            creative_delivery_status=insert_stmt.inserted.creative_delivery_status,
            raw_metrics=insert_stmt.inserted.raw_metrics,
            snapshot_at=insert_stmt.inserted.snapshot_at,
        )

        session.execute(update_stmt)
        rows_written += 1

    session.flush()
    return {"rows": rows_written, "snapshot_at": snapshot_at.isoformat()}


def latest_creative_metrics_snapshots(
    session: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_ids: Sequence[str],
    start_date: date,
    end_date: date,
    store_ids: Sequence[str] | None = None,
    product_ids: Sequence[str] | None = None,
) -> list[TTBGmvMaxCreativeMetrics10Min]:
    base = (
        select(
            TTBGmvMaxCreativeMetrics10Min.creative_id,
            TTBGmvMaxCreativeMetrics10Min.stat_time_day,
            func.max(TTBGmvMaxCreativeMetrics10Min.snapshot_at).label("latest_snapshot"),
        )
        .where(TTBGmvMaxCreativeMetrics10Min.workspace_id == workspace_id)
        .where(TTBGmvMaxCreativeMetrics10Min.provider == provider)
        .where(TTBGmvMaxCreativeMetrics10Min.auth_id == auth_id)
        .where(TTBGmvMaxCreativeMetrics10Min.campaign_id.in_(campaign_ids))
        .where(TTBGmvMaxCreativeMetrics10Min.stat_time_day >= start_date)
        .where(TTBGmvMaxCreativeMetrics10Min.stat_time_day <= end_date)
        .where(
            True
            if not store_ids
            else TTBGmvMaxCreativeMetrics10Min.store_id.in_(store_ids)
        )
        .where(
            True
            if not product_ids
            else TTBGmvMaxCreativeMetrics10Min.product_id.in_(product_ids)
        )
        .group_by(
            TTBGmvMaxCreativeMetrics10Min.creative_id,
            TTBGmvMaxCreativeMetrics10Min.stat_time_day,
        )
    ).subquery()

    stmt = (
        select(TTBGmvMaxCreativeMetrics10Min)
        .join(
            base,
            (TTBGmvMaxCreativeMetrics10Min.creative_id == base.c.creative_id)
            & (TTBGmvMaxCreativeMetrics10Min.stat_time_day == base.c.stat_time_day)
            & (TTBGmvMaxCreativeMetrics10Min.snapshot_at == base.c.latest_snapshot),
        )
        .order_by(TTBGmvMaxCreativeMetrics10Min.stat_time_day.asc())
    )
    return list(session.execute(stmt).scalars().all())
