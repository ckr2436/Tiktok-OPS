from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.services.gmvmax_hermes_context import build_product_performance


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"), sort_keys=True)


def _money_to_cents(value: Any) -> int:
    return int((_decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ensure_hermes_mysql_memory_tables(db: Session) -> None:
    db.execute(
        text(
            """
            create table if not exists gmv_hermes_ad_policy_evaluations (
                id bigint unsigned not null auto_increment primary key,
                workspace_id bigint unsigned not null,
                auth_id bigint unsigned not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                item_group_id varchar(64) not null,
                report_date date not null,
                source_report_id bigint unsigned null,
                source_plan_default_id bigint unsigned not null,
                params_json json not null,
                threshold_json json not null,
                cost_cents bigint not null default 0,
                gross_revenue_cents bigint not null default 0,
                orders bigint not null default 0,
                roi decimal(18,6) not null default 0,
                target_roi decimal(18,6) not null default 0,
                outcome varchar(32) not null,
                reason varchar(512) null,
                observed_at datetime(6) not null,
                created_at datetime(6) not null default current_timestamp(6),
                updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
                unique key uq_gmv_hermes_policy_evaluation (source_plan_default_id, report_date),
                key idx_gmv_hermes_policy_scope_date (
                    workspace_id, auth_id, advertiser_id, store_id, report_date
                ),
                key idx_gmv_hermes_policy_product_date (item_group_id, report_date, outcome)
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci
            """
        )
    )
    db.execute(
        text(
            """
            create table if not exists gmv_hermes_ad_memory_facts (
                id bigint unsigned not null auto_increment primary key,
                workspace_id bigint unsigned not null,
                auth_id bigint unsigned not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                item_group_id varchar(64) not null default '',
                memory_type varchar(64) not null,
                subject_key varchar(191) not null,
                statement text not null,
                status varchar(32) not null default 'CANDIDATE',
                confidence decimal(8,6) not null default 0,
                independent_days int not null default 0,
                evidence_orders bigint not null default 0,
                success_count int not null default 0,
                failure_count int not null default 0,
                evidence_json json not null,
                first_observed_date date null,
                last_observed_date date null,
                last_validated_at datetime(6) null,
                created_at datetime(6) not null default current_timestamp(6),
                updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
                unique key uq_gmv_hermes_memory_fact (
                    workspace_id, auth_id, advertiser_id, store_id, item_group_id,
                    memory_type, subject_key
                ),
                key idx_gmv_hermes_memory_scope_status (
                    workspace_id, auth_id, advertiser_id, store_id, status, updated_at
                ),
                key idx_gmv_hermes_memory_product (item_group_id, memory_type, status)
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci
            """
        )
    )


def _product_performance_map(report_input: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    product_rows = _rows(report_input.get("product_performance"))
    if not product_rows:
        product_rows = build_product_performance(_rows(report_input.get("campaigns")))
    return {
        str(row.get("item_group_id") or ""): row
        for row in product_rows
        if str(row.get("item_group_id") or "").strip()
    }


def record_policy_evaluations(
    db: Session,
    *,
    scope: Mapping[str, Any],
    report_date: date,
    report_input: Mapping[str, Any],
    source_report_id: int | None = None,
) -> dict[str, int]:
    ensure_hermes_mysql_memory_tables(db)
    plan_rows = db.execute(
        text(
            """
            select id, item_group_id, params_json
            from gmv_hermes_ad_plan_defaults
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and effective_date=:report_date
              and status='APPROVED'
            order by id asc
            """
        ),
        {**scope, "report_date": report_date},
    ).mappings().all()
    performance = _product_performance_map(report_input)
    summary = {"evaluated": 0, "success": 0, "failure": 0, "inconclusive": 0}

    for row in plan_rows:
        item_group_id = str(row.get("item_group_id") or "").strip()
        if not item_group_id:
            continue
        params = _dict(row.get("params_json"))
        metrics = performance.get(item_group_id) or {}
        cost_cents = _money_to_cents(metrics.get("cost"))
        gmv_cents = _money_to_cents(metrics.get("gmv"))
        orders = _integer(metrics.get("orders"))
        roi = _decimal(metrics.get("roi"))
        target_roi = _decimal(params.get("min_roi"))
        if target_roi <= 0:
            target_roi = _decimal(params.get("roas_bid"))
        if target_roi <= 0:
            target_roi = Decimal("1")

        configured_floor = max(0, _integer(params.get("min_spend_cents")))
        budget_floor = max(0, _money_to_cents(params.get("budget")) // 20)
        spend_floor_cents = max(1, configured_floor, budget_floor)
        performance_ratio = roi / target_roi if target_roi > 0 else Decimal("0")

        if not metrics:
            outcome = "INCONCLUSIVE"
            reason = "no_single_product_campaign_metrics"
        elif cost_cents < spend_floor_cents:
            outcome = "INCONCLUSIVE"
            reason = "spend_below_policy_evidence_floor"
        elif orders > 0 and performance_ratio >= Decimal("1"):
            outcome = "SUCCESS"
            reason = "roi_met_or_exceeded_policy_target"
        elif orders == 0 or performance_ratio <= Decimal("0.5"):
            outcome = "FAILURE"
            reason = "no_order_or_roi_far_below_policy_target"
        else:
            outcome = "INCONCLUSIVE"
            reason = "mixed_result_requires_more_independent_days"

        threshold = {
            "spend_floor_cents": spend_floor_cents,
            "target_roi": float(target_roi),
            "success_ratio": 1.0,
            "failure_ratio": 0.5,
            "basis": "configured_min_spend_or_five_percent_budget",
        }
        db.execute(
            text(
                """
                insert into gmv_hermes_ad_policy_evaluations (
                    workspace_id, auth_id, advertiser_id, store_id, item_group_id,
                    report_date, source_report_id, source_plan_default_id,
                    params_json, threshold_json, cost_cents, gross_revenue_cents,
                    orders, roi, target_roi, outcome, reason, observed_at,
                    created_at, updated_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :item_group_id,
                    :report_date, :source_report_id, :source_plan_default_id,
                    cast(:params_json as json), cast(:threshold_json as json),
                    :cost_cents, :gmv_cents, :orders, :roi, :target_roi,
                    :outcome, :reason, :observed_at,
                    current_timestamp(6), current_timestamp(6)
                )
                on duplicate key update
                    source_report_id=coalesce(values(source_report_id), source_report_id),
                    params_json=values(params_json),
                    threshold_json=values(threshold_json),
                    cost_cents=values(cost_cents),
                    gross_revenue_cents=values(gross_revenue_cents),
                    orders=values(orders),
                    roi=values(roi),
                    target_roi=values(target_roi),
                    outcome=values(outcome),
                    reason=values(reason),
                    observed_at=values(observed_at),
                    updated_at=current_timestamp(6)
                """
            ),
            {
                **scope,
                "item_group_id": item_group_id,
                "report_date": report_date,
                "source_report_id": source_report_id,
                "source_plan_default_id": int(row["id"]),
                "params_json": _json_dumps(params),
                "threshold_json": _json_dumps(threshold),
                "cost_cents": cost_cents,
                "gmv_cents": gmv_cents,
                "orders": orders,
                "roi": str(roi.quantize(Decimal("0.000001"))),
                "target_roi": str(target_roi.quantize(Decimal("0.000001"))),
                "outcome": outcome,
                "reason": reason,
                "observed_at": _utcnow().replace(tzinfo=None),
            },
        )
        summary["evaluated"] += 1
        summary[outcome.lower()] += 1
    return summary


def _best_success_params(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"params": {}, "successes": 0, "orders": 0, "cost_cents": 0, "gmv_cents": 0}
    )
    for row in rows:
        if str(row.get("outcome") or "") != "SUCCESS":
            continue
        params = _dict(row.get("params_json"))
        key = _json_dumps(params)
        item = grouped[key]
        item["params"] = params
        item["successes"] += 1
        item["orders"] += _integer(row.get("orders"))
        item["cost_cents"] += _integer(row.get("cost_cents"))
        item["gmv_cents"] += _integer(row.get("gross_revenue_cents"))
    if not grouped:
        return {}
    best = max(
        grouped.values(),
        key=lambda item: (
            item["successes"],
            item["orders"],
            item["gmv_cents"] / max(1, item["cost_cents"]),
        ),
    )
    return dict(best["params"])


def classify_policy_memory(
    *,
    independent_days: int,
    successes: int,
    failures: int,
    orders: int,
    weighted_roi: Decimal,
    weighted_target: Decimal,
) -> tuple[str, Decimal]:
    resolved = successes + failures
    success_rate = Decimal(successes) / Decimal(resolved) if resolved > 0 else Decimal("0")
    day_depth = min(Decimal("1"), Decimal(independent_days) / Decimal("7"))
    order_depth = min(Decimal("1"), Decimal(orders) / Decimal("10"))
    resolution_depth = min(Decimal("1"), Decimal(resolved) / Decimal("5"))
    confidence = (
        Decimal("0.15")
        + Decimal("0.30") * day_depth
        + Decimal("0.25") * order_depth
        + Decimal("0.20") * resolution_depth
        + Decimal("0.10") * success_rate
    )
    confidence = min(Decimal("0.95"), confidence)

    if (
        independent_days >= 3
        and resolved >= 3
        and successes >= 2
        and orders >= 3
        and success_rate >= Decimal("0.6")
        and weighted_roi >= weighted_target * Decimal("0.9")
    ):
        return "VALIDATED", confidence
    if independent_days >= 3 and failures >= 3 and success_rate <= Decimal("0.25"):
        return "RETIRED", confidence
    return "CANDIDATE", confidence


def refresh_strategy_memory(db: Session, *, scope: Mapping[str, Any], lookback_days: int = 90) -> dict[str, int]:
    ensure_hermes_mysql_memory_tables(db)
    rows = db.execute(
        text(
            """
            select item_group_id, report_date, params_json, cost_cents,
                   gross_revenue_cents, orders, roi, target_roi, outcome
            from gmv_hermes_ad_policy_evaluations
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and report_date >= :since_date
            order by report_date asc, id asc
            """
        ),
        {**scope, "since_date": _utcnow().date() - timedelta(days=max(1, lookback_days))},
    ).mappings().all()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("item_group_id") or "")].append(dict(row))

    refreshed = 0
    for item_group_id, product_rows in grouped.items():
        if not item_group_id:
            continue
        dates = sorted({row["report_date"] for row in product_rows if row.get("report_date")})
        successes = sum(1 for row in product_rows if row.get("outcome") == "SUCCESS")
        failures = sum(1 for row in product_rows if row.get("outcome") == "FAILURE")
        inconclusive = sum(1 for row in product_rows if row.get("outcome") == "INCONCLUSIVE")
        resolved = successes + failures
        cost_cents = sum(_integer(row.get("cost_cents")) for row in product_rows)
        gmv_cents = sum(_integer(row.get("gross_revenue_cents")) for row in product_rows)
        orders = sum(_integer(row.get("orders")) for row in product_rows)
        weighted_roi = Decimal(gmv_cents) / Decimal(cost_cents) if cost_cents > 0 else Decimal("0")
        weighted_target = Decimal("0")
        if cost_cents > 0:
            weighted_target = sum(
                _decimal(row.get("target_roi")) * Decimal(max(0, _integer(row.get("cost_cents"))))
                for row in product_rows
            ) / Decimal(cost_cents)
        success_rate = Decimal(successes) / Decimal(resolved) if resolved > 0 else Decimal("0")

        status, confidence = classify_policy_memory(
            independent_days=len(dates),
            successes=successes,
            failures=failures,
            orders=orders,
            weighted_roi=weighted_roi,
            weighted_target=weighted_target,
        )

        best_params = _best_success_params(product_rows)
        evidence = {
            "independent_days": len(dates),
            "successes": successes,
            "failures": failures,
            "inconclusive": inconclusive,
            "success_rate": round(float(success_rate), 4),
            "cost": round(cost_cents / 100, 2),
            "gmv": round(gmv_cents / 100, 2),
            "orders": orders,
            "weighted_roi": round(float(weighted_roi), 4),
            "weighted_target_roi": round(float(weighted_target), 4),
            "best_success_params": best_params,
            "validation_policy": {
                "minimum_independent_days": 3,
                "minimum_resolved_days": 3,
                "minimum_success_days": 2,
                "minimum_orders": 3,
                "minimum_success_rate": 0.6,
                "minimum_target_attainment": 0.9,
            },
        }
        statement = (
            f"商品 {item_group_id} 的策略经过 {len(dates)} 个独立投放日验证："
            f"成功 {successes} 次、失败 {failures} 次、订单 {orders}、加权 ROI {float(weighted_roi):.2f}。"
            f"当前记忆状态为 {status}；只有 VALIDATED 状态可以自动影响参数。"
        )
        db.execute(
            text(
                """
                insert into gmv_hermes_ad_memory_facts (
                    workspace_id, auth_id, advertiser_id, store_id, item_group_id,
                    memory_type, subject_key, statement, status, confidence,
                    independent_days, evidence_orders, success_count, failure_count,
                    evidence_json, first_observed_date, last_observed_date,
                    last_validated_at, created_at, updated_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id, :item_group_id,
                    'PRODUCT_POLICY', 'daily-plan-default', :statement, :status, :confidence,
                    :independent_days, :evidence_orders, :success_count, :failure_count,
                    cast(:evidence_json as json), :first_observed_date, :last_observed_date,
                    case when :status='VALIDATED' then current_timestamp(6) else null end,
                    current_timestamp(6), current_timestamp(6)
                )
                on duplicate key update
                    statement=values(statement),
                    status=values(status),
                    confidence=values(confidence),
                    independent_days=values(independent_days),
                    evidence_orders=values(evidence_orders),
                    success_count=values(success_count),
                    failure_count=values(failure_count),
                    evidence_json=values(evidence_json),
                    first_observed_date=values(first_observed_date),
                    last_observed_date=values(last_observed_date),
                    last_validated_at=case
                        when values(status)='VALIDATED' then current_timestamp(6)
                        else last_validated_at
                    end,
                    updated_at=current_timestamp(6)
                """
            ),
            {
                **scope,
                "item_group_id": item_group_id,
                "statement": statement,
                "status": status,
                "confidence": str(confidence.quantize(Decimal("0.000001"))),
                "independent_days": len(dates),
                "evidence_orders": orders,
                "success_count": successes,
                "failure_count": failures,
                "evidence_json": _json_dumps(evidence),
                "first_observed_date": dates[0] if dates else None,
                "last_observed_date": dates[-1] if dates else None,
            },
        )
        refreshed += 1
    return {"refreshed": refreshed, "evaluation_rows": len(rows)}


def load_strategy_memory(
    db: Session,
    *,
    scope: Mapping[str, Any],
    item_group_ids: Sequence[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    ensure_hermes_mysql_memory_tables(db)
    normalized_ids = sorted({str(item).strip() for item in item_group_ids or [] if str(item or "").strip()})
    sql = """
        select item_group_id, memory_type, subject_key, statement, status,
               confidence, independent_days, evidence_orders, success_count,
               failure_count, evidence_json, first_observed_date,
               last_observed_date, last_validated_at, updated_at
        from gmv_hermes_ad_memory_facts
        where workspace_id=:workspace_id
          and auth_id=:auth_id
          and advertiser_id=:advertiser_id
          and store_id=:store_id
          and status in ('VALIDATED','CANDIDATE')
    """
    params: dict[str, Any] = {**scope, "limit": max(1, min(int(limit), 100))}
    query = text(sql)
    if normalized_ids:
        query = text(sql + " and item_group_id in :item_group_ids " +
                     " order by field(status,'VALIDATED','CANDIDATE'), confidence desc, updated_at desc limit :limit")
        query = query.bindparams(bindparam("item_group_ids", expanding=True))
        params["item_group_ids"] = tuple(normalized_ids)
    else:
        query = text(sql +
                     " order by field(status,'VALIDATED','CANDIDATE'), confidence desc, updated_at desc limit :limit")
    rows = db.execute(query, params).mappings().all()
    items = [
        {
            "item_group_id": row.get("item_group_id"),
            "memory_type": row.get("memory_type"),
            "subject_key": row.get("subject_key"),
            "statement": row.get("statement"),
            "status": row.get("status"),
            "confidence": float(row.get("confidence") or 0),
            "independent_days": _integer(row.get("independent_days")),
            "evidence_orders": _integer(row.get("evidence_orders")),
            "success_count": _integer(row.get("success_count")),
            "failure_count": _integer(row.get("failure_count")),
            "evidence": _dict(row.get("evidence_json")),
            "first_observed_date": str(row.get("first_observed_date") or ""),
            "last_observed_date": str(row.get("last_observed_date") or ""),
            "last_validated_at": str(row.get("last_validated_at") or ""),
        }
        for row in rows
    ]
    return {
        "policy": {
            "fact_source": "gmv_mysql_structured_memory",
            "candidate_is_actionable": False,
            "validated_is_actionable": True,
            "validation_unit": "independent_advertiser_date",
        },
        "validated": [item for item in items if item["status"] == "VALIDATED"],
        "candidates": [item for item in items if item["status"] == "CANDIDATE"],
    }


def link_policy_evaluations_to_report(
    db: Session,
    *,
    scope: Mapping[str, Any],
    report_date: date,
    report_id: int,
) -> None:
    ensure_hermes_mysql_memory_tables(db)
    db.execute(
        text(
            """
            update gmv_hermes_ad_policy_evaluations
            set source_report_id=:report_id,
                updated_at=current_timestamp(6)
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and report_date=:report_date
            """
        ),
        {**scope, "report_date": report_date, "report_id": int(report_id)},
    )


def backfill_policy_memory_from_reports(db: Session, *, limit: int = 30) -> dict[str, int]:
    ensure_hermes_mysql_memory_tables(db)
    rows = db.execute(
        text(
            """
            select id, workspace_id, auth_id, advertiser_id, store_id,
                   report_date, input_json
            from gmv_hermes_ad_daily_reports
            where status='GENERATED'
              and input_json is not null
            order by report_date desc, id desc
            limit :limit
            """
        ),
        {"limit": max(1, min(int(limit), 365))},
    ).mappings().all()
    scopes: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    result = {"reports": 0, "evaluated": 0, "refreshed": 0}
    for row in reversed(rows):
        scope = {
            "workspace_id": int(row["workspace_id"]),
            "auth_id": int(row["auth_id"]),
            "advertiser_id": str(row["advertiser_id"]),
            "store_id": str(row["store_id"]),
        }
        report_input = _dict(row.get("input_json"))
        evaluation = record_policy_evaluations(
            db,
            scope=scope,
            report_date=row["report_date"],
            report_input=report_input,
            source_report_id=int(row["id"]),
        )
        result["reports"] += 1
        result["evaluated"] += evaluation["evaluated"]
        scopes[(scope["workspace_id"], scope["auth_id"], scope["advertiser_id"], scope["store_id"])] = scope
    for scope in scopes.values():
        result["refreshed"] += refresh_strategy_memory(db, scope=scope)["refreshed"]
    return result
