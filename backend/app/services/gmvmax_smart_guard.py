from __future__ import annotations

"""Near-real-time campaign guardrails for GMV Max automation.

The guard intentionally reads TikTok's current-day campaign report every cycle
instead of waiting for hourly fact tables. Hourly/daily tables remain the source
for charts and history; this service owns fast stop-loss state.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery import current_app
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvStrategyConfig
from app.providers.tiktok_business.gmvmax_client import (
    CampaignStatusUpdateRequest,
    GMVMAX_DIMENSIONS_BY_LEVEL,
    GMVMAX_METRICS_BY_LEVEL,
    GMVMaxCampaignUpdateBody,
    GMVMaxCampaignUpdateRequest,
    GMVMaxMetricsLevel,
    GMVMaxReportFiltering,
    GMVMaxReportGetRequest,
)
from app.gmvmax.services.report_pagination import (
    DEFAULT_NUMBERED_PAGE_LIMIT,
    OFFICIAL_REPORT_PAGE_SIZE,
    NumberedPaginationError,
    ReportPaginationState,
    report_page_has_more,
)
from app.services.ttb_client_factory import build_ttb_gmvmax_client
from app.services.gmvmax_hermes_advisor import review_smart_guard_action
from app.gmvmax.services.mutation_execution_lock import (
    GmvMaxMutationBusy,
    GmvMaxMutationFenceLost,
    gmvmax_mutation_lease,
)
from app.services.commerce_orders import current_order_timing_signal
from app.services.ttb_api import TTBBusinessError
from app.gmvmax.services.campaign_catalog_freshness import (
    catalog_observation_now,
)
from app.features.tenants.ttb.gmv_max.control import (
    is_manual_pause_override_active,
)

logger = logging.getLogger("gmv.services.gmvmax.smart_guard")

_MONEY_QUANT = Decimal("0.01")
_ROI_QUANT = Decimal("0.0001")
_ROAS_QUANT = Decimal("0.1")


@dataclass
class CatalogCampaign:
    workspace_id: int
    auth_id: int
    advertiser_id: str
    store_id: str
    campaign_id: str
    campaign_name: str | None
    promotion_type: str
    operation_status: str | None
    secondary_status: str | None
    budget_value: int | None
    roas_bid: Decimal | None


@dataclass
class RealtimeMetrics:
    cost_cents: int = 0
    net_cost_cents: int = 0
    gross_revenue_cents: int = 0
    orders: int = 0
    roi: Decimal | None = None
    raw: dict[str, Any] | None = None
    row_count: int = 0
    request_id: str | None = None
    fetched_at: datetime | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_naive(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_report_day(value: Any) -> str | None:
    """Normalize TikTok day dimensions without shifting advertiser-local dates."""

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        return date.fromisoformat(text_value).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(
            text_value.replace("Z", "+00:00")
        ).date().isoformat()
    except ValueError:
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))


def _source_age_seconds(db: Session, value: datetime | None) -> int | None:
    """Resolve legacy timestamps written as either UTC or database-local time."""

    if not isinstance(value, datetime):
        return None
    clocks = db.info.get("gmv_smart_guard_clock_pair")
    if not isinstance(clocks, Mapping):
        clocks = db.execute(
            text("select utc_timestamp(6) as utc_now, current_timestamp(6) as local_now")
        ).mappings().first() or {}
        db.info["gmv_smart_guard_clock_pair"] = clocks
    source = value.replace(tzinfo=None)
    candidates: list[int] = []
    for key in ("utc_now", "local_now"):
        clock = clocks.get(key)
        if not isinstance(clock, datetime):
            continue
        delta = int((clock.replace(tzinfo=None) - source).total_seconds())
        if delta >= -5:
            candidates.append(max(0, delta))
    return min(candidates) if candidates else None


def _strategy_config(strategy: GmvStrategyConfig) -> dict[str, Any]:
    value = strategy.config_json or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _runtime_state(strategy: GmvStrategyConfig) -> dict[str, Any]:
    value = getattr(strategy, "_gmv_guard_runtime", None)
    if isinstance(value, Mapping):
        return dict(value)
    config = _strategy_config(strategy)
    return {
        "smart_guard_state": dict(config.get("smart_guard_state") or {}),
        "creative_guard_state": dict(config.get("creative_guard_state") or {}),
    }


def _smart_guard_state(strategy: GmvStrategyConfig) -> dict[str, Any]:
    return dict(_runtime_state(strategy).get("smart_guard_state") or {})


def _set_smart_guard_state(strategy: GmvStrategyConfig, state: Mapping[str, Any]) -> None:
    runtime = _runtime_state(strategy)
    runtime["smart_guard_state"] = dict(state)
    setattr(strategy, "_gmv_guard_runtime", runtime)


def _load_runtime_state(
    db: Session,
    strategy: GmvStrategyConfig,
    *,
    campaign: CatalogCampaign | None = None,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            select runtime_json, state_version
            from gmv_campaign_realtime_state
            where strategy_id=:strategy_id
            order by updated_at desc, id desc
            limit 1
            """
        ),
        {"strategy_id": int(strategy.id)},
    ).mappings().first()
    if row is None and campaign is not None:
        row = db.execute(
            text(
                """
                select runtime_json, state_version
                from gmv_campaign_realtime_state
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and campaign_id=:campaign_id
                limit 1
                """
            ),
            {
                "workspace_id": campaign.workspace_id,
                "auth_id": campaign.auth_id,
                "advertiser_id": campaign.advertiser_id,
                "store_id": campaign.store_id,
                "campaign_id": campaign.campaign_id,
            },
        ).mappings().first()
    elif row is None:
        row = db.execute(
            text(
                """
                select runtime_json, state_version
                from gmv_campaign_realtime_state
                where id=(
                    select min(candidate.id)
                    from gmv_campaign_realtime_state candidate
                    where candidate.workspace_id=:workspace_id
                      and candidate.auth_id=:auth_id
                      and candidate.campaign_id=:campaign_id
                )
                  and 1=(
                    select count(*)
                    from gmv_campaign_realtime_state candidate
                    where candidate.workspace_id=:workspace_id
                      and candidate.auth_id=:auth_id
                      and candidate.campaign_id=:campaign_id
                  )
                """
            ),
            {
                "workspace_id": int(strategy.workspace_id),
                "auth_id": int(strategy.auth_id),
                "campaign_id": str(strategy.campaign_id),
            },
        ).mappings().first()
    runtime: dict[str, Any] = {}
    if row:
        value = row.get("runtime_json")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = {}
        if isinstance(value, Mapping):
            runtime = dict(value)
        setattr(strategy, "_gmv_guard_state_version", _to_int(row.get("state_version"), 0))
    legacy = _strategy_config(strategy)
    if not runtime.get("smart_guard_state") and legacy.get("smart_guard_state"):
        runtime["smart_guard_state"] = dict(legacy.get("smart_guard_state") or {})
    if not runtime.get("creative_guard_state") and legacy.get("creative_guard_state"):
        runtime["creative_guard_state"] = dict(legacy.get("creative_guard_state") or {})
    setattr(strategy, "_gmv_guard_runtime", runtime)
    return runtime


def _persist_runtime_state(
    db: Session,
    strategy: GmvStrategyConfig,
    *,
    campaign: CatalogCampaign,
    now: datetime,
) -> None:
    result = db.execute(
        text(
            """
            update gmv_campaign_realtime_state
            set runtime_json=:runtime_json,
                state_version=state_version+1,
                updated_at=:updated_at
            where strategy_id=:strategy_id
            """
        ),
        {
            "runtime_json": _json_dumps(_runtime_state(strategy)),
            "updated_at": now.replace(tzinfo=None),
            "strategy_id": int(strategy.id),
        },
    )
    if int(result.rowcount or 0) > 0:
        return
    db.execute(
        text(
            """
            update gmv_campaign_realtime_state
            set runtime_json=:runtime_json,
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
            "runtime_json": _json_dumps(_runtime_state(strategy)),
            "updated_at": now.replace(tzinfo=None),
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    )


def _clear_legacy_runtime_config(strategy: GmvStrategyConfig) -> None:
    config = _strategy_config(strategy)
    changed = False
    for key in ("smart_guard_state", "creative_guard_state"):
        if key in config:
            config.pop(key, None)
            changed = True
    if changed:
        strategy.config_json = config


def _guard_config(strategy: GmvStrategyConfig) -> dict[str, Any]:
    config = _strategy_config(strategy)
    guard = dict(config.get("smart_guard") or {})
    explicit_enabled = config.get("smart_guard_enabled")
    if "enabled" not in guard:
        guard["enabled"] = bool(explicit_enabled) if explicit_enabled is not None else False

    guard.setdefault("fast_monitor_interval_minutes", 1)
    guard.setdefault("monitor_interval_minutes", 3)
    guard.setdefault("slow_monitor_interval_minutes", 5)
    guard.setdefault("fast_spend_budget_share", "0.08")
    guard.setdefault("slow_spend_budget_share", "0.01")
    guard.setdefault("pause_cooldown_minutes", strategy.cooldown_minutes or 30)
    guard.setdefault("min_spend_cents", 300)
    guard.setdefault("no_order_spend_cents", None)
    guard.setdefault("use_effective_product_price", True)
    guard.setdefault("order_value_lookback_days", 90)
    guard.setdefault("no_order_allowed_cpa_multiplier", "1.0")
    guard.setdefault("no_order_price_multiplier_cap", "2.5")
    guard.setdefault("no_order_budget_share_cap", "0.20")
    guard.setdefault("early_spend_cap_cents", 800)
    guard.setdefault("daily_spend_cap_cents", None)
    guard.setdefault("daily_spend_cap_enabled", True)
    # GMV Max optimizes delivery at the product/day level. Intraday pacing by
    # repeatedly pausing the campaign destroys its learning signal.
    guard.setdefault("daily_budget_pacing", False)
    guard.setdefault("pacing_multiplier", "1.25")
    guard.setdefault("pacing_grace_minutes", 5)
    guard.setdefault("min_orders", 0)
    guard.setdefault("catastrophic_stop_enabled", True)
    guard.setdefault("catastrophic_no_order_multiplier", "3.0")
    guard.setdefault("catastrophic_bad_roi_budget_share", "0.25")
    guard.setdefault("catastrophic_bad_roi_ratio", "0.50")
    guard.setdefault("catastrophic_pause_cooldown_minutes", None)
    guard.setdefault("disable_strategy_on_catastrophic_stop", False)
    guard.setdefault("product_failure_lookback_hours", 24)
    guard.setdefault("dynamic_cooldown_enabled", True)
    guard.setdefault("cooldown_failure_step_ratio", "0.5")
    guard.setdefault("cooldown_severity_step_ratio", "0.25")
    guard.setdefault("no_order_cooldown_multiplier", "1.5")
    guard.setdefault("hard_stop_cooldown_multiplier", "2.0")
    guard.setdefault("max_pause_cooldown_minutes", 360)
    guard.setdefault("daily_loss_cap_enabled", True)
    guard.setdefault("daily_loss_no_order_multiplier", "3.0")
    guard.setdefault("daily_loss_budget_share", "0.25")
    guard.setdefault("daily_loss_repeat_shrink_ratio", "0.25")
    guard.setdefault("bad_roi_loss_budget_share", "0.35")
    guard.setdefault("bad_roi_loss_grace_cpa_multiplier", "1.0")
    guard.setdefault("recent_momentum_enabled", True)
    guard.setdefault("recent_momentum_window_minutes", 60)
    guard.setdefault("recent_momentum_min_orders", 2)
    guard.setdefault("recent_momentum_roi_multiplier", "0.80")
    guard.setdefault("recent_momentum_min_roi", None)
    guard.setdefault("recent_momentum_backfill_resume", True)
    guard.setdefault("dynamic_budget_enabled", True)
    guard.setdefault("budget_scale_min_orders", 2)
    guard.setdefault("budget_scale_roi_multiplier", "1.25")
    guard.setdefault("budget_scale_min_spend_share", "0.25")
    guard.setdefault("budget_scale_max_raise_pct", "0.20")
    guard.setdefault("budget_scale_sensitivity", "0.20")
    guard.setdefault("budget_scale_cooldown_minutes", 1440)
    guard.setdefault("roas_scale_enabled", True)
    guard.setdefault("roas_scale_roi_multiplier", "1.60")
    guard.setdefault("roas_scale_max_cut_pct", "0.08")
    guard.setdefault("decision_product_max_age_minutes", 15)
    guard.setdefault("decision_recent_max_age_minutes", 10)
    guard.setdefault("attribution_grace_minutes", 20)
    guard.setdefault("data_conflict_protective_pause_enabled", False)
    guard.setdefault("data_conflict_min_spend_cents", 300)
    guard.setdefault("protection_pause_minutes", 15)
    guard.setdefault("forced_sync_cooldown_minutes", 10)
    guard.setdefault("hermes_action_review_enabled", True)
    guard.setdefault("hermes_action_review_cache_minutes", 10)
    guard.setdefault("controlled_test_min_budget_cents", 1000)
    guard.setdefault("controlled_test_max_budget_cents", 5000)
    guard.setdefault("controlled_test_min_allowed_cpa_ratio", "1.5")
    guard.setdefault("controlled_test_max_allowed_cpa_ratio", "3.0")
    guard.setdefault("controlled_test_max_budget_share", "0.75")
    guard.setdefault("controlled_test_failure_budget_shrink_ratio", "0")
    guard.setdefault("controlled_test_fallback_enabled", True)
    guard.setdefault("peak_recovery_enabled", True)
    guard.setdefault("peak_recovery_min_confidence", "0.35")
    guard.setdefault("peak_recovery_min_multiplier", "1.15")
    guard.setdefault("peak_recovery_cooldown_cap_minutes", 20)
    guard.setdefault("peak_recovery_min_pause_minutes", 10)
    guard.setdefault("peak_recovery_max_performance_failures", 2)
    guard.setdefault("controlled_test_observation_minutes", 360)
    guard.setdefault("controlled_test_min_observation_minutes", 180)
    guard.setdefault("controlled_test_max_observation_minutes", 1440)
    guard.setdefault("controlled_test_success_roi_multiplier", "0.80")
    guard.setdefault("controlled_test_post_success_grace_minutes", 1440)
    guard.setdefault("controlled_test_monitor_interval_minutes", 1)
    guard.setdefault("controlled_test_delivery_probe_minutes", 120)
    guard.setdefault("controlled_test_delivery_spend_cents", 50)
    guard.setdefault("controlled_test_performance_min_allowed_cpa_ratio", "1.25")
    guard.setdefault("controlled_test_performance_max_allowed_cpa_ratio", "2.00")
    guard.setdefault("controlled_test_performance_min_budget_cents", 1500)
    guard.setdefault("controlled_test_performance_min_clicks", 12)
    guard.setdefault("controlled_test_performance_min_minutes", 360)
    guard.setdefault("controlled_test_performance_max_minutes", 1440)
    guard.setdefault("controlled_test_rebuild_after_no_delivery", 3)
    guard.setdefault("controlled_test_rebuild_limit_24h", 1)
    # TikTok's campaign budget floor is separate from the incremental test cap.
    guard.setdefault("controlled_test_budget_floor_cents", 5000)
    guard.setdefault("fresh_campaign_controlled_test_enabled", False)
    guard.setdefault("fresh_campaign_controlled_test_window_minutes", 1440)
    guard.setdefault("learning_min_run_minutes", 1440)
    guard.setdefault("learning_target_minutes", 4320)
    guard.setdefault("learning_exit_orders", 20)
    guard.setdefault("learning_emergency_min_spend_cents", 3000)
    guard.setdefault("roas_freeze_minutes", 4320)
    guard.setdefault("window_stop_enabled", False)
    guard.setdefault("delegate_performance_stop_to_creative_guard", True)
    return guard


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


def _normalize_roas_bid(value: Any, *, rounding: str = ROUND_HALF_UP) -> Decimal | None:
    decimal = _to_decimal(value)
    if decimal is None:
        return None
    if decimal <= 0:
        raise ValueError("roas_bid must be greater than zero")
    return decimal.quantize(_ROAS_QUANT, rounding=rounding)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(value)


def _money_to_cents(value: Any) -> int:
    decimal = _to_decimal(value, "0") or Decimal("0")
    cents = (decimal * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def _optional_money_to_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    cents = _money_to_cents(value)
    return cents if cents > 0 else None


def _budget_to_cents(value: Any) -> int:
    raw = _to_int(value, 0)
    if raw <= 0:
        return 0
    return raw


def _campaign_item_group_ids(db: Session, campaign: CatalogCampaign) -> list[str]:
    # Smart Guard decisions use the minimum price and aggregate evidence across
    # the campaign, so a fixed item limit would silently change the decision.
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
            union
            select item_group_id
            from gmvmax_product_creative_metrics_daily
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and item_group_id is not null
            order by item_group_id
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    ).scalars().all()
    return [str(row) for row in rows if row]


def _product_effective_price_basis(
    db: Session,
    *,
    campaign: CatalogCampaign,
    item_group_id: str,
    guard: Mapping[str, Any],
) -> dict[str, Any]:
    configured_prices = guard.get("product_effective_prices")
    if isinstance(configured_prices, Mapping):
        cents = _optional_money_to_cents(configured_prices.get(str(item_group_id)))
        if cents:
            return {"cents": cents, "source": "strategy_config", "item_group_id": item_group_id}
    default_cents = _optional_money_to_cents(guard.get("default_effective_product_price"))
    if default_cents:
        return {"cents": default_cents, "source": "strategy_default", "item_group_id": item_group_id}

    lookback_days = max(1, _to_int(guard.get("order_value_lookback_days"), 90))
    order_row = db.execute(
        text(
            """
            select sum(coalesce(order_count, 0)) as orders,
                   sum(coalesce(gross_revenue_cents, 0)) as gross_revenue_cents
            from gmv_product_order_events
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and store_id=:store_id
              and item_group_id=:item_group_id
              and order_time_hour >= date_sub(utc_timestamp(6), interval :lookback_days day)
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "store_id": campaign.store_id,
            "item_group_id": item_group_id,
            "lookback_days": lookback_days,
        },
    ).mappings().first()
    orders = _to_int((order_row or {}).get("orders"), 0)
    gross_cents = _to_int((order_row or {}).get("gross_revenue_cents"), 0)
    if orders > 0 and gross_cents > 0:
        return {
            "cents": int(Decimal(gross_cents) / Decimal(orders)),
            "source": f"order_aov_{lookback_days}d",
            "item_group_id": item_group_id,
        }

    product_row = db.execute(
        text(
            """
            select effective_price, price, min_price, max_price
            from ttb_products
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and store_id=:store_id
              and product_id=:item_group_id
            order by last_seen_at desc
            limit 1
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "store_id": campaign.store_id,
            "item_group_id": item_group_id,
        },
    ).mappings().first()
    if not product_row:
        return {"cents": None, "source": "missing_product", "item_group_id": item_group_id}
    if bool(guard.get("use_effective_product_price", True)):
        cents = _optional_money_to_cents(product_row.get("effective_price"))
        if cents:
            return {"cents": cents, "source": "ttb_products.effective_price", "item_group_id": item_group_id}
    for key in ("price", "min_price", "max_price"):
        cents = _optional_money_to_cents(product_row.get(key))
        if cents:
            return {"cents": cents, "source": f"ttb_products.{key}", "item_group_id": item_group_id}
    return {"cents": None, "source": "price_missing", "item_group_id": item_group_id}


def _campaign_effective_price_basis(
    db: Session,
    *,
    campaign: CatalogCampaign,
    guard: Mapping[str, Any],
) -> dict[str, Any]:
    bases = [
        _product_effective_price_basis(db, campaign=campaign, item_group_id=item_id, guard=guard)
        for item_id in _campaign_item_group_ids(db, campaign)
    ]
    priced = [basis for basis in bases if basis.get("cents")]
    if not priced:
        return {"cents": None, "source": "missing_campaign_price", "products": bases}
    selected = min(priced, key=lambda item: int(item.get("cents") or 0))
    return {**selected, "products": bases}


def _target_roas_for_guard(campaign: CatalogCampaign, guard: Mapping[str, Any]) -> Decimal:
    configured = _to_decimal(guard.get("target_roas"))
    if configured and configured > 0:
        return configured
    if campaign.roas_bid and campaign.roas_bid > 0:
        return campaign.roas_bid
    fallback = _to_decimal(guard.get("target_roas_fallback"), "1.2") or Decimal("1.2")
    return max(Decimal("0.1"), fallback)


def _dynamic_no_order_spend_cents(
    db: Session,
    *,
    campaign: CatalogCampaign,
    guard: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    min_spend = max(0, _to_int(guard.get("min_spend_cents"), 300))
    fixed = _to_int(guard.get("no_order_spend_cents"), 0)
    price_basis = _campaign_effective_price_basis(db, campaign=campaign, guard=guard)
    product_price_cents = int(price_basis.get("cents") or 0)
    target_roas = _target_roas_for_guard(campaign, guard)
    candidates: list[int] = []
    allowed_cpa_cents = None
    price_cap_cents = None
    budget_cap_cents = None
    if product_price_cents > 0:
        allowed_cpa_cents = int(
            (Decimal(product_price_cents) / target_roas * (_to_decimal(guard.get("no_order_allowed_cpa_multiplier"), "1.0") or Decimal("1.0")))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        price_cap_cents = int(
            (Decimal(product_price_cents) * (_to_decimal(guard.get("no_order_price_multiplier_cap"), "2.5") or Decimal("2.5")))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        candidates.extend([allowed_cpa_cents, price_cap_cents])
    budget = _budget_to_cents(campaign.budget_value)
    if budget > 0:
        budget_cap_cents = int(
            (Decimal(budget) * (_to_decimal(guard.get("no_order_budget_share_cap"), "0.20") or Decimal("0.20")))
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        candidates.append(budget_cap_cents)
    if fixed > 0:
        candidates.append(fixed)
    threshold = min([item for item in candidates if item and item > 0], default=min_spend)
    threshold = max(threshold, min_spend)
    return threshold, {
        "product_price_cents": product_price_cents or None,
        "product_price_source": price_basis.get("source"),
        "item_group_id": price_basis.get("item_group_id"),
        "target_roas": str(target_roas),
        "allowed_cpa_cents": allowed_cpa_cents,
        "price_cap_cents": price_cap_cents,
        "budget_cap_cents": budget_cap_cents,
        "fixed_no_order_spend_cents": fixed or None,
        "no_order_threshold_cents": threshold,
        "price_basis": price_basis,
    }


def _status_is_active(status: str | None) -> bool:
    text_value = str(status or "").upper()
    if "DISABLE" in text_value or "PAUSE" in text_value or "DELETE" in text_value:
        return False
    return "ENABLE" in text_value or "DELIVERY_OK" in text_value or "ACTIVE" in text_value


def _metrics_baseline(metrics: RealtimeMetrics) -> dict[str, int]:
    return {
        "cost_cents": int(metrics.cost_cents or 0),
        "gross_revenue_cents": int(metrics.gross_revenue_cents or 0),
        "orders": int(metrics.orders or 0),
    }


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


def _strategy_due(strategy: GmvStrategyConfig, now: datetime) -> bool:
    state = _smart_guard_state(strategy)
    paused_until = _parse_state_datetime(state.get("paused_until"))
    if paused_until is not None and now >= paused_until:
        return True
    controlled_test = dict(state.get("controlled_test") or {})
    if bool(controlled_test.get("active")):
        review_at = _parse_state_datetime(controlled_test.get("review_at"))
        if review_at is not None and now >= review_at:
            return True
    next_check_at = _parse_state_datetime(state.get("next_check_at"))
    return next_check_at is None or now >= next_check_at


def _window_metrics(metrics: RealtimeMetrics, baseline: Mapping[str, Any] | None) -> tuple[int, int, int, Decimal | None]:
    baseline = baseline or {}
    cost_cents = max(0, int(metrics.cost_cents or 0) - _to_int(baseline.get("cost_cents"), 0))
    gross_revenue_cents = max(
        0,
        int(metrics.gross_revenue_cents or 0) - _to_int(baseline.get("gross_revenue_cents"), 0),
    )
    orders = max(0, int(metrics.orders or 0) - _to_int(baseline.get("orders"), 0))
    roi = None
    if cost_cents > 0:
        roi = (Decimal(gross_revenue_cents) / Decimal(cost_cents)).quantize(_ROI_QUANT)
    return cost_cents, gross_revenue_cents, orders, roi


def _controlled_test_budget_bounds(
    *,
    guard: Mapping[str, Any],
    campaign: CatalogCampaign,
    threshold_context: Mapping[str, Any],
    attempt_count: int,
) -> dict[str, Any]:
    """Build a bounded search space; Hermes selects the amount inside it."""

    configured_min = max(100, _to_int(guard.get("controlled_test_min_budget_cents"), 200))
    configured_max = max(configured_min, _to_int(guard.get("controlled_test_max_budget_cents"), 2000))
    allowed_cpa = max(configured_min, _to_int(threshold_context.get("allowed_cpa_cents"), configured_min))
    min_ratio = _to_decimal(guard.get("controlled_test_min_allowed_cpa_ratio"), "0.50") or Decimal("0.50")
    max_ratio = _to_decimal(guard.get("controlled_test_max_allowed_cpa_ratio"), "1.25") or Decimal("1.25")
    lower = max(configured_min, int(Decimal(allowed_cpa) * min_ratio))
    upper = min(configured_max, max(lower, int(Decimal(allowed_cpa) * max_ratio)))

    current_budget = _budget_to_cents(campaign.budget_value)
    if current_budget > 0:
        max_budget_share = (
            _to_decimal(guard.get("controlled_test_max_budget_share"), "0.35") or Decimal("0.35")
        )
        upper = min(upper, max(lower, int(Decimal(current_budget) * max_budget_share)))
    shrink_ratio = (
        _to_decimal(guard.get("controlled_test_failure_budget_shrink_ratio"), "0.20")
        or Decimal("0.20")
    )
    if attempt_count > 0:
        shrink_factor = Decimal("1") + Decimal(min(attempt_count, 6)) * shrink_ratio
        upper = max(lower, int((Decimal(upper) / shrink_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    return {
        "min_cents": lower,
        "max_cents": max(lower, upper),
        "allowed_cpa_cents": allowed_cpa,
        "attempt_count": max(0, attempt_count),
        "campaign_budget_cents": current_budget,
        "selection_owner": "hermes",
    }


def _fallback_controlled_test_budget(
    *,
    guard: Mapping[str, Any],
    budget_bounds: Mapping[str, Any],
    order_timing: Mapping[str, Any] | None,
) -> int:
    """Choose a meaningful bounded test when Hermes is unavailable or defers."""

    lower = max(1, _to_int(budget_bounds.get("min_cents"), 1))
    upper = max(lower, _to_int(budget_bounds.get("max_cents"), lower))
    timing = dict(order_timing or {})
    confidence = _to_decimal(timing.get("confidence"), "0") or Decimal("0")
    multiplier = _to_decimal(timing.get("delivery_multiplier"), "1") or Decimal("1")
    peak_confidence = (
        _to_decimal(guard.get("peak_recovery_min_confidence"), "0.35") or Decimal("0.35")
    )
    peak_multiplier = (
        _to_decimal(guard.get("peak_recovery_min_multiplier"), "1.15") or Decimal("1.15")
    )
    if confidence >= peak_confidence and multiplier >= peak_multiplier:
        position = min(Decimal("1"), max(Decimal("0.60"), confidence))
    else:
        position = Decimal("0.35")
    return lower + int(
        (Decimal(upper - lower) * position).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _performance_test_plan(
    *,
    guard: Mapping[str, Any],
    threshold_context: Mapping[str, Any],
    campaign: CatalogCampaign,
    probe_spend_cents: int,
    probe_elapsed_minutes: int,
) -> dict[str, int]:
    """Size an evidence test from CPA economics and observed delivery speed."""

    allowed_cpa = max(300, _to_int(threshold_context.get("allowed_cpa_cents"), 300))
    min_ratio = (
        _to_decimal(guard.get("controlled_test_performance_min_allowed_cpa_ratio"), "1.25")
        or Decimal("1.25")
    )
    max_ratio = (
        _to_decimal(guard.get("controlled_test_performance_max_allowed_cpa_ratio"), "2.00")
        or Decimal("2.00")
    )
    min_clicks = max(5, _to_int(guard.get("controlled_test_performance_min_clicks"), 8))
    recent = dict(threshold_context.get("recent_momentum") or {})
    recent_cost = max(0, _to_int(recent.get("cost_cents"), 0))
    recent_clicks = max(0, _to_int(recent.get("clicks"), 0))
    estimated_cpc = int(Decimal(recent_cost) / Decimal(recent_clicks)) if recent_clicks >= 3 else 0

    lower = max(
        max(500, _to_int(guard.get("controlled_test_performance_min_budget_cents"), 800)),
        int(Decimal(allowed_cpa) * min_ratio),
    )
    if estimated_cpc > 0:
        lower = max(lower, estimated_cpc * min_clicks)
    upper = max(lower, int(Decimal(allowed_cpa) * max_ratio))
    campaign_budget = _budget_to_cents(campaign.budget_value)
    if campaign_budget > 0:
        upper = min(upper, max(lower, int(Decimal(campaign_budget) * Decimal("0.50"))))
    budget = max(lower, min(upper, lower))

    min_minutes = max(30, _to_int(guard.get("controlled_test_performance_min_minutes"), 45))
    max_minutes = max(min_minutes, _to_int(guard.get("controlled_test_performance_max_minutes"), 120))
    elapsed = max(1, probe_elapsed_minutes)
    if probe_spend_cents > 0:
        estimated_minutes = int(
            (Decimal(budget) * Decimal(elapsed) / Decimal(probe_spend_cents)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    else:
        estimated_minutes = min_minutes
    observation_minutes = max(min_minutes, min(max_minutes, estimated_minutes))
    minimum_evidence_spend = min(
        budget,
        max(300, int(Decimal(allowed_cpa) * Decimal("0.50")), estimated_cpc * 3),
    )
    return {
        "budget_cents": budget,
        "minimum_evidence_spend_cents": minimum_evidence_spend,
        "estimated_cpc_cents": estimated_cpc,
        "min_clicks": min_clicks,
        "observation_minutes": observation_minutes,
    }


def _learning_phase_active(
    *,
    guard: Mapping[str, Any],
    campaign_age_minutes: int,
    orders: int,
) -> bool:
    """Keep a stable campaign while TikTok is still learning its audience."""

    minimum_run = max(60, _to_int(guard.get("learning_min_run_minutes"), 1440))
    target_run = max(minimum_run, _to_int(guard.get("learning_target_minutes"), 4320))
    exit_orders = max(1, _to_int(guard.get("learning_exit_orders"), 20))
    if campaign_age_minutes < minimum_run:
        return True
    return campaign_age_minutes < target_run and max(0, orders) < exit_orders


def _safe_roi(gross_revenue_cents: int, cost_cents: int) -> Decimal | None:
    if cost_cents <= 0:
        return None
    return (Decimal(max(0, gross_revenue_cents)) / Decimal(cost_cents)).quantize(_ROI_QUANT)


def _product_day_stats(
    db: Session,
    *,
    campaign: CatalogCampaign,
    guard: Mapping[str, Any],
    metrics: RealtimeMetrics,
) -> dict[str, Any]:
    item_group_ids = _campaign_item_group_ids(db, campaign)
    stat_day = _advertiser_today(
        db,
        workspace_id=campaign.workspace_id,
        auth_id=campaign.auth_id,
        advertiser_id=campaign.advertiser_id,
    )
    empty = {
        "item_group_ids": item_group_ids,
        "stat_time_day": stat_day.isoformat(),
        "cost_cents": int(metrics.cost_cents or 0),
        "gross_revenue_cents": int(metrics.gross_revenue_cents or 0),
        "orders": int(metrics.orders or 0),
        "roi": metrics.roi,
        "campaign_count": 1 if metrics.cost_cents or metrics.orders else 0,
        "source": "current_campaign_metrics",
        "source_updated_at": (metrics.fetched_at or _utcnow()).isoformat(),
        "source_age_seconds": 0,
        "current_campaign": {
            "cost_cents": int(metrics.cost_cents or 0),
            "gross_revenue_cents": int(metrics.gross_revenue_cents or 0),
            "orders": int(metrics.orders or 0),
        },
    }
    if not item_group_ids:
        return empty

    stmt = text(
        """
        select
            coalesce(sum(coalesce(cost_cents, 0)), 0) as total_cost_cents,
            coalesce(sum(coalesce(gross_revenue_cents, 0)), 0) as total_gmv_cents,
            coalesce(sum(coalesce(orders, 0)), 0) as total_orders,
            count(distinct campaign_id) as campaign_count,
            coalesce(sum(case when campaign_id=:campaign_id then coalesce(cost_cents, 0) else 0 end), 0)
                as current_cost_cents,
            coalesce(sum(case when campaign_id=:campaign_id then coalesce(gross_revenue_cents, 0) else 0 end), 0)
                as current_gmv_cents,
            coalesce(sum(case when campaign_id=:campaign_id then coalesce(orders, 0) else 0 end), 0)
                as current_orders,
            max(updated_at) as source_updated_at
        from gmvmax_product_creative_metrics_daily
        where workspace_id=:workspace_id
          and auth_id=:auth_id
          and advertiser_id=:advertiser_id
          and store_id=:store_id
          and stat_time_day=:stat_time_day
          and item_group_id in :item_group_ids
        """
    ).bindparams(bindparam("item_group_ids", expanding=True))
    row = db.execute(
        stmt,
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
            "stat_time_day": stat_day,
            "item_group_ids": [str(item) for item in item_group_ids],
        },
    ).mappings().first() or {}
    total_cost = _to_int(row.get("total_cost_cents"), 0)
    total_gmv = _to_int(row.get("total_gmv_cents"), 0)
    total_orders = _to_int(row.get("total_orders"), 0)
    current_cost = _to_int(row.get("current_cost_cents"), 0)
    current_gmv = _to_int(row.get("current_gmv_cents"), 0)
    current_orders = _to_int(row.get("current_orders"), 0)

    # The official campaign report and creative daily table are two observations
    # of the same current-campaign totals. Never MAX individual fields across
    # them: doing so creates a synthetic row and prevents official corrections
    # from lowering a previously overstated value.
    official_usable = bool(metrics.row_count > 0 and metrics.raw is not None)
    if official_usable:
        canonical_current_cost = int(metrics.cost_cents or 0)
        canonical_current_gmv = int(metrics.gross_revenue_cents or 0)
        canonical_current_orders = int(metrics.orders or 0)
        canonical_current_source = "official_campaign_report"
        canonical_updated_at = metrics.fetched_at
    else:
        canonical_current_cost = current_cost
        canonical_current_gmv = current_gmv
        canonical_current_orders = current_orders
        canonical_current_source = "creative_daily_fallback"
        canonical_updated_at = row.get("source_updated_at")

    cost_cents = total_cost - current_cost + canonical_current_cost
    gross_revenue_cents = total_gmv - current_gmv + canonical_current_gmv
    orders = total_orders - current_orders + canonical_current_orders
    source_updated_at = canonical_updated_at
    return {
        "item_group_ids": item_group_ids,
        "stat_time_day": stat_day.isoformat(),
        "cost_cents": max(0, cost_cents),
        "gross_revenue_cents": max(0, gross_revenue_cents),
        "orders": max(0, orders),
        "roi": _safe_roi(gross_revenue_cents, cost_cents),
        "campaign_count": max(_to_int(row.get("campaign_count"), 0), empty["campaign_count"]),
        "source": "product_daily_plus_canonical_current",
        "canonical_current_source": canonical_current_source,
        "source_updated_at": source_updated_at.isoformat() if isinstance(source_updated_at, datetime) else None,
        "source_age_seconds": _source_age_seconds(db, source_updated_at),
        "current_campaign": {
            "cost_cents": canonical_current_cost,
            "gross_revenue_cents": canonical_current_gmv,
            "orders": canonical_current_orders,
            "source": canonical_current_source,
            "creative_daily_observation": {
                "cost_cents": current_cost,
                "gross_revenue_cents": current_gmv,
                "orders": current_orders,
            },
            "official_observation": {
                "cost_cents": int(metrics.cost_cents or 0),
                "gross_revenue_cents": int(metrics.gross_revenue_cents or 0),
                "orders": int(metrics.orders or 0),
                "usable": official_usable,
            },
        },
    }


def _recent_product_failure_stats(
    db: Session,
    *,
    campaign: CatalogCampaign,
    guard: Mapping[str, Any],
    item_group_ids: list[str],
    now: datetime,
) -> dict[str, Any]:
    if not item_group_ids:
        return {"failure_count": 0, "campaign_count": 0, "hard_stop_count": 0, "reset_count": 0}
    lookback_hours = max(1, _to_int(guard.get("product_failure_lookback_hours"), 24))
    cutoff = (now - timedelta(hours=lookback_hours)).replace(tzinfo=None)
    stmt = text(
        """
        select
            count(*) as failure_count,
            count(distinct e.campaign_id) as campaign_count,
            coalesce(sum(case when e.action='RESET_CAMPAIGN' then 1 else 0 end), 0) as reset_count,
            coalesce(sum(case when e.reason like '%hard stop%' then 1 else 0 end), 0) as hard_stop_count,
            coalesce(sum(case when e.reason like '%0 orders%' or e.reason like '%no_order%' then 1 else 0 end), 0)
                as no_order_count,
            coalesce(sum(coalesce(e.cost_cents, 0)), 0) as observed_cost_cents,
            coalesce(sum(coalesce(e.gross_revenue_cents, 0)), 0) as observed_gmv_cents,
            coalesce(sum(coalesce(e.orders, 0)), 0) as observed_orders,
            max(e.created_at) as last_failure_at
        from gmv_campaign_guard_events e
        where e.workspace_id=:workspace_id
          and e.auth_id=:auth_id
          and e.advertiser_id=:advertiser_id
          and e.store_id=:store_id
          and e.result='SUCCESS'
          and e.action in ('PAUSE', 'RESET_CAMPAIGN')
          and e.created_at >= :cutoff
          and (
              e.reason like '%hard stop%'
              or e.reason like '%no_order%'
              or e.reason like '%0 orders%'
              or e.reason like '%window roi%'
              or e.reason like '%roi_below_target%'
              or e.reason like '%daily spend cap%'
              or e.reason like '%pacing cap%'
              or e.reason like '%product risk cap%'
          )
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
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "cutoff": cutoff,
            "item_group_ids": [str(item) for item in item_group_ids],
        },
    ).mappings().first() or {}
    return {
        "failure_count": _to_int(row.get("failure_count"), 0),
        "campaign_count": _to_int(row.get("campaign_count"), 0),
        "reset_count": _to_int(row.get("reset_count"), 0),
        "hard_stop_count": _to_int(row.get("hard_stop_count"), 0),
        "no_order_count": _to_int(row.get("no_order_count"), 0),
        "observed_cost_cents": _to_int(row.get("observed_cost_cents"), 0),
        "observed_gmv_cents": _to_int(row.get("observed_gmv_cents"), 0),
        "observed_orders": _to_int(row.get("observed_orders"), 0),
        "last_failure_at": str(row.get("last_failure_at")) if row.get("last_failure_at") else None,
        "lookback_hours": lookback_hours,
    }


def _product_recent_momentum_stats(
    db: Session,
    *,
    campaign: CatalogCampaign,
    guard: Mapping[str, Any],
    item_group_ids: list[str],
    now: datetime,
) -> dict[str, Any]:
    if not bool(guard.get("recent_momentum_enabled", True)) or not item_group_ids:
        return {"enabled": bool(guard.get("recent_momentum_enabled", True)), "orders": 0}

    window_minutes = max(10, _to_int(guard.get("recent_momentum_window_minutes"), 60))
    now_utc = now.astimezone(timezone.utc) if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    now_naive = now_utc.replace(tzinfo=None)
    cutoff = now_naive - timedelta(minutes=window_minutes)
    since = cutoff - timedelta(minutes=180)
    campaign_rows = db.execute(
        text(
            """
            select distinct campaign_id
            from gmvmax_product_campaign_item_groups
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and item_group_id in :item_group_ids
            union
            select distinct campaign_id
            from gmvmax_product_creative_metrics_daily
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and item_group_id in :item_group_ids
            """
        ).bindparams(bindparam("item_group_ids", expanding=True)),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "item_group_ids": [str(item) for item in item_group_ids],
        },
    ).scalars().all()
    campaign_ids = [str(row) for row in campaign_rows if row]
    if not campaign_ids:
        return {"enabled": True, "orders": 0, "window_minutes": window_minutes}

    rows = db.execute(
        text(
            """
            select m.campaign_id, m.item_group_id, m.creative_id, m.snapshot_at,
                   latest.snapshot_at as latest_complete_snapshot_at,
                   coalesce(m.cost_cents, 0) as cost_cents,
                   coalesce(m.gross_revenue_cents, 0) as gross_revenue_cents,
                   coalesce(m.orders, 0) as orders,
                   coalesce(m.impressions, 0) as impressions,
                   coalesce(m.clicks, 0) as clicks
            from gmv_creative_metrics_10min m
            join gmv_creative_10min_batch_manifests b
              on b.workspace_id=m.workspace_id
             and b.auth_id=m.auth_id
             and b.advertiser_id=m.advertiser_id
             and b.store_id=m.store_id
             and b.campaign_id=m.campaign_id
             and b.stat_time_day=m.stat_time_day
             and b.snapshot_at=m.snapshot_at
             and b.complete=1
            join (
                select workspace_id, auth_id, advertiser_id, store_id,
                       campaign_id, stat_time_day,
                       max(snapshot_at) as snapshot_at
                from gmv_creative_10min_batch_manifests
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and campaign_id in :campaign_ids
                  and complete=1
                  and snapshot_at <= :now
                group by workspace_id, auth_id, advertiser_id, store_id,
                         campaign_id, stat_time_day
            ) latest
              on latest.workspace_id=m.workspace_id
             and latest.auth_id=m.auth_id
             and latest.advertiser_id=m.advertiser_id
             and latest.store_id=m.store_id
             and latest.campaign_id=m.campaign_id
             and latest.stat_time_day=m.stat_time_day
            where m.workspace_id=:workspace_id
              and m.auth_id=:auth_id
              and m.advertiser_id=:advertiser_id
              and m.store_id=:store_id
              and m.campaign_id in :campaign_ids
              and m.item_group_id in :item_group_ids
              and m.snapshot_at >= :since
              and m.snapshot_at <= :now
            order by m.campaign_id, m.item_group_id, m.creative_id, m.snapshot_at
            """
        ).bindparams(
            bindparam("campaign_ids", expanding=True),
            bindparam("item_group_ids", expanding=True),
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_ids": campaign_ids,
            "item_group_ids": [str(item) for item in item_group_ids],
            "since": since,
            "now": now_naive,
        },
    ).mappings().all()
    allowed_item_group_ids = {str(item) for item in item_group_ids}
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        item_group_id = str(row.get("item_group_id") or "")
        # SQL is authoritative; this defensive check also prevents an
        # unexpectedly broad/mock result from contaminating product momentum.
        if item_group_id not in allowed_item_group_ids:
            continue
        key = (
            str(row.get("campaign_id")),
            item_group_id,
            str(row.get("creative_id")),
        )
        grouped.setdefault(key, []).append(row)

    totals = {"cost_cents": 0, "gross_revenue_cents": 0, "orders": 0, "impressions": 0, "clicks": 0}
    active_campaign_totals = {"cost_cents": 0, "gross_revenue_cents": 0, "orders": 0}
    latest_snapshot_at: datetime | None = None
    reliable_group_count = 0
    for (campaign_id, _item_group_id, _creative_id), snapshots in grouped.items():
        if not snapshots:
            continue
        latest = snapshots[-1]
        latest_at = latest.get("snapshot_at")
        if latest_at != latest.get("latest_complete_snapshot_at"):
            # The creative disappeared from the latest complete campaign/day
            # inventory; never treat its older cumulative row as current.
            continue
        if isinstance(latest_at, datetime) and (
            latest_snapshot_at is None or latest_at > latest_snapshot_at
        ):
            latest_snapshot_at = latest_at
        baseline: Mapping[str, Any] | None = None
        for snapshot in snapshots:
            if snapshot.get("snapshot_at") <= cutoff:
                baseline = snapshot
            else:
                break
        if baseline is None:
            # A cumulative snapshot without an earlier baseline is not an
            # increment. Use the first observation so a newly discovered
            # campaign cannot masquerade as fresh 60-minute spend/orders.
            baseline = snapshots[0]
        if latest is not baseline:
            reliable_group_count += 1
        for key in totals:
            delta = _to_int(latest.get(key), 0) - _to_int(baseline.get(key), 0)
            if delta < 0:
                delta = _to_int(latest.get(key), 0)
            totals[key] += delta
            if campaign_id == str(campaign.campaign_id) and key in active_campaign_totals:
                active_campaign_totals[key] += delta

    roi = _safe_roi(totals["gross_revenue_cents"], totals["cost_cents"])
    active_roi = _safe_roi(active_campaign_totals["gross_revenue_cents"], active_campaign_totals["cost_cents"])
    return {
        "enabled": True,
        "window_minutes": window_minutes,
        "campaign_count": len(campaign_ids),
        "cost_cents": totals["cost_cents"],
        "gross_revenue_cents": totals["gross_revenue_cents"],
        "orders": totals["orders"],
        "impressions": totals["impressions"],
        "clicks": totals["clicks"],
        "roi": roi,
        "active_campaign": {
            "campaign_id": str(campaign.campaign_id),
            "cost_cents": active_campaign_totals["cost_cents"],
            "gross_revenue_cents": active_campaign_totals["gross_revenue_cents"],
            "orders": active_campaign_totals["orders"],
            "roi": active_roi,
        },
        "source": "creative_10min_delta",
        "source_updated_at": latest_snapshot_at.isoformat() if latest_snapshot_at else None,
        "source_age_seconds": _source_age_seconds(db, latest_snapshot_at),
        "reliable_group_count": reliable_group_count,
    }


def _recent_momentum_is_healthy(
    *,
    guard: Mapping[str, Any],
    recent_stats: Mapping[str, Any],
    min_roi: Decimal | None,
) -> bool:
    if not bool(guard.get("recent_momentum_enabled", True)):
        return False
    min_orders = max(1, _to_int(guard.get("recent_momentum_min_orders"), 2))
    orders = _to_int(recent_stats.get("orders"), 0)
    gmv_cents = _to_int(recent_stats.get("gross_revenue_cents"), 0)
    cost_cents = _to_int(recent_stats.get("cost_cents"), 0)
    if orders < min_orders or gmv_cents <= 0:
        return False
    if cost_cents <= 0:
        return True
    configured_min_roi = _to_decimal(guard.get("recent_momentum_min_roi"), None)
    if configured_min_roi is not None:
        threshold = configured_min_roi
    elif min_roi is not None:
        multiplier = _to_decimal(guard.get("recent_momentum_roi_multiplier"), "0.80") or Decimal("0.80")
        threshold = (min_roi * multiplier).quantize(_ROI_QUANT)
    else:
        threshold = Decimal("1.0")
    roi = recent_stats.get("roi")
    return isinstance(roi, Decimal) and roi >= threshold


def _decision_consistency_snapshot(
    *,
    guard: Mapping[str, Any],
    metrics: RealtimeMetrics,
    product_stats: Mapping[str, Any],
    recent_stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Assess whether campaign, product and creative evidence can be combined."""

    product_max_age = max(60, _to_int(guard.get("decision_product_max_age_minutes"), 15) * 60)
    recent_max_age = max(60, _to_int(guard.get("decision_recent_max_age_minutes"), 10) * 60)
    product_age = product_stats.get("source_age_seconds")
    recent_age = recent_stats.get("source_age_seconds")
    product_usable = product_age is None or _to_int(product_age, product_max_age + 1) <= product_max_age
    recent_usable = (
        _to_int(recent_stats.get("reliable_group_count"), 0) > 0
        and recent_age is not None
        and _to_int(recent_age, recent_max_age + 1) <= recent_max_age
    )
    conflicts: list[str] = []
    warnings: list[str] = []
    attribution_pending = False
    if not product_usable:
        warnings.append("product_snapshot_stale")
    if not recent_usable:
        warnings.append("recent_momentum_missing_or_stale")

    active_recent = dict(recent_stats.get("active_campaign") or {})
    if recent_usable:
        cost_tolerance = max(100, int(max(0, metrics.cost_cents) * 0.10))
        if _to_int(active_recent.get("cost_cents"), 0) > metrics.cost_cents + cost_tolerance:
            # Creative snapshots commonly arrive before the campaign report.
            # Cost-only lead is a freshness warning, not an attribution conflict.
            warnings.append("recent_cost_ahead_of_campaign_report")
        if _to_int(active_recent.get("orders"), 0) > metrics.orders:
            conflicts.append("recent_orders_ahead_of_campaign_report")
            attribution_pending = True
        if _to_int(active_recent.get("gross_revenue_cents"), 0) > metrics.gross_revenue_cents + 1:
            conflicts.append("recent_gmv_ahead_of_campaign_report")
            attribution_pending = True

    current_product = dict(product_stats.get("current_campaign") or {})
    if product_usable:
        if _to_int(current_product.get("orders"), 0) < metrics.orders:
            conflicts.append("product_orders_behind_campaign_report")
            attribution_pending = True
        if _to_int(current_product.get("gross_revenue_cents"), 0) < metrics.gross_revenue_cents:
            conflicts.append("product_gmv_behind_campaign_report")
            attribution_pending = True

    if conflicts:
        state = "conflict"
        confidence = 0.0
    elif warnings:
        state = "degraded"
        confidence = 0.65 if product_usable else 0.45
    else:
        state = "consistent"
        confidence = 1.0
    return {
        "valid": not conflicts,
        "state": state,
        "confidence": confidence,
        "conflicts": conflicts,
        "warnings": warnings,
        "attribution_pending": attribution_pending,
        "attribution_grace_minutes": max(5, _to_int(guard.get("attribution_grace_minutes"), 20)),
        "product_usable": product_usable,
        "recent_momentum_usable": recent_usable,
        "product_age_seconds": product_age,
        "recent_age_seconds": recent_age,
        "campaign_fetched_at": (metrics.fetched_at or _utcnow()).isoformat(),
        "campaign_request_id": metrics.request_id,
    }


def _performance_is_healthy(
    *,
    metrics: RealtimeMetrics,
    product_stats: Mapping[str, Any],
    min_roi: Decimal | None,
    recent_healthy: bool = False,
) -> bool:
    if recent_healthy:
        return True
    if min_roi is None:
        return False
    product_roi = product_stats.get("roi")
    if isinstance(product_roi, Decimal) and _to_int(product_stats.get("orders"), 0) > 0:
        if product_roi >= min_roi:
            return True
    if metrics.roi is not None and metrics.orders > 0 and metrics.roi >= min_roi:
        return True
    return False


def _dynamic_bad_performance_cap_cents(
    *,
    guard: Mapping[str, Any],
    metrics: RealtimeMetrics,
    product_stats: Mapping[str, Any],
    failure_stats: Mapping[str, Any],
    budget_cents: int,
    min_roi: Decimal | None,
    no_order_spend_cents: int,
    healthy: bool,
) -> tuple[int | None, dict[str, Any]]:
    if healthy or not bool(guard.get("daily_loss_cap_enabled", True)):
        return None, {"daily_loss_cap_enabled": bool(guard.get("daily_loss_cap_enabled", True)), "healthy": healthy}

    configured_daily_cap = _to_int(guard.get("daily_spend_cap_cents"), 0)
    product_cost = _to_int(product_stats.get("cost_cents"), 0)
    product_gmv = _to_int(product_stats.get("gross_revenue_cents"), 0)
    product_orders = _to_int(product_stats.get("orders"), 0)
    failure_count = _to_int(failure_stats.get("failure_count"), 0)
    no_order_floor = max(no_order_spend_cents, _to_int(guard.get("min_spend_cents"), 300))

    if product_orders <= 0:
        multiplier = _to_decimal(guard.get("daily_loss_no_order_multiplier"), "3.0") or Decimal("3.0")
        cap = max(no_order_floor, int(Decimal(no_order_floor) * multiplier))
        if budget_cents > 0:
            budget_share = _to_decimal(guard.get("daily_loss_budget_share"), "0.25") or Decimal("0.25")
            cap = min(cap, max(no_order_floor, int(Decimal(budget_cents) * budget_share)))
        cap_type = "no_order"
    else:
        budget_share = _to_decimal(guard.get("bad_roi_loss_budget_share"), "0.35") or Decimal("0.35")
        budget_cap = int(Decimal(budget_cents) * budget_share) if budget_cents > 0 else product_cost
        if min_roi and min_roi > 0:
            allowed_spend = int((Decimal(product_gmv) / min_roi).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            allowed_spend = 0
        grace = int(
            Decimal(no_order_floor)
            * (_to_decimal(guard.get("bad_roi_loss_grace_cpa_multiplier"), "1.0") or Decimal("1.0"))
        )
        cap = max(no_order_floor, min(max(allowed_spend + grace, no_order_floor), max(budget_cap, no_order_floor)))
        cap_type = "bad_roi"

    if configured_daily_cap > 0:
        cap = min(cap, configured_daily_cap)
    shrink_ratio = _to_decimal(guard.get("daily_loss_repeat_shrink_ratio"), "0.25") or Decimal("0.25")
    if failure_count > 0:
        shrink_factor = Decimal("1") + (Decimal(min(failure_count, 8)) * shrink_ratio)
        cap = max(no_order_floor, int((Decimal(cap) / shrink_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    return cap, {
        "daily_loss_cap_type": cap_type,
        "daily_loss_cap_cents": cap,
        "configured_daily_spend_cap_cents": configured_daily_cap or None,
        "product_day_cost_cents": product_cost,
        "product_day_gmv_cents": product_gmv,
        "product_day_orders": product_orders,
        "product_day_roi": str(product_stats.get("roi")) if product_stats.get("roi") is not None else None,
        "product_failure_count": failure_count,
        "product_failure_campaign_count": _to_int(failure_stats.get("campaign_count"), 0),
        "healthy": healthy,
    }


def _dynamic_cooldown_minutes(
    *,
    guard: Mapping[str, Any],
    reason_key: str,
    base_minutes: int,
    metrics: RealtimeMetrics,
    product_stats: Mapping[str, Any],
    failure_stats: Mapping[str, Any],
    min_roi: Decimal | None,
    no_order_spend_cents: int,
    order_timing: Mapping[str, Any] | None = None,
) -> int:
    base = max(5, int(base_minutes or 0))
    if not bool(guard.get("dynamic_cooldown_enabled", True)):
        return base
    timing = dict(order_timing or {})
    timing_confidence = _to_decimal(timing.get("confidence"), "0") or Decimal("0")
    timing_multiplier = _to_decimal(timing.get("delivery_multiplier"), "1") or Decimal("1")
    if timing_confidence >= Decimal("0.25"):
        timing_factor = Decimal("1") + ((Decimal("1") - timing_multiplier) * timing_confidence)
        base = max(5, int((Decimal(base) * timing_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    failure_count = min(8, _to_int(failure_stats.get("failure_count"), 0))
    failure_step = _to_decimal(guard.get("cooldown_failure_step_ratio"), "0.5") or Decimal("0.5")
    severity_step = _to_decimal(guard.get("cooldown_severity_step_ratio"), "0.25") or Decimal("0.25")
    multiplier = Decimal("1") + Decimal(failure_count) * failure_step
    product_cost = _to_int(product_stats.get("cost_cents"), 0)
    product_orders = _to_int(product_stats.get("orders"), 0)
    if product_orders <= 0 and no_order_spend_cents > 0:
        severity = min(Decimal("6"), Decimal(max(product_cost, metrics.cost_cents)) / Decimal(no_order_spend_cents))
        multiplier += severity * severity_step
        multiplier *= _to_decimal(guard.get("no_order_cooldown_multiplier"), "1.5") or Decimal("1.5")
    elif min_roi is not None:
        roi = product_stats.get("roi") if isinstance(product_stats.get("roi"), Decimal) else metrics.roi
        if roi is not None and roi < min_roi:
            gap = min(Decimal("3"), (min_roi - roi) / max(min_roi, Decimal("0.0001")))
            multiplier += gap * severity_step
    if "hard" in reason_key or "risk_cap" in reason_key:
        multiplier *= _to_decimal(guard.get("hard_stop_cooldown_multiplier"), "2.0") or Decimal("2.0")
    max_minutes = max(base, _to_int(guard.get("max_pause_cooldown_minutes"), 360))
    cooldown = max(
        base,
        min(max_minutes, int((Decimal(base) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
    )
    hard_stop = "hard" in reason_key or "risk_cap" in reason_key or "data_conflict" in reason_key
    peak_confidence = (
        _to_decimal(guard.get("peak_recovery_min_confidence"), "0.35") or Decimal("0.35")
    )
    peak_multiplier = (
        _to_decimal(guard.get("peak_recovery_min_multiplier"), "1.15") or Decimal("1.15")
    )
    if (
        not hard_stop
        and bool(guard.get("peak_recovery_enabled", True))
        and timing_confidence >= peak_confidence
        and timing_multiplier >= peak_multiplier
    ):
        cooldown = min(
            cooldown,
            max(5, _to_int(guard.get("peak_recovery_cooldown_cap_minutes"), 20)),
        )
    return cooldown


def _profitable_scale_adjustment(
    *,
    guard: Mapping[str, Any],
    state: Mapping[str, Any],
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    product_stats: Mapping[str, Any],
    min_roi: Decimal | None,
    now: datetime,
    order_timing: Mapping[str, Any] | None = None,
    allow_roas_adjustment: bool = True,
) -> dict[str, Any] | None:
    if not bool(guard.get("dynamic_budget_enabled", True)) or min_roi is None:
        return None
    budget_cents = _budget_to_cents(campaign.budget_value)
    if budget_cents <= 0:
        return None
    product_orders = _to_int(product_stats.get("orders"), 0)
    product_cost = _to_int(product_stats.get("cost_cents"), 0)
    product_roi = product_stats.get("roi") if isinstance(product_stats.get("roi"), Decimal) else metrics.roi
    min_orders = max(1, _to_int(guard.get("budget_scale_min_orders"), 2))
    if product_orders < min_orders or product_roi is None:
        return None
    roi_multiplier = _to_decimal(guard.get("budget_scale_roi_multiplier"), "1.25") or Decimal("1.25")
    if product_roi < (min_roi * roi_multiplier).quantize(_ROI_QUANT):
        return None
    min_spend_share = _to_decimal(guard.get("budget_scale_min_spend_share"), "0.25") or Decimal("0.25")
    if product_cost < int(Decimal(budget_cents) * min_spend_share):
        return None
    last_adjusted_at = _parse_state_datetime(state.get("last_adjusted_at"))
    cooldown_minutes = max(15, _to_int(guard.get("budget_scale_cooldown_minutes"), 180))
    if last_adjusted_at is not None and now < last_adjusted_at + timedelta(minutes=cooldown_minutes):
        return None
    max_raise = _to_decimal(guard.get("budget_scale_max_raise_pct"), "0.30") or Decimal("0.30")
    sensitivity = _to_decimal(guard.get("budget_scale_sensitivity"), "0.20") or Decimal("0.20")
    roi_lift = max(Decimal("0"), (product_roi / min_roi) - Decimal("1"))
    raise_pct = min(max_raise, max(Decimal("0.05"), roi_lift * sensitivity))
    timing = dict(order_timing or {})
    timing_confidence = _to_decimal(timing.get("confidence"), "0") or Decimal("0")
    timing_multiplier = _to_decimal(timing.get("delivery_multiplier"), "1") or Decimal("1")
    if timing_confidence >= Decimal("0.25"):
        timing_factor = Decimal("1") + ((timing_multiplier - Decimal("1")) * timing_confidence)
        raise_pct = min(max_raise, max(Decimal("0.03"), raise_pct * timing_factor))
    new_budget_cents = int((Decimal(budget_cents) * (Decimal("1") + raise_pct)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    adjustment: dict[str, Any] = {
        "budget_cents": new_budget_cents,
        "budget": float(Decimal(new_budget_cents) / Decimal("100")),
        "raise_pct": str(raise_pct.quantize(Decimal("0.0001"))),
        "current_budget_cents": budget_cents,
        "product_day_roi": str(product_roi),
        "product_day_orders": product_orders,
        "product_day_cost_cents": product_cost,
        "order_timing_multiplier": str(timing_multiplier),
    }
    if (
        allow_roas_adjustment
        and bool(guard.get("roas_scale_enabled", True))
        and campaign.roas_bid
        and campaign.roas_bid > 0
    ):
        roas_multiplier = _to_decimal(guard.get("roas_scale_roi_multiplier"), "1.60") or Decimal("1.60")
        if product_roi >= (campaign.roas_bid * roas_multiplier).quantize(_ROI_QUANT):
            max_cut = _to_decimal(guard.get("roas_scale_max_cut_pct"), "0.08") or Decimal("0.08")
            new_roas = max(
                min_roi,
                (campaign.roas_bid * (Decimal("1") - max_cut)).quantize(
                    _ROAS_QUANT,
                    rounding=ROUND_HALF_UP,
                ),
            )
            new_roas = _normalize_roas_bid(new_roas)
            if new_roas < campaign.roas_bid:
                adjustment["roas_bid"] = float(new_roas)
                adjustment["current_roas_bid"] = float(campaign.roas_bid)
    return adjustment


def _dynamic_monitor_interval_minutes(
    db: Session,
    *,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    guard: Mapping[str, Any],
    order_timing: Mapping[str, Any] | None = None,
) -> int:
    fast = max(1, _to_int(guard.get("fast_monitor_interval_minutes"), 1))
    normal = max(fast, _to_int(guard.get("monitor_interval_minutes"), 3))
    slow = max(normal, _to_int(guard.get("slow_monitor_interval_minutes"), 5))
    if not _status_is_active(campaign.operation_status):
        return slow

    timing = dict(order_timing or {})
    timing_confidence = _to_decimal(timing.get("confidence"), "0") or Decimal("0")
    timing_multiplier = _to_decimal(timing.get("delivery_multiplier"), "1") or Decimal("1")
    if timing_confidence >= Decimal("0.25") and timing_multiplier > Decimal("1"):
        return fast

    cost_cents = max(int(metrics.cost_cents or 0), int(metrics.net_cost_cents or 0))
    if cost_cents <= 0:
        return normal

    budget = _budget_to_cents(campaign.budget_value)
    if budget <= 0:
        return normal

    spend_share = Decimal(cost_cents) / Decimal(budget)
    fast_share = _to_decimal(guard.get("fast_spend_budget_share"), "0.08") or Decimal("0.08")
    slow_share = _to_decimal(guard.get("slow_spend_budget_share"), "0.01") or Decimal("0.01")
    if spend_share >= fast_share:
        return fast
    if spend_share <= slow_share:
        return slow

    progress = _day_progress(
        db,
        workspace_id=campaign.workspace_id,
        auth_id=campaign.auth_id,
        advertiser_id=campaign.advertiser_id,
    )
    pacing_multiplier = _to_decimal(guard.get("pacing_multiplier"), "1.25") or Decimal("1.25")
    expected_cost = max(1, int(Decimal(budget) * progress * pacing_multiplier))
    min_fast_cost = _to_int(guard.get("min_spend_cents"), 300)
    if cost_cents >= min_fast_cost and cost_cents >= expected_cost:
        return fast
    return normal


def _advertiser_timezone_name(db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str) -> str | None:
    row = db.execute(
        text(
            """
            select display_timezone, timezone
            from ttb_advertisers
            where workspace_id=:workspace_id and auth_id=:auth_id and advertiser_id=:advertiser_id
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
    tz_name = (row or {}).get("display_timezone") or (row or {}).get("timezone")
    if not tz_name:
        return None
    candidate = str(tz_name).strip()
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return candidate


def _advertiser_now(db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str) -> datetime:
    tz_name = _advertiser_timezone_name(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    if not tz_name:
        raise RuntimeError(
            "TikTok Business advertiser timezone is unavailable; "
            "time-based automation is blocked until account metadata is synced."
        )
    return datetime.now(ZoneInfo(tz_name))


def _advertiser_today(db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str) -> date:
    return _advertiser_now(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    ).date()


def _as_aware_utc(value: Any) -> datetime | None:
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


def _campaign_start_at_utc(db: Session, campaign: CatalogCampaign) -> datetime | None:
    table_name = (
        "gmvmax_live_campaign_catalog"
        if campaign.promotion_type == "LIVE"
        else "gmvmax_product_campaign_catalog"
    )
    row = db.execute(
        text(
            f"""
            select schedule_start_time_utc, create_time_utc, updated_at, created_at
            from {table_name}
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    ).mappings().first()
    if not row:
        return None
    return (
        _as_aware_utc(row.get("schedule_start_time_utc"))
        or _as_aware_utc(row.get("create_time_utc"))
        or _as_aware_utc(row.get("created_at"))
        or _as_aware_utc(row.get("updated_at"))
    )


def _campaign_created_at_utc(db: Session, campaign: CatalogCampaign) -> datetime | None:
    table_name = (
        "gmvmax_live_campaign_catalog"
        if campaign.promotion_type == "LIVE"
        else "gmvmax_product_campaign_catalog"
    )
    row = db.execute(
        text(
            f"""
            select create_time_utc, created_at
            from {table_name}
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    ).mappings().first()
    if not row:
        return None
    return _as_aware_utc(row.get("create_time_utc")) or _as_aware_utc(row.get("created_at"))


def _campaign_report_date_range(db: Session, campaign: CatalogCampaign) -> tuple[date, date]:
    """Return the advertiser-local current day for realtime guard metrics.

    Daily caps, pacing, and product cards must not use campaign-life cumulative
    report rows. Cumulative spend is useful for historical analysis, but the
    smart guard evaluates the current advertiser day every cycle.
    """

    end_day = _advertiser_today(
        db,
        workspace_id=campaign.workspace_id,
        auth_id=campaign.auth_id,
        advertiser_id=campaign.advertiser_id,
    )
    return end_day, end_day


def _advertiser_day_end_utc_iso(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str:
    now_local = _advertiser_now(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    tomorrow = now_local.date() + timedelta(days=1)
    end_local = datetime.combine(tomorrow, datetime.min.time())
    if now_local.tzinfo is not None:
        end_local = end_local.replace(tzinfo=now_local.tzinfo)
        return end_local.astimezone(timezone.utc).isoformat()
    return end_local.replace(tzinfo=timezone.utc).isoformat()


def _day_progress(db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str) -> Decimal:
    now = _advertiser_now(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
    )
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return max(Decimal("0.02"), min(Decimal("1"), Decimal(seconds) / Decimal(86400)))


def _load_enabled_strategies(db: Session) -> list[GmvStrategyConfig]:
    rows = (
        db.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.enabled.is_(True))
        .order_by(GmvStrategyConfig.updated_at.asc(), GmvStrategyConfig.id.asc())
        .all()
    )
    return [row for row in rows if bool(_guard_config(row).get("enabled", False))]


def _load_catalog_campaign(db: Session, strategy: GmvStrategyConfig) -> CatalogCampaign | None:
    params = {
        "workspace_id": int(strategy.workspace_id),
        "auth_id": int(strategy.auth_id),
        "campaign_id": str(strategy.campaign_id),
    }
    rows = db.execute(
        text(
            """
            select workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                   campaign_name, operation_status, secondary_status,
                   budget_cents, roas_bid, 'PRODUCT' as promotion_type
            from gmvmax_product_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and campaign_id=:campaign_id
            union all
            select workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                   campaign_name, operation_status, secondary_status,
                   budget_cents, roas_bid, 'LIVE' as promotion_type
            from gmvmax_live_campaign_catalog
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and campaign_id=:campaign_id
            """
        ),
        params,
    ).mappings().all()
    if len(rows) != 1:
        return None
    row = rows[0]
    advertiser_id = str(row.get("advertiser_id") or "").strip()
    store_id = str(row.get("store_id") or "").strip()
    if not advertiser_id or not store_id or set(store_id) == {"0"}:
        return None
    return CatalogCampaign(
        workspace_id=int(row["workspace_id"]),
        auth_id=int(row["auth_id"]),
        advertiser_id=advertiser_id,
        store_id=store_id,
        campaign_id=str(row["campaign_id"]),
        campaign_name=row.get("campaign_name"),
        promotion_type=str(row["promotion_type"]),
        operation_status=row.get("operation_status"),
        secondary_status=row.get("secondary_status"),
        budget_value=row.get("budget_cents"),
        roas_bid=_to_decimal(row.get("roas_bid")),
    )


def _reload_catalog_campaign_for_mutation(
    db: Session,
    campaign: CatalogCampaign,
) -> CatalogCampaign:
    """Reload the exact catalog row after both mutation locks are owned."""

    table_name = (
        "gmvmax_live_campaign_catalog"
        if campaign.promotion_type == "LIVE"
        else "gmvmax_product_campaign_catalog"
    )
    row = db.execute(
        text(
            f"""
            select workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                   campaign_name, operation_status, secondary_status,
                   budget_cents, roas_bid
            from {table_name}
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 2
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    ).mappings().all()
    if len(row) != 1:
        raise GmvMaxMutationFenceLost(
            "canonical GMV Max campaign changed while acquiring mutation locks"
        )
    item = row[0]
    return CatalogCampaign(
        workspace_id=int(item["workspace_id"]),
        auth_id=int(item["auth_id"]),
        advertiser_id=str(item["advertiser_id"]),
        store_id=str(item["store_id"]),
        campaign_id=str(item["campaign_id"]),
        campaign_name=item.get("campaign_name"),
        promotion_type=campaign.promotion_type,
        operation_status=item.get("operation_status"),
        secondary_status=item.get("secondary_status"),
        budget_value=item.get("budget_cents"),
        roas_bid=_to_decimal(item.get("roas_bid")),
    )


def _assert_smart_guard_mutation_allowed(
    db: Session,
    campaign: CatalogCampaign,
) -> None:
    """Re-check mutable control state while the account mutation lease is held."""

    strategy_rows = db.execute(
        text(
            """
            select enabled, config_json
            from gmv_strategy_configs
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and campaign_id=:campaign_id
            order by id desc
            limit 2
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "campaign_id": campaign.campaign_id,
        },
    ).mappings().all()
    if len(strategy_rows) != 1 or not bool(strategy_rows[0].get("enabled")):
        raise GmvMaxMutationFenceLost(
            "GMV Max strategy was disabled while Smart Guard was evaluating"
        )
    config = strategy_rows[0].get("config_json") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}
    creation_quarantine = (
        config.get("creation_quarantine") if isinstance(config, Mapping) else None
    )
    if isinstance(creation_quarantine, Mapping) and bool(
        creation_quarantine.get("enabled")
    ):
        raise GmvMaxMutationFenceLost(
            "GMV Max campaign creation is quarantined"
        )
    unfinished_create = db.execute(
        text(
            """
            select state
            from gmvmax_campaign_create_intents
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and state<>'SUCCEEDED'
              and (
                    campaign_id=:campaign_id
                 or replacement_campaign_id=:campaign_id
              )
            order by id desc
            limit 1
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    ).mappings().first()
    if unfinished_create:
        raise GmvMaxMutationFenceLost(
            "GMV Max campaign creation has not finalized"
        )
    if is_manual_pause_override_active(
        db,
        workspace_id=campaign.workspace_id,
        auth_id=campaign.auth_id,
        advertiser_id=campaign.advertiser_id,
        store_id=campaign.store_id,
        campaign_id=campaign.campaign_id,
    ):
        raise GmvMaxMutationFenceLost(
            "GMV Max campaign has an active manual pause override"
        )


def _merge_report_entry(entry: Any) -> dict[str, Any]:
    if entry is None:
        return {}
    if isinstance(entry, Mapping):
        return {**dict(entry.get("metrics") or {}), **dict(entry.get("dimensions") or {})}
    return {
        **dict(getattr(entry, "metrics", {}) or {}),
        **dict(getattr(entry, "dimensions", {}) or {}),
    }


async def _fetch_today_metrics(db: Session, campaign: CatalogCampaign) -> RealtimeMetrics:
    start_day, end_day = _campaign_report_date_range(db, campaign)
    if start_day != end_day:
        raise TTBBusinessError(
            "Smart Guard requires one advertiser-local report day",
            code="GMVMAX_SMART_GUARD_REPORT_SCOPE_INVALID",
            payload={
                "campaign_id": campaign.campaign_id,
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
            },
        )

    campaign_id = str(campaign.campaign_id)
    expected_day = start_day.isoformat()
    promotion_type = (
        "LIVE" if str(campaign.promotion_type or "").upper() == "LIVE" else "PRODUCT"
    )
    request = GMVMaxReportGetRequest(
        advertiser_id=str(campaign.advertiser_id),
        store_ids=[str(campaign.store_id)],
        start_date=expected_day,
        end_date=expected_day,
        metrics=list(GMVMAX_METRICS_BY_LEVEL[GMVMaxMetricsLevel.CAMPAIGN.value]),
        dimensions=list(
            GMVMAX_DIMENSIONS_BY_LEVEL[GMVMaxMetricsLevel.CAMPAIGN.value]
        ),
        gmv_max_promotion_types=[promotion_type],
        campaign_ids=[campaign_id],
        filtering=GMVMaxReportFiltering(
            gmv_max_promotion_types=[promotion_type],
            campaign_ids=[campaign_id],
        ),
        enable_total_metrics=False,
        page=1,
        page_size=OFFICIAL_REPORT_PAGE_SIZE,
    )

    client = build_ttb_gmvmax_client(db, auth_id=int(campaign.auth_id))
    entries: list[Any] = []
    request_ids: list[str] = []
    pagination_state = ReportPaginationState(require_dimensions=True)
    pages_fetched = 0
    try:
        for page in range(1, DEFAULT_NUMBERED_PAGE_LIMIT + 1):
            request.page = page
            response = await client.gmv_max_report_get(
                request,
                inject_promotion_types=True,
            )
            pages_fetched += 1
            request_id = str(getattr(response, "request_id", None) or "").strip()
            if request_id:
                request_ids.append(request_id)

            data = getattr(response, "data", None)
            page_entries = list(getattr(data, "list", None) or [])
            for entry in page_entries:
                raw_dimensions = (
                    entry.get("dimensions")
                    if isinstance(entry, Mapping)
                    else getattr(entry, "dimensions", None)
                )
                if not isinstance(raw_dimensions, Mapping):
                    raise TTBBusinessError(
                        "Smart Guard report row is missing official dimensions",
                        code="GMVMAX_SMART_GUARD_REPORT_SCOPE_INVALID",
                        payload={
                            "campaign_id": campaign_id,
                            "page": page,
                            "dimensions": raw_dimensions,
                        },
                    )
                returned_campaign_id = str(
                    raw_dimensions.get("campaign_id") or ""
                ).strip()
                returned_day_raw = raw_dimensions.get("stat_time_day")
                returned_day = _canonical_report_day(returned_day_raw) or ""
                if (
                    returned_campaign_id != campaign_id
                    or returned_day != expected_day
                ):
                    raise TTBBusinessError(
                        "Smart Guard report row escaped its exact campaign/day scope",
                        code="GMVMAX_SMART_GUARD_REPORT_SCOPE_INVALID",
                        payload={
                            "campaign_id": campaign_id,
                            "report_date": expected_day,
                            "returned_campaign_id": returned_campaign_id or None,
                            "returned_stat_time_day": returned_day or None,
                            "returned_stat_time_day_raw": returned_day_raw,
                            "page": page,
                        },
                    )
            entries.extend(page_entries)
            try:
                has_more = report_page_has_more(
                    data,
                    current_page=page,
                    rows=page_entries,
                    state=pagination_state,
                )
            except NumberedPaginationError as exc:
                raise TTBBusinessError(
                    "Smart Guard could not prove the official report snapshot complete",
                    code="GMVMAX_SMART_GUARD_REPORT_INCOMPLETE",
                    payload={
                        "campaign_id": campaign_id,
                        "report_date": expected_day,
                        "page": page,
                        "rows_seen": len(entries),
                    },
                ) from exc
            if not has_more:
                break
        else:
            raise TTBBusinessError(
                "Smart Guard report exceeded the pagination safety limit",
                code="GMVMAX_SMART_GUARD_REPORT_INCOMPLETE",
                payload={
                    "campaign_id": campaign_id,
                    "report_date": expected_day,
                    "max_pages": DEFAULT_NUMBERED_PAGE_LIMIT,
                },
            )
    finally:
        await client.aclose()

    payloads = [_merge_report_entry(entry) for entry in entries]
    if len(payloads) > 1:
        # The request dimension is exactly campaign_id × stat_time_day.  More
        # than one row is therefore never an additive partition; accepting it
        # would double-count a repeated official dimension.
        raise TTBBusinessError(
            "Smart Guard report returned duplicate campaign/day rows",
            code="GMVMAX_SMART_GUARD_REPORT_INCOMPLETE",
            payload={
                "campaign_id": campaign_id,
                "report_date": expected_day,
                "row_count": len(payloads),
            },
        )
    cost_cents = sum(_money_to_cents(payload.get("cost")) for payload in payloads)
    # Missing net_cost is not equivalent to total cost; keep it as an
    # independent net-spend fact instead of synthesizing a fallback.
    net_cost_cents = sum(_money_to_cents(payload.get("net_cost")) for payload in payloads)
    gross_cents = sum(_money_to_cents(payload.get("gross_revenue")) for payload in payloads)
    orders = sum(_to_int(payload.get("orders"), 0) for payload in payloads)
    reported_row_rois = [
        value
        for value in (_to_decimal(payload.get("roi")) for payload in payloads)
        if value is not None
    ]
    calculated_total_cost_roi = (
        (Decimal(gross_cents) / Decimal(cost_cents)).quantize(_ROI_QUANT)
        if cost_cents > 0
        else None
    )
    official_single_row_roi = (
        _to_decimal(payloads[0].get("roi")) if len(payloads) == 1 else None
    )
    # TikTok's ROI contract uses total ad cost, not net cost. Preserve its
    # reported value for a single official row; when the API returns multiple
    # rows, aggregate the additive gross/cost fields and derive one ratio.
    if official_single_row_roi is not None:
        roi = official_single_row_roi.quantize(_ROI_QUANT)
        roi_source = "official_single_row"
    else:
        roi = calculated_total_cost_roi
        roi_source = "aggregate_gross_over_total_cost" if roi is not None else "unavailable"
    raw_payload: dict[str, Any] = {
        "report_start_date": start_day.isoformat(),
        "report_end_date": end_day.isoformat(),
        "request_id": request_ids[0] if request_ids else None,
        "request_ids": request_ids,
        "pages_fetched": pages_fetched,
        "pagination_complete": True,
        "row_count": len(payloads),
        "fetched_at": _utcnow().isoformat(),
        "rows": payloads,
        "roi_audit": {
            "selected_source": roi_source,
            "selected_roi": str(roi) if roi is not None else None,
            "official_single_row_roi": (
                str(official_single_row_roi.quantize(_ROI_QUANT))
                if official_single_row_roi is not None
                else None
            ),
            "reported_row_rois": [
                str(value.quantize(_ROI_QUANT)) for value in reported_row_rois
            ],
            "calculated_gross_over_total_cost": (
                str(calculated_total_cost_roi)
                if calculated_total_cost_roi is not None
                else None
            ),
            "official_minus_calculated": (
                str((roi - calculated_total_cost_roi).quantize(_ROI_QUANT))
                if official_single_row_roi is not None
                and calculated_total_cost_roi is not None
                else None
            ),
            "total_cost_cents": cost_cents,
            "net_cost_cents": net_cost_cents,
        },
    }
    if len(payloads) == 1:
        raw_payload.update(payloads[0])
    return RealtimeMetrics(
        cost_cents=cost_cents,
        net_cost_cents=net_cost_cents,
        gross_revenue_cents=gross_cents,
        orders=orders,
        roi=roi,
        raw=raw_payload,
        row_count=len(payloads),
        request_id=request_ids[0] if request_ids else None,
        fetched_at=_utcnow(),
    )


def _assess_realtime_metrics_quality(
    db: Session,
    *,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
) -> dict[str, Any]:
    start_day, end_day = _campaign_report_date_range(db, campaign)
    quality: dict[str, Any] = {
        "valid": True,
        "state": "fresh",
        "reason": "fresh_tiktok_campaign_report",
        "source": "tiktok_gmv_max_report_get",
        "request_id": metrics.request_id,
        "row_count": int(metrics.row_count or 0),
        "report_start_date": start_day.isoformat(),
        "report_end_date": end_day.isoformat(),
        "fetched_at": (metrics.fetched_at or _utcnow()).isoformat(),
        "confirmation_count": 1,
    }
    if metrics.row_count <= 0:
        quality.update(
            valid=False,
            state="missing",
            reason="empty_tiktok_campaign_report",
            confirmation_count=0,
        )
        return quality

    previous = db.execute(
        text(
            """
            select report_start_date, report_end_date, latest_cost_cents,
                   latest_gross_revenue_cents, latest_orders, last_report_at
            from gmv_campaign_realtime_state
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            limit 1
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    ).mappings().first()
    if not previous or previous.get("report_end_date") != end_day:
        return quality

    regressed_fields = []
    for field, current_value, previous_key in (
        ("cost_cents", metrics.cost_cents, "latest_cost_cents"),
        ("gross_revenue_cents", metrics.gross_revenue_cents, "latest_gross_revenue_cents"),
        ("orders", metrics.orders, "latest_orders"),
    ):
        if int(current_value or 0) < int(previous.get(previous_key) or 0):
            regressed_fields.append(field)
    if not regressed_fields:
        return quality

    previous_cost_cents = int(previous.get("latest_cost_cents") or 0)
    current_cost_cents = int(metrics.cost_cents or 0)
    cost_correction_cents = max(0, previous_cost_cents - current_cost_cents)
    cost_correction_tolerance = max(100, int(previous_cost_cents * 0.02))
    if (
        regressed_fields == ["cost_cents"]
        and cost_correction_cents <= cost_correction_tolerance
    ):
        quality.update(
            valid=True,
            state="corrected",
            reason="minor_cost_correction_accepted",
            regressed_fields=regressed_fields,
            correction_cents=cost_correction_cents,
            correction_tolerance_cents=cost_correction_tolerance,
            current={
                "cost_cents": current_cost_cents,
                "gross_revenue_cents": int(metrics.gross_revenue_cents or 0),
                "orders": int(metrics.orders or 0),
            },
            previous={
                "cost_cents": previous_cost_cents,
                "gross_revenue_cents": int(previous.get("latest_gross_revenue_cents") or 0),
                "orders": int(previous.get("latest_orders") or 0),
                "last_report_at": str(previous.get("last_report_at") or ""),
            },
        )
        return quality

    fingerprint = ":".join(
        str(value)
        for value in (
            metrics.cost_cents,
            metrics.gross_revenue_cents,
            metrics.orders,
        )
    )
    state = _smart_guard_state(strategy)
    previous_quality = dict((state.get("last_decision") or {}).get("data_quality") or {})
    previous_fingerprint = str(previous_quality.get("fingerprint") or "")
    previous_candidate = dict(previous_quality.get("current") or {})
    if not previous_candidate and previous_fingerprint:
        fingerprint_parts = previous_fingerprint.split(":")
        if len(fingerprint_parts) == 3:
            previous_candidate = {
                "cost_cents": _to_int(fingerprint_parts[0], 0),
                "gross_revenue_cents": _to_int(fingerprint_parts[1], 0),
                "orders": _to_int(fingerprint_parts[2], 0),
            }
    current_candidate = {
        "cost_cents": int(metrics.cost_cents or 0),
        "gross_revenue_cents": int(metrics.gross_revenue_cents or 0),
        "orders": int(metrics.orders or 0),
    }
    same_report_window = (
        str(previous_quality.get("report_start_date") or "") == start_day.isoformat()
        and str(previous_quality.get("report_end_date") or "") == end_day.isoformat()
    )
    monotonic_correction = bool(previous_candidate) and all(
        current_candidate[key] >= _to_int(previous_candidate.get(key), 0)
        for key in current_candidate
    )
    previous_was_candidate = str(previous_quality.get("reason") or "") in {
        "tiktok_counter_regression_pending_confirmation",
        "confirmed_tiktok_counter_correction",
    }
    same_correction_stream = (
        previous_fingerprint == fingerprint
        or (same_report_window and previous_was_candidate and monotonic_correction)
    )
    confirmation_count = (
        _to_int(previous_quality.get("confirmation_count"), 0) + 1
        if same_correction_stream
        else 1
    )
    confirmed = confirmation_count >= 2
    quality.update(
        valid=confirmed,
        state="corrected" if confirmed else "regressed",
        reason=(
            "confirmed_tiktok_counter_correction"
            if confirmed
            else "tiktok_counter_regression_pending_confirmation"
        ),
        regressed_fields=regressed_fields,
        fingerprint=fingerprint,
        current=current_candidate,
        monotonic_correction=monotonic_correction,
        confirmation_count=confirmation_count,
        previous={
            "cost_cents": int(previous.get("latest_cost_cents") or 0),
            "gross_revenue_cents": int(previous.get("latest_gross_revenue_cents") or 0),
            "orders": int(previous.get("latest_orders") or 0),
            "last_report_at": str(previous.get("last_report_at") or ""),
        },
    )
    return quality


def _upsert_realtime_state(
    db: Session,
    *,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    now: datetime,
    guard_status: str,
    last_action: str | None,
    reason: str | None,
    paused_until: str | None,
    ) -> None:
    start_day, end_day = _campaign_report_date_range(db, campaign)
    db.execute(
        text(
            """
            insert into gmv_campaign_realtime_state (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                campaign_name, promotion_type, operation_status, secondary_status,
                strategy_id, daily_budget_cents, latest_cost_cents, latest_net_cost_cents,
                latest_gross_revenue_cents, latest_orders, latest_roi,
                report_start_date, report_end_date, source, raw_metrics_json,
                guard_status, last_action, last_reason, paused_until, last_report_at,
                last_checked_at, config_json, runtime_json, state_version, created_at, updated_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                :campaign_name, :promotion_type, :operation_status, :secondary_status,
                :strategy_id, :daily_budget_cents, :latest_cost_cents, :latest_net_cost_cents,
                :latest_gross_revenue_cents, :latest_orders, :latest_roi,
                :report_start_date, :report_end_date, :source, :raw_metrics_json,
                :guard_status, :last_action, :last_reason, :paused_until, :last_report_at,
                :last_checked_at, :config_json, :runtime_json, 1, :created_at, :updated_at
            )
            on duplicate key update
                campaign_name=values(campaign_name),
                promotion_type=values(promotion_type),
                operation_status=values(operation_status),
                secondary_status=values(secondary_status),
                strategy_id=values(strategy_id),
                daily_budget_cents=values(daily_budget_cents),
                latest_cost_cents=values(latest_cost_cents),
                latest_net_cost_cents=values(latest_net_cost_cents),
                latest_gross_revenue_cents=values(latest_gross_revenue_cents),
                latest_orders=values(latest_orders),
                latest_roi=values(latest_roi),
                report_start_date=values(report_start_date),
                report_end_date=values(report_end_date),
                source=values(source),
                raw_metrics_json=values(raw_metrics_json),
                guard_status=values(guard_status),
                last_action=values(last_action),
                last_reason=values(last_reason),
                paused_until=values(paused_until),
                last_report_at=values(last_report_at),
                last_checked_at=values(last_checked_at),
                config_json=values(config_json),
                runtime_json=values(runtime_json),
                state_version=state_version+1,
                updated_at=values(updated_at)
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
            "campaign_name": campaign.campaign_name,
            "promotion_type": campaign.promotion_type,
            "operation_status": campaign.operation_status,
            "secondary_status": campaign.secondary_status,
            "strategy_id": int(strategy.id),
            "daily_budget_cents": _budget_to_cents(campaign.budget_value),
            "latest_cost_cents": metrics.cost_cents,
            "latest_net_cost_cents": metrics.net_cost_cents,
            "latest_gross_revenue_cents": metrics.gross_revenue_cents,
            "latest_orders": metrics.orders,
            "latest_roi": str(metrics.roi) if metrics.roi is not None else None,
            "report_start_date": start_day,
            "report_end_date": end_day,
            "source": "tiktok_report_campaign_window",
            "raw_metrics_json": _json_dumps(metrics.raw),
            "guard_status": guard_status,
            "last_action": last_action,
            "last_reason": reason,
            "paused_until": _utc_naive(paused_until),
            "last_report_at": now.replace(tzinfo=None),
            "last_checked_at": now.replace(tzinfo=None),
            "config_json": _json_dumps(_guard_config(strategy)),
            "runtime_json": _json_dumps(_runtime_state(strategy)),
            "created_at": now.replace(tzinfo=None),
            "updated_at": now.replace(tzinfo=None),
        },
    )


def _stable_event_payload(value: Any) -> Any:
    """Remove observation timestamps while retaining material conflict state."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    if isinstance(value, Mapping):
        return {
            str(key): _stable_event_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in {"fetched_at", "observed_at", "request_id"}
            and not str(key).endswith("_at")
            and not str(key).endswith("_age_seconds")
        }
    if isinstance(value, (list, tuple)):
        return [_stable_event_payload(item) for item in value]
    return value


def _guard_event_signature(
    *,
    cost_cents: Any,
    gross_revenue_cents: Any,
    orders: Any,
    roi: Any,
    request_json: Any,
    response_json: Any,
    error_message: Any,
) -> str:
    material = {
        "cost_cents": _to_int(cost_cents, 0),
        "gross_revenue_cents": _to_int(gross_revenue_cents, 0),
        "orders": _to_int(orders, 0),
        "roi": str(roi) if roi is not None else None,
        "request": _stable_event_payload(request_json or {}),
        "response": _stable_event_payload(response_json or {}),
        "error_message": str(error_message) if error_message else None,
    }
    return hashlib.sha256(_json_dumps(material).encode("utf-8")).hexdigest()


def _insert_event(
    db: Session,
    *,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    action: str,
    reason: str,
    result: str,
    request_json: dict[str, Any] | None = None,
    response_json: dict[str, Any] | None = None,
    error_message: str | None = None,
    write_learning_sample: bool = True,
) -> bool:
    request_payload = dict(request_json or {})
    response_payload = dict(response_json or {})
    created_at = _utcnow().replace(tzinfo=None)
    # A HOLD/SKIPPED conflict is re-evaluated every minute, but an unchanged
    # observation only needs a ten-minute heartbeat in the append-only event
    # stream. Mutation successes/failures are never throttled.
    if str(action).upper() == "HOLD" and str(result).upper() == "SKIPPED":
        previous = db.execute(
            text(
                """
                select cost_cents, gross_revenue_cents, orders, roi,
                       request_json, response_json, error_message, created_at
                from gmv_campaign_guard_events
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and campaign_id=:campaign_id
                  and event_type='SMART_GUARD'
                  and action=:action
                  and reason=:reason
                  and result=:result
                  and created_at>=:heartbeat_cutoff
                order by created_at desc, id desc
                limit 1
                """
            ),
            {
                "workspace_id": campaign.workspace_id,
                "auth_id": campaign.auth_id,
                "advertiser_id": campaign.advertiser_id,
                "store_id": campaign.store_id,
                "campaign_id": campaign.campaign_id,
                "action": action,
                "reason": reason,
                "result": result,
                "heartbeat_cutoff": created_at - timedelta(minutes=10),
            },
        ).mappings().first()
        if previous is not None:
            current_signature = _guard_event_signature(
                cost_cents=metrics.cost_cents,
                gross_revenue_cents=metrics.gross_revenue_cents,
                orders=metrics.orders,
                roi=metrics.roi,
                request_json=request_payload,
                response_json=response_payload,
                error_message=error_message,
            )
            previous_signature = _guard_event_signature(
                cost_cents=previous.get("cost_cents"),
                gross_revenue_cents=previous.get("gross_revenue_cents"),
                orders=previous.get("orders"),
                roi=previous.get("roi"),
                request_json=previous.get("request_json"),
                response_json=previous.get("response_json"),
                error_message=previous.get("error_message"),
            )
            if current_signature == previous_signature:
                return False
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
                :strategy_id, :event_type, :action, :reason, :result,
                :cost_cents, :gross_revenue_cents, :orders, :roi,
                :request_json, :response_json, :error_message, :created_at
            )
            """
        ),
        {
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
            "strategy_id": int(strategy.id),
            "event_type": "SMART_GUARD",
            "action": action,
            "reason": reason,
            "result": result,
            "cost_cents": metrics.cost_cents,
            "gross_revenue_cents": metrics.gross_revenue_cents,
            "orders": metrics.orders,
            "roi": str(metrics.roi) if metrics.roi is not None else None,
            "request_json": _json_dumps(request_payload),
            "response_json": _json_dumps(response_payload),
            "error_message": error_message,
            "created_at": created_at,
        },
    )
    if not write_learning_sample:
        return True
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
                    :item_group_id, null, 'SMART_GUARD', :action, :reason, :result,
                    :cost_cents, :gross_revenue_cents, :orders, :roi,
                    :feature_json, :label_json, 'gmv_campaign_guard_events', :observed_at, :created_at
                )
                """
            ),
            {
                "workspace_id": campaign.workspace_id,
                "auth_id": campaign.auth_id,
                "advertiser_id": campaign.advertiser_id,
                "store_id": campaign.store_id,
                "campaign_id": campaign.campaign_id,
                "item_group_id": request_payload.get("threshold_context", {}).get("item_group_id")
                if isinstance(request_payload.get("threshold_context"), Mapping)
                else None,
                "action": action,
                "reason": reason,
                "result": result,
                "cost_cents": metrics.cost_cents,
                "gross_revenue_cents": metrics.gross_revenue_cents,
                "orders": metrics.orders,
                "roi": str(metrics.roi) if metrics.roi is not None else None,
                "feature_json": _json_dumps(
                    {
                        "request": request_payload,
                        "campaign": {
                            "operation_status": campaign.operation_status,
                            "secondary_status": campaign.secondary_status,
                            "budget_cents": campaign.budget_value,
                            "roas_bid": str(campaign.roas_bid) if campaign.roas_bid is not None else None,
                        },
                        "metrics": metrics.raw or {},
                    }
                ),
                "label_json": _json_dumps({"response": response_payload, "error": error_message}),
                "observed_at": _utcnow().replace(tzinfo=None),
                "created_at": _utcnow().replace(tzinfo=None),
            },
        )
    except Exception:
        logger.warning("failed to write Hermes smart learning sample", exc_info=True)
    return True


def _insert_smart_decision_sample(
    db: Session,
    *,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    decision: Mapping[str, Any],
    monitor_interval: int,
) -> None:
    threshold_context = decision.get("threshold_context")
    if not isinstance(threshold_context, Mapping):
        threshold_context = {}
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
                    :item_group_id, null, 'SMART_DECISION', :action, :reason, 'OBSERVED',
                    :cost_cents, :gross_revenue_cents, :orders, :roi,
                    :feature_json, :label_json, 'gmvmax_smart_guard_cycle', :observed_at, :created_at
                )
                """
            ),
            {
                "workspace_id": campaign.workspace_id,
                "auth_id": campaign.auth_id,
                "advertiser_id": campaign.advertiser_id,
                "store_id": campaign.store_id,
                "campaign_id": campaign.campaign_id,
                "item_group_id": threshold_context.get("item_group_id"),
                "action": str(decision.get("action") or "HOLD"),
                "reason": str(decision.get("reason") or ""),
                "cost_cents": metrics.cost_cents,
                "gross_revenue_cents": metrics.gross_revenue_cents,
                "orders": metrics.orders,
                "roi": str(metrics.roi) if metrics.roi is not None else None,
                "feature_json": _json_dumps(
                    {
                        "threshold_context": dict(threshold_context),
                        "monitor_interval_minutes": monitor_interval,
                        "operation_status": campaign.operation_status,
                        "secondary_status": campaign.secondary_status,
                        "budget_cents": campaign.budget_value,
                        "roas_bid": str(campaign.roas_bid) if campaign.roas_bid is not None else None,
                        "raw_metrics": metrics.raw or {},
                    }
                ),
                "label_json": _json_dumps({"paused_until": decision.get("paused_until")}),
                "observed_at": _utcnow().replace(tzinfo=None),
                "created_at": _utcnow().replace(tzinfo=None),
            },
        )
    except Exception:
        logger.warning("failed to write Hermes smart decision sample", exc_info=True)


def _update_strategy_state(
    strategy: GmvStrategyConfig,
    *,
    now: datetime,
    decision: dict[str, Any],
    paused_until: str | None,
    monitor_interval_minutes: int | None = None,
) -> None:
    state = _smart_guard_state(strategy)
    interval = monitor_interval_minutes
    if interval is None:
        interval = _to_int(decision.get("monitor_interval_minutes"), 3)
    interval = max(1, interval)
    next_check_at = now + timedelta(minutes=interval)
    stored_decision = dict(decision)
    stored_decision.setdefault("decided_at", now.isoformat())
    previous_decision = dict(state.get("last_decision") or {})
    current_pause = _review_datetime(paused_until)
    previous_pause = _review_datetime(state.get("paused_until"))
    same_cooldown_window = (
        current_pause is not None
        and previous_pause is not None
        and abs((current_pause - previous_pause).total_seconds()) <= 5
    )
    if same_cooldown_window and not stored_decision.get("hermes_review"):
        for key in ("hermes_review", "decision_phase", "approved_pause_minutes"):
            if previous_decision.get(key) is not None:
                stored_decision[key] = previous_decision.get(key)
    state.update(
        {
            "last_checked_at": now.isoformat(),
            "monitor_interval_minutes": interval,
            "next_check_at": next_check_at.isoformat(),
            "last_decision": stored_decision,
            "paused_until": paused_until,
        }
    )
    action = str(decision.get("action") or "").upper()
    if action == "ADJUST":
        state["last_adjusted_at"] = now.isoformat()
        if isinstance(decision.get("adjustment"), Mapping):
            state["last_adjustment"] = dict(decision.get("adjustment") or {})
    if action == "PAUSE":
        state["paused_at"] = now.isoformat()
        state["pause_owner"] = "smart_guard"
    elif action == "START" or paused_until is None:
        state.pop("paused_at", None)
        state.pop("pause_owner", None)
    if decision.get("forced_sync_at"):
        state["last_forced_sync_at"] = decision.get("forced_sync_at")
        state["last_forced_sync_task_id"] = decision.get("forced_sync_task_id")
    controlled_test_update = decision.get("controlled_test_update")
    if isinstance(controlled_test_update, Mapping):
        existing_test = dict(state.get("controlled_test") or {})
        if bool(controlled_test_update.get("clear")):
            state.pop("controlled_test", None)
        else:
            state["controlled_test"] = {**existing_test, **dict(controlled_test_update)}
    if decision.get("post_test_grace_until") is not None:
        state["post_test_grace_until"] = decision.get("post_test_grace_until")
    baseline = decision.get("next_baseline")
    if isinstance(baseline, Mapping):
        state["baseline"] = {
            "cost_cents": _to_int(baseline.get("cost_cents"), 0),
            "gross_revenue_cents": _to_int(baseline.get("gross_revenue_cents"), 0),
            "orders": _to_int(baseline.get("orders"), 0),
        }
    _set_smart_guard_state(strategy, state)


def _effective_paused_until(*, active: bool, paused_until: Any) -> Any:
    return None if active else paused_until


def _decide(
    db: Session,
    *,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    now: datetime,
) -> dict[str, Any]:
    guard = _guard_config(strategy)
    state = _smart_guard_state(strategy)
    active = _status_is_active(campaign.operation_status)
    delegate_performance_stop = bool(
        guard.get("delegate_performance_stop_to_creative_guard", True)
    ) and campaign.promotion_type == "PRODUCT"
    try:
        advertiser_timezone = _advertiser_timezone_name(
            db,
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
        )
        if not advertiser_timezone:
            raise RuntimeError("TikTok Business advertiser timezone is unavailable.")
        order_timing = current_order_timing_signal(
            db,
            workspace_id=campaign.workspace_id,
            auth_id=campaign.auth_id,
            advertiser_id=campaign.advertiser_id,
            store_id=campaign.store_id,
            advertiser_timezone=advertiser_timezone,
            now=now,
        )
    except Exception:  # Order intelligence is additive; stop-loss remains available.
        logger.warning("shop order timing signal unavailable", exc_info=True)
        order_timing = {
            "available": False,
            "confidence": 0,
            "delivery_multiplier": 1,
        }

    min_roi = _to_decimal(guard.get("min_roi"), None) or _to_decimal(strategy.min_roi)
    min_spend = _to_int(guard.get("min_spend_cents"), 300)
    no_order_spend, no_order_context = _dynamic_no_order_spend_cents(
        db,
        campaign=campaign,
        guard=guard,
    )
    early_cap = _to_int(guard.get("early_spend_cap_cents"), 800)
    cooldown_minutes = _to_int(guard.get("pause_cooldown_minutes"), 60)
    budget = _budget_to_cents(campaign.budget_value)
    paused_until = _effective_paused_until(
        active=active,
        paused_until=state.get("paused_until"),
    )
    baseline = dict(state.get("baseline") or {})
    controlled_test = dict(state.get("controlled_test") or {})
    controlled_test_active = bool(controlled_test.get("active"))
    post_test_grace_until = _parse_state_datetime(state.get("post_test_grace_until"))
    post_test_grace_active = post_test_grace_until is not None and now < post_test_grace_until
    window_cost_cents, window_gross_cents, window_orders, window_roi = _window_metrics(metrics, baseline)
    paused_until_dt = None
    if paused_until:
        try:
            paused_until_dt = datetime.fromisoformat(str(paused_until))
            if paused_until_dt.tzinfo is None:
                paused_until_dt = paused_until_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            paused_until_dt = None
    pause_started_at = _parse_state_datetime(state.get("paused_at"))
    last_decision = dict(state.get("last_decision") or {})
    last_threshold_context = dict(last_decision.get("threshold_context") or {})
    legacy_unscheduled_pause = bool(last_threshold_context.get("unscheduled_pause"))
    guard_owned_pause = paused_until_dt is not None and (
        str(state.get("pause_owner") or "") == "smart_guard"
        or (pause_started_at is not None and not legacy_unscheduled_pause)
    )
    product_stats = _product_day_stats(db, campaign=campaign, guard=guard, metrics=metrics)
    item_group_ids = [str(item) for item in product_stats.get("item_group_ids") or []]
    failure_stats = _recent_product_failure_stats(
        db,
        campaign=campaign,
        guard=guard,
        item_group_ids=item_group_ids,
        now=now,
    )
    recent_stats = _product_recent_momentum_stats(
        db,
        campaign=campaign,
        guard=guard,
        item_group_ids=item_group_ids,
        now=now,
    )
    data_consistency = _decision_consistency_snapshot(
        guard=guard,
        metrics=metrics,
        product_stats=product_stats,
        recent_stats=recent_stats,
    )
    decision_product_stats = product_stats
    if not bool(data_consistency.get("product_usable")):
        decision_product_stats = {
            **product_stats,
            "cost_cents": metrics.cost_cents,
            "gross_revenue_cents": metrics.gross_revenue_cents,
            "orders": metrics.orders,
            "roi": metrics.roi,
            "campaign_count": 1,
            "source": "current_campaign_fallback",
        }
    recent_healthy = bool(data_consistency.get("recent_momentum_usable")) and _recent_momentum_is_healthy(
        guard=guard,
        recent_stats=recent_stats,
        min_roi=min_roi,
    )
    healthy = _performance_is_healthy(
        metrics=metrics,
        product_stats=decision_product_stats,
        min_roi=min_roi,
        recent_healthy=recent_healthy,
    )
    daily_loss_cap_cents, daily_loss_context = _dynamic_bad_performance_cap_cents(
        guard=guard,
        metrics=metrics,
        product_stats=decision_product_stats,
        failure_stats=failure_stats,
        budget_cents=budget,
        min_roi=min_roi,
        no_order_spend_cents=no_order_spend,
        healthy=healthy,
    )

    def _pause_until(reason_key: str, base_minutes: int | None = None) -> str:
        minutes = _dynamic_cooldown_minutes(
            guard=guard,
            reason_key=reason_key,
            base_minutes=base_minutes or cooldown_minutes,
            metrics=metrics,
            product_stats=decision_product_stats,
            failure_stats=failure_stats,
            min_roi=min_roi,
            no_order_spend_cents=no_order_spend,
            order_timing=order_timing,
        )
        return (now + timedelta(minutes=minutes)).isoformat()

    base_context = {
        **no_order_context,
        "order_timing": order_timing,
        "product_day": {
            **product_stats,
            "roi": str(product_stats.get("roi")) if product_stats.get("roi") is not None else None,
        },
        "decision_product_day": {
            **decision_product_stats,
            "roi": (
                str(decision_product_stats.get("roi"))
                if decision_product_stats.get("roi") is not None
                else None
            ),
        },
        "data_consistency": data_consistency,
        "product_failures": failure_stats,
        "recent_momentum": {
            **recent_stats,
            "roi": str(recent_stats.get("roi")) if recent_stats.get("roi") is not None else None,
            "active_campaign": {
                **dict(recent_stats.get("active_campaign") or {}),
                "roi": (
                    str((recent_stats.get("active_campaign") or {}).get("roi"))
                    if (recent_stats.get("active_campaign") or {}).get("roi") is not None
                    else None
                ),
            },
            "healthy": recent_healthy,
        },
        **daily_loss_context,
    }
    previous_attempts = _to_int(controlled_test.get("performance_failure_count"), 0)
    test_budget_bounds = _controlled_test_budget_bounds(
        guard=guard,
        campaign=campaign,
        threshold_context=base_context,
        attempt_count=previous_attempts,
    )
    recovery_control = {
        "eligible": bool(data_consistency.get("valid"))
        and not bool(data_consistency.get("attribution_pending"))
        and not list(data_consistency.get("conflicts") or []),
        "paused_campaign": not active,
        "stale_recent_momentum_expected_while_paused": not active,
        "test_budget_bounds": test_budget_bounds,
        "previous_attempt_count": previous_attempts,
    }
    base_context["recovery_control"] = recovery_control
    base_context["pause_ownership"] = {
        "owned_by_smart_guard": guard_owned_pause,
        "has_recovery_window": paused_until_dt is not None,
        "legacy_unscheduled_pause": legacy_unscheduled_pause,
    }

    if not active and not guard_owned_pause:
        return {
            "action": "HOLD",
            "reason": (
                "smart_guard: disabled campaign has no guard-owned recovery window; "
                "explicit enable required"
            ),
            "paused_until": None,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": {
                **base_context,
                "external_or_unknown_pause": True,
            },
            "decision_phase": "EXPLICIT_ENABLE_REQUIRED",
            "monitor_interval_minutes": 1,
        }

    if not bool(data_consistency.get("valid")):
        protection_minutes = max(5, _to_int(guard.get("protection_pause_minutes"), 15))
        review_at = (now + timedelta(minutes=protection_minutes)).isoformat()
        conflict_reason = ",".join(data_consistency.get("conflicts") or []) or "cross_source_conflict"
        return {
            "action": "HOLD",
            "reason": f"smart_guard: data conflict; keep delivery stable while resyncing: {conflict_reason}",
            "paused_until": paused_until,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
            "decision_phase": "VERIFY",
            "requires_hermes_review": True,
            "force_sync": True,
        }

    campaign_started_at = _campaign_created_at_utc(db, campaign)
    fresh_window_minutes = max(
        10,
        _to_int(guard.get("fresh_campaign_controlled_test_window_minutes"), 45),
    )
    campaign_age_minutes = (
        max(0, int((now - campaign_started_at).total_seconds() // 60))
        if campaign_started_at is not None
        else fresh_window_minutes + 1
    )
    learning_active = _learning_phase_active(
        guard=guard,
        campaign_age_minutes=campaign_age_minutes,
        orders=max(metrics.orders, _to_int(decision_product_stats.get("orders"), 0)),
    )
    base_context["learning_phase"] = {
        "active": learning_active,
        "campaign_age_minutes": campaign_age_minutes,
        "minimum_run_minutes": max(60, _to_int(guard.get("learning_min_run_minutes"), 1440)),
        "target_minutes": max(1440, _to_int(guard.get("learning_target_minutes"), 4320)),
        "exit_orders": max(1, _to_int(guard.get("learning_exit_orders"), 20)),
    }
    if (
        active
        and not controlled_test_active
        and not delegate_performance_stop
        and bool(guard.get("fresh_campaign_controlled_test_enabled", True))
        and campaign_age_minutes <= fresh_window_minutes
        and bool(recovery_control.get("eligible"))
    ):
        test_budget = _fallback_controlled_test_budget(
            guard=guard,
            budget_bounds=test_budget_bounds,
            order_timing=order_timing,
        )
        observation_minutes = max(
            10,
            _to_int(guard.get("controlled_test_delivery_probe_minutes"), 20),
        )
        report_date = str((metrics.raw or {}).get("report_end_date") or "")
        return {
            "action": "HOLD",
            "reason": "smart_guard: fresh automated campaign entered bounded delivery probe",
            "paused_until": None,
            "threshold_context": base_context,
            "decision_phase": "FRESH_CAMPAIGN_DELIVERY_PROBE",
            "controlled_test_update": {
                "active": True,
                "status": "RUNNING",
                "stage": "DELIVERY_PROBE",
                "budget_cents": test_budget,
                "spent_cents": 0,
                "remaining_cents": test_budget,
                "gross_revenue_cents": 0,
                "orders": 0,
                "roi": None,
                "budget_owner": "hermes_policy_fallback",
                "budget_bounds": {
                    "min_cents": _to_int(test_budget_bounds.get("min_cents"), test_budget),
                    "max_cents": _to_int(test_budget_bounds.get("max_cents"), test_budget),
                },
                "baseline": _metrics_baseline(metrics),
                "report_date": report_date,
                "started_at": now.isoformat(),
                "review_at": (now + timedelta(minutes=observation_minutes)).isoformat(),
                "attempt_count": 1,
                "no_delivery_count": 0,
                "performance_failure_count": 0,
                "failure_class": None,
                "rebuild_pending": False,
            },
            "monitor_interval_minutes": 1,
        }

    if active and controlled_test_active and delegate_performance_stop:
        return {
            "action": "HOLD",
            "reason": "smart_guard: product performance delegated to creative guard",
            "paused_until": None,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
            "decision_phase": "CREATIVE_GUARD_DELEGATED",
            "controlled_test_update": {
                "active": False,
                "status": "DELEGATED_TO_CREATIVE_GUARD",
                "rebuild_pending": False,
                "completed_at": now.isoformat(),
            },
            "post_test_grace_until": (
                now + timedelta(minutes=max(1440, _to_int(guard.get("learning_min_run_minutes"), 1440)))
            ).isoformat(),
        }

    if active and controlled_test_active:
        test_baseline = dict(controlled_test.get("baseline") or baseline)
        report_date = str((metrics.raw or {}).get("report_end_date") or "")
        baseline_report_date = str(controlled_test.get("report_date") or "")
        rollover_update: dict[str, Any] = {}
        if report_date and baseline_report_date and report_date != baseline_report_date:
            test_baseline = {"cost_cents": 0, "gross_revenue_cents": 0, "orders": 0}
            rollover_update = {"baseline": test_baseline, "report_date": report_date}
        test_cost, test_gmv, test_orders, test_roi = _window_metrics(metrics, test_baseline)
        test_budget = max(100, _to_int(controlled_test.get("budget_cents"), 0))
        started_at = _parse_state_datetime(controlled_test.get("started_at")) or now
        stage = str(controlled_test.get("stage") or "DELIVERY_PROBE").upper()
        review_at = _parse_state_datetime(controlled_test.get("review_at"))
        if review_at is None:
            review_at = started_at + timedelta(
                minutes=max(10, _to_int(guard.get("controlled_test_observation_minutes"), 45))
            )
        success_multiplier = (
            _to_decimal(guard.get("controlled_test_success_roi_multiplier"), "0.80")
            or Decimal("0.80")
        )
        success_roi = (
            (min_roi * success_multiplier).quantize(_ROI_QUANT)
            if min_roi is not None
            else Decimal("1.0")
        )
        test_context = {
            "active": True,
            "budget_cents": test_budget,
            "spent_cents": test_cost,
            "remaining_cents": max(0, test_budget - test_cost),
            "gross_revenue_cents": test_gmv,
            "orders": test_orders,
            "roi": str(test_roi) if test_roi is not None else None,
            "success_roi": str(success_roi),
            "started_at": started_at.isoformat(),
            "review_at": review_at.isoformat(),
            "attempt_count": _to_int(controlled_test.get("attempt_count"), 1),
            "budget_owner": "hermes",
            "stage": stage,
        }
        base_context["controlled_test"] = test_context
        common_update = {
            **rollover_update,
            "spent_cents": test_cost,
            "remaining_cents": max(0, test_budget - test_cost),
            "gross_revenue_cents": test_gmv,
            "orders": test_orders,
            "roi": str(test_roi) if test_roi is not None else None,
            "last_evaluated_at": now.isoformat(),
        }
        if stage == "DELIVERY_PROBE":
            delivery_threshold = min(
                test_budget,
                max(25, _to_int(guard.get("controlled_test_delivery_spend_cents"), 50)),
            )
            elapsed_minutes = max(1, int((now - started_at).total_seconds() // 60))
            if test_cost >= delivery_threshold:
                performance_plan = _performance_test_plan(
                    guard=guard,
                    threshold_context=base_context,
                    campaign=campaign,
                    probe_spend_cents=test_cost,
                    probe_elapsed_minutes=elapsed_minutes,
                )
                performance_budget = performance_plan["budget_cents"]
                performance_minutes = performance_plan["observation_minutes"]
                desired_platform_budget = max(
                    metrics.cost_cents + performance_budget,
                    _to_int(guard.get("controlled_test_budget_floor_cents"), 2000),
                )
                return {
                    "action": "ADJUST"
                    if desired_platform_budget != _budget_to_cents(campaign.budget_value)
                    else "HOLD",
                    "reason": (
                        "smart_guard: delivery probe passed; collecting performance evidence "
                        f"with ${performance_budget / 100:.2f} incremental budget"
                    ),
                    "paused_until": None,
                    "adjustment": {
                        "budget_cents": desired_platform_budget,
                        "budget": float(Decimal(desired_platform_budget) / Decimal("100")),
                    },
                    "threshold_context": {**base_context, "performance_test_plan": performance_plan},
                    "decision_phase": "PERFORMANCE_TEST",
                    "controlled_test_update": {
                        **common_update,
                        "active": True,
                        "status": "RUNNING",
                        "stage": "PERFORMANCE_TEST",
                        "delivery_confirmed_at": now.isoformat(),
                        "delivery_probe_spend_cents": test_cost,
                        "budget_cents": performance_budget,
                        "spent_cents": 0,
                        "remaining_cents": performance_budget,
                        "gross_revenue_cents": 0,
                        "orders": 0,
                        "roi": None,
                        "baseline": _metrics_baseline(metrics),
                        "report_date": report_date,
                        "started_at": now.isoformat(),
                        "review_at": (now + timedelta(minutes=performance_minutes)).isoformat(),
                        "minimum_evidence_spend_cents": performance_plan["minimum_evidence_spend_cents"],
                        "performance_plan": performance_plan,
                        "failure_class": None,
                        "rebuild_pending": False,
                    },
                    "monitor_interval_minutes": 1,
                }
            if now >= review_at:
                no_delivery_count = max(0, _to_int(controlled_test.get("no_delivery_count"), 0)) + 1
                extension_minutes = max(
                    120,
                    _to_int(guard.get("controlled_test_delivery_probe_minutes"), 120),
                )
                return {
                    "action": "HOLD",
                    "reason": (
                        "smart_guard: low delivery during learning; keep campaign stable and extend probe, "
                        f"${test_cost / 100:.2f} delivered"
                    ),
                    "paused_until": None,
                    "threshold_context": base_context,
                    "decision_phase": "DELIVERY_PROBE_EXTENDED",
                    "controlled_test_update": {
                        **common_update,
                        "active": True,
                        "status": "RUNNING",
                        "stage": "DELIVERY_PROBE",
                        "review_at": (now + timedelta(minutes=extension_minutes)).isoformat(),
                        "failure_class": "INSUFFICIENT_DELIVERY",
                        "failure_reason": "delivery probe extended without resetting learning",
                        "no_delivery_count": no_delivery_count,
                        "rebuild_pending": False,
                    },
                    "monitor_interval_minutes": 3,
                }
            return {
                "action": "HOLD",
                "reason": (
                    "smart_guard: delivery probe collecting traffic, "
                    f"${test_cost / 100:.2f} of ${test_budget / 100:.2f} spent"
                ),
                "paused_until": None,
                "threshold_context": base_context,
                "decision_phase": "DELIVERY_PROBE",
                "controlled_test_update": common_update,
                "monitor_interval_minutes": 1,
            }

        if test_orders > 0 and test_roi is not None and test_roi >= success_roi:
            grace_until = now + timedelta(
                minutes=max(10, _to_int(guard.get("controlled_test_post_success_grace_minutes"), 30))
            )
            return {
                "action": "HOLD",
                "reason": (
                    "smart_guard: controlled test passed, "
                    f"incremental roi {test_roi} with {test_orders} new orders"
                ),
                "paused_until": None,
                "next_baseline": _metrics_baseline(metrics),
                "threshold_context": base_context,
                "decision_phase": "CONTROLLED_TEST_PASSED",
                "controlled_test_update": {
                    **common_update,
                    "active": False,
                    "status": "PASSED",
                    "stage": "PERFORMANCE_TEST",
                    "completed_at": now.isoformat(),
                    "failure_class": None,
                    "rebuild_pending": False,
                },
                "post_test_grace_until": grace_until.isoformat(),
            }
        budget_exhausted = test_cost >= test_budget
        observation_expired = now >= review_at
        minimum_evidence_spend = max(
            100,
            _to_int(controlled_test.get("minimum_evidence_spend_cents"), min(test_budget, 300)),
        )
        if learning_active and (budget_exhausted or observation_expired):
            extension_minutes = max(
                360,
                _to_int(guard.get("controlled_test_performance_min_minutes"), 360),
            )
            allowed_cpa = max(500, _to_int(base_context.get("allowed_cpa_cents"), 500))
            extended_budget = max(test_budget, test_cost + allowed_cpa)
            extended_budget = min(
                max(extended_budget, test_budget),
                max(test_budget, _to_int(guard.get("controlled_test_max_budget_cents"), 5000)),
            )
            return {
                "action": "HOLD",
                "reason": (
                    "smart_guard: learning window protected; extend performance evidence instead of pausing, "
                    f"spent ${test_cost / 100:.2f}, orders {test_orders}"
                ),
                "paused_until": None,
                "threshold_context": base_context,
                "decision_phase": "LEARNING_WINDOW_EXTENDED",
                "controlled_test_update": {
                    **common_update,
                    "active": True,
                    "status": "RUNNING",
                    "stage": "PERFORMANCE_TEST",
                    "budget_cents": extended_budget,
                    "remaining_cents": max(0, extended_budget - test_cost),
                    "review_at": (now + timedelta(minutes=extension_minutes)).isoformat(),
                    "failure_class": None,
                    "rebuild_pending": False,
                },
                "monitor_interval_minutes": 3,
            }
        if budget_exhausted or (observation_expired and test_cost >= minimum_evidence_spend):
            failure_reason = "budget exhausted" if budget_exhausted else "evidence window expired"
            attempt_count = max(1, _to_int(controlled_test.get("attempt_count"), 1))
            performance_failure_count = max(
                0, _to_int(controlled_test.get("performance_failure_count"), 0)
            ) + 1
            return {
                "action": "PAUSE",
                "reason": (
                    f"smart_guard: controlled test failed ({failure_reason}), "
                    f"spent ${test_cost / 100:.2f}, orders {test_orders}, "
                    f"roi {test_roi if test_roi is not None else 'n/a'}"
                ),
                "paused_until": _pause_until(
                    "controlled_test_failed",
                    cooldown_minutes + min(180, attempt_count * 15),
                ),
                "next_baseline": _metrics_baseline(metrics),
                "threshold_context": base_context,
                "decision_phase": "CONTROLLED_TEST_FAILED",
                "failure_class": "POOR_PERFORMANCE",
                "controlled_test_update": {
                    **common_update,
                    "active": False,
                    "status": "FAILED",
                    "stage": "PERFORMANCE_TEST",
                    "completed_at": now.isoformat(),
                    "failure_class": "POOR_PERFORMANCE",
                    "failure_reason": failure_reason,
                    "performance_failure_count": performance_failure_count,
                    "rebuild_pending": False,
                },
            }
        if observation_expired and test_cost < minimum_evidence_spend:
            no_delivery_count = max(0, _to_int(controlled_test.get("no_delivery_count"), 0)) + 1
            extension_minutes = max(
                360,
                _to_int(guard.get("controlled_test_performance_min_minutes"), 360),
            )
            return {
                "action": "HOLD",
                "reason": (
                    "smart_guard: insufficient delivery evidence; extend observation without resetting learning, "
                    f"spent ${test_cost / 100:.2f}, need ${minimum_evidence_spend / 100:.2f}"
                ),
                "paused_until": None,
                "threshold_context": base_context,
                "decision_phase": "PERFORMANCE_TEST_EXTENDED",
                "failure_class": "INSUFFICIENT_DELIVERY",
                "controlled_test_update": {
                    **common_update,
                    "active": True,
                    "status": "RUNNING",
                    "stage": "PERFORMANCE_TEST",
                    "review_at": (now + timedelta(minutes=extension_minutes)).isoformat(),
                    "failure_class": "INSUFFICIENT_DELIVERY",
                    "failure_reason": "minimum evidence spend not reached; observation extended",
                    "no_delivery_count": no_delivery_count,
                    "rebuild_pending": False,
                },
                "monitor_interval_minutes": 3,
            }
        return {
            "action": "HOLD",
            "reason": (
                "smart_guard: controlled test collecting evidence, "
                f"${test_cost / 100:.2f} of ${test_budget / 100:.2f} spent"
            ),
            "paused_until": None,
            "threshold_context": base_context,
            "decision_phase": "CONTROLLED_TEST",
            "controlled_test_update": common_update,
            "monitor_interval_minutes": max(
                1,
                _to_int(guard.get("controlled_test_monitor_interval_minutes"), 1),
            ),
        }

    if active:
        if (
            not post_test_grace_active
            and not learning_active
            and not delegate_performance_stop
            and
            daily_loss_cap_cents is not None
            and _to_int(decision_product_stats.get("cost_cents"), 0) >= daily_loss_cap_cents
        ):
            return {
                "action": "PAUSE",
                "reason": (
                    "smart_guard: product risk cap reached, "
                    f"cap ${daily_loss_cap_cents / 100:.2f}, "
                    f"product spend ${_to_int(decision_product_stats.get('cost_cents'), 0) / 100:.2f}"
                ),
                "paused_until": _pause_until("product_risk_cap", catastrophic_cooldown_minutes if 'catastrophic_cooldown_minutes' in locals() else cooldown_minutes),
                "next_baseline": _metrics_baseline(metrics),
                "threshold_context": base_context,
            }

        daily_cap_cents = _to_int(guard.get("daily_spend_cap_cents"), 0)
        if (
            not post_test_grace_active
            and bool(guard.get("daily_spend_cap_enabled", True))
            and daily_cap_cents > 0
        ):
            cap_floor = max(0, _to_int(guard.get("min_spend_cents"), 300))
            effective_daily_cap = max(daily_cap_cents, cap_floor)
            if metrics.cost_cents >= effective_daily_cap and not healthy:
                day_end = _advertiser_day_end_utc_iso(
                    db,
                    workspace_id=campaign.workspace_id,
                    auth_id=campaign.auth_id,
                    advertiser_id=campaign.advertiser_id,
                )
                return {
                    "action": "PAUSE",
                    "reason": (
                        "smart_guard: daily spend cap reached, "
                        f"cap ${effective_daily_cap / 100:.2f}, spend ${metrics.cost_cents / 100:.2f}"
                    ),
                    "paused_until": day_end,
                    "next_baseline": _metrics_baseline(metrics),
                    "threshold_context": {
                        **base_context,
                        "daily_spend_cap_cents": effective_daily_cap,
                        "daily_spend_cap": True,
                    },
                }

        catastrophic_cooldown_minutes = _to_int(
            guard.get("catastrophic_pause_cooldown_minutes"),
            0,
        )
        if catastrophic_cooldown_minutes <= 0:
            catastrophic_cooldown_minutes = max(cooldown_minutes, 120)
        disable_on_catastrophic = _to_bool(
            guard.get("disable_strategy_on_catastrophic_stop"),
            False,
        )
        catastrophic_paused_until = (
            None
            if disable_on_catastrophic
            else (now + timedelta(minutes=catastrophic_cooldown_minutes)).isoformat()
        )

        if (
            not post_test_grace_active
            and not delegate_performance_stop
            and bool(guard.get("catastrophic_stop_enabled", True))
        ):
            hard_no_order_multiplier = (
                _to_decimal(guard.get("catastrophic_no_order_multiplier"), "3.0")
                or Decimal("3.0")
            )
            hard_no_order_cents = max(
                no_order_spend,
                int(Decimal(no_order_spend) * hard_no_order_multiplier),
            )
            if learning_active:
                hard_no_order_cents = max(
                    hard_no_order_cents,
                    _to_int(guard.get("learning_emergency_min_spend_cents"), 3000),
                )
            if metrics.orders <= 0 and metrics.cost_cents >= hard_no_order_cents:
                return {
                    "action": "PAUSE",
                    "reason": (
                        "smart_guard: hard stop, "
                        f"spend ${metrics.cost_cents / 100:.2f} with 0 orders"
                    ),
                    "paused_until": (
                        None
                        if disable_on_catastrophic
                        else _pause_until("hard_no_order", catastrophic_cooldown_minutes)
                    ),
                    "next_baseline": _metrics_baseline(metrics),
                    "threshold_context": {
                        **base_context,
                        "hard_no_order_threshold_cents": hard_no_order_cents,
                        "catastrophic_stop": True,
                        "catastrophic_pause_cooldown_minutes": catastrophic_cooldown_minutes,
                        "disable_strategy_on_catastrophic_stop": disable_on_catastrophic,
                    },
                    "disable_strategy": disable_on_catastrophic,
                }

            bad_roi_share = (
                _to_decimal(guard.get("catastrophic_bad_roi_budget_share"), "0.25")
                or Decimal("0.25")
            )
            bad_roi_ratio = (
                _to_decimal(guard.get("catastrophic_bad_roi_ratio"), "0.50")
                or Decimal("0.50")
            )
            hard_bad_roi_cents = max(early_cap, int(Decimal(budget) * bad_roi_share)) if budget > 0 else 0
            hard_bad_roi = (min_roi * bad_roi_ratio).quantize(_ROI_QUANT) if min_roi is not None else None
            if (
                not learning_active
                and
                hard_bad_roi_cents > 0
                and metrics.cost_cents >= hard_bad_roi_cents
                and hard_bad_roi is not None
                and metrics.roi is not None
                and metrics.roi < hard_bad_roi
            ):
                return {
                    "action": "PAUSE",
                    "reason": (
                        "smart_guard: hard stop, "
                        f"roi {metrics.roi} < {hard_bad_roi} after spend ${metrics.cost_cents / 100:.2f}"
                    ),
                    "paused_until": (
                        None
                        if disable_on_catastrophic
                        else _pause_until("hard_bad_roi", catastrophic_cooldown_minutes)
                    ),
                    "next_baseline": _metrics_baseline(metrics),
                    "threshold_context": {
                        **base_context,
                        "hard_bad_roi_threshold": str(hard_bad_roi),
                        "hard_bad_roi_cost_cents": hard_bad_roi_cents,
                        "catastrophic_stop": True,
                        "catastrophic_pause_cooldown_minutes": catastrophic_cooldown_minutes,
                        "disable_strategy_on_catastrophic_stop": disable_on_catastrophic,
                    },
                    "disable_strategy": disable_on_catastrophic,
                }

        if (
            not learning_active
            and not delegate_performance_stop
            and bool(guard.get("window_stop_enabled", False))
            and window_cost_cents >= no_order_spend
            and window_orders <= 0
        ):
            return {
                "action": "PAUSE",
                "reason": f"smart_guard: spend ${window_cost_cents / 100:.2f} with 0 orders in current window",
                "paused_until": _pause_until("window_no_order", cooldown_minutes),
                "next_baseline": _metrics_baseline(metrics),
                "threshold_context": base_context,
            }
        if (
            not learning_active
            and not delegate_performance_stop
            and bool(guard.get("window_stop_enabled", False))
            and
            window_cost_cents >= min_spend
            and min_roi is not None
            and window_roi is not None
            and window_roi < min_roi
            and (window_orders > 0 or window_cost_cents >= no_order_spend)
        ):
            return {
                "action": "PAUSE",
                "reason": f"smart_guard: window roi {window_roi} < {min_roi}",
                "paused_until": _pause_until("window_bad_roi", cooldown_minutes),
                "next_baseline": _metrics_baseline(metrics),
                "threshold_context": base_context,
            }
        if (
            not post_test_grace_active
            and not learning_active
            and not delegate_performance_stop
            and bool(guard.get("daily_budget_pacing", False))
        ):
            if budget > 0:
                progress = _day_progress(
                    db,
                    workspace_id=campaign.workspace_id,
                    auth_id=campaign.auth_id,
                    advertiser_id=campaign.advertiser_id,
                )
                multiplier = _to_decimal(guard.get("pacing_multiplier"), "1.25") or Decimal("1.25")
                allowed = max(early_cap, int(Decimal(budget) * progress * multiplier))
                if metrics.cost_cents >= allowed and not healthy:
                    return {
                        "action": "PAUSE",
                        "reason": f"smart_guard: pacing cap ${allowed / 100:.2f}, spend ${metrics.cost_cents / 100:.2f}",
                        "paused_until": _pause_until("pacing_cap", cooldown_minutes),
                        "next_baseline": _metrics_baseline(metrics),
                        "threshold_context": base_context,
                    }

        adjustment = None
        minimum_learning_minutes = max(
            60,
            _to_int(guard.get("learning_min_run_minutes"), 1440),
        )
        if not post_test_grace_active and campaign_age_minutes >= minimum_learning_minutes:
            adjustment = _profitable_scale_adjustment(
                guard=guard,
                state=state,
                campaign=campaign,
                metrics=metrics,
                product_stats=decision_product_stats,
                min_roi=min_roi,
                now=now,
                order_timing=order_timing,
                allow_roas_adjustment=(
                    campaign_age_minutes
                    >= max(
                        minimum_learning_minutes,
                        _to_int(guard.get("roas_freeze_minutes"), 4320),
                    )
                ),
            )
        if adjustment:
            return {
                "action": "ADJUST",
                "reason": "smart_guard: profitable scale adjustment",
                "paused_until": paused_until,
                "adjustment": adjustment,
                "threshold_context": base_context,
            }

    if (
        not active
        and bool(controlled_test.get("rebuild_pending"))
        and (paused_until_dt is None or now >= paused_until_dt)
    ):
        return {
            "action": "REBUILD",
            "reason": "smart_guard: repeated insufficient delivery requires a fresh campaign",
            "paused_until": None,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
            "decision_phase": "REBUILD_PENDING",
            "failure_class": controlled_test.get("failure_class") or "NO_DELIVERY",
            "controlled_test_update": {
                "active": False,
                "status": "REBUILDING",
                "rebuild_pending": True,
                "last_rebuild_requested_at": now.isoformat(),
            },
        }

    if (
        not active
        and paused_until_dt is not None
        and recent_healthy
        and _to_bool(guard.get("recent_momentum_backfill_resume"), True)
    ):
        return {
            "action": "START",
            "reason": "smart_guard: recent momentum recovered during cooldown",
            "paused_until": None,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
        }

    if not active and paused_until_dt is not None and now >= paused_until_dt:
        if delegate_performance_stop:
            return {
                "action": "START",
                "reason": "smart_guard: cooldown complete; resume stable creative-level optimization",
                "paused_until": None,
                "next_baseline": _metrics_baseline(metrics),
                "threshold_context": base_context,
                "decision_phase": "CREATIVE_GUARD_STABLE_RESUME",
            }
        return {
            "action": "START",
            "reason": "smart_guard: cooldown complete; Hermes controlled test required",
            "paused_until": None,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
            "controlled_test_required": True,
            "decision_phase": "CONTROLLED_TEST_REVIEW",
        }

    peak_confidence = (
        _to_decimal(guard.get("peak_recovery_min_confidence"), "0.35") or Decimal("0.35")
    )
    peak_multiplier = (
        _to_decimal(guard.get("peak_recovery_min_multiplier"), "1.15") or Decimal("1.15")
    )
    timing_confidence = _to_decimal(order_timing.get("confidence"), "0") or Decimal("0")
    timing_multiplier = _to_decimal(order_timing.get("delivery_multiplier"), "1") or Decimal("1")
    paused_minutes = (
        int((now - pause_started_at).total_seconds() // 60)
        if pause_started_at is not None
        else 0
    )
    max_peak_failures = max(1, _to_int(guard.get("peak_recovery_max_performance_failures"), 2))
    if (
        not active
        and paused_until_dt is not None
        and now < paused_until_dt
        and bool(guard.get("peak_recovery_enabled", True))
        and timing_confidence >= peak_confidence
        and timing_multiplier >= peak_multiplier
        and bool(recovery_control.get("eligible"))
        and not bool(controlled_test.get("rebuild_pending"))
        and paused_minutes >= max(5, _to_int(guard.get("peak_recovery_min_pause_minutes"), 10))
        and _to_int(controlled_test.get("performance_failure_count"), 0) < max_peak_failures
    ):
        return {
            "action": "START",
            "reason": "smart_guard: peak demand window; bounded Hermes test overrides extended cooldown",
            "paused_until": None,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
            "controlled_test_required": True,
            "decision_phase": "PEAK_CONTROLLED_TEST_REVIEW",
        }

    if not active and paused_until_dt is not None:
        return {
            "action": "HOLD",
            "reason": "smart_guard: cooldown active",
            "paused_until": paused_until,
            "next_baseline": _metrics_baseline(metrics),
            "threshold_context": base_context,
        }

    return {
        "action": "HOLD",
        "reason": "smart_guard: no change",
        "paused_until": paused_until,
        "threshold_context": base_context,
    }


def _review_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _prepare_two_stage_decision(
    *,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    decision: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    prepared = dict(decision)
    guard = _guard_config(strategy)
    if not bool(guard.get("hermes_action_review_enabled", True)):
        return prepared
    action = str(prepared.get("action") or "HOLD").upper()
    protection_minutes = max(5, _to_int(guard.get("protection_pause_minutes"), 15))
    proposed_until = _review_datetime(prepared.get("paused_until"))
    proposed_minutes = (
        max(0, int((proposed_until - now).total_seconds() // 60))
        if proposed_until is not None
        else 0
    )

    review_required = bool(prepared.get("requires_hermes_review"))
    if action in {"START", "ADJUST"}:
        review_required = True
    elif action == "PAUSE":
        review_required = True
    elif action == "HOLD" and proposed_minutes > protection_minutes:
        review_required = True
    if not review_required:
        return prepared

    prepared["requires_hermes_review"] = True
    prepared["proposed_action"] = (
        "HOLD_EXTENSION" if action == "HOLD" else action
    )
    prepared["proposed_paused_until"] = (
        proposed_until.isoformat() if proposed_until is not None else None
    )
    prepared["proposed_pause_minutes"] = proposed_minutes
    prepared.setdefault("decision_phase", "PROTECTION" if action == "PAUSE" else "REVIEW")
    if action == "PAUSE":
        prepared["paused_until"] = (now + timedelta(minutes=protection_minutes)).isoformat()
        if bool(prepared.get("disable_strategy")):
            prepared["proposed_disable_strategy"] = True
            prepared["disable_strategy"] = False
    return prepared


def _action_review_fingerprint(
    *,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    decision: Mapping[str, Any],
) -> str:
    context = dict(decision.get("threshold_context") or {})
    material = {
        "campaign_id": campaign.campaign_id,
        "action": decision.get("proposed_action") or decision.get("action"),
        "reason": decision.get("reason"),
        "paused_until": decision.get("proposed_paused_until") or decision.get("paused_until"),
        "metrics": {
            "cost_cents": metrics.cost_cents,
            "gross_revenue_cents": metrics.gross_revenue_cents,
            "orders": metrics.orders,
        },
        "product_day": context.get("decision_product_day") or context.get("product_day"),
        "recent_momentum": context.get("recent_momentum"),
        "data_consistency": context.get("data_consistency"),
    }
    return hashlib.sha256(_json_dumps(material).encode("utf-8")).hexdigest()


async def _review_two_stage_decision(
    *,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    metrics: RealtimeMetrics,
    decision: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    reviewed = dict(decision)
    if not bool(reviewed.get("requires_hermes_review")):
        return reviewed
    guard = _guard_config(strategy)
    fingerprint = _action_review_fingerprint(
        campaign=campaign,
        metrics=metrics,
        decision=reviewed,
    )
    state = _smart_guard_state(strategy)
    previous = dict((state.get("last_decision") or {}).get("hermes_review") or {})
    previous_at = _review_datetime(previous.get("reviewed_at"))
    cache_minutes = max(1, _to_int(guard.get("hermes_action_review_cache_minutes"), 10))
    if (
        previous.get("fingerprint") == fingerprint
        and previous_at is not None
        and now < previous_at + timedelta(minutes=cache_minutes)
    ):
        review = {**previous, "status": "cached"}
    else:
        context = dict(reviewed.get("threshold_context") or {})
        try:
            review = await review_smart_guard_action(
                campaign={
                    "campaign_id": campaign.campaign_id,
                    "campaign_name": campaign.campaign_name,
                    "operation_status": campaign.operation_status,
                    "secondary_status": campaign.secondary_status,
                    "budget_cents": _budget_to_cents(campaign.budget_value),
                    "roas_bid": str(campaign.roas_bid) if campaign.roas_bid is not None else None,
                },
                metrics={
                    "cost_cents": metrics.cost_cents,
                    "gross_revenue_cents": metrics.gross_revenue_cents,
                    "orders": metrics.orders,
                    "roi": str(metrics.roi) if metrics.roi is not None else None,
                    "fetched_at": (metrics.fetched_at or now).isoformat(),
                    "request_id": metrics.request_id,
                },
                proposed_decision={
                    "action": reviewed.get("proposed_action") or reviewed.get("action"),
                    "reason": reviewed.get("reason"),
                    "proposed_pause_minutes": reviewed.get("proposed_pause_minutes"),
                    "adjustment": reviewed.get("adjustment"),
                },
                evidence={
                    "data_consistency": context.get("data_consistency"),
                    "product_day": context.get("decision_product_day") or context.get("product_day"),
                    "recent_momentum": context.get("recent_momentum"),
                    "product_failures": context.get("product_failures"),
                    "order_timing": context.get("order_timing"),
                    "daily_loss_cap_cents": context.get("daily_loss_cap_cents"),
                    "allowed_cpa_cents": context.get("allowed_cpa_cents"),
                    "recovery_control": context.get("recovery_control"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Hermes action review unavailable; retaining short protection window",
                extra={"campaign_id": campaign.campaign_id, "error_type": type(exc).__name__},
            )
            review = {
                "status": "unavailable",
                "decision": "HOLD",
                "confidence": "low",
                "reason": "Hermes action review is temporarily unavailable.",
                "risk_flags": ["review_unavailable"],
                "review_after_minutes": max(10, _to_int(guard.get("protection_pause_minutes"), 15)),
                "budget_multiplier": None,
                "test_budget_cents": None,
                "reviewed_at": now.isoformat(),
            }
    review = {**review, "fingerprint": fingerprint}
    reviewed["hermes_review"] = review
    review_decision = str(review.get("decision") or "HOLD").upper()
    action = str(reviewed.get("action") or "HOLD").upper()
    protection_minutes = max(5, _to_int(guard.get("protection_pause_minutes"), 15))
    proposed_minutes = max(protection_minutes, _to_int(reviewed.get("proposed_pause_minutes"), protection_minutes))
    review_after = _to_int(review.get("review_after_minutes"), 0)

    if action == "PAUSE":
        if review_decision == "APPROVE":
            final_minutes = review_after or proposed_minutes
        elif review_decision == "REVISE":
            final_minutes = review_after or min(proposed_minutes, 60)
        else:
            final_minutes = protection_minutes
        final_minutes = max(protection_minutes, min(_to_int(guard.get("max_pause_cooldown_minutes"), 360), final_minutes))
        reviewed["paused_until"] = (now + timedelta(minutes=final_minutes)).isoformat()
        reviewed["approved_pause_minutes"] = final_minutes
        reviewed["decision_phase"] = "COOLDOWN" if review_decision in {"APPROVE", "REVISE"} else "VERIFY"
        if review_decision == "APPROVE" and bool(reviewed.get("proposed_disable_strategy")):
            reviewed["disable_strategy"] = True
        return reviewed

    if action == "START":
        context = dict(reviewed.get("threshold_context") or {})
        recovery_control = dict(context.get("recovery_control") or {})
        budget_bounds = dict(recovery_control.get("test_budget_bounds") or {})
        controlled_required = bool(reviewed.get("controlled_test_required"))
        test_budget = _to_int(review.get("test_budget_cents"), 0)
        if controlled_required and test_budget <= 0 and review.get("budget_multiplier") is not None:
            lower = max(1, _to_int(budget_bounds.get("min_cents"), 1))
            upper = max(lower, _to_int(budget_bounds.get("max_cents"), lower))
            multiplier = Decimal(str(review.get("budget_multiplier")))
            test_budget = lower + int(Decimal(max(0, upper - lower)) * multiplier)
        if (
            controlled_required
            and bool(recovery_control.get("eligible"))
            and review_decision == "HOLD"
        ):
            if test_budget <= 0 and bool(guard.get("controlled_test_fallback_enabled", True)):
                test_budget = _fallback_controlled_test_budget(
                    guard=guard,
                    budget_bounds=budget_bounds,
                    order_timing=context.get("order_timing"),
                )
            review = {
                **review,
                "original_decision": "HOLD",
                "decision": "REVISE",
                "policy_resolution": "bounded_controlled_test",
                "test_budget_cents": test_budget,
            }
            reviewed["hermes_review"] = review
            review_decision = "REVISE"
        if review_decision not in {"APPROVE", "REVISE"}:
            reviewed["action"] = "HOLD"
            reviewed["reason"] = f"smart_guard: Hermes deferred recovery: {review.get('reason') or 'insufficient evidence'}"
            reviewed["paused_until"] = (now + timedelta(minutes=review_after or protection_minutes)).isoformat()
            reviewed["decision_phase"] = "VERIFY"
        elif controlled_required:
            lower = max(1, _to_int(budget_bounds.get("min_cents"), 1))
            upper = max(lower, _to_int(budget_bounds.get("max_cents"), lower))
            if test_budget <= 0:
                reviewed["action"] = "HOLD"
                reviewed["reason"] = "smart_guard: Hermes did not return a controlled test budget"
                reviewed["paused_until"] = (now + timedelta(minutes=protection_minutes)).isoformat()
                reviewed["decision_phase"] = "VERIFY"
                return reviewed
            test_budget = max(lower, min(upper, test_budget))
            min_observation = max(5, _to_int(guard.get("controlled_test_min_observation_minutes"), 10))
            max_observation = max(
                min_observation,
                _to_int(guard.get("controlled_test_max_observation_minutes"), 60),
            )
            observation_minutes = max(
                min_observation,
                min(
                    max_observation,
                    _to_int(guard.get("controlled_test_delivery_probe_minutes"), 20),
                ),
            )
            previous_test = dict(state.get("controlled_test") or {})
            attempt_count = max(1, _to_int(previous_test.get("attempt_count"), 0) + 1)
            report_date = str((metrics.raw or {}).get("report_end_date") or "")
            reviewed["controlled_test_budget_cents"] = test_budget
            reviewed["controlled_test_observation_minutes"] = observation_minutes
            reviewed["controlled_test_update"] = {
                "active": True,
                "status": "RUNNING",
                "stage": "DELIVERY_PROBE",
                "budget_cents": test_budget,
                "spent_cents": 0,
                "remaining_cents": test_budget,
                "gross_revenue_cents": 0,
                "orders": 0,
                "roi": None,
                "budget_owner": "hermes",
                "budget_bounds": {"min_cents": lower, "max_cents": upper},
                "baseline": _metrics_baseline(metrics),
                "report_date": report_date,
                "started_at": now.isoformat(),
                "review_at": (now + timedelta(minutes=observation_minutes)).isoformat(),
                "attempt_count": attempt_count,
                "no_delivery_count": _to_int(previous_test.get("no_delivery_count"), 0),
                "performance_failure_count": _to_int(
                    previous_test.get("performance_failure_count"), 0
                ),
                "failure_class": None,
                "rebuild_pending": False,
                "hermes_reviewed_at": review.get("reviewed_at"),
            }
            reviewed["decision_phase"] = "CONTROLLED_TEST"
            reviewed["paused_until"] = None
        elif review_decision == "REVISE" and review.get("budget_multiplier") is not None:
            reviewed["pre_start_budget_multiplier"] = review.get("budget_multiplier")
            reviewed["decision_phase"] = "CONTROLLED_TEST"
        else:
            reviewed["decision_phase"] = "APPROVED_RESUME"
        return reviewed

    if action == "ADJUST":
        if review_decision == "HOLD":
            reviewed["action"] = "HOLD"
            reviewed["reason"] = f"smart_guard: Hermes deferred adjustment: {review.get('reason') or 'insufficient evidence'}"
            reviewed["decision_phase"] = "VERIFY"
        elif review_decision == "REVISE" and review.get("budget_multiplier") is not None:
            adjustment = dict(reviewed.get("adjustment") or {})
            current_budget = _budget_to_cents(campaign.budget_value)
            proposed_budget = _to_int(adjustment.get("budget_cents"), current_budget)
            multiplier = Decimal(str(review.get("budget_multiplier")))
            revised_budget = max(
                0,
                current_budget
                + int(Decimal(proposed_budget - current_budget) * multiplier),
            )
            adjustment["budget_cents"] = revised_budget
            adjustment["budget"] = float(Decimal(revised_budget) / Decimal("100"))
            reviewed["adjustment"] = adjustment
        return reviewed

    if action == "HOLD":
        approved_until = _review_datetime(reviewed.get("proposed_paused_until"))
        if approved_until is not None:
            final_until = approved_until
            if review_decision in {"APPROVE", "REVISE"} and review_after > 0:
                final_until = min(
                    approved_until,
                    now + timedelta(minutes=max(10, min(360, review_after))),
                )
            reviewed["paused_until"] = final_until.isoformat()
            reviewed["decision_phase"] = "COOLDOWN"
            return reviewed
        if review_decision == "APPROVE":
            final_minutes = review_after or proposed_minutes
        elif review_decision == "REVISE":
            final_minutes = review_after or min(proposed_minutes, 60)
        else:
            final_minutes = review_after or protection_minutes
        reviewed["paused_until"] = (now + timedelta(minutes=max(10, min(360, final_minutes)))).isoformat()
        reviewed["decision_phase"] = "COOLDOWN" if review_decision in {"APPROVE", "REVISE"} else "VERIFY"
    return reviewed


def _enqueue_conflict_sync(
    *,
    db: Session,
    strategy: GmvStrategyConfig,
    campaign: CatalogCampaign,
    decision: dict[str, Any],
    now: datetime,
) -> None:
    if not bool(decision.get("force_sync")):
        return
    guard = _guard_config(strategy)
    state = _smart_guard_state(strategy)
    last_sync = _review_datetime(state.get("last_forced_sync_at"))
    cooldown = max(5, _to_int(guard.get("forced_sync_cooldown_minutes"), 10))
    if last_sync is not None and now < last_sync + timedelta(minutes=cooldown):
        decision["forced_sync_status"] = "deduplicated"
        return
    context = dict(decision.get("threshold_context") or {})
    product_day = dict(context.get("product_day") or {})
    start_day, end_day = _campaign_report_date_range(db, campaign)
    result = current_app.send_task(
        "gmvmax.manual_sync_levels",
        kwargs={
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "levels": ["CAMPAIGN", "PRODUCT", "CREATIVE"],
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "campaign_ids": [campaign.campaign_id],
            "item_group_ids": [str(item) for item in product_day.get("item_group_ids") or []],
        },
        queue="gmvmax",
    )
    decision["forced_sync_status"] = "enqueued"
    decision["forced_sync_task_id"] = result.id
    decision["forced_sync_at"] = now.isoformat()


async def _apply_status_action_unlocked(
    db: Session,
    *,
    campaign: CatalogCampaign,
    action: str,
    mutation: Any,
) -> dict[str, Any]:
    requested_campaign = campaign
    campaign = _reload_catalog_campaign_for_mutation(db, campaign)
    _assert_smart_guard_mutation_allowed(db, campaign)
    operation_status = "DISABLE" if action == "PAUSE" else "ENABLE"
    client = build_ttb_gmvmax_client(db, auth_id=int(campaign.auth_id))
    try:
        request = CampaignStatusUpdateRequest(
            advertiser_id=str(campaign.advertiser_id),
            campaign_ids=[str(campaign.campaign_id)],
            operation_status=operation_status,
        )
        mutation.assert_current(db)
        response = await client.campaign_status_update(request)
        mutation.assert_current(db)
        mutation_observed_at = catalog_observation_now()
    finally:
        await client.aclose()

    table_name = (
        "gmvmax_live_campaign_catalog"
        if campaign.promotion_type == "LIVE"
        else "gmvmax_product_campaign_catalog"
    )
    local_status = "DISABLE" if action == "PAUSE" else "ENABLE"
    local_secondary_status = (
        "CAMPAIGN_STATUS_DISABLE" if action == "PAUSE" else "CAMPAIGN_STATUS_ENABLE"
    )
    db.execute(
        text(
            f"""
            update {table_name}
            set operation_status=:status,
                secondary_status=:secondary_status,
                list_synced_at=:observed_at,
                detail_synced_at=:observed_at,
                modify_time_utc=:observed_at,
                updated_at=:updated_at
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        {
            "status": local_status,
            "secondary_status": local_secondary_status,
            "observed_at": mutation_observed_at,
            "updated_at": mutation_observed_at,
            "workspace_id": campaign.workspace_id,
            "auth_id": campaign.auth_id,
            "advertiser_id": campaign.advertiser_id,
            "store_id": campaign.store_id,
            "campaign_id": campaign.campaign_id,
        },
    )
    campaign.operation_status = local_status
    campaign.secondary_status = local_secondary_status
    requested_campaign.operation_status = local_status
    requested_campaign.secondary_status = local_secondary_status
    mutation.commit(db)
    if hasattr(response, "model_dump"):
        return response.model_dump(exclude_none=True)
    return {"request_id": getattr(response, "request_id", None)}


async def _apply_status_action(
    db: Session,
    *,
    campaign: CatalogCampaign,
    action: str,
) -> dict[str, Any]:
    with gmvmax_mutation_lease(
        db,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        owner_prefix="smart-guard-status",
        timeout=0.1,
    ) as mutation:
        return await _apply_status_action_unlocked(
            db,
            campaign=campaign,
            action=action,
            mutation=mutation,
        )


def _is_active_campaign_conflict(exc: TTBBusinessError) -> bool:
    message = str(exc).lower()
    return str(getattr(exc, "code", "")) == "40002" and all(
        token in message for token in ("active", "gmv max", "campaign")
    )


def _active_campaign_conflict_backoff(
    strategy: GmvStrategyConfig,
    *,
    guard: Mapping[str, Any],
) -> tuple[int, int]:
    previous = dict(_smart_guard_state(strategy).get("last_decision") or {})
    previous_phase = str(previous.get("decision_phase") or "").upper()
    previous_count = (
        _to_int(previous.get("start_conflict_count"), 0)
        if previous_phase == "START_CONFLICT_HOLD"
        else 0
    )
    conflict_count = min(previous_count + 1, 8)
    base_minutes = max(
        5,
        _to_int(guard.get("active_campaign_conflict_retry_minutes"), 15),
    )
    max_minutes = max(
        base_minutes,
        _to_int(guard.get("max_pause_cooldown_minutes"), 360),
    )
    multiplier = 2 ** min(conflict_count - 1, 4)
    return conflict_count, min(max_minutes, base_minutes * multiplier)


async def _apply_campaign_adjustment_unlocked(
    db: Session,
    *,
    campaign: CatalogCampaign,
    adjustment: Mapping[str, Any],
    current_spend_cents: int = 0,
    mutation: Any,
) -> dict[str, Any]:
    requested_campaign = campaign
    campaign = _reload_catalog_campaign_for_mutation(db, campaign)
    _assert_smart_guard_mutation_allowed(db, campaign)
    update_payload: dict[str, Any] = {"campaign_id": str(campaign.campaign_id)}
    applied_budget_cents: int | None = None
    if adjustment.get("budget") is not None:
        requested_budget_cents = max(
            0,
            _to_int(
                adjustment.get("budget_cents"),
                int(Decimal(str(adjustment["budget"])) * Decimal("100")),
            ),
        )
        minimum_budget_cents = (
            (max(0, int(current_spend_cents)) * 105 + 99) // 100
            if current_spend_cents > 0
            else 0
        )
        applied_budget_cents = max(requested_budget_cents, minimum_budget_cents)
        update_payload["budget"] = float(Decimal(applied_budget_cents) / Decimal("100"))
    if adjustment.get("roas_bid") is not None:
        normalized_roas = _normalize_roas_bid(adjustment["roas_bid"])
        update_payload["roas_bid"] = float(normalized_roas) if normalized_roas is not None else None
    if len(update_payload) <= 1:
        return {"skipped": True, "reason": "empty_adjustment"}

    client = build_ttb_gmvmax_client(db, auth_id=int(campaign.auth_id))
    try:
        request = GMVMaxCampaignUpdateRequest(
            advertiser_id=str(campaign.advertiser_id),
            body=GMVMaxCampaignUpdateBody(**update_payload),
        )
        mutation.assert_current(db)
        response = await client.gmv_max_campaign_update(request)
        mutation.assert_current(db)
        mutation_observed_at = catalog_observation_now()
    finally:
        await client.aclose()

    table_name = (
        "gmvmax_live_campaign_catalog"
        if campaign.promotion_type == "LIVE"
        else "gmvmax_product_campaign_catalog"
    )
    assignments = [
        "list_synced_at=:observed_at",
        "detail_synced_at=:observed_at",
        "modify_time_utc=:observed_at",
        "updated_at=:observed_at",
    ]
    params: dict[str, Any] = {
        "observed_at": mutation_observed_at,
        "workspace_id": campaign.workspace_id,
        "auth_id": campaign.auth_id,
        "advertiser_id": campaign.advertiser_id,
        "store_id": campaign.store_id,
        "campaign_id": campaign.campaign_id,
    }
    if applied_budget_cents is not None:
        assignments.append("budget_cents=:budget_cents")
        params["budget_cents"] = applied_budget_cents
        campaign.budget_value = params["budget_cents"]
        requested_campaign.budget_value = params["budget_cents"]
    if adjustment.get("roas_bid") is not None:
        assignments.append("roas_bid=:roas_bid")
        params["roas_bid"] = str(update_payload["roas_bid"])
        campaign.roas_bid = _to_decimal(update_payload["roas_bid"])
        requested_campaign.roas_bid = campaign.roas_bid
    db.execute(
        text(
            f"""
            update {table_name}
            set {", ".join(assignments)}
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
            """
        ),
        params,
    )
    mutation.commit(db)
    if hasattr(response, "model_dump"):
        return response.model_dump(exclude_none=True)
    return {"request_id": getattr(response, "request_id", None)}


async def _apply_campaign_adjustment(
    db: Session,
    *,
    campaign: CatalogCampaign,
    adjustment: Mapping[str, Any],
    current_spend_cents: int = 0,
) -> dict[str, Any]:
    with gmvmax_mutation_lease(
        db,
        workspace_id=int(campaign.workspace_id),
        auth_id=int(campaign.auth_id),
        owner_prefix="smart-guard-adjust",
        timeout=0.1,
    ) as mutation:
        return await _apply_campaign_adjustment_unlocked(
            db,
            campaign=campaign,
            adjustment=adjustment,
            current_spend_cents=current_spend_cents,
            mutation=mutation,
        )


async def run_smart_guard_cycle(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    cycle_time = now or _utcnow()
    strategies = _load_enabled_strategies(db)
    summary: dict[str, Any] = {
        "strategies": len(strategies),
        "checked": 0,
        "paused": 0,
        "resumed": 0,
        "adjusted": 0,
        "rebuilt": 0,
        "held": 0,
        "stopped": 0,
        "hermes_reviewed": 0,
        "data_conflicts": 0,
        "forced_syncs": 0,
        "manual_override_holds": 0,
        "skipped_not_due": 0,
        "errors": 0,
    }

    for strategy in strategies:
        campaign = _load_catalog_campaign(db, strategy)
        _load_runtime_state(db, strategy, campaign=campaign)
        if not _strategy_due(strategy, cycle_time):
            summary["skipped_not_due"] += 1
            continue
        if campaign is None:
            _update_strategy_state(
                strategy,
                now=cycle_time,
                decision={"action": "SKIP", "reason": "campaign_missing_in_catalog"},
                paused_until=None,
            )
            db.add(strategy)
            db.commit()
            summary["errors"] += 1
            continue

        try:
            if is_manual_pause_override_active(
                db,
                workspace_id=campaign.workspace_id,
                auth_id=campaign.auth_id,
                advertiser_id=campaign.advertiser_id,
                store_id=campaign.store_id,
                campaign_id=campaign.campaign_id,
                now=cycle_time,
            ):
                decision = {
                    "action": "HOLD",
                    "reason": "manual_pause_override",
                    "decision_phase": "MANUAL_OVERRIDE",
                    "monitor_interval_minutes": 1,
                }
                _update_strategy_state(
                    strategy,
                    now=cycle_time,
                    decision=decision,
                    paused_until=None,
                    monitor_interval_minutes=1,
                )
                _persist_runtime_state(db, strategy, campaign=campaign, now=cycle_time)
                _clear_legacy_runtime_config(strategy)
                db.add(strategy)
                db.commit()
                summary["manual_override_holds"] += 1
                summary["held"] += 1
                summary["checked"] += 1
                continue
            metrics = await _fetch_today_metrics(db, campaign)
            data_quality = _assess_realtime_metrics_quality(
                db,
                strategy=strategy,
                campaign=campaign,
                metrics=metrics,
            )
            if metrics.raw is None:
                metrics.raw = {}
            metrics.raw["data_quality"] = data_quality
            if not bool(data_quality.get("valid")):
                reason = f"data_quality:{data_quality.get('reason') or 'invalid_report'}"
                decision = {
                    "action": "HOLD",
                    "reason": reason,
                    "data_quality": data_quality,
                    "monitor_interval_minutes": 1,
                }
                _insert_event(
                    db,
                    strategy=strategy,
                    campaign=campaign,
                    metrics=metrics,
                    action="HOLD",
                    reason=reason,
                    result="SKIPPED",
                    response_json={"data_quality": data_quality},
                    write_learning_sample=False,
                )
                smart_state = _smart_guard_state(strategy)
                _update_strategy_state(
                    strategy,
                    now=cycle_time,
                    decision=decision,
                    paused_until=smart_state.get("paused_until"),
                    monitor_interval_minutes=1,
                )
                _persist_runtime_state(db, strategy, campaign=campaign, now=cycle_time)
                _clear_legacy_runtime_config(strategy)
                db.execute(
                    text(
                        """
                        update gmv_campaign_realtime_state
                        set guard_status='data_hold', last_action='HOLD',
                            last_reason=:reason, last_checked_at=:checked_at,
                            updated_at=:checked_at
                        where workspace_id=:workspace_id
                          and auth_id=:auth_id
                          and advertiser_id=:advertiser_id
                          and store_id=:store_id
                          and campaign_id=:campaign_id
                        """
                    ),
                    {
                        "reason": reason,
                        "checked_at": cycle_time.replace(tzinfo=None),
                        "workspace_id": campaign.workspace_id,
                        "auth_id": campaign.auth_id,
                        "advertiser_id": campaign.advertiser_id,
                        "store_id": campaign.store_id,
                        "campaign_id": campaign.campaign_id,
                    },
                )
                db.add(strategy)
                db.commit()
                summary["held"] += 1
                summary["checked"] += 1
                continue
            decision = _decide(db, strategy=strategy, campaign=campaign, metrics=metrics, now=cycle_time)
            decision["data_quality"] = data_quality
            guard = _guard_config(strategy)
            decision = _prepare_two_stage_decision(
                strategy=strategy,
                campaign=campaign,
                decision=decision,
                now=cycle_time,
            )
            consistency = dict((decision.get("threshold_context") or {}).get("data_consistency") or {})
            if consistency.get("state") == "conflict":
                summary["data_conflicts"] += 1
            _enqueue_conflict_sync(
                db=db,
                strategy=strategy,
                campaign=campaign,
                decision=decision,
                now=cycle_time,
            )
            if decision.get("forced_sync_status") == "enqueued":
                summary["forced_syncs"] += 1

            pre_applied_pause = False
            pre_applied_response: dict[str, Any] | None = None
            proposed_action = str(decision.get("action") or "HOLD").upper()
            if (
                proposed_action == "PAUSE"
                and bool(decision.get("requires_hermes_review"))
                and _status_is_active(campaign.operation_status)
            ):
                try:
                    pre_applied_response = await _apply_status_action(
                        db,
                        campaign=campaign,
                        action="PAUSE",
                    )
                    pre_applied_pause = True
                except Exception as exc:  # noqa: BLE001
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action="PAUSE",
                        reason=str(decision.get("reason") or "protective pause"),
                        result="FAILED",
                        request_json={"decision_phase": "PROTECTION"},
                        error_message=str(exc),
                    )
                    raise

            decision = await _review_two_stage_decision(
                strategy=strategy,
                campaign=campaign,
                metrics=metrics,
                decision=decision,
                now=cycle_time,
            )
            if decision.get("hermes_review"):
                summary["hermes_reviewed"] += 1
            monitor_interval = _dynamic_monitor_interval_minutes(
                db,
                campaign=campaign,
                metrics=metrics,
                guard=guard,
                order_timing=(decision.get("threshold_context") or {}).get("order_timing"),
            )
            decision_phase = str(decision.get("decision_phase") or "").upper()
            current_test_state = dict(_smart_guard_state(strategy).get("controlled_test") or {})
            if decision_phase.startswith("CONTROLLED_TEST") or bool(current_test_state.get("active")):
                monitor_interval = max(
                    1,
                    _to_int(guard.get("controlled_test_monitor_interval_minutes"), 1),
                )
            action = str(decision.get("action") or "HOLD").upper()
            reason = str(decision.get("reason") or "")
            response_payload: dict[str, Any] | None = None
            request_payload: dict[str, Any] | None = None

            if action in {"PAUSE", "START"}:
                request_payload = {
                    "campaign_id": campaign.campaign_id,
                    "action": action,
                    "operation_status": "DISABLE" if action == "PAUSE" else "ENABLE",
                    "threshold_context": decision.get("threshold_context"),
                    "disable_strategy": bool(decision.get("disable_strategy")),
                    "decision_phase": decision.get("decision_phase"),
                    "hermes_review": decision.get("hermes_review"),
                }
                try:
                    if action == "START" and decision.get("controlled_test_budget_cents") is not None:
                        test_budget = max(100, _to_int(decision.get("controlled_test_budget_cents"), 0))
                        current_budget = _budget_to_cents(campaign.budget_value)
                        controlled_budget = max(
                            _to_int(guard.get("controlled_test_budget_floor_cents"), 2000),
                            metrics.cost_cents + test_budget,
                        )
                        controlled_adjustment = {
                            "budget_cents": controlled_budget,
                            "budget": float(Decimal(controlled_budget) / Decimal("100")),
                        }
                        if controlled_budget != current_budget:
                            budget_response = await _apply_campaign_adjustment(
                                db,
                                campaign=campaign,
                                adjustment=controlled_adjustment,
                                current_spend_cents=metrics.cost_cents,
                            )
                        else:
                            budget_response = {
                                "skipped": True,
                                "reason": "existing_budget_matches_controlled_test_cap",
                            }
                        test_update = dict(decision.get("controlled_test_update") or {})
                        decision["controlled_test_update"] = {
                            **test_update,
                            "platform_budget_cents": controlled_budget,
                        }
                        request_payload["controlled_test_budget_cents"] = test_budget
                        request_payload["controlled_test_adjustment"] = controlled_adjustment
                        request_payload["controlled_test_response"] = budget_response
                    elif action == "START" and decision.get("pre_start_budget_multiplier") is not None:
                        current_budget = _budget_to_cents(campaign.budget_value)
                        multiplier = Decimal(str(decision.get("pre_start_budget_multiplier")))
                        controlled_budget = max(
                            _to_int(guard.get("controlled_test_budget_floor_cents"), 2000),
                            int(Decimal(current_budget) * multiplier),
                        )
                        controlled_adjustment = {
                            "budget_cents": controlled_budget,
                            "budget": float(Decimal(controlled_budget) / Decimal("100")),
                        }
                        budget_response = await _apply_campaign_adjustment(
                            db,
                            campaign=campaign,
                            adjustment=controlled_adjustment,
                            current_spend_cents=metrics.cost_cents,
                        )
                        request_payload["controlled_test_adjustment"] = controlled_adjustment
                        request_payload["controlled_test_response"] = budget_response
                    response_payload = (
                        pre_applied_response
                        if action == "PAUSE" and pre_applied_pause
                        else await _apply_status_action(db, campaign=campaign, action=action)
                    )
                    if action == "PAUSE" and bool(decision.get("disable_strategy")):
                        strategy.enabled = False
                        summary["stopped"] += 1
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action=action,
                        reason=reason,
                        result="SUCCESS",
                        request_json=request_payload,
                        response_json=response_payload,
                    )
                    if action == "PAUSE":
                        summary["paused"] += 1
                    else:
                        summary["resumed"] += 1
                except TTBBusinessError as exc:
                    if action != "START" or not _is_active_campaign_conflict(exc):
                        _insert_event(
                            db,
                            strategy=strategy,
                            campaign=campaign,
                            metrics=metrics,
                            action=action,
                            reason=reason,
                            result="FAILED",
                            request_json=request_payload,
                            error_message=str(exc),
                        )
                        raise

                    conflict_count, retry_minutes = _active_campaign_conflict_backoff(
                        strategy,
                        guard=guard,
                    )
                    paused_until = (cycle_time + timedelta(minutes=retry_minutes)).isoformat()
                    reason = (
                        "smart_guard: resume deferred because the product is occupied "
                        "by another active GMV Max campaign"
                    )
                    action = "HOLD"
                    monitor_interval = retry_minutes
                    decision.update(
                        {
                            "action": action,
                            "reason": reason,
                            "paused_until": paused_until,
                            "decision_phase": "START_CONFLICT_HOLD",
                            "start_conflict_count": conflict_count,
                            "start_conflict_retry_minutes": retry_minutes,
                        }
                    )
                    if request_payload is not None:
                        request_payload["platform_error_code"] = getattr(exc, "code", None)
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action=action,
                        reason=reason,
                        result="SKIPPED",
                        request_json=request_payload,
                        response_json={
                            "platform_error_code": getattr(exc, "code", None),
                            "retry_minutes": retry_minutes,
                            "conflict_count": conflict_count,
                        },
                        write_learning_sample=False,
                    )
                    summary["held"] += 1
                except Exception as exc:  # noqa: BLE001
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action=action,
                        reason=reason,
                        result="FAILED",
                        request_json=request_payload,
                        error_message=str(exc),
                    )
                    raise
            elif action == "REBUILD":
                from app.services.gmvmax_creative_guard import (
                    rebuild_campaign_for_delivery_failure,
                )

                current_roas = campaign.roas_bid or _to_decimal(strategy.min_roi, "0.8") or Decimal("0.8")
                minimum_roas = _to_decimal(strategy.min_roi, "0.6") or Decimal("0.6")
                rebuild_roas = max(
                    _normalize_roas_bid(minimum_roas, rounding=ROUND_DOWN) or Decimal("0.1"),
                    _normalize_roas_bid(current_roas * Decimal("0.90"), rounding=ROUND_DOWN)
                    or Decimal("0.1"),
                )
                request_payload = {
                    "campaign_id": campaign.campaign_id,
                    "action": "RESET_CAMPAIGN",
                    "failure_class": decision.get("failure_class"),
                    "rebuild_roas_bid": str(rebuild_roas),
                }
                try:
                    rebuild_request, response_payload = await rebuild_campaign_for_delivery_failure(
                        db,
                        strategy_id=int(strategy.id),
                        reason="creative_guard:no_spend_timeout",
                        context={
                            "source": "smart_guard",
                            "failure_class": decision.get("failure_class"),
                            "rebuild_roas_bid": str(rebuild_roas),
                            "rebuild_limit_24h": _to_int(
                                guard.get("controlled_test_rebuild_limit_24h"), 2
                            ),
                        },
                    )
                    request_payload.update(rebuild_request)
                    deferred = bool((response_payload or {}).get("rebuild_deferred"))
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action="RESET_CAMPAIGN",
                        reason=reason,
                        result="SKIPPED" if deferred else "SUCCESS",
                        request_json=request_payload,
                        response_json=response_payload,
                    )
                    if deferred:
                        action = "HOLD"
                        decision["action"] = "HOLD"
                        decision["reason"] = "smart_guard: rebuild circuit open; recovery deferred"
                        decision["paused_until"] = (
                            cycle_time
                            + timedelta(
                                minutes=max(
                                    60,
                                    _to_int(guard.get("max_pause_cooldown_minutes"), 360),
                                )
                            )
                        ).isoformat()
                        decision["controlled_test_update"] = {
                            **dict(decision.get("controlled_test_update") or {}),
                            "active": False,
                            "status": "REBUILD_CIRCUIT_OPEN",
                            "rebuild_pending": True,
                        }
                        summary["held"] += 1
                    else:
                        decision["controlled_test_update"] = {
                            **dict(decision.get("controlled_test_update") or {}),
                            "active": False,
                            "status": "REBUILT",
                            "rebuild_pending": False,
                            "completed_at": cycle_time.isoformat(),
                        }
                        summary["rebuilt"] += 1
                except Exception as exc:  # noqa: BLE001
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action="RESET_CAMPAIGN",
                        reason=reason,
                        result="FAILED",
                        request_json=request_payload,
                        error_message=str(exc),
                    )
                    non_retryable = type(exc).__name__ == "TTBBusinessError"
                    retry_minutes = 30 if non_retryable else 5
                    action = "HOLD"
                    reason = f"smart_guard: campaign rebuild failed ({type(exc).__name__}); retry deferred"
                    decision.update(
                        {
                            "action": "HOLD",
                            "reason": reason,
                            "paused_until": (cycle_time + timedelta(minutes=retry_minutes)).isoformat(),
                            "decision_phase": "REBUILD_FAILED",
                            "controlled_test_update": {
                                **dict(decision.get("controlled_test_update") or {}),
                                "active": False,
                                "status": "REBUILD_FAILED",
                                "rebuild_pending": True,
                                "last_error": str(exc)[:500],
                                "last_error_at": cycle_time.isoformat(),
                                "retry_after_minutes": retry_minutes,
                            },
                        }
                    )
                    monitor_interval = retry_minutes
                    summary["errors"] += 1
                    summary["held"] += 1
            elif action == "ADJUST":
                adjustment = dict(decision.get("adjustment") or {})
                request_payload = {
                    "campaign_id": campaign.campaign_id,
                    "action": action,
                    "adjustment": adjustment,
                    "threshold_context": decision.get("threshold_context"),
                }
                try:
                    response_payload = await _apply_campaign_adjustment(
                        db,
                        campaign=campaign,
                        adjustment=adjustment,
                        current_spend_cents=metrics.cost_cents,
                    )
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action=action,
                        reason=reason,
                        result="SUCCESS",
                        request_json=request_payload,
                        response_json=response_payload,
                    )
                    summary["adjusted"] += 1
                except Exception as exc:  # noqa: BLE001
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action=action,
                        reason=reason,
                        result="FAILED",
                        request_json=request_payload,
                        error_message=str(exc),
                    )
                    raise
            else:
                summary["held"] += 1
                if decision.get("hermes_review") or decision.get("force_sync"):
                    _insert_event(
                        db,
                        strategy=strategy,
                        campaign=campaign,
                        metrics=metrics,
                        action="HOLD",
                        reason=reason,
                        result="SKIPPED",
                        request_json={
                            "decision_phase": decision.get("decision_phase"),
                            "proposed_action": decision.get("proposed_action"),
                            "threshold_context": decision.get("threshold_context"),
                        },
                        response_json={
                            "hermes_review": decision.get("hermes_review"),
                            "forced_sync_task_id": decision.get("forced_sync_task_id"),
                        },
                    )

            paused_until = decision.get("paused_until")
            _insert_smart_decision_sample(
                db,
                campaign=campaign,
                metrics=metrics,
                decision=decision,
                monitor_interval=monitor_interval,
            )
            _update_strategy_state(
                strategy,
                now=cycle_time,
                decision={
                    **decision,
                    "monitor_interval_minutes": monitor_interval,
                    "metrics": {
                        "cost": metrics.cost_cents / 100,
                        "gmv": metrics.gross_revenue_cents / 100,
                        "orders": metrics.orders,
                        "roi": float(metrics.roi) if metrics.roi is not None else None,
                    },
                },
                paused_until=paused_until,
                monitor_interval_minutes=monitor_interval,
            )
            _upsert_realtime_state(
                db,
                strategy=strategy,
                campaign=campaign,
                metrics=metrics,
                now=cycle_time,
                guard_status="active",
                last_action=action,
                reason=reason,
                paused_until=paused_until,
            )
            _clear_legacy_runtime_config(strategy)
            db.add(strategy)
            db.commit()
            summary["checked"] += 1
        except (GmvMaxMutationBusy, GmvMaxMutationFenceLost) as exc:
            db.rollback()
            logger.warning(
                "gmvmax smart guard mutation held for the next cycle",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "auth_id": strategy.auth_id,
                    "campaign_id": strategy.campaign_id,
                    "reason": str(exc),
                },
            )
            summary["held"] += 1
            summary["checked"] += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception(
                "gmvmax realtime smart guard failed",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "auth_id": strategy.auth_id,
                    "campaign_id": strategy.campaign_id,
                },
            )
            summary["errors"] += 1

    return summary


def run_smart_guard_cycle_sync(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    return asyncio.run(run_smart_guard_cycle(db, now=now))
