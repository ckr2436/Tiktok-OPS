from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.gmvmax_hermes_memory import load_strategy_memory
from app.services.hermes_agent.client import HermesAdsRealtimeClient, extract_output_text

logger = logging.getLogger("gmv.services.gmvmax.hermes_advisor")

_REALTIME_PROMPT_VERSION = "gmvmax_realtime_advisor_v2"
_REALTIME_REVIEW_MIN_INTERVAL = timedelta(minutes=30)
_ACTION_REVIEW_PROMPT_VERSION = "gmvmax_action_review_v2"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))


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


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _clamp_decimal(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


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


def _extract_json_object(text_value: str) -> dict[str, Any]:
    text_value = str(text_value or "").strip()
    if not text_value:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text_value, flags=re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text_value[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _sanitize_realtime_review(raw: Mapping[str, Any]) -> dict[str, Any]:
    decision = str(raw.get("decision") or "").strip().upper()
    if decision not in {"APPLY", "REVISE", "HOLD"}:
        return {}
    confidence_value = raw.get("confidence")
    numeric_confidence = _to_decimal(confidence_value)
    if numeric_confidence is not None:
        if numeric_confidence >= Decimal("0.8"):
            confidence = "high"
        elif numeric_confidence >= Decimal("0.5"):
            confidence = "medium"
        else:
            confidence = "low"
    else:
        confidence = str(confidence_value or "low").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
    overrides_raw = _dict(raw.get("parameter_overrides"))
    overrides: dict[str, Any] = {}
    if overrides_raw.get("monitor_interval_minutes") is not None:
        overrides["monitor_interval_minutes"] = _clamp_int(
            _to_int(overrides_raw.get("monitor_interval_minutes"), 3), 1, 5
        )
    if overrides_raw.get("pause_cooldown_minutes") is not None:
        overrides["pause_cooldown_minutes"] = _clamp_int(
            _to_int(overrides_raw.get("pause_cooldown_minutes"), 30), 30, 60
        )
    if overrides_raw.get("min_spend_cents") is not None:
        overrides["min_spend_cents"] = _clamp_int(
            _to_int(overrides_raw.get("min_spend_cents"), 300), 300, 3000
        )
    if overrides_raw.get("min_roi") is not None:
        overrides["min_roi"] = float(
            _clamp_decimal(
                _to_decimal(overrides_raw.get("min_roi"), "0.8") or Decimal("0.8"),
                Decimal("0.6"),
                Decimal("1.2"),
            )
        )
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(raw.get("reason") or "")[:1000],
        "risk_flags": [str(item)[:200] for item in raw.get("risk_flags") or [] if str(item).strip()][:10],
        "parameter_overrides": overrides,
    }


def _sanitize_action_review(
    raw: Mapping[str, Any],
    *,
    test_budget_min_cents: int | None = None,
    test_budget_max_cents: int | None = None,
) -> dict[str, Any]:
    decision = str(raw.get("decision") or "").strip().upper()
    if decision not in {"APPROVE", "REVISE", "HOLD"}:
        return {}
    confidence_value = _to_decimal(raw.get("confidence"))
    if confidence_value is None:
        confidence = str(raw.get("confidence") or "low").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
    elif confidence_value >= Decimal("0.8"):
        confidence = "high"
    elif confidence_value >= Decimal("0.5"):
        confidence = "medium"
    else:
        confidence = "low"
    review_after = raw.get("review_after_minutes")
    budget_multiplier = _to_decimal(raw.get("budget_multiplier"))
    test_budget = raw.get("test_budget_cents")
    sanitized_test_budget = None
    if test_budget is not None:
        low = max(1, _to_int(test_budget_min_cents, 1))
        high = max(low, _to_int(test_budget_max_cents, low))
        sanitized_test_budget = _clamp_int(_to_int(test_budget, low), low, high)
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(raw.get("reason") or "")[:1000],
        "risk_flags": [
            str(item)[:200]
            for item in raw.get("risk_flags") or []
            if str(item).strip()
        ][:10],
        "review_after_minutes": (
            _clamp_int(_to_int(review_after, 15), 10, 360)
            if review_after is not None
            else None
        ),
        "budget_multiplier": (
            float(_clamp_decimal(budget_multiplier, Decimal("0.2"), Decimal("1.0")))
            if budget_multiplier is not None
            else None
        ),
        "test_budget_cents": sanitized_test_budget,
    }


async def review_smart_guard_action(
    *,
    campaign: Mapping[str, Any],
    metrics: Mapping[str, Any],
    proposed_decision: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Review a high-impact action without granting the model unbounded control."""

    payload = {
        "input_meta": {
            "current_time_utc": _utcnow().isoformat(),
            "timestamp_timezone": "UTC",
            "prompt_version": _ACTION_REVIEW_PROMPT_VERSION,
        },
        "campaign": dict(campaign),
        "metrics": dict(metrics),
        "proposed_decision": dict(proposed_decision),
        "evidence": dict(evidence),
    }
    instructions = (
        "你是 GMV Max 高影响动作审批器。规则引擎已经负责临时保护暂停，你负责审批长冷却、恢复和扩量。"
        "只输出合法 JSON 对象，字段必须为 decision、confidence、reason、risk_flags、"
        "review_after_minutes、budget_multiplier、test_budget_cents。decision 只能是 APPROVE、REVISE、HOLD。"
        "若 evidence.data_consistency.state 为 conflict、当前系列报表不新鲜、归因仍在宽限期或证据互相矛盾，必须 HOLD，"
        "并给出 10 到 30 分钟的 review_after_minutes。"
        "PAUSE 表示审批是否延长保护暂停；START 表示审批是否恢复；ADJUST 表示审批预算或出价变更；"
        "HOLD_EXTENSION 表示审批继续冷却。"
        "恢复要求同一决策快照数据一致，且不能仅因冷却倒计时结束就批准。"
        "暂停状态下 recent_momentum_missing_or_stale 是预期现象；当 recovery_control.eligible=true 时，"
        "不能仅因暂停前 ROI 低、历史失败或近期动量陈旧而无限 HOLD，必须批准或修订一次受控小预算测试。"
        "此时必须在 recovery_control.test_budget_bounds 的 min_cents 和 max_cents 之间给出 test_budget_cents，"
        "历史越差、连续失败越多越靠近下限；review_after_minutes 表示测试观察窗口，建议 10 到 60 分钟。"
        "REVISE 可缩短复核时间；普通恢复仍可用 0.2 到 1.0 的 budget_multiplier 建议降预算验证。"
        "不得输出具体 API、素材排除、永久停用、计划重建或超出输入边界的动作。"
    )
    client = HermesAdsRealtimeClient()
    response, latency_ms = await client.create_response(
        input_text=_json_dumps(payload),
        instructions=instructions,
        metadata={
            "source": "gmvmax_action_review",
            "prompt_version": _ACTION_REVIEW_PROMPT_VERSION,
            "campaign_id": str(campaign.get("campaign_id") or ""),
            "action": str(proposed_decision.get("action") or ""),
        },
    )
    recovery_control = _dict(evidence.get("recovery_control"))
    budget_bounds = _dict(recovery_control.get("test_budget_bounds"))
    review = _sanitize_action_review(
        _extract_json_object(extract_output_text(response)),
        test_budget_min_cents=_to_int(budget_bounds.get("min_cents"), 1),
        test_budget_max_cents=_to_int(budget_bounds.get("max_cents"), 1),
    )
    if not review:
        return {
            "status": "invalid_response",
            "decision": "HOLD",
            "confidence": "low",
            "reason": "Hermes returned an invalid action review.",
            "risk_flags": ["invalid_model_response"],
            "review_after_minutes": 15,
            "budget_multiplier": None,
            "test_budget_cents": None,
            "latency_ms": latency_ms,
            "reviewed_at": _utcnow().isoformat(),
            "prompt_version": _ACTION_REVIEW_PROMPT_VERSION,
            "model": "gmv-ops-hermes-ads-realtime",
        }
    return {
        **review,
        "status": "reviewed",
        "latency_ms": latency_ms,
        "reviewed_at": _utcnow().isoformat(),
        "prompt_version": _ACTION_REVIEW_PROMPT_VERSION,
        "model": "gmv-ops-hermes-ads-realtime",
    }


def _realtime_review_required(recommendation: Mapping[str, Any]) -> bool:
    stats = _dict(recommendation.get("stats_24h"))
    samples = _to_int(stats.get("samples"), 0)
    if samples < 2:
        return False
    cost_cents = _to_int(stats.get("latest_cost_cents"), 0)
    orders = _to_int(stats.get("latest_orders"), 0)
    pauses = _to_int(stats.get("pauses"), 0)
    allowed_cpa = _to_int(recommendation.get("allowed_cpa_cents"), 0)
    no_order_trigger = max(500, min(allowed_cpa or 1000, 3000))
    return pauses >= 2 or (orders <= 0 and cost_cents >= no_order_trigger)


def _last_realtime_review(db: Session, scope: Mapping[str, Any]) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            select recommendation_json
            from gmv_hermes_ad_recommendations
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and recommendation_type='GMVMAX_STRATEGY'
            limit 1
            """
        ),
        {
            "workspace_id": scope["workspace_id"],
            "auth_id": scope["auth_id"],
            "advertiser_id": scope["advertiser_id"],
            "store_id": scope["store_id"],
            "campaign_id": scope["campaign_id"],
        },
    ).mappings().first()
    return _dict(_dict(row or {}).get("recommendation_json"))


def _realtime_review_due(db: Session, scope: Mapping[str, Any]) -> bool:
    previous = _dict(_last_realtime_review(db, scope).get("realtime_review"))
    if str(previous.get("prompt_version") or "") != _REALTIME_PROMPT_VERSION:
        return True
    reviewed_at = str(previous.get("reviewed_at") or "").strip()
    if not reviewed_at:
        return True
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return _utcnow() - parsed.astimezone(timezone.utc) >= _REALTIME_REVIEW_MIN_INTERVAL


async def _request_realtime_review(
    scope: Mapping[str, Any], recommendation: Mapping[str, Any]
) -> tuple[dict[str, Any], int]:
    payload = {
        "input_meta": {
            "current_time_utc": _utcnow().isoformat(),
            "timestamp_timezone": "UTC",
            "latest_values_are_one_coherent_cumulative_snapshot": True,
            "sampled_average_roi_is_not_a_period_total": True,
        },
        "campaign": {
            "campaign_id": str(scope.get("campaign_id") or ""),
            "campaign_name": str(scope.get("campaign_name") or "")[:200],
            "status": str(scope.get("operation_status") or ""),
            "secondary_status": str(scope.get("secondary_status") or ""),
            "budget_cents": _to_int(scope.get("budget_cents"), 0),
            "roas_bid": float(_to_decimal(scope.get("roas_bid"), "0") or Decimal("0")),
        },
        "evidence": {
            "stats_24h": _dict(recommendation.get("stats_24h")),
            "stats_7d": _dict(recommendation.get("stats_7d")),
            "price_basis": _dict(recommendation.get("price_basis")),
            "allowed_cpa_cents": recommendation.get("allowed_cpa_cents"),
            "strategy_memory": _dict(recommendation.get("strategy_memory")),
        },
        "bounded_baseline": {
            "smart_guard": _dict(recommendation.get("smart_guard")),
            "creative_guard": _dict(recommendation.get("creative_guard")),
        },
    }
    instructions = (
        "你是 GMV Max 实时风险参数审批器，不直接暂停、恢复、重建或排除素材。"
        "只审核输入中的 bounded_baseline 是否可以安全应用。只输出合法 JSON 对象，"
        "字段必须为 decision、confidence、reason、risk_flags、parameter_overrides。"
        "decision 只能是 APPLY、REVISE、HOLD。证据陈旧、冲突或样本不足时必须 HOLD。"
        "所有时间均为UTC；latest字段来自同一条最新累计快照。24小时和7天窗口共享同一最新快照并不构成冲突。"
        "campaign为DISABLE不应单独导致HOLD，参数可供下次恢复时使用。"
        "parameter_overrides 只允许 monitor_interval_minutes、pause_cooldown_minutes、"
        "min_spend_cents、min_roi；不得输出预算、出价或任何广告执行动作。"
    )
    client = HermesAdsRealtimeClient()
    response, latency_ms = await client.create_response(
        input_text=_json_dumps(payload),
        instructions=instructions,
        metadata={
            "source": "gmvmax_realtime_advisor",
            "prompt_version": _REALTIME_PROMPT_VERSION,
            "campaign_id": str(scope.get("campaign_id") or ""),
        },
    )
    review = _sanitize_realtime_review(_extract_json_object(extract_output_text(response)))
    return review, latency_ms


def _attach_realtime_review(
    db: Session,
    *,
    scope: Mapping[str, Any],
    recommendation: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    next_recommendation = dict(recommendation)
    if not _realtime_review_required(recommendation):
        next_recommendation["realtime_review"] = {"status": "not_required"}
        return next_recommendation, True
    if not _realtime_review_due(db, scope):
        previous = _dict(_last_realtime_review(db, scope).get("realtime_review"))
        next_recommendation["realtime_review"] = {**previous, "status": "rate_limited"}
        return next_recommendation, str(previous.get("decision") or "APPLY") != "HOLD"
    reviewed_at = _utcnow().isoformat()
    try:
        review, latency_ms = asyncio.run(_request_realtime_review(scope, recommendation))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Realtime Hermes review unavailable; bounded baseline remains authoritative",
            extra={"campaign_id": scope.get("campaign_id"), "error_type": type(exc).__name__},
        )
        next_recommendation["realtime_review"] = {
            "status": "unavailable",
            "reviewed_at": reviewed_at,
            "prompt_version": _REALTIME_PROMPT_VERSION,
            "error_type": type(exc).__name__,
        }
        return next_recommendation, True
    if not review:
        next_recommendation["realtime_review"] = {
            "status": "invalid_response",
            "reviewed_at": reviewed_at,
            "latency_ms": latency_ms,
            "prompt_version": _REALTIME_PROMPT_VERSION,
        }
        return next_recommendation, True
    review_meta = {
        **review,
        "status": "reviewed",
        "reviewed_at": reviewed_at,
        "latency_ms": latency_ms,
        "model": "gmv-ops-hermes-ads-realtime",
        "prompt_version": _REALTIME_PROMPT_VERSION,
    }
    next_recommendation["realtime_review"] = review_meta
    if review["decision"] == "REVISE":
        smart = _dict(next_recommendation.get("smart_guard"))
        smart.update(_dict(review.get("parameter_overrides")))
        next_recommendation["smart_guard"] = smart
    return next_recommendation, review["decision"] != "HOLD"


def _load_hermes_scopes(db: Session) -> list[dict[str, Any]]:
    # Process every enabled scope. The cycle isolates failures per scope, while
    # a fixed oldest-N prefix lets HOLD/non-auto-apply/error rows starve the
    # scopes behind them indefinitely.
    rows = db.execute(
        text(
            """
            select s.id as strategy_id, s.workspace_id, s.auth_id, s.campaign_id,
                   s.enabled, s.target_roi, s.min_roi, s.cooldown_minutes,
                   s.config_json,
                   c.advertiser_id, c.store_id, c.campaign_name, c.operation_status,
                   c.secondary_status, c.budget_cents, c.roas_bid
            from gmv_strategy_configs s
            join gmvmax_product_campaign_catalog c
              on c.workspace_id=s.workspace_id
             and c.auth_id=s.auth_id
             and c.campaign_id=s.campaign_id
             and c.advertiser_id is not null
             and c.advertiser_id <> ''
             and c.store_id is not null
             and c.store_id <> ''
            where s.enabled=1
              and (
                select count(*)
                from gmvmax_product_campaign_catalog candidate
                where candidate.workspace_id=s.workspace_id
                  and candidate.auth_id=s.auth_id
                  and candidate.campaign_id=s.campaign_id
              ) = 1
              and (
                json_extract(coalesce(s.config_json, json_object()), '$.hermes_enabled') = true
                or json_extract(coalesce(s.config_json, json_object()), '$.smart_guard.hermes_enabled') = true
              )
            order by s.updated_at asc, s.id asc
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _sample_stats(db: Session, scope: Mapping[str, Any], *, hours: int) -> dict[str, Any]:
    params = {
        "workspace_id": scope["workspace_id"],
        "auth_id": scope["auth_id"],
        "advertiser_id": scope["advertiser_id"],
        "store_id": scope["store_id"],
        "campaign_id": scope["campaign_id"],
        "since_at": (_utcnow() - timedelta(hours=hours)).replace(tzinfo=None),
    }
    aggregate = db.execute(
        text(
            """
            select count(*) as samples,
                   sum(case when action='PAUSE' then 1 else 0 end) as pauses,
                   sum(case when action='START' then 1 else 0 end) as starts,
                   sum(case when action in ('REMOVE','RESET_CAMPAIGN') then 1 else 0 end) as creative_actions,
                   avg(nullif(roi, 0)) as avg_sampled_positive_roi
            from gmv_hermes_ad_learning_samples
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and observed_at >= :since_at
            """
        ),
        params,
    ).mappings().first()
    latest = db.execute(
        text(
            """
            select cost_cents as latest_cost_cents,
                   gross_revenue_cents as latest_gmv_cents,
                   orders as latest_orders,
                   roi as latest_roi,
                   action as latest_action,
                   observed_at as latest_observed_at
            from gmv_hermes_ad_learning_samples
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and campaign_id=:campaign_id
              and observed_at >= :since_at
            order by observed_at desc, id desc
            limit 1
            """
        ),
        params,
    ).mappings().first()
    result = {**dict(aggregate or {}), **dict(latest or {})}
    result["window_hours"] = int(hours)
    observed_at = result.pop("latest_observed_at", None)
    if isinstance(observed_at, datetime):
        result["latest_observed_at_utc"] = observed_at.replace(tzinfo=timezone.utc).isoformat()
    elif observed_at:
        result["latest_observed_at_utc"] = str(observed_at)
    return result


def _price_basis(db: Session, scope: Mapping[str, Any]) -> dict[str, Any]:
    rows = db.execute(
        text(
            """
            select p.product_id, p.effective_price, p.price, p.min_price, p.max_price
            from ttb_products p
            where p.workspace_id=:workspace_id
              and p.auth_id=:auth_id
              and p.store_id=:store_id
              and p.product_id in (
                select item_group_id
                from gmvmax_product_campaign_item_groups
                where workspace_id=:workspace_id
                  and auth_id=:auth_id
                  and advertiser_id=:advertiser_id
                  and store_id=:store_id
                  and campaign_id=:campaign_id
              )
            """
        ),
        {
            "workspace_id": scope["workspace_id"],
            "auth_id": scope["auth_id"],
            "advertiser_id": scope["advertiser_id"],
            "store_id": scope["store_id"],
            "campaign_id": scope["campaign_id"],
        },
    ).mappings().all()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        for key in ("effective_price", "price", "min_price", "max_price"):
            value = _to_decimal(row.get(key))
            if value and value > 0:
                cents = int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                candidates.append(
                    {
                        "item_group_id": str(row.get("product_id")),
                        "cents": cents,
                        "source": f"ttb_products.{key}",
                    }
                )
                break
    if not candidates:
        return {"cents": None, "source": "missing_product_price", "products": []}
    selected = min(candidates, key=lambda item: int(item["cents"]))
    return {**selected, "products": candidates}


def _target_roas(scope: Mapping[str, Any], config: Mapping[str, Any]) -> Decimal:
    smart = _dict(config.get("smart_guard"))
    for candidate in (smart.get("target_roas"), scope.get("target_roi"), scope.get("roas_bid"), "1.2"):
        value = _to_decimal(candidate)
        if value and value > 0:
            return value
    return Decimal("1.2")


def _build_recommendation(scope: Mapping[str, Any]) -> dict[str, Any]:
    config = _dict(scope.get("config_json"))
    stats_24h = _dict(scope.get("stats_24h"))
    stats_7d = _dict(scope.get("stats_7d"))
    price = _dict(scope.get("price_basis"))
    strategy_memory = _dict(scope.get("strategy_memory"))
    validated_memory = [item for item in strategy_memory.get("validated") or [] if isinstance(item, Mapping)]
    memory_fact = max(
        validated_memory,
        key=lambda item: float(item.get("confidence") or 0),
        default=None,
    )
    memory_evidence = _dict(memory_fact.get("evidence")) if memory_fact else {}
    memory_params = _dict(memory_evidence.get("best_success_params"))
    target_roas = _target_roas(scope, config)
    price_cents = _to_int(price.get("cents"), 0)
    allowed_cpa = None
    if price_cents > 0:
        allowed_cpa = max(
            300,
            int((Decimal(price_cents) / target_roas).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        )

    pauses_24h = _to_int(stats_24h.get("pauses"), 0)
    actions_24h = _to_int(stats_24h.get("creative_actions"), 0)
    latest_cost = _to_int(stats_24h.get("latest_cost_cents"), 0)
    budget_cents = _to_int(scope.get("budget_cents"), 0)
    spend_share = Decimal(latest_cost) / Decimal(budget_cents) if budget_cents > 0 else Decimal("0")
    monitor_interval = 1 if spend_share >= Decimal("0.08") or pauses_24h > 0 else 3
    if latest_cost <= 0:
        monitor_interval = 5
    if memory_fact and memory_params.get("monitor_interval_minutes") is not None:
        monitor_interval = min(
            monitor_interval,
            _clamp_int(_to_int(memory_params.get("monitor_interval_minutes"), monitor_interval), 1, 5),
        )

    current_min_roi = _to_decimal(_dict(config.get("smart_guard")).get("min_roi"), "0.8") or Decimal("0.8")
    avg_positive_roi = _to_decimal(stats_7d.get("avg_sampled_positive_roi"))
    validated_roi = _to_decimal(memory_evidence.get("weighted_roi"))
    if memory_fact and validated_roi and validated_roi > 0:
        proposed_min_roi = _clamp_decimal(validated_roi * Decimal("0.80"), Decimal("0.6"), Decimal("1.2"))
    elif avg_positive_roi and avg_positive_roi > 0:
        proposed_min_roi = _clamp_decimal(avg_positive_roi * Decimal("0.80"), Decimal("0.6"), Decimal("1.2"))
    else:
        proposed_min_roi = _clamp_decimal(current_min_roi, Decimal("0.6"), Decimal("1.0"))

    cooldown = 30 if pauses_24h <= 3 else 45
    if memory_fact and memory_params.get("cooldown_minutes") is not None:
        learned_cooldown = _clamp_int(_to_int(memory_params.get("cooldown_minutes"), cooldown), 30, 60)
        cooldown = max(cooldown, learned_cooldown) if pauses_24h > 3 else learned_cooldown
    smart_updates = {
        "hermes_enabled": True,
        "hermes_auto_apply": True,
        "monitor_interval_minutes": monitor_interval,
        "fast_monitor_interval_minutes": 1,
        "slow_monitor_interval_minutes": 5,
        "pause_cooldown_minutes": cooldown,
        "min_spend_cents": 300,
        "min_roi": float(proposed_min_roi),
        "use_effective_product_price": True,
    }
    if price.get("item_group_id") and price_cents > 0:
        smart_updates["product_effective_prices"] = {str(price["item_group_id"]): price_cents / 100}

    creative_updates = {
        "enabled": True,
        "monitor_interval_minutes": monitor_interval,
        "fast_monitor_interval_minutes": 1,
        "slow_monitor_interval_minutes": 5,
        "use_effective_product_price": True,
        "product_card_reset": {
            "enabled": True,
            "recreate": True,
            "disable_old_strategy": True,
            "protect_good_campaign": True,
            "min_product_card_spend_share": "0.35",
            "campaign_roi_floor": "0.8",
            "video_roi_floor": "1.0",
            "campaign_min_orders": 1,
            "video_min_orders": 1,
        },
        "no_order_budget_share_floor": "0.0",
        "no_order_min_spend_cents": 300,
    }
    if price.get("item_group_id") and price_cents > 0:
        creative_updates["product_effective_prices"] = {str(price["item_group_id"]): price_cents / 100}

    confidence = "low"
    if memory_fact:
        memory_confidence = _to_decimal(memory_fact.get("confidence"), "0") or Decimal("0")
        independent_days = _to_int(memory_fact.get("independent_days"), 0)
        confidence = "high" if memory_confidence >= Decimal("0.8") and independent_days >= 5 else "medium"

    return {
        "version": "hermes_gmvmax_advisor_v2",
        "confidence": confidence,
        "reason": (
            "derived_from_validated_mysql_memory_guard_samples_and_effective_price"
            if memory_fact
            else "derived_from_guard_samples_and_effective_price"
        ),
        "price_basis": price,
        "stats_24h": stats_24h,
        "stats_7d": stats_7d,
        "allowed_cpa_cents": allowed_cpa,
        "strategy_memory": {
            "applied": bool(memory_fact),
            "fact": dict(memory_fact) if memory_fact else None,
            "candidate_count": len(strategy_memory.get("candidates") or []),
        },
        "smart_guard": smart_updates,
        "creative_guard": creative_updates,
        "notes": [
            "Hermes advisor applies only bounded strategy settings.",
            "TikTok status/budget actions remain executed by Smart Guard and Creative Guard.",
        ],
    }


def _safe_apply_config(config: Mapping[str, Any], recommendation: Mapping[str, Any]) -> dict[str, Any]:
    next_config = _dict(config)
    # Runtime state is persisted independently. An advisor run can take long
    # enough for a guard cycle to advance, so never write a stale copy back.
    next_config.pop("smart_guard_state", None)
    next_config.pop("creative_guard_state", None)
    next_config["hermes_enabled"] = True
    next_config["hermes_last_recommendation"] = dict(recommendation)
    smart = _dict(next_config.get("smart_guard"))
    creative = _dict(next_config.get("creative_guard"))
    rec_smart = _dict(recommendation.get("smart_guard"))
    rec_creative = _dict(recommendation.get("creative_guard"))

    for key in (
        "hermes_enabled",
        "hermes_auto_apply",
        "use_effective_product_price",
        "daily_budget_pacing",
    ):
        if key in rec_smart:
            smart[key] = bool(rec_smart[key])
    for key in ("monitor_interval_minutes", "fast_monitor_interval_minutes", "slow_monitor_interval_minutes"):
        if key in rec_smart:
            smart[key] = _clamp_int(_to_int(rec_smart[key], 3), 1, 5)
    if "pause_cooldown_minutes" in rec_smart:
        smart["pause_cooldown_minutes"] = _clamp_int(_to_int(rec_smart["pause_cooldown_minutes"], 30), 30, 60)
    if "min_spend_cents" in rec_smart:
        smart["min_spend_cents"] = _clamp_int(_to_int(rec_smart["min_spend_cents"], 300), 300, 3000)
    if "min_roi" in rec_smart:
        smart["min_roi"] = float(_clamp_decimal(_to_decimal(rec_smart["min_roi"], "0.8") or Decimal("0.8"), Decimal("0.6"), Decimal("1.2")))
    if isinstance(rec_smart.get("product_effective_prices"), Mapping):
        smart["product_effective_prices"] = dict(rec_smart["product_effective_prices"])

    for key in ("enabled", "use_effective_product_price"):
        if key in rec_creative:
            creative[key] = bool(rec_creative[key])
    for key in ("monitor_interval_minutes", "fast_monitor_interval_minutes", "slow_monitor_interval_minutes"):
        if key in rec_creative:
            creative[key] = _clamp_int(_to_int(rec_creative[key], 3), 1, 5)
    if isinstance(rec_creative.get("product_effective_prices"), Mapping):
        creative["product_effective_prices"] = dict(rec_creative["product_effective_prices"])
    if isinstance(rec_creative.get("product_card_reset"), Mapping):
        creative["product_card_reset"] = dict(rec_creative["product_card_reset"])
    creative["no_order_budget_share_floor"] = "0.0"
    creative["no_order_min_spend_cents"] = _clamp_int(_to_int(creative.get("no_order_min_spend_cents"), 300), 300, 3000)

    next_config["smart_guard"] = smart
    next_config["creative_guard"] = creative
    return next_config


def _upsert_recommendation(
    db: Session,
    *,
    scope: Mapping[str, Any],
    recommendation: Mapping[str, Any],
    applied: bool,
) -> None:
    db.execute(
        text(
            """
            insert into gmv_hermes_ad_recommendations (
                workspace_id, auth_id, advertiser_id, store_id, campaign_id,
                strategy_id, recommendation_type, recommendation_json,
                confidence, status, applied_at, created_at, updated_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :campaign_id,
                :strategy_id, 'GMVMAX_STRATEGY', :recommendation_json,
                :confidence, :status, :applied_at, :created_at, :updated_at
            )
            on duplicate key update
                recommendation_json=values(recommendation_json),
                confidence=values(confidence),
                status=values(status),
                applied_at=values(applied_at),
                updated_at=values(updated_at)
            """
        ),
        {
            "workspace_id": scope["workspace_id"],
            "auth_id": scope["auth_id"],
            "advertiser_id": scope["advertiser_id"],
            "store_id": scope["store_id"],
            "campaign_id": scope["campaign_id"],
            "strategy_id": scope["strategy_id"],
            "recommendation_json": _json_dumps(recommendation),
            "confidence": recommendation.get("confidence") or "low",
            "status": "APPLIED" if applied else "PROPOSED",
            "applied_at": _utcnow().replace(tzinfo=None) if applied else None,
            "created_at": _utcnow().replace(tzinfo=None),
            "updated_at": _utcnow().replace(tzinfo=None),
        },
    )


def run_hermes_advisor_cycle(db: Session) -> dict[str, Any]:
    scopes = _load_hermes_scopes(db)
    summary = {"scopes": len(scopes), "recommended": 0, "applied": 0, "skipped": 0, "errors": 0}
    for scope in scopes:
        try:
            scope["stats_24h"] = _sample_stats(db, scope, hours=24)
            scope["stats_7d"] = _sample_stats(db, scope, hours=24 * 7)
            scope["price_basis"] = _price_basis(db, scope)
            product_ids = [
                str(item.get("item_group_id"))
                for item in _dict(scope.get("price_basis")).get("products") or []
                if isinstance(item, Mapping) and item.get("item_group_id")
            ]
            try:
                scope["strategy_memory"] = load_strategy_memory(
                    db,
                    scope=scope,
                    item_group_ids=product_ids,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Hermes MySQL strategy memory unavailable; continuing without learned params",
                    extra={"campaign_id": scope.get("campaign_id")},
                )
                scope["strategy_memory"] = {"validated": [], "candidates": []}
            recommendation = _build_recommendation(scope)
            recommendation, realtime_apply_allowed = _attach_realtime_review(
                db,
                scope=scope,
                recommendation=recommendation,
            )
            config = _dict(scope.get("config_json"))
            auto_apply = bool(_dict(config.get("smart_guard")).get("hermes_auto_apply", True))
            applied = False
            if auto_apply and realtime_apply_allowed:
                next_config = _safe_apply_config(config, recommendation)
                db.execute(
                    text(
                        """
                        update gmv_strategy_configs
                        set config_json=:config_json,
                            cooldown_minutes=:cooldown_minutes,
                            updated_at=current_timestamp(6)
                        where id=:strategy_id
                        """
                    ),
                    {
                        "config_json": _json_dumps(next_config),
                        "cooldown_minutes": _to_int(
                            _dict(next_config.get("smart_guard")).get("pause_cooldown_minutes"),
                            30,
                        ),
                        "strategy_id": scope["strategy_id"],
                    },
                )
                applied = True
                summary["applied"] += 1
            _upsert_recommendation(db, scope=scope, recommendation=recommendation, applied=applied)
            summary["recommended"] += 1
            db.commit()
        except Exception:
            db.rollback()
            summary["errors"] += 1
            logger.exception("Hermes GMV Max advisor failed", extra={"campaign_id": scope.get("campaign_id")})
    return summary
