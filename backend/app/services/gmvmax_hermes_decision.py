from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.data.models.gmv_restructured import GmvStrategyConfig
from app.services.gmvmax_hermes_context import build_decision_performance_context
from app.services.hermes_agent.client import HermesAdsReviewClient, extract_output_text

logger = logging.getLogger("gmv.services.gmvmax.hermes_decision")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _utcnow().date()


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


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


def _clamp_decimal(value: Decimal, low: str, high: str) -> Decimal:
    return max(Decimal(low), min(Decimal(high), value))


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _extract_json_object(text_value: str) -> dict[str, Any]:
    if not text_value:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text_value, flags=re.S)
    candidates: list[str] = []
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


def ensure_hermes_plan_default_table(db: Session) -> None:
    db.execute(
        text(
            """
            create table if not exists gmv_hermes_ad_plan_defaults (
                id bigint unsigned not null auto_increment primary key,
                workspace_id bigint not null,
                auth_id bigint not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                item_group_id varchar(64) not null default '',
                effective_date date not null,
                source_report_id bigint unsigned null,
                status varchar(32) not null default 'APPROVED',
                confidence varchar(32) null,
                decision_json json null,
                params_json json not null,
                discussion_json json null,
                created_at datetime(6) not null default current_timestamp(6),
                updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
                unique key uq_gmv_hermes_plan_default (
                    workspace_id, auth_id, advertiser_id, store_id, item_group_id, effective_date
                ),
                key idx_gmv_hermes_plan_default_scope (
                    workspace_id, auth_id, advertiser_id, store_id, status, effective_date
                ),
                key idx_gmv_hermes_plan_default_report (source_report_id)
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci
            """
        )
    )


def _load_report(db: Session, report_id: int) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            select id, workspace_id, auth_id, advertiser_id, store_id, report_date,
                   advertiser_timezone, input_json, recommendation_json, report_markdown
            from gmv_hermes_ad_daily_reports
            where id=:report_id
            limit 1
            """
        ),
        {"report_id": int(report_id)},
    ).mappings().first()
    return dict(row) if row else None


def _report_product_ids(report_input: Mapping[str, Any]) -> list[str]:
    seen: set[str] = set()
    for item_group_id in _list(report_input.get("product_ids")):
        value = str(item_group_id or "").strip()
        if value:
            seen.add(value)
    for campaign in _list(report_input.get("campaigns")):
        if not isinstance(campaign, Mapping):
            continue
        for item_group_id in _list(campaign.get("item_group_ids")):
            value = str(item_group_id or "").strip()
            if value:
                seen.add(value)
    for creative in _list(report_input.get("creatives_by_spend")):
        if not isinstance(creative, Mapping):
            continue
        value = str(creative.get("item_group_id") or "").strip()
        if value:
            seen.add(value)
    return sorted(seen)


def _decision_instructions() -> str:
    return (
        "你是 TikTok Shop GMV Max 自动投放系统里的 Hermes 决策层。"
        "输入包含 ChatGPT 每日投放报告、结构化建议、全量汇总和分层选取的关键投放证据。"
        "input_meta 会说明证据覆盖率；覆盖不足或数据口径冲突时必须降低置信度。"
        "strategy_memory 中 CANDIDATE 记忆禁止直接执行，只有 VALIDATED 记忆可以调整参数。"
        "你的任务不是写报告，而是判断这些建议是否可以被无人值守系统采纳。"
        "只输出严格合法 JSON，不要输出 Markdown 或解释。"
        "JSON 字段：decision、reason、confidence、approved_params、product_params、questions_or_objections。"
        "decision 只能是 ACCEPT、REVISE 或 REJECT。"
        "approved_params 可包含 budget、roas_bid、min_roi、monitor_interval_minutes、cooldown_minutes、min_spend_cents。"
        "monitor_interval_minutes 是系统读取实时指标的动态间隔，只能在 1-5 分钟内，不能因素材复测而降为小时级。"
        "cooldown_minutes 仅表示计划级暂停后的恢复冷却，不是单素材移除后的复测冷却；"
        "单素材复测由 creative guard 根据素材历史单独计算，禁止把报告里的素材复测时长映射成全计划冷却。"
        "低效单素材应优先素材级移除，除非商品卡主导空耗、视频长期无流量或计划异常不消耗，否则不要暂停或重建整条计划。"
        "product_params 是数组，每项包含 item_group_id，并可覆盖 approved_params。"
        "样本不足时 confidence=low 且预算只能小幅调整；持续出单且证据充分时可阶梯放量，不能因低 ROI 总览一刀切停止所有探索。"
        "ROAS 不能低到放任亏损，预算、出价与冷却必须结合商品级表现动态决定。"
        "如果建议方向正确但参数不安全，请 decision=REVISE 并给出你认为安全的参数。"
    )


def _revision_instructions(objections: Sequence[str]) -> str:
    return (
        "你刚才给出的 GMV Max 自动投放参数没有通过 Hermes 安全审查。"
        "请根据审查意见重新给出更保守、可无人值守执行的参数。"
        f"审查意见：{'; '.join(str(item) for item in objections if item)}。"
        "只输出严格合法 JSON，字段仍为 decision、reason、confidence、approved_params、product_params、questions_or_objections。"
        "如果无法安全执行，请 decision=REJECT。"
    )


async def _call_gpt(payload: Mapping[str, Any], *, instructions: str, source: str) -> tuple[dict[str, Any], str]:
    # Daily report approval is retrospective, long-context work. Keep it on the
    # review agent so a slow model response cannot consume the realtime guard's
    # tighter latency budget.
    client = HermesAdsReviewClient()
    response, _latency_ms = await client.create_response(
        input_text=_json_dumps(payload),
        instructions=instructions,
        metadata={"source": source, "prompt_version": "gmvmax_plan_decision_v2"},
    )
    output_text = extract_output_text(response)
    return response if isinstance(response, dict) else _dict(response), output_text


def _sanitize_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    budget = _to_decimal(raw.get("budget"))
    if budget is not None and budget > 0:
        params["budget"] = float(_clamp_decimal(budget, "20", "2000").quantize(Decimal("0.01")))
    roas_bid = _to_decimal(raw.get("roas_bid"))
    if roas_bid is not None and roas_bid > 0:
        params["roas_bid"] = float(
            _clamp_decimal(roas_bid, "1.0", "5.0").quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        )
    min_roi = _to_decimal(raw.get("min_roi"))
    if min_roi is not None and min_roi > 0:
        params["min_roi"] = float(_clamp_decimal(min_roi, "0.6", "1.5").quantize(Decimal("0.01")))
    if raw.get("monitor_interval_minutes") is not None:
        params["monitor_interval_minutes"] = _clamp_int(_to_int(raw.get("monitor_interval_minutes"), 3), 1, 5)
    if raw.get("cooldown_minutes") is not None:
        params["cooldown_minutes"] = _clamp_int(_to_int(raw.get("cooldown_minutes"), 30), 30, 120)
    if raw.get("min_spend_cents") is not None:
        params["min_spend_cents"] = _clamp_int(_to_int(raw.get("min_spend_cents"), 300), 300, 10000)
    if raw.get("min_runtime_minutes_before_first_change") is not None:
        params["min_runtime_minutes_before_first_change"] = _clamp_int(
            _to_int(raw.get("min_runtime_minutes_before_first_change"), 10),
            5,
            60,
        )
    return params


def _apply_performance_safety(params: Mapping[str, Any], report_input: Mapping[str, Any]) -> dict[str, Any]:
    guarded = dict(params)
    summary = _dict(report_input.get("summary"))
    cost = _to_decimal(summary.get("cost"), "0") or Decimal("0")
    roi = _to_decimal(summary.get("roi"), "0") or Decimal("0")
    if cost >= Decimal("100") and roi < Decimal("0.8"):
        if "budget" in guarded:
            guarded["budget"] = float(min(Decimal(str(guarded["budget"])), Decimal("50")))
        guarded["monitor_interval_minutes"] = 1
        guarded["cooldown_minutes"] = max(_to_int(guarded.get("cooldown_minutes"), 30), 60)
        guarded["min_roi"] = float(max(_to_decimal(guarded.get("min_roi"), "0.8") or Decimal("0.8"), Decimal("1.0")))
        guarded["min_spend_cents"] = min(max(_to_int(guarded.get("min_spend_cents"), 500), 300), 500)
        guarded["hermes_safety_override"] = "high_spend_low_roi"
    elif cost >= Decimal("20") and roi < Decimal("0.8"):
        if "budget" in guarded:
            guarded["budget"] = float(min(Decimal(str(guarded["budget"])), Decimal("100")))
        guarded["monitor_interval_minutes"] = min(_to_int(guarded.get("monitor_interval_minutes"), 3), 3)
        guarded["cooldown_minutes"] = max(_to_int(guarded.get("cooldown_minutes"), 30), 45)
        guarded["hermes_safety_override"] = "low_roi"
    return guarded


def _hermes_review(decision: Mapping[str, Any]) -> tuple[bool, list[str]]:
    objections: list[str] = []
    decision_text = str(decision.get("decision") or "").upper()
    if decision_text not in {"ACCEPT", "REVISE"}:
        objections.append("decision is not acceptable")
    params = _sanitize_params(_dict(decision.get("approved_params")))
    product_params = _list(decision.get("product_params"))
    if not params and not product_params:
        objections.append("no executable params")
    raw_params = _dict(decision.get("approved_params"))
    raw_budget = _to_decimal(raw_params.get("budget"))
    if raw_budget is not None and (raw_budget < Decimal("20") or raw_budget > Decimal("2000")):
        objections.append("budget outside safe bounds")
    raw_roas = _to_decimal(raw_params.get("roas_bid"))
    if raw_roas is not None and (raw_roas < Decimal("1.0") or raw_roas > Decimal("5.0")):
        objections.append("roas_bid outside safe bounds")
    return not objections, objections


def _decision_rows(
    *,
    decision: Mapping[str, Any],
    report: Mapping[str, Any],
    report_input: Mapping[str, Any],
) -> list[dict[str, Any]]:
    base_params = _apply_performance_safety(
        _sanitize_params(_dict(decision.get("approved_params"))),
        report_input,
    )
    product_params = _list(decision.get("product_params"))
    rows: list[dict[str, Any]] = []
    for item in product_params:
        if not isinstance(item, Mapping):
            continue
        item_group_id = str(item.get("item_group_id") or "").strip()
        params = dict(base_params)
        params.update(_sanitize_params(item))
        params = _apply_performance_safety(params, report_input)
        if item_group_id and params:
            rows.append({"item_group_id": item_group_id, "params": params})
    if not rows and base_params:
        product_ids = _report_product_ids(report_input)
        if product_ids:
            rows.extend({"item_group_id": item_group_id, "params": dict(base_params)} for item_group_id in product_ids)
        else:
            rows.append({"item_group_id": "", "params": base_params})
    return rows


def _save_decision_rows(
    db: Session,
    *,
    report: Mapping[str, Any],
    decision: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    discussion: Mapping[str, Any],
) -> int:
    ensure_hermes_plan_default_table(db)
    report_date = report.get("report_date")
    effective_date = report_date + timedelta(days=1) if isinstance(report_date, date) else _today()
    saved = 0
    for row in rows:
        params = _dict(row.get("params"))
        if not params:
            continue
        db.execute(
            text(
                """
                insert into gmv_hermes_ad_plan_defaults (
                    workspace_id, auth_id, advertiser_id, store_id, item_group_id,
                    effective_date, source_report_id, status, confidence,
                    decision_json, params_json, discussion_json, created_at, updated_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :item_group_id,
                    :effective_date, :source_report_id, 'APPROVED', :confidence,
                    cast(:decision_json as json), cast(:params_json as json),
                    cast(:discussion_json as json), current_timestamp(6), current_timestamp(6)
                )
                on duplicate key update
                    source_report_id=values(source_report_id),
                    status='APPROVED',
                    confidence=values(confidence),
                    decision_json=values(decision_json),
                    params_json=values(params_json),
                    discussion_json=values(discussion_json),
                    updated_at=current_timestamp(6)
                """
            ),
            {
                "workspace_id": int(report["workspace_id"]),
                "auth_id": int(report["auth_id"]),
                "advertiser_id": str(report["advertiser_id"]),
                "store_id": str(report["store_id"]),
                "item_group_id": str(row.get("item_group_id") or ""),
                "effective_date": effective_date,
                "source_report_id": int(report["id"]),
                "confidence": str(decision.get("confidence") or "low")[:32],
                "decision_json": _json_dumps(decision),
                "params_json": _json_dumps(params),
                "discussion_json": _json_dumps(discussion),
            },
        )
        saved += 1
    return saved


async def run_hermes_report_decision(db: Session, *, report_id: int) -> dict[str, Any]:
    ensure_hermes_plan_default_table(db)
    report = _load_report(db, int(report_id))
    if not report:
        return {"report_id": report_id, "status": "missing", "approved": 0}
    report_input = _dict(report.get("input_json"))
    recommendation = _dict(report.get("recommendation_json"))
    recommendations = _list(recommendation.get("recommendations"))
    if not recommendations:
        return {"report_id": report_id, "status": "no_recommendations", "approved": 0}

    request_payload = {
        "report": {
            "id": report.get("id"),
            "report_date": str(report.get("report_date")),
            "markdown": str(report.get("report_markdown") or "")[:2500],
        },
        "recommendation": recommendation,
        "performance_context": build_decision_performance_context(report_input),
        "safety_bounds": {
            "budget": [20, 2000],
            "roas_bid": [1.0, 5.0],
            "min_roi": [0.6, 1.5],
            "monitor_interval_minutes": [1, 5],
            "cooldown_minutes": [30, 120],
            "min_spend_cents": [300, 10000],
        },
    }
    primary_response, primary_text = await _call_gpt(
        request_payload,
        instructions=_decision_instructions(),
        source="gmvmax_hermes_plan_decision",
    )
    decision = _extract_json_object(primary_text)
    accepted, objections = _hermes_review(decision)
    discussion: dict[str, Any] = {
        "rounds": [
            {
                "type": "initial",
                "response": primary_response,
                "output_text": primary_text,
                "parsed": decision,
                "accepted_by_hermes": accepted,
                "objections": objections,
            }
        ]
    }
    if not accepted:
        revision_payload = {
            **request_payload,
            "previous_decision": decision,
            "hermes_objections": objections,
        }
        revision_response, revision_text = await _call_gpt(
            revision_payload,
            instructions=_revision_instructions(objections),
            source="gmvmax_hermes_plan_decision_revision",
        )
        revised = _extract_json_object(revision_text)
        accepted, objections = _hermes_review(revised)
        discussion["rounds"].append(
            {
                "type": "revision",
                "response": revision_response,
                "output_text": revision_text,
                "parsed": revised,
                "accepted_by_hermes": accepted,
                "objections": objections,
            }
        )
        if accepted:
            decision = revised

    if not accepted:
        logger.info("Hermes plan decision rejected", extra={"report_id": report_id, "objections": objections})
        return {"report_id": report_id, "status": "rejected", "approved": 0, "objections": objections}

    rows = _decision_rows(decision=decision, report=report, report_input=report_input)
    saved = _save_decision_rows(db, report=report, decision=decision, rows=rows, discussion=discussion)
    return {"report_id": report_id, "status": "approved", "approved": saved}


def _lookup_approved_defaults(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    item_group_ids: Sequence[str] | None,
) -> dict[str, Any] | None:
    ensure_hermes_plan_default_table(db)
    normalized_ids = [str(item).strip() for item in (item_group_ids or []) if str(item or "").strip()]
    lookup_ids = normalized_ids + [""]
    rows = db.execute(
        text(
            """
            select item_group_id, effective_date, source_report_id, params_json, decision_json, confidence
            from gmv_hermes_ad_plan_defaults
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and status='APPROVED'
              and item_group_id in :item_group_ids
              and effective_date <= :max_effective_date
              and effective_date >= :min_effective_date
            order by case when item_group_id <> '' then 0 else 1 end,
                     effective_date desc, updated_at desc, id desc
            limit 1
            """
        ).bindparams(bindparam("item_group_ids", expanding=True)),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
            "item_group_ids": tuple(lookup_ids),
            "max_effective_date": _today() + timedelta(days=1),
            "min_effective_date": _today() - timedelta(days=7),
        },
    ).mappings().first()
    return dict(rows) if rows else None


def _is_high_confidence(row: Mapping[str, Any] | None) -> bool:
    return str((row or {}).get("confidence") or "").strip().lower() in {
        "high",
        "高度",
        "高",
    }


def _bounded_decimal(
    value: Any,
    *,
    baseline: Any,
    lower_multiplier: str,
    upper_multiplier: str,
) -> Decimal | None:
    try:
        candidate = Decimal(str(value))
        base = Decimal(str(baseline))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if candidate <= 0 or base <= 0:
        return None
    lower = base * Decimal(lower_multiplier)
    upper = base * Decimal(upper_multiplier)
    return max(lower, min(upper, candidate))


def _bounded_roas(value: Any, *, baseline: Any) -> Decimal | None:
    try:
        candidate = Decimal(str(value))
        base = Decimal(str(baseline))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if candidate <= 0 or base <= 0:
        return None
    bounded = max(base - Decimal("0.2"), min(base + Decimal("0.2"), candidate))
    return bounded.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _validated_model_update(model: Any, updates: Mapping[str, Any]) -> Any:
    if not updates:
        return model
    payload = model.model_dump() if hasattr(model, "model_dump") else dict(model)
    payload.update(dict(updates))
    model_type = type(model)
    if hasattr(model_type, "model_validate"):
        return model_type.model_validate(payload)
    return model_type(**payload)


def _merge_automation_defaults(automation: Mapping[str, Any] | None, params: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(automation or {})
    if merged.get("disable_hermes_plan_defaults"):
        return merged
    if _is_high_confidence(row):
        for key in ("min_roi", "monitor_interval_minutes", "cooldown_minutes", "min_spend_cents", "min_runtime_minutes_before_first_change"):
            if key in params:
                merged[key] = params[key]
    merged.setdefault("enabled", True)
    merged.setdefault("smart_guard_enabled", True)
    merged.setdefault("creative_guard_enabled", True)
    merged.setdefault("hermes_enabled", True)
    merged["hermes_plan_default"] = {
        "source_report_id": row.get("source_report_id"),
        "effective_date": str(row.get("effective_date") or ""),
        "item_group_id": row.get("item_group_id") or "",
        "confidence": row.get("confidence") or "low",
        "applied_at": _utcnow().isoformat(),
    }
    return merged


def apply_approved_plan_defaults_to_create_payload(
    db: Session,
    payload: Any,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> Any:
    automation = _dict(getattr(payload, "automation", None))
    if automation.get("disable_hermes_plan_defaults"):
        return payload
    if getattr(payload, "automation", None) is None:
        return payload
    item_group_ids = list(getattr(payload, "item_group_ids", None) or [])
    row = _lookup_approved_defaults(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        store_id=str(getattr(payload, "store_id", "")),
        item_group_ids=item_group_ids,
    )
    if not row:
        return payload
    params = _dict(row.get("params_json"))
    updates: dict[str, Any] = {
        "automation": _merge_automation_defaults(automation, params, row),
    }
    if _is_high_confidence(row):
        budget = _bounded_decimal(
            params.get("budget"),
            baseline=getattr(payload, "budget", None),
            lower_multiplier="0.8",
            upper_multiplier="1.25",
        )
        if budget is not None:
            updates["budget"] = int(budget.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        roas_bid = _bounded_roas(params.get("roas_bid"), baseline=getattr(payload, "roas_bid", None))
        if roas_bid is not None:
            updates["roas_bid"] = float(roas_bid)
    return _validated_model_update(payload, updates)


def apply_approved_plan_defaults_to_body(
    db: Session,
    body: Any,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
) -> Any:
    item_group_ids = list(getattr(body, "item_group_ids", None) or [])
    row = _lookup_approved_defaults(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        item_group_ids=item_group_ids,
    )
    if not row:
        return body
    params = _dict(row.get("params_json"))
    updates: dict[str, Any] = {}
    if _is_high_confidence(row):
        budget = _bounded_decimal(
            params.get("budget"),
            baseline=getattr(body, "budget", None),
            lower_multiplier="0.8",
            upper_multiplier="1.25",
        )
        if budget is not None:
            updates["budget"] = float(budget.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        roas_bid = _bounded_roas(params.get("roas_bid"), baseline=getattr(body, "roas_bid", None))
        if roas_bid is not None:
            updates["roas_bid"] = float(roas_bid)
    return _validated_model_update(body, updates)


def apply_approved_plan_defaults_to_strategy(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_id: str,
    item_group_ids: Sequence[str] | None,
) -> bool:
    row = _lookup_approved_defaults(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        item_group_ids=item_group_ids,
    )
    if not row:
        return False
    params = _dict(row.get("params_json"))
    strategy = (
        db.query(GmvStrategyConfig)
        .filter(
            GmvStrategyConfig.workspace_id == int(workspace_id),
            GmvStrategyConfig.auth_id == int(auth_id),
            GmvStrategyConfig.campaign_id == str(campaign_id),
        )
        .order_by(GmvStrategyConfig.id.desc())
        .first()
    )
    if strategy is None:
        return False
    config = _dict(strategy.config_json)
    smart = _dict(config.get("smart_guard"))
    creative = _dict(config.get("creative_guard"))
    operational_params = params if _is_high_confidence(row) else {}
    for key in ("min_roi", "monitor_interval_minutes", "min_spend_cents"):
        if key in operational_params:
            smart[key] = operational_params[key]
    if "cooldown_minutes" in operational_params:
        smart["pause_cooldown_minutes"] = operational_params["cooldown_minutes"]
    for key in ("monitor_interval_minutes", "min_spend_cents"):
        if key in operational_params:
            creative[key] = operational_params[key]
    config["hermes_enabled"] = True
    config["smart_guard"] = smart
    config["creative_guard"] = creative
    config["hermes_plan_default"] = {
        "source_report_id": row.get("source_report_id"),
        "effective_date": str(row.get("effective_date") or ""),
        "item_group_id": row.get("item_group_id") or "",
        "confidence": row.get("confidence") or "low",
        "applied_at": _utcnow().isoformat(),
    }
    if operational_params.get("roas_bid") is not None:
        strategy.target_roi = operational_params["roas_bid"]
    if operational_params.get("min_roi") is not None:
        strategy.min_roi = operational_params["min_roi"]
    if operational_params.get("cooldown_minutes") is not None:
        strategy.cooldown_minutes = operational_params["cooldown_minutes"]
    strategy.config_json = config
    db.add(strategy)
    db.flush()
    return True
