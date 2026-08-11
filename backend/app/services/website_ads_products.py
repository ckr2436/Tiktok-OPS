from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.hermes_agent import HermesContentProduct
from app.data.models.website_ads import WebsiteAdsLandingPage


_MATCH_STOP_WORDS = {
    "a",
    "and",
    "bottle",
    "bottles",
    "for",
    "myupona",
    "of",
    "pack",
    "the",
    "with",
}


def _facts_result(product: HermesContentProduct) -> dict[str, Any]:
    facts = product.facts_json if isinstance(product.facts_json, dict) else {}
    result = facts.get("result")
    return result if isinstance(result, dict) else {}


def _concise_product_details(product: HermesContentProduct) -> str:
    result = _facts_result(product)
    handoff = result.get("product_truth_handoff") if isinstance(result.get("product_truth_handoff"), dict) else {}
    passport = result.get("product_passport") if isinstance(result.get("product_passport"), dict) else {}
    sections: list[str] = []
    if product.product_brief:
        sections.append(str(product.product_brief).strip())
    if handoff:
        sections.append("内容工厂商品事实：\n" + "\n".join(f"{key}: {value}" for key, value in handoff.items() if value not in (None, "")))
    elif passport:
        sections.append("内容工厂商品档案：\n" + json.dumps(passport, ensure_ascii=False, indent=2, default=str))
    approved = result.get("approved_claims")
    if isinstance(approved, list) and approved:
        sections.append("允许使用的表述：" + "；".join(str(item) for item in approved if str(item).strip()))
    prohibited = result.get("prohibited_claims")
    if isinstance(prohibited, list) and prohibited:
        sections.append("禁止使用的表述：" + "；".join(str(item) for item in prohibited if str(item).strip()))
    return "\n\n".join(section for section in sections if section).strip()[:20000]


def content_product_summary(product: HermesContentProduct, *, include_facts: bool = False) -> dict[str, Any]:
    result = {
        "id": int(product.id),
        "product_key": product.product_key,
        "brand_name": product.brand_name,
        "product_name": product.product_name,
        "market": product.market,
        "product_brief": product.product_brief,
        "status": product.status,
        "updated_at": product.updated_at,
    }
    if include_facts:
        result["facts"] = product.facts_json
        result["inherited_product_details"] = _concise_product_details(product)
    return result


def get_content_product(db: Session, landing: WebsiteAdsLandingPage) -> HermesContentProduct | None:
    if not landing.content_product_id:
        return None
    product = db.get(HermesContentProduct, int(landing.content_product_id))
    if not product or product.workspace_id != landing.workspace_id or product.status != "active":
        return None
    return product


def effective_product_profile(db: Session, landing: WebsiteAdsLandingPage) -> dict[str, Any]:
    content_product = get_content_product(db, landing)
    inherited_details = _concise_product_details(content_product) if content_product else None
    return {
        "content_product": content_product_summary(content_product) if content_product else None,
        "brand": landing.brand or (content_product.brand_name if content_product else None),
        "content_name": landing.content_name or (content_product.product_name if content_product else landing.title),
        "description": landing.description or (content_product.product_brief if content_product else None),
        "product_details": landing.product_details or inherited_details,
        "seller_profile": landing.seller_profile,
        "promotion_text": landing.promotion_text,
    }


def bind_content_product(
    db: Session,
    landing: WebsiteAdsLandingPage,
    content_product_id: int | None,
) -> HermesContentProduct | None:
    if content_product_id is None:
        landing.content_product_id = None
        return None
    product = db.get(HermesContentProduct, int(content_product_id))
    if not product or product.workspace_id != landing.workspace_id or product.status != "active":
        raise ValueError("Content Factory product is unavailable")
    landing.content_product_id = product.id
    return product


def _match_tokens(value: str | None) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    normalized = {"easy" if token == "ease" else token for token in tokens}
    return {token for token in normalized if len(token) >= 3 and token not in _MATCH_STOP_WORDS}


def find_content_product_match(db: Session, landing: WebsiteAdsLandingPage) -> HermesContentProduct | None:
    if landing.content_product_id:
        return get_content_product(db, landing)
    landing_tokens = _match_tokens(" ".join(filter(None, [landing.title, landing.content_name, landing.brand])))
    if len(landing_tokens) < 2:
        return None
    candidates = db.scalars(
        select(HermesContentProduct).where(
            HermesContentProduct.workspace_id == landing.workspace_id,
            HermesContentProduct.status == "active",
        )
    ).all()
    ranked: list[tuple[float, HermesContentProduct]] = []
    for candidate in candidates:
        candidate_tokens = _match_tokens(f"{candidate.brand_name} {candidate.product_name}")
        shared = landing_tokens & candidate_tokens
        if len(shared) < 2:
            continue
        score = len(shared) / max(1, min(len(landing_tokens), len(candidate_tokens)))
        if score >= 0.6:
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (item[0], item[1].updated_at, item[1].id), reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def auto_bind_content_product(db: Session, landing: WebsiteAdsLandingPage) -> HermesContentProduct | None:
    match = find_content_product_match(db, landing)
    if match and not landing.content_product_id:
        landing.content_product_id = match.id
    return match


__all__ = [
    "auto_bind_content_product",
    "bind_content_product",
    "content_product_summary",
    "effective_product_profile",
    "find_content_product_match",
    "get_content_product",
]
