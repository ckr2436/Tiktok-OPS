from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
import json
import logging
from pathlib import Path
import re
from typing import Any, Mapping

from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.website_ads import (
    WebsiteAdsCreativeAsset,
    WebsiteAdsDailyReport,
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsLandingPage,
    WebsiteAdsMediaPlan,
    WebsiteAdsMediaPlanCreative,
    WebsiteAdsMediaPlanGroup,
    WebsiteAdsMetricHourly,
)
from app.features.tenants.ttb.gmv_max._helpers import resolve_account_binding
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.hermes_agent.client import HermesAdsRealtimeClient, HermesAdsReviewClient, extract_output_text
from app.services.ttb_client_factory import build_ttb_client
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy
from app.services.website_ads_media_cache import (
    merge_tiktok_media_metadata,
    public_asset_media_url,
    resolve_asset_media,
)
from app.services.website_ads_products import _match_tokens, effective_product_profile
from app.services.website_ads_targeting_catalog import (
    match_general_interest_ids,
    record_targeting_discovery,
)
from app.services.website_ads_tiktok_contract import (
    TIKTOK_CONTIGUOUS_US_LOCATION_IDS,
    WEBSITE_ADS_PLACEMENT_TYPE,
    WEBSITE_ADS_PLACEMENTS,
    WEBSITE_ADS_OPTIMIZATION_EVENT,
    WEBSITE_ADS_OPTIMIZATION_EVENT_LABEL,
    normalize_website_sales_call_to_action,
    select_website_ads_pixel,
    select_tiktok_video_identity,
    website_ads_optimization_fields,
)


MIN_ADGROUP_DAILY_BUDGET = Decimal("20.00")
MIN_TARGETED_ADGROUPS_PER_PLAN = 3
MIN_CREATIVES_PER_GROUP = 4
MAX_CREATIVES_PER_GROUP = 6
MAX_AUTO_CANDIDATES = 40
MAX_AUTO_SELECTED_ASSETS = 24
MAX_ADGROUPS_PER_PLAN = 6
PROMPT_VERSION = "website_ads_media_planner_v8"

HEALTH_INTEREST_FALLBACK_KEYWORDS = (
    "wellness",
    "healthy lifestyle",
    "fitness",
    "yoga",
    "online shopping",
)
BEAUTY_INTEREST_FALLBACK_KEYWORDS = (
    "beauty",
    "personal care",
    "wellness",
    "fitness",
    "online shopping",
)

PRODUCTION_ORIGIN_PRIORITY = {
    "REAL_CREATOR": 5,
    "REAL_CUSTOMER": 4,
    "BRAND_STAFF": 3,
    "MIXED": 2,
    "UNKNOWN": 1,
    "AIGC": 0,
}
ALLOWED_GENDERS = {"GENDER_UNLIMITED", "GENDER_FEMALE", "GENDER_MALE"}
ALLOWED_AGE_GROUPS = {"AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"}

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _snapshot_items(payload: Any) -> tuple[list[Any], bool]:
    if isinstance(payload, list):
        return list(payload), True
    if not isinstance(payload, dict):
        return [], False
    for key in ("items", "list", "videos", "identity_list", "pixels"):
        if key in payload:
            value = payload.get(key)
            return (list(value), True) if isinstance(value, list) else ([], False)
    if "data" not in payload:
        return [], False
    return _snapshot_items(payload.get("data"))


def _items(payload: Any) -> list[Any]:
    """Return raw rows without hiding malformed entries from snapshot callers."""

    rows, _ = _snapshot_items(payload)
    return rows


def _extract_json_object(value: str) -> dict[str, Any]:
    text_value = str(value or "").strip()
    if not text_value:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text_value, flags=re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text_value.find("{"), text_value.rfind("}")
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


def _clean_list(value: Any, *, limit: int = 12, length: int = 160) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip()[:length] for item in value if str(item).strip()))[:limit]


def _normalize_creative_policy(analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(analysis or {})
    policy = assess_website_ads_creative_policy(normalized)
    normalized["policy_readiness"] = policy["readiness"]
    normalized["policy_flags"] = policy["flags"]
    return normalized


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _production_origin(asset: WebsiteAdsCreativeAsset, analysis: Mapping[str, Any] | None = None) -> str:
    details = dict(analysis or asset.hermes_analysis_json or {})
    if str(asset.source or "").upper() == "SPARK_AUTHORIZED_POST":
        return "REAL_CREATOR"
    proposed = str(details.get("production_origin") or "").upper()
    if proposed in PRODUCTION_ORIGIN_PRIORITY:
        return proposed
    talent_type = str(details.get("talent_type") or "").lower()
    if talent_type == "creator":
        return "REAL_CREATOR"
    if talent_type == "customer":
        return "REAL_CUSTOMER"
    if talent_type == "brand_staff":
        return "BRAND_STAFF"
    text_value = " ".join([
        str(asset.title or ""),
        str(asset.file_name or ""),
        " ".join(str(value) for value in (asset.tags_json or [])),
    ]).casefold()
    if any(marker in text_value for marker in ("ai generated", "aigc", "sora", "synthetic")):
        return "AIGC"
    return "UNKNOWN"


def _normalize_gender(value: Any, default: str = "GENDER_UNLIMITED") -> str:
    normalized = str(value or "").upper()
    return normalized if normalized in ALLOWED_GENDERS else default


def _normalize_age_groups(value: Any, default: list[str] | None = None) -> list[str]:
    values = value if isinstance(value, list) else []
    normalized = list(dict.fromkeys(str(item).upper() for item in values if str(item).upper() in ALLOWED_AGE_GROUPS))
    return normalized or list(default or ["AGE_25_34", "AGE_35_44", "AGE_45_54"])


def _normalize_audience_hypotheses(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        keywords = _clean_list(item.get("interest_keywords"), limit=5, length=80)
        if not keywords:
            continue
        result.append({
            "segment": str(item.get("segment") or f"Audience hypothesis {index + 1}")[:160],
            "interest_keywords": keywords,
            "gender": _normalize_gender(item.get("gender")),
            "age_groups": _normalize_age_groups(item.get("age_groups")),
            "rationale": str(item.get("rationale") or "Product-relevant audience hypothesis")[:500],
        })
    return result[:MAX_ADGROUPS_PER_PLAN]


def _contact_sheet_data_url(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    with Image.open(path) as image:
        frame = image.convert("RGB")
        frame.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        output = BytesIO()
        frame.save(output, format="JPEG", quality=78, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _canonical_product_name(product: WebsiteAdsLandingPage) -> str:
    return str(product.content_name or product.title or "").strip()


def _product_catalog(db: Session, workspace_id: int) -> list[dict[str, Any]]:
    products = db.scalars(
        select(WebsiteAdsLandingPage).where(
            WebsiteAdsLandingPage.workspace_id == int(workspace_id),
            WebsiteAdsLandingPage.is_active.is_(True),
        ).order_by(WebsiteAdsLandingPage.id)
    ).all()
    return [
        {
            "landing_page_id": int(product.id),
            "product_name": _canonical_product_name(product),
            "landing_page_title": product.title,
            "content_name": product.content_name,
            "brand": product.brand,
            "category": product.content_category,
            "product_details": effective_product_profile(db, product).get("product_details"),
        }
        for product in products
    ]


def _fallback_product_match(asset: WebsiteAdsCreativeAsset, catalog: list[dict[str, Any]], transcript: str) -> dict[str, Any]:
    source_tokens = _match_tokens(" ".join((asset.title or "", asset.file_name or "", asset.user_notes or "", transcript or "")))
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for product in catalog:
        product_tokens = _match_tokens(
            " ".join(
                str(product.get(key) or "")
                for key in ("product_name", "content_name", "brand", "category", "product_details")
            )
        )
        overlap = source_tokens & product_tokens
        score = len(overlap) / max(1, min(5, len(product_tokens)))
        if score > best[0]:
            best = (score, product)
    if not best[1] or best[0] < 0.2:
        return {"landing_page_id": None, "product_name": None, "confidence": 0.0, "evidence": []}
    return {
        "landing_page_id": int(best[1]["landing_page_id"]),
        "product_name": best[1]["product_name"],
        "confidence": round(min(0.75, 0.3 + best[0]), 3),
        "evidence": ["title, filename, notes, or transcript token overlap"],
    }


def _product_snapshot(db: Session, product: WebsiteAdsLandingPage) -> dict[str, Any]:
    effective = effective_product_profile(db, product)
    return {
        "id": int(product.id),
        "content_product_id": int(product.content_product_id) if product.content_product_id else None,
        "tiktok_shop_product_id": product.product_id,
        "product_name": _canonical_product_name(product),
        "landing_page_title": product.title,
        "title": product.title,
        "content_name": effective["content_name"],
        "brand": effective["brand"],
        "category": product.content_category,
        "description": effective["description"],
        "product_details": effective["product_details"],
        "seller_profile": effective["seller_profile"],
        "promotion_text": effective["promotion_text"],
        "reference_price": float(product.reference_price) if product.reference_price is not None else None,
        "currency": product.currency,
        "landing_url": product.landing_url,
        "content_factory_product": _json_safe(effective["content_product"]),
    }


def _asset_snapshot(asset: WebsiteAdsCreativeAsset) -> dict[str, Any]:
    return {
        "id": int(asset.id),
        "video_id": asset.video_id,
        "source": asset.source,
        "title": asset.title,
        "file_name": asset.file_name,
        "duration_seconds": float(asset.duration_seconds) if asset.duration_seconds is not None else None,
        "dimensions": [asset.width, asset.height],
        "user_notes": asset.user_notes,
        "tags": list(asset.tags_json or []),
        "analysis_status": asset.analysis_status,
        "analysis_version": asset.analysis_version,
        "transcript": str(asset.transcript_text or "")[:12000],
        "transcript_language": asset.transcript_language,
        "evidence": dict(asset.analysis_inputs_json or {}),
        "analysis": dict(asset.hermes_analysis_json or {}),
    }


def _asset_performance(db: Session, asset: WebsiteAdsCreativeAsset) -> dict[str, Any]:
    row = db.execute(
        select(
            func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.conversion_value), 0),
            func.min(WebsiteAdsMetricHourly.stat_hour),
            func.max(WebsiteAdsMetricHourly.stat_hour),
        )
        .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
        .join(WebsiteAdsCampaign, WebsiteAdsCampaign.id == WebsiteAdsAd.campaign_local_id)
        .where(
            WebsiteAdsCampaign.workspace_id == asset.workspace_id,
            WebsiteAdsCampaign.auth_id == asset.auth_id,
            WebsiteAdsCampaign.advertiser_id == asset.advertiser_id,
            WebsiteAdsAd.video_id == asset.video_id,
        )
    ).one()
    spend, impressions, clicks, conversions, value, first_hour, last_hour = row
    spend = Decimal(str(spend or 0))
    value = Decimal(str(value or 0))
    clicks = int(clicks or 0)
    impressions = int(impressions or 0)
    conversions = Decimal(str(conversions or 0))
    return {
        "spend": float(spend),
        "impressions": impressions,
        "clicks": clicks,
        "conversions": float(conversions),
        "conversion_value": float(value),
        "ctr": clicks / impressions if impressions else 0.0,
        "cpc": float(spend / clicks) if clicks else 0.0,
        "cpa": float(spend / conversions) if conversions else 0.0,
        "roas": float(value / spend) if spend else 0.0,
        "first_observed_at": first_hour,
        "last_observed_at": last_hour,
    }


def _product_performance(db: Session, product: WebsiteAdsLandingPage) -> dict[str, Any]:
    row = db.execute(
        select(
            func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
            func.coalesce(func.sum(WebsiteAdsMetricHourly.conversion_value), 0),
            func.count(func.distinct(WebsiteAdsCampaign.id)),
        )
        .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
        .join(WebsiteAdsCampaign, WebsiteAdsCampaign.id == WebsiteAdsAd.campaign_local_id)
        .where(
            WebsiteAdsCampaign.workspace_id == product.workspace_id,
            WebsiteAdsCampaign.landing_page_id == product.id,
        )
    ).one()
    spend, conversions, value, campaign_count = row
    spend = Decimal(str(spend or 0))
    value = Decimal(str(value or 0))
    return {
        "spend": float(spend),
        "conversions": float(Decimal(str(conversions or 0))),
        "conversion_value": float(value),
        "roas": float(value / spend) if spend else 0.0,
        "campaign_count": int(campaign_count or 0),
    }


def _targeting_performance_history(
    db: Session,
    product: WebsiteAdsLandingPage,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    groups = list(
        db.scalars(
            select(WebsiteAdsAdGroup)
            .join(WebsiteAdsCampaign, WebsiteAdsCampaign.id == WebsiteAdsAdGroup.campaign_local_id)
            .where(
                WebsiteAdsCampaign.workspace_id == product.workspace_id,
                WebsiteAdsCampaign.landing_page_id == product.id,
            )
            .order_by(WebsiteAdsAdGroup.created_at.desc())
        ).all()
    )
    history: list[dict[str, Any]] = []
    for group in groups:
        spend, impressions, clicks, conversions = db.execute(
            select(
                func.coalesce(func.sum(WebsiteAdsMetricHourly.spend), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.impressions), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.clicks), 0),
                func.coalesce(func.sum(WebsiteAdsMetricHourly.conversions), 0),
            )
            .join(WebsiteAdsAd, WebsiteAdsAd.id == WebsiteAdsMetricHourly.ad_local_id)
            .where(WebsiteAdsAd.adgroup_local_id == group.id)
        ).one()
        spend_value = Decimal(str(spend or 0))
        impression_count = int(impressions or 0)
        click_count = int(clicks or 0)
        targeting = dict(group.targeting_json or {})
        history.append({
            "adgroup_local_id": int(group.id),
            "name": group.name,
            "audience_segment": targeting.get("audience_segment"),
            "interest_keywords": list(targeting.get("interest_keywords") or []),
            "interest_category_ids": list(targeting.get("interest_category_ids") or []),
            "gender": targeting.get("gender"),
            "age_groups": list(targeting.get("age_groups") or []),
            "spend": float(spend_value),
            "impressions": impression_count,
            "clicks": click_count,
            "ctr": click_count / impression_count if impression_count else 0.0,
            "cpc": float(spend_value / click_count) if click_count else None,
            "view_content_events": float(Decimal(str(conversions or 0))),
        })
    history.sort(key=lambda item: (item["spend"], item["clicks"]), reverse=True)
    return history[:limit]


def _daily_audience_learning(
    db: Session,
    product: WebsiteAdsLandingPage,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(WebsiteAdsDailyReport)
            .where(
                WebsiteAdsDailyReport.workspace_id == int(product.workspace_id),
                WebsiteAdsDailyReport.landing_page_id == int(product.id),
            )
            .order_by(WebsiteAdsDailyReport.report_date.desc(), WebsiteAdsDailyReport.id.desc())
            .limit(max(1, int(limit)))
        ).all()
    )
    learning: list[dict[str, Any]] = []
    for row in rows:
        report = dict(row.hermes_report_json or {})
        learning.append(
            {
                "report_date": row.report_date.isoformat(),
                "status": row.status,
                "audience_learning": list(report.get("audience_learning") or []),
                "next_audience_tests": list(report.get("next_audience_tests") or []),
            }
        )
    return learning


def _asset_product_confidence(asset: WebsiteAdsCreativeAsset, product_id: int) -> float:
    if asset.landing_page_id is not None:
        return 1.0 if int(asset.landing_page_id) == int(product_id) else 0.0
    analysis = asset.hermes_analysis_json if isinstance(asset.hermes_analysis_json, dict) else {}
    match = analysis.get("product_match") if isinstance(analysis.get("product_match"), dict) else {}
    matched_id = int(match.get("landing_page_id")) if str(match.get("landing_page_id") or "").isdigit() else None
    if matched_id is not None:
        return max(0.0, min(1.0, _safe_float(match.get("confidence")))) if matched_id == int(product_id) else 0.0
    return 0.25


def _creative_selection_score(
    performance: Mapping[str, Any],
    *,
    product_confidence: float,
    analysis_status: str,
    production_origin: str,
    reference_price: float,
) -> float:
    del reference_price
    spend = max(0.0, _safe_float(performance.get("spend")))
    impressions = max(0, int(_safe_float(performance.get("impressions"))))
    clicks = max(0, int(_safe_float(performance.get("clicks"))))
    conversions = max(0.0, _safe_float(performance.get("conversions")))
    ctr = clicks / impressions if impressions else 0.0
    cpc = spend / clicks if clicks else 0.0
    cost_per_view_content = spend / conversions if conversions else 0.0

    score = 30.0 * max(0.0, min(1.0, product_confidence))
    score += 10.0 if analysis_status == "READY" else 5.0 if analysis_status == "PARTIAL" else 0.0
    score += {
        "REAL_CREATOR": 35.0,
        "REAL_CUSTOMER": 28.0,
        "BRAND_STAFF": 15.0,
        "MIXED": 6.0,
        "UNKNOWN": 0.0,
        "AIGC": -30.0,
    }.get(production_origin, 0.0)
    score += min(10.0, ctr * 250.0)
    if clicks:
        score += min(8.0, clicks * 0.25)
        score += max(0.0, 8.0 - cpc * 4.0)
    if conversions > 0:
        score += 25.0 + min(16.0, conversions * 2.0)
        score += max(0.0, 12.0 - cost_per_view_content * 3.0)
    elif spend <= 1.0:
        score += 20.0
    else:
        score += max(-30.0, 12.0 - spend * 2.0 - clicks * 0.35)
    return round(score, 4)


def _auto_select_assets(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    product: WebsiteAdsLandingPage,
) -> tuple[list[WebsiteAdsCreativeAsset], list[dict[str, Any]]]:
    assets = list(
        db.scalars(
            select(WebsiteAdsCreativeAsset).where(
                WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
                WebsiteAdsCreativeAsset.auth_id == int(auth_id),
                WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
                WebsiteAdsCreativeAsset.is_active.is_(True),
            ).order_by(WebsiteAdsCreativeAsset.updated_at.desc())
        ).all()
    )
    reference_price = float(product.reference_price or 0)
    ranked: list[dict[str, Any]] = []
    for asset in assets:
        if str(asset.analysis_status or "").upper() != "READY":
            continue
        confidence = _asset_product_confidence(asset, int(product.id))
        if confidence < 0.35:
            continue
        performance = _asset_performance(db, asset)
        analysis = asset.hermes_analysis_json if isinstance(asset.hermes_analysis_json, dict) else {}
        production_origin = _production_origin(asset, analysis)
        policy = assess_website_ads_creative_policy(analysis)
        if not policy["eligible_for_automatic_launch"]:
            continue
        ranked.append({
            "asset": asset,
            "score": _creative_selection_score(
                performance,
                product_confidence=confidence,
                analysis_status=str(asset.analysis_status or ""),
                production_origin=production_origin,
                reference_price=reference_price,
            ),
            "product_confidence": confidence,
            "performance": performance,
            "creative_type": str(analysis.get("creative_type") or "unclassified"),
            "talent_type": str(analysis.get("talent_type") or "unknown"),
            "production_origin": production_origin,
            "policy_readiness": policy["readiness"],
            "policy_flags": policy["flags"],
        })
    ranked.sort(
        key=lambda item: (
            PRODUCTION_ORIGIN_PRIORITY.get(item["production_origin"], 0),
            item["score"],
            item["performance"].get("clicks", 0),
        ),
        reverse=True,
    )
    prompt_pool = ranked[:MAX_AUTO_CANDIDATES]

    selected: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    for candidate in prompt_pool:
        creative_type = candidate["creative_type"]
        if creative_type not in seen_types:
            selected.append(candidate)
            seen_types.add(creative_type)
        if len(selected) >= MAX_AUTO_SELECTED_ASSETS:
            break
    for candidate in prompt_pool:
        if candidate not in selected:
            selected.append(candidate)
        if len(selected) >= MAX_AUTO_SELECTED_ASSETS:
            break
    return [item["asset"] for item in selected], prompt_pool


def _fallback_product_analysis(db: Session, product: WebsiteAdsLandingPage) -> dict[str, Any]:
    snapshot = _product_snapshot(db, product)
    keywords = [product.content_category, product.title, snapshot["brand"]]
    return {
        "summary": snapshot["description"] or snapshot["product_details"] or product.title,
        "target_segments": ["Adults actively researching the product category", "Problem-aware shoppers ready to compare solutions"],
        "pain_points": ["Need a clear, credible reason to purchase now"],
        "value_propositions": [product.title, product.promotion_text or "Direct-to-consumer offer"],
        "interest_keywords": [str(item)[:80] for item in keywords if item][:6],
        "audience_hypotheses": [
            {
                "segment": "Problem-aware category shoppers",
                "interest_keywords": [str(item)[:80] for item in keywords if item][:3],
                "gender": "GENDER_UNLIMITED",
                "age_groups": ["AGE_25_34", "AGE_35_44", "AGE_45_54"],
                "rationale": "Reach adults actively researching this product category.",
            },
            {
                "segment": "Solution comparison shoppers",
                "interest_keywords": [str(product.content_category or product.title)[:80], "online shopping"],
                "gender": "GENDER_UNLIMITED",
                "age_groups": ["AGE_25_34", "AGE_35_44", "AGE_45_54"],
                "rationale": "Reach adults comparing relevant products and offers.",
            },
            {
                "segment": "Lifestyle affinity shoppers",
                "interest_keywords": [str(product.title)[:80], "health and wellness"],
                "gender": "GENDER_UNLIMITED",
                "age_groups": ["AGE_25_34", "AGE_35_44", "AGE_45_54"],
                "rationale": "Reach a distinct adjacent lifestyle audience with product relevance.",
            },
        ],
        "ad_angles": ["Problem and solution", "Product demonstration", "Offer and urgency"],
        "compliance_notes": ["Use only substantiated product claims and the real promotion terms"],
    }


async def analyze_product_profile(db: Session, product: WebsiteAdsLandingPage) -> WebsiteAdsLandingPage:
    product.analysis_status = "ANALYZING"
    product.analysis_error = None
    db.add(product)
    db.commit()
    source = "HERMES"
    raw_response: dict[str, Any] = {}
    try:
        client = HermesAdsReviewClient()
        response, _ = await client.create_response(
            input_text=json.dumps(_product_snapshot(db, product), ensure_ascii=False, default=str),
            instructions=(
                "You are a senior TikTok performance marketing strategist. Analyze only the supplied product facts, seller facts, "
                "real selling price, and promotion. Return one strict JSON object with keys summary, target_segments, pain_points, "
                "value_propositions, interest_keywords, audience_hypotheses, ad_angles, compliance_notes. audience_hypotheses must "
                "contain 3 to 6 distinct objects with segment, TikTok-searchable interest_keywords, gender, age_groups, and rationale. "
                "Use only supported gender values GENDER_UNLIMITED, GENDER_FEMALE, GENDER_MALE and age values AGE_18_24 through "
                "AGE_55_100. Do not invent discounts, claims, certifications, or product capabilities."
            ),
            metadata={"source": "website_ads_product_analysis", "prompt_version": PROMPT_VERSION, "product_id": str(product.id)},
        )
        raw_response = _extract_json_object(extract_output_text(response))
        if not raw_response:
            raise ValueError("Hermes returned an invalid product analysis")
    except Exception as exc:
        source = "BOUNDED_FALLBACK"
        raw_response = _fallback_product_analysis(db, product)
        product.analysis_error = f"{type(exc).__name__}: {exc}"[:2000]

    analysis = {
        "summary": str(raw_response.get("summary") or product.title)[:2000],
        "target_segments": _clean_list(raw_response.get("target_segments")),
        "pain_points": _clean_list(raw_response.get("pain_points")),
        "value_propositions": _clean_list(raw_response.get("value_propositions")),
        "interest_keywords": _clean_list(raw_response.get("interest_keywords"), limit=10, length=80),
        "audience_hypotheses": _normalize_audience_hypotheses(raw_response.get("audience_hypotheses")),
        "ad_angles": _clean_list(raw_response.get("ad_angles")),
        "compliance_notes": _clean_list(raw_response.get("compliance_notes")),
        "source": source,
        "prompt_version": PROMPT_VERSION,
    }
    product.hermes_analysis_json = analysis
    product.analysis_status = "READY"
    product.analyzed_at = _utcnow()
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


async def analyze_creative_asset(
    db: Session,
    asset: WebsiteAdsCreativeAsset,
    *,
    transcript: str | None = None,
    contact_sheet_path: Path | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> WebsiteAdsCreativeAsset:
    asset.analysis_status = "ANALYZING"
    asset.analysis_error = None
    db.add(asset)
    db.commit()
    raw_response: dict[str, Any] = {}
    source = "HERMES"
    transcript_text = str(transcript if transcript is not None else asset.transcript_text or "")[:50000]
    catalog = _product_catalog(db, int(asset.workspace_id))
    performance = _asset_performance(db, asset)
    fallback_match = _fallback_product_match(asset, catalog, transcript_text)
    analysis_input = {
        **_asset_snapshot(asset),
        "transcript": transcript_text[:12000],
        "extraction_evidence": dict(evidence or asset.analysis_inputs_json or {}),
        "product_catalog": catalog,
        "historical_performance": performance,
    }
    instructions = (
        "You are the asset intelligence analyst for a production TikTok advertising system. Analyze the supplied transcript, "
        "keyframe contact sheet when present, metadata, product catalog, and historical delivery results. Return one strict JSON "
        "object with keys: product_match, creative_type, talent_type, production_origin, is_real_creator, video_description, "
        "creator_style, hook_type, funnel_stage, visual_summary, "
        "opening_hook, content_angle, key_scenes, spoken_claims, call_to_action, audience_signals, strengths, risks, testing_notes, "
        "policy_readiness, policy_flags, "
        "and tags. product_match must contain landing_page_id, product_name, confidence from 0 to 1, and evidence. creative_type "
        "should be one of ugc, creator_endorsement, testimonial, product_demo, unboxing, problem_solution, lifestyle, offer, "
        "comparison, how_to, brand_story, or other. talent_type should be creator, customer, brand_staff, voiceover, no_person, "
        "or unknown. production_origin must be REAL_CREATOR, REAL_CUSTOMER, BRAND_STAFF, MIXED, AIGC, or UNKNOWN. Classify from "
        "visible evidence and metadata, never from production polish alone. video_description must be a concise factual description "
        "of the people, product, hook, scenes, and format for media planning. policy_readiness must be APPROVED, REVIEW, or BLOCKED. Use BLOCKED for medical or professional endorsement, "
        "prescription or diagnosis references, guaranteed outcomes, and ingredient, serving-size, dosage, or formula claims that do "
        "not exactly match the supplied verified catalog. Use REVIEW when evidence is incomplete or ambiguous. Only use APPROVED when "
        "the creative can be launched unchanged. policy_flags must explain every REVIEW or BLOCKED decision. Use only supplied facts, "
        "distinguish visual evidence from transcript evidence, and never invent product claims."
    )
    try:
        client = HermesAdsReviewClient()
        input_text = json.dumps(analysis_input, ensure_ascii=False, default=str)
        image_url = _contact_sheet_data_url(contact_sheet_path)
        try:
            response, _ = await client.create_response(
                input_text=input_text,
                input_items=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": input_text},
                        {"type": "input_image", "image_url": image_url, "detail": "low"},
                    ],
                }] if image_url else None,
                instructions=instructions,
                metadata={"source": "website_ads_creative_analysis", "prompt_version": PROMPT_VERSION, "asset_id": str(asset.id)},
            )
        except Exception:
            response, _ = await client.create_response(
                input_text=input_text,
                instructions=instructions,
                metadata={"source": "website_ads_creative_analysis_text_fallback", "prompt_version": PROMPT_VERSION, "asset_id": str(asset.id)},
            )
        raw_response = _extract_json_object(extract_output_text(response))
        if not raw_response:
            raise ValueError("Hermes returned an invalid creative analysis")
    except Exception as exc:
        source = "BOUNDED_FALLBACK"
        asset.analysis_error = f"{type(exc).__name__}: {exc}"[:2000]
        raw_response = {
            "product_match": fallback_match,
            "creative_type": "other",
            "talent_type": "unknown",
            "production_origin": _production_origin(asset, {}),
            "is_real_creator": str(asset.source or "").upper() == "SPARK_AUTHORIZED_POST",
            "video_description": f"Automatic analysis is pending for {asset.title}",
            "creator_style": False,
            "hook_type": "metadata_only",
            "funnel_stage": "prospecting",
            "visual_summary": "Visual classification is pending usable keyframes",
            "opening_hook": asset.title,
            "content_angle": "Unclassified",
            "key_scenes": [],
            "spoken_claims": [],
            "call_to_action": None,
            "audience_signals": list(asset.tags_json or []),
            "strengths": ["Available in the authorized TikTok asset library"],
            "risks": ["Creative meaning is based on incomplete extraction evidence"],
            "testing_notes": ["Keep an exploration slot until enough delivery evidence is available"],
            "policy_readiness": "REVIEW",
            "policy_flags": ["incomplete transcript and visual extraction evidence"],
            "tags": list(asset.tags_json or []),
        }

    valid_product_ids = {int(item["landing_page_id"]): item for item in catalog}
    proposed_match = raw_response.get("product_match") if isinstance(raw_response.get("product_match"), dict) else {}
    proposed_id = int(proposed_match.get("landing_page_id")) if str(proposed_match.get("landing_page_id") or "").isdigit() else None
    confidence = max(0.0, min(1.0, _safe_float(proposed_match.get("confidence"))))
    if proposed_id not in valid_product_ids:
        proposed_match = fallback_match
        proposed_id = proposed_match.get("landing_page_id")
        confidence = _safe_float(proposed_match.get("confidence"))
    if asset.landing_page_id and int(asset.landing_page_id) in valid_product_ids:
        proposed_id = int(asset.landing_page_id)
        confidence = max(confidence, 0.98)
        proposed_match = {
            "landing_page_id": proposed_id,
            "product_name": valid_product_ids[proposed_id]["product_name"],
            "confidence": confidence,
            "evidence": ["existing verified asset-to-product binding"],
        }
    elif proposed_id and confidence >= 0.68:
        asset.landing_page_id = int(proposed_id)

    creative_type = str(raw_response.get("creative_type") or "other").lower()[:64]
    talent_type = str(raw_response.get("talent_type") or "unknown").lower()[:64]
    production_origin = str(raw_response.get("production_origin") or "").upper()
    if production_origin not in PRODUCTION_ORIGIN_PRIORITY:
        production_origin = _production_origin(asset, {"talent_type": talent_type})
    if str(asset.source or "").upper() == "SPARK_AUTHORIZED_POST":
        production_origin = "REAL_CREATOR"
    generated_tags = _clean_list(raw_response.get("tags"), limit=20, length=64)
    generated_tags.extend(value for value in (creative_type, talent_type) if value not in generated_tags)
    asset.hermes_analysis_json = {
        "product_match": {
            "landing_page_id": int(proposed_id) if proposed_id else None,
            "product_name": proposed_match.get("product_name"),
            "confidence": round(confidence, 4),
            "evidence": _clean_list(proposed_match.get("evidence"), limit=8, length=240),
        },
        "creative_type": creative_type,
        "talent_type": talent_type,
        "production_origin": production_origin,
        "is_real_creator": production_origin in {"REAL_CREATOR", "REAL_CUSTOMER"},
        "video_description": str(
            raw_response.get("video_description") or raw_response.get("visual_summary") or asset.title
        )[:2000],
        "creator_style": str(raw_response.get("creator_style") or "unknown")[:100],
        "hook_type": str(raw_response.get("hook_type") or "unknown")[:100],
        "funnel_stage": str(raw_response.get("funnel_stage") or "prospecting")[:100],
        "visual_summary": str(raw_response.get("visual_summary") or "")[:2000],
        "opening_hook": str(raw_response.get("opening_hook") or asset.title)[:500],
        "content_angle": str(raw_response.get("content_angle") or "")[:500],
        "key_scenes": _clean_list(raw_response.get("key_scenes"), limit=12, length=300),
        "spoken_claims": _clean_list(raw_response.get("spoken_claims"), limit=16, length=300),
        "call_to_action": str(raw_response.get("call_to_action") or "")[:300] or None,
        "audience_signals": _clean_list(raw_response.get("audience_signals")),
        "strengths": _clean_list(raw_response.get("strengths")),
        "risks": _clean_list(raw_response.get("risks")),
        "testing_notes": _clean_list(raw_response.get("testing_notes")),
        "policy_readiness": str(raw_response.get("policy_readiness") or "REVIEW").upper()[:16],
        "policy_flags": _clean_list(raw_response.get("policy_flags"), limit=12, length=240),
        "tags": list(dict.fromkeys(generated_tags))[:20],
        "historical_performance": _json_safe(performance),
        "evidence": _json_safe(dict(evidence or asset.analysis_inputs_json or {})),
        "source": source,
        "prompt_version": PROMPT_VERSION,
    }
    asset.hermes_analysis_json = _normalize_creative_policy(asset.hermes_analysis_json)
    asset.tags_json = list(dict.fromkeys([*(asset.tags_json or []), *generated_tags]))[:30]
    extracted = dict(evidence or asset.analysis_inputs_json or {})
    asset.analysis_status = "READY" if (
        extracted.get("transcript_status") == "success" or extracted.get("contact_sheet_status") == "success"
    ) else "PARTIAL"
    asset.analysis_version = str(extracted.get("analysis_version") or asset.analysis_version or "metadata_only_v2")[:64]
    asset.analyzed_at = _utcnow()
    asset.analysis_next_retry_at = None
    if str(asset.auto_launch_status or "PENDING").upper() != "DEPLOYED":
        asset.auto_launch_status = "PENDING"
        asset.auto_launch_next_retry_at = None
        asset.auto_launch_error = None
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def sync_creative_assets(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    videos_payload: dict[str, Any],
    complete_snapshot: bool = False,
) -> list[WebsiteAdsCreativeAsset]:
    """Upsert advertiser-library videos from one official snapshot.

    ``complete_snapshot`` must only be set after ``list_all_videos`` has
    finished every official page successfully.  Keeping the default
    fail-closed prevents a partial payload (or a future filtered caller) from
    deactivating valid cached assets.
    """

    seen: set[str] = set()
    snapshot_rows, snapshot_shape_valid = _snapshot_items(videos_payload)
    invalid_rows = 0
    now = _utcnow()
    for raw_item in snapshot_rows:
        if not isinstance(raw_item, Mapping):
            invalid_rows += 1
            continue
        item = dict(raw_item)
        video_id = str(item.get("video_id") or item.get("material_id") or item.get("id") or "").strip()
        if not video_id:
            invalid_rows += 1
            continue
        if video_id in seen:
            invalid_rows += 1
            continue
        seen.add(video_id)
        row = db.scalar(
            select(WebsiteAdsCreativeAsset).where(
                WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
                WebsiteAdsCreativeAsset.auth_id == int(auth_id),
                WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
                WebsiteAdsCreativeAsset.video_id == video_id,
            )
        ) or WebsiteAdsCreativeAsset(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
            video_id=video_id,
            title=str(item.get("file_name") or item.get("video_name") or item.get("name") or video_id),
        )
        previous_source_url = str(row.preview_url or "").strip()
        remote_preview_url = item.get("preview_url")
        remote_cover_url = item.get("video_cover_url") or item.get("cover_url")
        row.file_name = str(item.get("file_name") or item.get("video_name") or row.file_name or row.title)
        if not row.title:
            row.title = row.file_name or video_id
        row.preview_url = remote_preview_url or row.preview_url
        row.cover_url = remote_cover_url or row.cover_url
        if item.get("duration") not in (None, ""):
            row.duration_seconds = Decimal(str(item["duration"]))
        row.width = int(item["width"]) if item.get("width") not in (None, "") else row.width
        row.height = int(item["height"]) if item.get("height") not in (None, "") else row.height
        row.source = "ADVERTISER_LIBRARY"
        row.raw_json = merge_tiktok_media_metadata(
            row.raw_json if isinstance(row.raw_json, dict) else {},
            item,
            video_url=remote_preview_url,
            cover_url=remote_cover_url,
        )
        if row.id is not None and resolve_asset_media(row, "video"):
            row.preview_url = public_asset_media_url(row, "video")
        if row.id is not None and resolve_asset_media(row, "cover"):
            row.cover_url = public_asset_media_url(row, "cover")
        if not previous_source_url and str(row.preview_url or "").strip() and row.analysis_status == "PARTIAL":
            row.analysis_status = "NOT_ANALYZED"
            row.analysis_version = None
            row.analysis_error = None
        row.last_synced_at = now
        row.is_active = True
        db.add(row)

    db.flush()
    deactivate_allowed = bool(
        complete_snapshot and snapshot_shape_valid and invalid_rows == 0
    )
    if complete_snapshot and not deactivate_allowed:
        logger.warning(
            "Website Ads advertiser-library snapshot contains invalid rows; "
            "absence reconciliation disabled",
            extra={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "invalid_rows": int(invalid_rows),
                "snapshot_shape_valid": bool(snapshot_shape_valid),
            },
        )
    if deactivate_allowed:
        existing = list(
            db.scalars(
                select(WebsiteAdsCreativeAsset).where(
                    WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
                    WebsiteAdsCreativeAsset.auth_id == int(auth_id),
                    WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
                    WebsiteAdsCreativeAsset.source == "ADVERTISER_LIBRARY",
                )
            ).all()
        )
        for row in existing:
            if row.video_id not in seen:
                row.is_active = False
                row.last_synced_at = now
                db.add(row)
    db.commit()
    rows = db.scalars(
        select(WebsiteAdsCreativeAsset).where(
            WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
            WebsiteAdsCreativeAsset.auth_id == int(auth_id),
            WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
            WebsiteAdsCreativeAsset.source == "ADVERTISER_LIBRARY",
            WebsiteAdsCreativeAsset.is_active.is_(True),
        ).order_by(WebsiteAdsCreativeAsset.updated_at.desc())
    ).all()
    return list(rows)


def _spark_auth_end_at(value: Any) -> datetime | None:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.strptime(text_value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def sync_spark_creative_assets(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    spark_payloads: list[Any],
    complete_snapshot: bool = False,
) -> list[WebsiteAdsCreativeAsset]:
    seen: set[str] = set()
    invalid_rows = 0
    snapshot_shape_valid = True
    now = _utcnow()
    for payload in spark_payloads:
        snapshot_rows, payload_shape_valid = _snapshot_items(payload)
        snapshot_shape_valid = snapshot_shape_valid and payload_shape_valid
        for raw_item in snapshot_rows:
            if not isinstance(raw_item, Mapping):
                invalid_rows += 1
                continue
            item = dict(raw_item)
            item_info = item.get("item_info") if isinstance(item.get("item_info"), dict) else {}
            user_info = item.get("user_info") if isinstance(item.get("user_info"), dict) else {}
            auth_info = item.get("auth_info") if isinstance(item.get("auth_info"), dict) else {}
            video_info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
            item_id = str(item_info.get("item_id") or "").strip()
            if not item_id or str(item_info.get("item_type") or "VIDEO").upper() != "VIDEO":
                invalid_rows += 1
                continue
            if item_id in seen:
                invalid_rows += 1
                continue
            seen.add(item_id)
            row = db.scalar(
                select(WebsiteAdsCreativeAsset).where(
                    WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
                    WebsiteAdsCreativeAsset.auth_id == int(auth_id),
                    WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
                    WebsiteAdsCreativeAsset.video_id == item_id,
                )
            ) or WebsiteAdsCreativeAsset(
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                advertiser_id=str(advertiser_id),
                video_id=item_id,
                title=item_id,
            )
            creator_name = str(user_info.get("tiktok_name") or "").strip()
            caption = str(item_info.get("text") or "").strip()
            title = caption or (f"{creator_name} - {item_id}" if creator_name else item_id)
            previous_source_url = str(row.preview_url or "").strip()
            remote_preview_url = str(video_info.get("preview_url") or "").strip() or None
            remote_cover_url = str(video_info.get("poster_url") or "").strip() or None
            row.title = title[:512]
            row.file_name = None
            row.preview_url = remote_preview_url or row.preview_url
            row.cover_url = remote_cover_url or row.cover_url
            if video_info.get("duration") not in (None, ""):
                row.duration_seconds = Decimal(str(video_info["duration"]))
            row.width = int(video_info["width"]) if video_info.get("width") not in (None, "") else row.width
            row.height = int(video_info["height"]) if video_info.get("height") not in (None, "") else row.height
            row.source = "SPARK_AUTHORIZED_POST"
            row.raw_json = merge_tiktok_media_metadata(
                row.raw_json if isinstance(row.raw_json, dict) else {},
                item,
                video_url=remote_preview_url,
                cover_url=remote_cover_url,
            )
            if row.id is not None and resolve_asset_media(row, "video"):
                row.preview_url = public_asset_media_url(row, "video")
            if row.id is not None and resolve_asset_media(row, "cover"):
                row.cover_url = public_asset_media_url(row, "cover")
            authorization_status = str(auth_info.get("ad_auth_status") or "").upper()
            auth_end_raw = str(auth_info.get("auth_end_time") or "").strip()
            auth_end_at = _spark_auth_end_at(auth_end_raw)
            row.is_active = (
                authorization_status == "AUTHORIZED"
                and (not auth_end_raw or (auth_end_at is not None and auth_end_at > now))
            )
            if not previous_source_url and row.preview_url and row.analysis_status == "PARTIAL":
                row.analysis_status = "NOT_ANALYZED"
                row.analysis_version = None
                row.analysis_error = None
            row.last_synced_at = now
            db.add(row)

    db.flush()
    existing = list(
        db.scalars(
            select(WebsiteAdsCreativeAsset).where(
                WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
                WebsiteAdsCreativeAsset.auth_id == int(auth_id),
                WebsiteAdsCreativeAsset.advertiser_id == str(advertiser_id),
                WebsiteAdsCreativeAsset.source == "SPARK_AUTHORIZED_POST",
            )
        ).all()
    )
    deactivate_allowed = bool(
        complete_snapshot and snapshot_shape_valid and invalid_rows == 0
    )
    if complete_snapshot and not deactivate_allowed:
        logger.warning(
            "Website Ads Spark snapshot contains invalid rows; absence reconciliation disabled",
            extra={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
                "advertiser_id": str(advertiser_id),
                "invalid_rows": int(invalid_rows),
                "snapshot_shape_valid": bool(snapshot_shape_valid),
            },
        )
    if deactivate_allowed:
        for row in existing:
            if row.video_id not in seen:
                row.is_active = False
                row.last_synced_at = now
                db.add(row)
    db.commit()
    return [row for row in existing if row.is_active]


def _allocate_budgets(total: Decimal, group_count: int) -> list[Decimal]:
    if group_count < 1:
        raise ValueError("group_count must be positive")
    minimum_total = MIN_ADGROUP_DAILY_BUDGET * group_count
    if total < minimum_total:
        raise ValueError("total budget is below TikTok's ad-group minimum")
    budgets = [MIN_ADGROUP_DAILY_BUDGET for _ in range(group_count)]
    remaining = total - minimum_total
    weights = [Decimal("5")] + [Decimal("3") for _ in range(group_count - 1)]
    weight_total = sum(weights)
    for index in range(group_count - 1):
        addition = (remaining * weights[index] / weight_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        budgets[index] += addition
    budgets[-1] = total - sum(budgets[:-1])
    return budgets


def _creative_slots_for_group_budget(budget: Decimal) -> int:
    if budget >= Decimal("60"):
        return MAX_CREATIVES_PER_GROUP
    if budget >= Decimal("35"):
        return 5
    return MIN_CREATIVES_PER_GROUP


def _fill_creative_slots(
    preferred_ids: list[int],
    fallback_ids: list[int],
    allowed_ids: list[int],
    *,
    target: int,
    excluded_ids: set[int] | None = None,
) -> list[int]:
    allowed = set(allowed_ids)
    excluded = set(excluded_ids or set())
    selected: list[int] = []

    def append_from(values: list[int], *, allow_excluded: bool) -> None:
        for value in values:
            asset_id = int(value)
            if asset_id not in allowed or asset_id in selected:
                continue
            if not allow_excluded and asset_id in excluded:
                continue
            selected.append(asset_id)
            if len(selected) >= target:
                return

    for values in (preferred_ids, fallback_ids, allowed_ids):
        append_from(values, allow_excluded=False)
        if len(selected) >= target:
            return selected
    if excluded:
        for values in (preferred_ids, fallback_ids, allowed_ids):
            append_from(values, allow_excluded=True)
            if len(selected) >= target:
                break
    return selected


def _fallback_groups(
    assets: list[WebsiteAdsCreativeAsset],
    group_count: int,
    *,
    creatives_per_group: int = MIN_CREATIVES_PER_GROUP,
    audience_hypotheses: list[dict[str, Any]] | None = None,
    product_interest_keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    allowed_ids = [int(item.id) for item in assets]
    slot_count = max(1, min(MAX_CREATIVES_PER_GROUP, int(creatives_per_group)))
    core = allowed_ids[:slot_count]
    hypotheses = list(audience_hypotheses or [])
    groups: list[dict[str, Any]] = []
    for index in range(group_count):
        hypothesis = hypotheses[index] if index < len(hypotheses) else {}
        keywords = _clean_list(hypothesis.get("interest_keywords"), limit=5, length=80)
        if not keywords:
            keywords = _audience_keyword_slice(list(product_interest_keywords or []), index)
        groups.append({
            "name": str(hypothesis.get("segment") or f"Target audience {index + 1}")[:160],
            "role": "AUDIENCE_TEST",
            "hypothesis": str(
                hypothesis.get("rationale")
                or "Use the same creator-led creative set to measure one distinct product-relevant audience."
            )[:500],
            "audience_segment": str(hypothesis.get("segment") or f"Target audience {index + 1}")[:160],
            "interest_keywords": keywords,
            "gender": _normalize_gender(hypothesis.get("gender")),
            "age_groups": _normalize_age_groups(hypothesis.get("age_groups")),
            "creative_asset_ids": core,
        })
    return groups


def _audience_keyword_slice(keywords: list[str], audience_index: int) -> list[str]:
    cleaned = _clean_list(keywords, limit=10, length=80)
    if not cleaned:
        return []
    width = 2 if len(cleaned) >= 4 else 1
    start = (max(0, audience_index) * width) % len(cleaned)
    return [cleaned[(start + offset) % len(cleaned)] for offset in range(width)]


def _verified_interest_fallback_keywords(
    product: WebsiteAdsLandingPage,
    analysis: Mapping[str, Any],
) -> list[str]:
    searchable_text = " ".join((
        str(product.title or ""),
        str(product.content_name or ""),
        str(product.content_category or ""),
        json.dumps(dict(analysis or {}), ensure_ascii=False, default=str),
    )).casefold()
    health_markers = (
        "sleep", "wellness", "health", "gummy", "supplement", "vitamin",
        "magnesium", "theanine", "gaba", "relax", "fitness",
    )
    beauty_markers = (
        "beauty", "skin", "body", "balm", "massage", "personal care",
        "cosmetic", "moistur", "pain relief",
    )
    candidates: list[str] = []
    if any(marker in searchable_text for marker in health_markers):
        candidates.extend(HEALTH_INTEREST_FALLBACK_KEYWORDS)
    if any(marker in searchable_text for marker in beauty_markers):
        candidates.extend(BEAUTY_INTEREST_FALLBACK_KEYWORDS)
    if not candidates:
        candidates.extend((
            "wellness", "healthy lifestyle", "online shopping", "fitness", "beauty",
        ))
    candidates.extend(HEALTH_INTEREST_FALLBACK_KEYWORDS)
    candidates.extend(BEAUTY_INTEREST_FALLBACK_KEYWORDS)
    return list(dict.fromkeys(candidates))


def _flatten_interest_ids(payload: dict[str, Any], limit: int = 8) -> list[str]:
    found: list[str] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return found
    for category_data in data.values():
        search_result = category_data.get("search_result") if isinstance(category_data, dict) else None
        if not isinstance(search_result, dict):
            continue
        for values in search_result.values():
            if not isinstance(values, list):
                continue
            for item in values:
                value = str(item.get("id") or "") if isinstance(item, dict) else ""
                if value and value not in found:
                    found.append(value)
                if len(found) >= limit:
                    return found
    return found


async def _search_general_interest_ids(
    api: TikTokWebsiteAdsClient,
    advertiser_id: str,
    keywords: list[str],
    *,
    limit: int = 8,
) -> list[str]:
    found = match_general_interest_ids(
        advertiser_id,
        keywords,
        language="en",
        limit=limit,
    )
    for keyword in keywords:
        if len(found) >= limit:
            return found
        try:
            payload = await api.search_targeting(
                advertiser_id,
                "INTEREST_AND_BEHAVIOR",
                [keyword],
                sub_targeting_types=["GENERAL_INTEREST"],
                language="en",
            )
        except Exception as exc:
            logger.warning(
                "Website Ads verified interest lookup failed advertiser_id=%s keyword=%r error=%s",
                advertiser_id,
                keyword,
                exc,
            )
            continue
        try:
            record_targeting_discovery(
                advertiser_id,
                keyword,
                payload,
                language="en",
            )
        except Exception:
            logger.exception(
                "Website Ads targeting discovery persistence failed",
                extra={"advertiser_id": advertiser_id, "keyword": keyword},
            )
        for interest_id in _flatten_interest_ids(payload, limit=limit):
            if interest_id not in found:
                found.append(interest_id)
            if len(found) >= limit:
                return found
    return found


def _identity_context(metadata: dict[str, Any]) -> dict[str, Any]:
    pixel = select_website_ads_pixel(metadata.get("pixels"))
    pixel_id = str(pixel.get("pixel_id") or pixel.get("pixel_code") or pixel.get("id") or "")
    if not pixel_id:
        raise ValueError("TikTok Pixel metadata is incomplete")
    identity = select_tiktok_video_identity(metadata.get("identities"))
    return {
        "pixel_id": pixel_id,
        **identity,
    }


def _audience_size_summary(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    data = dict(data) if isinstance(data, Mapping) else {}
    count = data.get("user_count") if isinstance(data.get("user_count"), Mapping) else {}
    stage = int(data.get("user_count_stage") or 0)
    lower_end = int(count.get("lower_end") or 0)
    if not stage and lower_end:
        stage = 1 if lower_end < 10_000 else 2
    labels = {1: "TOO_NARROW", 2: "NARROW", 3: "BALANCED", 4: "FAIRLY_BROAD"}
    return {
        "stage": stage or None,
        "label": labels.get(stage, "UNKNOWN"),
        "lower_end": lower_end or None,
        "upper_end": int(count.get("upper_end") or 0) or None,
        "request_id": str((payload or {}).get("request_id") or "") or None,
    }


def _audience_estimate_body(
    *,
    advertiser_id: str,
    pixel_id: str,
    group: Mapping[str, Any],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "advertiser_id": str(advertiser_id),
        "placement_type": WEBSITE_ADS_PLACEMENT_TYPE,
        "placements": list(WEBSITE_ADS_PLACEMENTS),
        "objective_type": "WEB_CONVERSIONS",
        "promotion_type": "WEBSITE",
        "pixel_id": str(pixel_id),
        "location_ids": list(TIKTOK_CONTIGUOUS_US_LOCATION_IDS),
        "auto_targeting_enabled": False,
        "gender": str(group.get("gender") or "GENDER_UNLIMITED"),
        "age_groups": list(group.get("age_groups") or []),
        "interest_category_ids": list(group.get("interest_category_ids") or []),
        **website_ads_optimization_fields(),
    }
    return {key: value for key, value in body.items() if value not in (None, [], "")}


def resolve_website_ads_advertiser_id(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    provider: str,
) -> str:
    binding = resolve_account_binding(
        db,
        int(workspace_id),
        provider,
        int(auth_id),
    )
    advertiser_id = str(binding.advertiser_id or "").strip()
    if not advertiser_id:
        raise ValueError("Advertiser is not configured")
    return advertiser_id


async def generate_media_plan(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    provider: str,
    landing_page_id: int,
    creative_asset_ids: list[int] | None,
    daily_budget: float,
    activate_after_create: bool,
    request_notes: str | None,
    pending_plan_id: int | None = None,
) -> WebsiteAdsMediaPlan:
    total_budget = Decimal(str(daily_budget)).quantize(Decimal("0.01"))
    minimum_plan_budget = MIN_ADGROUP_DAILY_BUDGET * MIN_TARGETED_ADGROUPS_PER_PLAN
    if total_budget < minimum_plan_budget:
        raise ValueError(
            f"Daily budget must be at least USD {minimum_plan_budget:.2f} for three precise audience groups"
        )
    product = db.get(WebsiteAdsLandingPage, int(landing_page_id))
    if not product or product.workspace_id != int(workspace_id) or not product.is_active:
        raise ValueError("Product is unavailable")
    if product.analysis_status != "READY" or not product.hermes_analysis_json:
        product = await analyze_product_profile(db, product)

    advertiser_id = resolve_website_ads_advertiser_id(
        db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        provider=provider,
    )

    ranked_pool: list[dict[str, Any]] = []
    explicit_ids = list(dict.fromkeys(int(value) for value in (creative_asset_ids or [])))
    if explicit_ids:
        assets = list(
            db.scalars(
                select(WebsiteAdsCreativeAsset).where(
                    WebsiteAdsCreativeAsset.workspace_id == int(workspace_id),
                    WebsiteAdsCreativeAsset.auth_id == int(auth_id),
                    WebsiteAdsCreativeAsset.advertiser_id == advertiser_id,
                    WebsiteAdsCreativeAsset.id.in_(explicit_ids),
                    WebsiteAdsCreativeAsset.is_active.is_(True),
                )
            ).all()
        )
        if len(assets) != len(explicit_ids):
            raise ValueError("One or more requested creative assets are unavailable")
        ineligible = []
        for asset in assets:
            policy = assess_website_ads_creative_policy(asset.hermes_analysis_json)
            if str(asset.analysis_status or "").upper() != "READY" or not policy["eligible_for_automatic_launch"]:
                ineligible.append(f"{asset.id}:{policy['readiness']}")
        if ineligible:
            raise ValueError(
                "Creative assets are not approved for launch: " + ", ".join(ineligible)
            )
        ranked_pool = [
            {
                "asset": asset,
                "score": 100.0,
                "product_confidence": _asset_product_confidence(asset, int(product.id)),
                "performance": _asset_performance(db, asset),
                "creative_type": str(dict(asset.hermes_analysis_json or {}).get("creative_type") or "unclassified"),
                "talent_type": str(dict(asset.hermes_analysis_json or {}).get("talent_type") or "unknown"),
                "production_origin": _production_origin(asset),
                "policy_readiness": assess_website_ads_creative_policy(asset.hermes_analysis_json)["readiness"],
            }
            for asset in assets
        ]
    else:
        assets, ranked_pool = _auto_select_assets(
            db,
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=advertiser_id,
            product=product,
        )
    if len(assets) < MIN_CREATIVES_PER_GROUP:
        raise ValueError(
            f"At least {MIN_CREATIVES_PER_GROUP} eligible creative assets are required for a production media plan"
        )
    ranked_by_asset_id = {int(item["asset"].id): item for item in ranked_pool}

    api = TikTokWebsiteAdsClient(build_ttb_client(db, int(auth_id)))
    try:
        pixels = await api.list_all_pixels(advertiser_id)
        identities = await api.list_all_identities(advertiser_id)
        execution_context = _identity_context({"pixels": pixels.get("data", {}), "identities": identities.get("data", {})})
        analysis = dict(product.hermes_analysis_json or {})
        product_interest_keywords = _clean_list(analysis.get("interest_keywords"), limit=10, length=80)
        audience_hypotheses = _normalize_audience_hypotheses(analysis.get("audience_hypotheses"))
        for hypothesis in audience_hypotheses:
            for keyword in hypothesis["interest_keywords"]:
                if keyword not in product_interest_keywords:
                    product_interest_keywords.append(keyword)
    finally:
        await api.aclose()

    group_count = min(
        MAX_ADGROUPS_PER_PLAN,
        max(MIN_TARGETED_ADGROUPS_PER_PLAN, int(total_budget // MIN_ADGROUP_DAILY_BUDGET)),
    )
    budgets = _allocate_budgets(total_budget, group_count)
    creative_slot_targets = [_creative_slots_for_group_budget(value) for value in budgets]
    fallback_groups = _fallback_groups(
        assets,
        group_count,
        creatives_per_group=max(creative_slot_targets),
        audience_hypotheses=audience_hypotheses,
        product_interest_keywords=product_interest_keywords,
    )
    required_roles = [group["role"] for group in fallback_groups]
    minimum_unique_assets = min(len(assets), max(creative_slot_targets))
    raw_plan: dict[str, Any] = {}
    strategy_source = "HERMES"
    try:
        client = HermesAdsReviewClient()
        response, _ = await client.create_response(
            input_text=json.dumps(
                {
                    "product": _product_snapshot(db, product),
                    "product_analysis": product.hermes_analysis_json,
                    "product_historical_performance": _product_performance(db, product),
                    "audience_historical_performance": _targeting_performance_history(db, product),
                    "daily_audience_learning": _daily_audience_learning(db, product),
                    "candidate_creatives": [
                        {
                            **_asset_snapshot(asset),
                            "historical_performance": ranked_by_asset_id[int(asset.id)]["performance"],
                            "selection_score": ranked_by_asset_id[int(asset.id)]["score"],
                            "product_match_confidence": ranked_by_asset_id[int(asset.id)]["product_confidence"],
                            "creative_type": ranked_by_asset_id[int(asset.id)]["creative_type"],
                            "talent_type": ranked_by_asset_id[int(asset.id)]["talent_type"],
                            "production_origin": ranked_by_asset_id[int(asset.id)]["production_origin"],
                        }
                        for asset in assets
                    ],
                    "constraints": {
                        "total_daily_budget": float(total_budget),
                        "adgroup_count": group_count,
                        "minimum_adgroup_daily_budget": float(MIN_ADGROUP_DAILY_BUDGET),
                        "maximum_creatives_per_adgroup": MAX_CREATIVES_PER_GROUP,
                        "minimum_creatives_per_adgroup": creative_slot_targets,
                        "minimum_unique_assets_across_plan": minimum_unique_assets,
                        "required_group_roles_in_order": required_roles,
                        "optimization_event": WEBSITE_ADS_OPTIMIZATION_EVENT,
                        "optimization_event_label": WEBSITE_ADS_OPTIMIZATION_EVENT_LABEL,
                        "country": "United States",
                        "candidate_asset_ids_only": True,
                        "asset_selection_mode": "operator_override" if explicit_ids else "hermes_automatic",
                        "precise_targeting_required": True,
                        "minimum_verified_interest_ids_per_group": int(
                            settings.WEBSITE_ADS_AUDIENCE_MIN_INTEREST_IDS
                        ),
                        "maximum_verified_interest_ids_per_group": int(
                            settings.WEBSITE_ADS_AUDIENCE_MAX_INTEREST_IDS
                        ),
                        "audience_estimate_target_grade": int(
                            settings.WEBSITE_ADS_AUDIENCE_ESTIMATE_TARGET_GRADE
                        ),
                        "minimum_ctr": 0.04,
                        "maximum_cpc": 0.30,
                        "primary_business_kpi": "QUALIFIED_CLICKS",
                        "prefer_real_creator_assets_over_aigc": True,
                        "reserve_exploration_slots_for_low_sample_assets": True,
                    },
                    "operator_notes": request_notes,
                },
                ensure_ascii=False,
                default=str,
            ),
            instructions=(
                "You are the Hermes media planning officer for TikTok web-link promotion whose business KPI is qualified clicks. Build a controlled, precisely "
                "targeted experiment. Return strict JSON with strategy_summary, confidence, campaign_name, groups. groups must contain "
                "exactly the requested adgroup_count. Every group must use role AUDIENCE_TEST and needs name, audience_segment, hypothesis, "
                "interest_keywords, gender, age_groups, creative_asset_ids, and ads. Each ad needs creative_asset_id, ad_text under 100 "
                "characters, call_to_action, rationale. Use SHOP_NOW by default for this direct-response sales destination. "
                "ORDER_NOW and PREORDER_NOW are allowed only when the verified offer supports them. LEARN_MORE is allowed only for "
                "a genuinely educational pre-sell landing page, and its rationale must begin exactly with [EDUCATIONAL_FUNNEL]. "
                "Optimize for CTR at or above 4%, CPC at or below USD 0.30, then View Content quality; do not judge this plan by purchases or ROAS. "
                "Every group must provide a distinct product-relevant set of TikTok-searchable interest_keywords and a defined demographic hypothesis. "
                "Broad targeting is forbidden. Use historical audience performance when available. Select only from supplied candidate asset IDs. Prioritize "
                "REAL_CREATOR and REAL_CUSTOMER assets over AIGC, then use product match, click performance, sample reliability, and format diversity. "
                "Use 4 to 6 videos per group and reuse the same strongest creator-led set across audience groups so targeting is the measured variable. "
                "Reserve at most one exploration slot per group for a strong low-sample non-AIGC candidate. Never invent promotion terms or medical claims."
            ),
            metadata={"source": "website_ads_media_plan", "prompt_version": PROMPT_VERSION, "product_id": str(product.id)},
        )
        raw_plan = _extract_json_object(extract_output_text(response))
        if not raw_plan:
            raise ValueError("Hermes returned an invalid media plan")
    except Exception:
        strategy_source = "BOUNDED_FALLBACK"
        raw_plan = {"groups": fallback_groups, "strategy_summary": "Bounded control plan generated from verified product and asset data."}

    allowed_asset_ids = {int(asset.id) for asset in assets}
    allowed_asset_order = [int(asset.id) for asset in assets]
    model_groups = raw_plan.get("groups") if isinstance(raw_plan.get("groups"), list) else []
    resolved_interest_keywords: dict[int, list[str]] = {}
    resolved_interest_ids: dict[int, list[str]] = {}
    audience_requests: list[tuple[int, list[str]]] = []
    used_audience_keywords: set[str] = set()
    audience_index = 0
    for index in range(group_count):
        fallback = fallback_groups[index]
        candidate = model_groups[index] if index < len(model_groups) and isinstance(model_groups[index], dict) else {}
        candidate_role = str(candidate.get("role") or "").upper()
        role = fallback["role"]
        keywords = _clean_list(candidate.get("interest_keywords"), limit=5, length=80) if candidate_role == role else []
        if role == "AUDIENCE_TEST":
            distinct_keywords = [value for value in keywords if value.casefold() not in used_audience_keywords]
            if not distinct_keywords:
                distinct_keywords = [
                    value
                    for value in (
                        _clean_list(fallback.get("interest_keywords"), limit=5, length=80)
                        or _audience_keyword_slice(product_interest_keywords, audience_index)
                    )
                    if value.casefold() not in used_audience_keywords
                ]
            keywords = distinct_keywords
            used_audience_keywords.update(value.casefold() for value in keywords)
            audience_index += 1
        else:
            keywords = []
        resolved_interest_keywords[index] = keywords
        if keywords:
            audience_requests.append((index, keywords))
    if audience_requests:
        targeting_api = TikTokWebsiteAdsClient(build_ttb_client(db, int(auth_id)))
        try:
            used_interest_ids: set[str] = set()
            search_cache: dict[tuple[str, ...], list[str]] = {}
            fallback_keywords = _verified_interest_fallback_keywords(product, analysis)

            async def verified_ids(keywords: list[str]) -> list[str]:
                cache_key = tuple(value.casefold() for value in keywords)
                if cache_key not in search_cache:
                    search_cache[cache_key] = await _search_general_interest_ids(
                        targeting_api,
                        advertiser_id,
                        keywords,
                    )
                return search_cache[cache_key]

            for index, keywords in audience_requests:
                found_ids = await verified_ids(keywords[:3])
                unique_ids = [value for value in found_ids if value not in used_interest_ids]
                if not unique_ids:
                    for fallback_keyword in fallback_keywords:
                        fallback_ids = await verified_ids([fallback_keyword])
                        unique_ids = [value for value in fallback_ids if value not in used_interest_ids]
                        if unique_ids:
                            resolved_interest_keywords[index] = [fallback_keyword]
                            found_ids = fallback_ids
                            break
                minimum_ids = max(1, int(settings.WEBSITE_ADS_AUDIENCE_MIN_INTEREST_IDS))
                maximum_ids = max(minimum_ids, int(settings.WEBSITE_ADS_AUDIENCE_MAX_INTEREST_IDS))
                selected_ids = list(unique_ids)
                for value in found_ids:
                    if value not in selected_ids:
                        selected_ids.append(value)
                    if len(selected_ids) >= minimum_ids:
                        break
                resolved_interest_ids[index] = selected_ids[:maximum_ids]
                if resolved_interest_ids[index]:
                    used_interest_ids.add(resolved_interest_ids[index][0])
        finally:
            await targeting_api.aclose()

    unresolved_groups = [
        index + 1
        for index in range(group_count)
        if not resolved_interest_keywords.get(index) or not resolved_interest_ids.get(index)
    ]
    if unresolved_groups:
        raise ValueError(
            "Precise targeting could not be verified for audience groups "
            + ", ".join(str(value) for value in unresolved_groups)
            + "; the media plan was not created because broad targeting is disabled"
        )

    sanitized_groups: list[dict[str, Any]] = []
    control_asset_ids: list[int] = []
    for index in range(group_count):
        fallback = fallback_groups[index]
        candidate = model_groups[index] if index < len(model_groups) and isinstance(model_groups[index], dict) else {}
        candidate_role = str(candidate.get("role") or "").upper()
        requested_role = fallback["role"]
        resolved_role = requested_role
        use_candidate = candidate_role == requested_role
        preferred_ids = [
            int(value)
            for value in (candidate.get("creative_asset_ids") or [])
            if str(value).isdigit() and int(value) in allowed_asset_ids
        ] if use_candidate else []
        if control_asset_ids:
            preferred_ids = list(control_asset_ids)
        excluded_ids: set[int] = set()
        selected_ids = _fill_creative_slots(
            preferred_ids,
            list(fallback["creative_asset_ids"]),
            allowed_asset_order,
            target=creative_slot_targets[index],
            excluded_ids=excluded_ids,
        )
        if not control_asset_ids:
            control_asset_ids = list(selected_ids)
        ads_by_asset = {
            int(item.get("creative_asset_id")): item
            for item in candidate.get("ads") or []
            if isinstance(item, dict) and str(item.get("creative_asset_id") or "").isdigit()
        }
        sanitized_groups.append({
            "name": str(
                candidate.get("name") if use_candidate and candidate.get("name") else fallback["name"]
            )[:512],
            "role": resolved_role,
            "hypothesis": str(
                candidate.get("hypothesis") if use_candidate and candidate.get("hypothesis") else fallback["hypothesis"]
            )[:2000],
            "audience_segment": str(
                candidate.get("audience_segment") if use_candidate and candidate.get("audience_segment") else fallback["audience_segment"]
            )[:160],
            "gender": _normalize_gender(
                candidate.get("gender") if use_candidate else fallback.get("gender"),
                default=_normalize_gender(fallback.get("gender")),
            ),
            "age_groups": _normalize_age_groups(
                candidate.get("age_groups") if use_candidate else fallback.get("age_groups"),
                default=_normalize_age_groups(fallback.get("age_groups")),
            ),
            "daily_budget": budgets[index],
            "interest_keywords": resolved_interest_keywords.get(index, []),
            "interest_category_ids": resolved_interest_ids.get(index, []),
            "creative_asset_ids": selected_ids,
            "ads": ads_by_asset,
        })

    audience_api = TikTokWebsiteAdsClient(build_ttb_client(db, int(auth_id)))
    try:
        minimum_ids = max(1, int(settings.WEBSITE_ADS_AUDIENCE_MIN_INTEREST_IDS))
        maximum_ids = max(minimum_ids, int(settings.WEBSITE_ADS_AUDIENCE_MAX_INTEREST_IDS))
        minimum_grade = max(1, int(settings.WEBSITE_ADS_AUDIENCE_ESTIMATE_MIN_GRADE))
        target_grade = max(minimum_grade, int(settings.WEBSITE_ADS_AUDIENCE_ESTIMATE_TARGET_GRADE))
        fallback_keywords = _verified_interest_fallback_keywords(product, analysis)
        for group in sanitized_groups:
            adjustments: list[str] = []
            ids = list(group["interest_category_ids"])
            local_matches = match_general_interest_ids(
                advertiser_id,
                list(group["interest_keywords"]) + product_interest_keywords + fallback_keywords,
                limit=maximum_ids,
            )
            for value in local_matches:
                if value not in ids:
                    ids.append(value)
                if len(ids) >= minimum_ids:
                    break
            group["interest_category_ids"] = ids[:maximum_ids]
            if len(group["interest_category_ids"]) < minimum_ids:
                adjustments.append("INTEREST_POOL_BELOW_TARGET")

            try:
                payload = await audience_api.estimate_audience_size(
                    _audience_estimate_body(
                        advertiser_id=advertiser_id,
                        pixel_id=str(execution_context["pixel_id"]),
                        group=group,
                    )
                )
                estimate = _audience_size_summary(payload)
                if int(estimate.get("stage") or 0) < target_grade:
                    group["gender"] = "GENDER_UNLIMITED"
                    group["age_groups"] = sorted(ALLOWED_AGE_GROUPS)
                    for value in local_matches:
                        if value not in group["interest_category_ids"]:
                            group["interest_category_ids"].append(value)
                        if len(group["interest_category_ids"]) >= maximum_ids:
                            break
                    adjustments.append("BROADENED_TO_REACH_TARGET_AUDIENCE_GRADE")
                    payload = await audience_api.estimate_audience_size(
                        _audience_estimate_body(
                            advertiser_id=advertiser_id,
                            pixel_id=str(execution_context["pixel_id"]),
                            group=group,
                        )
                    )
                    estimate = _audience_size_summary(payload)
                estimate["target_grade"] = target_grade
                estimate["minimum_grade"] = minimum_grade
                estimate["meets_minimum"] = int(estimate.get("stage") or 0) >= minimum_grade
                estimate["meets_target"] = int(estimate.get("stage") or 0) >= target_grade
                group["audience_size_estimate"] = estimate
            except Exception as exc:
                group["audience_size_estimate"] = {
                    "label": "UNAVAILABLE",
                    "meets_minimum": None,
                    "meets_target": None,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
                adjustments.append("AUDIENCE_ESTIMATE_UNAVAILABLE")
            group["audience_width_adjustments"] = adjustments
    finally:
        await audience_api.aclose()

    planned_asset_ids = sorted({
        asset_id
        for group in sanitized_groups
        for asset_id in group["creative_asset_ids"]
    })
    planned_ad_count = sum(len(group["creative_asset_ids"]) for group in sanitized_groups)
    effective_roles = [group["role"] for group in sanitized_groups]

    suffix = datetime.now().strftime("%Y%m%d_%H%M")
    plan = db.get(WebsiteAdsMediaPlan, int(pending_plan_id)) if pending_plan_id is not None else None
    if pending_plan_id is not None and plan is None:
        raise ValueError("Pending media plan no longer exists")
    if plan is not None and (
        int(plan.workspace_id) != int(workspace_id)
        or int(plan.auth_id) != int(auth_id)
        or int(plan.landing_page_id) != int(product.id)
    ):
        raise ValueError("Pending media plan scope does not match the generation request")
    if plan is None:
        plan = WebsiteAdsMediaPlan(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=advertiser_id,
            landing_page_id=product.id,
            name=f"{product.title} Hermes Website Sales {suffix}"[:512],
            status="GENERATING",
            daily_budget=total_budget,
            activate_after_create=bool(activate_after_create),
            strategy_source="PENDING",
        )
    elif str(plan.status or "").upper() in {"READY", "EXECUTING", "EXECUTED"}:
        return plan

    existing_group_ids = list(db.scalars(
        select(WebsiteAdsMediaPlanGroup.id).where(WebsiteAdsMediaPlanGroup.media_plan_id == plan.id)
    ).all())
    if existing_group_ids:
        db.execute(
            delete(WebsiteAdsMediaPlanCreative).where(
                WebsiteAdsMediaPlanCreative.media_plan_group_id.in_(existing_group_ids)
            )
        )
        db.execute(
            delete(WebsiteAdsMediaPlanGroup).where(WebsiteAdsMediaPlanGroup.id.in_(existing_group_ids))
        )
    plan.advertiser_id = advertiser_id
    plan.name = str(raw_plan.get("campaign_name") or f"{product.title} Hermes Website Sales {suffix}")[:512]
    plan.status = "READY"
    plan.daily_budget = total_budget
    plan.activate_after_create = bool(activate_after_create)
    plan.strategy_source = strategy_source
    plan.confidence = str(raw_plan.get("confidence") or ("medium" if strategy_source == "HERMES" else "bounded"))[:16]
    plan.strategy_summary = str(raw_plan.get("strategy_summary") or "Hermes controlled media plan")[:8000]
    plan.product_snapshot_json = _product_snapshot(db, product)
    plan.selected_asset_ids_json = planned_asset_ids
    plan.execution_context_json = {
        **execution_context,
        "location_ids": list(TIKTOK_CONTIGUOUS_US_LOCATION_IDS),
        "location_policy": "US_CONTIGUOUS_48_PLUS_DC",
        "candidate_asset_ids": allowed_asset_order,
        "candidate_asset_count": len(allowed_asset_order),
        "planned_unique_asset_count": len(planned_asset_ids),
        "planned_ad_count": planned_ad_count,
        "creative_slot_targets": creative_slot_targets,
        "required_group_roles": effective_roles,
        "optimization_event": WEBSITE_ADS_OPTIMIZATION_EVENT,
        "optimization_event_label": WEBSITE_ADS_OPTIMIZATION_EVENT_LABEL,
        "optimization_strategy": "PRECISE_AUDIENCE_QUALIFIED_CLICKS",
        "creative_quality_thresholds": {
            "min_ctr": 0.04,
            "max_cpc": 0.30,
            "min_video_2s_rate": 0.20,
            "min_video_6s_rate": 0.06,
            "min_video_impressions_before_action": 150,
            "min_video_spend_before_action": 0.75,
            "qualified_click_override_ctr": 0.04,
            "qualified_click_override_cpc": 0.30,
        },
        "broad_targeting_allowed": False,
    }
    plan.hermes_response_json = raw_plan
    plan.error_message = None
    plan.generated_at = _utcnow()
    db.add(plan)
    db.flush()
    assets_by_id = {int(asset.id): asset for asset in assets}
    product_name = product.title[:48]
    for group_index, group_data in enumerate(sanitized_groups, start=1):
        targeting = {
            "location_ids": list(TIKTOK_CONTIGUOUS_US_LOCATION_IDS),
            "gender": group_data["gender"],
            "age_groups": group_data["age_groups"],
            "interest_category_ids": group_data["interest_category_ids"],
            "audience_ids": [],
            "excluded_audience_ids": [],
            "placement_type": "PLACEMENT_TYPE_NORMAL",
            "placements": ["PLACEMENT_TIKTOK"],
            "audience_segment": group_data["audience_segment"],
            "targeting_rationale": group_data["hypothesis"],
            "interest_keywords": group_data["interest_keywords"],
            "audience_size_estimate": group_data.get("audience_size_estimate"),
            "audience_width_adjustments": group_data.get("audience_width_adjustments", []),
        }
        group = WebsiteAdsMediaPlanGroup(
            media_plan_id=plan.id,
            name=f"{product_name} | {group_data['name']}"[:512],
            role=group_data["role"],
            hypothesis=group_data["hypothesis"],
            targeting_json=targeting,
            daily_budget=group_data["daily_budget"],
            bid_strategy="LOWEST_COST",
            sort_order=group_index,
        )
        db.add(group)
        db.flush()
        for creative_index, asset_id in enumerate(group_data["creative_asset_ids"], start=1):
            asset = assets_by_id[asset_id]
            recommendation = group_data["ads"].get(asset_id, {})
            default_text = f"Discover {product.title}. Shop now."
            ad_text = str(recommendation.get("ad_text") or default_text)[:100]
            rationale = str(recommendation.get("rationale") or "Selected for the controlled test")[:2000]
            db.add(WebsiteAdsMediaPlanCreative(
                media_plan_group_id=group.id,
                creative_asset_id=asset.id,
                ad_name=f"{group.name} | {asset.title}"[:512],
                ad_text=ad_text,
                call_to_action=normalize_website_sales_call_to_action(
                    recommendation.get("call_to_action"),
                    rationale=rationale,
                ),
                rationale=rationale,
                sort_order=creative_index,
            ))
    db.commit()
    db.refresh(plan)
    return plan


def media_plan_dict(db: Session, plan: WebsiteAdsMediaPlan) -> dict[str, Any]:
    execution_context = dict(plan.execution_context_json or {})
    groups = db.scalars(
        select(WebsiteAdsMediaPlanGroup).where(WebsiteAdsMediaPlanGroup.media_plan_id == plan.id).order_by(WebsiteAdsMediaPlanGroup.sort_order)
    ).all()
    group_items = []
    for group in groups:
        creatives = db.scalars(
            select(WebsiteAdsMediaPlanCreative)
            .where(WebsiteAdsMediaPlanCreative.media_plan_group_id == group.id)
            .order_by(WebsiteAdsMediaPlanCreative.sort_order)
        ).all()
        creative_items = []
        for creative in creatives:
            asset = db.get(WebsiteAdsCreativeAsset, creative.creative_asset_id)
            creative_items.append({
                "id": creative.id,
                "creative_asset_id": creative.creative_asset_id,
                "title": asset.title if asset else "Unavailable asset",
                "video_id": asset.video_id if asset else None,
                "cover_url": asset.cover_url if asset else None,
                "preview_url": asset.preview_url if asset else None,
                "ad_text": creative.ad_text,
                "call_to_action": creative.call_to_action,
                "rationale": creative.rationale,
            })
        group_items.append({
            "id": group.id,
            "name": group.name,
            "role": group.role,
            "hypothesis": group.hypothesis,
            "daily_budget": float(group.daily_budget),
            "bid_strategy": group.bid_strategy,
            "targeting": group.targeting_json,
            "creatives": creative_items,
        })
    return {
        "id": plan.id,
        "name": plan.name,
        "status": plan.status,
        "daily_budget": float(plan.daily_budget),
        "strategy_source": plan.strategy_source,
        "confidence": plan.confidence,
        "strategy_summary": plan.strategy_summary,
        "optimization_event": execution_context.get("optimization_event", WEBSITE_ADS_OPTIMIZATION_EVENT),
        "optimization_event_label": execution_context.get(
            "optimization_event_label", WEBSITE_ADS_OPTIMIZATION_EVENT_LABEL
        ),
        "product": plan.product_snapshot_json,
        "groups": group_items,
        "campaign_local_id": plan.campaign_local_id,
        "activate_after_create": plan.activate_after_create,
        "error_message": plan.error_message,
        "generated_at": plan.generated_at,
        "executed_at": plan.executed_at,
        "created_at": plan.created_at,
    }


def _guard_trigger_samples_satisfied(metrics: Mapping[str, Any]) -> bool:
    reasons = {
        str(reason).upper()
        for reason in metrics.get("trigger_reasons", [])
        if str(reason).strip()
    }
    sample = metrics.get("sample") if isinstance(metrics.get("sample"), Mapping) else {}
    requirements = {
        "LOW_CTR": bool(sample.get("ctr_ready")),
        "HIGH_CPC": bool(sample.get("cpc_ready")),
        "LOW_VIDEO_RETENTION": bool(sample.get("video_ready"))
        and not bool(sample.get("qualified_click_override")),
        "ZERO_CLICK_SPEND_CAP": True,
    }
    return bool(reasons) and all(requirements.get(reason, False) for reason in reasons)


async def review_website_ad_guard_action(
    *,
    ad: WebsiteAdsCreativeAsset | Any,
    metrics: Mapping[str, Any],
    proposed_reason: str,
) -> dict[str, Any]:
    """Ask the isolated realtime Hermes agent to review a bounded ad-level pause."""

    try:
        client = HermesAdsRealtimeClient()
        response, latency_ms = await client.create_response(
            input_text=json.dumps(
                {
                    "ad": {
                        "id": getattr(ad, "id", None),
                        "video_id": getattr(ad, "video_id", None),
                        "guard_config": getattr(ad, "guard_config_json", None),
                    },
                    "metrics": dict(metrics),
                    "proposed_action": {"action": "PAUSE_AD", "reason": proposed_reason},
                },
                ensure_ascii=False,
                default=str,
            ),
            instructions=(
                "You are the realtime Hermes risk reviewer for a single TikTok web-link ad whose primary business KPI is qualified clicks and whose secondary creative-quality signals are 2-second and 6-second video retention. The deterministic guard has "
                "proposed pausing one ad, not the campaign. Return strict JSON with decision (APPROVE or HOLD), confidence, reason, "
                "risk_flags. Enforce the supplied CTR minimum, CPC maximum, and paired 2-second/6-second retention floors after their respective deterministic evidence gates are satisfied. "
                "When LOW_VIDEO_RETENTION is present, APPROVE after video_ready is true unless qualified_click_override is true; strong qualified-click performance takes priority over retention alone. "
                "Treat conversions as secondary View Content context, not purchases, and do not use purchase value or ROAS. HOLD only "
                "when the metrics are stale, contradictory, or the sample gate that corresponds to a trigger reason is not satisfied. "
                "Evaluate each trigger independently: LOW_CTR requires ctr_ready, HIGH_CPC requires cpc_ready, LOW_VIDEO_RETENTION requires video_ready, and ZERO_CLICK_SPEND_CAP is already evidence-complete. "
                "Never require CTR or video readiness for a HIGH_CPC-only action, and never require unrelated sample gates. "
                "Do not propose campaign rebuilds, new targeting, or budget changes in this review."
            ),
            metadata={"source": "website_ads_guard_review", "prompt_version": PROMPT_VERSION, "ad_id": str(getattr(ad, "id", ""))},
        )
        raw = _extract_json_object(extract_output_text(response))
        decision = str(raw.get("decision") or "").upper()
        if decision not in {"APPROVE", "HOLD"}:
            raise ValueError("Hermes returned an invalid guard decision")
        reason = str(raw.get("reason") or proposed_reason)[:1000]
        risk_flags = _clean_list(raw.get("risk_flags"), limit=8, length=120)
        source = "HERMES"
        if decision == "HOLD" and _guard_trigger_samples_satisfied(metrics):
            decision = "APPROVE"
            source = "HERMES_EVIDENCE_VALIDATOR"
            risk_flags.append("invalid_cross_gate_hold_overridden")
            reason = (
                "Hermes HOLD used an unrelated evidence gate; the deterministic sample gate "
                f"for {','.join(str(item) for item in metrics.get('trigger_reasons', []))} is satisfied. "
                f"Proceed with the bounded ad-level pause. Original review: {reason}"
            )[:1000]
        return {
            "decision": decision,
            "confidence": str(raw.get("confidence") or "low")[:16],
            "reason": reason,
            "risk_flags": risk_flags,
            "source": source,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {
            "decision": "APPROVE",
            "confidence": "bounded",
            "reason": proposed_reason,
            "risk_flags": [f"hermes_unavailable:{type(exc).__name__}"],
            "source": "BOUNDED_FALLBACK",
        }


async def review_website_adgroup_race_action(
    *,
    campaign_id: int,
    winner: Mapping[str, Any],
    loser: Mapping[str, Any],
) -> dict[str, Any]:
    """Review one bounded audience-group race without changing total budget."""

    proposed_reason = (
        f"Scale targeting group {winner.get('adgroup_local_id')} and replace "
        f"underperforming group {loser.get('adgroup_local_id')} within the same budget"
    )
    try:
        client = HermesAdsRealtimeClient()
        response, latency_ms = await client.create_response(
            input_text=json.dumps(
                {
                    "campaign_local_id": int(campaign_id),
                    "winner": dict(winner),
                    "loser": dict(loser),
                    "proposed_action": {
                        "action": "REPLACE_TARGETING_GROUP",
                        "budget_change": 0,
                        "reason": proposed_reason,
                    },
                },
                ensure_ascii=False,
                default=str,
            ),
            instructions=(
                "You are the realtime Hermes audience-race reviewer for TikTok web-link advertising. The deterministic layer has "
                "compared audience groups from the same campaign, advertiser day and optimization event after the configured sample "
                "gates. Return strict JSON with decision (APPROVE or HOLD), confidence, reason, risk_flags. APPROVE replacing the "
                "clear loser with a clone of the clear winner when the supplied winner/loser flags and sample_ready values are true. "
                "The loser budget is transferred to the clone, so total active daily budget must not increase. HOLD only when data is "
                "stale, contradictory, from different date windows, below sample gates, or the action would increase total budget. "
                "Do not convert this into broad targeting and do not pause the campaign."
            ),
            metadata={
                "source": "website_ads_adgroup_race_review",
                "prompt_version": PROMPT_VERSION,
                "campaign_id": str(campaign_id),
            },
        )
        raw = _extract_json_object(extract_output_text(response))
        decision = str(raw.get("decision") or "").upper()
        if decision not in {"APPROVE", "HOLD"}:
            raise ValueError("Hermes returned an invalid audience-race decision")
        return {
            "decision": decision,
            "confidence": str(raw.get("confidence") or "low")[:16],
            "reason": str(raw.get("reason") or proposed_reason)[:1000],
            "risk_flags": _clean_list(raw.get("risk_flags"), limit=8, length=120),
            "source": "HERMES",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {
            "decision": "APPROVE",
            "confidence": "bounded",
            "reason": proposed_reason,
            "risk_flags": [f"hermes_unavailable:{type(exc).__name__}"],
            "source": "BOUNDED_FALLBACK",
        }


async def review_website_asset_expansion_action(
    *,
    asset: WebsiteAdsCreativeAsset,
    product: WebsiteAdsLandingPage,
    proposed_action: Mapping[str, Any],
) -> dict[str, Any]:
    """Review one bounded new-creative placement without allowing budget expansion."""

    canonical_product_name = _canonical_product_name(product)
    asset_analysis = dict(asset.hermes_analysis_json or {})
    product_match = (
        dict(asset_analysis.get("product_match") or {})
        if isinstance(asset_analysis.get("product_match"), Mapping)
        else {}
    )
    if str(product_match.get("landing_page_id") or "") == str(product.id):
        product_match["product_name"] = canonical_product_name
        product_match["landing_page_title"] = product.title
        asset_analysis["product_match"] = product_match

    try:
        client = HermesAdsRealtimeClient()
        response, latency_ms = await client.create_response(
            input_text=json.dumps(
                {
                    "asset": {
                        "id": int(asset.id),
                        "video_id": asset.video_id,
                        "source": asset.source,
                        "title": asset.title,
                        "analysis": asset_analysis,
                    },
                    "product": {
                        "id": int(product.id),
                        "product_name": canonical_product_name,
                        "landing_page_title": product.title,
                        "content_name": product.content_name,
                        "tiktok_shop_product_id": product.product_id,
                        "reference_price": float(product.reference_price or 0),
                    },
                    "proposed_action": dict(proposed_action),
                },
                ensure_ascii=False,
                default=str,
            ),
            instructions=(
                "You are the realtime Hermes creative expansion reviewer for TikTok web-link advertising. The deterministic layer "
                "has matched one newly analyzed creative to a verified product and proposed either adding it to existing precise "
                "audience groups or cloning one precise group without increasing the media-plan daily budget. Return strict JSON "
                "with decision (APPROVE or HOLD), confidence, reason, risk_flags. Internal policy findings are advisory risk labels; "
                "TikTok platform review is the authoritative launch gate. APPROVE controlled exploration even when the analysis says "
                "REVIEW or BLOCKED, and preserve those labels for audit learning. Lack of historical delivery data is expected for a "
                "new creative and is not a reason to HOLD. HOLD only for a concrete product mismatch, expired Spark authorization, "
                "duplicate placement, contradictory identity evidence, or an explicit budget-cap violation. Do not alter the "
                "budget, targeting, optimization event, placement, or campaign status. product_name/content_name identify the sold "
                "product; landing_page_title is only an offer or landing-page label and must never be treated as a different product."
            ),
            metadata={
                "source": "website_ads_asset_expansion_review",
                "prompt_version": PROMPT_VERSION,
                "asset_id": str(asset.id),
            },
        )
        raw = _extract_json_object(extract_output_text(response))
        decision = str(raw.get("decision") or "").upper()
        if decision not in {"APPROVE", "HOLD"}:
            raise ValueError("Hermes returned an invalid creative expansion decision")
        return {
            "decision": decision,
            "confidence": str(raw.get("confidence") or "low")[:16],
            "reason": str(raw.get("reason") or "Bounded creative exploration review")[:1000],
            "risk_flags": _clean_list(raw.get("risk_flags"), limit=8, length=120),
            "source": "HERMES",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {
            "decision": "APPROVE",
            "confidence": "bounded",
            "reason": "Deterministic product, policy, capacity, and budget gates passed.",
            "risk_flags": [f"hermes_unavailable:{type(exc).__name__}"],
            "source": "BOUNDED_FALLBACK",
        }


async def review_website_campaign_conversion_guard_action(
    *,
    campaign: WebsiteAdsCampaign,
    product: WebsiteAdsLandingPage,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Review one bounded Website Ads pause after all cross-channel gates pass."""

    proposed_reason = (
        "No new product-level GMV Max order pulse arrived during the completed "
        "Website Ads observation window after minimum click and spend evidence."
    )
    hard_gates = evidence.get("hard_gates") if isinstance(evidence.get("hard_gates"), Mapping) else {}
    evidence_ready = bool(hard_gates) and all(bool(value) for value in hard_gates.values())
    try:
        client = HermesAdsRealtimeClient()
        response, latency_ms = await client.create_response(
            input_text=json.dumps(
                {
                    "campaign": {
                        "local_id": int(campaign.id),
                        "remote_id": campaign.campaign_id,
                        "name": campaign.name,
                    },
                    "product": {
                        "landing_page_id": int(product.id),
                        "tiktok_shop_product_id": product.product_id,
                        "name": product.content_name or product.title,
                        "reference_price": float(product.reference_price or 0),
                    },
                    "evidence": dict(evidence),
                    "proposed_action": {
                        "action": "PAUSE_WEBSITE_CAMPAIGN",
                        "scope": "WEBSITE_ADS_ONLY",
                        "reason": proposed_reason,
                    },
                },
                ensure_ascii=False,
                default=str,
            ),
            instructions=(
                "You are the realtime Hermes cross-channel guard for TikTok Website Ads. The landing page redirects users to "
                "TikTok Shop, so Website Ads cannot directly observe purchases. Product-level GMV Max orders are a directional "
                "demand pulse, not deterministic attribution. Return strict JSON with decision (APPROVE or HOLD), confidence, "
                "reason, risk_flags. APPROVE only the bounded pause of the Website Ads campaign when every supplied hard gate is "
                "true: the GMV source is fresh, the observation window is complete, minimum incremental spend and clicks are met, "
                "and no new product order pulse appeared. Never pause, edit, or rebuild a GMV Max campaign. HOLD only for stale or "
                "contradictory evidence, a recent order pulse, manual operator control, or an incomplete hard gate. Do not require "
                "ROAS or direct purchase attribution because those signals do not exist on this web-link path."
            ),
            metadata={
                "source": "website_ads_cross_channel_guard",
                "prompt_version": "website_ads_cross_channel_guard_v1",
                "campaign_id": str(campaign.id),
                "product_id": str(product.product_id or ""),
            },
        )
        raw = _extract_json_object(extract_output_text(response))
        decision = str(raw.get("decision") or "").upper()
        if decision not in {"APPROVE", "HOLD"}:
            raise ValueError("Hermes returned an invalid cross-channel guard decision")
        return {
            "decision": decision,
            "confidence": str(raw.get("confidence") or "low")[:16],
            "reason": str(raw.get("reason") or proposed_reason)[:1000],
            "risk_flags": _clean_list(raw.get("risk_flags"), limit=8, length=120),
            "source": "HERMES",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        logger.warning(
            "Realtime Hermes unavailable for campaign-wide Website Ads pause; deferring action",
            exc_info=True,
            extra={
                "campaign_local_id": getattr(campaign, "id", None),
                "evidence_ready": evidence_ready,
            },
        )
        return {
            "decision": "HOLD",
            "confidence": "none",
            "reason": (
                "Realtime Hermes review is unavailable; defer this campaign-wide pause and retry on the next evaluation."
            ),
            "risk_flags": [f"hermes_unavailable:{type(exc).__name__}"],
            "source": "HERMES_UNAVAILABLE_FAIL_CLOSED",
        }
