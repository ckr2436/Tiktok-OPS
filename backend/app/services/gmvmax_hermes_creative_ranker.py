from __future__ import annotations

from typing import Any, Mapping, Sequence


TIER_PRIORITY = {
    "WINNER": 5,
    "PROMISING": 4,
    "EXPLORATION": 3,
    "UNRATED": 2,
    "WEAK": 1,
    "REJECTED": 0,
}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _rank_one(
    candidate: Mapping[str, Any],
    *,
    product_price: float,
    minimum_roi: float,
) -> dict[str, Any]:
    item = dict(candidate)
    metrics = dict(item.get("metrics") or {})
    spend = _number(metrics.get("spend"))
    gmv = _number(metrics.get("gmv"))
    orders = int(_number(metrics.get("orders")))
    roi = _number(metrics.get("roi"))
    ctr = _number(metrics.get("ctr"))
    view_2s = _number(metrics.get("ad_video_view_rate_2s"))
    view_6s = _number(metrics.get("ad_video_view_rate_6s"))
    completion = _number(metrics.get("ad_video_view_rate_p100"))
    excluded = bool(item.get("historically_excluded"))
    selectable = bool(item.get("selectable"))

    prior_spend = max(product_price, 8.0)
    adjusted_roi = (gmv + prior_spend * minimum_roi) / max(spend + prior_spend, 0.01)
    evidence = _clamp((spend / max(product_price * 2.0, 15.0)) + (orders * 0.25))
    score = (
        _clamp(adjusted_roi / 2.0) * 42.0
        + _clamp(orders / 3.0) * 24.0
        + evidence * 12.0
        + _clamp(ctr / 0.03) * 8.0
        + _clamp(view_2s / 0.35) * 5.0
        + _clamp(view_6s / 0.18) * 5.0
        + _clamp(completion / 0.12) * 4.0
    )

    if excluded:
        tier = "REJECTED"
        score = max(0.0, score - 80.0)
        reason = "\u66fe\u88ab\u5b88\u62a4\u7b56\u7565\u6392\u9664\uff0c\u4e0d\u8fdb\u5165\u4f18\u8d28\u7d20\u6750\u63a8\u8350"
    elif not selectable:
        tier = "UNRATED"
        score = min(score, 20.0)
        reason = "\u7f3a\u5c11 TikTok \u89c6\u9891 ID \u6216\u6388\u6743\u8eab\u4efd\uff0c\u6682\u4e0d\u53ef\u6307\u5b9a\u6295\u653e"
    elif orders >= 2 and roi >= minimum_roi:
        tier = "WINNER"
        reason = f"\u5df2\u6709 {orders} \u5355\uff0cROAS {roi:.2f}\uff0c\u6210\u4ea4\u8bc1\u636e\u8f83\u5f3a"
    elif orders >= 1 and roi >= max(minimum_roi, 1.0):
        tier = "WINNER"
        reason = f"\u5df2\u6709\u6210\u4ea4\uff0cROAS {roi:.2f} \u8fbe\u6807"
    elif orders >= 1 and adjusted_roi >= minimum_roi * 0.8:
        tier = "PROMISING"
        reason = f"\u5df2\u6709 {orders} \u5355\uff0c\u98ce\u9669\u6821\u6b63 ROAS {adjusted_roi:.2f}"
    elif spend >= max(product_price * 1.5, 10.0) and orders == 0:
        tier = "WEAK"
        score = max(0.0, score - 35.0)
        reason = f"\u5df2\u82b1\u8d39 ${spend:.2f} \u4f46\u5c1a\u65e0\u6210\u4ea4"
    elif spend < max(product_price, 8.0) and (
        ctr >= 0.015 or view_2s >= 0.25 or view_6s >= 0.12 or completion >= 0.08
    ):
        tier = "EXPLORATION"
        reason = "\u4e92\u52a8\u6570\u636e\u8f83\u597d\uff0c\u4f46\u6210\u4ea4\u6837\u672c\u4e0d\u8db3\uff0c\u5efa\u8bae\u5c0f\u9884\u7b97\u9a8c\u8bc1"
    elif spend <= 0:
        tier = "UNRATED"
        reason = "\u6682\u65e0\u6295\u653e\u6570\u636e\uff0c\u5c5e\u4e8e\u5f85\u63a2\u7d22\u7d20\u6750"
    else:
        tier = "WEAK"
        reason = "\u73b0\u6709\u6570\u636e\u5c1a\u672a\u8fbe\u5230 Hermes \u63a8\u8350\u6807\u51c6"

    confidence = "high" if orders >= 3 or spend >= max(product_price * 3.0, 30.0) else (
        "medium" if orders >= 1 or spend >= max(product_price, 10.0) else "low"
    )
    item.update(
        {
            "score": round(score, 2),
            "hermes_tier": tier,
            "hermes_confidence": confidence,
            "hermes_reason": reason,
            "hermes_recommended": tier in {"WINNER", "PROMISING"},
            "hermes_adjusted_roi": round(adjusted_roi, 4),
            "ranking_source": "HERMES_PERFORMANCE_RANKER_V1",
        }
    )
    return item


def rank_creative_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    product_price: float | None = None,
    minimum_roi: float = 0.8,
    recommendation_limit: int = 4,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    effective_price = max(_number(product_price), 0.01)
    ranked = [
        _rank_one(
            candidate,
            product_price=effective_price,
            minimum_roi=max(_number(minimum_roi), 0.01),
        )
        for candidate in candidates
    ]
    # Python's sort is stable, so establish the unique resource key first and
    # preserve it whenever all Hermes performance signals are tied.
    ranked.sort(
        key=lambda item: str(
            item.get("item_id")
            or item.get("creative_id")
            or item.get("id")
            or ""
        )
    )
    ranked.sort(
        key=lambda item: (
            TIER_PRIORITY.get(str(item.get("hermes_tier") or ""), 0),
            _number(item.get("score")),
            _number((item.get("metrics") or {}).get("orders")),
            _number((item.get("metrics") or {}).get("spend")),
        ),
        reverse=True,
    )

    rank = 0
    for item in ranked:
        if item.get("hermes_recommended") and rank < int(recommendation_limit):
            rank += 1
            item["hermes_rank"] = rank
        else:
            item["hermes_recommended"] = False
            item["hermes_rank"] = None
    recommendation_mode = "scale"
    if rank == 0:
        recommendation_mode = "validation"
        validation_limit = max(1, min(int(recommendation_limit), 2))
        for item in ranked:
            if rank >= validation_limit:
                break
            if item.get("hermes_tier") != "EXPLORATION":
                continue
            if item.get("historically_excluded") or not item.get("selectable"):
                continue
            rank += 1
            item["hermes_recommended"] = True
            item["hermes_rank"] = rank
            item["hermes_reason"] = (
                str(item.get("hermes_reason") or "")
                + "\uff1bHermes \u5efa\u8bae\u4f5c\u4e3a\u5c0f\u9884\u7b97\u624b\u52a8\u9a8c\u8bc1\u5019\u9009"
            )

    summary = {
        "model": "HERMES_PERFORMANCE_RANKER_V1",
        "status": "ok",
        "evaluated": len(ranked),
        "recommended": rank,
        "recommendation_mode": recommendation_mode,
        "product_price": round(effective_price, 2),
        "minimum_roi": round(max(_number(minimum_roi), 0.01), 2),
        "has_proven_winners": any(item.get("hermes_tier") == "WINNER" for item in ranked),
    }
    return ranked, summary


__all__ = ["rank_creative_candidates"]
