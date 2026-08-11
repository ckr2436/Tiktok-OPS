from __future__ import annotations

"""Canonical creative metric reads used by the legacy heating feature.

Heating is a consumer of the shared GMV Max data plane.  It must never call the
official report endpoint or maintain a second copy of creative facts.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.data.models.gmvmax_creative_metrics import (
    GmvmaxProductCreativeMetricsDaily,
)
from app.data.models.ttb_entities import TTBAdvertiser


_SUPPORTED_PROVIDER = "tiktok-business"


@dataclass(frozen=True)
class CreativeMetricsAggregate:
    """Metrics for one creative and one item group over an evaluation window."""

    creative_id: str
    item_group_id: str
    clicks: int
    ad_click_rate: float | None
    gross_revenue: Decimal | None
    cost: Decimal | None
    orders: int | None
    roi: float | None
    creative_status: str | None


def _canonical_provider(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _advertiser_zone(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> ZoneInfo | timezone:
    row = db.execute(
        select(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .where(TTBAdvertiser.workspace_id == int(workspace_id))
        .where(TTBAdvertiser.auth_id == int(auth_id))
        .where(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .limit(1)
    ).first()
    timezone_name = (
        str((row.display_timezone or row.timezone) if row is not None else "").strip()
    )
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def _first_complete_historical_day(local_start: datetime) -> date:
    """Daily facts are authoritative only for complete days inside the window."""

    if local_start.timetz().replace(tzinfo=None) == time.min:
        return local_start.date()
    return local_start.date() + timedelta(days=1)


def _metric_values(row: Any) -> dict[str, int]:
    return {
        "clicks": int(getattr(row, "clicks", None) or 0),
        "impressions": int(getattr(row, "impressions", None) or 0),
        "gross_revenue_cents": int(
            getattr(row, "gross_revenue_cents", None) or 0
        ),
        "cost_cents": int(getattr(row, "cost_cents", None) or 0),
        "orders": int(getattr(row, "orders", None) or 0),
    }


def _add_values(target: dict[str, int], source: dict[str, int]) -> None:
    for field in target:
        target[field] += int(source.get(field, 0))


def _snapshot_delta(
    snapshots: list[GmvCreativeMetrics10Min],
    *,
    cutoff: datetime,
    window_starts_today: bool,
) -> tuple[dict[str, int] | None, str | None]:
    """Return a reliable delta from cumulative intraday snapshots.

    A single observation inside an intraday window is not treated as fresh
    performance: doing so would mistake all spend since midnight for the last
    N minutes and could stop a healthy creative.
    """

    if not snapshots:
        return None, None
    latest = snapshots[-1]
    latest_values = _metric_values(latest)
    status = str(latest.creative_status) if latest.creative_status else None
    if not window_starts_today:
        return latest_values, status

    baseline: GmvCreativeMetrics10Min | None = None
    for snapshot in snapshots:
        if snapshot.snapshot_at <= cutoff:
            baseline = snapshot
        else:
            break
    if baseline is None:
        baseline = snapshots[0]
    if baseline is latest:
        return None, status

    baseline_values = _metric_values(baseline)
    delta: dict[str, int] = {}
    for field, latest_value in latest_values.items():
        value = latest_value - baseline_values[field]
        # TikTok cumulative counters may reset after a correction.  In that
        # case the newest official value is the only safe non-negative delta.
        delta[field] = latest_value if value < 0 else value
    return delta, status


async def get_recent_creative_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    item_group_id: str,
    window_minutes: int,
    creative_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, CreativeMetricsAggregate]:
    """Read official historical daily facts plus today's cumulative snapshots.

    Every query uses the complete tenant, advertiser, store, campaign, item
    group and creative identity.  Historical rows must carry official source
    provenance; today's mutable value comes only from the latest 10-minute
    snapshots and is converted to a window delta.
    """

    if _canonical_provider(provider) != _SUPPORTED_PROVIDER:
        raise ValueError("unsupported creative metrics provider")

    advertiser_key = str(advertiser_id or "").strip()
    store_key = str(store_id or "").strip()
    campaign_key = str(campaign_id or "").strip()
    item_group_key = str(item_group_id or "").strip()
    if not all((advertiser_key, store_key, campaign_key, item_group_key)):
        raise ValueError("complete creative metric scope is required")

    creative_keys = list(
        dict.fromkeys(
            str(value).strip()
            for value in (creative_ids or [])
            if str(value).strip()
        )
    )
    current_utc = _as_utc(now)
    window = max(1, int(window_minutes or 0))
    window_start_utc = current_utc - timedelta(minutes=window)
    zone = _advertiser_zone(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_key,
    )
    local_now = current_utc.astimezone(zone)
    local_start = window_start_utc.astimezone(zone)
    today = local_now.date()
    first_historical_day = _first_complete_historical_day(local_start)

    totals: dict[str, dict[str, int]] = {}
    status_by_creative: dict[str, tuple[date, str]] = {}

    if first_historical_day < today:
        daily_query: Select[GmvmaxProductCreativeMetricsDaily] = (
            select(GmvmaxProductCreativeMetricsDaily)
            .where(
                GmvmaxProductCreativeMetricsDaily.workspace_id == int(workspace_id),
                GmvmaxProductCreativeMetricsDaily.auth_id == int(auth_id),
                GmvmaxProductCreativeMetricsDaily.advertiser_id == advertiser_key,
                GmvmaxProductCreativeMetricsDaily.store_id == store_key,
                GmvmaxProductCreativeMetricsDaily.campaign_id == campaign_key,
                GmvmaxProductCreativeMetricsDaily.item_group_id == item_group_key,
                GmvmaxProductCreativeMetricsDaily.stat_time_day
                >= first_historical_day,
                GmvmaxProductCreativeMetricsDaily.stat_time_day < today,
                GmvmaxProductCreativeMetricsDaily.source_observed_at.is_not(None),
            )
            .order_by(
                GmvmaxProductCreativeMetricsDaily.creative_id.asc(),
                GmvmaxProductCreativeMetricsDaily.stat_time_day.asc(),
            )
        )
        if creative_keys:
            daily_query = daily_query.where(
                GmvmaxProductCreativeMetricsDaily.creative_id.in_(creative_keys)
            )
        for row in db.execute(daily_query).scalars():
            creative_id = str(row.creative_id)
            values = totals.setdefault(
                creative_id,
                {
                    "clicks": 0,
                    "impressions": 0,
                    "gross_revenue_cents": 0,
                    "cost_cents": 0,
                    "orders": 0,
                },
            )
            _add_values(values, _metric_values(row))
            if row.creative_delivery_status:
                status_by_creative[creative_id] = (
                    row.stat_time_day,
                    str(row.creative_delivery_status),
                )

    latest_complete_snapshot_at = db.scalar(
        select(func.max(GmvCreative10MinBatchManifest.snapshot_at)).where(
            GmvCreative10MinBatchManifest.workspace_id == int(workspace_id),
            GmvCreative10MinBatchManifest.auth_id == int(auth_id),
            GmvCreative10MinBatchManifest.advertiser_id == advertiser_key,
            GmvCreative10MinBatchManifest.store_id == store_key,
            GmvCreative10MinBatchManifest.campaign_id == campaign_key,
            GmvCreative10MinBatchManifest.stat_time_day == today,
            GmvCreative10MinBatchManifest.snapshot_at
            <= current_utc.replace(tzinfo=None),
            GmvCreative10MinBatchManifest.complete.is_(True),
        )
    )
    snapshot_query: Select[GmvCreativeMetrics10Min] = (
        select(GmvCreativeMetrics10Min)
        .join(
            GmvCreative10MinBatchManifest,
            and_(
                GmvCreative10MinBatchManifest.workspace_id
                == GmvCreativeMetrics10Min.workspace_id,
                GmvCreative10MinBatchManifest.auth_id
                == GmvCreativeMetrics10Min.auth_id,
                GmvCreative10MinBatchManifest.advertiser_id
                == GmvCreativeMetrics10Min.advertiser_id,
                GmvCreative10MinBatchManifest.store_id
                == GmvCreativeMetrics10Min.store_id,
                GmvCreative10MinBatchManifest.campaign_id
                == GmvCreativeMetrics10Min.campaign_id,
                GmvCreative10MinBatchManifest.stat_time_day
                == GmvCreativeMetrics10Min.stat_time_day,
                GmvCreative10MinBatchManifest.snapshot_at
                == GmvCreativeMetrics10Min.snapshot_at,
                GmvCreative10MinBatchManifest.complete.is_(True),
            ),
        )
        .where(
            GmvCreativeMetrics10Min.workspace_id == int(workspace_id),
            GmvCreativeMetrics10Min.auth_id == int(auth_id),
            GmvCreativeMetrics10Min.advertiser_id == advertiser_key,
            GmvCreativeMetrics10Min.store_id == store_key,
            GmvCreativeMetrics10Min.campaign_id == campaign_key,
            GmvCreativeMetrics10Min.item_group_id == item_group_key,
            GmvCreativeMetrics10Min.stat_time_day == today,
            GmvCreativeMetrics10Min.snapshot_at
            <= current_utc.replace(tzinfo=None),
            GmvCreativeMetrics10Min.source_observed_at.is_not(None),
        )
        .order_by(
            GmvCreativeMetrics10Min.creative_id.asc(),
            GmvCreativeMetrics10Min.snapshot_at.asc(),
        )
    )
    if creative_keys:
        snapshot_query = snapshot_query.where(
            GmvCreativeMetrics10Min.creative_id.in_(creative_keys)
        )
    snapshots_by_creative: dict[str, list[GmvCreativeMetrics10Min]] = {}
    if latest_complete_snapshot_at is not None:
        for row in db.execute(snapshot_query).scalars():
            snapshots_by_creative.setdefault(str(row.creative_id), []).append(row)

    cutoff = window_start_utc.replace(tzinfo=None)
    for creative_id, snapshots in snapshots_by_creative.items():
        if snapshots[-1].snapshot_at != latest_complete_snapshot_at:
            # The creative is absent from the latest authoritative inventory.
            continue
        delta, status = _snapshot_delta(
            snapshots,
            cutoff=cutoff,
            window_starts_today=local_start.date() == today,
        )
        if status:
            status_by_creative[creative_id] = (today, status)
        if delta is None:
            continue
        values = totals.setdefault(
            creative_id,
            {
                "clicks": 0,
                "impressions": 0,
                "gross_revenue_cents": 0,
                "cost_cents": 0,
                "orders": 0,
            },
        )
        _add_values(values, delta)

    aggregates: dict[str, CreativeMetricsAggregate] = {}
    for creative_id, values in totals.items():
        cost = Decimal(values["cost_cents"]) / Decimal("100")
        revenue = Decimal(values["gross_revenue_cents"]) / Decimal("100")
        status_entry = status_by_creative.get(creative_id)
        aggregates[creative_id] = CreativeMetricsAggregate(
            creative_id=creative_id,
            item_group_id=item_group_key,
            clicks=values["clicks"],
            ad_click_rate=(
                values["clicks"] / values["impressions"]
                if values["impressions"]
                else None
            ),
            gross_revenue=revenue,
            cost=cost,
            orders=values["orders"],
            roi=float(revenue / cost) if cost else None,
            creative_status=status_entry[1] if status_entry else None,
        )
    return aggregates
