"""
Endpoints for retrieving meta data (business centers, advertisers, stores,
and products) associated with a TikTok Business tenant.  These routes
provide paginated and filtered listings and handle automatic backfilling
when data is missing.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple, Set
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, bindparam, or_, func, select, text
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_tenant_member
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBAdvertiserStoreLink,
    TTBBCAdvertiserLink,
    TTBProduct,
    TTBProductAdvertiserEligibility,
)
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)

from . import common

# Import identifier normalization utility from the original sync module
from app.services.ttb_sync import _normalize_identifier


# Subrouter for meta‑related endpoints.  Paths are defined relative to the
# tenant prefix configured in __init__.py.
router = APIRouter()

def _money_from_cents(value: Any) -> float:
    try:
        cents = int(value or 0)
    except (TypeError, ValueError):
        cents = 0
    return round(cents / 100, 2)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _isoformat_utc(value: Any) -> Optional[str]:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    text_value = parsed.isoformat()
    if parsed.tzinfo is not None:
        return text_value
    return f'{text_value}Z'


def _advertiser_date_window_utc(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    start_date: Optional[date],
    end_date: Optional[date],
) -> tuple[Optional[datetime], Optional[datetime]]:
    row = (
        db.query(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .filter(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .first()
    )
    timezone_name = (row.display_timezone or row.timezone) if row else None
    try:
        advertiser_timezone = ZoneInfo(str(timezone_name)) if timezone_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        advertiser_timezone = timezone.utc

    start_utc = None
    if start_date is not None:
        start_utc = datetime.combine(start_date, time.min, advertiser_timezone).astimezone(
            timezone.utc
        ).replace(tzinfo=None)

    end_utc_exclusive = None
    if end_date is not None:
        end_utc_exclusive = datetime.combine(
            end_date + timedelta(days=1),
            time.min,
            advertiser_timezone,
        ).astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc_exclusive


def _load_product_automation_stats(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: Optional[str],
    store_id: str,
    product_ids: List[str],
    stats_start_date: Optional[date] = None,
    stats_end_date: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    clean_ids = sorted({str(item).strip() for item in product_ids if str(item or '').strip()})
    if not clean_ids or not advertiser_id:
        return {}

    stats_start_utc, stats_end_utc_exclusive = _advertiser_date_window_utc(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        start_date=stats_start_date,
        end_date=stats_end_date,
    )
    rows = db.execute(
        text(
            """
            with managed_campaigns as (
                select distinct
                    ig.item_group_id as product_id,
                    ig.campaign_id,
                    c.campaign_name,
                    c.operation_status as catalog_status,
                    greatest(
                        coalesce(c.list_synced_at, '1970-01-01 00:00:00'),
                        coalesce(c.detail_synced_at, '1970-01-01 00:00:00'),
                        coalesce(c.updated_at, '1970-01-01 00:00:00')
                    ) as catalog_observed_at,
                    c.create_time_utc,
                    c.created_at,
                    c.updated_at,
                    s.id as strategy_id,
                    s.enabled as strategy_enabled,
                    s.config_json as strategy_config_json,
                    r.runtime_json as runtime_json,
                    r.operation_status as realtime_status,
                    coalesce(r.last_report_at, r.last_checked_at, r.updated_at) as realtime_observed_at,
                    case
                        when r.operation_status is not null
                         and coalesce(r.last_report_at, r.last_checked_at, r.updated_at)
                             >= greatest(
                                 coalesce(c.list_synced_at, '1970-01-01 00:00:00'),
                                 coalesce(c.detail_synced_at, '1970-01-01 00:00:00'),
                                 coalesce(c.updated_at, '1970-01-01 00:00:00')
                             )
                        then r.operation_status
                        else c.operation_status
                    end as effective_operation_status,
                    r.guard_status,
                    r.last_action,
                    r.last_reason,
                    r.paused_until,
                    r.last_checked_at
                from gmvmax_product_campaign_item_groups ig
                join gmvmax_product_campaign_catalog c
                  on c.workspace_id=ig.workspace_id
                 and c.auth_id=ig.auth_id
                 and c.advertiser_id=ig.advertiser_id
                 and c.store_id=ig.store_id
                 and c.campaign_id=ig.campaign_id
                left join gmv_strategy_configs s
                  on s.workspace_id=ig.workspace_id
                 and s.auth_id=ig.auth_id
                 and s.campaign_id=ig.campaign_id
                left join gmv_campaign_realtime_state r
                  on r.workspace_id=ig.workspace_id
                 and r.auth_id=ig.auth_id
                 and r.advertiser_id=ig.advertiser_id
                 and r.store_id=ig.store_id
                 and r.campaign_id=ig.campaign_id
                where ig.workspace_id=:workspace_id
                  and ig.auth_id=:auth_id
                  and ig.advertiser_id=:advertiser_id
                  and ig.store_id=:store_id
                  and ig.item_group_id in :product_ids
                  and (s.id is not null or r.strategy_id is not null)
            ), latest_managed as (
                select *
                from (
                    select
                        mc.*,
                        row_number() over (
                            partition by mc.product_id
                            order by
                                coalesce(mc.create_time_utc, mc.created_at) desc,
                                mc.updated_at desc,
                                mc.campaign_id desc
                        ) as row_num
                    from managed_campaigns mc
                ) ranked
                where ranked.row_num=1
            ), product_daily as (
                select
                    m.item_group_id as product_id,
                    m.campaign_id,
                    m.stat_time_day as metric_date,
                    coalesce(m.cost_cents, 0) as spend_cents,
                    coalesce(m.gross_revenue_cents, 0) as gross_revenue_cents,
                    coalesce(m.orders, 0) as orders,
                    m.source_observed_at
                from gmv_product_metrics_daily m
                join latest_managed lm
                  on lm.product_id=m.item_group_id
                 and lm.campaign_id=m.campaign_id
                where m.workspace_id=:workspace_id
                  and m.auth_id=:auth_id
                  and m.advertiser_id=:advertiser_id
                  and m.store_id=:store_id
                  and (:stats_start_date is null or m.stat_time_day >= :stats_start_date)
                  and (:stats_end_date is null or m.stat_time_day <= :stats_end_date)
            ), product_hourly as (
                select
                    m.item_group_id as product_id,
                    m.campaign_id,
                    date(m.stat_time_hour) as metric_date,
                    sum(coalesce(m.cost_cents, 0)) as spend_cents,
                    sum(coalesce(m.gross_revenue_cents, 0)) as gross_revenue_cents,
                    sum(coalesce(m.orders, 0)) as orders
                from gmv_product_metrics_hourly m
                join latest_managed lm
                  on lm.product_id=m.item_group_id
                 and lm.campaign_id=m.campaign_id
                where m.workspace_id=:workspace_id
                  and m.auth_id=:auth_id
                  and m.advertiser_id=:advertiser_id
                  and m.store_id=:store_id
                  and (:stats_start_date is null or date(m.stat_time_hour) >= :stats_start_date)
                  and (:stats_end_date is null or date(m.stat_time_hour) <= :stats_end_date)
                group by m.item_group_id, m.campaign_id, date(m.stat_time_hour)
            ), metric_by_day as (
                select
                    d.product_id, d.campaign_id, d.metric_date,
                    d.spend_cents, d.gross_revenue_cents, d.orders
                from product_daily d
                where d.source_observed_at is not null
                union all
                select
                    h.product_id, h.campaign_id, h.metric_date,
                    h.spend_cents, h.gross_revenue_cents, h.orders
                from product_hourly h
                where not exists (
                      select 1
                      from product_daily d
                      where d.product_id=h.product_id
                        and d.campaign_id=h.campaign_id
                        and d.metric_date=h.metric_date
                        and d.source_observed_at is not null
                  )
            ), metric_totals as (
                select
                    product_id,
                    sum(coalesce(spend_cents, 0)) as spend_cents,
                    sum(coalesce(gross_revenue_cents, 0)) as gross_revenue_cents,
                    sum(coalesce(orders, 0)) as orders,
                    max(metric_date) as latest_metric_date
                from metric_by_day
                group by product_id
            ), event_totals as (
                select
                    mc.product_id,
                    count(*) as guard_event_count,
                    sum(case when e.result='SUCCESS' and e.action='PAUSE' then 1 else 0 end) as pause_count,
                    sum(case when e.result='SUCCESS' and e.action in ('START', 'RESUME') then 1 else 0 end) as resume_count,
                    sum(case when e.result='SUCCESS' and e.action='RESET_CAMPAIGN' then 1 else 0 end) as reset_count,
                    sum(case when e.result='SUCCESS' and e.action='REMOVE' then 1 else 0 end) as creative_exclude_count
                from managed_campaigns mc
                join gmv_campaign_guard_events e
                  on e.workspace_id=:workspace_id
                 and e.auth_id=:auth_id
                 and e.advertiser_id=:advertiser_id
                 and e.store_id=:store_id
                 and e.campaign_id=mc.campaign_id
                where (:stats_start_utc is null or e.created_at >= :stats_start_utc)
                  and (:stats_end_utc_exclusive is null or e.created_at < :stats_end_utc_exclusive)
                group by mc.product_id
            ), latest_events as (
                select product_id, action, reason, result, created_at
                from (
                    select
                        mc.product_id,
                        e.action,
                        e.reason,
                        e.result,
                        e.created_at,
                        row_number() over (
                            partition by mc.product_id
                            order by e.created_at desc, e.id desc
                        ) as row_num
                    from managed_campaigns mc
                    join gmv_campaign_guard_events e
                      on e.workspace_id=:workspace_id
                     and e.auth_id=:auth_id
                     and e.advertiser_id=:advertiser_id
                     and e.store_id=:store_id
                     and e.campaign_id=mc.campaign_id
                    where (:stats_start_utc is null or e.created_at >= :stats_start_utc)
                      and (:stats_end_utc_exclusive is null or e.created_at < :stats_end_utc_exclusive)
                ) ranked
                where ranked.row_num=1
            ), latest_control_events as (
                select product_id, action, reason, result, created_at
                from (
                    select
                        lm.product_id,
                        e.action,
                        e.reason,
                        e.result,
                        e.created_at,
                        row_number() over (
                            partition by lm.product_id
                            order by e.created_at desc, e.id desc
                        ) as row_num
                    from latest_managed lm
                    join gmv_campaign_guard_events e
                      on e.workspace_id=:workspace_id
                     and e.auth_id=:auth_id
                     and e.advertiser_id=:advertiser_id
                     and e.store_id=:store_id
                     and e.campaign_id=lm.campaign_id
                    where e.event_type='SMART_GUARD'
                      and e.action in ('PAUSE', 'START', 'RESUME')
                ) ranked
                where ranked.row_num=1
            )
            select
                mc.product_id,
                count(distinct case
                    when (:stats_start_utc is null or coalesce(mc.create_time_utc, mc.created_at) >= :stats_start_utc)
                     and (:stats_end_utc_exclusive is null or coalesce(mc.create_time_utc, mc.created_at) < :stats_end_utc_exclusive)
                    then mc.campaign_id end
                ) as campaign_count,
                count(distinct mc.campaign_id) as lifetime_campaign_count,
                count(distinct case when mc.effective_operation_status='ENABLE' then mc.campaign_id end) as active_campaign_count,
                count(distinct case when coalesce(mc.strategy_enabled, 0)=1 then mc.campaign_id end) as strategy_enabled_campaign_count,
                min(coalesce(mc.create_time_utc, mc.created_at)) as first_campaign_created_at,
                max(coalesce(mc.create_time_utc, mc.created_at)) as latest_campaign_created_at,
                max(lm.campaign_id) as latest_campaign_id,
                max(lm.campaign_name) as latest_campaign_name,
                max(lm.strategy_enabled) as latest_strategy_enabled,
                max(cast(lm.strategy_config_json as char)) as latest_strategy_config_json,
                max(cast(lm.runtime_json as char)) as latest_runtime_json,
                max(lm.realtime_status) as latest_realtime_status,
                max(lm.catalog_status) as latest_catalog_status,
                max(lm.effective_operation_status) as latest_effective_status,
                max(lm.last_action) as last_action,
                max(lm.last_reason) as last_reason,
                max(lm.paused_until) as paused_until,
                max(lm.last_checked_at) as last_checked_at,
                coalesce(max(mt.spend_cents), 0) as spend_cents,
                coalesce(max(mt.gross_revenue_cents), 0) as gross_revenue_cents,
                coalesce(max(mt.orders), 0) as orders,
                max(mt.latest_metric_date) as latest_metric_date,
                coalesce(max(et.guard_event_count), 0) as guard_event_count,
                coalesce(max(et.pause_count), 0) as pause_count,
                coalesce(max(et.resume_count), 0) as resume_count,
                coalesce(max(et.reset_count), 0) as reset_count,
                coalesce(max(et.creative_exclude_count), 0) as creative_exclude_count,
                max(le.action) as latest_event_action,
                max(le.reason) as latest_event_reason,
                max(le.result) as latest_event_result,
                max(le.created_at) as latest_event_at,
                max(lce.action) as latest_control_action,
                max(lce.reason) as latest_control_reason,
                max(lce.result) as latest_control_result,
                max(lce.created_at) as latest_control_at
            from managed_campaigns mc
            left join latest_managed lm on lm.product_id=mc.product_id
            left join metric_totals mt on mt.product_id=mc.product_id
            left join event_totals et on et.product_id=mc.product_id
            left join latest_events le on le.product_id=mc.product_id
            left join latest_control_events lce on lce.product_id=mc.product_id
            group by mc.product_id
            """
        ).bindparams(bindparam('product_ids', expanding=True)),
        {
            'workspace_id': int(workspace_id),
            'auth_id': int(auth_id),
            'advertiser_id': str(advertiser_id),
            'store_id': str(store_id),
            'product_ids': clean_ids,
            'stats_start_date': stats_start_date,
            'stats_end_date': stats_end_date,
            'stats_start_utc': stats_start_utc,
            'stats_end_utc_exclusive': stats_end_utc_exclusive,
        },
    ).mappings().all()

    stats: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        product_id = str(row.get('product_id') or '')
        raw_strategy_config = row.get('latest_strategy_config_json')
        if isinstance(raw_strategy_config, str):
            try:
                strategy_config = json.loads(raw_strategy_config)
            except (TypeError, ValueError):
                strategy_config = {}
        elif isinstance(raw_strategy_config, dict):
            strategy_config = raw_strategy_config
        else:
            strategy_config = {}
        raw_runtime = row.get('latest_runtime_json')
        if isinstance(raw_runtime, str):
            try:
                runtime_state = json.loads(raw_runtime)
            except (TypeError, ValueError):
                runtime_state = {}
        elif isinstance(raw_runtime, dict):
            runtime_state = raw_runtime
        else:
            runtime_state = {}
        smart_guard = strategy_config.get('smart_guard') or strategy_config.get('smartGuard') or {}
        creative_guard = strategy_config.get('creative_guard') or strategy_config.get('creativeGuard') or {}
        smart_state = (
            runtime_state.get('smart_guard_state')
            or strategy_config.get('smart_guard_state')
            or strategy_config.get('smartGuardState')
            or {}
        )
        controlled_test = smart_state.get('controlled_test') if isinstance(smart_state, dict) else {}
        controlled_test = controlled_test if isinstance(controlled_test, dict) else {}
        last_decision = smart_state.get('last_decision') if isinstance(smart_state, dict) else {}
        last_decision = last_decision if isinstance(last_decision, dict) else {}
        hermes_review = last_decision.get('hermes_review') if isinstance(last_decision, dict) else {}
        hermes_review = hermes_review if isinstance(hermes_review, dict) else {}
        threshold_context = last_decision.get('threshold_context') if isinstance(last_decision, dict) else {}
        threshold_context = threshold_context if isinstance(threshold_context, dict) else {}
        data_consistency = threshold_context.get('data_consistency') if isinstance(threshold_context, dict) else {}
        data_consistency = data_consistency if isinstance(data_consistency, dict) else {}
        effective_prices = (
            smart_guard.get('product_effective_prices')
            or smart_guard.get('productEffectivePrices')
            or creative_guard.get('product_effective_prices')
            or creative_guard.get('productEffectivePrices')
            or {}
        )
        reference_price = effective_prices.get(product_id) if isinstance(effective_prices, dict) else None
        daily_cap_cents = smart_guard.get('daily_spend_cap_cents')
        if daily_cap_cents is None:
            daily_cap_cents = smart_guard.get('dailySpendCapCents')
        spend_cents = int(row.get('spend_cents') or 0)
        revenue_cents = int(row.get('gross_revenue_cents') or 0)
        orders = int(row.get('orders') or 0)
        roas = round(revenue_cents / spend_cents, 4) if spend_cents > 0 else None
        paused_until_value = row.get('paused_until')
        paused_until_dt = _parse_datetime(paused_until_value)
        next_start_at = None
        next_start_reason = None
        if bool(row.get('latest_strategy_enabled')) and paused_until_dt is not None:
            compare_now = datetime.utcnow() if paused_until_dt.tzinfo is None else datetime.now(paused_until_dt.tzinfo)
            if paused_until_dt > compare_now:
                next_start_at = _isoformat_utc(paused_until_dt)
                next_start_reason = row.get('last_reason') or row.get('latest_control_reason') or row.get('latest_event_reason')
        next_review_at = next_start_at
        next_review_reason = next_start_reason
        if bool(controlled_test.get('active')):
            controlled_review_at = _parse_datetime(controlled_test.get('review_at'))
            if controlled_review_at is not None:
                next_review_at = _isoformat_utc(controlled_review_at)
            next_review_reason = 'Hermes 小预算测试到达预算或观察窗口后自动复核增量效果'
        resume_condition = None
        consistency_state = str(data_consistency.get('state') or '')
        if consistency_state == 'conflict':
            resume_condition = '等待系列、商品、素材和订单数据完成一致性校验'
        elif str(hermes_review.get('decision') or '').upper() == 'HOLD':
            resume_condition = '等待 Hermes 获得足够且一致的证据后重新审批'
        elif bool(controlled_test.get('active')):
            resume_condition = '受控测试中，仅按本次恢复后的新增花费、成交和 ROAS 判定'
        elif next_review_at:
            resume_condition = '到达复核时间后重新拉取实时数据，满足收益与风险条件才会恢复'
        latest_event = None
        if row.get('latest_event_at'):
            latest_event = {
                'action': row.get('latest_event_action'),
                'reason': row.get('latest_event_reason'),
                'result': row.get('latest_event_result'),
                'created_at': row.get('latest_event_at').isoformat() if row.get('latest_event_at') else None,
            }
        latest_control_event = None
        if row.get('latest_control_at'):
            latest_control_event = {
                'action': row.get('latest_control_action'),
                'reason': row.get('latest_control_reason'),
                'result': row.get('latest_control_result'),
                'created_at': row.get('latest_control_at').isoformat() if row.get('latest_control_at') else None,
            }
        stats[product_id] = {
            'enabled': bool(row.get('latest_strategy_enabled')),
            'strategy_enabled': bool(row.get('latest_strategy_enabled')),
            # Select the freshest official observation.  Catalog is refreshed by
            # the 10-minute campaign sync while realtime state is refreshed by the
            # one-minute guard; neither source is unconditionally authoritative.
            'campaign_operation_status': (
                row.get('latest_effective_status')
                or row.get('latest_catalog_status')
                or row.get('latest_realtime_status')
            ),
            'metric_scope': 'LATEST_CAMPAIGN_PRODUCT',
            'reference_price': float(reference_price) if reference_price is not None else None,
            'daily_spend_cap': _money_from_cents(daily_cap_cents) if daily_cap_cents is not None else None,
            'daily_spend_cap_cents': int(daily_cap_cents) if daily_cap_cents is not None else None,
            'spend': _money_from_cents(spend_cents),
            'gross_revenue': _money_from_cents(revenue_cents),
            'gmv': _money_from_cents(revenue_cents),
            'orders': orders,
            'roas': roas,
            'campaign_count': int(row.get('campaign_count') or 0),
            'lifetime_campaign_count': int(row.get('lifetime_campaign_count') or 0),
            'active_campaign_count': int(row.get('active_campaign_count') or 0),
            'strategy_enabled_campaign_count': int(row.get('strategy_enabled_campaign_count') or 0),
            'pause_count': int(row.get('pause_count') or 0),
            'resume_count': int(row.get('resume_count') or 0),
            'reset_count': int(row.get('reset_count') or 0),
            'creative_exclude_count': int(row.get('creative_exclude_count') or 0),
            'guard_event_count': int(row.get('guard_event_count') or 0),
            'latest_campaign_id': row.get('latest_campaign_id'),
            'latest_campaign_name': row.get('latest_campaign_name'),
            'first_campaign_created_at': row.get('first_campaign_created_at').isoformat() if row.get('first_campaign_created_at') else None,
            'latest_campaign_created_at': row.get('latest_campaign_created_at').isoformat() if row.get('latest_campaign_created_at') else None,
            'latest_metric_date': row.get('latest_metric_date').isoformat() if row.get('latest_metric_date') else None,
            'last_action': row.get('last_action'),
            'last_reason': row.get('last_reason'),
            'paused_until': row.get('paused_until').isoformat() if row.get('paused_until') else None,
            'next_automation_start_at': next_start_at,
            'next_automation_start_reason': next_start_reason,
            'next_automation_start_source': 'smart_guard.paused_until' if next_start_at else None,
            'next_automation_review_at': next_review_at,
            'next_automation_review_reason': next_review_reason,
            'next_automation_review_source': (
                'smart_guard.controlled_test.review_at'
                if bool(controlled_test.get('active'))
                else ('smart_guard.paused_until' if next_review_at else None)
            ),
            'resume_is_conditional': bool(next_review_at),
            'resume_condition': resume_condition,
            'decision_phase': last_decision.get('decision_phase'),
            'data_consistency': data_consistency or None,
            'hermes_action_review': hermes_review or None,
            'controlled_test': {
                'active': bool(controlled_test.get('active')),
                'status': controlled_test.get('status'),
                'stage': controlled_test.get('stage'),
                'failure_class': controlled_test.get('failure_class'),
                'rebuild_pending': bool(controlled_test.get('rebuild_pending')),
                'budget': _money_from_cents(controlled_test.get('budget_cents')),
                'budget_cents': int(controlled_test.get('budget_cents') or 0),
                'spent': _money_from_cents(controlled_test.get('spent_cents')),
                'spent_cents': int(controlled_test.get('spent_cents') or 0),
                'remaining': _money_from_cents(controlled_test.get('remaining_cents')),
                'remaining_cents': int(controlled_test.get('remaining_cents') or 0),
                'orders': int(controlled_test.get('orders') or 0),
                'roas': float(controlled_test.get('roi')) if controlled_test.get('roi') is not None else None,
                'attempt_count': int(controlled_test.get('attempt_count') or 0),
                'no_delivery_count': int(controlled_test.get('no_delivery_count') or 0),
                'performance_failure_count': int(controlled_test.get('performance_failure_count') or 0),
                'budget_owner': controlled_test.get('budget_owner'),
                'started_at': controlled_test.get('started_at'),
                'review_at': controlled_test.get('review_at'),
            } if controlled_test else None,
            'last_checked_at': row.get('last_checked_at').isoformat() if row.get('last_checked_at') else None,
            'latest_event': latest_event,
            'latest_control_event': latest_control_event,
            'date_range': {
                'start_date': stats_start_date.isoformat() if stats_start_date else None,
                'end_date': stats_end_date.isoformat() if stats_end_date else None,
            },
        }
    return stats


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/business-centers",
    response_model=common.BusinessCenterList,
)
def list_account_business_centers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.BusinessCenterList:
    """Return all business centers for the given account."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    query = (
        db.query(TTBBusinessCenter)
        .filter(TTBBusinessCenter.workspace_id == int(workspace_id))
        .filter(TTBBusinessCenter.auth_id == int(auth_id))
    )
    rows = query.order_by(TTBBusinessCenter.name.asc(), TTBBusinessCenter.bc_id.asc()).all()
    items = [common.BusinessCenterItem(**common._serialize_bc(r)) for r in rows]
    return common.BusinessCenterList(items=items)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/advertisers",
    response_model=common.AdvertiserList,
)
def list_account_advertisers(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.AdvertiserList:
    """Return advertisers linked to the given account, optionally filtered by BC."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    # Legacy bc_id parameter is no longer supported
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    query = (
        db.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
    )
    normalized_owner = _normalize_identifier(owner_bc_id)
    if normalized_owner:
        link_subquery = (
            db.query(TTBBCAdvertiserLink.advertiser_id)
            .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
            .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
            .filter(TTBBCAdvertiserLink.bc_id == normalized_owner)
        )
        query = query.filter(
            or_(TTBAdvertiser.advertiser_id.in_(link_subquery), TTBAdvertiser.bc_id == normalized_owner)
    )
    rows = query.order_by(TTBAdvertiser.display_name.asc(), TTBAdvertiser.advertiser_id.asc()).all()
    advertiser_ids = [str(row.advertiser_id) for row in rows if row and row.advertiser_id]
    bc_hints: Dict[str, Tuple[int, str]] = {}
    if advertiser_ids:
        link_rows = (
            db.query(
                TTBBCAdvertiserLink.advertiser_id,
                TTBBCAdvertiserLink.bc_id,
                TTBBCAdvertiserLink.relation_type,
            )
            .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
            .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
            .filter(TTBBCAdvertiserLink.advertiser_id.in_(advertiser_ids))
            .all()
        )
        for adv_id, bc_id, relation_type in link_rows:
            if not adv_id or not bc_id:
                continue
            key = str(adv_id)
            rank = common._relation_rank(relation_type)
            existing = bc_hints.get(key)
            if existing is None or rank < existing[0]:
                bc_hints[key] = (rank, str(bc_id))
    items: List[common.AdvertiserItem] = []
    for row in rows:
        payload = common._serialize_adv(row)
        adv_id = payload.get("advertiser_id")
        if adv_id:
            hint = bc_hints.get(str(adv_id))
            if hint and not payload.get("bc_id"):
                payload["bc_id"] = hint[1]
        items.append(common.AdvertiserItem(**payload))
    return common.AdvertiserList(items=items)


def _build_account_store_list(
    db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str
) -> common.StoreList:
    """Build a list of stores linked to the given advertiser for the account."""
    normalized_adv = _normalize_identifier(advertiser_id)
    if not normalized_adv:
        return common.StoreList(items=[])
    link_rows = (
        db.query(
            TTBAdvertiserStoreLink.store_id,
            TTBAdvertiserStoreLink.relation_type,
            TTBAdvertiserStoreLink.store_authorized_bc_id,
            TTBAdvertiserStoreLink.bc_id_hint,
        )
        .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
        .filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)
        .all()
    )
    linked_store_ids = {str(store_id) for store_id, *_ in link_rows if store_id}
    if not linked_store_ids:
        return common.StoreList(items=[])
    store_rows: List[TTBStore] = (
        db.query(TTBStore)
        .filter(TTBStore.workspace_id == int(workspace_id))
        .filter(TTBStore.auth_id == int(auth_id))
        .filter(TTBStore.store_id.in_(linked_store_ids))
        .all()
    )
    best_link_by_store: Dict[str, Dict[str, Any]] = {}
    for store_id, relation_type, store_authorized_bc_id, bc_id_hint in link_rows:
        sid = str(store_id)
        current = best_link_by_store.get(sid)
        if (
            current is None
            or common._relation_rank(relation_type) < common._relation_rank(current.get("relation_type"))
        ):
            best_link_by_store[sid] = {
                "relation_type": relation_type,
                "store_authorized_bc_id": store_authorized_bc_id,
                "bc_id_hint": bc_id_hint,
            }
    items: List[Dict[str, Any]] = []
    for row in store_rows:
        payload = common._serialize_store(row)
        payload["advertiser_id"] = normalized_adv
        link_info = best_link_by_store.get(str(row.store_id)) or {}
        if not payload.get("store_authorized_bc_id") and link_info.get("store_authorized_bc_id"):
            payload["store_authorized_bc_id"] = link_info["store_authorized_bc_id"]
        if not payload.get("bc_id") and link_info.get("bc_id_hint"):
            payload["bc_id"] = link_info["bc_id_hint"]
        items.append(payload)
    return common.StoreList(items=items)


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/stores",
    response_model=common.StoreList,
)
def list_account_stores_query(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    advertiser_id: str = Query(..., max_length=64),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.StoreList:
    """List stores for an advertiser using a query parameter."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    stores = _build_account_store_list(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    return stores


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/advertisers/{advertiser_id}/stores",
    response_model=common.StoreList,
)
def list_account_stores(
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.StoreList:
    """List stores for a specific advertiser using a path parameter."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    stores = _build_account_store_list(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    return stores


@router.get(
    "/{workspace_id}/providers/{provider}/accounts/{auth_id}/products",
    response_model=common.ProductList,
)
def list_account_products(
    workspace_id: int,
    provider: str,
    auth_id: int,
    request: Request,
    store_id: str = Query(..., max_length=64),
    advertiser_id: Optional[str] = Query(default=None, max_length=64),
    owner_bc_id: Optional[str] = Query(default=None, max_length=64),
    automation_stats_start_date: Optional[date] = Query(
        default=None,
        description="Start date for product automation execution stats, in advertiser timezone.",
    ),
    automation_stats_end_date: Optional[date] = Query(
        default=None,
        description="End date for product automation execution stats, in advertiser timezone.",
    ),
    only_unassigned: bool = Query(False, description="Filter to GMV Max-eligible unassigned products."),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(200, ge=1, le=500),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
) -> common.ProductList:
    """List products for a given store, optionally filtering by advertiser and BC."""
    common._normalize_provider(provider)
    common._ensure_account(db, workspace_id, auth_id)
    if "bc_id" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="bc_id parameter is no longer supported; please use owner_bc_id",
        )
    normalized_store = _normalize_identifier(store_id)
    if not normalized_store:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="store_id is required",
        )
    if (
        automation_stats_start_date
        and automation_stats_end_date
        and automation_stats_start_date > automation_stats_end_date
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="automation_stats_start_date must be earlier than or equal to automation_stats_end_date",
        )
    normalized_adv = _normalize_identifier(advertiser_id)
    normalized_owner = _normalize_identifier(owner_bc_id)
    store = common._get_store(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        store_id=normalized_store,
    )
    advertiser = None
    if normalized_adv:
        advertiser = common._get_advertiser(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_adv,
        )
    # If an owner BC is provided without an advertiser filter, verify it aligns with the store
    if normalized_owner and not advertiser:
        store_candidates, _ = common._resolve_store_bc_candidates(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            store_id=normalized_store,
        )
        store_candidates = common._collect_bc_candidates(
            store.bc_id,
            store.store_authorized_bc_id,
            *store_candidates,
        )
        if store_candidates and normalized_owner not in store_candidates:
            raise APIError(
                "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE",
                "Store belongs to a different business center.",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
    if advertiser:
        expected_bc = normalized_owner or advertiser.bc_id or store.bc_id
        common._validate_bc_alignment(
            db=db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            expected_bc_id=expected_bc,
            advertiser=advertiser,
            store=store,
        )
        link_exists = (
            db.query(TTBAdvertiserStoreLink.id)
            .filter(TTBAdvertiserStoreLink.workspace_id == int(workspace_id))
            .filter(TTBAdvertiserStoreLink.auth_id == int(auth_id))
            .filter(TTBAdvertiserStoreLink.advertiser_id == normalized_adv)
            .filter(TTBAdvertiserStoreLink.store_id == normalized_store)
            .first()
        )
        if link_exists is None:
            raise APIError(
                "ADVERTISER_STORE_LINK_NOT_FOUND",
                "Store is not linked to the advertiser.",
                status.HTTP_404_NOT_FOUND,
            )
    offset = (page - 1) * page_size
    def _load_products() -> Tuple[int, List[TTBProduct], Set[str], Dict[str, Optional[str]]]:
        base_query = (
            db.query(TTBProduct)
            .filter(TTBProduct.workspace_id == int(workspace_id))
            .filter(TTBProduct.auth_id == int(auth_id))
            .filter(TTBProduct.store_id == normalized_store)
        )
        assignment_stmt = (
            select(GmvmaxProductCampaignItemGroup.item_group_id)
            .join(
                GmvmaxProductCampaignCatalog,
                (
                    GmvmaxProductCampaignCatalog.workspace_id
                    == GmvmaxProductCampaignItemGroup.workspace_id
                )
                & (
                    GmvmaxProductCampaignCatalog.auth_id
                    == GmvmaxProductCampaignItemGroup.auth_id
                )
                & (
                    GmvmaxProductCampaignCatalog.advertiser_id
                    == GmvmaxProductCampaignItemGroup.advertiser_id
                )
                & (
                    GmvmaxProductCampaignCatalog.campaign_id
                    == GmvmaxProductCampaignItemGroup.campaign_id
                ),
            )
            .where(GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id))
            .where(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
            .where(GmvmaxProductCampaignItemGroup.store_id == str(normalized_store))
            .where(GmvmaxProductCampaignCatalog.operation_status == "ENABLE")
        )
        if normalized_adv:
            assignment_stmt = assignment_stmt.where(
                GmvmaxProductCampaignItemGroup.advertiser_id == normalized_adv
            )
        assigned_ids = {
            str(item)
            for item in db.execute(assignment_stmt).scalars().all()
            if item is not None
        }

        # GMV_MAX product eligibility is advertiser-dependent.  Join the
        # dedicated evidence table before count/offset so pagination remains
        # exact even for large stores.
        if normalized_adv:
            base_query = base_query.join(
                TTBProductAdvertiserEligibility,
                and_(
                    TTBProductAdvertiserEligibility.workspace_id
                    == TTBProduct.workspace_id,
                    TTBProductAdvertiserEligibility.auth_id
                    == TTBProduct.auth_id,
                    TTBProductAdvertiserEligibility.store_id
                    == TTBProduct.store_id,
                    TTBProductAdvertiserEligibility.product_id
                    == TTBProduct.product_id,
                    TTBProductAdvertiserEligibility.advertiser_id
                    == normalized_adv,
                    TTBProductAdvertiserEligibility.is_eligible.is_(True),
                ),
            )
        elif only_unassigned:
            # The official eligibility filter requires an advertiser.  Do not
            # infer it from the store-level compatibility projection.
            return 0, [], assigned_ids, {}

        if only_unassigned:
            base_query = base_query.filter(
                func.upper(TTBProduct.status) == "AVAILABLE",
                func.upper(
                    TTBProductAdvertiserEligibility.gmv_max_ads_status
                )
                == "UNOCCUPIED",
                ~TTBProduct.product_id.in_(assignment_stmt),
            )

        total_rows = base_query.count()
        rows = (
            base_query.order_by(TTBProduct.title.asc(), TTBProduct.product_id.asc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
        eligibility_statuses: Dict[str, Optional[str]] = {}
        if normalized_adv and rows:
            status_rows = (
                db.query(
                    TTBProductAdvertiserEligibility.product_id,
                    TTBProductAdvertiserEligibility.gmv_max_ads_status,
                )
                .filter(
                    TTBProductAdvertiserEligibility.workspace_id
                    == int(workspace_id),
                    TTBProductAdvertiserEligibility.auth_id == int(auth_id),
                    TTBProductAdvertiserEligibility.advertiser_id
                    == normalized_adv,
                    TTBProductAdvertiserEligibility.store_id
                    == normalized_store,
                    TTBProductAdvertiserEligibility.is_eligible.is_(True),
                    TTBProductAdvertiserEligibility.product_id.in_(
                        [str(row.product_id) for row in rows]
                    ),
                )
                .all()
            )
            eligibility_statuses = {
                str(product_id): _normalize_identifier(advertiser_status)
                for product_id, advertiser_status in status_rows
            }
        return total_rows, rows, assigned_ids, eligibility_statuses
    total, rows, assigned_ids, eligibility_statuses = _load_products()
    product_ids = [str(row.product_id) for row in rows if row.product_id]
    automation_stats = _load_product_automation_stats(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=normalized_adv,
        store_id=normalized_store,
        product_ids=product_ids,
        stats_start_date=automation_stats_start_date,
        stats_end_date=automation_stats_end_date,
    )
    items = []
    for row in rows:
        payload = common._serialize_product(row, assigned_ids=assigned_ids)
        if normalized_adv and str(row.product_id) not in assigned_ids:
            payload["gmv_max_ads_status"] = eligibility_statuses.get(
                str(row.product_id)
            )
        payload["gmvmax_automation_stats"] = automation_stats.get(str(row.product_id))
        items.append(common.ProductItem(**payload))
    return common.ProductList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = [
    "router",
    "list_account_business_centers",
    "list_account_advertisers",
    "list_account_stores_query",
    "list_account_stores",
    "list_account_products",
]
