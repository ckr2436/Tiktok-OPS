from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _campaign_key(row: Mapping[str, Any]) -> tuple[str]:
    return (str(row.get("campaign_id") or ""),)


def _creative_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("campaign_id") or ""),
        str(row.get("item_group_id") or ""),
        str(row.get("creative_id") or ""),
    )


def _add_ranked(
    selected: dict[tuple[str, ...], dict[str, Any]],
    rows: Iterable[dict[str, Any]],
    *,
    key_fn: Any,
    quota: int,
    limit: int,
) -> None:
    added = 0
    for row in rows:
        if len(selected) >= limit or added >= quota:
            return
        key = key_fn(row)
        if key in selected:
            continue
        selected[key] = row
        added += 1


def select_campaign_signals(campaigns: Sequence[Mapping[str, Any]], *, limit: int = 36) -> list[dict[str, Any]]:
    rows = _rows(campaigns)
    meaningful = [
        row
        for row in rows
        if _number(row.get("cost")) > 0
        or _number(row.get("gmv")) > 0
        or _integer(row.get("orders")) > 0
        or str(row.get("status") or "").upper() == "ENABLE"
    ]
    if meaningful:
        rows = meaningful
    if len(rows) <= limit:
        return rows

    by_spend = sorted(rows, key=lambda row: (_number(row.get("cost")), _number(row.get("gmv"))), reverse=True)
    winners = sorted(
        (row for row in rows if _integer(row.get("orders")) > 0),
        key=lambda row: (_integer(row.get("orders")), _number(row.get("gmv")), _number(row.get("roi"))),
        reverse=True,
    )
    loss_risks = sorted(
        (
            row
            for row in rows
            if _number(row.get("cost")) > 0
            and (_integer(row.get("orders")) == 0 or _number(row.get("roi")) < 0.8)
        ),
        key=lambda row: (_number(row.get("cost")), -_number(row.get("roi"))),
        reverse=True,
    )
    active = sorted(
        (row for row in rows if str(row.get("status") or "").upper() == "ENABLE"),
        key=lambda row: (_number(row.get("cost")), _integer(row.get("orders"))),
        reverse=True,
    )

    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    spend_quota = max(6, round(limit * 0.4))
    winner_quota = max(3, round(limit * 0.22))
    loss_quota = max(3, round(limit * 0.22))
    active_quota = max(2, limit - spend_quota - winner_quota - loss_quota)
    _add_ranked(selected, by_spend, key_fn=_campaign_key, quota=spend_quota, limit=limit)
    _add_ranked(selected, winners, key_fn=_campaign_key, quota=winner_quota, limit=limit)
    _add_ranked(selected, loss_risks, key_fn=_campaign_key, quota=loss_quota, limit=limit)
    _add_ranked(selected, active, key_fn=_campaign_key, quota=active_quota, limit=limit)
    _add_ranked(selected, by_spend, key_fn=_campaign_key, quota=limit, limit=limit)
    return list(selected.values())


def select_creative_signals(creatives: Sequence[Mapping[str, Any]], *, limit: int = 40) -> list[dict[str, Any]]:
    rows = _rows(creatives)
    if len(rows) <= limit:
        return rows

    by_spend = sorted(rows, key=lambda row: (_number(row.get("cost")), _number(row.get("gmv"))), reverse=True)
    product_cards = [row for row in by_spend if str(row.get("creative_id") or "") == "-1"]
    winners = sorted(
        (row for row in rows if _integer(row.get("orders")) > 0),
        key=lambda row: (_integer(row.get("orders")), _number(row.get("gmv")), _number(row.get("roi"))),
        reverse=True,
    )
    loss_risks = sorted(
        (
            row
            for row in rows
            if _number(row.get("cost")) > 0
            and (_integer(row.get("orders")) == 0 or _number(row.get("roi")) < 0.8)
        ),
        key=lambda row: (_number(row.get("cost")), -_number(row.get("roi"))),
        reverse=True,
    )
    excluded = sorted(
        (row for row in rows if "EXCLUD" in str(row.get("status") or "").upper()),
        key=lambda row: (_number(row.get("cost")), _integer(row.get("orders"))),
        reverse=True,
    )

    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    card_quota = max(2, round(limit * 0.16))
    spend_quota = max(5, round(limit * 0.3))
    winner_quota = max(4, round(limit * 0.25))
    loss_quota = max(3, round(limit * 0.2))
    excluded_quota = max(2, limit - card_quota - spend_quota - winner_quota - loss_quota)
    _add_ranked(selected, product_cards, key_fn=_creative_key, quota=card_quota, limit=limit)
    _add_ranked(selected, by_spend, key_fn=_creative_key, quota=spend_quota, limit=limit)
    _add_ranked(selected, winners, key_fn=_creative_key, quota=winner_quota, limit=limit)
    _add_ranked(selected, loss_risks, key_fn=_creative_key, quota=loss_quota, limit=limit)
    _add_ranked(selected, excluded, key_fn=_creative_key, quota=excluded_quota, limit=limit)
    _add_ranked(selected, by_spend, key_fn=_creative_key, quota=limit, limit=limit)
    return list(selected.values())


def _coverage(all_rows: Sequence[Mapping[str, Any]], selected_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | int]:
        return {
            "cost": round(sum(_number(row.get("cost")) for row in rows), 2),
            "gmv": round(sum(_number(row.get("gmv")) for row in rows), 2),
            "orders": sum(_integer(row.get("orders")) for row in rows),
        }

    complete = totals(all_rows)
    selected = totals(selected_rows)

    def ratio(name: str) -> float:
        denominator = _number(complete.get(name))
        if denominator <= 0:
            return 100.0
        return round(min(100.0, _number(selected.get(name)) * 100 / denominator), 2)

    return {
        "total_rows": len(all_rows),
        "selected_rows": len(selected_rows),
        "totals": complete,
        "selected_totals": selected,
        "coverage_pct": {"cost": ratio("cost"), "gmv": ratio("gmv"), "orders": ratio("orders")},
    }


def summarize_guard_events(events: Sequence[Mapping[str, Any]], *, latest_limit: int = 24) -> dict[str, Any]:
    rows = _rows(events)
    rollup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    material_rollup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        reason = str(row.get("reason") or "")[:160]
        key = (
            str(row.get("event_type") or ""),
            str(row.get("action") or ""),
            str(row.get("result") or ""),
            reason,
        )
        bucket = rollup.setdefault(
            key,
            {
                "event_type": key[0],
                "action": key[1],
                "result": key[2],
                "reason": key[3],
                "count": 0,
                "first_at": "",
                "latest_snapshot": {},
                "latest_at": "",
                "_creative_ids": set(),
            },
        )
        bucket["count"] += 1
        creative_id = str(row.get("creative_id") or "").strip()
        if creative_id:
            bucket["_creative_ids"].add(creative_id)
        created_at = str(row.get("created_at") or "")
        if not bucket["first_at"] or created_at < bucket["first_at"]:
            bucket["first_at"] = created_at
        if created_at > bucket["latest_at"]:
            bucket["latest_at"] = created_at
            bucket["latest_snapshot"] = {
                "cost": round(_number(row.get("cost")), 2),
                "gmv": round(_number(row.get("gmv")), 2),
                "orders": _integer(row.get("orders")),
            }

        if creative_id:
            material_key = (str(row.get("campaign_id") or ""), creative_id)
            material = material_rollup.setdefault(
                material_key,
                {
                    "campaign_id": material_key[0],
                    "creative_id": creative_id,
                    "item_group_id": str(row.get("item_group_id") or ""),
                    "count": 0,
                    "successful_adds": 0,
                    "successful_removes": 0,
                    "failed_actions": 0,
                    "first_at": "",
                    "latest_at": "",
                    "reasons": set(),
                },
            )
            material["count"] += 1
            action = str(row.get("action") or "").upper()
            result = str(row.get("result") or "").upper()
            if result == "SUCCESS" and action == "ADD":
                material["successful_adds"] += 1
            elif result == "SUCCESS" and action == "REMOVE":
                material["successful_removes"] += 1
            elif result != "SUCCESS":
                material["failed_actions"] += 1
            material["reasons"].add(reason)
            if not material["first_at"] or created_at < material["first_at"]:
                material["first_at"] = created_at
            if created_at > material["latest_at"]:
                material["latest_at"] = created_at

    grouped = []
    for bucket in rollup.values():
        creative_ids = sorted(bucket.pop("_creative_ids"))
        bucket["distinct_creatives"] = len(creative_ids)
        bucket["creative_ids"] = creative_ids[:20]
        grouped.append(bucket)
    grouped.sort(key=lambda item: (item["count"], item["latest_at"]), reverse=True)

    material_groups = []
    for material in material_rollup.values():
        material["reasons"] = sorted(material["reasons"])
        material["same_material_reentry"] = bool(
            material["successful_adds"] > 0 and material["successful_removes"] > 0
        )
        material_groups.append(material)
    material_groups.sort(key=lambda item: (item["count"], item["latest_at"]), reverse=True)

    latest = []
    for row in rows[:latest_limit]:
        item = dict(row)
        item["reason"] = str(item.get("reason") or "")[:240]
        item["error"] = str(item.get("error") or "")[:240]
        latest.append(item)
    return {
        "total_events": len(rows),
        "distinct_creatives": len(material_groups),
        "groups": grouped[:30],
        "material_groups": material_groups[:40],
        "latest_events": latest,
    }


def _all_product_ids(campaigns: Sequence[Mapping[str, Any]], creatives: Sequence[Mapping[str, Any]]) -> list[str]:
    values: set[str] = set()
    for campaign in campaigns:
        for item_group_id in campaign.get("item_group_ids") or []:
            value = str(item_group_id or "").strip()
            if value:
                values.add(value)
    for creative in creatives:
        value = str(creative.get("item_group_id") or "").strip()
        if value:
            values.add(value)
    return sorted(values)


def build_product_performance(campaigns: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate authoritative campaign metrics for single-product campaigns."""
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost": 0.0, "gmv": 0.0, "orders": 0, "campaigns": 0}
    )
    ambiguous_campaigns = 0
    for campaign in _rows(campaigns):
        item_group_ids = sorted(
            {
                str(item).strip()
                for item in campaign.get("item_group_ids") or []
                if str(item or "").strip()
            }
        )
        if len(item_group_ids) != 1:
            if item_group_ids:
                ambiguous_campaigns += 1
            continue
        item = grouped[item_group_ids[0]]
        item["cost"] += _number(campaign.get("cost"))
        item["gmv"] += _number(campaign.get("gmv"))
        item["orders"] += _integer(campaign.get("orders"))
        item["campaigns"] += 1

    result: list[dict[str, Any]] = []
    for item_group_id, item in grouped.items():
        cost = float(item["cost"])
        gmv = float(item["gmv"])
        result.append(
            {
                "item_group_id": item_group_id,
                "cost": round(cost, 2),
                "gmv": round(gmv, 2),
                "orders": int(item["orders"]),
                "roi": round(gmv / cost, 4) if cost > 0 else 0.0,
                "campaigns": int(item["campaigns"]),
                "source": "single_product_campaign_metrics",
                "ambiguous_campaigns_excluded": ambiguous_campaigns,
            }
        )
    return sorted(result, key=lambda item: (item["cost"], item["orders"]), reverse=True)


def build_report_context(
    *,
    campaigns: Sequence[Mapping[str, Any]],
    creatives: Sequence[Mapping[str, Any]],
    guard_events: Sequence[Mapping[str, Any]],
    learning_stats: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    campaign_rows = _rows(campaigns)
    creative_rows = _rows(creatives)
    selected_campaigns = select_campaign_signals(campaign_rows)
    selected_creatives = select_creative_signals(creative_rows)
    guard_summary = summarize_guard_events(guard_events)
    product_performance = build_product_performance(campaign_rows)
    return {
        "product_ids": _all_product_ids(campaign_rows, creative_rows),
        "product_performance": product_performance,
        "campaigns": selected_campaigns,
        "creatives_by_spend": selected_creatives,
        "guard_events": guard_summary["latest_events"],
        "guard_event_rollup": {
            "total_events": guard_summary["total_events"],
            "distinct_creatives": guard_summary["distinct_creatives"],
            "groups": guard_summary["groups"],
            "material_groups": guard_summary["material_groups"],
        },
        "learning_stats": _rows(learning_stats)[:40],
        "input_meta": {
            "selection_policy": "full_aggregate_plus_nonzero_or_active_campaigns_and_top_creative_evidence",
            "campaigns": _coverage(campaign_rows, selected_campaigns),
            "creatives": _coverage(creative_rows, selected_creatives),
            "guard_events": {
                "total_rows": guard_summary["total_events"],
                "selected_rows": len(guard_summary["latest_events"]),
                "rollup_groups": len(guard_summary["groups"]),
            },
        },
    }


def _product_signals(creatives: Sequence[Mapping[str, Any]], product_ids: Sequence[str]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cost": 0.0, "gmv": 0.0, "orders": 0, "creative_rows": 0, "product_card_cost": 0.0}
    )
    for creative in creatives:
        item_group_id = str(creative.get("item_group_id") or "").strip()
        if not item_group_id:
            continue
        item = grouped[item_group_id]
        item["cost"] += _number(creative.get("cost"))
        item["gmv"] += _number(creative.get("gmv"))
        item["orders"] += _integer(creative.get("orders"))
        item["creative_rows"] += 1
        if str(creative.get("creative_id") or "") == "-1":
            item["product_card_cost"] += _number(creative.get("cost"))

    for item_group_id in product_ids:
        grouped[str(item_group_id)]

    result: list[dict[str, Any]] = []
    for item_group_id, item in grouped.items():
        cost = float(item["cost"])
        gmv = float(item["gmv"])
        result.append(
            {
                "item_group_id": item_group_id,
                "cost": round(cost, 2),
                "gmv": round(gmv, 2),
                "orders": int(item["orders"]),
                "roi": round(gmv / cost, 4) if cost > 0 else 0.0,
                "creative_rows": int(item["creative_rows"]),
                "product_card_cost": round(float(item["product_card_cost"]), 2),
            }
        )
    return sorted(result, key=lambda item: (item["cost"], item["orders"]), reverse=True)


def build_decision_performance_context(report_input: Mapping[str, Any]) -> dict[str, Any]:
    campaigns = _rows(report_input.get("campaigns"))
    creatives = _rows(report_input.get("creatives_by_spend"))
    product_ids = [str(item) for item in report_input.get("product_ids") or [] if str(item or "").strip()]
    return {
        "report_date": report_input.get("report_date"),
        "scope": dict(report_input.get("scope") or {}),
        "summary": dict(report_input.get("summary") or {}),
        "input_meta": dict(report_input.get("input_meta") or {}),
        "strategy_memory": dict(report_input.get("strategy_memory") or {}),
        "product_ids": sorted(set(product_ids)),
        "product_performance": _rows(report_input.get("product_performance"))[:30],
        "creative_product_signals": _product_signals(creatives, product_ids)[:30],
        "campaign_signals": select_campaign_signals(campaigns, limit=18),
        "creative_signals": select_creative_signals(creatives, limit=24),
        "guard_event_rollup": dict(report_input.get("guard_event_rollup") or {}),
        "recent_guard_events": _rows(report_input.get("guard_events"))[:10],
        "learning_stats": _rows(report_input.get("learning_stats"))[:20],
    }
