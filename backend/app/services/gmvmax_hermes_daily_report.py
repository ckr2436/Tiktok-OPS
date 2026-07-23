from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.services.gmvmax_hermes_context import build_report_context
from app.services.commerce_orders import order_summary, validate_timezone
from app.services.gmvmax_hermes_decision import run_hermes_report_decision
from app.services.gmvmax_hermes_memory import (
    link_policy_evaluations_to_report,
    load_strategy_memory,
    record_policy_evaluations,
    refresh_strategy_memory,
)
from app.services.hermes_agent.client import HermesAdsReviewClient, extract_output_text, extract_usage

logger = logging.getLogger("gmv.services.gmvmax.hermes_daily_report")

_DECISION_TERMINAL_STATUSES = {"APPROVED", "NO_RECOMMENDATIONS", "REJECTED"}
_DECISION_MAX_ATTEMPTS = 4
_DECISION_RETRY_BASE_MINUTES = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _report_evidence_fingerprint(payload: Mapping[str, Any]) -> str:
    evidence = {
        "summary": payload.get("summary"),
        "product_performance": payload.get("product_performance"),
        "campaigns": payload.get("campaigns"),
        "creatives_by_spend": payload.get("creatives_by_spend"),
        "guard_event_rollup": payload.get("guard_event_rollup"),
        "shop_orders": payload.get("shop_orders"),
    }
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _report_final_cutoff_reached(local_now: datetime, *, force: bool = False) -> bool:
    if force:
        return True
    cutoff_hour = max(
        int(settings.GMVMAX_HERMES_DAILY_REPORT_LOCAL_HOUR),
        min(23, int(settings.GMVMAX_HERMES_DAILY_REPORT_FINAL_CUTOFF_HOUR)),
    )
    return local_now.hour >= cutoff_hour


def _existing_report_finalized(existing: Mapping[str, Any] | None) -> bool:
    report_input = _dict((existing or {}).get("input_json"))
    return bool(_dict(report_input.get("data_quality")).get("report_finalized"))


async def _refresh_official_overview(
    db: Session,
    scope: Mapping[str, Any],
    report_date: date,
) -> dict[str, Any]:
    from app.services.ttb_client_factory import build_ttb_gmvmax_client
    from app.services.ttb_gmvmax import sync_gmvmax_overview_metrics

    client = build_ttb_gmvmax_client(
        db,
        auth_id=int(scope["auth_id"]),
        timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
    )
    try:
        result = await sync_gmvmax_overview_metrics(
            db,
            client,
            workspace_id=int(scope["workspace_id"]),
            auth_id=int(scope["auth_id"]),
            advertiser_id=str(scope["advertiser_id"]),
            store_ids=[str(scope["store_id"])],
            start_date=report_date,
            end_date=report_date,
            granularity="DAILY",
        )
        return {"status": "success", "synced_rows": int(result.get("synced_rows") or 0)}
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.warning("Hermes daily report TikTok client close failed", exc_info=True)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(cents: Any) -> float:
    try:
        return round(float(int(cents or 0)) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def ensure_hermes_daily_report_table(db: Session) -> None:
    db.execute(
        text(
            """
            create table if not exists gmv_hermes_ad_daily_reports (
                id bigint unsigned not null auto_increment primary key,
                workspace_id bigint not null,
                auth_id bigint not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                report_date date not null,
                advertiser_timezone varchar(64) null,
                report_type varchar(64) not null default 'DAILY',
                status varchar(32) not null default 'GENERATED',
                input_json json null,
                response_json json null,
                report_markdown mediumtext null,
                recommendation_json json null,
                hermes_response_id varchar(128) null,
                prompt_tokens int null,
                completion_tokens int null,
                total_tokens int null,
                error_message text null,
                decision_status varchar(32) null,
                decision_attempts int not null default 0,
                decision_last_attempt_at datetime(6) null,
                decision_error_message text null,
                created_at datetime(6) not null default current_timestamp(6),
                updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
                unique key uq_gmv_hermes_daily_scope (
                    workspace_id, auth_id, advertiser_id, store_id, report_date, report_type
                ),
                key idx_gmv_hermes_daily_date (report_date),
                key idx_gmv_hermes_daily_status (status)
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci
            """
        )
    )
    existing_columns = {
        str(row[0])
        for row in db.execute(
            text(
                """
                select column_name
                from information_schema.columns
                where table_schema=database()
                  and table_name='gmv_hermes_ad_daily_reports'
                """
            )
        ).all()
    }
    decision_columns = {
        "decision_status": "varchar(32) null",
        "decision_attempts": "int not null default 0",
        "decision_last_attempt_at": "datetime(6) null",
        "decision_error_message": "text null",
    }
    for column_name, definition in decision_columns.items():
        if column_name not in existing_columns:
            db.execute(
                text(
                    f"alter table gmv_hermes_ad_daily_reports "
                    f"add column {column_name} {definition}"
                )
            )


def _decision_retry_due(
    *,
    status: str | None,
    attempts: int,
    last_attempt_at: datetime | None,
    now: datetime,
) -> bool:
    normalized_status = str(status or "").upper()
    if normalized_status in _DECISION_TERMINAL_STATUSES:
        return False
    if attempts >= _DECISION_MAX_ATTEMPTS:
        return False
    if last_attempt_at is None:
        return True
    last_attempt = last_attempt_at
    if last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    retry_minutes = min(
        120,
        _DECISION_RETRY_BASE_MINUTES * (2 ** max(0, attempts - 1)),
    )
    return now >= last_attempt + timedelta(minutes=retry_minutes)


def _load_report_decision_state(db: Session, report_id: int) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            select id, decision_status, decision_attempts, decision_last_attempt_at
            from gmv_hermes_ad_daily_reports
            where id=:report_id
            limit 1
            """
        ),
        {"report_id": int(report_id)},
    ).mappings().first()
    return dict(row) if row else {}


def _update_report_decision_state(
    db: Session,
    *,
    report_id: int,
    status: str,
    attempts: int,
    error_message: str | None = None,
) -> None:
    db.execute(
        text(
            """
            update gmv_hermes_ad_daily_reports
            set decision_status=:status,
                decision_attempts=:attempts,
                decision_last_attempt_at=utc_timestamp(6),
                decision_error_message=:error_message,
                updated_at=current_timestamp(6)
            where id=:report_id
            """
        ),
        {
            "report_id": int(report_id),
            "status": str(status),
            "attempts": int(attempts),
            "error_message": error_message,
        },
    )


async def _run_tracked_report_decision(
    db: Session,
    *,
    report_id: int,
    force: bool = False,
) -> dict[str, Any]:
    state = _load_report_decision_state(db, int(report_id))
    attempts = int(state.get("decision_attempts") or 0)
    now = _utcnow()
    if not force and not _decision_retry_due(
        status=state.get("decision_status"),
        attempts=attempts,
        last_attempt_at=state.get("decision_last_attempt_at"),
        now=now,
    ):
        return {
            "report_id": int(report_id),
            "status": "not_due",
            "approved": 0,
            "attempted": False,
        }

    attempt_number = attempts + 1
    _update_report_decision_state(
        db,
        report_id=int(report_id),
        status="RUNNING",
        attempts=attempt_number,
    )
    db.commit()
    try:
        result = await run_hermes_report_decision(db, report_id=int(report_id))
        result_status = str(result.get("status") or "").lower()
        terminal_status = {
            "approved": "APPROVED",
            "no_recommendations": "NO_RECOMMENDATIONS",
            "rejected": "REJECTED",
        }.get(result_status, "RETRY_PENDING")
        _update_report_decision_state(
            db,
            report_id=int(report_id),
            status=terminal_status,
            attempts=attempt_number,
        )
        db.commit()
        return {**result, "attempted": True, "decision_status": terminal_status}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        ensure_hermes_daily_report_table(db)
        retry_status = (
            "FAILED" if attempt_number >= _DECISION_MAX_ATTEMPTS else "RETRY_PENDING"
        )
        _update_report_decision_state(
            db,
            report_id=int(report_id),
            status=retry_status,
            attempts=attempt_number,
            error_message=str(exc)[:4000],
        )
        db.commit()
        logger.exception(
            "Hermes daily report decision failed",
            extra={"report_id": int(report_id), "attempt": attempt_number},
        )
        return {
            "report_id": int(report_id),
            "status": retry_status.lower(),
            "approved": 0,
            "attempted": True,
            "error": str(exc),
        }


def _extract_json_object(text_value: str) -> dict[str, Any]:
    if not text_value:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text_value, flags=re.S)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text_value[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except json.JSONDecodeError:
            continue
    return {}


def _required_advertiser_timezone(timezone_name: str | None) -> str:
    return validate_timezone(timezone_name)


def _scope_report_date(timezone_name: str | None, requested: date | None) -> date:
    if requested:
        return requested
    tz_name = _required_advertiser_timezone(timezone_name)
    return datetime.now(ZoneInfo(tz_name)).date() - timedelta(days=1)


def _scope_local_now(timezone_name: str | None, now: datetime | None = None) -> datetime:
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(
        ZoneInfo(_required_advertiser_timezone(timezone_name))
    )


def _advertiser_day_utc_bounds(
    timezone_name: str | None,
    report_date: date,
) -> tuple[datetime, datetime]:
    advertiser_tz = ZoneInfo(_required_advertiser_timezone(timezone_name))
    local_start = datetime.combine(report_date, datetime.min.time(), tzinfo=advertiser_tz)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _report_generation_ready(
    timezone_name: str | None,
    report_date: date,
    *,
    now: datetime | None = None,
) -> tuple[bool, str, datetime]:
    local_now = _scope_local_now(timezone_name, now)
    if report_date >= local_now.date():
        return False, "advertiser_day_not_closed", local_now
    ready_minutes = (
        max(0, min(23, int(settings.GMVMAX_HERMES_DAILY_REPORT_LOCAL_HOUR))) * 60
        + max(0, min(59, int(settings.GMVMAX_HERMES_DAILY_REPORT_LOCAL_MINUTE)))
    )
    current_minutes = local_now.hour * 60 + local_now.minute
    if current_minutes < ready_minutes:
        return False, "waiting_for_daily_attribution", local_now
    return True, "ready", local_now


def _load_report_scopes(db: Session) -> list[dict[str, Any]]:
    # This is intentionally complete. A fixed newest-N prefix repeatedly
    # selects already-generated stores and can leave older stores without a
    # daily report forever.
    rows = db.execute(
        text(
            """
            select c.workspace_id, c.auth_id, c.advertiser_id, c.store_id,
                   coalesce(
                       nullif(max(a.display_timezone), ''),
                       nullif(max(a.timezone), '')
                   ) as advertiser_timezone,
                   max(a.currency) as currency
            from gmvmax_product_campaign_catalog c
            left join ttb_advertisers a
              on a.workspace_id=c.workspace_id
             and a.auth_id=c.auth_id
             and a.advertiser_id=c.advertiser_id
            where c.store_id is not null
              and c.store_id <> ''
            group by c.workspace_id, c.auth_id, c.advertiser_id, c.store_id
            order by max(c.updated_at) desc
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _load_campaign_summary(db: Session, scope: Mapping[str, Any], report_date: date) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            select c.campaign_id, c.campaign_name, c.operation_status, c.secondary_status,
                   c.budget_cents, c.roas_bid,
                   coalesce(m.cost_cents, 0) as cost_cents,
                   coalesce(m.gross_revenue_cents, 0) as gross_revenue_cents,
                   coalesce(m.orders, 0) as orders,
                   ig.item_group_ids,
                   ig.product_titles
            from gmvmax_product_campaign_catalog c
            left join (
                select workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                       sum(coalesce(cost_cents, 0)) as cost_cents,
                       sum(coalesce(gross_revenue_cents, 0)) as gross_revenue_cents,
                       sum(coalesce(orders, 0)) as orders
                from gmvmax_product_campaign_metrics_daily
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and stat_time_day=:report_date
                  and source_observed_at is not null
                group by workspace_id, auth_id, advertiser_id, store_id, campaign_id
            ) m
              on m.workspace_id=c.workspace_id
             and m.auth_id=c.auth_id
             and m.advertiser_id=c.advertiser_id
             and m.store_id=c.store_id
             and m.campaign_id=c.campaign_id
            left join (
                select g.workspace_id, g.auth_id, g.advertiser_id, g.store_id,
                       g.campaign_id,
                       group_concat(
                           distinct g.item_group_id
                           order by g.item_group_id separator ','
                       ) as item_group_ids,
                       group_concat(
                           distinct left(p.title, 80)
                           order by p.product_id separator ' | '
                       ) as product_titles
                from gmvmax_product_campaign_item_groups g
                left join ttb_products p
                  on p.workspace_id=g.workspace_id
                 and p.auth_id=g.auth_id
                 and p.store_id=g.store_id
                 and p.product_id=g.item_group_id
                where g.workspace_id=:workspace_id
                  and g.auth_id=:auth_id
                  and g.advertiser_id=:advertiser_id
                  and g.store_id=:store_id
                group by g.workspace_id, g.auth_id, g.advertiser_id, g.store_id,
                         g.campaign_id
            ) ig
              on ig.workspace_id=c.workspace_id
             and ig.auth_id=c.auth_id
             and ig.advertiser_id=c.advertiser_id
             and ig.store_id=c.store_id
             and ig.campaign_id=c.campaign_id
            where c.workspace_id=:workspace_id
              and c.auth_id=:auth_id
              and c.advertiser_id=:advertiser_id
              and c.store_id=:store_id
              and (
                   m.campaign_id is not null
                or c.operation_status='ENABLE'
                or c.updated_at >= date_sub(:report_date, interval 2 day)
              )
            order by cost_cents desc, c.updated_at desc
            """
        ),
        {
            **scope,
            "report_date": report_date,
        },
    ).mappings().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        cost = int(row.get("cost_cents") or 0)
        gmv = int(row.get("gross_revenue_cents") or 0)
        orders = int(row.get("orders") or 0)
        result.append(
            {
                "campaign_id": str(row.get("campaign_id") or ""),
                "campaign_name": row.get("campaign_name"),
                "status": row.get("operation_status"),
                "secondary_status": row.get("secondary_status"),
                "budget": _money(row.get("budget_cents")),
                "roas_bid": _safe_float(row.get("roas_bid")),
                "cost": _money(cost),
                "gmv": _money(gmv),
                "orders": orders,
                "roi": round(gmv / cost, 4) if cost > 0 else 0.0,
                "cpo": round(cost / 100 / orders, 2) if orders > 0 else None,
                "item_group_ids": [item for item in str(row.get("item_group_ids") or "").split(",") if item],
                "product_titles": row.get("product_titles"),
            }
        )
    return result


def _load_overview_summary(db: Session, scope: Mapping[str, Any], report_date: date) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            select cost_cents, net_cost_cents, gross_revenue_cents, orders
            from gmv_overview_metrics_daily
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and stat_time_day=:report_date
              and source_observed_at is not null
            limit 1
            """
        ),
        {**scope, "report_date": report_date},
    ).mappings().first()
    if row:
        return {
            "source": "overview_daily",
            "authoritative": True,
            "cost_cents": int(row.get("cost_cents") or 0),
            "net_cost_cents": int(row.get("net_cost_cents") or 0),
            "gross_revenue_cents": int(row.get("gross_revenue_cents") or 0),
            "orders": int(row.get("orders") or 0),
        }
    snapshot = db.execute(
        text(
            """
            select cost_cents, net_cost_cents, gross_revenue_cents, orders, snapshot_at
            from gmv_overview_snapshots
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and start_date=:report_date
              and end_date=:report_date
            order by snapshot_at desc
            limit 1
            """
        ),
        {**scope, "report_date": report_date},
    ).mappings().first()
    if not snapshot:
        return {}
    return {
        "source": "overview_snapshot",
        "authoritative": False,
        "cost_cents": int(snapshot.get("cost_cents") or 0),
        "net_cost_cents": int(snapshot.get("net_cost_cents") or 0),
        "gross_revenue_cents": int(snapshot.get("gross_revenue_cents") or 0),
        "orders": int(snapshot.get("orders") or 0),
        "snapshot_at": str(snapshot.get("snapshot_at") or ""),
    }


def _reconcile_report_summary(
    campaigns: list[Mapping[str, Any]],
    overview: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    detail_cost_cents = int(round(sum(float(item.get("cost") or 0) for item in campaigns) * 100))
    detail_gmv_cents = int(round(sum(float(item.get("gmv") or 0) for item in campaigns) * 100))
    detail_orders = sum(int(item.get("orders") or 0) for item in campaigns)
    overview_dict = dict(overview or {})
    has_overview = bool(overview_dict)
    authoritative = bool(overview_dict) and bool(
        overview_dict.get("authoritative", overview_dict.get("source") == "overview_daily")
    )
    cost_cents = int(overview_dict.get("cost_cents") if has_overview else detail_cost_cents)
    net_cost_cents = int(
        overview_dict.get("net_cost_cents")
        if has_overview and overview_dict.get("net_cost_cents") is not None
        else cost_cents
    )
    gmv_cents = int(
        overview_dict.get("gross_revenue_cents") if has_overview else detail_gmv_cents
    )
    orders = int(overview_dict.get("orders") if has_overview else detail_orders)
    tolerance = max(
        0.0,
        min(0.5, float(settings.GMVMAX_HERMES_DAILY_REPORT_DETAIL_TOLERANCE)),
    )

    def _within(detail: int, total: int, *, absolute: int = 1) -> bool:
        return abs(detail - total) <= max(absolute, int(abs(total) * tolerance))

    detail_complete = authoritative and all(
        (
            _within(detail_cost_cents, cost_cents, absolute=100),
            _within(detail_gmv_cents, gmv_cents, absolute=100),
            _within(detail_orders, orders, absolute=0),
        )
    )
    summary = {
        "cost": _money(cost_cents),
        "net_cost": _money(net_cost_cents),
        "gmv": _money(gmv_cents),
        "orders": orders,
        "roi": round(gmv_cents / cost_cents, 4) if cost_cents > 0 else 0.0,
        "summary_source": overview_dict.get("source") or "campaign_daily_fallback",
        "active_campaigns": sum(1 for item in campaigns if item.get("status") == "ENABLE"),
        "campaigns_with_spend": sum(1 for item in campaigns if float(item.get("cost") or 0) > 0),
    }
    data_quality = {
        "state": "complete" if detail_complete else "degraded",
        "authoritative_totals": authoritative,
        "authoritative_source": overview_dict.get("source") or None,
        "overview_snapshot_at": overview_dict.get("snapshot_at") or None,
        "campaign_detail_complete": detail_complete,
        "campaign_detail": {
            "cost": _money(detail_cost_cents),
            "gmv": _money(detail_gmv_cents),
            "orders": detail_orders,
        },
        "difference": {
            "cost": _money(cost_cents - detail_cost_cents),
            "gmv": _money(gmv_cents - detail_gmv_cents),
            "orders": orders - detail_orders,
        },
    }
    return summary, data_quality


def _load_creative_summary(db: Session, scope: Mapping[str, Any], report_date: date) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            select m.campaign_id, max(c.campaign_name) as campaign_name,
                   m.item_group_id, m.creative_id,
                   max(m.creative_delivery_status) as status,
                   sum(coalesce(m.cost_cents, 0)) as cost_cents,
                   sum(coalesce(m.gross_revenue_cents, 0)) as gross_revenue_cents,
                   sum(coalesce(m.orders, 0)) as orders,
                   sum(coalesce(m.impressions, 0)) as impressions,
                   sum(coalesce(m.clicks, 0)) as clicks,
                   sum(coalesce(m.product_clicks, 0)) as product_clicks,
                   max(m.ad_click_rate) as ctr,
                   max(m.ad_video_view_rate_2s) as view_2s,
                   max(m.ad_video_view_rate_6s) as view_6s,
                   max(m.ad_video_view_rate_p100) as completion_rate
            from gmvmax_product_creative_metrics_daily m
            left join gmvmax_product_campaign_catalog c
              on c.workspace_id=m.workspace_id
             and c.auth_id=m.auth_id
             and c.advertiser_id=m.advertiser_id
             and c.store_id=m.store_id
             and c.campaign_id=m.campaign_id
            where m.workspace_id=:workspace_id
              and m.auth_id=:auth_id
              and m.advertiser_id=:advertiser_id
              and m.store_id=:store_id
              and m.stat_time_day=:report_date
            group by m.campaign_id, m.item_group_id, m.creative_id
            having cost_cents > 0
            order by cost_cents desc
            """
        ),
        {
            **scope,
            "report_date": report_date,
        },
    ).mappings().all()
    creatives: list[dict[str, Any]] = []
    for row in rows:
        cost = int(row.get("cost_cents") or 0)
        gmv = int(row.get("gross_revenue_cents") or 0)
        orders = int(row.get("orders") or 0)
        creatives.append(
            {
                "campaign_id": row.get("campaign_id"),
                "campaign_name": row.get("campaign_name"),
                "item_group_id": row.get("item_group_id"),
                "creative_id": row.get("creative_id"),
                "status": row.get("status"),
                "cost": _money(cost),
                "gmv": _money(gmv),
                "orders": orders,
                "roi": round(gmv / cost, 4) if cost > 0 else 0.0,
                "impressions": int(row.get("impressions") or 0),
                "clicks": int(row.get("clicks") or 0),
                "product_clicks": int(row.get("product_clicks") or 0),
                "ctr": _safe_float(row.get("ctr")),
                "view_2s": _safe_float(row.get("view_2s")),
                "view_6s": _safe_float(row.get("view_6s")),
                "completion_rate": _safe_float(row.get("completion_rate")),
            }
        )
    return creatives


def _extract_guard_event_identity(request_json: Any) -> dict[str, Any]:
    request = _dict(request_json)
    body = _dict(request.get("body"))
    decision = _dict(request.get("decision"))
    context = _dict(decision.get("context"))
    retest = _dict(request.get("retest"))
    item_list = body.get("item_list") if isinstance(body.get("item_list"), list) else []
    first_item = _dict(item_list[0]) if item_list else {}
    spu_ids = first_item.get("spu_id_list") if isinstance(first_item.get("spu_id_list"), list) else []
    creative_id = str(context.get("creative_id") or first_item.get("item_id") or "").strip()
    item_group_id = str(context.get("item_group_id") or (spu_ids[0] if spu_ids else "")).strip()
    return {
        "creative_id": creative_id or None,
        "item_group_id": item_group_id or None,
        "retest_attempt": int(retest.get("attempt") or 0) or None,
        "retest_cooldown_minutes": int(retest.get("cooldown_minutes") or 0) or None,
        "retest_time_bucket": retest.get("time_bucket") or None,
        "retest_high_quality": _dict(retest.get("quality")).get("high_quality"),
    }


def _load_guard_events(db: Session, scope: Mapping[str, Any], report_date: date) -> list[dict[str, Any]]:
    start_utc, end_utc = _advertiser_day_utc_bounds(
        _required_advertiser_timezone(scope.get("advertiser_timezone")),
        report_date,
    )
    rows = db.execute(
        text(
            """
            select campaign_id, event_type, action, reason, result,
                   cost_cents, gross_revenue_cents, orders, roi,
                   request_json, error_message, created_at
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and created_at >= :start_utc
              and created_at < :end_utc
            order by created_at desc
            """
        ),
        {**scope, "start_utc": start_utc, "end_utc": end_utc},
    ).mappings().all()
    events: list[dict[str, Any]] = []
    for row in rows:
        events.append({
            "campaign_id": row.get("campaign_id"),
            "event_type": row.get("event_type"),
            "action": row.get("action"),
            "reason": row.get("reason"),
            "result": row.get("result"),
            "cost": _money(row.get("cost_cents")),
            "gmv": _money(row.get("gross_revenue_cents")),
            "orders": int(row.get("orders") or 0),
            "roi": _safe_float(row.get("roi")),
            "error": row.get("error_message"),
            "created_at": str(row.get("created_at")),
            **_extract_guard_event_identity(row.get("request_json")),
        })
    return events


def _load_learning_stats(db: Session, scope: Mapping[str, Any], report_date: date) -> list[dict[str, Any]]:
    start_utc, end_utc = _advertiser_day_utc_bounds(
        _required_advertiser_timezone(scope.get("advertiser_timezone")),
        report_date,
    )
    rows = db.execute(
        text(
            """
            select sample_type, action, result, count(*) as samples,
                   avg(nullif(roi, 0)) as avg_positive_roi
            from gmv_hermes_ad_learning_samples
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and observed_at >= :start_utc
              and observed_at < :end_utc
            group by sample_type, action, result
            order by samples desc
            """
        ),
        {**scope, "start_utc": start_utc, "end_utc": end_utc},
    ).mappings().all()
    return [
        {
            "sample_type": row.get("sample_type"),
            "action": row.get("action"),
            "result": row.get("result"),
            "samples": int(row.get("samples") or 0),
            "avg_positive_roi": _safe_float(row.get("avg_positive_roi")),
        }
        for row in rows
    ]


def _build_report_input(
    db: Session,
    scope: Mapping[str, Any],
    report_date: date,
    *,
    overview_refresh: Mapping[str, Any] | None = None,
    report_finalized: bool = False,
    generated_local_at: datetime | None = None,
) -> dict[str, Any]:
    advertiser_timezone = _required_advertiser_timezone(
        scope.get("advertiser_timezone")
    )
    campaigns = _load_campaign_summary(db, scope, report_date)
    overview_summary = _load_overview_summary(db, scope, report_date)
    creatives = _load_creative_summary(db, scope, report_date)
    guard_events = _load_guard_events(db, scope, report_date)
    learning_stats = _load_learning_stats(db, scope, report_date)
    summary, data_quality = _reconcile_report_summary(campaigns, overview_summary)
    data_quality["official_overview_refresh"] = dict(overview_refresh or {})
    data_quality["report_finalized"] = bool(report_finalized)
    data_quality["report_stage"] = "final" if report_finalized else "initial"
    data_quality["generated_local_at"] = (
        generated_local_at.isoformat() if generated_local_at is not None else None
    )
    report_context = build_report_context(
        campaigns=campaigns,
        creatives=creatives,
        guard_events=guard_events,
        learning_stats=learning_stats,
    )
    try:
        shop_orders = order_summary(
            db,
            workspace_id=int(scope["workspace_id"]),
            auth_id=int(scope["auth_id"]),
            advertiser_id=str(scope["advertiser_id"]),
            store_id=str(scope["store_id"]),
            start_date=report_date,
            end_date=report_date,
            advertiser_timezone=advertiser_timezone,
        )
        order_timing_model = order_summary(
            db,
            workspace_id=int(scope["workspace_id"]),
            auth_id=int(scope["auth_id"]),
            advertiser_id=str(scope["advertiser_id"]),
            store_id=str(scope["store_id"]),
            start_date=report_date - timedelta(days=29),
            end_date=report_date,
            advertiser_timezone=advertiser_timezone,
        )
    except Exception:  # noqa: BLE001 - order imports are additive to the ad report.
        logger.exception("Hermes shop order context load failed", extra={"scope": dict(scope)})
        shop_orders = {"order_count": 0, "available": False}
        order_timing_model = {"order_count": 0, "available": False}
    try:
        strategy_memory = load_strategy_memory(
            db,
            scope=scope,
            item_group_ids=report_context.get("product_ids") or [],
        )
    except Exception:  # noqa: BLE001
        logger.exception("Hermes MySQL strategy memory load failed", extra={"scope": dict(scope)})
        strategy_memory = {
            "policy": {
                "fact_source": "gmv_mysql_structured_memory",
                "available": False,
                "candidate_is_actionable": False,
                "validated_is_actionable": True,
            },
            "validated": [],
            "candidates": [],
        }
    payload = {
        "report_date": report_date.isoformat(),
        "scope": {
            "workspace_id": int(scope["workspace_id"]),
            "auth_id": int(scope["auth_id"]),
            "advertiser_id": str(scope["advertiser_id"]),
            "store_id": str(scope["store_id"]),
            "advertiser_timezone": advertiser_timezone,
            "currency": scope.get("currency") or "USD",
        },
        "summary": summary,
        "data_quality": data_quality,
        "strategy_memory": strategy_memory,
        "shop_orders": shop_orders,
        "order_timing_model": order_timing_model,
        "order_data_policy": {
            "pii_included": False,
            "attribution_warning": "Shop orders measure total store demand and are not direct ad attribution.",
            "allowed_use": "Use hourly demand for pacing and cooldown context; keep ROI safeguards authoritative.",
        },
        **report_context,
    }
    payload.setdefault("input_meta", {})["evidence_fingerprint"] = _report_evidence_fingerprint(payload)
    return payload


def _instructions() -> str:
    return (
        "你是资深 TikTok Shop GMV Max 广告优化主管。"
        "请基于输入 JSON 生成中文每日投放复盘报告，并给出可执行、分层且兼顾探索与止损的优化建议。"
        "只输出一个合法 JSON 对象，不要输出解释、前缀、Markdown 代码块或 ```。"
        "JSON 字段必须为 markdown_report、recommendations、risk_flags、next_actions。"
        "markdown_report 用 Markdown，包含：整体表现、计划复盘、素材复盘、系统动作复盘、明日策略。"
        "summary 是广告户官方总览的权威汇总，不得用 campaigns 或 creatives 重新求和替代。"
        "整体表现必须同时写明 cost 总花费与 net_cost 净花费。"
        "若 data_quality.campaign_detail_complete 为 false，必须明确说明计划明细仍在回填，"
        "不得据此做激进的单计划或单素材调整。"
        "若 data_quality.report_finalized 为 false，必须标注为日终初版，后续会按官方日表自动校正。"
        "recommendations 是数组，每项包含 scope、priority、action、reason、expected_impact、safety_limit。"
        "不要建议绕过平台规则；不要夸大结论；样本不足时明确标注低置信度。"
        "涉及自动化调整时，只能建议监控间隔、冷却时长、素材复测、预算/ROAS 的动态安全范围。"
        "guard_event_rollup 的 count 才是动作次数；latest_snapshot 只是该组最后一次累计快照，绝对不能跨事件相加。"
        "guard_event_rollup.material_groups 才能判断同一素材是否反复移除和加回；不同 creative_id 的动作不得描述为同一素材频繁启停。"
        "learning_stats 是重复决策观察次数，不是新增消耗或新增订单，不得据此汇总金额。"
        "必须区分数据质量 HOLD 与真实暂停、重建、素材移除；没有对应动作计数时不得推断发生了重建。"
        "strategy_memory 中只有 VALIDATED 记忆可以影响参数；CANDIDATE 只能作为待验证假设。"
        "shop_orders 是店铺总订单而非广告归因订单；order_timing_model 只用于识别出单高峰、动态投放节奏和冷却时长，不能替代广告 ROI。"
        "高峰建议必须结合样本量与 confidence，样本不足时不得激进加预算。"
        "必须区分三种时间参数：系统监控间隔保持动态 1-5 分钟；计划级暂停冷却通常 30-120 分钟；单素材复测冷却按素材历史动态计算。"
        "不得把单素材复测冷却写成全局监控降频，也不得建议把素材监控改成 24 小时一次。"
        "GMV Max 以计划持续学习、素材级止损为主：单视频高消耗无转化时优先移除该视频而非暂停整个计划；"
        "只有商品卡主导空耗且视频长期无法获得流量，或计划本身异常不消耗时，才建议受控重建。"
    )


def _repair_instructions() -> str:
    return (
        "把输入内容整理为严格合法 JSON。只输出 JSON 对象，不要输出任何解释或代码块。"
        "字段必须为 markdown_report、recommendations、risk_flags、next_actions。"
        "markdown_report 保留原有中文投放复盘。"
        "recommendations 必须是数组；如果原文有建议，请拆成结构化对象，"
        "每项包含 scope、priority、action、reason、expected_impact、safety_limit。"
        "risk_flags 和 next_actions 也必须是数组。"
    )


def _normalize_summary_list(items: Any, *, kind: str) -> list[str]:
    normalized: list[str] = []
    for item in items or []:
        if isinstance(item, Mapping):
            row = dict(item)
            if kind == "risk":
                level = str(row.get("level") or "").strip()
                title = str(row.get("flag") or row.get("title") or "").strip()
                detail = str(row.get("detail") or row.get("reason") or "").strip()
                prefix = f"[{level}] " if level else ""
                value = f"{prefix}{title}：{detail}" if title and detail else f"{prefix}{title or detail}"
            else:
                timing = str(row.get("timing") or row.get("when") or "").strip()
                action = str(row.get("action") or row.get("task") or row.get("detail") or "").strip()
                value = f"{timing}：{action}" if timing and action else timing or action
        else:
            value = str(item).strip()
        if value:
            normalized.append(value[:500])
    return normalized[:50]


def _normalize_report_output(raw: Mapping[str, Any]) -> dict[str, Any]:
    markdown = raw.get("markdown_report")
    if not isinstance(markdown, str) or not markdown.strip():
        return {}
    normalized_recommendations: list[dict[str, str]] = []
    for item in raw.get("recommendations") or []:
        if not isinstance(item, Mapping):
            continue
        normalized_recommendations.append(
            {
                "scope": str(item.get("scope") or "")[:200],
                "priority": str(item.get("priority") or "medium")[:32],
                "action": str(item.get("action") or "")[:500],
                "reason": str(item.get("reason") or "")[:1000],
                "expected_impact": str(item.get("expected_impact") or "")[:500],
                "safety_limit": str(item.get("safety_limit") or "")[:500],
            }
        )
    return {
        "markdown_report": markdown.strip(),
        "recommendations": normalized_recommendations,
        "risk_flags": _normalize_summary_list(raw.get("risk_flags"), kind="risk"),
        "next_actions": _normalize_summary_list(raw.get("next_actions"), kind="action"),
    }


def _initial_report_output(input_payload: Mapping[str, Any]) -> dict[str, Any]:
    summary = _dict(input_payload.get("summary"))
    report_date = str(input_payload.get("report_date") or "")
    markdown = (
        f"# GMV Max 投放日报（日终初版）\n\n"
        f"统计日期：{report_date}\n\n"
        f"- 总花费：${float(summary.get('cost') or 0):.2f}\n"
        f"- 净花费：${float(summary.get('net_cost') or 0):.2f}\n"
        f"- GMV：${float(summary.get('gmv') or 0):.2f}\n"
        f"- 订单：{int(summary.get('orders') or 0)}\n"
        f"- ROAS：{float(summary.get('roi') or 0):.2f}\n\n"
        "官方日表已完成首次同步。系统将在广告主时区 01:30 完成归因校正、Hermes 复盘与次日参数审批。"
    )
    return {
        "markdown_report": markdown,
        "recommendations": [],
        "risk_flags": ["日终数据仍在归因校正窗口"],
        "next_actions": ["01:30 自动最终化并执行 Hermes 决策审批"],
    }


async def _call_hermes(input_payload: Mapping[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    client = HermesAdsReviewClient()
    response, _latency_ms = await client.create_response(
        input_text=_json_dumps(input_payload),
        instructions=_instructions(),
        metadata={
            "source": "gmvmax_hermes_daily_report",
            "prompt_version": "gmvmax_daily_report_v2",
            "report_date": str(input_payload.get("report_date") or ""),
        },
    )
    output_text = extract_output_text(response)
    parsed = _normalize_report_output(_extract_json_object(output_text))
    if not parsed.get("markdown_report"):
        repair_response, _repair_latency_ms = await client.create_response(
            input_text=output_text,
            instructions=_repair_instructions(),
            metadata={
                "source": "gmvmax_hermes_daily_report_json_repair",
                "prompt_version": "gmvmax_daily_report_repair_v2",
                "report_date": str(input_payload.get("report_date") or ""),
            },
        )
        repair_text = extract_output_text(repair_response)
        repaired = _normalize_report_output(_extract_json_object(repair_text))
        if repaired.get("markdown_report"):
            response = {
                "primary_response": response,
                "repair_response": repair_response,
            }
            output_text = repair_text
            parsed = repaired
    if not parsed:
        raise APIError("HERMES_BAD_RESPONSE", "Hermes review output did not match the report schema.", 502)
    return response, output_text, parsed


def _save_report(
    db: Session,
    *,
    scope: Mapping[str, Any],
    report_date: date,
    input_payload: Mapping[str, Any],
    response_payload: Mapping[str, Any] | None,
    output_text: str | None,
    parsed: Mapping[str, Any] | None,
    status: str,
    error_message: str | None = None,
) -> int | None:
    parsed_dict = _dict(parsed)
    response_dict = _dict(response_payload)
    report_markdown = parsed_dict.get("markdown_report") if parsed_dict else None
    if not report_markdown:
        report_markdown = output_text or ""
    recommendations = {
        "recommendations": parsed_dict.get("recommendations") or [],
        "risk_flags": parsed_dict.get("risk_flags") or [],
        "next_actions": parsed_dict.get("next_actions") or [],
    }
    usage = extract_usage(response_dict)
    hermes_response_id = response_dict.get("id") if isinstance(response_dict.get("id"), str) else None
    db.execute(
        text(
            """
            insert into gmv_hermes_ad_daily_reports (
                workspace_id, auth_id, advertiser_id, store_id, report_date,
                advertiser_timezone, report_type, status, input_json, response_json,
                report_markdown, recommendation_json, hermes_response_id,
                prompt_tokens, completion_tokens, total_tokens, error_message,
                created_at, updated_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :report_date,
                :advertiser_timezone, 'DAILY', :status, cast(:input_json as json), cast(:response_json as json),
                :report_markdown, cast(:recommendation_json as json), :hermes_response_id,
                :prompt_tokens, :completion_tokens, :total_tokens, :error_message,
                current_timestamp(6), current_timestamp(6)
            )
            on duplicate key update
                advertiser_timezone=values(advertiser_timezone),
                status=values(status),
                input_json=values(input_json),
                response_json=values(response_json),
                report_markdown=values(report_markdown),
                recommendation_json=values(recommendation_json),
                hermes_response_id=values(hermes_response_id),
                prompt_tokens=values(prompt_tokens),
                completion_tokens=values(completion_tokens),
                total_tokens=values(total_tokens),
                error_message=values(error_message),
                decision_status=null,
                decision_attempts=0,
                decision_last_attempt_at=null,
                decision_error_message=null,
                updated_at=current_timestamp(6)
            """
        ),
        {
            "workspace_id": int(scope["workspace_id"]),
            "auth_id": int(scope["auth_id"]),
            "advertiser_id": str(scope["advertiser_id"]),
            "store_id": str(scope["store_id"]),
            "report_date": report_date,
            "advertiser_timezone": _required_advertiser_timezone(
                scope.get("advertiser_timezone")
            ),
            "status": status,
            "input_json": _json_dumps(input_payload),
            "response_json": _json_dumps(response_dict),
            "report_markdown": str(report_markdown or ""),
            "recommendation_json": _json_dumps(recommendations),
            "hermes_response_id": hermes_response_id,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "error_message": error_message,
        },
    )
    row = db.execute(
        text(
            """
            select id
            from gmv_hermes_ad_daily_reports
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and report_date=:report_date
              and report_type='DAILY'
            limit 1
            """
        ),
        {
            "workspace_id": int(scope["workspace_id"]),
            "auth_id": int(scope["auth_id"]),
            "advertiser_id": str(scope["advertiser_id"]),
            "store_id": str(scope["store_id"]),
            "report_date": report_date,
        },
    ).mappings().first()
    return int(row["id"]) if row else None


async def run_hermes_daily_report_cycle(
    db: Session,
    *,
    report_date: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    ensure_hermes_daily_report_table(db)
    scopes = _load_report_scopes(db)
    summary = {
        "scopes": len(scopes),
        "generated": 0,
        "skipped": 0,
        "deferred": 0,
        "errors": 0,
        "decision_approved": 0,
        "decision_errors": 0,
        "decision_retried": 0,
        "decision_pending": 0,
        "memory_evaluated": 0,
        "memory_refreshed": 0,
        "memory_errors": 0,
        "overview_refreshed": 0,
        "overview_refresh_errors": 0,
        "initial_reports": 0,
        "finalized_reports": 0,
    }
    for scope in scopes:
        try:
            advertiser_timezone = _required_advertiser_timezone(
                scope.get("advertiser_timezone")
            )
        except Exception as exc:  # noqa: BLE001 - isolate invalid account metadata.
            summary["errors"] += 1
            logger.error(
                "Hermes daily report skipped: advertiser timezone unavailable",
                extra={"scope": dict(scope), "error": str(exc)},
            )
            continue
        scope = {**scope, "advertiser_timezone": advertiser_timezone}
        scope_report_date = _scope_report_date(advertiser_timezone, report_date)
        ready, ready_reason, local_now = _report_generation_ready(
            advertiser_timezone,
            scope_report_date,
        )
        if not force and not ready:
            summary["deferred"] += 1
            logger.info(
                "Hermes daily report deferred",
                extra={
                    "scope": dict(scope),
                    "report_date": scope_report_date.isoformat(),
                    "reason": ready_reason,
                    "advertiser_local_time": local_now.isoformat(),
                },
            )
            continue
        existing = db.execute(
            text(
                """
                select id, input_json, decision_status, decision_attempts,
                       decision_last_attempt_at
                from gmv_hermes_ad_daily_reports
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and report_date=:report_date
                  and report_type='DAILY'
                  and status='GENERATED'
                limit 1
                """
            ),
            {**scope, "report_date": scope_report_date},
        ).mappings().first()
        if not force and existing and _existing_report_finalized(existing):
            decision_result = await _run_tracked_report_decision(
                db,
                report_id=int(existing["id"]),
            )
            if decision_result.get("attempted"):
                summary["decision_retried"] += 1
            if decision_result.get("error"):
                summary["decision_errors"] += 1
            elif decision_result.get("attempted"):
                summary["decision_approved"] += int(decision_result.get("approved") or 0)
            elif str(decision_result.get("status") or "") == "not_due":
                summary["decision_pending"] += 1
            summary["skipped"] += 1
            continue

        try:
            overview_refresh = await _refresh_official_overview(db, scope, scope_report_date)
            db.commit()
            summary["overview_refreshed"] += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            ensure_hermes_daily_report_table(db)
            overview_refresh = {"status": "failed", "error": str(exc)[:500]}
            summary["overview_refresh_errors"] += 1
            logger.exception(
                "Hermes daily report official overview refresh failed",
                extra={"scope": dict(scope), "report_date": scope_report_date.isoformat()},
            )
            if existing and not force:
                summary["deferred"] += 1
                continue

        report_finalized = (
            _report_final_cutoff_reached(local_now, force=force)
            and overview_refresh.get("status") == "success"
        )
        input_payload = _build_report_input(
            db,
            scope,
            scope_report_date,
            overview_refresh=overview_refresh,
            report_finalized=report_finalized,
            generated_local_at=local_now,
        )
        if input_payload.get("summary", {}).get("summary_source") != "overview_daily":
            report_finalized = False
            input_payload["data_quality"]["report_finalized"] = False
            input_payload["data_quality"]["report_stage"] = "initial"

        if existing and not force:
            if not report_finalized:
                summary["skipped"] += 1
                continue
        try:
            memory_evaluation = record_policy_evaluations(
                db,
                scope=scope,
                report_date=scope_report_date,
                report_input=input_payload,
            )
            memory_refresh = refresh_strategy_memory(db, scope=scope)
            input_payload["strategy_memory"] = load_strategy_memory(
                db,
                scope=scope,
                item_group_ids=input_payload.get("product_ids") or [],
            )
            db.commit()
            summary["memory_evaluated"] += int(memory_evaluation.get("evaluated") or 0)
            summary["memory_refreshed"] += int(memory_refresh.get("refreshed") or 0)
        except Exception:  # noqa: BLE001
            db.rollback()
            summary["memory_errors"] += 1
            logger.exception("Hermes MySQL strategy memory refresh failed", extra={"scope": dict(scope)})
        if (
            float(input_payload.get("summary", {}).get("cost") or 0) <= 0
            and not input_payload.get("guard_events")
            and not input_payload.get("learning_stats")
            and int(input_payload.get("shop_orders", {}).get("order_count") or 0) <= 0
        ):
            _save_report(
                db,
                scope=scope,
                report_date=scope_report_date,
                input_payload=input_payload,
                response_payload={},
                output_text="无投放数据，未调用 Hermes。",
                parsed={"markdown_report": "无投放数据，未调用 Hermes。", "recommendations": []},
                status="SKIPPED_EMPTY",
            )
            db.commit()
            summary["skipped"] += 1
            continue
        try:
            if report_finalized:
                response_payload, output_text, parsed = await _call_hermes(input_payload)
            else:
                response_payload = {}
                parsed = _initial_report_output(input_payload)
                output_text = str(parsed.get("markdown_report") or "")
            report_id = _save_report(
                db,
                scope=scope,
                report_date=scope_report_date,
                input_payload=input_payload,
                response_payload=response_payload,
                output_text=output_text,
                parsed=parsed,
                status="GENERATED",
            )
            db.commit()
            summary["generated"] += 1
            if report_finalized:
                summary["finalized_reports"] += 1
            else:
                summary["initial_reports"] += 1
            if report_id:
                try:
                    link_policy_evaluations_to_report(
                        db,
                        scope=scope,
                        report_date=scope_report_date,
                        report_id=int(report_id),
                    )
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                    summary["memory_errors"] += 1
                    logger.exception(
                        "Hermes policy evaluation report link failed",
                        extra={"scope": dict(scope), "report_id": report_id},
                    )
                if report_finalized:
                    decision_result = await _run_tracked_report_decision(
                        db,
                        report_id=int(report_id),
                        force=True,
                    )
                    summary["decision_approved"] += int(
                        decision_result.get("approved") or 0
                    )
                    if decision_result.get("error"):
                        summary["decision_errors"] += 1
        except APIError as exc:
            db.rollback()
            ensure_hermes_daily_report_table(db)
            _save_report(
                db,
                scope=scope,
                report_date=scope_report_date,
                input_payload=input_payload,
                response_payload={},
                output_text="",
                parsed={},
                status="FAILED",
                error_message=str(exc),
            )
            db.commit()
            summary["errors"] += 1
            logger.exception("Hermes daily report failed", extra={"scope": dict(scope)})
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            ensure_hermes_daily_report_table(db)
            _save_report(
                db,
                scope=scope,
                report_date=scope_report_date,
                input_payload=input_payload,
                response_payload={},
                output_text="",
                parsed={},
                status="FAILED",
                error_message=str(exc),
            )
            db.commit()
            summary["errors"] += 1
            logger.exception("GMV Max daily report failed", extra={"scope": dict(scope)})
    return summary


def run_hermes_daily_report_cycle_sync(
    db: Session,
    *,
    report_date: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return asyncio.run(run_hermes_daily_report_cycle(db, report_date=report_date, force=force))
