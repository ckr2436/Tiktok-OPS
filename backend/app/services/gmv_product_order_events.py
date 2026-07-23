"""Derive product order timing events from GMV Max product hourly reports."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def _date_start(value: date | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(minute=0, second=0, microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    parsed = datetime.fromisoformat(str(value).strip())
    return datetime.combine(parsed.date(), time.min)


def _date_end_exclusive(value: date | str | None) -> datetime | None:
    start = _date_start(value)
    if start is None:
        return None
    return start + timedelta(days=1)


def sync_product_order_events_from_hourly(
    db: Session,
    *,
    workspace_id: int | None = None,
    auth_id: int | None = None,
    advertiser_id: str | None = None,
    store_id: str | None = None,
    campaign_id: str | None = None,
    item_group_id: str | None = None,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> int:
    """Upsert one event per product/campaign/hour where hourly orders are present."""

    filters: list[str] = ["h.orders IS NOT NULL", "h.orders > 0"]
    params: dict[str, Any] = {}

    for key, value in (
        ("workspace_id", workspace_id),
        ("auth_id", auth_id),
        ("advertiser_id", advertiser_id),
        ("store_id", store_id),
        ("campaign_id", campaign_id),
        ("item_group_id", item_group_id),
    ):
        if value is not None:
            filters.append(f"h.{key} = :{key}")
            params[key] = value

    start_dt = _date_start(start_date)
    end_dt = _date_end_exclusive(end_date)
    if start_dt is not None:
        filters.append("h.stat_time_hour >= :start_dt")
        params["start_dt"] = start_dt
    if end_dt is not None:
        filters.append("h.stat_time_hour < :end_dt")
        params["end_dt"] = end_dt

    where_sql = " AND ".join(filters)
    statement = text(
        f"""
        INSERT INTO gmv_product_order_events (
            workspace_id,
            auth_id,
            advertiser_id,
            store_id,
            campaign_id,
            item_group_id,
            order_time_hour,
            advertiser_timezone,
            order_count,
            gross_revenue_cents,
            cost_cents,
            cost_per_order,
            roi,
            source,
            raw_json,
            first_seen_at,
            last_seen_at,
            created_at,
            updated_at
        )
        SELECT
            h.workspace_id,
            h.auth_id,
            h.advertiser_id,
            h.store_id,
            h.campaign_id,
            h.item_group_id,
            h.stat_time_hour,
            COALESCE(a.display_timezone, a.timezone),
            COALESCE(h.orders, 0),
            h.gross_revenue_cents,
            h.cost_cents,
            h.cost_per_order,
            h.roi,
            'GMVMAX_PRODUCT_HOURLY',
            JSON_OBJECT(
                'product_metrics_hourly_id', h.id,
                'bid_type', h.bid_type,
                'stat_time_hour', DATE_FORMAT(h.stat_time_hour, '%Y-%m-%d %H:%i:%s')
            ),
            CURRENT_TIMESTAMP(6),
            CURRENT_TIMESTAMP(6),
            CURRENT_TIMESTAMP(6),
            CURRENT_TIMESTAMP(6)
        FROM gmv_product_metrics_hourly h
        LEFT JOIN ttb_advertisers a
          ON a.workspace_id = h.workspace_id
         AND a.auth_id = h.auth_id
         AND a.advertiser_id = h.advertiser_id
        WHERE {where_sql}
        ON DUPLICATE KEY UPDATE
            advertiser_timezone = VALUES(advertiser_timezone),
            order_count = VALUES(order_count),
            gross_revenue_cents = VALUES(gross_revenue_cents),
            cost_cents = VALUES(cost_cents),
            cost_per_order = VALUES(cost_per_order),
            roi = VALUES(roi),
            source = VALUES(source),
            raw_json = VALUES(raw_json),
            last_seen_at = CURRENT_TIMESTAMP(6),
            updated_at = CURRENT_TIMESTAMP(6)
        """
    )
    result = db.execute(statement, params)
    return int(result.rowcount or 0)
