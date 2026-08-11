from __future__ import annotations

import logging
import json
import os
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvCreativeMetrics10Min
from app.data.models.gmvmax_sync_state import GmvCreative10MinBatchManifest
from app.providers.tiktok_business.gmvmax_client import TikTokBusinessGMVMaxClient
from app.gmvmax.services.fact_freshness import settlement_metadata, utc_now_naive
from app.services.gmvmax_spec import GMVMaxReportLevel
from app.services.ttb_gmvmax import (
    fetch_gmvmax_current_creative_statuses,
    fetch_gmvmax_report_by_level,
)
from app.services.gmvmax_creative_assets import (
    resolve_store_authorized_bc_id,
    sync_creative_assets_for_scope,
)

logger = logging.getLogger("gmv.services.gmvmax.creative_metrics")

CREATIVE_ASSET_REFRESH_SECONDS = max(
    60,
    int(os.getenv("GMVMAX_CREATIVE_ASSET_REFRESH_SECONDS", "600")),
)

_UPSERT_CREATIVE_10MIN_SQL = text(
    """
    insert into gmv_creative_metrics_10min (
        workspace_id, auth_id, advertiser_id, store_id,
        campaign_id, item_group_id, creative_id, stat_time_day, snapshot_at,
        creative_status,
        impressions, clicks, product_impressions, product_clicks,
        product_click_rate, ad_click_rate, ad_conversion_rate, conversion_rate,
        cost_cents, net_cost_cents, orders, gross_revenue_cents, cost_per_order, roi,
        video_view_rate_2s, video_view_rate_6s, video_view_rate_25,
        video_view_rate_50, video_view_rate_75, video_view_rate_100,
        source_observed_at, ingested_at, is_final, settled_at
    ) values (
        :workspace_id, :auth_id, :advertiser_id, :store_id,
        :campaign_id, :item_group_id, :creative_id, :stat_time_day, :snapshot_at,
        :creative_status,
        :impressions, :clicks, :product_impressions, :product_clicks,
        :product_click_rate, :ad_click_rate, :ad_conversion_rate, :conversion_rate,
        :cost_cents, :net_cost_cents, :orders, :gross_revenue_cents, :cost_per_order, :roi,
        :video_view_rate_2s, :video_view_rate_6s, :video_view_rate_25,
        :video_view_rate_50, :video_view_rate_75, :video_view_rate_100,
        :source_observed_at, :ingested_at, :is_final, :settled_at
    )
    on duplicate key update
        creative_status=coalesce(values(creative_status), creative_status),
        impressions=coalesce(values(impressions), impressions),
        clicks=coalesce(values(clicks), clicks),
        product_impressions=coalesce(values(product_impressions), product_impressions),
        product_clicks=coalesce(values(product_clicks), product_clicks),
        product_click_rate=coalesce(values(product_click_rate), product_click_rate),
        ad_conversion_rate=coalesce(values(ad_conversion_rate), ad_conversion_rate),
        cost_per_order=coalesce(values(cost_per_order), cost_per_order),
        cost_cents=coalesce(values(cost_cents), cost_cents),
        net_cost_cents=coalesce(values(net_cost_cents), net_cost_cents),
        orders=coalesce(values(orders), orders),
        gross_revenue_cents=coalesce(values(gross_revenue_cents), gross_revenue_cents),
        roi=coalesce(values(roi), roi),
        ad_click_rate=coalesce(values(ad_click_rate), ad_click_rate),
        conversion_rate=coalesce(values(conversion_rate), conversion_rate),
        video_view_rate_2s=coalesce(values(video_view_rate_2s), video_view_rate_2s),
        video_view_rate_6s=coalesce(values(video_view_rate_6s), video_view_rate_6s),
        video_view_rate_25=coalesce(values(video_view_rate_25), video_view_rate_25),
        video_view_rate_50=coalesce(values(video_view_rate_50), video_view_rate_50),
        video_view_rate_75=coalesce(values(video_view_rate_75), video_view_rate_75),
        video_view_rate_100=coalesce(values(video_view_rate_100), video_view_rate_100),
        source_observed_at=values(source_observed_at),
        ingested_at=values(ingested_at),
        is_final=(is_final or values(is_final)),
        settled_at=coalesce(values(settled_at), settled_at)
    """
)


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


def _campaign_attr(campaign: Any, key: str, default: Any = None) -> Any:
    if isinstance(campaign, Mapping):
        return campaign.get(key, default)
    return getattr(campaign, key, default)


def _load_json(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _normalize_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_item_group_ids_from_raw(raw: Any) -> list[str]:
    payload = _load_json(raw)
    candidates = payload.get("item_group_ids")
    if not candidates and isinstance(payload.get("_campaign_info"), Mapping):
        candidates = payload["_campaign_info"].get("item_group_ids")
    if isinstance(candidates, (str, int)):
        candidates = [candidates]
    if not isinstance(candidates, Sequence):
        return []
    return list(dict.fromkeys(filter(None, (_normalize_identifier(item) for item in candidates))))


def _item_group_ids_for_campaign(session: Session, campaign: Any) -> list[str]:
    campaign_id = str(_campaign_attr(campaign, "campaign_id", "") or "")
    workspace_id = int(_campaign_attr(campaign, "workspace_id", 0) or 0)
    auth_id = int(_campaign_attr(campaign, "auth_id", 0) or 0)
    advertiser_id = str(_campaign_attr(campaign, "advertiser_id", "") or "")
    store_id = str(_campaign_attr(campaign, "store_id", "") or "")

    resolved: list[str] = []
    for raw_attribute in ("raw_json", "detail_raw_json", "list_raw_json"):
        resolved.extend(
            _extract_item_group_ids_from_raw(
                _campaign_attr(campaign, raw_attribute)
            )
        )

    catalog_row = session.execute(
        text(
            """
            select detail_raw_json, list_raw_json
            from gmvmax_product_campaign_catalog
            where campaign_id=:campaign_id
              and workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
            limit 1
            """
        ),
        {
            "campaign_id": campaign_id,
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": advertiser_id,
            "store_id": store_id,
        },
    ).mappings().first()
    for raw_attribute in ("detail_raw_json", "list_raw_json"):
        resolved.extend(
            _extract_item_group_ids_from_raw(
                (catalog_row or {}).get(raw_attribute)
            )
        )

    rows = session.execute(
        text(
            """
            select item_group_id
            from gmvmax_product_campaign_item_groups
            where campaign_id=:campaign_id
              and workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
            union
            select item_group_id
            from gmvmax_product_creative_metrics_daily
            where campaign_id=:campaign_id
              and workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and item_group_id is not null
            """
        ),
        {
            "campaign_id": campaign_id,
            "workspace_id": workspace_id,
            "auth_id": auth_id,
            "advertiser_id": advertiser_id,
            "store_id": store_id,
        },
    ).scalars().all()
    resolved.extend(
        item
        for item in (_normalize_identifier(row) for row in rows)
        if item
    )
    return list(dict.fromkeys(resolved))


def _advertiser_timezone_for_scope(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str | None:
    row = session.execute(
        text(
            """
            select display_timezone, timezone
            from ttb_advertisers
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
            order by last_seen_at desc
            limit 1
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
        },
    ).mappings().first()
    if not row:
        return None
    return _normalize_identifier(row.get("display_timezone") or row.get("timezone"))


def _entry_stat_day(
    dimensions: Mapping[str, Any],
    fallback: date | None = None,
) -> date | None:
    raw = dimensions.get("stat_time_day")
    if raw is None:
        raw = fallback
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _entry_parts(entry: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(entry, Mapping):
        return (
            dict(entry.get("metrics") or {}),
            dict(entry.get("dimensions") or {}),
        )
    return (
        dict(getattr(entry, "metrics", {}) or {}),
        dict(getattr(entry, "dimensions", {}) or {}),
    )


def _assert_complete_creative_rows(
    entries: Sequence[Any],
    *,
    campaign_id: str,
    item_group_ids: Sequence[str],
    start_date: date,
    end_date: date,
) -> None:
    """Reject a batch if any official row cannot be assigned to its exact scope."""

    allowed_item_groups = {str(item) for item in item_group_ids}
    seen_keys: set[tuple[str, str, str, date]] = set()
    for index, entry in enumerate(entries):
        _metrics, dimensions = _entry_parts(entry)
        resolved_campaign_id = _normalize_identifier(dimensions.get("campaign_id"))
        item_group_id = _normalize_identifier(
            dimensions.get("item_group_id") or dimensions.get("product_id")
        )
        creative_id = _normalize_identifier(
            dimensions.get("shop_content_id")
            or dimensions.get("creative_id")
            or dimensions.get("item_id")
        )
        stat_time_day = _entry_stat_day(dimensions)
        if (
            resolved_campaign_id != str(campaign_id)
            or item_group_id not in allowed_item_groups
            or not creative_id
            or stat_time_day is None
            or stat_time_day < start_date
            or stat_time_day > end_date
        ):
            raise ValueError(
                "incomplete GMV Max creative row in official snapshot "
                f"at index {index}"
            )
        key = (
            str(resolved_campaign_id),
            str(item_group_id),
            str(creative_id),
            stat_time_day,
        )
        if key in seen_keys:
            raise ValueError(
                "duplicate GMV Max creative row in official snapshot "
                f"at index {index}"
            )
        seen_keys.add(key)


def _snapshot_days(start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    days: list[date] = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current += timedelta(days=1)
    return days


def _register_complete_batch_manifests(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    snapshot_at: datetime,
    source_observed_at: datetime,
    row_counts: Mapping[date, int],
) -> None:
    """Upsert complete watermarks after every metric row has flushed."""

    for stat_time_day, row_count in row_counts.items():
        manifest = session.scalar(
            select(GmvCreative10MinBatchManifest).where(
                GmvCreative10MinBatchManifest.workspace_id == int(workspace_id),
                GmvCreative10MinBatchManifest.auth_id == int(auth_id),
                GmvCreative10MinBatchManifest.advertiser_id
                == str(advertiser_id),
                GmvCreative10MinBatchManifest.store_id == str(store_id),
                GmvCreative10MinBatchManifest.campaign_id == str(campaign_id),
                GmvCreative10MinBatchManifest.stat_time_day == stat_time_day,
                GmvCreative10MinBatchManifest.snapshot_at == snapshot_at,
            )
        )
        if manifest is None:
            manifest = GmvCreative10MinBatchManifest(
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                advertiser_id=str(advertiser_id),
                store_id=str(store_id),
                campaign_id=str(campaign_id),
                stat_time_day=stat_time_day,
                snapshot_at=snapshot_at,
            )
        manifest.complete = True
        manifest.row_count = max(0, int(row_count))
        manifest.source_observed_at = source_observed_at
        session.add(manifest)


def _merge_creative_entries_by_day(
    entries: Sequence[Mapping[str, Any]],
    status_entries: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    item_group_ids: Sequence[str],
    status_day: date,
) -> list[dict[str, Any]]:
    """Merge current status without collapsing performance from other dates."""

    rows: dict[tuple[str, str, str, date], dict[str, Any]] = {}

    def _resolve(
        entry: Any,
        *,
        fallback_day: date | None = None,
    ) -> tuple[tuple[str, str, str, date], dict[str, Any]] | None:
        _metrics, dimensions = _entry_parts(entry)
        stat_day = _entry_stat_day(dimensions, fallback_day)
        item_group_id = str(
            dimensions.get("product_id")
            or dimensions.get("item_group_id")
            or (item_group_ids[0] if len(item_group_ids) == 1 else "")
        ).strip()
        creative_id = str(
            dimensions.get("shop_content_id")
            or dimensions.get("creative_id")
            or dimensions.get("item_id")
            or ""
        ).strip()
        resolved_campaign_id = str(
            dimensions.get("campaign_id") or campaign_id
        ).strip()
        if not resolved_campaign_id or not item_group_id or not creative_id or stat_day is None:
            return None
        dimensions.update(
            {
                "campaign_id": resolved_campaign_id,
                "item_group_id": item_group_id,
                "creative_id": creative_id,
                "stat_time_day": stat_day,
            }
        )
        return (
            (resolved_campaign_id, item_group_id, creative_id, stat_day),
            dimensions,
        )

    for entry in entries:
        entry_metrics, _entry_dimensions = _entry_parts(entry)
        resolved = _resolve(entry)
        if resolved is None:
            continue
        key, dimensions = resolved
        rows[key] = {
            "metrics": entry_metrics,
            "dimensions": dimensions,
        }

    for entry in status_entries:
        entry_metrics, _entry_dimensions = _entry_parts(entry)
        resolved = _resolve(entry, fallback_day=status_day)
        if resolved is None:
            continue
        key, dimensions = resolved
        status_metrics = entry_metrics
        existing = rows.get(key)
        if existing is None:
            rows[key] = {
                "metrics": status_metrics,
                "dimensions": dimensions,
            }
            continue
        status = status_metrics.get("creative_delivery_status")
        if status is not None:
            existing["metrics"]["creative_delivery_status"] = status

    return list(rows.values())


def _creative_asset_refresh_needed(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    creative_refs: Sequence[Mapping[str, Any]],
) -> tuple[bool, str]:
    creative_ids = sorted(
        {
            str(item.get("creative_id") or "").strip()
            for item in creative_refs
            if str(item.get("creative_id") or "").strip() not in {"", "-1", "0"}
        }
    )
    if not creative_ids:
        return False, "no_video_creatives"

    query = text(
        """
        select count(distinct case when item_id in :creative_ids then item_id end) as matched,
               sum(
                   case
                       when item_id in :creative_ids and media_cache_status='SOURCE_EXPIRED' then 1
                       else 0
                   end
               ) as expired_sources,
               coalesce(
                   timestampdiff(
                       second,
                       max(case when item_id in :creative_ids then fetched_at end),
                       current_timestamp(6)
                   ),
                   999999
               ) as matched_age_seconds,
               coalesce(
                   timestampdiff(second, max(fetched_at), current_timestamp(6)),
                   999999
               ) as scope_age_seconds
        from gmvmax_creative_asset_cache
        where workspace_id=:workspace_id
          and auth_id=:auth_id
          and advertiser_id=:advertiser_id
          and store_id=:store_id
        """
    ).bindparams(bindparam("creative_ids", expanding=True))
    try:
        row = session.execute(
            query,
            {
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "store_id": str(store_id),
                "creative_ids": creative_ids,
            },
        ).mappings().one()
    except Exception:  # noqa: BLE001 - a missing cache must fall back to enrichment
        logger.warning("gmvmax creative asset freshness check failed", exc_info=True)
        return True, "freshness_check_failed"

    matched_age_seconds = int(row.get("matched_age_seconds") or 0)
    if int(row.get("expired_sources") or 0) > 0:
        # SOURCE_EXPIRED describes a temporary remote URL, not a missing
        # creative. Re-scanning the complete video library every few minutes
        # cannot repair a still-valid local cache any faster. Probe again only
        # at the normal inventory refresh boundary.
        if matched_age_seconds >= CREATIVE_ASSET_REFRESH_SECONDS:
            return True, "expired_media_source"
        return False, "expired_media_source_recently_probed"
    if int(row.get("matched") or 0) < len(creative_ids):
        if int(row.get("scope_age_seconds") or 0) < CREATIVE_ASSET_REFRESH_SECONDS:
            return False, "unmatched_creative_recently_probed"
        return True, "new_video_creative"
    if matched_age_seconds >= CREATIVE_ASSET_REFRESH_SECONDS:
        return True, "asset_cache_stale"
    return False, "asset_cache_fresh"


async def sync_creative_metrics_10min_for_campaign(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    campaign: Any,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    item_group_ids = _item_group_ids_for_campaign(session, campaign)
    campaign_id = str(_campaign_attr(campaign, "campaign_id", "") or "")
    store_id = str(_campaign_attr(campaign, "store_id", "") or "")
    if not workspace_id or not auth_id or not advertiser_id or not campaign_id or not store_id:
        raise ValueError("complete GMV Max tenant and campaign scope is required")
    if not item_group_ids:
        logger.warning(
            "gmvmax creative metrics missing item groups",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "campaign_id": campaign_id,
                "store_id": store_id,
            },
        )
        return {"rows": 0}

    entries = await fetch_gmvmax_report_by_level(
        client,
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=campaign_id,
        campaign_ids=[campaign_id],
        level=GMVMaxReportLevel.CREATIVE.value,
        start_date=start_date,
        end_date=end_date,
        item_group_ids=item_group_ids,
    )
    _assert_complete_creative_rows(
        entries,
        campaign_id=campaign_id,
        item_group_ids=item_group_ids,
        start_date=start_date,
        end_date=end_date,
    )

    status_entries = await fetch_gmvmax_current_creative_statuses(
        client,
        advertiser_id=str(advertiser_id),
        store_id=store_id,
        campaign_ids=[campaign_id],
        item_group_ids=item_group_ids,
        report_date=end_date,
    )
    _assert_complete_creative_rows(
        status_entries,
        campaign_id=campaign_id,
        item_group_ids=item_group_ids,
        start_date=end_date,
        end_date=end_date,
    )
    entries = _merge_creative_entries_by_day(
        entries,
        status_entries,
        campaign_id=campaign_id,
        item_group_ids=item_group_ids,
        status_day=end_date,
    )

    snapshot_at = _floor_snapshot()
    source_observed_at = utc_now_naive()
    ingested_at = utc_now_naive()
    snapshot_days = _snapshot_days(start_date, end_date)
    row_counts = {stat_time_day: 0 for stat_time_day in snapshot_days}
    # A retry inside the same ten-minute bucket replaces the prior whole batch.
    # The delete and all following inserts/manifests share the caller's
    # transaction, so readers see either the old complete batch or the new one.
    session.execute(
        delete(GmvCreativeMetrics10Min).where(
            GmvCreativeMetrics10Min.workspace_id == int(workspace_id),
            GmvCreativeMetrics10Min.auth_id == int(auth_id),
            GmvCreativeMetrics10Min.advertiser_id == str(advertiser_id),
            GmvCreativeMetrics10Min.store_id == store_id,
            GmvCreativeMetrics10Min.campaign_id == campaign_id,
            GmvCreativeMetrics10Min.stat_time_day.in_(snapshot_days),
            GmvCreativeMetrics10Min.snapshot_at == snapshot_at,
        )
    )
    advertiser_timezone = (
        _normalize_identifier(_campaign_attr(campaign, "advertiser_timezone"))
        or _normalize_identifier(_campaign_attr(campaign, "display_timezone"))
        or _normalize_identifier(_campaign_attr(campaign, "timezone"))
        or _advertiser_timezone_for_scope(
            session,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
        )
    )
    rows_written = 0
    creative_refs: list[dict[str, str]] = []
    for entry in entries:
        metrics, dimensions = _entry_parts(entry)
        creative_id = str(dimensions.get("shop_content_id") or dimensions.get("creative_id") or "").strip()
        if not creative_id:
            raise ValueError("complete creative snapshot lost creative_id before write")
        item_group_id = str(
            dimensions.get("item_group_id")
            or dimensions.get("product_id")
            or (item_group_ids[0] if len(item_group_ids) == 1 else "")
            or ""
        ).strip()
        if not item_group_id:
            raise ValueError("complete creative snapshot lost item_group_id before write")
        if creative_id not in {"-1", "0"}:
            creative_refs.append({"creative_id": creative_id, "item_group_id": item_group_id})

        stat_time_day = _entry_stat_day(dimensions)
        if stat_time_day not in row_counts:
            raise ValueError("complete creative snapshot lost stat_time_day before write")
        is_final, settled_at = settlement_metadata(
            stat_time_day,
            source_observed_at=source_observed_at,
            advertiser_timezone=advertiser_timezone,
        )

        session.execute(
            _UPSERT_CREATIVE_10MIN_SQL,
            {
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "store_id": store_id,
                "campaign_id": campaign_id,
                "item_group_id": item_group_id,
                "creative_id": creative_id,
                "stat_time_day": stat_time_day,
                "snapshot_at": snapshot_at,
                "creative_status": _normalize_identifier(
                    metrics.get("creative_delivery_status")
                    or dimensions.get("creative_delivery_status")
                    or metrics.get("status")
                    or dimensions.get("status")
                ),
                "impressions": _to_int(
                    _first_not_none(
                        metrics.get("product_impressions"),
                        metrics.get("impressions"),
                    )
                ),
                "clicks": _to_int(metrics.get("clicks")),
                "product_impressions": _to_int(metrics.get("product_impressions")),
                "product_clicks": _to_int(metrics.get("product_clicks")),
                "product_click_rate": _to_decimal(metrics.get("product_click_rate")),
                "ad_click_rate": _to_decimal(metrics.get("ad_click_rate")),
                "ad_conversion_rate": _to_decimal(metrics.get("ad_conversion_rate")),
                "conversion_rate": _to_decimal(
                    _first_not_none(
                        metrics.get("conversion_rate"),
                        metrics.get("ad_conversion_rate"),
                    )
                ),
                "video_view_rate_25": _to_decimal(metrics.get("ad_video_view_rate_p25")),
                "video_view_rate_50": _to_decimal(metrics.get("ad_video_view_rate_p50")),
                "video_view_rate_75": _to_decimal(metrics.get("ad_video_view_rate_p75")),
                "video_view_rate_100": _to_decimal(metrics.get("ad_video_view_rate_p100")),
                "video_view_rate_2s": _to_decimal(metrics.get("ad_video_view_rate_2s")),
                "video_view_rate_6s": _to_decimal(metrics.get("ad_video_view_rate_6s")),
                "orders": _to_int(metrics.get("orders")),
                "cost_cents": _to_cents(metrics.get("cost")),
                "net_cost_cents": _to_cents(metrics.get("net_cost")),
                "gross_revenue_cents": _to_cents(metrics.get("gross_revenue")),
                "cost_per_order": _to_decimal(metrics.get("cost_per_order")),
                "roi": _to_decimal(metrics.get("roi")),
                "source_observed_at": source_observed_at,
                "ingested_at": ingested_at,
                "is_final": is_final,
                "settled_at": settled_at,
            },
        )
        rows_written += 1
        row_counts[stat_time_day] += 1

    session.flush()
    _register_complete_batch_manifests(
        session,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=str(advertiser_id),
        store_id=store_id,
        campaign_id=campaign_id,
        snapshot_at=snapshot_at,
        source_observed_at=source_observed_at,
        row_counts=row_counts,
    )
    # A manifest is the commit watermark.  Flush it only after all metric
    # writes succeeded; the outer task commits both atomically.
    session.flush()

    asset_result: dict[str, Any] | None = None
    if creative_refs and store_id:
        refresh_assets, refresh_reason = _creative_asset_refresh_needed(
            session,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            creative_refs=creative_refs,
        )
        if not refresh_assets:
            asset_result = {
                "skipped": True,
                "reason": refresh_reason,
                "requested": len(creative_refs),
                "upserted": 0,
            }
        store_authorized_bc_id = resolve_store_authorized_bc_id(
            session,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
        )
        if refresh_assets and store_authorized_bc_id:
            try:
                asset_result = await sync_creative_assets_for_scope(
                    session,
                    client,
                    workspace_id=int(workspace_id),
                    auth_id=int(auth_id),
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    store_authorized_bc_id=str(store_authorized_bc_id),
                    creative_refs=creative_refs,
                    item_group_ids=item_group_ids,
                )
            except Exception:  # noqa: BLE001 - asset enrichment must not break realtime metrics
                logger.warning(
                    "gmvmax creative asset sync failed after 10min metrics",
                    exc_info=True,
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": advertiser_id,
                        "campaign_id": campaign_id,
                        "store_id": store_id,
                    },
                )

    result = {
        "rows": rows_written,
        "snapshot_at": snapshot_at.isoformat(),
        "complete_batches": len(row_counts),
    }
    if asset_result is not None:
        result["assets"] = asset_result
    return result


def latest_creative_metrics_snapshots(
    session: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str | None = None,
    campaign_ids: Sequence[str],
    start_date: date,
    end_date: date,
    store_ids: Sequence[str] | None = None,
    product_ids: Sequence[str] | None = None,
) -> list[Any]:
    clean_campaign_ids = [str(item) for item in campaign_ids if str(item).strip()]
    if not clean_campaign_ids:
        return []
    batch_filters = [
        "workspace_id=:workspace_id",
        "auth_id=:auth_id",
        "campaign_id in :campaign_ids",
        "stat_time_day >= :start_date",
        "stat_time_day <= :end_date",
        "complete=1",
    ]
    params: dict[str, Any] = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "campaign_ids": clean_campaign_ids,
        "start_date": start_date,
        "end_date": end_date,
    }
    expanding_params = ["campaign_ids"]
    if advertiser_id:
        batch_filters.append("advertiser_id=:advertiser_id")
        params["advertiser_id"] = str(advertiser_id)
    clean_store_ids = [str(item) for item in (store_ids or []) if str(item).strip()]
    if clean_store_ids:
        batch_filters.append("store_id in :store_ids")
        params["store_ids"] = clean_store_ids
        expanding_params.append("store_ids")
    clean_product_ids = [str(item) for item in (product_ids or []) if str(item).strip()]
    metric_filter = ""
    if clean_product_ids:
        metric_filter = "and m.item_group_id in :product_ids"
        params["product_ids"] = clean_product_ids
        expanding_params.append("product_ids")

    query = text(
        f"""
        select m.*
        from gmv_creative_metrics_10min m
        join (
            select workspace_id, auth_id, advertiser_id, store_id,
                   campaign_id, stat_time_day,
                   max(snapshot_at) as latest_snapshot
            from gmv_creative_10min_batch_manifests
            where {" and ".join(batch_filters)}
            group by workspace_id, auth_id, advertiser_id, store_id,
                     campaign_id, stat_time_day
        ) latest
          on latest.workspace_id=m.workspace_id
         and latest.auth_id=m.auth_id
         and latest.advertiser_id=m.advertiser_id
         and latest.store_id=m.store_id
         and latest.campaign_id=m.campaign_id
         and latest.stat_time_day=m.stat_time_day
         and latest.latest_snapshot=m.snapshot_at
        where 1=1
          {metric_filter}
        order by m.stat_time_day asc, m.campaign_id asc,
                 m.item_group_id asc, m.creative_id asc
        """
    )
    for name in expanding_params:
        query = query.bindparams(bindparam(name, expanding=True))
    rows = session.execute(
        query,
        params,
    ).mappings().all()
    return [SimpleNamespace(**dict(row)) for row in rows]
