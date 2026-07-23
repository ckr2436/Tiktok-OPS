from __future__ import annotations

"""Dynamic creative-level guardrails for GMV Max campaigns.

The campaign smart guard protects the whole budget. This service protects the
budget inside a campaign by removing individual creatives whose spend is no
longer statistically reasonable for the product price, target ROAS, and daily
budget.
"""

import asyncio
import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import ceil
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import JSON as SAJSON
from sqlalchemy import and_, bindparam, or_, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.gmv_restructured import GmvStrategyConfig
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMaxCampaignCreateBody,
    GMVMaxBidRecommendRequest,
    GMVMaxCreativeStatusUpdateBody,
    GMVMaxCreativeStatusUpdateItem,
    GMVMaxCreativeStatusUpdateRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxIdentityInfo,
)
from app.services.gmvmax_hermes_decision import (
    apply_approved_plan_defaults_to_body,
    apply_approved_plan_defaults_to_strategy,
)
from app.gmvmax.services.campaign_catalog_freshness import (
    catalog_observation_now,
)
from app.gmvmax.services.mutation_execution_lock import (
    GmvMaxMutationBusy,
    GmvMaxMutationFenceLost,
    active_gmvmax_mutation_lease,
    assert_gmvmax_mutation_current,
    gmvmax_mutation_lease,
)
from app.features.tenants.ttb.gmv_max.control import (
    is_manual_pause_override_active,
)
from app.features.tenants.ttb.gmv_max.schemas import CreateCampaignRequest
from app.features.tenants.ttb.gmv_max.service import (
    create_gmvmax_campaign as create_gmvmax_campaign_durable,
    get_gmvmax_create_intent,
    mark_gmvmax_create_intent,
    prepare_gmvmax_create_intent,
)
from app.services.ttb_client_factory import build_ttb_gmvmax_client
from app.services.ttb_api import TTBBusinessError

logger = logging.getLogger("gmv.services.gmvmax.creative_guard")

_PRODUCT_CARD_REBUILD_SUFFIX_RE = re.compile(
    r"(?:(?:_商品卡重建_|__pc_reset_)\d{8}_\d{6}|_鍟嗗搧鍗￠噸寤篲\d{8}_\d{6}|_i[0-9a-fA-F]{8})+$"
)

_REBUILD_WORKFLOW_SOURCE = "creative_guard_rebuild"
_REBUILD_RESUMABLE_STATES = frozenset(
    {
        "PREPARED",
        "SUBMITTING",
        "UNKNOWN",
        "REMOTE_CREATED",
        "FINALIZING",
    }
)
_REBUILD_RECOVERY_ONLY_STATES = frozenset(
    {"COMPENSATION_PENDING"}
)
_CREATE_INTENT_BLOCKING_STATES = tuple(
    sorted(
        _REBUILD_RESUMABLE_STATES
        | _REBUILD_RECOVERY_ONLY_STATES
        | {"QUARANTINED"}
    )
)


@dataclass
class CampaignScope:
    strategy_id: int
    workspace_id: int
    auth_id: int
    advertiser_id: str
    store_id: str
    campaign_id: str
    campaign_name: str | None
    operation_status: str | None
    secondary_status: str | None
    budget_cents: int
    roas_bid: Decimal | None
    config: dict[str, Any]
    monitor_state: dict[str, Any]
    smart_guard_state: dict[str, Any]


@dataclass
class CreativeMetric:
    creative_id: str
    item_group_id: str | None
    status: str | None
    cost_cents: int
    gross_revenue_cents: int
    orders: int
    product_impressions: int
    product_clicks: int
    ad_click_rate: Decimal | None
    product_click_rate: Decimal | None
    ad_conversion_rate: Decimal | None
    roi: Decimal | None
    video_view_rate_2s: Decimal | None = None
    video_view_rate_6s: Decimal | None = None
    video_view_rate_100: Decimal | None = None


@dataclass
class CampaignActivity:
    campaign_start_at: datetime | None
    latest_metric_at: datetime | None
    today_cost_cents: int
    recent_snapshot_count: int
    low_spend_window_minutes: int
    low_spend_delta_cents: int
    low_spend_order_delta: int
    low_spend_latest_cost_cents: int
    low_spend_latest_orders: int


class CreativeGuardAutomationHold(RuntimeError):
    """A safe HOLD condition, not an automation failure."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _base_campaign_name(name: str | None) -> str:
    base = str(name or "GMV Max").strip() or "GMV Max"
    base = _PRODUCT_CARD_REBUILD_SUFFIX_RE.sub("", base).strip("_")
    return base or "GMV Max"


def _to_decimal(value: Any, default: str | None = None) -> Decimal | None:
    if value is None or value == "":
        if default is None:
            return None
        value = default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default) if default is not None else None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_state_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scope_due(scope: CampaignScope, now: datetime) -> bool:
    next_check_at = _parse_state_datetime(scope.monitor_state.get("next_check_at"))
    return next_check_at is None or now >= next_check_at


def _controlled_test_active(scope: CampaignScope, *, now: datetime) -> bool:
    test_state = dict(scope.smart_guard_state.get("controlled_test") or {})
    if not bool(test_state.get("active")):
        return False
    review_at = _parse_state_datetime(test_state.get("review_at"))
    if review_at is None:
        return True
    max_overrun = max(5, _to_int(scope.config.get("controlled_test_reset_grace_minutes"), 10))
    return now <= review_at + timedelta(minutes=max_overrun)


def _money_to_cents(value: Decimal | int | float | str | None) -> int | None:
    decimal = _to_decimal(value)
    if decimal is None:
        return None
    cents = (decimal * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def _strategy_config(strategy: GmvStrategyConfig) -> dict[str, Any]:
    value = strategy.config_json or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _advertiser_timezone_name(db: Session, scope: CampaignScope) -> str | None:
    row = db.execute(
        text(
            """
            select display_timezone, timezone
            from ttb_advertisers
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
        },
    ).mappings().first()
    if not row:
        return None
    timezone_name = row.get("display_timezone") or row.get("timezone")
    return str(timezone_name) if timezone_name else None


def _advertiser_today(db: Session, scope: CampaignScope) -> date:
    timezone_name = _advertiser_timezone_name(db, scope)
    if timezone_name:
        try:
            return datetime.now(ZoneInfo(str(timezone_name))).date()
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "gmvmax creative guard ignored invalid advertiser timezone",
                extra={
                    "workspace_id": scope.workspace_id,
                    "auth_id": scope.auth_id,
                    "advertiser_id": scope.advertiser_id,
                    "timezone": timezone_name,
                },
            )
    return date.today()


def _campaign_report_start_day(db: Session, scope: CampaignScope) -> date:
    """Keep young reset campaigns visible across advertiser-timezone midnight."""

    today = _advertiser_today(db, scope)
    row = db.execute(
        text(
            """
            select schedule_start_time_utc, create_time_utc, updated_at, created_at
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    if not row:
        return today
    start_at = (
        _as_utc(row.get("schedule_start_time_utc"))
        or _as_utc(row.get("create_time_utc"))
        or _as_utc(row.get("created_at"))
        or _as_utc(row.get("updated_at"))
    )
    if not start_at:
        return today
    timezone_name = _advertiser_timezone_name(db, scope)
    if timezone_name and start_at.tzinfo:
        try:
            start_day = start_at.astimezone(ZoneInfo(str(timezone_name))).date()
        except (ZoneInfoNotFoundError, ValueError):
            start_day = start_at.date()
    else:
        start_day = start_at.date()
    return min(start_day, today)


def default_creative_guard_config() -> dict[str, Any]:
    """Default dynamic rule set.

    All money thresholds are derived at runtime. The defaults express ratios,
    not fixed dollar cutoffs, so a $50 test budget and a $2,000 scale budget do
    not behave as if they had the same risk tolerance.
    """

    return {
        "enabled": False,
        "fast_monitor_interval_minutes": 1,
        "monitor_interval_minutes": 3,
        "slow_monitor_interval_minutes": 5,
        "fast_spend_budget_share": "0.08",
        "slow_spend_budget_share": "0.01",
        "data_max_age_minutes": 10,
        "evaluation_days": 2,
        # Realtime reports are useful for monitoring, but same-day GMV Max
        # attribution is not mature enough to justify deleting a creative or
        # rebuilding its campaign.  Keep the platform's exploration space
        # intact until a campaign and its creative have meaningful evidence.
        "learning_protection": {
            "enabled": True,
            "min_campaign_age_hours": 72,
            "min_orders_for_roi_remove": 3,
            "min_no_order_spend_multiplier": "3.0",
            "min_roi_spend_multiplier": "2.0",
            "auto_rebuild_enabled": False,
        },
        "action": "REMOVE",
        "min_statuses": ["DELIVERING", "LEARNING", "IN_QUEUE"],
        "ignore_creative_ids": ["0"],
        "product_card_reset": {
            # Product-card performance is an observation signal by default.
            # Automatic campaign recreation destroys GMV Max learning and is
            # only permitted through an explicit strategy override.
            "enabled": False,
            "recreate": False,
            "disable_old_strategy": True,
            "protect_good_campaign": True,
            "min_product_card_spend_share": "0.70",
            "require_video_starvation": True,
            "max_video_spend_share": "0.20",
            "campaign_roi_floor": "0.8",
            "video_roi_floor": "1.0",
            "campaign_min_orders": 1,
            "video_min_orders": 1,
        },
        "product_rebuild_cooldown": {
            "enabled": True,
            "lookback_minutes": 180,
            "max_recent_resets": 2,
        },
        "no_spend_reset": {
            # Low campaign delivery is handled by GMV Max bid/budget learning.
            # It must never cause a campaign rebuild by itself.
            "enabled": False,
            "grace_minutes": 45,
            "cooldown_minutes": 120,
            "require_active_campaign": True,
            "low_spend_enabled": True,
            "low_spend_window_minutes": 60,
            "low_spend_grace_minutes": 90,
            "low_spend_min_delta_cents": 100,
            "low_spend_budget_share": "0.03",
            "low_spend_require_no_new_orders": True,
        },
        "use_effective_product_price": True,
        "order_value_lookback_days": 90,
        "target_roas_fallback": "1.2",
        "expected_cvr_fallback": "0.02",
        "no_order_allowed_cpa_multiplier": "1.5",
        "legacy_absolute_no_order_threshold_enabled": False,
        "no_order_price_multiplier_cap": "2.5",
        "no_order_budget_share_cap": "0.20",
        "no_order_min_spend_cents": 300,
        "no_order_budget_share_floor": "0.0",
        "roi_min_spend_allowed_cpa_multiplier": "0.8",
        "click_no_order_expected_orders": "1.0",
        "protect_converting_creatives": True,
        "converting_creative_min_orders": 1,
        "converting_creative_roi_grace_ratio": "0.85",
        "converting_creative_min_roi": "1.2",
        "retest": {
            "enabled": True,
            "base_cooldown_minutes": 24 * 60,
            "min_cooldown_minutes": 24 * 60,
            "max_cooldown_minutes": 7 * 24 * 60,
            "failure_cooldown_multiplier": "1.75",
            "high_quality_cooldown_ratio": "0.5",
            "low_delivery_cooldown_ratio": "0.5",
            "max_attempts_per_campaign": 4,
            "max_add_back_per_cycle": 1,
            "time_bucket_hours": 24,
            "require_new_time_bucket": True,
            "low_delivery_override_new_bucket": False,
            "low_delivery_window_minutes": 60,
            "low_delivery_min_spend_cents": 100,
            "low_delivery_budget_share": "0.01",
            "quality_ctr_floor": "1.0",
            "quality_video_6s_floor": "8.0",
            "quality_completion_floor": "5.0",
            "quality_relative_ratio": "1.0",
        },
        "historical_reinclude_min_orders": 1,
        "historical_reinclude_min_roi": "1.2",
        "historical_blacklist_enabled": True,
        "historical_blacklist_min_remove_events": 3,
        "historical_blacklist_min_distinct_campaigns": 2,
        "historical_blacklist_min_distinct_time_buckets": 3,
        "historical_blacklist_time_bucket_hours": 4,
        "historical_blacklist_require_repeated_campaign_evidence": True,
        "historical_blacklist_zero_order_price_multiplier": "1.5",
        "historical_blacklist_poor_roi_price_multiplier": "2.0",
        "historical_blacklist_single_event_price_multiplier": "3.0",
        "historical_blacklist_poor_roi_min_orders": 2,
        "historical_blacklist_poor_roi_floor": "0.8",
        "historical_blacklist_min_spend_cents": 300,
        "historical_blacklist_honor_add_events": True,
        "max_remove_per_campaign_per_cycle": 10,
        "dry_run": False,
    }


def _creative_guard_config(strategy: GmvStrategyConfig) -> dict[str, Any]:
    config = _strategy_config(strategy)
    guard = dict(default_creative_guard_config())
    guard.update(dict(config.get("creative_guard") or {}))
    default_retest = dict(default_creative_guard_config().get("retest") or {})
    default_retest.update(dict(guard.get("retest") or {}))
    guard["retest"] = default_retest
    default_learning = dict(default_creative_guard_config().get("learning_protection") or {})
    default_learning.update(dict(guard.get("learning_protection") or {}))
    guard["learning_protection"] = default_learning
    explicit_enabled = config.get("creative_guard_enabled")
    if explicit_enabled is not None:
        guard["enabled"] = bool(explicit_enabled)
    return guard


def _creative_retest_config(scope: CampaignScope) -> dict[str, Any]:
    defaults = dict(default_creative_guard_config().get("retest") or {})
    defaults.update(dict(scope.config.get("retest") or {}))
    return defaults


def _learning_protection_config(scope: CampaignScope) -> dict[str, Any]:
    """Return the destructive-action gate loaded by production scope setup."""

    value = scope.config.get("learning_protection")
    return dict(value) if isinstance(value, Mapping) else {}


def _hold_decision(
    decision: Mapping[str, Any],
    *,
    reason: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    original_context = decision.get("context")
    merged_context = dict(original_context) if isinstance(original_context, Mapping) else {}
    merged_context.update(context)
    merged_context["original_action"] = str(decision.get("action") or "HOLD")
    merged_context["original_reason"] = str(decision.get("reason") or "")
    return {"action": "HOLD", "reason": reason, "context": merged_context}


def _apply_learning_protection(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Downgrade immature destructive decisions to auditable HOLD decisions."""

    action = str(decision.get("action") or "HOLD").upper()
    if action not in {"REMOVE", "RESET_CAMPAIGN"}:
        return dict(decision)
    config = _learning_protection_config(scope)
    if not bool(config.get("enabled", True)):
        return dict(decision)
    if action == "RESET_CAMPAIGN" and not bool(config.get("auto_rebuild_enabled", False)):
        return _hold_decision(
            decision,
            reason="creative_guard:learning_protection:auto_rebuild_disabled",
            context={"creative_id": metric.creative_id},
        )

    activity = _load_campaign_activity(db, scope, now=now)
    if activity.campaign_start_at is None:
        return _hold_decision(
            decision,
            reason="creative_guard:learning_protection:campaign_age_unknown",
            context={"creative_id": metric.creative_id},
        )
    age_hours = max(0, int((now - activity.campaign_start_at).total_seconds() // 3600))
    min_age_hours = max(1, _to_int(config.get("min_campaign_age_hours"), 72))
    if age_hours < min_age_hours:
        return _hold_decision(
            decision,
            reason="creative_guard:learning_protection:campaign_learning",
            context={
                "creative_id": metric.creative_id,
                "campaign_age_hours": age_hours,
                "min_campaign_age_hours": min_age_hours,
            },
        )

    reason = str(decision.get("reason") or "")
    decision_context = decision.get("context")
    decision_context = dict(decision_context) if isinstance(decision_context, Mapping) else {}
    no_order_threshold = max(0, _to_int(decision_context.get("no_order_threshold_cents"), 0))
    if reason == "creative_guard:roi_below_target":
        min_orders = max(1, _to_int(config.get("min_orders_for_roi_remove"), 3))
        min_spend = int(
            Decimal(no_order_threshold)
            * (_to_decimal(config.get("min_roi_spend_multiplier"), "2.0") or Decimal("2.0"))
        )
        if metric.orders < min_orders or (min_spend > 0 and metric.cost_cents < min_spend):
            return _hold_decision(
                decision,
                reason="creative_guard:learning_protection:roi_sample_immature",
                context={
                    "creative_id": metric.creative_id,
                    "orders": metric.orders,
                    "min_orders": min_orders,
                    "cost_cents": metric.cost_cents,
                    "min_spend_cents": min_spend,
                },
            )
    elif reason in {
        "creative_guard:no_order_spend_threshold",
        "creative_guard:clicks_without_order",
    }:
        min_spend = int(
            Decimal(no_order_threshold)
            * (
                _to_decimal(config.get("min_no_order_spend_multiplier"), "3.0")
                or Decimal("3.0")
            )
        )
        if min_spend > 0 and metric.cost_cents < min_spend:
            return _hold_decision(
                decision,
                reason="creative_guard:learning_protection:no_order_sample_immature",
                context={
                    "creative_id": metric.creative_id,
                    "cost_cents": metric.cost_cents,
                    "min_spend_cents": min_spend,
                },
            )
    return dict(decision)


def _load_scopes(db: Session) -> list[CampaignScope]:
    rows = (
        db.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.enabled.is_(True))
        .order_by(GmvStrategyConfig.updated_at.asc(), GmvStrategyConfig.id.asc())
        .all()
    )
    scopes: list[CampaignScope] = []
    for strategy in rows:
        strategy_config = _strategy_config(strategy)
        guard = _creative_guard_config(strategy)
        if not bool(guard.get("enabled", False)):
            continue
        row = db.execute(
            text(
                """
                select c.workspace_id, c.auth_id, c.advertiser_id, c.store_id, c.campaign_id,
                       c.campaign_name, c.operation_status, c.secondary_status,
                       c.budget_cents, c.roas_bid, r.runtime_json
                from gmvmax_product_campaign_catalog c
                left join gmv_campaign_realtime_state r
                  on r.workspace_id=c.workspace_id
                 and r.auth_id=c.auth_id
                 and r.advertiser_id=c.advertiser_id
                 and r.store_id=c.store_id
                 and r.campaign_id=c.campaign_id
                where c.workspace_id=:workspace_id
                  and c.auth_id=:auth_id
                  and c.campaign_id=:campaign_id
                  and c.advertiser_id is not null
                  and c.advertiser_id<>''
                  and c.store_id is not null
                  and c.store_id<>''
                  and (
                    select count(*)
                    from gmvmax_product_campaign_catalog candidate
                    where candidate.workspace_id=c.workspace_id
                      and candidate.auth_id=c.auth_id
                      and candidate.campaign_id=c.campaign_id
                  )=1
                """
            ),
            {
                "workspace_id": int(strategy.workspace_id),
                "auth_id": int(strategy.auth_id),
                "campaign_id": str(strategy.campaign_id),
            },
        ).mappings().first()
        if not row:
            continue
        runtime = _json_dict(row.get("runtime_json"))
        scopes.append(
            CampaignScope(
                strategy_id=int(strategy.id),
                workspace_id=int(row["workspace_id"]),
                auth_id=int(row["auth_id"]),
                advertiser_id=str(row["advertiser_id"]),
                store_id=str(row["store_id"]),
                campaign_id=str(row["campaign_id"]),
                campaign_name=row.get("campaign_name"),
                operation_status=row.get("operation_status"),
                secondary_status=row.get("secondary_status"),
                budget_cents=_to_int(row.get("budget_cents"), 0),
                roas_bid=_to_decimal(row.get("roas_bid")),
                config=guard,
                monitor_state=dict(
                    runtime.get("creative_guard_state")
                    or strategy_config.get("creative_guard_state")
                    or {}
                ),
                smart_guard_state=dict(
                    runtime.get("smart_guard_state")
                    or strategy_config.get("smart_guard_state")
                    or {}
                ),
            )
        )
    return scopes


def _catalog_primary_item_group_id(db: Session, scope: CampaignScope) -> str | None:
    row = db.execute(
        text(
            """
            select json_unquote(json_extract(detail_raw_json, '$.item_group_ids[0]')) as item_group_id
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    item_group_id = (row or {}).get("item_group_id")
    text_value = str(item_group_id or "").strip()
    return text_value or None


def _normalized_campaign_item_group_ids(
    db: Session,
    scope: CampaignScope,
) -> list[str]:
    rows = db.execute(
        text(
            """
            select item_group_id
            from gmvmax_product_campaign_item_groups
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).scalars().all()
    result = list(
        dict.fromkeys(
            str(item).strip()
            for item in rows
            if str(item or "").strip()
        )
    )
    return result


def _campaign_item_group_ids(db: Session, scope: CampaignScope) -> list[str]:
    result = _normalized_campaign_item_group_ids(db, scope)
    if result:
        return result
    row = db.execute(
        text(
            """
            select detail_raw_json
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    raw = (row or {}).get("detail_raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    item_group_ids = raw.get("item_group_ids") if isinstance(raw, Mapping) else None
    if not isinstance(item_group_ids, list):
        return []
    seen: set[str] = set()
    result = []
    for item in item_group_ids:
        text_value = str(item or "").strip()
        if text_value and text_value not in seen:
            seen.add(text_value)
            result.append(text_value)
    return result


def _update_creative_guard_state(
    db: Session,
    scope: CampaignScope,
    *,
    now: datetime,
    interval_minutes: int,
    checked_creatives: int,
    action_count: int,
    data_quality: Mapping[str, Any] | None = None,
) -> None:
    strategy = db.get(GmvStrategyConfig, scope.strategy_id)
    if strategy is None:
        return
    interval = max(1, int(interval_minutes or 1))
    state = dict(scope.monitor_state or {})
    state.update(
        {
            "last_checked_at": now.isoformat(),
            "monitor_interval_minutes": interval,
            "next_check_at": (now + timedelta(minutes=interval)).isoformat(),
            "checked_creatives": int(checked_creatives or 0),
            "action_count": int(action_count or 0),
        }
    )
    if data_quality is not None:
        state["data_quality"] = dict(data_quality)
    scope.monitor_state = state
    result = db.execute(
        text(
            """
            update gmv_campaign_realtime_state
            set runtime_json=json_set(
                    coalesce(runtime_json, json_object()),
                    '$.creative_guard_state', json_extract(:state_json, '$')
                ),
                state_version=state_version+1,
                updated_at=:updated_at
            where strategy_id=:strategy_id
            """
        ),
        {
            "state_json": _json_dumps(state),
            "updated_at": now.replace(tzinfo=None),
            "strategy_id": scope.strategy_id,
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "campaign_id": scope.campaign_id,
        },
    )
    if int(result.rowcount or 0) > 0:
        return
    db.execute(
        text(
            """
            update gmv_campaign_realtime_state
            set runtime_json=json_set(
                    coalesce(runtime_json, json_object()),
                    '$.creative_guard_state', json_extract(:state_json, '$')
                ),
                state_version=state_version+1,
                updated_at=:updated_at
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        {
            "state_json": _json_dumps(state),
            "updated_at": now.replace(tzinfo=None),
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    )


def _creative_monitor_interval_minutes(scope: CampaignScope, metrics: list[CreativeMetric]) -> int:
    fast = max(1, _to_int(scope.config.get("fast_monitor_interval_minutes"), 1))
    normal = max(fast, _to_int(scope.config.get("monitor_interval_minutes"), 3))
    slow = max(normal, _to_int(scope.config.get("slow_monitor_interval_minutes"), 5))
    if not _campaign_can_update_creatives(scope):
        return slow
    if not metrics:
        return fast

    budget = max(0, int(scope.budget_cents or 0))
    total_cost = sum(max(0, int(metric.cost_cents or 0)) for metric in metrics)
    top_cost = max((max(0, int(metric.cost_cents or 0)) for metric in metrics), default=0)
    if total_cost <= 0:
        return normal
    if budget <= 0:
        return normal

    top_share = Decimal(top_cost) / Decimal(budget)
    fast_share = _to_decimal(scope.config.get("fast_spend_budget_share"), "0.08") or Decimal("0.08")
    slow_share = _to_decimal(scope.config.get("slow_spend_budget_share"), "0.01") or Decimal("0.01")
    if top_share >= fast_share:
        return fast
    if top_share <= slow_share:
        return slow
    return normal


def _load_creatives(db: Session, scope: CampaignScope) -> list[CreativeMetric]:
    realtime_metrics = _load_realtime_creatives(db, scope)
    if realtime_metrics:
        return realtime_metrics
    return _load_daily_creatives(db, scope)


def _load_daily_creatives(db: Session, scope: CampaignScope) -> list[CreativeMetric]:
    days = max(1, _to_int(scope.config.get("evaluation_days"), 1))
    start_day = _campaign_report_start_day(db, scope)
    if days > 1:
        start_day = start_day - timedelta(days=days - 1)
    rows = db.execute(
        text(
            """
            select creative_id,
                   item_group_id,
                   max(creative_delivery_status) as creative_delivery_status,
                   sum(coalesce(nullif(net_cost_cents, 0), cost_cents, 0)) as cost_cents,
                   sum(coalesce(gross_revenue_cents, 0)) as gross_revenue_cents,
                   sum(coalesce(orders, 0)) as orders,
                   sum(coalesce(product_impressions, 0)) as product_impressions,
                   sum(coalesce(product_clicks, 0)) as product_clicks,
                   max(ad_click_rate) as ad_click_rate,
                   max(product_click_rate) as product_click_rate,
                   max(ad_conversion_rate) as ad_conversion_rate,
                   max(ad_video_view_rate_2s) as video_view_rate_2s,
                   max(ad_video_view_rate_6s) as video_view_rate_6s,
                   max(ad_video_view_rate_p100) as video_view_rate_100
            from gmvmax_product_creative_metrics_daily
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and stat_time_day >= :start_day
            group by item_group_id, creative_id
            having sum(coalesce(nullif(net_cost_cents, 0), cost_cents, 0)) > 0
                or upper(coalesce(max(creative_delivery_status), '')) like '%EXCLUD%'
            order by sum(coalesce(nullif(net_cost_cents, 0), cost_cents, 0)) desc
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "start_day": start_day,
        },
    ).mappings().all()

    metrics: list[CreativeMetric] = []
    for row in rows:
        cost_cents = _to_int(row.get("cost_cents"), 0)
        gross_cents = _to_int(row.get("gross_revenue_cents"), 0)
        roi = (Decimal(gross_cents) / Decimal(cost_cents)).quantize(Decimal("0.0001")) if cost_cents else None
        metrics.append(
            CreativeMetric(
                creative_id=str(row.get("creative_id") or ""),
                item_group_id=str(row.get("item_group_id") or "") or None,
                status=row.get("creative_delivery_status"),
                cost_cents=cost_cents,
                gross_revenue_cents=gross_cents,
                orders=_to_int(row.get("orders"), 0),
                product_impressions=_to_int(row.get("product_impressions"), 0),
                product_clicks=_to_int(row.get("product_clicks"), 0),
                ad_click_rate=_to_decimal(row.get("ad_click_rate")),
                product_click_rate=_to_decimal(row.get("product_click_rate")),
                ad_conversion_rate=_to_decimal(row.get("ad_conversion_rate")),
                roi=roi,
                video_view_rate_2s=_to_decimal(row.get("video_view_rate_2s")),
                video_view_rate_6s=_to_decimal(row.get("video_view_rate_6s")),
                video_view_rate_100=_to_decimal(row.get("video_view_rate_100")),
            )
        )
    return metrics


def _load_realtime_creatives(db: Session, scope: CampaignScope) -> list[CreativeMetric]:
    primary_item_group_id = _catalog_primary_item_group_id(db, scope)
    start_day = _campaign_report_start_day(db, scope)
    rows = db.execute(
        text(
            """
            select daily.creative_id,
                   daily.item_group_id,
                   coalesce(max(daily.creative_delivery_status), 'CANDIDATE') as creative_delivery_status,
                   sum(daily.cost_cents) as cost_cents,
                   sum(daily.gross_revenue_cents) as gross_revenue_cents,
                   sum(daily.orders) as orders,
                   sum(daily.product_impressions) as product_impressions,
                   sum(daily.product_clicks) as product_clicks,
                   max(daily.ad_click_rate) as ad_click_rate,
                   max(daily.product_click_rate) as product_click_rate,
                   max(daily.ad_conversion_rate) as ad_conversion_rate,
                   max(daily.video_view_rate_2s) as video_view_rate_2s,
                   max(daily.video_view_rate_6s) as video_view_rate_6s,
                   max(daily.video_view_rate_100) as video_view_rate_100,
                   null as roi
            from (
                select m.creative_id,
                       m.stat_time_day,
                       coalesce(m.item_group_id, :primary_item_group_id) as item_group_id,
                       coalesce(d.creative_delivery_status, m.creative_status, 'CANDIDATE') as creative_delivery_status,
                       coalesce(m.net_cost_cents, m.cost_cents, 0) as cost_cents,
                       coalesce(m.gross_revenue_cents, 0) as gross_revenue_cents,
                       coalesce(m.orders, 0) as orders,
                       coalesce(m.impressions, 0) as product_impressions,
                       coalesce(m.product_clicks, m.clicks, 0) as product_clicks,
                        m.ad_click_rate as ad_click_rate,
                        m.product_click_rate as product_click_rate,
                        coalesce(m.ad_conversion_rate, m.conversion_rate) as ad_conversion_rate,
                        m.video_view_rate_2s as video_view_rate_2s,
                        m.video_view_rate_6s as video_view_rate_6s,
                        m.video_view_rate_100 as video_view_rate_100
                from gmv_creative_metrics_10min m
                join (
                    select workspace_id, auth_id, advertiser_id, store_id,
                           campaign_id, stat_time_day,
                           max(snapshot_at) as snapshot_at
                    from gmv_creative_10min_batch_manifests
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day >= :start_day
                      and complete=1
                    group by workspace_id, auth_id, advertiser_id, store_id,
                             campaign_id, stat_time_day
                ) latest
                  on latest.workspace_id=m.workspace_id
                 and latest.auth_id=m.auth_id
                 and latest.advertiser_id=m.advertiser_id
                 and latest.store_id=m.store_id
                 and latest.campaign_id=m.campaign_id
                 and latest.stat_time_day=m.stat_time_day
                 and latest.snapshot_at=m.snapshot_at
                left join (
                    select campaign_id, item_group_id, creative_id, stat_time_day,
                           max(creative_delivery_status) as creative_delivery_status
                    from gmvmax_product_creative_metrics_daily
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day >= :start_day
                    group by campaign_id, item_group_id, creative_id, stat_time_day
                ) d
                  on d.campaign_id=m.campaign_id
                 and d.item_group_id=m.item_group_id
                 and d.creative_id=m.creative_id
                 and d.stat_time_day=m.stat_time_day
                where m.workspace_id=:workspace_id
                  and m.auth_id=:auth_id
                  and m.advertiser_id=:advertiser_id
                  and m.store_id=:store_id
                  and m.campaign_id=:campaign_id
                  and m.stat_time_day >= :start_day
            ) daily
            group by daily.item_group_id, daily.creative_id
            having sum(daily.cost_cents) > 0
                or upper(coalesce(max(daily.creative_delivery_status), '')) like '%EXCLUD%'
            order by sum(daily.cost_cents) desc
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "start_day": start_day,
            "primary_item_group_id": primary_item_group_id,
        },
    ).mappings().all()

    metrics: list[CreativeMetric] = []
    for row in rows:
        cost_cents = _to_int(row.get("cost_cents"), 0)
        gross_cents = _to_int(row.get("gross_revenue_cents"), 0)
        roi = _to_decimal(row.get("roi"))
        if roi is None and cost_cents:
            roi = (Decimal(gross_cents) / Decimal(cost_cents)).quantize(Decimal("0.0001"))
        metrics.append(
            CreativeMetric(
                creative_id=str(row.get("creative_id") or ""),
                item_group_id=str(row.get("item_group_id") or "") or None,
                status=row.get("creative_delivery_status"),
                cost_cents=cost_cents,
                gross_revenue_cents=gross_cents,
                orders=_to_int(row.get("orders"), 0),
                product_impressions=_to_int(row.get("product_impressions"), 0),
                product_clicks=_to_int(row.get("product_clicks"), 0),
                ad_click_rate=_to_decimal(row.get("ad_click_rate")),
                product_click_rate=_to_decimal(row.get("product_click_rate")),
                ad_conversion_rate=_to_decimal(row.get("ad_conversion_rate")),
                roi=roi,
                video_view_rate_2s=_to_decimal(row.get("video_view_rate_2s")),
                video_view_rate_6s=_to_decimal(row.get("video_view_rate_6s")),
                video_view_rate_100=_to_decimal(row.get("video_view_rate_100")),
            )
        )
    return metrics


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _minimum_nonnegative_age(*values: Any) -> int | None:
    ages = []
    for value in values:
        if value is None:
            continue
        try:
            age = int(value)
        except (TypeError, ValueError):
            continue
        if age >= -5:
            ages.append(max(0, age))
    return min(ages) if ages else None


def _creative_guard_data_quality(
    db: Session,
    scope: CampaignScope,
    *,
    now: datetime,
) -> dict[str, Any]:
    today = _advertiser_today(db, scope)
    row = db.execute(
        text(
            """
            select
                (select max(last_report_at)
                   from gmv_campaign_realtime_state
                  where workspace_id=:workspace_id and auth_id=:auth_id
                    and campaign_id=:campaign_id and report_end_date=:today) as campaign_report_at,
                (select timestampdiff(second, max(updated_at), current_timestamp(6))
                   from gmvmax_product_campaign_metrics_daily
                  where workspace_id=:workspace_id and auth_id=:auth_id
                    and campaign_id=:campaign_id and stat_time_day=:today) as campaign_age_local,
                (select timestampdiff(second, max(updated_at), utc_timestamp(6))
                   from gmvmax_product_campaign_metrics_daily
                  where workspace_id=:workspace_id and auth_id=:auth_id
                    and campaign_id=:campaign_id and stat_time_day=:today) as campaign_age_utc,
                (select snapshot_at
                   from gmv_creative_10min_batch_manifests
                  where workspace_id=:workspace_id and auth_id=:auth_id
                    and advertiser_id=:advertiser_id and store_id=:store_id
                    and campaign_id=:campaign_id and stat_time_day=:today
                    and complete=1
                  order by snapshot_at desc
                  limit 1) as creative_snapshot_at,
                (select source_observed_at
                   from gmv_creative_10min_batch_manifests
                  where workspace_id=:workspace_id and auth_id=:auth_id
                    and advertiser_id=:advertiser_id and store_id=:store_id
                    and campaign_id=:campaign_id and stat_time_day=:today
                    and complete=1
                  order by snapshot_at desc
                  limit 1) as creative_observed_at,
                (select row_count from gmv_creative_10min_batch_manifests
                  where workspace_id=:workspace_id and auth_id=:auth_id
                    and advertiser_id=:advertiser_id and store_id=:store_id
                    and campaign_id=:campaign_id and stat_time_day=:today
                    and complete=1
                  order by snapshot_at desc
                  limit 1) as creative_snapshot_rows
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "today": today,
        },
    ).mappings().first() or {}
    report_at = _as_utc(row.get("campaign_report_at"))
    report_age = int((now - report_at).total_seconds()) if report_at else None
    creative_observed_at = _as_utc(
        row.get("creative_observed_at") or row.get("creative_snapshot_at")
    )
    snapshot_age = (
        int((now - creative_observed_at).total_seconds())
        if creative_observed_at
        else None
    )
    campaign_age = _minimum_nonnegative_age(
        report_age,
        row.get("campaign_age_local"),
        row.get("campaign_age_utc"),
    )
    creative_age = _minimum_nonnegative_age(snapshot_age)
    max_age_minutes = max(3, _to_int(scope.config.get("data_max_age_minutes"), 10))
    max_age_seconds = max_age_minutes * 60
    campaign_valid = campaign_age is not None and campaign_age <= max_age_seconds
    creative_rows = _to_int(row.get("creative_snapshot_rows"), 0)
    creative_valid = (
        creative_rows > 0
        and creative_age is not None
        and creative_age <= max_age_seconds
    )
    return {
        "state": "fresh" if campaign_valid and creative_valid else "hold",
        "campaign_valid": campaign_valid,
        "creative_valid": creative_valid,
        "campaign_age_seconds": campaign_age,
        "creative_age_seconds": creative_age,
        "creative_rows": creative_rows,
        "max_age_seconds": max_age_seconds,
        "advertiser_day": today.isoformat(),
        "checked_at": now.isoformat(),
        "reason": (
            "fresh"
            if campaign_valid and creative_valid
            else "campaign_data_stale_or_missing"
            if not campaign_valid
            else "creative_data_stale_or_missing"
        ),
    }


def _load_campaign_activity(db: Session, scope: CampaignScope, *, now: datetime) -> CampaignActivity:
    today = _advertiser_today(db, scope)
    no_spend_config = dict(scope.config.get("no_spend_reset") or {})
    low_spend_window_minutes = max(
        10, _to_int(no_spend_config.get("low_spend_window_minutes"), 60)
    )
    row = db.execute(
        text(
            """
            select schedule_start_time_utc, create_time_utc, created_at
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    campaign_start_at = None
    if row:
        campaign_start_at = (
            _as_utc(row.get("schedule_start_time_utc"))
            or _as_utc(row.get("create_time_utc"))
            or _as_utc(row.get("created_at"))
        )

    metrics = db.execute(
        text(
            """
            select
                coalesce((
                    select count(*)
                    from gmvmax_product_campaign_metrics_hourly
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and date(stat_time_hour)=:today
                ), 0) as hourly_rows,
                coalesce((
                    select sum(coalesce(cost_cents, 0))
                    from gmvmax_product_campaign_metrics_hourly
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and date(stat_time_hour)=:today
                ), 0) as hourly_cost_cents,
                coalesce((
                    select count(*)
                    from gmvmax_product_campaign_metrics_daily
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day=:today
                      and source_observed_at is not null
                ), 0) as official_daily_rows,
                coalesce((
                    select sum(coalesce(cost_cents, 0))
                    from gmvmax_product_campaign_metrics_daily
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day=:today
                      and source_observed_at is not null
                ), 0) as official_daily_cost_cents,
                coalesce((
                    select count(*)
                    from gmvmax_product_creative_metrics_daily
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day=:today
                ), 0) as creative_rows,
                coalesce((
                    select sum(coalesce(cost_cents, 0))
                    from gmvmax_product_creative_metrics_daily
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day=:today
                ), 0) as creative_cost_cents,
                (
                    select max(source_observed_at)
                    from gmv_creative_10min_batch_manifests
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day=:today
                      and complete=1
                ) as latest_metric_at,
                coalesce((
                    select count(distinct snapshot_at)
                    from gmv_creative_10min_batch_manifests
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                      and stat_time_day=:today
                      and complete=1
                      and snapshot_at >= :recent_cutoff
                ), 0) as recent_snapshot_count
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "today": today,
            "recent_cutoff": (now - timedelta(minutes=60)).replace(tzinfo=None),
        },
    ).mappings().first() or {}
    # Choose one authoritative whole-row campaign fact for the advertiser's
    # current day.  Never MAX independent sources: official corrections may
    # legitimately lower a previously observed value (for example 100 -> 20).
    # Hourly is the freshest current-day campaign source; official daily is its
    # fallback. Creative facts are used only when no campaign fact exists.
    if _to_int(metrics.get("hourly_rows"), 0) > 0:
        today_cost = _to_int(metrics.get("hourly_cost_cents"), 0)
    elif _to_int(metrics.get("official_daily_rows"), 0) > 0:
        today_cost = _to_int(metrics.get("official_daily_cost_cents"), 0)
    elif _to_int(metrics.get("creative_rows"), 0) > 0:
        today_cost = _to_int(metrics.get("creative_cost_cents"), 0)
    else:
        today_cost = 0
    latest_snapshot = db.execute(
        text(
            """
            select b.snapshot_at,
                   sum(coalesce(m.cost_cents, 0)) as cost_cents,
                   sum(coalesce(m.orders, 0)) as orders
            from gmv_creative_10min_batch_manifests b
            left join gmv_creative_metrics_10min m
              on m.workspace_id=b.workspace_id
             and m.auth_id=b.auth_id
             and m.advertiser_id=b.advertiser_id
             and m.store_id=b.store_id
             and m.campaign_id=b.campaign_id
             and m.stat_time_day=b.stat_time_day
             and m.snapshot_at=b.snapshot_at
            where b.workspace_id=:workspace_id
              and b.auth_id=:auth_id
              and b.advertiser_id=:advertiser_id
              and b.store_id=:store_id
              and b.campaign_id=:campaign_id
              and b.stat_time_day=:today
              and b.complete=1
            group by b.snapshot_at
            order by b.snapshot_at desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "today": today,
        },
    ).mappings().first()
    latest_snapshot_at = None
    latest_cost_cents = 0
    latest_orders = 0
    baseline_cost_cents = 0
    baseline_orders = 0
    if latest_snapshot:
        latest_snapshot_at = _as_utc(latest_snapshot.get("snapshot_at"))
        latest_cost_cents = _to_int(latest_snapshot.get("cost_cents"), 0)
        latest_orders = _to_int(latest_snapshot.get("orders"), 0)
        baseline = None
        if latest_snapshot_at is not None:
            cutoff = latest_snapshot_at - timedelta(minutes=low_spend_window_minutes)
            baseline = db.execute(
                text(
                    """
                    select b.snapshot_at,
                           sum(coalesce(m.cost_cents, 0)) as cost_cents,
                           sum(coalesce(m.orders, 0)) as orders
                    from gmv_creative_10min_batch_manifests b
                    left join gmv_creative_metrics_10min m
                      on m.workspace_id=b.workspace_id
                     and m.auth_id=b.auth_id
                     and m.advertiser_id=b.advertiser_id
                     and m.store_id=b.store_id
                     and m.campaign_id=b.campaign_id
                     and m.stat_time_day=b.stat_time_day
                     and m.snapshot_at=b.snapshot_at
                    where b.workspace_id=:workspace_id
                      and b.auth_id=:auth_id
                      and b.advertiser_id=:advertiser_id
                      and b.store_id=:store_id
                      and b.campaign_id=:campaign_id
                      and b.stat_time_day=:today
                      and b.complete=1
                      and b.snapshot_at <= :cutoff
                    group by b.snapshot_at
                    order by b.snapshot_at desc
                    limit 1
                    """
                ),
                {
                    "workspace_id": scope.workspace_id,
                    "auth_id": scope.auth_id,
                    "advertiser_id": scope.advertiser_id,
                    "store_id": scope.store_id,
                    "campaign_id": scope.campaign_id,
                    "today": today,
                    "cutoff": cutoff.replace(tzinfo=None),
                },
            ).mappings().first()
            if baseline is None:
                # The configured window can be longer than the retained
                # history. Use the earliest real snapshot as a partial-window
                # baseline instead of silently substituting a fixed row cap.
                baseline = db.execute(
                    text(
                        """
                        select b.snapshot_at,
                               sum(coalesce(m.cost_cents, 0)) as cost_cents,
                               sum(coalesce(m.orders, 0)) as orders
                        from gmv_creative_10min_batch_manifests b
                        left join gmv_creative_metrics_10min m
                          on m.workspace_id=b.workspace_id
                         and m.auth_id=b.auth_id
                         and m.advertiser_id=b.advertiser_id
                         and m.store_id=b.store_id
                         and m.campaign_id=b.campaign_id
                         and m.stat_time_day=b.stat_time_day
                         and m.snapshot_at=b.snapshot_at
                        where b.workspace_id=:workspace_id
                          and b.auth_id=:auth_id
                          and b.advertiser_id=:advertiser_id
                          and b.store_id=:store_id
                          and b.campaign_id=:campaign_id
                          and b.stat_time_day=:today
                          and b.complete=1
                          and b.snapshot_at < :latest_snapshot_at
                        group by b.snapshot_at
                        order by b.snapshot_at asc
                        limit 1
                        """
                    ),
                    {
                        "workspace_id": scope.workspace_id,
                        "auth_id": scope.auth_id,
                        "advertiser_id": scope.advertiser_id,
                        "store_id": scope.store_id,
                        "campaign_id": scope.campaign_id,
                        "today": today,
                        "latest_snapshot_at": latest_snapshot_at.replace(tzinfo=None),
                    },
                ).mappings().first()
        if baseline is not None:
            baseline_cost_cents = _to_int(baseline.get("cost_cents"), 0)
            baseline_orders = _to_int(baseline.get("orders"), 0)
    return CampaignActivity(
        campaign_start_at=campaign_start_at,
        latest_metric_at=_as_utc(metrics.get("latest_metric_at")),
        today_cost_cents=today_cost,
        recent_snapshot_count=_to_int(metrics.get("recent_snapshot_count"), 0),
        low_spend_window_minutes=low_spend_window_minutes,
        low_spend_delta_cents=max(0, latest_cost_cents - baseline_cost_cents),
        low_spend_order_delta=max(0, latest_orders - baseline_orders),
        low_spend_latest_cost_cents=latest_cost_cents,
        low_spend_latest_orders=latest_orders,
    )


def _decide_no_spend_reset(
    db: Session,
    scope: CampaignScope,
    *,
    now: datetime,
) -> tuple[CreativeMetric, dict[str, Any]] | None:
    learning_protection = _learning_protection_config(scope)
    if bool(learning_protection.get("enabled", True)) and not bool(
        learning_protection.get("auto_rebuild_enabled", False)
    ):
        # GMV Max needs uninterrupted exploration.  A low/no-spend signal is
        # recorded by the smart guard but cannot recreate a campaign unless an
        # operator explicitly opts that strategy into automatic rebuilds.
        return None
    config = dict(scope.config.get("no_spend_reset") or {})
    if not bool(config.get("enabled", True)):
        return None
    if bool(config.get("require_active_campaign", True)) and not _campaign_can_update_creatives(scope):
        return None
    if _controlled_test_active(scope, now=now):
        logger.info(
            "creative guard campaign reset deferred during controlled test",
            extra={"campaign_id": scope.campaign_id},
        )
        return None
    cooling, cooldown_reason, paused_until = _campaign_pause_cooldown_active(db, scope, now=now)
    if cooling:
        logger.info(
            "creative guard no-spend reset deferred during smart cooldown",
            extra={
                "campaign_id": scope.campaign_id,
                "paused_until": paused_until.isoformat() if paused_until else None,
                "reason": cooldown_reason,
            },
        )
        return None

    activity = _load_campaign_activity(db, scope, now=now)
    grace_minutes = max(5, _to_int(config.get("grace_minutes"), 45))
    cooldown_minutes = max(grace_minutes, _to_int(config.get("cooldown_minutes"), 120))
    start_at = activity.campaign_start_at
    if start_at is None:
        return None
    if start_at > now:
        return None
    age_minutes = int((now - start_at).total_seconds() // 60)
    if age_minutes < grace_minutes:
        return None
    if _already_reset_campaign(db, scope):
        return None
    if activity.today_cost_cents > 0:
        if not bool(config.get("low_spend_enabled", True)):
            return None
        low_grace_minutes = max(
            grace_minutes,
            _to_int(config.get("low_spend_grace_minutes"), 90),
        )
        if age_minutes < low_grace_minutes:
            return None
        if activity.latest_metric_at is None:
            return None
        latest_age_minutes = int((now - activity.latest_metric_at).total_seconds() // 60)
        if latest_age_minutes > max(30, activity.low_spend_window_minutes + 30):
            return None
        if bool(config.get("low_spend_require_no_new_orders", True)) and activity.low_spend_order_delta > 0:
            return None
        budget_threshold = 0
        if scope.budget_cents > 0:
            budget_share = _to_decimal(config.get("low_spend_budget_share"), "0.03") or Decimal("0.03")
            budget_threshold = int(
                (Decimal(scope.budget_cents) * budget_share).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
        low_spend_threshold = max(
            _to_int(config.get("low_spend_min_delta_cents"), 100),
            budget_threshold,
        )
        if activity.low_spend_delta_cents >= low_spend_threshold:
            return None
        recent_reset = db.execute(
            text(
                """
                select id
                from gmv_campaign_guard_events
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and event_type='CREATIVE_GUARD'
                  and action='RESET_CAMPAIGN'
                  and result='SUCCESS'
                  and reason='creative_guard:low_spend_timeout'
                  and created_at >= :cutoff
                order by id desc
                limit 1
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "auth_id": scope.auth_id,
                "advertiser_id": scope.advertiser_id,
                "store_id": scope.store_id,
                "cutoff": (now - timedelta(minutes=cooldown_minutes)).replace(tzinfo=None),
            },
        ).first()
        if recent_reset:
            return None
        item_group_ids = _campaign_item_group_ids(db, scope)
        metric = CreativeMetric(
            creative_id="__campaign_low_spend__",
            item_group_id=item_group_ids[0] if item_group_ids else None,
            status=scope.secondary_status or scope.operation_status,
            cost_cents=activity.low_spend_delta_cents,
            gross_revenue_cents=0,
            orders=activity.low_spend_order_delta,
            product_impressions=0,
            product_clicks=0,
            ad_click_rate=None,
            product_click_rate=None,
            ad_conversion_rate=None,
            roi=None,
        )
        decision = {
            "action": "RESET_CAMPAIGN",
            "reason": "creative_guard:low_spend_timeout",
            "context": {
                "campaign_start_at": start_at.isoformat(),
                "age_minutes": age_minutes,
                "low_spend_grace_minutes": low_grace_minutes,
                "cooldown_minutes": cooldown_minutes,
                "today_cost_cents": activity.today_cost_cents,
                "latest_metric_at": activity.latest_metric_at.isoformat(),
                "recent_snapshot_count": activity.recent_snapshot_count,
                "low_spend_window_minutes": activity.low_spend_window_minutes,
                "low_spend_delta_cents": activity.low_spend_delta_cents,
                "low_spend_order_delta": activity.low_spend_order_delta,
                "low_spend_threshold_cents": low_spend_threshold,
                "low_spend_latest_cost_cents": activity.low_spend_latest_cost_cents,
                "low_spend_latest_orders": activity.low_spend_latest_orders,
                "item_group_ids": item_group_ids,
            },
        }
        return metric, decision

    recent_reset = db.execute(
        text(
            """
            select id
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and event_type='CREATIVE_GUARD'
              and action='RESET_CAMPAIGN'
              and result='SUCCESS'
              and reason='creative_guard:no_spend_timeout'
              and created_at >= :cutoff
            order by id desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "cutoff": (now - timedelta(minutes=cooldown_minutes)).replace(tzinfo=None),
        },
    ).first()
    if recent_reset:
        return None

    item_group_ids = _campaign_item_group_ids(db, scope)
    metric = CreativeMetric(
        creative_id="__campaign_no_spend__",
        item_group_id=item_group_ids[0] if item_group_ids else None,
        status=scope.secondary_status or scope.operation_status,
        cost_cents=0,
        gross_revenue_cents=0,
        orders=0,
        product_impressions=0,
        product_clicks=0,
        ad_click_rate=None,
        product_click_rate=None,
        ad_conversion_rate=None,
        roi=None,
    )
    decision = {
        "action": "RESET_CAMPAIGN",
        "reason": "creative_guard:no_spend_timeout",
        "context": {
            "campaign_start_at": start_at.isoformat(),
            "age_minutes": age_minutes,
            "grace_minutes": grace_minutes,
            "cooldown_minutes": cooldown_minutes,
            "today_cost_cents": activity.today_cost_cents,
            "latest_metric_at": activity.latest_metric_at.isoformat()
            if activity.latest_metric_at
            else None,
            "recent_snapshot_count": activity.recent_snapshot_count,
            "item_group_ids": item_group_ids,
        },
    }
    return metric, decision


def _load_product_price_basis(db: Session, scope: CampaignScope, item_group_id: str | None) -> dict[str, Any]:
    if not item_group_id:
        return {"cents": None, "source": "missing_item_group_id"}

    configured_prices = scope.config.get("product_effective_prices")
    if isinstance(configured_prices, Mapping):
        configured = configured_prices.get(str(item_group_id))
        cents = _money_to_cents(configured)
        if cents and cents > 0:
            return {"cents": cents, "source": "strategy_config"}

    configured_default = _money_to_cents(scope.config.get("default_effective_product_price"))
    if configured_default and configured_default > 0:
        return {"cents": configured_default, "source": "strategy_default"}

    lookback_days = max(1, _to_int(scope.config.get("order_value_lookback_days"), 90))
    order_row = db.execute(
        text(
            """
            select sum(coalesce(order_count, 0)) as orders,
                   sum(coalesce(gross_revenue_cents, 0)) as gross_revenue_cents
            from gmv_product_order_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and store_id=:store_id
              and item_group_id=:product_id
              and order_time_hour >= date_sub(utc_timestamp(6), interval :lookback_days day)
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "store_id": scope.store_id,
            "product_id": str(item_group_id),
            "lookback_days": lookback_days,
        },
    ).mappings().first()
    order_count = _to_int((order_row or {}).get("orders"), 0)
    gross_revenue_cents = _to_int((order_row or {}).get("gross_revenue_cents"), 0)
    if order_count > 0 and gross_revenue_cents > 0:
        return {
            "cents": int(Decimal(gross_revenue_cents) / Decimal(order_count)),
            "source": f"order_aov_{lookback_days}d",
        }

    row = db.execute(
        text(
            """
            select effective_price, price, min_price, max_price
            from ttb_products
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and store_id=:store_id
              and product_id=:product_id
            order by last_seen_at desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "store_id": scope.store_id,
            "product_id": str(item_group_id),
        },
    ).mappings().first()
    if not row:
        return {"cents": None, "source": "missing_product"}
    if bool(scope.config.get("use_effective_product_price", True)):
        cents = _money_to_cents(row.get("effective_price"))
        if cents and cents > 0:
            return {"cents": cents, "source": "ttb_products.effective_price"}
    for key in ("price", "min_price", "max_price"):
        cents = _money_to_cents(row.get(key))
        if cents and cents > 0:
            return {"cents": cents, "source": f"ttb_products.{key}"}
    return {"cents": None, "source": "price_missing"}


def _load_product_price_cents(db: Session, scope: CampaignScope, item_group_id: str | None) -> int | None:
    basis = _load_product_price_basis(db, scope, item_group_id)
    cents = basis.get("cents")
    return int(cents) if cents else None


def _campaign_expected_cvr(db: Session, scope: CampaignScope, item_group_id: str | None) -> Decimal:
    row = db.execute(
        text(
            """
            select sum(coalesce(orders, 0)) as orders,
                   sum(coalesce(product_clicks, 0)) as clicks
            from gmvmax_product_creative_metrics_daily
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and (:item_group_id is null or item_group_id=:item_group_id)
              and stat_time_day >= date_sub(curdate(), interval 30 day)
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "item_group_id": item_group_id,
        },
    ).mappings().first()
    orders = _to_int((row or {}).get("orders"), 0)
    clicks = _to_int((row or {}).get("clicks"), 0)
    if orders > 0 and clicks > 0:
        return Decimal(orders) / Decimal(clicks)
    fallback = _to_decimal(scope.config.get("expected_cvr_fallback"), "0.02") or Decimal("0.02")
    return max(Decimal("0.0001"), fallback)


def _target_roas(scope: CampaignScope) -> Decimal:
    configured = _to_decimal(scope.config.get("target_roas"))
    if configured and configured > 0:
        return configured
    if scope.roas_bid and scope.roas_bid > 0:
        return scope.roas_bid
    fallback = _to_decimal(scope.config.get("target_roas_fallback"), "1.2") or Decimal("1.2")
    return max(Decimal("0.1"), fallback)


def _safe_roi(gross_revenue_cents: int, cost_cents: int) -> Decimal | None:
    if cost_cents <= 0:
        return None
    return (Decimal(max(0, gross_revenue_cents)) / Decimal(cost_cents)).quantize(Decimal("0.0001"))


def _product_card_reset_protection(
    scope: CampaignScope,
    metric: CreativeMetric,
    campaign_metrics: list[CreativeMetric],
    reason_context: dict[str, Any],
) -> dict[str, Any] | None:
    reset_config = dict(scope.config.get("product_card_reset") or {})
    if not bool(reset_config.get("protect_good_campaign", True)):
        return None

    total_cost = sum(max(0, int(item.cost_cents or 0)) for item in campaign_metrics)
    total_gmv = sum(max(0, int(item.gross_revenue_cents or 0)) for item in campaign_metrics)
    total_orders = sum(max(0, int(item.orders or 0)) for item in campaign_metrics)
    video_metrics = [item for item in campaign_metrics if str(item.creative_id or "") != "-1"]
    video_cost = sum(max(0, int(item.cost_cents or 0)) for item in video_metrics)
    video_gmv = sum(max(0, int(item.gross_revenue_cents or 0)) for item in video_metrics)
    video_orders = sum(max(0, int(item.orders or 0)) for item in video_metrics)
    card_cost = max(0, int(metric.cost_cents or 0))
    card_share = (Decimal(card_cost) / Decimal(total_cost)).quantize(Decimal("0.0001")) if total_cost > 0 else Decimal("0")
    video_share = (Decimal(video_cost) / Decimal(total_cost)).quantize(Decimal("0.0001")) if total_cost > 0 else Decimal("0")
    campaign_roi = _safe_roi(total_gmv, total_cost)
    video_roi = _safe_roi(video_gmv, video_cost)

    min_card_share = _to_decimal(reset_config.get("min_product_card_spend_share"), "0.70") or Decimal("0.70")
    require_video_starvation = bool(reset_config.get("require_video_starvation", True))
    max_video_share = _to_decimal(reset_config.get("max_video_spend_share"), "0.20") or Decimal("0.20")
    campaign_roi_floor = _to_decimal(reset_config.get("campaign_roi_floor"), "0.8") or Decimal("0.8")
    video_roi_floor = _to_decimal(reset_config.get("video_roi_floor"), "1.0") or Decimal("1.0")
    campaign_min_orders = max(1, _to_int(reset_config.get("campaign_min_orders"), 1))
    video_min_orders = max(1, _to_int(reset_config.get("video_min_orders"), 1))

    protection_context = {
        "product_card_cost_cents": card_cost,
        "product_card_spend_share": str(card_share),
        "total_cost_cents": total_cost,
        "total_gmv_cents": total_gmv,
        "total_orders": total_orders,
        "campaign_roi": str(campaign_roi) if campaign_roi is not None else None,
        "video_cost_cents": video_cost,
        "video_spend_share": str(video_share),
        "video_gmv_cents": video_gmv,
        "video_orders": video_orders,
        "video_roi": str(video_roi) if video_roi is not None else None,
        "min_product_card_spend_share": str(min_card_share),
        "require_video_starvation": require_video_starvation,
        "max_video_spend_share": str(max_video_share),
        "campaign_roi_floor": str(campaign_roi_floor),
        "video_roi_floor": str(video_roi_floor),
        "campaign_min_orders": campaign_min_orders,
        "video_min_orders": video_min_orders,
    }
    reason_context["product_card_reset_protection"] = protection_context

    if total_cost <= 0:
        return {
            "action": "HOLD",
            "reason": "creative_guard:product_card_reset_deferred:no_campaign_spend",
            "context": reason_context,
        }
    if card_share < min_card_share:
        return {
            "action": "HOLD",
            "reason": "creative_guard:product_card_reset_deferred:card_spend_not_dominant",
            "context": reason_context,
        }
    if require_video_starvation and video_share > max_video_share:
        return {
            "action": "HOLD",
            "reason": "creative_guard:product_card_reset_deferred:videos_are_delivering",
            "context": reason_context,
        }
    if (
        total_orders >= campaign_min_orders
        and campaign_roi is not None
        and campaign_roi >= campaign_roi_floor
    ):
        return {
            "action": "HOLD",
            "reason": "creative_guard:product_card_reset_deferred:campaign_healthy",
            "context": reason_context,
        }
    if (
        video_orders >= video_min_orders
        and video_roi is not None
        and video_roi >= video_roi_floor
    ):
        return {
            "action": "HOLD",
            "reason": "creative_guard:product_card_reset_deferred:video_creatives_healthy",
            "context": reason_context,
        }
    return None


def _latest_successful_creative_action(
    db: Session,
    scope: CampaignScope,
    creative_id: str,
    *,
    campaign_only: bool = True,
) -> Mapping[str, Any] | None:
    campaign_clause = "and campaign_id=:campaign_id" if campaign_only else ""
    row = db.execute(
        text(
            f"""
            select id, campaign_id, action, reason, request_json, response_json, created_at,
                   cost_cents, gross_revenue_cents, orders, roi
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              {campaign_clause}
              and event_type='CREATIVE_GUARD'
              and action in ('REMOVE', 'ADD')
              and result='SUCCESS'
              and (request_json like :needle or response_json like :needle)
            order by created_at desc, id desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "needle": f'%"{creative_id}"%',
        },
    ).mappings().first()
    return row


def _already_removed(db: Session, scope: CampaignScope, creative_id: str) -> bool:
    row = _latest_successful_creative_action(db, scope, creative_id)
    return bool(row and str(row.get("action") or "").upper() == "REMOVE")


def _metric_snapshot(metric: CreativeMetric) -> dict[str, Any]:
    return {
        "cost_cents": int(metric.cost_cents or 0),
        "gross_revenue_cents": int(metric.gross_revenue_cents or 0),
        "orders": int(metric.orders or 0),
        "product_impressions": int(metric.product_impressions or 0),
        "product_clicks": int(metric.product_clicks or 0),
    }


def _delta_counter(current: int, baseline: Any) -> int:
    current_value = max(0, int(current or 0))
    baseline_value = max(0, _to_int(baseline, 0))
    return current_value - baseline_value if current_value >= baseline_value else current_value


def _metric_from_baseline(metric: CreativeMetric, baseline: Mapping[str, Any]) -> CreativeMetric:
    cost_cents = _delta_counter(metric.cost_cents, baseline.get("cost_cents"))
    gross_revenue_cents = _delta_counter(
        metric.gross_revenue_cents, baseline.get("gross_revenue_cents")
    )
    roi = (
        (Decimal(gross_revenue_cents) / Decimal(cost_cents)).quantize(Decimal("0.0001"))
        if cost_cents
        else None
    )
    return CreativeMetric(
        creative_id=metric.creative_id,
        item_group_id=metric.item_group_id,
        status=metric.status,
        cost_cents=cost_cents,
        gross_revenue_cents=gross_revenue_cents,
        orders=_delta_counter(metric.orders, baseline.get("orders")),
        product_impressions=_delta_counter(
            metric.product_impressions, baseline.get("product_impressions")
        ),
        product_clicks=_delta_counter(metric.product_clicks, baseline.get("product_clicks")),
        ad_click_rate=metric.ad_click_rate,
        product_click_rate=metric.product_click_rate,
        ad_conversion_rate=metric.ad_conversion_rate,
        roi=roi,
        video_view_rate_2s=metric.video_view_rate_2s,
        video_view_rate_6s=metric.video_view_rate_6s,
        video_view_rate_100=metric.video_view_rate_100,
    )


def _metric_for_current_retest_window(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
) -> tuple[CreativeMetric, dict[str, Any]]:
    latest = _latest_successful_creative_action(db, scope, metric.creative_id)
    if not latest or str(latest.get("action") or "").upper() != "ADD":
        return metric, {}
    payload = _json_dict(latest.get("request_json"))
    retest = _json_dict(payload.get("retest"))
    baseline = _json_dict(retest.get("baseline_metrics"))
    if not baseline:
        return metric, {"active": True, "event_id": latest.get("id")}
    return _metric_from_baseline(metric, baseline), {
        "active": True,
        "event_id": latest.get("id"),
        "attempt": _to_int(retest.get("attempt"), 1),
        "added_at": retest.get("added_at"),
        "baseline_metrics": baseline,
    }


def _creative_event_history(
    db: Session,
    scope: CampaignScope,
    creative_id: str,
) -> list[Mapping[str, Any]]:
    return list(
        db.execute(
            text(
                """
                select id, action, reason, request_json, response_json, created_at,
                       cost_cents, gross_revenue_cents, orders, roi
                from gmv_campaign_guard_events
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and campaign_id=:campaign_id
                  and event_type='CREATIVE_GUARD'
                  and action in ('REMOVE', 'ADD')
                  and result='SUCCESS'
                  and (request_json like :needle or response_json like :needle)
                order by created_at asc, id asc
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "auth_id": scope.auth_id,
                "advertiser_id": scope.advertiser_id,
                "store_id": scope.store_id,
                "campaign_id": scope.campaign_id,
                "needle": f'%"{creative_id}"%',
            },
        ).mappings().all()
    )


def _failed_retest_state(
    db: Session,
    scope: CampaignScope,
    creative_id: str,
) -> tuple[int, datetime | None]:
    """Return attributable failed add-back attempts for one creative.

    Older failed events did not persist the creative id and cannot be safely
    attributed.  New events include an explicit top-level id so they can drive
    durable retry backoff without blocking unrelated candidates.
    """

    row = db.execute(
        text(
            """
            select count(*) as failure_count, max(created_at) as latest_failure_at
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and event_type='CREATIVE_GUARD'
              and action='ADD'
              and reason='creative_guard:scheduled_retest'
              and result='FAILED'
              and request_json like :creative_needle
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "creative_needle": f'%"creative_id":%"{creative_id}"%',
        },
    ).mappings().first()
    if not row:
        return 0, None
    return (
        max(0, _to_int(row.get("failure_count"), 0)),
        _as_utc(row.get("latest_failure_at")),
    )


def _advertiser_local_datetime(db: Session, scope: CampaignScope, value: datetime) -> datetime:
    timestamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    timezone_name = _advertiser_timezone_name(db, scope)
    if timezone_name:
        try:
            return timestamp.astimezone(ZoneInfo(str(timezone_name)))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timestamp.astimezone(timezone.utc)


def _creative_time_bucket(
    db: Session,
    scope: CampaignScope,
    value: datetime,
    *,
    bucket_hours: int,
) -> str:
    local = _advertiser_local_datetime(db, scope, value)
    hours = max(1, min(24, int(bucket_hours or 4)))
    start_hour = (local.hour // hours) * hours
    return f"{local.date().isoformat()}:{start_hour:02d}-{min(24, start_hour + hours):02d}"


def _creative_quality_context(
    metric: CreativeMetric,
    campaign_metrics: list[CreativeMetric],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    ctr = (
        (Decimal(metric.product_clicks) * Decimal("100") / Decimal(metric.product_impressions))
        if metric.product_impressions > 0
        else (metric.product_click_rate or metric.ad_click_rate)
    )
    total_impressions = sum(max(0, int(item.product_impressions or 0)) for item in campaign_metrics)
    total_clicks = sum(max(0, int(item.product_clicks or 0)) for item in campaign_metrics)
    campaign_ctr = (
        Decimal(total_clicks) * Decimal("100") / Decimal(total_impressions)
        if total_impressions > 0
        else None
    )
    video_metrics = [item for item in campaign_metrics if str(item.creative_id or "") != "-1"]

    def average(field: str) -> Decimal | None:
        values = [getattr(item, field) for item in video_metrics if getattr(item, field) is not None]
        return sum(values, Decimal("0")) / Decimal(len(values)) if values else None

    campaign_6s = average("video_view_rate_6s")
    campaign_completion = average("video_view_rate_100")
    relative = _to_decimal(config.get("quality_relative_ratio"), "1.0") or Decimal("1.0")
    ctr_floor = _to_decimal(config.get("quality_ctr_floor"), "1.0") or Decimal("1.0")
    video_6s_floor = _to_decimal(config.get("quality_video_6s_floor"), "8.0") or Decimal("8.0")
    completion_floor = (
        _to_decimal(config.get("quality_completion_floor"), "5.0") or Decimal("5.0")
    )
    ctr_good = bool(
        ctr is not None
        and ctr >= ctr_floor
        and (campaign_ctr is None or ctr >= campaign_ctr * relative)
    )
    six_second_good = bool(
        metric.video_view_rate_6s is not None
        and metric.video_view_rate_6s >= video_6s_floor
        and (campaign_6s is None or metric.video_view_rate_6s >= campaign_6s * relative)
    )
    completion_good = bool(
        metric.video_view_rate_100 is not None
        and metric.video_view_rate_100 >= completion_floor
        and (
            campaign_completion is None
            or metric.video_view_rate_100 >= campaign_completion * relative
        )
    )
    quality_score = sum((3 if ctr_good else 0, 2 if six_second_good else 0, 2 if completion_good else 0))
    return {
        "high_quality": quality_score >= 2,
        "quality_score": quality_score,
        "ctr": str(ctr.quantize(Decimal("0.0001"))) if ctr is not None else None,
        "campaign_ctr": str(campaign_ctr.quantize(Decimal("0.0001"))) if campaign_ctr is not None else None,
        "video_view_rate_6s": str(metric.video_view_rate_6s) if metric.video_view_rate_6s is not None else None,
        "campaign_video_view_rate_6s": str(campaign_6s) if campaign_6s is not None else None,
        "video_completion_rate": str(metric.video_view_rate_100) if metric.video_view_rate_100 is not None else None,
        "campaign_video_completion_rate": str(campaign_completion) if campaign_completion is not None else None,
        "signals": {
            "ctr": ctr_good,
            "video_6s": six_second_good,
            "completion": completion_good,
        },
    }


def _dynamic_retest_cooldown_minutes(
    config: Mapping[str, Any],
    *,
    prior_attempts: int,
    high_quality: bool,
    low_delivery: bool,
) -> int:
    cooldown = Decimal(max(1, _to_int(config.get("base_cooldown_minutes"), 120)))
    multiplier = (
        _to_decimal(config.get("failure_cooldown_multiplier"), "1.75") or Decimal("1.75")
    )
    cooldown *= multiplier ** max(0, int(prior_attempts or 0))
    if high_quality:
        cooldown *= _to_decimal(config.get("high_quality_cooldown_ratio"), "0.5") or Decimal("0.5")
    if low_delivery:
        cooldown *= _to_decimal(config.get("low_delivery_cooldown_ratio"), "0.5") or Decimal("0.5")
    minimum = max(1, _to_int(config.get("min_cooldown_minutes"), 45))
    maximum = max(minimum, _to_int(config.get("max_cooldown_minutes"), 1440))
    return max(minimum, min(maximum, int(cooldown.quantize(Decimal("1"), rounding=ROUND_HALF_UP))))


def _campaign_pause_cooldown_active(
    db: Session,
    scope: CampaignScope,
    *,
    now: datetime,
) -> tuple[bool, str | None, datetime | None]:
    """Return true when smart guard has paused this campaign into a cooling window."""

    row = db.execute(
        text(
            """
            select paused_until, last_reason
            from gmv_campaign_realtime_state
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            order by updated_at desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    if not row or not row.get("paused_until"):
        return False, None, None
    paused_until_raw = row.get("paused_until")
    paused_until: datetime | None = None
    if isinstance(paused_until_raw, datetime):
        paused_until = paused_until_raw
    else:
        try:
            paused_until = datetime.fromisoformat(str(paused_until_raw))
        except ValueError:
            paused_until = None
    if paused_until is None:
        return False, str(row.get("last_reason") or ""), None
    if paused_until.tzinfo is None:
        paused_until = paused_until.replace(tzinfo=timezone.utc)
    comparable_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return comparable_now < paused_until, str(row.get("last_reason") or ""), paused_until


def _already_reset_campaign(db: Session, scope: CampaignScope) -> bool:
    row = db.execute(
        text(
            """
            select id
            from gmv_campaign_guard_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and event_type='CREATIVE_GUARD'
              and action='RESET_CAMPAIGN'
              and result='SUCCESS'
            order by id desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).first()
    return row is not None


def _product_rebuild_cooldown_decision(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    config = dict(scope.config.get("product_rebuild_cooldown") or {})
    if not bool(config.get("enabled", True)):
        return None
    item_group_ids = _campaign_item_group_ids(db, scope)
    if metric.item_group_id:
        item_group_ids = sorted({*item_group_ids, str(metric.item_group_id)})
    if not item_group_ids:
        return None
    lookback_minutes = max(30, _to_int(config.get("lookback_minutes"), 180))
    max_recent_resets = max(1, _to_int(config.get("max_recent_resets"), 2))
    stmt = text(
        """
        select count(*) as reset_count,
               count(distinct e.campaign_id) as campaign_count,
               max(e.created_at) as last_reset_at
        from gmv_campaign_guard_events e
        where e.workspace_id=:workspace_id
          and e.auth_id=:auth_id
          and e.advertiser_id=:advertiser_id
          and e.store_id=:store_id
          and e.event_type='CREATIVE_GUARD'
          and e.action='RESET_CAMPAIGN'
          and e.result='SUCCESS'
          and e.created_at >= :cutoff
          and exists (
              select 1
              from gmvmax_product_campaign_item_groups ig
              where ig.workspace_id=e.workspace_id
                and ig.auth_id=e.auth_id
                and ig.advertiser_id=e.advertiser_id
                and ig.campaign_id=e.campaign_id
                and ig.item_group_id in :item_group_ids
          )
        """
    ).bindparams(bindparam("item_group_ids", expanding=True))
    row = db.execute(
        stmt,
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "cutoff": (now - timedelta(minutes=lookback_minutes)).replace(tzinfo=None),
            "item_group_ids": [str(item) for item in item_group_ids],
        },
    ).mappings().first() or {}
    reset_count = _to_int(row.get("reset_count"), 0)
    if reset_count < max_recent_resets:
        return None
    context = dict(decision.get("context") or {}) if isinstance(decision.get("context"), Mapping) else {}
    context.update(
        {
            "defer_recreate": True,
            "product_rebuild_cooldown": True,
            "recent_reset_count": reset_count,
            "recent_reset_campaign_count": _to_int(row.get("campaign_count"), 0),
            "last_reset_at": str(row.get("last_reset_at")) if row.get("last_reset_at") else None,
            "lookback_minutes": lookback_minutes,
            "max_recent_resets": max_recent_resets,
            "original_reason": decision.get("reason"),
        }
    )
    return {
        "action": "HOLD",
        "reason": "creative_guard:product_rebuild_deferred:cooldown",
        "context": context,
    }


def _campaign_can_update_creatives(scope: CampaignScope) -> bool:
    operation_status = str(scope.operation_status or "").upper()
    secondary_status = str(scope.secondary_status or "").upper()
    if operation_status in {"DISABLE", "DISABLED"}:
        return False
    if secondary_status in {"CAMPAIGN_STATUS_DISABLE", "CAMPAIGN_STATUS_DISABLED"}:
        return False
    return True


def _manual_pause_override_active(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    return is_manual_pause_override_active(
        db,
        workspace_id=scope.workspace_id,
        auth_id=scope.auth_id,
        advertiser_id=scope.advertiser_id,
        store_id=scope.store_id,
        campaign_id=str(campaign_id or scope.campaign_id),
        now=now,
    )


def _campaign_is_currently_enabled(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str | None = None,
) -> bool:
    """Re-read mutable campaign state before a destructive remote operation."""
    target_campaign_id = str(campaign_id or scope.campaign_id)
    row = db.execute(
        text(
            """
            select c.operation_status as catalog_operation_status,
                   c.secondary_status as catalog_secondary_status,
                   r.operation_status as realtime_operation_status,
                   r.secondary_status as realtime_secondary_status
            from gmvmax_product_campaign_catalog c
            left join gmv_campaign_realtime_state r
              on r.workspace_id=c.workspace_id
             and r.auth_id=c.auth_id
             and r.advertiser_id=c.advertiser_id
             and r.store_id=c.store_id
             and r.campaign_id=c.campaign_id
            where c.workspace_id=:workspace_id
              and c.auth_id=:auth_id
              and c.advertiser_id=:advertiser_id
              and c.store_id=:store_id
              and c.campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": target_campaign_id,
        },
    ).mappings().first()
    if not row:
        return False
    operation_status = str(
        row.get("realtime_operation_status")
        or row.get("catalog_operation_status")
        or ""
    ).upper()
    secondary_status = str(
        row.get("realtime_secondary_status")
        or row.get("catalog_secondary_status")
        or ""
    ).upper()
    if target_campaign_id == str(scope.campaign_id):
        scope.operation_status = operation_status or scope.operation_status
        scope.secondary_status = secondary_status or scope.secondary_status
    if "DISABLE" in operation_status or "DELETE" in operation_status:
        return False
    if "DISABLE" in secondary_status or "DELETE" in secondary_status:
        return False
    return (
        operation_status in {"ENABLE", "ENABLED"}
        or "CAMPAIGN_STATUS_ENABLE" in secondary_status
        or "DELIVERY_OK" in secondary_status
    )


def _assert_campaign_supports_creative_status_update(
    db: Session,
    scope: CampaignScope,
) -> None:
    """Fail closed before calling an endpoint unsupported by custom selection."""

    row = db.execute(
        text(
            """
            select product_specific_type, detail_raw_json
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    if row is None:
        raise CreativeGuardAutomationHold(
            "creative_guard: campaign catalog record is unavailable"
        )
    detail = _json_dict(row.get("detail_raw_json"))
    selection_type = str(
        row.get("product_specific_type")
        or detail.get("product_video_specific_type")
        or detail.get("video_specific_type")
        or ""
    ).upper()
    if "CUSTOM" in selection_type:
        raise CreativeGuardAutomationHold(
            "creative_guard: custom video selection does not support creative status update"
        )


def _assert_creative_guard_mutation_allowed(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str | None = None,
    require_enabled: bool = True,
    allow_creative_rebuild_intent: bool = False,
) -> None:
    assert_gmvmax_mutation_current(db)
    target_campaign_id = str(campaign_id or scope.campaign_id)
    strategy_row = db.execute(
        text(
            """
            select enabled, config_json
            from gmv_strategy_configs
            where id=:strategy_id
              and workspace_id=:workspace_id
              and auth_id=:auth_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "strategy_id": scope.strategy_id,
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    if not strategy_row or not bool(strategy_row.get("enabled")):
        raise CreativeGuardAutomationHold(
            "creative_guard: strategy is no longer enabled"
        )
    strategy_config = strategy_row.get("config_json") or {}
    if isinstance(strategy_config, str):
        try:
            strategy_config = json.loads(strategy_config)
        except json.JSONDecodeError:
            strategy_config = {}
    creation_quarantine = (
        strategy_config.get("creation_quarantine")
        if isinstance(strategy_config, Mapping)
        else None
    )
    if isinstance(creation_quarantine, Mapping) and bool(
        creation_quarantine.get("enabled")
    ):
        raise CreativeGuardAutomationHold(
            "creative_guard: campaign creation is quarantined"
        )
    owned_rebuild_inflight = False
    unfinished_create = db.execute(
        text(
            """
            select state, request_json
            from gmvmax_campaign_create_intents
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and state in (
                    'PREPARED',
                    'SUBMITTING',
                    'UNKNOWN',
                    'REMOTE_CREATED',
                    'FINALIZING',
                    'COMPENSATION_PENDING',
                    'QUARANTINE_PENDING',
                    'QUARANTINED'
              )
              and (
                    campaign_id=:target_campaign_id
                 or replacement_campaign_id=:scope_campaign_id
              )
            order by id desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "target_campaign_id": target_campaign_id,
            "scope_campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    if unfinished_create:
        request_json = unfinished_create.get("request_json") or {}
        if isinstance(request_json, str):
            try:
                request_json = json.loads(request_json)
            except json.JSONDecodeError:
                request_json = {}
        automation = (
            request_json.get("automation")
            if isinstance(request_json, Mapping)
            else None
        )
        is_owned_rebuild = (
            allow_creative_rebuild_intent
            and isinstance(automation, Mapping)
            and str(automation.get("source") or "")
            == "creative_guard_rebuild"
        )
        owned_rebuild_inflight = is_owned_rebuild
        if not is_owned_rebuild:
            raise CreativeGuardAutomationHold(
                "creative_guard: campaign creation has not finalized"
            )
    if _manual_pause_override_active(
        db,
        scope,
        campaign_id=target_campaign_id,
    ):
        raise CreativeGuardAutomationHold(
            "creative_guard: manual pause override requires HOLD"
        )
    if require_enabled and not owned_rebuild_inflight and not _campaign_is_currently_enabled(
        db,
        scope,
        campaign_id=target_campaign_id,
    ):
        raise CreativeGuardAutomationHold(
            "creative_guard: campaign is not currently enabled"
        )


def _mark_campaign_disabled_best_effort(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str | None = None,
) -> bool:
    """Keep catalog and realtime state aligned after a remote pause."""
    target_campaign_id = str(campaign_id or scope.campaign_id)
    mutation_observed_at = catalog_observation_now()
    parameters = {
        "workspace_id": scope.workspace_id,
        "auth_id": scope.auth_id,
        "advertiser_id": scope.advertiser_id,
        "store_id": scope.store_id,
        "campaign_id": target_campaign_id,
        "observed_at": mutation_observed_at,
    }
    catalog_result = db.execute(
        text(
            """
            update gmvmax_product_campaign_catalog
            set operation_status='DISABLE',
                secondary_status='CAMPAIGN_STATUS_DISABLE',
                list_synced_at=:observed_at,
                detail_synced_at=:observed_at,
                modify_time_utc=:observed_at,
                updated_at=:observed_at
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        parameters,
    )
    if getattr(catalog_result, "rowcount", 1) != 1:
        return False
    db.execute(
        text(
            """
            update gmv_campaign_realtime_state
            set operation_status='DISABLE',
                secondary_status='CAMPAIGN_STATUS_DISABLE',
                updated_at=:observed_at
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        parameters,
    )
    if target_campaign_id == str(scope.campaign_id):
        scope.operation_status = "DISABLE"
        scope.secondary_status = "CAMPAIGN_STATUS_DISABLE"
    return True


def _mark_campaign_enabled_best_effort(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str,
) -> bool:
    """Keep catalog and realtime state aligned after a remote enable."""

    target_campaign_id = str(campaign_id)
    mutation_observed_at = catalog_observation_now()
    parameters = {
        "workspace_id": scope.workspace_id,
        "auth_id": scope.auth_id,
        "advertiser_id": scope.advertiser_id,
        "store_id": scope.store_id,
        "campaign_id": target_campaign_id,
        "observed_at": mutation_observed_at,
    }
    catalog_result = db.execute(
        text(
            """
            update gmvmax_product_campaign_catalog
            set operation_status='ENABLE',
                secondary_status='CAMPAIGN_STATUS_ENABLE',
                list_synced_at=:observed_at,
                detail_synced_at=:observed_at,
                modify_time_utc=:observed_at,
                updated_at=:observed_at
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        parameters,
    )
    if getattr(catalog_result, "rowcount", 1) != 1:
        return False
    db.execute(
        text(
            """
            update gmv_campaign_realtime_state
            set operation_status='ENABLE',
                secondary_status='CAMPAIGN_STATUS_ENABLE',
                updated_at=:observed_at
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        parameters,
    )
    return True


def _persist_duplicate_pause_result(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str,
    request_payload: Mapping[str, Any],
    response_payload: Mapping[str, Any],
    result: str,
    remote_paused: bool,
) -> bool:
    """Persist one duplicate result without letting a bad row poison the batch."""

    mutation_observed_at = catalog_observation_now()
    try:
        if remote_paused:
            db.execute(
                text(
                    """
                    update gmvmax_product_campaign_catalog
                    set operation_status='DISABLE',
                        secondary_status='CAMPAIGN_STATUS_DISABLE',
                        list_synced_at=:observed_at,
                        detail_synced_at=:observed_at,
                        modify_time_utc=:observed_at,
                        updated_at=:observed_at
                    where workspace_id=:workspace_id
                      and auth_id=:auth_id
                      and advertiser_id=:advertiser_id
                      and store_id=:store_id
                      and campaign_id=:campaign_id
                    """
                ),
                {
                    "workspace_id": scope.workspace_id,
                    "auth_id": scope.auth_id,
                    "advertiser_id": scope.advertiser_id,
                    "store_id": scope.store_id,
                    "campaign_id": campaign_id,
                    "observed_at": mutation_observed_at,
                },
            )
        db.execute(
            text(
                """
                insert into gmv_campaign_guard_events (
                    workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                    strategy_id, event_type, action, reason, result,
                    request_json, response_json, created_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                    :strategy_id, 'CREATIVE_GUARD', 'PAUSE',
                    'creative_guard:pause_unmanaged_duplicate_campaign',
                    :result, cast(:request_json as json), cast(:response_json as json),
                    current_timestamp(6)
                )
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "auth_id": scope.auth_id,
                "advertiser_id": scope.advertiser_id,
                "store_id": scope.store_id,
                "campaign_id": campaign_id,
                "strategy_id": scope.strategy_id,
                "result": str(result),
                "request_json": _json_dumps(request_payload),
                "response_json": _json_dumps(response_payload),
            },
        )
        db.flush()
        return True
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "creative guard failed to persist one duplicate campaign result",
            extra={
                "campaign_id": campaign_id,
                "managed_campaign_id": scope.campaign_id,
                "remote_paused": remote_paused,
                "result": result,
            },
        )
        return False


async def _pause_unmanaged_duplicate_campaigns(db: Session, scope: CampaignScope) -> int:
    if _manual_pause_override_active(db, scope):
        return 0
    item_group_ids = _campaign_item_group_ids(db, scope)
    if not item_group_ids:
        return 0
    candidate_ids: set[str] = set()
    for item_group_id in item_group_ids:
        rows = db.execute(
            text(
                """
                select c.workspace_id, c.auth_id, c.advertiser_id, c.store_id, c.campaign_id,
                       c.campaign_name, s.id as strategy_id, s.enabled as strategy_enabled
                from gmvmax_product_campaign_catalog c
                left join gmv_strategy_configs s
                  on s.workspace_id=c.workspace_id
                 and s.auth_id=c.auth_id
                 and s.campaign_id=c.campaign_id
                where c.workspace_id=:workspace_id
                  and c.auth_id=:auth_id
                  and c.advertiser_id=:advertiser_id
                  and c.store_id=:store_id
                  and c.campaign_id<>:campaign_id
                  and c.operation_status='ENABLE'
                  and s.id is not null
                  and coalesce(s.enabled, 0)=0
                  and (
                        exists (
                            select 1
                            from gmvmax_product_campaign_item_groups ig
                            where ig.workspace_id=c.workspace_id
                              and ig.auth_id=c.auth_id
                              and ig.advertiser_id=c.advertiser_id
                              and ig.store_id=c.store_id
                              and ig.campaign_id=c.campaign_id
                              and ig.item_group_id=:item_group_id
                        )
                     or json_search(c.detail_raw_json, 'one', :item_group_id, null, '$.item_group_ids') is not null
                  )
                order by c.campaign_id
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "auth_id": scope.auth_id,
                "advertiser_id": scope.advertiser_id,
                "store_id": scope.store_id,
                "campaign_id": scope.campaign_id,
                "item_group_id": str(item_group_id),
            },
        ).mappings().all()
        for row in rows:
            campaign_id = str(row.get("campaign_id") or "").strip()
            if campaign_id:
                candidate_ids.add(campaign_id)
    if not candidate_ids:
        return 0

    paused = 0
    with gmvmax_mutation_lease(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        owner_prefix="creative-guard-duplicate-pause",
        timeout=0.1,
    ) as mutation:
        client = build_ttb_gmvmax_client(
            db,
            auth_id=scope.auth_id,
            timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
        )
        try:
            for campaign_id in sorted(candidate_ids):
                try:
                    _assert_creative_guard_mutation_allowed(
                        db,
                        scope,
                        campaign_id=campaign_id,
                        require_enabled=True,
                    )
                except CreativeGuardAutomationHold:
                    continue
                request = CampaignStatusUpdateRequest(
                    advertiser_id=str(scope.advertiser_id),
                    campaign_ids=[campaign_id],
                    operation_status="DISABLE",
                )
                try:
                    mutation.assert_current(db)
                    response = await client.campaign_status_update(request)
                    mutation.assert_current(db)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "creative guard failed to pause one unmanaged duplicate campaign",
                        exc_info=True,
                        extra={
                            "campaign_id": campaign_id,
                            "managed_campaign_id": scope.campaign_id,
                        },
                    )
                    if isinstance(
                        exc,
                        (GmvMaxMutationBusy, GmvMaxMutationFenceLost),
                    ):
                        raise
                    _persist_duplicate_pause_result(
                        db,
                        scope,
                        campaign_id=campaign_id,
                        request_payload=request.model_dump(exclude_none=True),
                        response_payload={"error": str(exc)},
                        result="FAILED",
                        remote_paused=False,
                    )
                    mutation.commit(db)
                    continue
                response_payload = (
                    response.model_dump(exclude_none=True)
                    if hasattr(response, "model_dump")
                    else {"request_id": getattr(response, "request_id", None)}
                )
                if not _persist_duplicate_pause_result(
                    db,
                    scope,
                    campaign_id=campaign_id,
                    request_payload=request.model_dump(exclude_none=True),
                    response_payload=response_payload,
                    result="SUCCESS",
                    remote_paused=True,
                ):
                    raise RuntimeError(
                        "remote duplicate pause succeeded but local state failed"
                    )
                mutation.commit(db)
                paused += 1
        finally:
            await client.aclose()
    return paused


def _clone_campaign_body(db: Session, scope: CampaignScope) -> GMVMaxCampaignCreateBody:
    row = db.execute(
        text(
            """
            select detail_raw_json, campaign_name, store_id, shopping_ads_type,
                   optimization_goal, deep_bid_type, product_specific_type,
                   budget_cents, roas_bid, schedule_type
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).mappings().first()
    if not row:
        raise RuntimeError(f"campaign catalog not found: {scope.campaign_id}")

    raw = row.get("detail_raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    payload = dict(raw) if isinstance(raw, Mapping) else {}
    allowed_fields = set(GMVMaxCampaignCreateBody.model_fields)
    payload = {key: value for key, value in payload.items() if key in allowed_fields}
    for key in (
        "campaign_id",
        "request_id",
        "operation_status",
        "schedule_start_time",
        "schedule_end_time",
        "identity_list",
    ):
        payload.pop(key, None)

    timezone_name = _advertiser_timezone_name(db, scope)
    try:
        advertiser_now = datetime.now(ZoneInfo(str(timezone_name))) if timezone_name else _utcnow()
    except (ZoneInfoNotFoundError, ValueError):
        advertiser_now = _utcnow()
    suffix = advertiser_now.strftime("%Y%m%d_%H%M%S")
    base_name = _base_campaign_name(str(row.get("campaign_name") or scope.campaign_name or "GMV Max"))
    payload["campaign_name"] = f"{base_name}__pc_reset_{suffix}"[:255]
    payload["store_id"] = str(payload.get("store_id") or row.get("store_id") or scope.store_id)
    payload["shopping_ads_type"] = str(payload.get("shopping_ads_type") or row.get("shopping_ads_type") or "PRODUCT")
    payload["optimization_goal"] = str(payload.get("optimization_goal") or row.get("optimization_goal") or "VALUE")
    if row.get("deep_bid_type") and not payload.get("deep_bid_type"):
        payload["deep_bid_type"] = row.get("deep_bid_type")
    if row.get("product_specific_type") and not payload.get("product_specific_type"):
        payload["product_specific_type"] = row.get("product_specific_type")
    raw_item_group_ids = [
        str(item).strip()
        for item in (payload.get("item_group_ids") or [])
        if str(item or "").strip()
    ]
    raw_item_group_ids = list(dict.fromkeys(raw_item_group_ids))
    normalized_item_group_ids = _normalized_campaign_item_group_ids(db, scope)
    # The normalized current relation is authoritative. Raw campaign detail is
    # only a compatibility fallback when the relation has not been populated;
    # historical metric rows must never resurrect removed products.
    item_group_ids = normalized_item_group_ids or raw_item_group_ids
    if len(item_group_ids) > 50:
        raise CreativeGuardAutomationHold(
            "creative_guard: campaign product evidence exceeds the official "
            f"50-SPU create limit ({len(item_group_ids)}); refusing a partial rebuild"
        )
    if item_group_ids:
        payload["item_group_ids"] = item_group_ids
    elif str(payload.get("product_specific_type") or "").upper() == "CUSTOMIZED_PRODUCTS":
        raise CreativeGuardAutomationHold(
            "creative_guard: current campaign product relation is missing; "
            "refusing to rebuild from historical metrics"
        )
    if row.get("budget_cents") is not None and not payload.get("budget"):
        payload["budget"] = float(Decimal(int(row["budget_cents"])) / Decimal("100"))
    if row.get("roas_bid") is not None and not payload.get("roas_bid"):
        payload["roas_bid"] = float(row["roas_bid"])
    # A replacement is born ENABLED by the official API.  Use a real future
    # schedule (SCHEDULE_FROM_NOW discards schedule_start_time) so a worker
    # crash between CREATE and DISABLE cannot spend.
    payload["schedule_type"] = "SCHEDULE_START_END"
    safety_minutes = max(
        10,
        _to_int(scope.config.get("rebuild_schedule_safety_minutes"), 30),
    )
    # TikTok expects schedule values in the ad account timezone, without an
    # offset suffix. UTC here can postpone an America/New_York campaign by hours.
    schedule_start = advertiser_now + timedelta(minutes=safety_minutes)
    schedule_end = schedule_start + timedelta(days=3650)
    payload["schedule_start_time"] = schedule_start.strftime("%Y-%m-%d %H:%M:%S")
    payload["schedule_end_time"] = schedule_end.strftime("%Y-%m-%d %H:%M:%S")
    return GMVMaxCampaignCreateBody(**payload)


async def _apply_official_bid_recommendation(
    client: Any,
    scope: CampaignScope,
    body: GMVMaxCampaignCreateBody,
) -> tuple[GMVMaxCampaignCreateBody, dict[str, Any]]:
    item_group_ids = [str(item) for item in (body.item_group_ids or []) if str(item or "").strip()]
    if not item_group_ids:
        return body, {"applied": False, "reason": "missing_item_group_ids"}
    request = GMVMaxBidRecommendRequest(
        advertiser_id=str(scope.advertiser_id),
        store_id=str(scope.store_id),
        shopping_ads_type=str(body.shopping_ads_type),
        optimization_goal=str(body.optimization_goal),
        item_group_ids=item_group_ids,
    )
    try:
        response = await client.gmv_max_bid_recommend(request)
        data = getattr(response, "data", None)
        updates: dict[str, Any] = {}
        budget = _to_decimal(getattr(data, "budget", None))
        roas_bid = _to_decimal(getattr(data, "roas_bid", None))
        if budget is not None and budget > 0:
            updates["budget"] = float(budget.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if roas_bid is not None and roas_bid > 0:
            updates["roas_bid"] = float(roas_bid.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        if not updates:
            return body, {
                "applied": False,
                "reason": "empty_recommendation",
                "request_id": getattr(response, "request_id", None),
            }
        payload = body.model_dump()
        payload.update(updates)
        return GMVMaxCampaignCreateBody.model_validate(payload), {
            "applied": True,
            "budget": updates.get("budget"),
            "roas_bid": updates.get("roas_bid"),
            "request_id": getattr(response, "request_id", None),
        }
    except GmvMaxMutationFenceLost:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "TikTok bid recommendation unavailable during campaign rebuild",
            exc_info=True,
            extra={"campaign_id": scope.campaign_id},
        )
        return body, {"applied": False, "reason": "api_error", "error": str(exc)}


async def _load_rebuild_identity_list(
    client: Any,
    scope: CampaignScope,
    *,
    store_authorized_bc_id: str | None,
) -> list[GMVMaxIdentityInfo]:
    if not store_authorized_bc_id:
        return []
    response = await client.gmv_max_identity_get(
        GMVMaxIdentityGetRequest(
            advertiser_id=str(scope.advertiser_id),
            store_id=str(scope.store_id),
            store_authorized_bc_id=str(store_authorized_bc_id),
        )
    )
    identities: list[GMVMaxIdentityInfo] = []
    seen: set[tuple[str, str]] = set()
    for entry in getattr(getattr(response, "data", None), "identity_list", []) or []:
        if getattr(entry, "product_gmv_max_available", None) is False:
            continue
        info = getattr(entry, "identity_info", None)
        raw = entry.model_dump(exclude_none=True) if hasattr(entry, "model_dump") else {}
        identity_id = str(
            getattr(info, "identity_id", None)
            or raw.get("identity_id")
            or ""
        ).strip()
        identity_type = (
            getattr(info, "identity_type", None)
            or raw.get("identity_type")
            or None
        )
        identity_key = (identity_id, str(identity_type or ""))
        if not identity_id or not identity_type or identity_key in seen:
            continue
        seen.add(identity_key)
        payload: dict[str, Any] = {
            "identity_id": identity_id,
            "identity_type": str(identity_type),
            "user_name": (
                getattr(info, "user_name", None)
                or raw.get("user_name")
                or raw.get("display_name")
            ),
            "profile_image": getattr(info, "profile_image", None) or raw.get("profile_image"),
            "store_id": str(raw.get("store_id") or scope.store_id),
        }
        if raw.get("identity_authorized_bc_id"):
            payload["identity_authorized_bc_id"] = str(raw["identity_authorized_bc_id"])
        if raw.get("identity_authorized_shop_id"):
            payload["identity_authorized_shop_id"] = str(raw["identity_authorized_shop_id"])
        identities.append(GMVMaxIdentityInfo(**{key: value for key, value in payload.items() if value}))
        if len(identities) >= 20:
            break
    return identities


def _durable_rebuild_create_request(
    db: Session,
    scope: CampaignScope,
    body: GMVMaxCampaignCreateBody,
    *,
    historical_creatives: Sequence[tuple[str, str | None]] | None = None,
) -> CreateCampaignRequest:
    """Reuse a frozen replacement request or create one stable logical intent."""

    existing = db.execute(
        text(
            """
            select request_json
            from gmvmax_campaign_create_intents
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and replacement_campaign_id=:replacement_campaign_id
              and state in (
                    'PREPARED',
                    'SUBMITTING',
                    'UNKNOWN',
                    'REMOTE_CREATED',
                    'FINALIZING',
                    'COMPENSATION_PENDING',
                    'QUARANTINE_PENDING',
                    'QUARANTINED'
              )
            order by id desc
            limit 1
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "replacement_campaign_id": scope.campaign_id,
        },
    ).scalar_one_or_none()
    if existing:
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except json.JSONDecodeError as exc:
                raise CreativeGuardAutomationHold(
                    "creative_guard: frozen rebuild request is invalid"
                ) from exc
        request = CreateCampaignRequest.model_validate(existing)
        if (
            str(request.store_id) != str(scope.store_id)
            or str(request.advertiser_id or scope.advertiser_id)
            != str(scope.advertiser_id)
            or str(request.replacement_campaign_id or "")
            != str(scope.campaign_id)
            or not request.idempotency_key
        ):
            raise CreativeGuardAutomationHold(
                "creative_guard: frozen rebuild request scope does not match"
            )
        return request

    seed = "|".join(
        [
            str(scope.workspace_id),
            str(scope.auth_id),
            str(scope.advertiser_id),
            str(scope.store_id),
            str(scope.campaign_id),
            str(body.campaign_name),
        ]
    )
    raw_id = int.from_bytes(
        hashlib.sha256(seed.encode("utf-8")).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    minimum_id = 100_000_000_000_000_000
    request_id = str(
        minimum_id
        + (raw_id % (((1 << 63) - 1) - minimum_id))
    )
    payload = body.model_dump(mode="json", exclude_none=True)
    payload = {
        key: value
        for key, value in payload.items()
        if key in CreateCampaignRequest.model_fields
    }
    payload.pop("request_id", None)
    if payload.get("budget") is not None:
        budget = Decimal(str(payload["budget"]))
        if budget != budget.to_integral_value():
            raise CreativeGuardAutomationHold(
                "creative_guard: non-integral campaign budget cannot be frozen safely"
            )
        payload["budget"] = int(budget)
    payload.update(
        {
            "advertiser_id": str(scope.advertiser_id),
            "store_id": str(scope.store_id),
            "campaign_name": str(body.campaign_name),
            "request_id": request_id,
            "idempotency_key": request_id,
            "replacement_campaign_id": str(scope.campaign_id),
            "automation": {
                "source": _REBUILD_WORKFLOW_SOURCE,
                "historical_creatives": [
                    {
                        "creative_id": str(creative_id),
                        "item_group_id": (
                            str(item_group_id)
                            if item_group_id is not None
                            else None
                        ),
                    }
                    for creative_id, item_group_id in (historical_creatives or [])
                ],
            },
        }
    )
    return CreateCampaignRequest.model_validate(payload)


def _intent_request_mapping(
    intent: GmvmaxCampaignCreateIntent,
) -> dict[str, Any]:
    value = intent.request_json or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _is_creative_rebuild_intent(intent: GmvmaxCampaignCreateIntent) -> bool:
    automation = _intent_request_mapping(intent).get("automation")
    return (
        isinstance(automation, Mapping)
        and str(automation.get("source") or "") == _REBUILD_WORKFLOW_SOURCE
    )


def _load_rebuild_intent(
    db: Session,
    scope: CampaignScope,
    *,
    states: Sequence[str] | None = None,
) -> GmvmaxCampaignCreateIntent | None:
    query = (
        db.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.workspace_id == int(scope.workspace_id),
            GmvmaxCampaignCreateIntent.auth_id == int(scope.auth_id),
            GmvmaxCampaignCreateIntent.advertiser_id
            == str(scope.advertiser_id),
            GmvmaxCampaignCreateIntent.store_id == str(scope.store_id),
            GmvmaxCampaignCreateIntent.replacement_campaign_id
            == str(scope.campaign_id),
        )
        .order_by(GmvmaxCampaignCreateIntent.id.desc())
    )
    if states is not None:
        query = query.filter(
            GmvmaxCampaignCreateIntent.state.in_(
                [str(state).upper() for state in states]
            )
        )
    for intent in query.limit(10).all():
        if _is_creative_rebuild_intent(intent):
            return intent
    return None


def _request_from_rebuild_intent(
    intent: GmvmaxCampaignCreateIntent,
    scope: CampaignScope,
) -> CreateCampaignRequest:
    try:
        request = CreateCampaignRequest.model_validate(
            _intent_request_mapping(intent)
        )
    except Exception as exc:  # noqa: BLE001
        raise CreativeGuardAutomationHold(
            "creative_guard: frozen rebuild request is invalid"
        ) from exc
    if (
        str(request.store_id) != str(scope.store_id)
        or str(request.advertiser_id or scope.advertiser_id)
        != str(scope.advertiser_id)
        or str(request.replacement_campaign_id or "") != str(scope.campaign_id)
        or str(request.idempotency_key or "") != str(intent.idempotency_key)
    ):
        raise CreativeGuardAutomationHold(
            "creative_guard: frozen rebuild request scope does not match"
        )
    return request


def _frozen_historical_creatives(
    request: CreateCampaignRequest,
) -> list[tuple[str, str | None]]:
    automation = request.automation or {}
    values = (
        automation.get("historical_creatives")
        if isinstance(automation, Mapping)
        else []
    )
    result: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for value in values or []:
        if not isinstance(value, Mapping):
            continue
        creative_id = str(value.get("creative_id") or "").strip()
        item_group_value = value.get("item_group_id")
        item_group_id = (
            str(item_group_value).strip()
            if item_group_value is not None
            else None
        )
        if not creative_id:
            continue
        key = (creative_id, item_group_id or None)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _mark_rebuild_phase(
    db: Session,
    scope: CampaignScope,
    request: CreateCampaignRequest,
    *,
    phase: str,
    state: str | None = None,
    campaign_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    error_json: Mapping[str, Any] | None = None,
) -> GmvmaxCampaignCreateIntent:
    intent = get_gmvmax_create_intent(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        advertiser_id=str(scope.advertiser_id),
        store_id=str(scope.store_id),
        idempotency_key=str(request.idempotency_key or ""),
    )
    if intent is None or not _is_creative_rebuild_intent(intent):
        raise RuntimeError("creative guard rebuild intent disappeared")
    result = (
        dict(intent.result_json)
        if isinstance(intent.result_json, Mapping)
        else {}
    )
    workflow = (
        dict(result.get("rebuild_workflow"))
        if isinstance(result.get("rebuild_workflow"), Mapping)
        else {}
    )
    workflow.update(
        {
            "source": _REBUILD_WORKFLOW_SOURCE,
            "phase": str(phase),
            "updated_at": _utcnow().isoformat(),
        }
    )
    if details:
        workflow.update(dict(details))
    marked = mark_gmvmax_create_intent(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        advertiser_id=str(scope.advertiser_id),
        store_id=str(scope.store_id),
        idempotency_key=str(request.idempotency_key or ""),
        state=str(state or intent.state),
        campaign_id=campaign_id,
        result_json={"rebuild_workflow": workflow},
        error_json=error_json,
    )
    if marked is None:
        raise RuntimeError("creative guard rebuild intent could not be updated")
    return marked


def _rebuild_schedule_has_safety_margin(
    db: Session,
    scope: CampaignScope,
    request: CreateCampaignRequest,
    *,
    minimum_seconds: int = 120,
) -> bool:
    if str(request.schedule_type or "").upper() != "SCHEDULE_START_END":
        return False
    start = request.schedule_start_time
    if start is None:
        return False
    timezone_name = _advertiser_timezone_name(db, scope)
    try:
        advertiser_timezone = (
            ZoneInfo(str(timezone_name)) if timezone_name else timezone.utc
        )
    except (ZoneInfoNotFoundError, ValueError):
        advertiser_timezone = timezone.utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=advertiser_timezone)
    start_utc = start.astimezone(timezone.utc)
    return start_utc >= _utcnow() + timedelta(seconds=max(0, minimum_seconds))


def _deep_merge_mapping(
    base: Mapping[str, Any] | None,
    overlay: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base or {}))
    for key, value in dict(overlay or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge_mapping(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _upsert_replacement_strategy(
    db: Session,
    scope: CampaignScope,
    new_campaign_id: str,
    *,
    quarantine_state: str = "FINALIZING",
) -> GmvStrategyConfig:
    source = (
        db.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.id == int(scope.strategy_id),
            GmvStrategyConfig.workspace_id == int(scope.workspace_id),
            GmvStrategyConfig.auth_id == int(scope.auth_id),
            GmvStrategyConfig.campaign_id == str(scope.campaign_id),
        )
        .first()
    )
    if source is None:
        raise CreativeGuardAutomationHold(
            "creative_guard: source strategy disappeared during rebuild"
        )
    target = (
        db.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.workspace_id == int(scope.workspace_id),
            GmvStrategyConfig.auth_id == int(scope.auth_id),
            GmvStrategyConfig.campaign_id == str(new_campaign_id),
        )
        .first()
    )
    if target is None:
        target = GmvStrategyConfig(
            workspace_id=int(scope.workspace_id),
            auth_id=int(scope.auth_id),
            campaign_id=str(new_campaign_id),
            enabled=False,
        )

    for field in (
        "target_roi",
        "min_roi",
        "max_roi",
        "min_impressions",
        "min_clicks",
        "max_budget_raise_pct_per_day",
        "max_budget_cut_pct_per_day",
        "max_roas_step_per_adjust",
        "cooldown_minutes",
        "min_runtime_minutes_before_first_change",
    ):
        setattr(target, field, getattr(source, field))

    source_config = (
        dict(source.config_json)
        if isinstance(source.config_json, Mapping)
        else {}
    )
    target_config = (
        dict(target.config_json)
        if isinstance(target.config_json, Mapping)
        else {}
    )
    source_config.pop("smart_guard_state", None)
    source_config.pop("creative_guard_state", None)
    config = _deep_merge_mapping(source_config, target_config)
    smart_guard = dict(config.get("smart_guard") or {})
    smart_guard["delegate_performance_stop_to_creative_guard"] = True
    config["smart_guard"] = smart_guard
    creative_guard = dict(config.get("creative_guard") or {})
    creative_guard.update(
        {
            "ignore_creative_ids": ["0"],
            "min_statuses": ["DELIVERING", "LEARNING", "IN_QUEUE"],
            "historical_blacklist_require_repeated_campaign_evidence": True,
            "legacy_absolute_no_order_threshold_enabled": False,
            "no_order_allowed_cpa_multiplier": "1.5",
        }
    )
    reset_config = dict(creative_guard.get("product_card_reset") or {})
    reset_config.update(
        {
            "enabled": True,
            "recreate": True,
            "disable_old_strategy": True,
            "protect_good_campaign": True,
            "min_product_card_spend_share": "0.70",
            "require_video_starvation": True,
            "max_video_spend_share": "0.20",
            "campaign_roi_floor": "0.8",
            "video_roi_floor": "1.0",
            "campaign_min_orders": 1,
            "video_min_orders": 1,
        }
    )
    creative_guard["product_card_reset"] = reset_config
    config["creative_guard"] = creative_guard
    config["creation_quarantine"] = {
        "enabled": True,
        "state": str(quarantine_state),
        "source": _REBUILD_WORKFLOW_SOURCE,
        "updated_at": _utcnow().isoformat(),
    }
    target.enabled = False
    target.config_json = config
    db.add(target)
    db.flush()
    return target


def _copy_strategy_to_campaign(db: Session, scope: CampaignScope, new_campaign_id: str) -> None:
    _upsert_replacement_strategy(db, scope, new_campaign_id)


def _copy_campaign_item_groups(
    db: Session,
    scope: CampaignScope,
    new_campaign_id: str,
    *,
    item_group_ids: Sequence[str] | None = None,
) -> None:
    expected = {
        str(item).strip()
        for item in (
            item_group_ids
            if item_group_ids is not None
            else _campaign_item_group_ids(db, scope)
        )
        if str(item).strip()
    }
    rows = (
        db.query(GmvmaxProductCampaignItemGroup)
        .filter(
            GmvmaxProductCampaignItemGroup.workspace_id == int(scope.workspace_id),
            GmvmaxProductCampaignItemGroup.auth_id == int(scope.auth_id),
            GmvmaxProductCampaignItemGroup.advertiser_id
            == str(scope.advertiser_id),
            GmvmaxProductCampaignItemGroup.store_id == str(scope.store_id),
            GmvmaxProductCampaignItemGroup.campaign_id == str(new_campaign_id),
        )
        .all()
    )
    actual = {str(row.item_group_id) for row in rows}
    extras = actual - expected
    if extras:
        raise RuntimeError(
            "replacement campaign product relation differs from its frozen "
            f"create intent: unexpected={sorted(extras)}"
        )
    for item_group_id in sorted(expected - actual):
        db.add(
            GmvmaxProductCampaignItemGroup(
                workspace_id=int(scope.workspace_id),
                auth_id=int(scope.auth_id),
                advertiser_id=str(scope.advertiser_id),
                store_id=str(scope.store_id),
                campaign_id=str(new_campaign_id),
                item_group_id=str(item_group_id),
            )
        )
    db.flush()


def _historical_product_price_cents(config: Mapping[str, Any], item_group_id: str | None) -> int | None:
    prices = config.get("product_effective_prices") or {}
    if not isinstance(prices, Mapping):
        return None
    candidates = []
    if item_group_id:
        candidates.append(str(item_group_id))
    candidates.append("default")
    for key in candidates:
        value = prices.get(key)
        price = _to_decimal(value)
        if price and price > 0:
            return int((price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return None


def _historical_price_threshold_cents(
    config: Mapping[str, Any],
    *,
    item_group_id: str | None,
    multiplier_key: str,
) -> int:
    min_spend = max(0, _to_int(config.get("historical_blacklist_min_spend_cents"), 300))
    price_cents = _historical_product_price_cents(config, item_group_id)
    if not price_cents:
        return min_spend
    multiplier = _to_decimal(config.get(multiplier_key), "1.0") or Decimal("1.0")
    price_threshold = int((Decimal(price_cents) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(min_spend, price_threshold)


def _historical_removed_creatives_for_scope(db: Session, scope: CampaignScope) -> list[tuple[str, str | None]]:
    if not bool(scope.config.get("historical_blacklist_enabled", True)):
        return []

    rows = db.execute(
        text(
            """
            select
                m.creative_id,
                m.item_group_id,
                m.campaign_id as metric_campaign_id,
                m.stat_time_day,
                coalesce(m.cost_cents, 0) as metric_cost_cents,
                coalesce(m.gross_revenue_cents, 0) as metric_gmv_cents,
                coalesce(m.orders, 0) as metric_orders,
                e.id as remove_event_id,
                e.campaign_id as remove_campaign_id,
                e.reason as remove_reason,
                coalesce(e.cost_cents, 0) as remove_cost_cents,
                coalesce(e.gross_revenue_cents, 0) as remove_gmv_cents,
                coalesce(e.orders, 0) as remove_orders,
                coalesce(e.roi, 0) as remove_roi,
                e.created_at as remove_created_at,
                m.updated_at as metric_updated_at
            from gmvmax_product_creative_metrics_daily m
            join gmv_campaign_guard_events e
              on e.workspace_id=m.workspace_id
             and e.auth_id=m.auth_id
             and e.advertiser_id=m.advertiser_id
             and e.store_id=m.store_id
             and e.campaign_id=m.campaign_id
            where m.workspace_id=:workspace_id
              and m.auth_id=:auth_id
              and m.advertiser_id=:advertiser_id
              and m.store_id=:store_id
              and m.creative_id is not null
              and m.creative_id not in ('', '-1', '0')
              and e.event_type='CREATIVE_GUARD'
              and e.action='REMOVE'
              and e.result='SUCCESS'
              and e.reason <> 'creative_guard:inherit_historical_exclusions'
              and (
                    json_search(e.request_json, 'one', m.creative_id, null, '$') is not null
                 or json_search(e.response_json, 'one', m.creative_id, null, '$') is not null
              )
              and not exists (
                    select 1
                    from gmvmax_product_creative_metrics_daily good
                    where good.workspace_id=m.workspace_id
                      and good.auth_id=m.auth_id
                      and good.advertiser_id=m.advertiser_id
                      and good.store_id=m.store_id
                      and good.item_group_id=m.item_group_id
                      and good.creative_id=m.creative_id
                      and coalesce(good.orders, 0) >= :historical_reinclude_min_orders
                      and coalesce(good.roi, 0) >= :historical_reinclude_min_roi
              )
              and (
                    m.item_group_id in (
                        select item_group_id
                        from gmvmax_product_campaign_item_groups
                        where workspace_id=:workspace_id
                          and auth_id=:auth_id
                          and advertiser_id=:advertiser_id
                          and store_id=:store_id
                          and campaign_id=:campaign_id
                    )
                 or not exists (
                        select 1
                        from gmvmax_product_campaign_item_groups
                        where workspace_id=:workspace_id
                          and auth_id=:auth_id
                          and advertiser_id=:advertiser_id
                          and store_id=:store_id
                          and campaign_id=:campaign_id
                    )
              )
            order by m.updated_at desc
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "historical_reinclude_min_orders": max(
                1, _to_int(scope.config.get("historical_reinclude_min_orders"), 1)
            ),
            "historical_reinclude_min_roi": str(
                _to_decimal(scope.config.get("historical_reinclude_min_roi"), "1.2")
                or Decimal("1.2")
            ),
        },
    ).mappings().all()

    grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
    timezone_name = _advertiser_timezone_name(db, scope)
    bucket_hours = max(
        1, min(24, _to_int(scope.config.get("historical_blacklist_time_bucket_hours"), 4))
    )

    def event_time_bucket(value: Any) -> str | None:
        created_at = _as_utc(value)
        if created_at is None:
            return None
        if timezone_name:
            try:
                created_at = created_at.astimezone(ZoneInfo(str(timezone_name)))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        start_hour = (created_at.hour // bucket_hours) * bucket_hours
        return f"{created_at.date().isoformat()}:{start_hour:02d}"

    for row in rows:
        creative_id = str(row.get("creative_id") or "").strip()
        if not creative_id:
            continue
        item_group_id = str(row.get("item_group_id") or "").strip() or None
        key = (creative_id, item_group_id)
        group = grouped.setdefault(
            key,
            {
                "metric_keys": set(),
                "event_ids": set(),
                "event_campaigns": set(),
                "event_time_buckets": set(),
                "cost_cents": 0,
                "gmv_cents": 0,
                "orders": 0,
                "max_event_cost_cents": 0,
                "last_metric_at": row.get("metric_updated_at"),
            },
        )
        metric_key = (
            str(row.get("metric_campaign_id") or ""),
            str(row.get("stat_time_day") or ""),
            creative_id,
            str(item_group_id or ""),
        )
        if metric_key not in group["metric_keys"]:
            group["metric_keys"].add(metric_key)
            group["cost_cents"] += _to_int(row.get("metric_cost_cents"), 0)
            group["gmv_cents"] += _to_int(row.get("metric_gmv_cents"), 0)
            group["orders"] += _to_int(row.get("metric_orders"), 0)
        event_id = row.get("remove_event_id")
        if event_id is not None and event_id not in group["event_ids"]:
            group["event_ids"].add(event_id)
            group["event_campaigns"].add(str(row.get("remove_campaign_id") or ""))
            bucket = event_time_bucket(row.get("remove_created_at"))
            if bucket:
                group["event_time_buckets"].add(bucket)
            group["max_event_cost_cents"] = max(
                group["max_event_cost_cents"],
                _to_int(row.get("remove_cost_cents"), 0),
            )
        if row.get("metric_updated_at") and (
            not group["last_metric_at"] or row.get("metric_updated_at") > group["last_metric_at"]
        ):
            group["last_metric_at"] = row.get("metric_updated_at")

    min_remove_events = max(1, _to_int(scope.config.get("historical_blacklist_min_remove_events"), 3))
    min_distinct_campaigns = max(1, _to_int(scope.config.get("historical_blacklist_min_distinct_campaigns"), 2))
    min_distinct_time_buckets = max(
        1, _to_int(scope.config.get("historical_blacklist_min_distinct_time_buckets"), 3)
    )
    poor_roi_min_orders = max(1, _to_int(scope.config.get("historical_blacklist_poor_roi_min_orders"), 2))
    poor_roi_floor = (
        _to_decimal(scope.config.get("historical_blacklist_poor_roi_floor"), "0.8")
        or Decimal("0.8")
    )

    qualified: list[tuple[tuple[str, str | None], dict[str, Any]]] = []
    for key, group in grouped.items():
        creative_id, item_group_id = key
        if bool(scope.config.get("historical_blacklist_honor_add_events", True)):
            latest_action = _latest_successful_creative_action(
                db, scope, creative_id, campaign_only=False
            )
            if latest_action and str(latest_action.get("action") or "").upper() != "REMOVE":
                continue
        cost_cents = int(group["cost_cents"])
        gmv_cents = int(group["gmv_cents"])
        orders = int(group["orders"])
        if cost_cents <= 0:
            continue
        remove_events = len(group["event_ids"])
        distinct_campaigns = len({item for item in group["event_campaigns"] if item})
        distinct_time_buckets = len(group["event_time_buckets"])
        aggregate_roi = Decimal(gmv_cents) / Decimal(cost_cents) if cost_cents > 0 else Decimal("0")
        zero_order_spend = _historical_price_threshold_cents(
            scope.config,
            item_group_id=item_group_id,
            multiplier_key="historical_blacklist_zero_order_price_multiplier",
        )
        poor_roi_spend = _historical_price_threshold_cents(
            scope.config,
            item_group_id=item_group_id,
            multiplier_key="historical_blacklist_poor_roi_price_multiplier",
        )
        single_event_spend = _historical_price_threshold_cents(
            scope.config,
            item_group_id=item_group_id,
            multiplier_key="historical_blacklist_single_event_price_multiplier",
        )
        if bool(scope.config.get("historical_blacklist_require_repeated_campaign_evidence", True)):
            enough_repeated_evidence = (
                remove_events >= min_remove_events
                and distinct_campaigns >= min_distinct_campaigns
                and distinct_time_buckets >= min_distinct_time_buckets
            )
        else:
            enough_repeated_evidence = (
                remove_events >= min_remove_events
                or distinct_campaigns >= min_distinct_campaigns
                or distinct_time_buckets >= min_distinct_time_buckets
            )
        zero_order_bad = (
            enough_repeated_evidence
            and orders <= 0
            and cost_cents >= zero_order_spend
        )
        poor_roi_bad = (
            enough_repeated_evidence
            and
            orders >= poor_roi_min_orders
            and cost_cents >= poor_roi_spend
            and aggregate_roi <= poor_roi_floor
        )
        high_spend_bad_converter = (
            enough_repeated_evidence
            and
            orders > 0
            and cost_cents >= single_event_spend
            and aggregate_roi <= poor_roi_floor
        )
        if zero_order_bad or poor_roi_bad or high_spend_bad_converter:
            qualified.append((key, group))

    qualified.sort(key=lambda item: item[1].get("last_metric_at") or datetime.min, reverse=True)
    return [key for key, _ in qualified]


def _historical_exclusion_item_list(
    creatives: Sequence[tuple[str, str | None]],
) -> tuple[list[GMVMaxCreativeStatusUpdateItem], list[str]]:
    products_by_creative: dict[str, list[str]] = {}
    for creative_id, item_group_id in creatives:
        creative_key = str(creative_id).strip()
        if not creative_key:
            continue
        products = products_by_creative.setdefault(creative_key, [])
        product_key = str(item_group_id or "").strip()
        if product_key and product_key not in products:
            products.append(product_key)
    return (
        [
            GMVMaxCreativeStatusUpdateItem(
                item_id=creative_id,
                spu_id_list=product_ids or None,
            )
            for creative_id, product_ids in products_by_creative.items()
        ],
        list(products_by_creative),
    )


async def _exclude_historical_removed_creatives_unlocked(
    db: Session,
    scope: CampaignScope,
    *,
    new_campaign_id: str,
    creatives: Sequence[tuple[str, str | None]] | None = None,
    mutation: Any,
) -> dict[str, Any]:
    if creatives is None:
        creatives = _historical_removed_creatives_for_scope(db, scope)
    else:
        creatives = list(creatives)
    if not creatives:
        return {"excluded": 0, "reason": "no_historical_removed_creatives"}

    item_list, creative_ids = _historical_exclusion_item_list(creatives)
    if not item_list:
        return {"excluded": 0, "reason": "no_valid_historical_creatives"}
    request_payload: dict[str, Any] = {
        "advertiser_id": str(scope.advertiser_id),
        "campaign_id": str(new_campaign_id),
        "action": "REMOVE",
        "requested": len(item_list),
        "batch_size": 400,
        "creative_ids": creative_ids,
    }
    excluded_count = 0
    if len(item_list) > 10_000:
        response_payload = {
            "excluded": 0,
            "requested": len(item_list),
            "error": (
                "historical exclusion set exceeds TikTok's official "
                "10,000-post per-campaign limit"
            ),
        }
        result_status = "FAILED"
    elif bool(scope.config.get("dry_run", False)):
        response_payload = {
            "dry_run": True,
            "excluded": len(item_list),
            "requested": len(item_list),
            "batches": (len(item_list) + 399) // 400,
        }
        excluded_count = len(item_list)
        result_status = "SUCCESS"
    elif _manual_pause_override_active(db, scope):
        return {
            "excluded": 0,
            "requested": len(item_list),
            "manual_pause_override": True,
            "reason": "creative_guard: manual pause override requires HOLD",
        }
    else:
        client = build_ttb_gmvmax_client(
            db,
            auth_id=scope.auth_id,
            timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
        )
        try:
            batch_results: list[dict[str, Any]] = []
            batch_errors: list[dict[str, Any]] = []
            for batch_index, offset in enumerate(range(0, len(item_list), 400), start=1):
                batch_items = item_list[offset : offset + 400]
                body = GMVMaxCreativeStatusUpdateBody(
                    campaign_id=str(new_campaign_id),
                    item_list=batch_items,
                    action="REMOVE",
                )
                request = GMVMaxCreativeStatusUpdateRequest(
                    advertiser_id=str(scope.advertiser_id),
                    body=body,
                )
                try:
                    mutation.assert_current(db)
                    response = await client.gmv_max_creative_status_update(request)
                    mutation.assert_current(db)
                    batch_payload = (
                        response.model_dump(exclude_none=True)
                        if hasattr(response, "model_dump")
                        else {"request_id": getattr(response, "request_id", None)}
                    )
                    excluded_count += len(batch_items)
                    batch_results.append(
                        {
                            "batch": batch_index,
                            "requested": len(batch_items),
                            "response": batch_payload,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "creative guard historical exclusion batch failed for recreated campaign",
                        exc_info=True,
                        extra={
                            "old_campaign_id": scope.campaign_id,
                            "new_campaign_id": new_campaign_id,
                            "batch": batch_index,
                            "batch_size": len(batch_items),
                            "creative_count": len(item_list),
                        },
                    )
                    batch_errors.append(
                        {
                            "batch": batch_index,
                            "requested": len(batch_items),
                            "error": str(exc),
                        }
                    )
            result_status = "SUCCESS" if not batch_errors else "FAILED"
            response_payload = {
                "excluded": excluded_count,
                "requested": len(item_list),
                "successful_batches": batch_results,
                "failed_batches": batch_errors,
            }
            if batch_errors:
                response_payload["error"] = (
                    f"{len(batch_errors)} historical exclusion batch(es) failed"
                )
        finally:
            await client.aclose()
    event_statement = text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, event_type, action, reason, result,
                request_json, response_json, created_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                :strategy_id, 'CREATIVE_GUARD', 'REMOVE',
                'creative_guard:inherit_historical_exclusions', :result,
                :request_json, :response_json, :created_at
            )
            """
        ).bindparams(
            bindparam("request_json", type_=SAJSON),
            bindparam("response_json", type_=SAJSON),
        )
    db.execute(
        event_statement,
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": str(new_campaign_id),
            "strategy_id": scope.strategy_id,
            "result": result_status,
            "request_json": request_payload,
            "response_json": response_payload,
            "created_at": _utcnow().replace(tzinfo=None),
        },
    )
    mutation.commit(db)
    if result_status != "SUCCESS":
        return {
            "excluded": excluded_count,
            "requested": len(item_list),
            "creative_ids": creative_ids,
            "error": response_payload.get("error"),
        }
    return {
        "excluded": excluded_count,
        "requested": len(item_list),
        "creative_ids": creative_ids,
    }


async def _exclude_historical_removed_creatives(
    db: Session,
    scope: CampaignScope,
    *,
    new_campaign_id: str,
    creatives: Sequence[tuple[str, str | None]] | None = None,
) -> dict[str, Any]:
    with gmvmax_mutation_lease(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        owner_prefix="creative-guard-historical-exclusion",
        timeout=0.1,
    ) as mutation:
        return await _exclude_historical_removed_creatives_unlocked(
            db,
            scope,
            new_campaign_id=new_campaign_id,
            creatives=creatives,
            mutation=mutation,
        )


async def _prepare_recreated_campaign_body(
    db: Session,
    client: Any,
    scope: CampaignScope,
    *,
    decision_context: Mapping[str, Any],
    request_payload: dict[str, Any],
) -> tuple[GMVMaxCampaignCreateBody, list[tuple[str, str | None]]]:
    """Build and validate the replacement before pausing the live campaign."""

    body = _clone_campaign_body(db, scope)
    body, official_recommendation = await _apply_official_bid_recommendation(
        client,
        scope,
        body,
    )
    request_payload["official_bid_recommendation"] = official_recommendation
    body = apply_approved_plan_defaults_to_body(
        db,
        body,
        workspace_id=scope.workspace_id,
        auth_id=scope.auth_id,
        advertiser_id=scope.advertiser_id,
        store_id=scope.store_id,
    )
    rebuild_roas_bid = _to_decimal(decision_context.get("rebuild_roas_bid"))
    if rebuild_roas_bid is not None and rebuild_roas_bid > 0:
        baseline_roas = _to_decimal(body.roas_bid)
        if baseline_roas is not None and baseline_roas > 0:
            rebuild_roas_bid = max(
                baseline_roas - Decimal("0.2"),
                min(baseline_roas + Decimal("0.2"), rebuild_roas_bid),
            )
        rebuild_roas_bid = rebuild_roas_bid.quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
        body_payload = body.model_dump()
        body_payload["roas_bid"] = float(rebuild_roas_bid)
        body = GMVMaxCampaignCreateBody.model_validate(body_payload)
        request_payload["rebuild_roas_bid"] = str(rebuild_roas_bid)
    identities = await _load_rebuild_identity_list(
        client,
        scope,
        store_authorized_bc_id=str(
            getattr(body, "store_authorized_bc_id", "") or ""
        ),
    )
    if identities:
        body_payload = body.model_dump()
        body_payload["identity_list"] = identities
        body = GMVMaxCampaignCreateBody.model_validate(body_payload)
        request_payload["rebuild_identity_count"] = len(identities)
    historical_creatives = _historical_removed_creatives_for_scope(db, scope)
    historical_items, _ = _historical_exclusion_item_list(historical_creatives)
    if len(historical_items) > 10_000:
        raise CreativeGuardAutomationHold(
            "creative_guard: historical exclusion set exceeds TikTok's official "
            f"10,000-post campaign limit ({len(historical_items)}); refusing rebuild"
        )
    request_payload["historical_exclusion_preflight_count"] = len(historical_items)
    return body, historical_creatives


async def _quarantine_incomplete_recreated_campaign(
    db: Session,
    client: Any,
    scope: CampaignScope,
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Pause a replacement that could not inherit its full safety exclusions."""

    mutation = active_gmvmax_mutation_lease(db)
    if mutation is None:
        raise GmvMaxMutationFenceLost(
            "replacement quarantine has no active mutation lease"
        )
    request = CampaignStatusUpdateRequest(
        advertiser_id=str(scope.advertiser_id),
        campaign_ids=[str(campaign_id)],
        operation_status="DISABLE",
    )
    try:
        mutation.assert_current(db)
        response = await client.campaign_status_update(request)
        mutation.assert_current(db)
        response_payload = (
            response.model_dump(exclude_none=True)
            if hasattr(response, "model_dump")
            else {"request_id": getattr(response, "request_id", None)}
        )
        local_synced = _mark_campaign_disabled_best_effort(
            db,
            scope,
            campaign_id=str(campaign_id),
        )
        mutation.commit(db)
        return {
            "paused": True,
            "local_pause_synced": local_synced,
            "request": request.model_dump(exclude_none=True),
            "response": response_payload,
        }
    except GmvMaxMutationFenceLost:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.critical(
            "creative guard could not quarantine an incompletely protected campaign",
            exc_info=True,
            extra={
                "old_campaign_id": scope.campaign_id,
                "new_campaign_id": campaign_id,
            },
        )
        return {
            "paused": False,
            "request": request.model_dump(exclude_none=True),
            "error": str(exc),
        }


def _set_replacement_strategy_quarantine(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str,
    state: str,
) -> None:
    _upsert_replacement_strategy(
        db,
        scope,
        str(campaign_id),
        quarantine_state=str(state),
    )


def _complete_replacement_strategy(
    db: Session,
    scope: CampaignScope,
    *,
    campaign_id: str,
) -> None:
    strategy = (
        db.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.workspace_id == int(scope.workspace_id),
            GmvStrategyConfig.auth_id == int(scope.auth_id),
            GmvStrategyConfig.campaign_id == str(campaign_id),
        )
        .first()
    )
    if strategy is None:
        raise RuntimeError("replacement strategy disappeared before enable")
    config = (
        dict(strategy.config_json)
        if isinstance(strategy.config_json, Mapping)
        else {}
    )
    quarantine = config.get("creation_quarantine")
    if not (
        isinstance(quarantine, Mapping)
        and bool(quarantine.get("enabled"))
        and str(quarantine.get("source") or "") == _REBUILD_WORKFLOW_SOURCE
    ):
        raise RuntimeError(
            "replacement strategy lost its creation quarantine before enable"
        )
    config.pop("creation_quarantine", None)
    strategy.config_json = config
    strategy.enabled = True
    db.add(strategy)


def _intent_rebuild_phase(intent: GmvmaxCampaignCreateIntent) -> str:
    result = intent.result_json or {}
    if not isinstance(result, Mapping):
        return ""
    workflow = result.get("rebuild_workflow")
    if not isinstance(workflow, Mapping):
        return ""
    return str(workflow.get("phase") or "").upper()


async def _compensate_old_campaign_after_unsent_rebuild(
    db: Session,
    client: Any,
    scope: CampaignScope,
    request: CreateCampaignRequest,
    *,
    mutation: Any,
    cause: BaseException,
) -> bool:
    intent = get_gmvmax_create_intent(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        advertiser_id=str(scope.advertiser_id),
        store_id=str(scope.store_id),
        idempotency_key=str(request.idempotency_key or ""),
    )
    if intent is None:
        return False
    state = str(intent.state or "").upper()
    phase = _intent_rebuild_phase(intent)
    if state not in {"PREPARED", "FAILED_TERMINAL", "COMPENSATION_PENDING"}:
        return False
    if phase not in {
        "OLD_PAUSE_PENDING",
        "OLD_PAUSED",
        "COMPENSATION_PENDING",
    }:
        return False
    _assert_creative_guard_mutation_allowed(
        db,
        scope,
        require_enabled=False,
        allow_creative_rebuild_intent=True,
    )
    _mark_rebuild_phase(
        db,
        scope,
        request,
        phase="COMPENSATION_PENDING",
        state="COMPENSATION_PENDING",
        error_json={
            "reason": "rebuild_not_created",
            "type": type(cause).__name__,
            "message": str(cause)[:1000],
        },
    )
    mutation.commit(db)
    mutation.assert_current(db)
    await client.campaign_status_update(
        CampaignStatusUpdateRequest(
            advertiser_id=str(scope.advertiser_id),
            campaign_ids=[str(scope.campaign_id)],
            operation_status="ENABLE",
        )
    )
    mutation.assert_current(db)
    if not _mark_campaign_enabled_best_effort(
        db,
        scope,
        campaign_id=str(scope.campaign_id),
    ):
        raise RuntimeError(
            "old campaign was re-enabled remotely but its local state was not found"
        )
    _mark_rebuild_phase(
        db,
        scope,
        request,
        phase="COMPENSATED",
        state="FAILED_TERMINAL",
        error_json={
            "reason": "rebuild_not_created_old_campaign_restored",
            "type": type(cause).__name__,
            "message": str(cause)[:1000],
        },
    )
    mutation.commit(db)
    return True


async def _retry_rebuild_quarantine(
    db: Session,
    scope: CampaignScope,
    intent: GmvmaxCampaignCreateIntent,
) -> bool:
    request = _request_from_rebuild_intent(intent, scope)
    campaign_id = str(intent.campaign_id or "").strip()
    if not campaign_id:
        raise RuntimeError(
            "quarantine-pending rebuild intent has no replacement campaign id"
        )
    with gmvmax_mutation_lease(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        owner_prefix="creative-guard-rebuild-quarantine",
        timeout=0.1,
    ) as mutation:
        current = _load_rebuild_intent(
            db,
            scope,
            states={"REMOTE_CREATED", "FINALIZING"},
        )
        if current is None or _intent_rebuild_phase(current) != "QUARANTINE_PENDING":
            return False
        client = build_ttb_gmvmax_client(
            db,
            auth_id=int(scope.auth_id),
            timeout=float(
                getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)
            ),
        )
        try:
            quarantine = await _quarantine_incomplete_recreated_campaign(
                db,
                client,
                scope,
                campaign_id=campaign_id,
            )
            pause_confirmed = bool(quarantine.get("paused"))
            marker_state = (
                "QUARANTINED" if pause_confirmed else "QUARANTINE_PENDING"
            )
            _set_replacement_strategy_quarantine(
                db,
                scope,
                campaign_id=campaign_id,
                state=marker_state,
            )
            _mark_rebuild_phase(
                db,
                scope,
                request,
                phase=marker_state,
                state=("QUARANTINED" if pause_confirmed else "REMOTE_CREATED"),
                campaign_id=campaign_id,
                details={"remote_pause_confirmed": pause_confirmed},
                error_json=(
                    None
                    if pause_confirmed
                    else {
                        "reason": "replacement_quarantine_retry_pending",
                        "message": str(quarantine.get("error") or "")[:1000],
                    }
                ),
            )
            mutation.commit(db)
            return pause_confirmed
        finally:
            await client.aclose()


async def _create_rebuild_after_occupancy_converges(
    db: Session,
    scope: CampaignScope,
    *,
    request: CreateCampaignRequest,
    store_authorized_bc_id: str | None,
    client: Any,
    mutation: Any,
) -> Any:
    delays = (1.0, 2.0, 4.0, 8.0)
    for attempt in range(len(delays) + 1):
        try:
            return await create_gmvmax_campaign_durable(
                db,
                workspace_id=int(scope.workspace_id),
                provider="tiktok-business",
                auth_id=int(scope.auth_id),
                advertiser_id=str(scope.advertiser_id),
                payload=request,
                store_authorized_bc_id=store_authorized_bc_id,
                client=client,
                execution_guard=mutation.assert_current,
            )
        except TTBBusinessError as exc:
            if (
                str(getattr(exc, "code", "") or "")
                != "GMVMAX_PRODUCT_OCCUPIED"
                or attempt >= len(delays)
            ):
                raise
            # TikTok may briefly report the product as occupied after the old
            # campaign's DISABLE is acknowledged. Re-run the official precheck
            # with the same frozen intent; never relax or bypass occupancy.
            mutation.assert_current(db)
            await asyncio.sleep(delays[attempt])
            mutation.assert_current(db)
    raise RuntimeError("unreachable rebuild occupancy retry state")


def _rebuild_recovery_candidates(
    db: Session,
) -> list[tuple[CampaignScope, GmvmaxCampaignCreateIntent, bool]]:
    """Load recovery work independently from the normal enabled-guard scopes.

    Existing rebuild intents must remain recoverable when Creative Guard is
    subsequently disabled. Enabled source strategies are prioritized over
    revoked/orphaned work so an old first page of unusable intents cannot
    starve a valid workflow.
    """

    def scope_for_intent(
        intent: GmvmaxCampaignCreateIntent,
    ) -> tuple[CampaignScope, bool] | None:
        strategy_rows = (
            db.query(GmvStrategyConfig)
            .filter(
                GmvStrategyConfig.workspace_id == int(intent.workspace_id),
                GmvStrategyConfig.auth_id == int(intent.auth_id),
                GmvStrategyConfig.campaign_id
                == str(intent.replacement_campaign_id or ""),
            )
            .order_by(GmvStrategyConfig.id.desc())
            .limit(2)
            .all()
        )
        strategy = strategy_rows[0] if len(strategy_rows) == 1 else None
        catalog_rows = (
            db.query(GmvmaxProductCampaignCatalog)
            .filter(
                GmvmaxProductCampaignCatalog.workspace_id
                == int(intent.workspace_id),
                GmvmaxProductCampaignCatalog.auth_id == int(intent.auth_id),
                GmvmaxProductCampaignCatalog.advertiser_id
                == str(intent.advertiser_id),
                GmvmaxProductCampaignCatalog.store_id == str(intent.store_id),
                GmvmaxProductCampaignCatalog.campaign_id
                == str(intent.replacement_campaign_id or ""),
            )
            .limit(2)
            .all()
        )
        catalog = catalog_rows[0] if len(catalog_rows) == 1 else None
        strategy_config = _strategy_config(strategy) if strategy is not None else {}
        runtime = dict(strategy_config.get("creative_guard_state") or {})
        scope = CampaignScope(
            strategy_id=int(strategy.id) if strategy is not None else 0,
            workspace_id=int(intent.workspace_id),
            auth_id=int(intent.auth_id),
            advertiser_id=str(intent.advertiser_id),
            store_id=str(intent.store_id),
            campaign_id=str(intent.replacement_campaign_id),
            campaign_name=getattr(catalog, "campaign_name", None),
            operation_status=getattr(catalog, "operation_status", None),
            secondary_status=getattr(catalog, "secondary_status", None),
            budget_cents=_to_int(getattr(catalog, "budget_cents", None), 0),
            roas_bid=_to_decimal(getattr(catalog, "roas_bid", None)),
            config=(
                _creative_guard_config(strategy)
                if strategy is not None
                else default_creative_guard_config()
            ),
            monitor_state=runtime,
            smart_guard_state=dict(
                strategy_config.get("smart_guard_state") or {}
            ),
        )
        return scope, bool(strategy is not None and strategy.enabled)

    authorized: list[
        tuple[CampaignScope, GmvmaxCampaignCreateIntent, bool]
    ] = []
    revoked: list[
        tuple[CampaignScope, GmvmaxCampaignCreateIntent, bool]
    ] = []
    seen: set[tuple[int, int, str, str, str]] = set()
    last_updated_at: datetime | None = None
    last_id = 0
    while len(authorized) < 100:
        query = db.query(GmvmaxCampaignCreateIntent).filter(
            GmvmaxCampaignCreateIntent.state.in_(
                sorted(_REBUILD_RESUMABLE_STATES | _REBUILD_RECOVERY_ONLY_STATES)
            ),
            GmvmaxCampaignCreateIntent.replacement_campaign_id.is_not(None),
            GmvmaxCampaignCreateIntent.request_json["automation"]["source"]
            .as_string()
            == _REBUILD_WORKFLOW_SOURCE,
        )
        if last_updated_at is not None:
            query = query.filter(
                or_(
                    GmvmaxCampaignCreateIntent.updated_at > last_updated_at,
                    and_(
                        GmvmaxCampaignCreateIntent.updated_at == last_updated_at,
                        GmvmaxCampaignCreateIntent.id > last_id,
                    ),
                )
            )
        page = (
            query.order_by(
                GmvmaxCampaignCreateIntent.updated_at.asc(),
                GmvmaxCampaignCreateIntent.id.asc(),
            )
            .limit(100)
            .all()
        )
        if not page:
            break
        for intent in page:
            if not _is_creative_rebuild_intent(intent):
                continue
            key = (
                int(intent.workspace_id),
                int(intent.auth_id),
                str(intent.advertiser_id),
                str(intent.store_id),
                str(intent.replacement_campaign_id or ""),
            )
            if key in seen:
                continue
            scoped = scope_for_intent(intent)
            if scoped is None:
                continue
            scope, strategy_enabled = scoped
            seen.add(key)
            candidate = (scope, intent, strategy_enabled)
            if strategy_enabled:
                authorized.append(candidate)
            elif str(intent.state or "").upper() in {
                "PREPARED",
                "COMPENSATION_PENDING",
            }:
                if len(revoked) < 100:
                    revoked.append(candidate)
        last_updated_at = page[-1].updated_at
        last_id = int(page[-1].id)
        if len(page) < 100:
            break
    remaining = max(0, 100 - len(authorized))
    return authorized[:100] + revoked[:remaining]


def _rebuild_source_strategy_enabled(
    db: Session,
    scope: CampaignScope,
) -> bool:
    return bool(
        db.query(GmvStrategyConfig.enabled)
        .filter(
            GmvStrategyConfig.id == int(scope.strategy_id),
            GmvStrategyConfig.workspace_id == int(scope.workspace_id),
            GmvStrategyConfig.auth_id == int(scope.auth_id),
            GmvStrategyConfig.campaign_id == str(scope.campaign_id),
        )
        .scalar()
    )


def _terminalize_rebuild_after_permission_revoked(
    db: Session,
    scope: CampaignScope,
    intent: GmvmaxCampaignCreateIntent,
) -> bool:
    """Fail closed without re-enabling an ad after strategy permission is revoked."""

    with gmvmax_mutation_lease(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        owner_prefix="creative-guard-rebuild-permission-revoked",
        timeout=0.1,
    ) as mutation:
        if _rebuild_source_strategy_enabled(db, scope):
            return False
        current = _load_rebuild_intent(
            db,
            scope,
            states={"PREPARED", "COMPENSATION_PENDING"},
        )
        if current is None or int(current.id) != int(intent.id):
            return False
        request = _request_from_rebuild_intent(current, scope)
        _mark_rebuild_phase(
            db,
            scope,
            request,
            phase="PERMISSION_REVOKED",
            state="FAILED_TERMINAL",
            error_json={
                "reason": (
                    "Source strategy permission was revoked before the "
                    "replacement workflow could finish. No campaign was enabled."
                )
            },
        )
        mutation.commit(db)
        return True


def _rotate_rebuild_recovery_candidate(
    db: Session,
    scope: CampaignScope,
    intent: GmvmaxCampaignCreateIntent,
) -> bool:
    """Move local-only recovery work behind its peers without changing state.

    Remote create states deliberately are not touched: their ``updated_at`` is
    the five-minute generic fail-closed takeover heartbeat.
    """

    updated = (
        db.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.id == int(intent.id),
            GmvmaxCampaignCreateIntent.workspace_id == int(scope.workspace_id),
            GmvmaxCampaignCreateIntent.auth_id == int(scope.auth_id),
            GmvmaxCampaignCreateIntent.advertiser_id
            == str(scope.advertiser_id),
            GmvmaxCampaignCreateIntent.store_id == str(scope.store_id),
            GmvmaxCampaignCreateIntent.replacement_campaign_id
            == str(scope.campaign_id),
            GmvmaxCampaignCreateIntent.state.in_(
                ["PREPARED", "COMPENSATION_PENDING"]
            ),
        )
        .update(
            {"updated_at": _utcnow().replace(tzinfo=None)},
            synchronize_session=False,
        )
    )
    db.commit()
    return bool(updated)


async def _reset_campaign_for_product_card_unlocked(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
    *,
    mutation: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reset_config = dict(scope.config.get("product_card_reset") or {})
    is_no_spend_reset = str(decision.get("reason") or "") in {
        "creative_guard:no_spend_timeout",
        "creative_guard:low_spend_timeout",
    }
    is_rebuild_recovery = (
        str(decision.get("reason") or "")
        == "creative_guard:rebuild_recovery"
    )
    if is_rebuild_recovery and bool(scope.config.get("dry_run", False)):
        # A durable intent can only have been created by a real mutation. A
        # later dry-run toggle must not turn required official exclusions into
        # simulated success before the replacement is enabled.
        scope = replace(
            scope,
            config={**scope.config, "dry_run": False},
        )
    if is_no_spend_reset:
        reset_config.update(dict(scope.config.get("no_spend_reset") or {}))
        reset_config.setdefault("recreate", True)
        reset_config.setdefault("disable_old_strategy", True)
    decision_context = (
        dict(decision.get("context") or {})
        if isinstance(decision.get("context"), Mapping)
        else {}
    )
    if bool(decision_context.get("defer_recreate")):
        reset_config["recreate"] = False
        reset_config["disable_old_strategy"] = False
    if (
        not is_no_spend_reset
        and not is_rebuild_recovery
        and not bool(reset_config.get("enabled", True))
    ):
        raise RuntimeError("product_card_reset disabled")

    request_payload: dict[str, Any] = {
        "old_campaign_id": scope.campaign_id,
        "trigger_creative_id": metric.creative_id,
        "decision": dict(decision),
    }
    if bool(scope.config.get("dry_run", False)) and not is_rebuild_recovery:
        return request_payload, {"dry_run": True}

    _assert_creative_guard_mutation_allowed(
        db,
        scope,
        require_enabled=True,
        allow_creative_rebuild_intent=True,
    )
    client = build_ttb_gmvmax_client(
        db,
        auth_id=scope.auth_id,
        timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
    )
    rebuild_request: CreateCampaignRequest | None = None
    new_campaign_id: str | None = None
    quarantine_attempted = False
    old_pause_confirmed = False
    try:
        recreate_enabled = bool(reset_config.get("recreate", True))
        historical_creatives: list[tuple[str, str | None]] = []
        prepared_body: GMVMaxCampaignCreateBody | None = None
        existing_intent = _load_rebuild_intent(
            db,
            scope,
            states=(
                _REBUILD_RESUMABLE_STATES
                | _REBUILD_RECOVERY_ONLY_STATES
                | {"QUARANTINED"}
            ),
        )
        # A frozen in-flight intent owns the workflow until it reaches a safe
        # terminal state. A later config toggle may prevent future rebuilds,
        # but cannot abandon an already-created replacement.
        if existing_intent is not None:
            recreate_enabled = True
        if existing_intent is not None:
            existing_state = str(existing_intent.state or "").upper()
            existing_phase = _intent_rebuild_phase(existing_intent)
            if existing_state == "QUARANTINED":
                raise CreativeGuardAutomationHold(
                    "creative_guard: replacement is quarantined for operator review"
                )
            if existing_phase == "QUARANTINE_PENDING":
                raise CreativeGuardAutomationHold(
                    "creative_guard: replacement quarantine is still pending"
                )
            if existing_state in _REBUILD_RECOVERY_ONLY_STATES:
                raise CreativeGuardAutomationHold(
                    f"creative_guard: rebuild requires {existing_state.lower()} recovery"
                )
            rebuild_request = _request_from_rebuild_intent(
                existing_intent,
                scope,
            )
            historical_creatives = _frozen_historical_creatives(rebuild_request)
            prepared_body = rebuild_request.to_client_body(
                store_authorized_bc_id=rebuild_request.store_authorized_bc_id,
                advertiser_timezone=_advertiser_timezone_name(db, scope),
                official_request_id=str(existing_intent.official_request_id),
            )
        elif recreate_enabled:
            if _manual_pause_override_active(db, scope):
                raise CreativeGuardAutomationHold(
                    "creative_guard: manual pause override activated during reset"
                )
            prepared_body, historical_creatives = (
                await _prepare_recreated_campaign_body(
                    db,
                    client,
                    scope,
                    decision_context=decision_context,
                    request_payload=request_payload,
                )
            )
            proposed_request = _durable_rebuild_create_request(
                db,
                scope,
                prepared_body,
                historical_creatives=historical_creatives,
            )
            prepared = prepare_gmvmax_create_intent(
                db,
                workspace_id=int(scope.workspace_id),
                provider="tiktok-business",
                auth_id=int(scope.auth_id),
                advertiser_id=str(scope.advertiser_id),
                payload=proposed_request,
                store_authorized_bc_id=str(
                    getattr(prepared_body, "store_authorized_bc_id", "") or ""
                )
                or None,
                execution_guard=mutation.assert_current,
            )
            if str(prepared.intent.state or "").upper() != "PREPARED":
                raise CreativeGuardAutomationHold(
                    "creative_guard: rebuild idempotency key is already bound "
                    f"to terminal state {prepared.intent.state}"
                )
            rebuild_request = prepared.frozen_payload
            prepared_body = prepared.body
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="PREPARED",
                state="PREPARED",
                details={
                    "old_campaign_id": str(scope.campaign_id),
                    "historical_exclusion_count": len(historical_creatives),
                },
            )
            mutation.commit(db)

        if recreate_enabled:
            if rebuild_request is None or prepared_body is None:
                raise RuntimeError("recreated campaign request was not prepared")
            current_intent = get_gmvmax_create_intent(
                db,
                workspace_id=int(scope.workspace_id),
                auth_id=int(scope.auth_id),
                advertiser_id=str(scope.advertiser_id),
                store_id=str(scope.store_id),
                idempotency_key=str(rebuild_request.idempotency_key or ""),
            )
            if current_intent is None:
                raise RuntimeError("rebuild create intent disappeared before old pause")
            if (
                str(current_intent.state or "").upper() == "PREPARED"
                and not _rebuild_schedule_has_safety_margin(
                    db,
                    scope,
                    rebuild_request,
                )
            ):
                safety_error = CreativeGuardAutomationHold(
                    "creative_guard: rebuild safety window expired before submission"
                )
                prior_phase = _intent_rebuild_phase(current_intent)
                if prior_phase in {
                    "OLD_PAUSE_PENDING",
                    "OLD_PAUSED",
                    "COMPENSATION_PENDING",
                }:
                    _mark_rebuild_phase(
                        db,
                        scope,
                        rebuild_request,
                        phase="COMPENSATION_PENDING",
                        state="COMPENSATION_PENDING",
                        error_json={
                            "reason": (
                                "The frozen future schedule no longer leaves "
                                "enough time to quarantine a newly created "
                                "replacement."
                            )
                        },
                    )
                    mutation.commit(db)
                    await _compensate_old_campaign_after_unsent_rebuild(
                        db,
                        client,
                        scope,
                        rebuild_request,
                        mutation=mutation,
                        cause=safety_error,
                    )
                else:
                    # PREPARED alone proves no Creative Guard status mutation
                    # was checkpointed. Enabling the source here could override
                    # a user's direct TikTok pause, so terminate without any
                    # official write.
                    _mark_rebuild_phase(
                        db,
                        scope,
                        rebuild_request,
                        phase="SAFETY_WINDOW_EXPIRED_UNSENT",
                        state="FAILED_TERMINAL",
                        error_json={
                            "reason": (
                                "The rebuild safety window expired before the "
                                "old campaign pause checkpoint."
                            )
                        },
                    )
                    mutation.commit(db)
                raise safety_error
            request_payload["create_idempotency_key"] = str(
                rebuild_request.idempotency_key
            )
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="OLD_PAUSE_PENDING",
                details={"old_campaign_id": str(scope.campaign_id)},
            )
            # This checkpoint is deliberately before the first official write.
            mutation.commit(db)

        _assert_creative_guard_mutation_allowed(
            db,
            scope,
            require_enabled=True,
            allow_creative_rebuild_intent=True,
        )
        response_payload: dict[str, Any] = {"paused_old_campaign": False}
        pause_request = CampaignStatusUpdateRequest(
            advertiser_id=str(scope.advertiser_id),
            campaign_ids=[str(scope.campaign_id)],
            operation_status="DISABLE",
        )
        mutation.assert_current(db)
        await client.campaign_status_update(pause_request)
        mutation.assert_current(db)
        old_pause_confirmed = True
        request_payload["pause_request"] = pause_request.model_dump(exclude_none=True)
        response_payload["paused_old_campaign"] = True
        response_payload["local_pause_synced"] = _mark_campaign_disabled_best_effort(
            db,
            scope,
        )
        if not response_payload["local_pause_synced"]:
            raise RuntimeError(
                "old campaign was paused remotely but its scoped local row was not found"
            )
        if rebuild_request is not None:
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="OLD_PAUSED",
                details={"old_campaign_id": str(scope.campaign_id)},
            )
        mutation.commit(db)

        if recreate_enabled:
            assert rebuild_request is not None
            assert prepared_body is not None
            new_row = await _create_rebuild_after_occupancy_converges(
                db,
                scope,
                request=rebuild_request,
                store_authorized_bc_id=str(
                    getattr(prepared_body, "store_authorized_bc_id", "") or ""
                )
                or None,
                client=client,
                mutation=mutation,
            )
            mutation.assert_current(db)
            new_campaign_id = str(getattr(new_row, "campaign_id", "") or "")
            if not new_campaign_id:
                raise RuntimeError(
                    "TikTok campaign create response did not include campaign_id"
                )
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="NEW_PAUSE_PENDING",
                state="REMOTE_CREATED",
                campaign_id=new_campaign_id,
                details={"new_campaign_id": new_campaign_id},
            )
            mutation.commit(db)

            pause_new_request = CampaignStatusUpdateRequest(
                advertiser_id=str(scope.advertiser_id),
                campaign_ids=[new_campaign_id],
                operation_status="DISABLE",
            )
            mutation.assert_current(db)
            await client.campaign_status_update(pause_new_request)
            mutation.assert_current(db)
            response_payload["new_campaign_paused_for_finalization"] = True
            response_payload["new_campaign_local_pause_synced"] = (
                _mark_campaign_disabled_best_effort(
                    db,
                    scope,
                    campaign_id=new_campaign_id,
                )
            )
            if not response_payload["new_campaign_local_pause_synced"]:
                raise RuntimeError(
                    "replacement campaign was paused remotely but its scoped "
                    "local row was not found"
                )

            frozen_item_group_ids = [
                str(value)
                for value in (rebuild_request.item_group_ids or [])
                if str(value).strip()
            ]
            _upsert_replacement_strategy(
                db,
                scope,
                new_campaign_id,
                quarantine_state="FINALIZING",
            )
            _copy_campaign_item_groups(
                db,
                scope,
                new_campaign_id,
                item_group_ids=frozen_item_group_ids,
            )
            apply_approved_plan_defaults_to_strategy(
                db,
                workspace_id=int(scope.workspace_id),
                auth_id=int(scope.auth_id),
                advertiser_id=str(scope.advertiser_id),
                store_id=str(scope.store_id),
                campaign_id=new_campaign_id,
                item_group_ids=frozen_item_group_ids,
            )
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="FINALIZING",
                state="FINALIZING",
                campaign_id=new_campaign_id,
            )
            mutation.commit(db)

            inherited_exclusions = await _exclude_historical_removed_creatives(
                db,
                scope,
                new_campaign_id=new_campaign_id,
                creatives=historical_creatives,
            )
            exclusion_requested = _to_int(
                inherited_exclusions.get("requested"),
                0,
            )
            exclusion_applied = _to_int(
                inherited_exclusions.get("excluded"),
                0,
            )
            exclusion_incomplete = bool(inherited_exclusions.get("error")) or (
                exclusion_requested > exclusion_applied
            )
            if exclusion_incomplete:
                quarantine_attempted = True
                quarantine = await _quarantine_incomplete_recreated_campaign(
                    db,
                    client,
                    scope,
                    campaign_id=new_campaign_id,
                )
                pause_confirmed = bool(quarantine.get("paused"))
                quarantine_state = (
                    "QUARANTINED"
                    if pause_confirmed
                    else "QUARANTINE_PENDING"
                )
                quarantine_intent_state = (
                    "QUARANTINED" if pause_confirmed else "REMOTE_CREATED"
                )
                _set_replacement_strategy_quarantine(
                    db,
                    scope,
                    campaign_id=new_campaign_id,
                    state=quarantine_state,
                )
                _mark_rebuild_phase(
                    db,
                    scope,
                    rebuild_request,
                    phase=quarantine_state,
                    state=quarantine_intent_state,
                    campaign_id=new_campaign_id,
                    details={
                        "excluded": exclusion_applied,
                        "requested": exclusion_requested,
                        "remote_pause_confirmed": pause_confirmed,
                    },
                    error_json={
                        "reason": "historical_exclusion_incomplete",
                        "excluded": exclusion_applied,
                        "requested": exclusion_requested,
                    },
                )
                response_payload.update(
                    {
                        "recreated": False,
                        "new_campaign_id": new_campaign_id,
                        "inherited_exclusions": inherited_exclusions,
                        "new_campaign_quarantine": quarantine,
                    }
                )
                mutation.commit(db)
                raise RuntimeError(
                    "recreated campaign failed to inherit the complete historical "
                    f"exclusion set ({exclusion_applied}/{exclusion_requested}); "
                    f"quarantine_paused={pause_confirmed}"
                )

            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="EXCLUSIONS_COMPLETE",
                state="FINALIZING",
                campaign_id=new_campaign_id,
                details={
                    "excluded": exclusion_applied,
                    "requested": exclusion_requested,
                },
            )
            mutation.commit(db)
            _assert_creative_guard_mutation_allowed(
                db,
                scope,
                require_enabled=False,
                allow_creative_rebuild_intent=True,
            )
            _assert_creative_guard_mutation_allowed(
                db,
                scope,
                campaign_id=new_campaign_id,
                require_enabled=False,
                allow_creative_rebuild_intent=True,
            )
            if _manual_pause_override_active(
                db,
                scope,
                campaign_id=new_campaign_id,
            ):
                raise CreativeGuardAutomationHold(
                    "creative_guard: replacement was manually paused during finalization"
                )
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="ENABLE_PENDING",
                state="FINALIZING",
                campaign_id=new_campaign_id,
            )
            mutation.commit(db)
            enable_new_request = CampaignStatusUpdateRequest(
                advertiser_id=str(scope.advertiser_id),
                campaign_ids=[new_campaign_id],
                operation_status="ENABLE",
            )
            mutation.assert_current(db)
            await client.campaign_status_update(enable_new_request)
            mutation.assert_current(db)
            response_payload["new_campaign_enabled"] = True
            response_payload["new_campaign_local_enable_synced"] = (
                _mark_campaign_enabled_best_effort(
                    db,
                    scope,
                    campaign_id=new_campaign_id,
                )
            )
            if not response_payload["new_campaign_local_enable_synced"]:
                raise RuntimeError(
                    "replacement campaign was enabled remotely but its scoped "
                    "local row was not found"
                )
            _complete_replacement_strategy(
                db,
                scope,
                campaign_id=new_campaign_id,
            )
            if bool(reset_config.get("disable_old_strategy", True)):
                source_strategy = (
                    db.query(GmvStrategyConfig)
                    .filter(
                        GmvStrategyConfig.id == int(scope.strategy_id),
                        GmvStrategyConfig.workspace_id == int(scope.workspace_id),
                        GmvStrategyConfig.auth_id == int(scope.auth_id),
                        GmvStrategyConfig.campaign_id == str(scope.campaign_id),
                    )
                    .first()
                )
                if source_strategy is None:
                    raise RuntimeError(
                        "old strategy disappeared before rebuild finalization"
                    )
                source_strategy.enabled = False
                db.add(source_strategy)
                response_payload["old_strategy_disabled"] = True
            _mark_rebuild_phase(
                db,
                scope,
                rebuild_request,
                phase="SUCCEEDED",
                state="SUCCEEDED",
                campaign_id=new_campaign_id,
            )
            mutation.commit(db)
            response_payload.update(
                {
                    "recreated": True,
                    "new_campaign_id": new_campaign_id,
                    "new_campaign_name": getattr(new_row, "campaign_name", None),
                    "inherited_exclusions": inherited_exclusions,
                }
            )
        elif bool(reset_config.get("disable_old_strategy", True)):
            source_strategy = (
                db.query(GmvStrategyConfig)
                .filter(
                    GmvStrategyConfig.id == int(scope.strategy_id),
                    GmvStrategyConfig.workspace_id == int(scope.workspace_id),
                    GmvStrategyConfig.auth_id == int(scope.auth_id),
                    GmvStrategyConfig.campaign_id == str(scope.campaign_id),
                )
                .first()
            )
            if source_strategy is None:
                raise RuntimeError("old strategy disappeared during reset")
            source_strategy.enabled = False
            db.add(source_strategy)
            response_payload["old_strategy_disabled"] = True
            mutation.commit(db)

        if bool(decision_context.get("defer_recreate")):
            response_payload["recreate_deferred"] = True
            response_payload["old_strategy_disabled"] = False
        return request_payload, response_payload
    except GmvMaxMutationFenceLost:
        db.rollback()
        raise
    except Exception as exc:
        if (
            new_campaign_id
            and rebuild_request is not None
            and not quarantine_attempted
        ):
            try:
                db.rollback()
                quarantine_attempted = True
                quarantine = await _quarantine_incomplete_recreated_campaign(
                    db,
                    client,
                    scope,
                    campaign_id=new_campaign_id,
                )
                pause_confirmed = bool(quarantine.get("paused"))
                quarantine_state = (
                    "QUARANTINED"
                    if pause_confirmed
                    else "QUARANTINE_PENDING"
                )
                quarantine_intent_state = (
                    "QUARANTINED" if pause_confirmed else "REMOTE_CREATED"
                )
                _set_replacement_strategy_quarantine(
                    db,
                    scope,
                    campaign_id=new_campaign_id,
                    state=quarantine_state,
                )
                _mark_rebuild_phase(
                    db,
                    scope,
                    rebuild_request,
                    phase=quarantine_state,
                    state=quarantine_intent_state,
                    campaign_id=new_campaign_id,
                    details={"remote_pause_confirmed": pause_confirmed},
                    error_json={
                        "reason": "creative_guard_rebuild_finalization_failed",
                        "type": type(exc).__name__,
                        "message": str(exc)[:1000],
                    },
                )
                mutation.commit(db)
            except GmvMaxMutationFenceLost:
                db.rollback()
                raise
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.critical(
                    "creative guard failed to persist replacement quarantine",
                    exc_info=True,
                    extra={
                        "old_campaign_id": scope.campaign_id,
                        "new_campaign_id": new_campaign_id,
                    },
                )
        elif rebuild_request is not None and old_pause_confirmed:
            try:
                db.rollback()
                await _compensate_old_campaign_after_unsent_rebuild(
                    db,
                    client,
                    scope,
                    rebuild_request,
                    mutation=mutation,
                    cause=exc,
                )
            except GmvMaxMutationFenceLost:
                db.rollback()
                raise
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.critical(
                    "creative guard could not restore the old campaign after "
                    "a definitely unsent/rejected replacement",
                    exc_info=True,
                    extra={"old_campaign_id": scope.campaign_id},
                )
        raise
    finally:
        await client.aclose()


async def _reset_campaign_for_product_card(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    with gmvmax_mutation_lease(
        db,
        workspace_id=int(scope.workspace_id),
        auth_id=int(scope.auth_id),
        owner_prefix="creative-guard-reset",
        timeout=0.1,
    ) as mutation:
        return await _reset_campaign_for_product_card_unlocked(
            db,
            scope,
            metric,
            decision,
            mutation=mutation,
        )


def _decide_creative(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
    campaign_metrics: list[CreativeMetric] | None = None,
) -> dict[str, Any]:
    is_product_card = str(metric.creative_id or "") == "-1"
    ignored = {str(item) for item in (scope.config.get("ignore_creative_ids") or [])}
    if not metric.creative_id or (metric.creative_id in ignored and not is_product_card):
        return {"action": "HOLD", "reason": "ignored_creative"}

    allowed_statuses = {str(item).upper() for item in (scope.config.get("min_statuses") or [])}
    status = str(metric.status or "").upper()
    if allowed_statuses and status not in allowed_statuses:
        return {
            "action": "HOLD",
            "reason": f"status_not_eligible:{status or 'MISSING'}",
        }

    product_price_basis = _load_product_price_basis(db, scope, metric.item_group_id)
    product_price_cents = (
        int(product_price_basis["cents"])
        if product_price_basis.get("cents")
        else None
    )
    target_roas = _target_roas(scope)
    base_allowed_cpa_cents = None
    allowed_cpa_cents = None
    if product_price_cents:
        base_allowed_cpa_cents = int(
            (Decimal(product_price_cents) / target_roas).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        evidence_multiplier = (
            _to_decimal(scope.config.get("no_order_allowed_cpa_multiplier"), "1.5")
            or Decimal("1.5")
        )
        allowed_cpa_cents = int(
            (Decimal(base_allowed_cpa_cents) * evidence_multiplier).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    budget = max(0, int(scope.budget_cents or 0))
    budget_cap = None
    if budget > 0:
        budget_cap = int(
            (Decimal(budget) * (_to_decimal(scope.config.get("no_order_budget_share_cap"), "0.20") or Decimal("0.20")))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    price_cap = None
    if product_price_cents:
        price_cap = int(
            (Decimal(product_price_cents) * (_to_decimal(scope.config.get("no_order_price_multiplier_cap"), "2.5") or Decimal("2.5")))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    threshold_candidates = [item for item in (allowed_cpa_cents, budget_cap, price_cap) if item and item > 0]
    if not threshold_candidates:
        return {"action": "HOLD", "reason": "threshold_basis_missing"}
    no_order_threshold = min(threshold_candidates)
    configured_no_order_threshold = max(
        0,
        _to_int(scope.config.get("no_order_spend_cents"), 0),
    )
    if (
        bool(scope.config.get("legacy_absolute_no_order_threshold_enabled", False))
        and configured_no_order_threshold > 0
    ):
        no_order_threshold = max(no_order_threshold, configured_no_order_threshold)
    if budget > 0:
        floor = int(
            (Decimal(budget) * (_to_decimal(scope.config.get("no_order_budget_share_floor"), "0.0") or Decimal("0.0")))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        floor = min(floor, _to_int(scope.config.get("no_order_min_spend_cents"), 300))
        no_order_threshold = max(no_order_threshold, floor)

    expected_cvr = _campaign_expected_cvr(db, scope, metric.item_group_id)
    click_threshold = max(1, ceil((_to_decimal(scope.config.get("click_no_order_expected_orders"), "1.0") or Decimal("1.0")) / expected_cvr))

    reason_context = {
        "creative_id": metric.creative_id,
        "item_group_id": metric.item_group_id,
        "creative_delivery_status": metric.status,
        "cost_cents": metric.cost_cents,
        "orders": metric.orders,
        "gross_revenue_cents": metric.gross_revenue_cents,
        "product_clicks": metric.product_clicks,
        "product_price_cents": product_price_cents,
        "product_price_source": product_price_basis.get("source"),
        "target_roas": str(target_roas),
        "base_allowed_cpa_cents": base_allowed_cpa_cents,
        "allowed_cpa_cents": allowed_cpa_cents,
        "budget_cap_cents": budget_cap,
        "price_cap_cents": price_cap,
        "no_order_threshold_cents": no_order_threshold,
        "configured_no_order_threshold_cents": configured_no_order_threshold,
        "expected_cvr": str(expected_cvr),
        "click_no_order_threshold": click_threshold,
    }

    remove_action = "RESET_CAMPAIGN" if is_product_card else "REMOVE"

    if metric.orders <= 0 and metric.cost_cents >= no_order_threshold:
        if is_product_card:
            protected = _product_card_reset_protection(scope, metric, campaign_metrics or [metric], reason_context)
            if protected:
                return protected
        return {
            "action": remove_action,
            "reason": "creative_guard:no_order_spend_threshold",
            "context": reason_context,
        }
    if metric.orders <= 0 and metric.product_clicks >= click_threshold:
        if is_product_card:
            protected = _product_card_reset_protection(scope, metric, campaign_metrics or [metric], reason_context)
            if protected:
                return protected
        return {
            "action": remove_action,
            "reason": "creative_guard:clicks_without_order",
            "context": reason_context,
        }

    min_spend = int(
        Decimal(no_order_threshold)
        * (_to_decimal(scope.config.get("roi_min_spend_allowed_cpa_multiplier"), "0.8") or Decimal("0.8"))
    )
    has_conversion_signal = metric.orders > 0 or metric.gross_revenue_cents > 0
    if has_conversion_signal and metric.cost_cents >= min_spend and metric.roi is not None and metric.roi < target_roas:
        if bool(scope.config.get("protect_converting_creatives", True)):
            min_orders = max(1, _to_int(scope.config.get("converting_creative_min_orders"), 1))
            grace_ratio = (
                _to_decimal(scope.config.get("converting_creative_roi_grace_ratio"), "0.85")
                or Decimal("0.85")
            )
            min_roi_floor = (
                _to_decimal(scope.config.get("converting_creative_min_roi"), "1.2")
                or Decimal("1.2")
            )
            grace_floor = max(min_roi_floor, (target_roas * grace_ratio).quantize(Decimal("0.0001")))
            if metric.orders >= min_orders and metric.roi >= grace_floor:
                reason_context.update(
                    {
                        "protect_converting_creatives": True,
                        "converting_creative_min_orders": min_orders,
                        "converting_creative_roi_grace_floor": str(grace_floor),
                    }
                )
                return {
                    "action": "HOLD",
                    "reason": "creative_guard:converting_creative_grace",
                    "context": reason_context,
                }
        if is_product_card:
            protected = _product_card_reset_protection(scope, metric, campaign_metrics or [metric], reason_context)
            if protected:
                return protected
        return {
            "action": remove_action,
            "reason": "creative_guard:roi_below_target",
            "context": reason_context,
        }

    return {"action": "HOLD", "reason": "creative_guard:no_change", "context": reason_context}


def _insert_event(
    db: Session,
    *,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
    result: str,
    request_json: Mapping[str, Any] | None = None,
    response_json: Mapping[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    request_payload = dict(request_json or {})
    response_payload = dict(response_json or {})
    db.execute(
        text(
            """
            insert into gmv_campaign_guard_events (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, event_type, action, reason, result,
                cost_cents, gross_revenue_cents, orders, roi,
                request_json, response_json, error_message, created_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                :strategy_id, 'CREATIVE_GUARD', :action, :reason, :result,
                :cost_cents, :gross_revenue_cents, :orders, :roi,
                :request_json, :response_json, :error_message, :created_at
            )
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
            "strategy_id": scope.strategy_id,
            "action": str(decision.get("action") or "HOLD"),
            "reason": str(decision.get("reason") or ""),
            "result": result,
            "cost_cents": metric.cost_cents,
            "gross_revenue_cents": metric.gross_revenue_cents,
            "orders": metric.orders,
            "roi": str(metric.roi) if metric.roi is not None else None,
            "request_json": _json_dumps(request_payload),
            "response_json": _json_dumps(response_payload),
            "error_message": error_message,
            "created_at": _utcnow().replace(tzinfo=None),
        },
    )
    context = {}
    if isinstance(decision.get("context"), Mapping):
        context = dict(decision.get("context") or {})
    try:
        db.execute(
            text(
                """
                insert into gmv_hermes_ad_learning_samples (
                    workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                    item_group_id, creative_id, sample_type, action, reason, result,
                    cost_cents, gross_revenue_cents, orders, roi,
                    feature_json, label_json, source_event, observed_at, created_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                    :item_group_id, :creative_id, 'CREATIVE_GUARD', :action, :reason, :result,
                    :cost_cents, :gross_revenue_cents, :orders, :roi,
                    :feature_json, :label_json, 'gmv_campaign_guard_events', :observed_at, :created_at
                )
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "auth_id": scope.auth_id,
                "advertiser_id": scope.advertiser_id,
                "store_id": scope.store_id,
                "campaign_id": scope.campaign_id,
                "item_group_id": metric.item_group_id,
                "creative_id": metric.creative_id,
                "action": str(decision.get("action") or "HOLD"),
                "reason": str(decision.get("reason") or ""),
                "result": result,
                "cost_cents": metric.cost_cents,
                "gross_revenue_cents": metric.gross_revenue_cents,
                "orders": metric.orders,
                "roi": str(metric.roi) if metric.roi is not None else None,
                "feature_json": _json_dumps(
                    {
                        "decision_context": context,
                        "request": request_payload,
                        "creative_status": metric.status,
                        "product_clicks": metric.product_clicks,
                        "product_impressions": metric.product_impressions,
                        "ad_click_rate": str(metric.ad_click_rate) if metric.ad_click_rate is not None else None,
                        "product_click_rate": str(metric.product_click_rate) if metric.product_click_rate is not None else None,
                        "video_view_rate_2s": str(metric.video_view_rate_2s) if metric.video_view_rate_2s is not None else None,
                        "video_view_rate_6s": str(metric.video_view_rate_6s) if metric.video_view_rate_6s is not None else None,
                        "video_completion_rate": str(metric.video_view_rate_100) if metric.video_view_rate_100 is not None else None,
                    }
                ),
                "label_json": _json_dumps({"response": response_payload, "error": error_message}),
                "observed_at": _utcnow().replace(tzinfo=None),
                "created_at": _utcnow().replace(tzinfo=None),
            },
        )
    except Exception:
        logger.warning("failed to write Hermes creative learning sample", exc_info=True)


def _insert_product_card_monitor_sample(
    db: Session,
    *,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
) -> None:
    context = {}
    if isinstance(decision.get("context"), Mapping):
        context = dict(decision.get("context") or {})
    try:
        db.execute(
            text(
                """
                insert into gmv_hermes_ad_learning_samples (
                    workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                    item_group_id, creative_id, sample_type, action, reason, result,
                    cost_cents, gross_revenue_cents, orders, roi,
                    feature_json, label_json, source_event, observed_at, created_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                    :item_group_id, :creative_id, 'PRODUCT_CARD_MONITOR', :action, :reason, 'OBSERVED',
                    :cost_cents, :gross_revenue_cents, :orders, :roi,
                    :feature_json, cast(:label_json as json), 'gmvmax_creative_guard_cycle',
                    :observed_at, :created_at
                )
                """
            ),
            {
                "workspace_id": scope.workspace_id,
                "auth_id": scope.auth_id,
                "advertiser_id": scope.advertiser_id,
                "store_id": scope.store_id,
                "campaign_id": scope.campaign_id,
                "item_group_id": metric.item_group_id,
                "creative_id": metric.creative_id,
                "action": str(decision.get("action") or "HOLD"),
                "reason": str(decision.get("reason") or ""),
                "cost_cents": metric.cost_cents,
                "gross_revenue_cents": metric.gross_revenue_cents,
                "orders": metric.orders,
                "roi": str(metric.roi) if metric.roi is not None else None,
                "feature_json": _json_dumps(
                    {
                        "context": context,
                        "creative_status": metric.status,
                        "product_clicks": metric.product_clicks,
                        "product_impressions": metric.product_impressions,
                        "ad_click_rate": str(metric.ad_click_rate)
                        if metric.ad_click_rate is not None
                        else None,
                        "product_click_rate": str(metric.product_click_rate)
                        if metric.product_click_rate is not None
                        else None,
                    }
                ),
                "label_json": _json_dumps({"decision": dict(decision)}),
                "observed_at": _utcnow().replace(tzinfo=None),
                "created_at": _utcnow().replace(tzinfo=None),
            },
        )
    except Exception:
        logger.warning("failed to write Hermes product card monitor sample", exc_info=True)


async def _remove_creative(db: Session, scope: CampaignScope, metric: CreativeMetric, decision: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    item = GMVMaxCreativeStatusUpdateItem(
        item_id=str(metric.creative_id),
        spu_id_list=[str(metric.item_group_id)] if metric.item_group_id else None,
    )
    body = GMVMaxCreativeStatusUpdateBody(
        campaign_id=str(scope.campaign_id),
        item_list=[item],
        action="REMOVE",
    )
    request = GMVMaxCreativeStatusUpdateRequest(advertiser_id=str(scope.advertiser_id), body=body)
    request_payload = request.model_dump(exclude_none=True)
    if bool(scope.config.get("dry_run", False)):
        return request_payload, {"dry_run": True, "decision": dict(decision)}
    mutation = active_gmvmax_mutation_lease(db)
    if mutation is None:
        raise GmvMaxMutationFenceLost(
            "creative removal has no active mutation lease"
        )
    _assert_creative_guard_mutation_allowed(db, scope)
    _assert_campaign_supports_creative_status_update(db, scope)
    client = build_ttb_gmvmax_client(
        db,
        auth_id=scope.auth_id,
        timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
    )
    try:
        mutation.assert_current(db)
        response = await client.gmv_max_creative_status_update(request)
        mutation.assert_current(db)
    finally:
        await client.aclose()
    if hasattr(response, "model_dump"):
        response_payload = response.model_dump(exclude_none=True)
    else:
        response_payload = {"request_id": getattr(response, "request_id", None)}
    return request_payload, response_payload


async def _add_back_creative(
    db: Session,
    scope: CampaignScope,
    metric: CreativeMetric,
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    item = GMVMaxCreativeStatusUpdateItem(
        item_id=str(metric.creative_id),
        spu_id_list=[str(metric.item_group_id)] if metric.item_group_id else None,
    )
    body = GMVMaxCreativeStatusUpdateBody(
        campaign_id=str(scope.campaign_id),
        item_list=[item],
        action="ADD",
    )
    request = GMVMaxCreativeStatusUpdateRequest(
        advertiser_id=str(scope.advertiser_id), body=body
    )
    request_payload = request.model_dump(exclude_none=True)
    if bool(scope.config.get("dry_run", False)):
        return request_payload, {"dry_run": True, "decision": dict(decision)}
    mutation = active_gmvmax_mutation_lease(db)
    if mutation is None:
        raise GmvMaxMutationFenceLost(
            "creative add-back has no active mutation lease"
        )
    _assert_creative_guard_mutation_allowed(db, scope)
    _assert_campaign_supports_creative_status_update(db, scope)
    client = build_ttb_gmvmax_client(
        db,
        auth_id=scope.auth_id,
        timeout=float(getattr(settings, "GMVMAX_TIKTOK_TIMEOUT_SECONDS", 45.0)),
    )
    try:
        mutation.assert_current(db)
        response = await client.gmv_max_creative_status_update(request)
        mutation.assert_current(db)
    finally:
        await client.aclose()
    if hasattr(response, "model_dump"):
        response_payload = response.model_dump(exclude_none=True)
    else:
        response_payload = {"request_id": getattr(response, "request_id", None)}
    return request_payload, response_payload


async def _retest_removed_creatives(
    db: Session,
    scope: CampaignScope,
    metrics: list[CreativeMetric],
    *,
    now: datetime,
) -> dict[str, Any]:
    config = _creative_retest_config(scope)
    if (
        not bool(config.get("enabled", True))
        or _manual_pause_override_active(db, scope, now=now)
        or not _campaign_is_currently_enabled(db, scope)
    ):
        return {"added": 0, "creative_ids": [], "errors": 0}

    activity = _load_campaign_activity(db, scope, now=now)
    low_delivery_threshold = max(
        max(0, _to_int(config.get("low_delivery_min_spend_cents"), 100)),
        int(
            Decimal(max(0, int(scope.budget_cents or 0)))
            * (_to_decimal(config.get("low_delivery_budget_share"), "0.01") or Decimal("0.01"))
        ),
    )
    low_delivery = (
        activity.low_spend_delta_cents < low_delivery_threshold
        and activity.low_spend_order_delta <= 0
    )
    max_attempts = max(1, _to_int(config.get("max_attempts_per_campaign"), 4))
    bucket_hours = max(1, _to_int(config.get("time_bucket_hours"), 4))
    current_bucket = _creative_time_bucket(db, scope, now, bucket_hours=bucket_hours)
    permanent = set(_historical_removed_creatives_for_scope(db, scope))
    candidates: list[dict[str, Any]] = []

    for metric in metrics:
        creative_id = str(metric.creative_id or "")
        if creative_id in {"", "-1", "0"} or (creative_id, metric.item_group_id) in permanent:
            continue
        history = _creative_event_history(db, scope, creative_id)
        if not history or str(history[-1].get("action") or "").upper() != "REMOVE":
            continue
        latest_remove = history[-1]
        inherited_exclusion = (
            str(latest_remove.get("reason") or "")
            == "creative_guard:inherit_historical_exclusions"
        )
        prior_adds = [event for event in history if str(event.get("action") or "").upper() == "ADD"]
        if len(prior_adds) >= max_attempts:
            continue
        quality = _creative_quality_context(metric, metrics, config)
        failed_attempts, latest_failure_at = _failed_retest_state(
            db,
            scope,
            creative_id,
        )
        cooldown_minutes = _dynamic_retest_cooldown_minutes(
            config,
            prior_attempts=len(prior_adds) + failed_attempts,
            high_quality=bool(quality.get("high_quality")),
            low_delivery=low_delivery,
        )
        removed_at = _as_utc(latest_remove.get("created_at"))
        if removed_at is None or now < removed_at + timedelta(minutes=cooldown_minutes):
            continue
        if (
            latest_failure_at is not None
            and now
            < latest_failure_at + timedelta(minutes=cooldown_minutes)
        ):
            continue
        used_buckets = {
            str(_json_dict(_json_dict(event.get("request_json")).get("retest")).get("time_bucket"))
            for event in prior_adds
            if _json_dict(_json_dict(event.get("request_json")).get("retest")).get("time_bucket")
        }
        removed_bucket = _creative_time_bucket(
            db, scope, removed_at, bucket_hours=bucket_hours
        )
        require_new_bucket = bool(config.get("require_new_time_bucket", True))
        low_delivery_override = bool(config.get("low_delivery_override_new_bucket", True))
        if (
            require_new_bucket
            and not (low_delivery and low_delivery_override)
            and (current_bucket == removed_bucket or current_bucket in used_buckets)
        ):
            continue
        candidates.append(
            {
                "metric": metric,
                "quality": quality,
                "attempt": len(prior_adds) + 1,
                "cooldown_minutes": cooldown_minutes,
                "failed_attempts": failed_attempts,
                "removed_at": removed_at,
                "inherited_exclusion": inherited_exclusion,
            }
        )

    candidates.sort(
        key=lambda item: (
            int(bool(item["quality"].get("high_quality"))),
            _to_int(item["quality"].get("quality_score"), 0),
            int(item["metric"].cost_cents or 0),
        ),
        reverse=True,
    )
    limit = max(1, _to_int(config.get("max_add_back_per_cycle"), 1))
    added_ids: list[str] = []
    errors = 0
    for candidate in candidates:
        if len(added_ids) >= limit:
            break
        metric = candidate["metric"]
        retest_context = {
            "attempt": candidate["attempt"],
            "failed_attempts": candidate["failed_attempts"],
            "time_bucket": current_bucket,
            "cooldown_minutes": candidate["cooldown_minutes"],
            "baseline_metrics": _metric_snapshot(metric),
            "quality": candidate["quality"],
            "low_delivery": low_delivery,
            "low_delivery_delta_cents": activity.low_spend_delta_cents,
            "low_delivery_threshold_cents": low_delivery_threshold,
            "added_at": now.isoformat(),
            "inherited_exclusion": bool(candidate.get("inherited_exclusion")),
        }
        decision = {
            "action": "ADD",
            "reason": "creative_guard:scheduled_retest",
            "context": retest_context,
        }
        try:
            with gmvmax_mutation_lease(
                db,
                workspace_id=int(scope.workspace_id),
                auth_id=int(scope.auth_id),
                owner_prefix="creative-guard-add-back",
                timeout=0.1,
            ) as mutation:
                request_payload, response_payload = await _add_back_creative(
                    db, scope, metric, decision
                )
                _insert_event(
                    db,
                    scope=scope,
                    metric=metric,
                    decision=decision,
                    result="SUCCESS",
                    request_json={
                        **request_payload,
                        "creative_id": str(metric.creative_id),
                        "item_group_id": str(metric.item_group_id or ""),
                        "retest": retest_context,
                    },
                    response_json=response_payload,
                )
                mutation.commit(db)
            added_ids.append(str(metric.creative_id))
        except (GmvMaxMutationBusy, GmvMaxMutationFenceLost):
            logger.info(
                "creative guard scheduled retest held by mutation ownership",
                extra={
                    "campaign_id": scope.campaign_id,
                    "creative_id": metric.creative_id,
                },
            )
            break
        except CreativeGuardAutomationHold:
            logger.info(
                "creative guard scheduled retest held by campaign control state",
                extra={
                    "campaign_id": scope.campaign_id,
                    "creative_id": metric.creative_id,
                },
            )
            break
        except Exception as exc:  # noqa: BLE001
            errors += 1
            _insert_event(
                db,
                scope=scope,
                metric=metric,
                decision=decision,
                result="FAILED",
                request_json={
                    "creative_id": str(metric.creative_id),
                    "item_group_id": str(metric.item_group_id or ""),
                    "retest": retest_context,
                },
                error_message=str(exc),
            )
            logger.exception(
                "creative guard scheduled retest failed",
                extra={"campaign_id": scope.campaign_id, "creative_id": metric.creative_id},
            )
    return {"added": len(added_ids), "creative_ids": added_ids, "errors": errors}


async def rebuild_campaign_for_delivery_failure(
    db: Session,
    *,
    strategy_id: int,
    reason: str,
    context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild a managed campaign after repeated verified delivery failures."""

    scope = next((item for item in _load_scopes(db) if item.strategy_id == int(strategy_id)), None)
    if scope is None:
        raise RuntimeError(f"creative guard scope not found for strategy {strategy_id}")
    decision_context = dict(context or {})
    manual_pause = _manual_pause_override_active(db, scope)
    currently_enabled = _campaign_is_currently_enabled(db, scope)
    if manual_pause or not currently_enabled:
        return (
            {
                "old_campaign_id": scope.campaign_id,
                "reason": reason,
            },
            {
                "rebuild_deferred": True,
                "manual_pause_override": manual_pause,
                "reason": (
                    "manual pause override requires HOLD"
                    if manual_pause
                    else "campaign is not currently enabled; explicit enable required"
                ),
            },
        )
    rebuild_limit = max(1, _to_int(decision_context.get("rebuild_limit_24h"), 2))
    recent_rebuilds = db.execute(
        text(
            """
            select count(distinct e.id)
            from gmv_campaign_guard_events e
            where e.workspace_id=:workspace_id
              and e.auth_id=:auth_id
              and e.advertiser_id=:advertiser_id
              and e.store_id=:store_id
              and e.action='RESET_CAMPAIGN'
              and e.result='SUCCESS'
              and e.created_at >= utc_timestamp(6) - interval 24 hour
              and e.campaign_id in (
                  select distinct sibling.campaign_id
                  from gmvmax_product_campaign_item_groups sibling
                  where sibling.workspace_id=:workspace_id
                    and sibling.auth_id=:auth_id
                    and sibling.advertiser_id=:advertiser_id
                    and sibling.store_id=:store_id
                    and sibling.item_group_id in (
                        select current_scope.item_group_id
                        from gmvmax_product_campaign_item_groups current_scope
                        where current_scope.workspace_id=:workspace_id
                          and current_scope.auth_id=:auth_id
                          and current_scope.advertiser_id=:advertiser_id
                          and current_scope.store_id=:store_id
                          and current_scope.campaign_id=:campaign_id
                    )
              )
            """
        ),
        {
            "workspace_id": scope.workspace_id,
            "auth_id": scope.auth_id,
            "advertiser_id": scope.advertiser_id,
            "store_id": scope.store_id,
            "campaign_id": scope.campaign_id,
        },
    ).scalar_one()
    if int(recent_rebuilds or 0) >= rebuild_limit:
        return (
            {
                "old_campaign_id": scope.campaign_id,
                "reason": reason,
                "recent_rebuilds_24h": int(recent_rebuilds or 0),
                "rebuild_limit_24h": rebuild_limit,
            },
            {
                "rebuild_deferred": True,
                "reason": "rebuild circuit open",
                "recent_rebuilds_24h": int(recent_rebuilds or 0),
                "rebuild_limit_24h": rebuild_limit,
            },
        )

    metric = CreativeMetric(
        creative_id="__campaign_delivery_failure__",
        item_group_id=_catalog_primary_item_group_id(db, scope),
        status=scope.secondary_status or scope.operation_status,
        cost_cents=0,
        gross_revenue_cents=0,
        orders=0,
        product_impressions=0,
        product_clicks=0,
        ad_click_rate=None,
        product_click_rate=None,
        ad_conversion_rate=None,
        roi=None,
    )
    return await _reset_campaign_for_product_card(
        db,
        scope,
        metric,
        {
            "action": "RESET_CAMPAIGN",
            "reason": reason or "creative_guard:no_spend_timeout",
            "context": decision_context,
        },
    )


async def run_creative_guard_cycle(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    cycle_time = now or _utcnow()
    scopes = _load_scopes(db)
    summary: dict[str, Any] = {
        "campaigns": len(scopes),
        "checked_creatives": 0,
        "removed": 0,
        "readded": 0,
        "reset_campaigns": 0,
        "no_spend_resets": 0,
        "low_spend_resets": 0,
        "paused_duplicates": 0,
        "held": 0,
        "data_holds": 0,
        "manual_override_holds": 0,
        "skipped": 0,
        "errors": 0,
        "rebuild_recovery_candidates": 0,
        "rebuilds_recovered": 0,
        "rebuild_quarantines_retried": 0,
        "rebuild_compensations": 0,
    }
    recovery_scope_keys: set[tuple[int, int, str]] = set()
    recovery_candidates = _rebuild_recovery_candidates(db)
    summary["rebuild_recovery_candidates"] = len(recovery_candidates)
    for recovery_scope, intent, strategy_enabled in recovery_candidates:
        recovery_key = (
            int(recovery_scope.workspace_id),
            int(recovery_scope.auth_id),
            str(recovery_scope.campaign_id),
        )
        recovery_scope_keys.add(recovery_key)
        recovery_metric = CreativeMetric(
            creative_id="__rebuild_recovery__",
            item_group_id=(
                str((intent.request_json or {}).get("item_group_ids", [""])[0])
                if isinstance(intent.request_json, Mapping)
                and (intent.request_json or {}).get("item_group_ids")
                else None
            ),
            status=recovery_scope.secondary_status or recovery_scope.operation_status,
            cost_cents=0,
            gross_revenue_cents=0,
            orders=0,
            product_impressions=0,
            product_clicks=0,
            ad_click_rate=None,
            product_click_rate=None,
            ad_conversion_rate=None,
            roi=None,
        )
        try:
            state = str(intent.state or "").upper()
            phase = _intent_rebuild_phase(intent)
            if not strategy_enabled and _terminalize_rebuild_after_permission_revoked(
                db,
                recovery_scope,
                intent,
            ):
                summary["held"] += 1
                continue
            if phase == "QUARANTINE_PENDING":
                if await _retry_rebuild_quarantine(db, recovery_scope, intent):
                    summary["rebuild_quarantines_retried"] += 1
                else:
                    summary["held"] += 1
                continue
            if state == "COMPENSATION_PENDING":
                with gmvmax_mutation_lease(
                    db,
                    workspace_id=int(recovery_scope.workspace_id),
                    auth_id=int(recovery_scope.auth_id),
                    owner_prefix="creative-guard-rebuild-compensation",
                    timeout=0.1,
                ) as mutation:
                    client = build_ttb_gmvmax_client(
                        db,
                        auth_id=int(recovery_scope.auth_id),
                        timeout=float(
                            getattr(
                                settings,
                                "GMVMAX_TIKTOK_TIMEOUT_SECONDS",
                                45.0,
                            )
                        ),
                    )
                    try:
                        request = _request_from_rebuild_intent(
                            intent,
                            recovery_scope,
                        )
                        if await _compensate_old_campaign_after_unsent_rebuild(
                            db,
                            client,
                            recovery_scope,
                            request,
                            mutation=mutation,
                            cause=RuntimeError(
                                "resuming interrupted rebuild compensation"
                            ),
                        ):
                            summary["rebuild_compensations"] += 1
                    finally:
                        await client.aclose()
                continue

            recovery_decision = {
                "action": "RESET_CAMPAIGN",
                "reason": "creative_guard:rebuild_recovery",
                "context": {"resume_intent_id": int(intent.id)},
            }
            request_json, response_json = await _reset_campaign_for_product_card(
                db,
                recovery_scope,
                recovery_metric,
                recovery_decision,
            )
            _insert_event(
                db,
                scope=recovery_scope,
                metric=recovery_metric,
                decision=recovery_decision,
                result="SUCCESS",
                request_json=request_json,
                response_json=response_json,
            )
            db.commit()
            summary["rebuilds_recovered"] += 1
            summary["reset_campaigns"] += 1
        except (
            CreativeGuardAutomationHold,
            GmvMaxMutationBusy,
            GmvMaxMutationFenceLost,
        ):
            db.rollback()
            try:
                _rotate_rebuild_recovery_candidate(
                    db,
                    recovery_scope,
                    intent,
                )
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.warning(
                    "creative guard could not rotate held rebuild recovery",
                    exc_info=True,
                    extra={
                        "campaign_id": recovery_scope.campaign_id,
                        "intent_id": int(intent.id),
                    },
                )
            summary["held"] += 1
            logger.info(
                "creative guard rebuild recovery is held",
                extra={
                    "campaign_id": recovery_scope.campaign_id,
                    "intent_id": int(intent.id),
                },
            )
        except Exception:
            db.rollback()
            try:
                _rotate_rebuild_recovery_candidate(
                    db,
                    recovery_scope,
                    intent,
                )
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.warning(
                    "creative guard could not rotate failed rebuild recovery",
                    exc_info=True,
                    extra={
                        "campaign_id": recovery_scope.campaign_id,
                        "intent_id": int(intent.id),
                    },
                )
            summary["errors"] += 1
            logger.exception(
                "creative guard rebuild recovery failed",
                extra={
                    "campaign_id": recovery_scope.campaign_id,
                    "intent_id": int(intent.id),
                },
            )

    for scope in scopes:
        if (
            int(scope.workspace_id),
            int(scope.auth_id),
            str(scope.campaign_id),
        ) in recovery_scope_keys:
            summary["skipped"] += 1
            continue
        if not _scope_due(scope, cycle_time):
            summary["skipped"] += 1
            continue
        if _manual_pause_override_active(db, scope, now=cycle_time):
            _update_creative_guard_state(
                db,
                scope,
                now=cycle_time,
                interval_minutes=1,
                checked_creatives=0,
                action_count=0,
                data_quality={"state": "manual_pause_override"},
            )
            db.commit()
            summary["held"] += 1
            summary["manual_override_holds"] += 1
            continue
        data_quality = _creative_guard_data_quality(db, scope, now=cycle_time)
        if not bool(data_quality.get("campaign_valid")) or not bool(
            data_quality.get("creative_valid")
        ):
            _update_creative_guard_state(
                db,
                scope,
                now=cycle_time,
                interval_minutes=1,
                checked_creatives=0,
                action_count=0,
                data_quality=data_quality,
            )
            db.commit()
            summary["held"] += 1
            summary["data_holds"] += 1
            continue
        removed_in_campaign = 0
        max_remove = max(1, _to_int(scope.config.get("max_remove_per_campaign_per_cycle"), 10))
        try:
            paused_duplicates = await _pause_unmanaged_duplicate_campaigns(db, scope)
            if paused_duplicates:
                summary["paused_duplicates"] += paused_duplicates
                db.commit()
        except (GmvMaxMutationBusy, GmvMaxMutationFenceLost):
            db.rollback()
            summary["held"] += 1
            logger.info(
                "creative guard duplicate pause held by mutation ownership",
                extra={"campaign_id": scope.campaign_id},
            )
        except Exception:
            db.rollback()
            summary["errors"] += 1
            logger.exception(
                "creative guard failed to pause unmanaged duplicate campaigns",
                extra={"campaign_id": scope.campaign_id},
            )
        reset_failure_metric: CreativeMetric | None = None
        reset_failure_decision: dict[str, Any] = {
            "action": "RESET_CAMPAIGN",
            "reason": "creative_guard:no_spend_timeout",
        }
        try:
            no_spend_decision = _decide_no_spend_reset(db, scope, now=cycle_time)
            if no_spend_decision is not None:
                metric, decision = no_spend_decision
                reset_failure_metric = metric
                reset_failure_decision = dict(decision)
                request_payload, response_payload = await _reset_campaign_for_product_card(
                    db, scope, metric, decision
                )
                _insert_event(
                    db,
                    scope=scope,
                    metric=metric,
                    decision=decision,
                    result="SUCCESS",
                    request_json=request_payload,
                    response_json=response_payload,
                )
                summary["reset_campaigns"] += 1
                if str(decision.get("reason") or "") == "creative_guard:low_spend_timeout":
                    summary["low_spend_resets"] += 1
                else:
                    summary["no_spend_resets"] += 1
                _update_creative_guard_state(
                    db,
                    scope,
                    now=cycle_time,
                    interval_minutes=max(1, _to_int(scope.config.get("monitor_interval_minutes"), 3)),
                    checked_creatives=0,
                    action_count=1,
                    data_quality=data_quality,
                )
                db.commit()
                continue
        except (
            CreativeGuardAutomationHold,
            GmvMaxMutationBusy,
            GmvMaxMutationFenceLost,
        ):
            db.rollback()
            _update_creative_guard_state(
                db,
                scope,
                now=cycle_time,
                interval_minutes=1,
                checked_creatives=0,
                action_count=0,
                data_quality={"state": "campaign_control_hold"},
            )
            db.commit()
            summary["held"] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            summary["errors"] += 1
            logger.exception(
                "creative guard no-spend reset failed",
                extra={"campaign_id": scope.campaign_id},
            )
            metric = reset_failure_metric or CreativeMetric(
                creative_id="__campaign_no_spend__",
                item_group_id=None,
                status=scope.secondary_status or scope.operation_status,
                cost_cents=0,
                gross_revenue_cents=0,
                orders=0,
                product_impressions=0,
                product_clicks=0,
                ad_click_rate=None,
                product_click_rate=None,
                ad_conversion_rate=None,
                roi=None,
            )
            _insert_event(
                db,
                scope=scope,
                metric=metric,
                decision=reset_failure_decision,
                result="FAILED",
                error_message=str(exc),
            )
            db.commit()
        scope_metrics = _load_creatives(db, scope)
        monitor_interval = _creative_monitor_interval_minutes(scope, scope_metrics)
        retest_result: dict[str, Any] = {"added": 0, "creative_ids": [], "errors": 0}
        try:
            retest_result = await _retest_removed_creatives(
                db, scope, scope_metrics, now=cycle_time
            )
            summary["readded"] += _to_int(retest_result.get("added"), 0)
            summary["errors"] += _to_int(retest_result.get("errors"), 0)
            if retest_result.get("added") or retest_result.get("errors"):
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            summary["errors"] += 1
            logger.exception(
                "creative guard retest evaluation failed",
                extra={"campaign_id": scope.campaign_id},
            )
        just_readded = {str(item) for item in (retest_result.get("creative_ids") or [])}
        action_count = _to_int(retest_result.get("added"), 0)
        for metric in scope_metrics:
            summary["checked_creatives"] += 1
            action = "HOLD"
            try:
                if str(metric.creative_id or "") in just_readded:
                    summary["held"] += 1
                    continue
                evaluation_metric, retest_context = _metric_for_current_retest_window(
                    db, scope, metric
                )
                decision = _decide_creative(
                    db, scope, evaluation_metric, scope_metrics
                )
                if retest_context:
                    context = dict(decision.get("context") or {})
                    context["retest_window"] = retest_context
                    decision = {**decision, "context": context}
                decision = _apply_learning_protection(
                    db,
                    scope,
                    evaluation_metric,
                    decision,
                    now=cycle_time,
                )
                action = str(decision.get("action") or "HOLD").upper()
                if str(metric.creative_id or "") == "-1":
                    _insert_product_card_monitor_sample(
                        db,
                        scope=scope,
                        metric=evaluation_metric,
                        decision=decision,
                    )
                if action not in {"REMOVE", "RESET_CAMPAIGN"}:
                    summary["held"] += 1
                    continue
                if action == "RESET_CAMPAIGN":
                    if _controlled_test_active(scope, now=cycle_time):
                        summary["skipped"] += 1
                        logger.info(
                            "creative guard product-card reset deferred during controlled test",
                            extra={
                                "campaign_id": scope.campaign_id,
                                "creative_id": metric.creative_id,
                            },
                        )
                        continue
                    cooling, cooldown_reason, paused_until = _campaign_pause_cooldown_active(db, scope, now=cycle_time)
                    if cooling:
                        summary["skipped"] += 1
                        logger.info(
                            "creative guard reset deferred during smart cooldown",
                            extra={
                                "campaign_id": scope.campaign_id,
                                "creative_id": metric.creative_id,
                                "paused_until": paused_until.isoformat() if paused_until else None,
                                "reason": cooldown_reason,
                            },
                        )
                        continue
                    if _already_reset_campaign(db, scope):
                        summary["skipped"] += 1
                        continue
                    defer_decision = _product_rebuild_cooldown_decision(
                        db,
                        scope,
                        metric,
                        decision,
                        now=cycle_time,
                    )
                    if defer_decision is not None:
                        decision = defer_decision
                    if str(decision.get("action") or "HOLD").upper() != "RESET_CAMPAIGN":
                        summary["held"] += 1
                        continue
                    request_payload, response_payload = await _reset_campaign_for_product_card(
                        db, scope, evaluation_metric, decision
                    )
                    _insert_event(
                        db,
                        scope=scope,
                        metric=evaluation_metric,
                        decision=decision,
                        result="SUCCESS",
                        request_json=request_payload,
                        response_json=response_payload,
                    )
                    summary["reset_campaigns"] += 1
                    action_count += 1
                    _update_creative_guard_state(
                        db,
                        scope,
                        now=cycle_time,
                        interval_minutes=monitor_interval,
                        checked_creatives=len(scope_metrics),
                        action_count=action_count,
                        data_quality=data_quality,
                    )
                    db.commit()
                    break
                if not _campaign_is_currently_enabled(db, scope):
                    summary["skipped"] += 1
                    logger.info(
                        "creative guard removal deferred because campaign is not enabled",
                        extra={
                            "campaign_id": scope.campaign_id,
                            "creative_id": metric.creative_id,
                            "operation_status": scope.operation_status,
                            "secondary_status": scope.secondary_status,
                        },
                    )
                    continue
                if removed_in_campaign >= max_remove:
                    summary["skipped"] += 1
                    continue
                if _already_removed(db, scope, metric.creative_id):
                    summary["skipped"] += 1
                    continue
                with gmvmax_mutation_lease(
                    db,
                    workspace_id=int(scope.workspace_id),
                    auth_id=int(scope.auth_id),
                    owner_prefix="creative-guard-remove",
                    timeout=0.1,
                ) as mutation:
                    request_payload, response_payload = await _remove_creative(
                        db, scope, evaluation_metric, decision
                    )
                    _insert_event(
                        db,
                        scope=scope,
                        metric=evaluation_metric,
                        decision=decision,
                        result="SUCCESS",
                        request_json={**request_payload, "decision": decision},
                        response_json=response_payload,
                    )
                    mutation.commit(db)
                removed_in_campaign += 1
                summary["removed"] += 1
                action_count += 1
                db.commit()
            except (
                CreativeGuardAutomationHold,
                GmvMaxMutationBusy,
                GmvMaxMutationFenceLost,
            ):
                db.rollback()
                summary["held"] += 1
                logger.info(
                    "creative guard mutation held by campaign control state",
                    extra={
                        "campaign_id": scope.campaign_id,
                        "creative_id": metric.creative_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception(
                    "creative guard evaluation failed",
                    extra={
                        "campaign_id": scope.campaign_id,
                        "creative_id": metric.creative_id,
                    },
                )
                _insert_event(
                    db,
                    scope=scope,
                    metric=metric,
                    decision={"action": action if action in {"REMOVE", "RESET_CAMPAIGN"} else "REMOVE", "reason": "creative_guard:error"},
                    result="FAILED",
                    error_message=str(exc),
                )
                db.commit()
                summary["errors"] += 1
        _update_creative_guard_state(
            db,
            scope,
            now=cycle_time,
            interval_minutes=monitor_interval,
            checked_creatives=len(scope_metrics),
            action_count=action_count,
            data_quality=data_quality,
        )
        db.commit()
    return summary


def run_creative_guard_cycle_sync(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    return asyncio.run(run_creative_guard_cycle(db, now=now))
