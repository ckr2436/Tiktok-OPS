from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeMetric


_KNOWN_METRIC_FIELDS = {
    "creative_name": "creative_name",
    "adgroup_id": "adgroup_id",
    "product_id": "product_id",
    "item_id": "item_id",
    "impressions": "impressions",
    "clicks": "clicks",
    "cost": "cost",
    "net_cost": "net_cost",
    "orders": "orders",
    "gross_revenue": "gross_revenue",
    "roi": "roi",
    "ad_click_rate": "ad_click_rate",
    "ad_conversion_rate": "ad_conversion_rate",
    "ad_video_view_rate_2s": "ad_video_view_rate_2s",
    "ad_video_view_rate_6s": "ad_video_view_rate_6s",
    "ad_video_view_rate_p25": "ad_video_view_rate_p25",
    "ad_video_view_rate_p50": "ad_video_view_rate_p50",
    "ad_video_view_rate_p75": "ad_video_view_rate_p75",
    "ad_video_view_rate_p100": "ad_video_view_rate_p100",
}


@dataclass
class CreativeMetricsAggregate:
    """Aggregated creative metrics for an evaluation window."""

    creative_id: str
    clicks: int
    ad_click_rate: float | None
    gross_revenue: Any | None


def _normalize_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min)


def _find_cached_instance(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    creative_id: str,
    stat_time_day: datetime,
) -> TTBGmvMaxCreativeMetric | None:
    for obj in db.identity_map.values():
        if not isinstance(obj, TTBGmvMaxCreativeMetric):
            continue
        if (
            obj.workspace_id == workspace_id
            and obj.provider == provider
            and obj.auth_id == auth_id
            and obj.campaign_id == campaign_id
            and obj.creative_id == creative_id
            and obj.stat_time_day == stat_time_day
        ):
            return obj
    for obj in list(db.new):
        if not isinstance(obj, TTBGmvMaxCreativeMetric):
            continue
        if (
            obj.workspace_id == workspace_id
            and obj.provider == provider
            and obj.auth_id == auth_id
            and obj.campaign_id == campaign_id
            and obj.creative_id == creative_id
            and obj.stat_time_day == stat_time_day
        ):
            return obj
    return None


async def upsert_creative_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    creative_id: str,
    stat_time_day: datetime | date,
    metrics: dict[str, Any],
) -> TTBGmvMaxCreativeMetric:
    if not isinstance(metrics, dict):  # pragma: no cover - defensive
        raise ValueError("metrics must be a dict")

    provider_key = str(provider)
    campaign_key = str(campaign_id)
    creative_key = str(creative_id)

    payload = dict(metrics)
    stat_day = _normalize_datetime(stat_time_day)
    instance = _find_cached_instance(
        db,
        workspace_id=workspace_id,
        provider=provider_key,
        auth_id=auth_id,
        campaign_id=campaign_key,
        creative_id=creative_key,
        stat_time_day=stat_day,
    )
    if instance is None:
        stmt: Select[TTBGmvMaxCreativeMetric] = (
            select(TTBGmvMaxCreativeMetric)
            .where(TTBGmvMaxCreativeMetric.workspace_id == workspace_id)
            .where(TTBGmvMaxCreativeMetric.provider == provider_key)
            .where(TTBGmvMaxCreativeMetric.auth_id == auth_id)
            .where(TTBGmvMaxCreativeMetric.campaign_id == campaign_key)
            .where(TTBGmvMaxCreativeMetric.creative_id == creative_key)
            .where(TTBGmvMaxCreativeMetric.stat_time_day == stat_day)
        )
        instance = db.execute(stmt).scalars().first()
    if instance is None:
        instance = TTBGmvMaxCreativeMetric(
            workspace_id=workspace_id,
            provider=provider_key,
            auth_id=auth_id,
            campaign_id=campaign_key,
            creative_id=creative_key,
            stat_time_day=stat_day,
        )
        db.add(instance)
    else:
        instance.provider = provider_key
        instance.campaign_id = campaign_key
        instance.creative_id = creative_key

    for key, attr in _KNOWN_METRIC_FIELDS.items():
        if key in payload:
            setattr(instance, attr, payload.get(key))

    instance.raw_metrics = payload or None
    return instance


def _apply_required_filters(
    *,
    query: Select[TTBGmvMaxCreativeMetric],
    workspace_id: int,
    provider: str,
    auth_id: int,
) -> Select[TTBGmvMaxCreativeMetric]:
    return (
        query.where(TTBGmvMaxCreativeMetric.workspace_id == workspace_id)
        .where(TTBGmvMaxCreativeMetric.provider == str(provider))
        .where(TTBGmvMaxCreativeMetric.auth_id == auth_id)
    )


async def list_creative_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str | None = None,
    creative_ids: Iterable[str] | None = None,
    date_from: datetime | date | None = None,
    date_to: datetime | date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TTBGmvMaxCreativeMetric]:
    db.flush()
    query: Select[TTBGmvMaxCreativeMetric] = select(TTBGmvMaxCreativeMetric)
    query = _apply_required_filters(
        query=query, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )

    if campaign_id is not None:
        query = query.where(TTBGmvMaxCreativeMetric.campaign_id == campaign_id)

    if creative_ids:
        query = query.where(TTBGmvMaxCreativeMetric.creative_id.in_(list(creative_ids)))

    if date_from is not None:
        query = query.where(
            TTBGmvMaxCreativeMetric.stat_time_day >= _normalize_datetime(date_from)
        )
    if date_to is not None:
        query = query.where(
            TTBGmvMaxCreativeMetric.stat_time_day <= _normalize_datetime(date_to)
        )

    query = query.order_by(
        TTBGmvMaxCreativeMetric.stat_time_day.desc(),
        TTBGmvMaxCreativeMetric.creative_id.asc(),
    ).limit(limit).offset(offset)

    return list(db.execute(query).scalars().all())


async def get_latest_metrics_for_creative(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    creative_id: str,
) -> TTBGmvMaxCreativeMetric | None:
    db.flush()
    query: Select[TTBGmvMaxCreativeMetric] = select(TTBGmvMaxCreativeMetric)
    query = _apply_required_filters(
        query=query, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )
    query = (
        query.where(TTBGmvMaxCreativeMetric.campaign_id == campaign_id)
        .where(TTBGmvMaxCreativeMetric.creative_id == creative_id)
        .order_by(TTBGmvMaxCreativeMetric.stat_time_day.desc())
        .limit(1)
    )

    return db.execute(query).scalars().first()


async def get_recent_creative_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    window_minutes: int,
    creative_ids: Iterable[str] | None = None,
) -> dict[str, CreativeMetricsAggregate]:
    """Aggregate metrics for creatives within the provided lookback window."""

    db.flush()
    minutes = max(int(window_minutes or 0), 0)
    now = datetime.now(timezone.utc)
    start_day = (now - timedelta(minutes=minutes)).date()
    window_start = datetime.combine(start_day, time.min)

    query: Select = select(
        TTBGmvMaxCreativeMetric.creative_id,
        func.sum(TTBGmvMaxCreativeMetric.clicks).label("clicks"),
        func.avg(TTBGmvMaxCreativeMetric.ad_click_rate).label("ad_click_rate"),
        func.sum(TTBGmvMaxCreativeMetric.gross_revenue).label("gross_revenue"),
    )
    query = _apply_required_filters(
        query=query, workspace_id=workspace_id, provider=provider, auth_id=auth_id
    )
    query = query.where(TTBGmvMaxCreativeMetric.campaign_id == str(campaign_id))
    query = query.where(TTBGmvMaxCreativeMetric.stat_time_day >= window_start)

    if creative_ids:
        query = query.where(
            TTBGmvMaxCreativeMetric.creative_id.in_([str(cid) for cid in creative_ids])
        )

    query = query.group_by(TTBGmvMaxCreativeMetric.creative_id)

    aggregates: dict[str, CreativeMetricsAggregate] = {}
    for row in db.execute(query):
        creative_id_value = getattr(row, "creative_id")
        clicks_value = getattr(row, "clicks", 0) or 0
        ctr_value = getattr(row, "ad_click_rate", None)
        revenue_value = getattr(row, "gross_revenue", None)
        aggregates[str(creative_id_value)] = CreativeMetricsAggregate(
            creative_id=str(creative_id_value),
            clicks=int(clicks_value),
            ad_click_rate=float(ctr_value) if ctr_value is not None else None,
            gross_revenue=revenue_value,
        )

    return aggregates
