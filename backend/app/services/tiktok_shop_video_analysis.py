from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.gmvmax_creative_metrics import GmvmaxProductCreativeMetricsDaily
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopVideoContentAnalysis,
    TikTokShopVideoDailyMetric,
)
from app.services.gmvmax_creative_media_cache import resolve_creative_media
from app.services.hermes_agent.client import (
    HermesVideoAnalystClient,
    extract_output_text,
    extract_usage,
)


logger = logging.getLogger("gmv.tiktok_shop.video_analysis")
PROMPT_VERSION = "shop-video-analyst-v4-zh-compact"
TRANSCRIPT_PIPELINE_VERSION = "openai-whisper-v1"
# Persist the platform-owned logical role, never a guessed upstream provider.
# The actual provider/model is available in metadata-only AiRouteAttempt rows
# and may change transparently when the gateway fails over.
PROVIDER_MODEL = "gmv-shop-video-analyst-v1"
MAX_PRODUCTS = 20
MAX_IMAGE_BYTES = 2_000_000
FINAL_STATUSES = {"SUCCEEDED", "FAILED", "UNAVAILABLE"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _weighted_rate(rows: Iterable[Any], field: str, weight_field: str) -> float | None:
    numerator = 0.0
    denominator = 0
    fallback: list[float] = []
    for row in rows:
        value = getattr(row, field, None)
        if value is None:
            continue
        rate = _number(value)
        fallback.append(rate)
        weight = _integer(getattr(row, weight_field, 0))
        if weight > 0:
            numerator += rate * weight
            denominator += weight
    if denominator > 0:
        return round(numerator / denominator, 8)
    return round(sum(fallback) / len(fallback), 8) if fallback else None


def _sum_optional(rows: Iterable[Any], field: str) -> int | None:
    values = [getattr(row, field, None) for row in rows]
    present = [value for value in values if value is not None]
    return sum(_integer(value) for value in present) if present else None


def _weighted_provider_percent(
    rows: Iterable[Any],
    field: str,
    weight_field: str,
) -> float | None:
    """Aggregate TikTok percentage strings and normalize them to 0..1.

    GMV Max returns rate values such as ``45.00`` for 45%, not ``0.45``.
    The API does not expose total video-view counts at creative grain, so the
    stored daily rates use the closest official volume available for weighting:
    product impressions (or product clicks for conversion rate). Zero-volume
    rows never dilute an active video's rate.
    """

    weighted_total = 0.0
    weight_total = 0
    observed: list[float] = []
    for row in rows:
        raw = getattr(row, field, None)
        if raw is None:
            continue
        value = max(0.0, min(100.0, _number(raw)))
        observed.append(value)
        weight = max(0, _integer(getattr(row, weight_field, 0)))
        if weight > 0:
            weighted_total += value * weight
            weight_total += weight
    if weight_total > 0:
        return round(weighted_total / weight_total / 100, 8)
    return round(sum(observed) / len(observed) / 100, 8) if observed else None


def _product_snapshots(rows: list[TikTokShopVideoDailyMetric]) -> list[dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: item.report_date, reverse=True):
        for raw in row.products_json or []:
            if not isinstance(raw, dict):
                continue
            product_id = str(raw.get("product_id") or raw.get("id") or "").strip()
            name = str(raw.get("product_name") or raw.get("title") or raw.get("name") or "").strip()
            key = product_id or name
            if not key or key in products:
                continue
            products[key] = {
                "product_id": product_id or None,
                "name": name[:500] or None,
            }
            if len(products) >= MAX_PRODUCTS:
                return list(products.values())
    return list(products.values())


def build_metric_packet(
    db: Session,
    *,
    workspace_id: int,
    shop: OAuthTikTokShopShop,
    video_id: str,
    start_date: date,
    end_date_exclusive: date,
) -> dict[str, Any]:
    shop_rows = list(
        db.scalars(
            select(TikTokShopVideoDailyMetric)
            .where(
                TikTokShopVideoDailyMetric.workspace_id == int(workspace_id),
                TikTokShopVideoDailyMetric.shop_row_id == int(shop.id),
                TikTokShopVideoDailyMetric.video_id == str(video_id),
                TikTokShopVideoDailyMetric.report_date >= start_date,
                TikTokShopVideoDailyMetric.report_date < end_date_exclusive,
            )
            .order_by(TikTokShopVideoDailyMetric.report_date.desc())
        ).all()
    )
    if not shop_rows:
        raise APIError(
            "TIKTOK_SHOP_VIDEO_NOT_FOUND",
            "The video has no official Shop analytics in the selected range.",
            404,
        )

    ad_rows = list(
        db.scalars(
            select(GmvmaxProductCreativeMetricsDaily).where(
                GmvmaxProductCreativeMetricsDaily.workspace_id == int(workspace_id),
                GmvmaxProductCreativeMetricsDaily.store_id == str(shop.shop_id),
                GmvmaxProductCreativeMetricsDaily.creative_id == str(video_id),
                GmvmaxProductCreativeMetricsDaily.stat_time_day >= start_date,
                GmvmaxProductCreativeMetricsDaily.stat_time_day < end_date_exclusive,
            )
        ).all()
    )
    latest = shop_rows[0]
    views = sum(_integer(row.views) for row in shop_rows)
    shop_gmv = sum(_number(row.gmv) for row in shop_rows)
    ad_cost_cents = sum(_integer(row.cost_cents) for row in ad_rows)
    ad_revenue_cents = sum(_integer(row.gross_revenue_cents) for row in ad_rows)
    impressions = _sum_optional(ad_rows, "impressions")
    clicks = _sum_optional(ad_rows, "clicks")
    product_impressions = sum(_integer(row.product_impressions) for row in ad_rows)
    product_clicks = sum(_integer(row.product_clicks) for row in ad_rows)
    paid_rates = {
        "ad_click_rate": _weighted_provider_percent(ad_rows, "ad_click_rate", "product_impressions"),
        "product_click_rate": _weighted_provider_percent(ad_rows, "product_click_rate", "product_impressions"),
        "conversion_rate": _weighted_provider_percent(ad_rows, "ad_conversion_rate", "product_clicks"),
        "view_rate_2s": _weighted_provider_percent(ad_rows, "ad_video_view_rate_2s", "product_impressions"),
        "view_rate_6s": _weighted_provider_percent(ad_rows, "ad_video_view_rate_6s", "product_impressions"),
        "view_rate_25": _weighted_provider_percent(ad_rows, "ad_video_view_rate_p25", "product_impressions"),
        "view_rate_50": _weighted_provider_percent(ad_rows, "ad_video_view_rate_p50", "product_impressions"),
        "view_rate_75": _weighted_provider_percent(ad_rows, "ad_video_view_rate_p75", "product_impressions"),
        "view_rate_100": _weighted_provider_percent(ad_rows, "ad_video_view_rate_p100", "product_impressions"),
    }
    completion_funnel = [
        paid_rates["view_rate_25"],
        paid_rates["view_rate_50"],
        paid_rates["view_rate_75"],
        paid_rates["view_rate_100"],
    ]
    rate_quality_flags: list[str] = []
    comparable = [value for value in completion_funnel if value is not None]
    if len(comparable) == 4 and any(
        float(completion_funnel[index + 1]) > float(completion_funnel[index])
        for index in range(3)
    ):
        rate_quality_flags.append("OFFICIAL_COMPLETION_FUNNEL_NON_MONOTONIC")
    packet = {
        "scope": {
            "workspace_id": int(workspace_id),
            "shop_row_id": int(shop.id),
            "provider_shop_id": str(shop.shop_id),
            "video_id": str(video_id),
            "start_date": start_date.isoformat(),
            "end_date_exclusive": end_date_exclusive.isoformat(),
            "timezone": str(shop.timezone_name),
        },
        "video": {
            "title": str(latest.title or "")[:1000],
            "creator_username": str(latest.creator_username or "")[:255] or None,
            "author_type": latest.author_type,
            "duration_seconds": latest.duration_seconds,
            "posted_at": latest.video_post_time.isoformat() if latest.video_post_time else None,
            "products": _product_snapshots(shop_rows),
        },
        "shop_official_metrics": {
            "source": "TikTok Shop Analytics API",
            "grain": "video_day",
            "days_present": len({row.report_date for row in shop_rows}),
            "currency": latest.currency,
            "gmv": round(shop_gmv, 6),
            "views": views,
            "sku_orders": sum(_integer(row.sku_orders) for row in shop_rows),
            "items_sold": sum(_integer(row.items_sold) for row in shop_rows),
            "avg_customers": sum(_integer(row.avg_customers) for row in shop_rows),
            "gpm": round(shop_gmv * 1000 / views, 6) if views > 0 else None,
            "click_through_rate": _weighted_rate(shop_rows, "click_through_rate", "views"),
            "latest_report_date": max(row.report_date for row in shop_rows).isoformat(),
            "latest_synced_at": max(row.synced_at for row in shop_rows).isoformat(),
        },
        "gmv_max_paid_metrics": {
            "source": "TikTok Business GMV Max creative report API",
            "grain": "campaign_item_group_creative_day",
            "available": bool(ad_rows),
            "rows": len(ad_rows),
            "impressions": impressions,
            "clicks": clicks,
            "product_impressions": product_impressions,
            "product_clicks": product_clicks,
            "cost": round(ad_cost_cents / 100, 2),
            "gross_revenue": round(ad_revenue_cents / 100, 2),
            "orders": sum(_integer(row.orders) for row in ad_rows),
            "roi": round(ad_revenue_cents / ad_cost_cents, 4) if ad_cost_cents > 0 else None,
            **paid_rates,
            "rate_aggregation": "PRODUCT_IMPRESSION_WEIGHTED_DAILY",
            "rate_quality_flags": rate_quality_flags,
            "latest_report_date": max((row.stat_time_day for row in ad_rows), default=None).isoformat() if ad_rows else None,
        },
        "data_contract": {
            "shop_and_paid_metrics_are_separate_sources": True,
            "no_cross_source_attribution_claim": True,
            "rates_are_impression_or_event_weighted": True,
            "provider_percentages_are_normalized_to_ratios": True,
            "creative_video_rates_use_product_impressions_as_the_available_volume_weight": True,
            "missing_paid_metrics_mean_not_observed_not_zero": True,
        },
    }
    return packet


def _media_row(
    db: Session,
    *,
    workspace_id: int,
    provider_shop_id: str,
    video_id: str,
) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            select id, workspace_id, auth_id, advertiser_id, store_id, item_id,
                   local_preview_path, local_cover_path,
                   preview_content_type, cover_content_type,
                   media_cache_status, media_cached_at, updated_at
            from gmvmax_creative_asset_cache
            where workspace_id=:workspace_id
              and store_id=:store_id
              and item_id=:video_id
            order by case when media_cache_status='READY' then 0 else 1 end,
                     updated_at desc, id desc
            limit 1
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "store_id": str(provider_shop_id),
            "video_id": str(video_id),
        },
    ).mappings().first()
    return dict(row) if row else None


def media_identity(row: dict[str, Any] | None) -> tuple[int | None, str | None]:
    if not row:
        return None, None
    material: list[str] = [str(row.get("id") or "")]
    for key in ("local_preview_path", "local_cover_path"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        try:
            stat = path.stat()
            material.extend((key, str(stat.st_size), str(stat.st_mtime_ns)))
        except OSError:
            material.extend((key, "missing"))
    return int(row["id"]), hashlib.sha256("|".join(material).encode()).hexdigest()


def analysis_cache_key(
    *,
    packet: dict[str, Any],
    media_fingerprint: str | None,
) -> str:
    material = {
        "packet": packet,
        "media_fingerprint": media_fingerprint,
        "model": PROVIDER_MODEL,
        "prompt_version": PROMPT_VERSION,
        "transcript_pipeline_version": TRANSCRIPT_PIPELINE_VERSION,
        "max_frames": max(1, min(8, int(settings.HERMES_VIDEO_ANALYSIS_MAX_FRAMES))),
        "image_detail": "low",
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def find_media_identity(
    db: Session,
    *,
    workspace_id: int,
    shop: OAuthTikTokShopShop,
    video_id: str,
) -> tuple[dict[str, Any] | None, int | None, str | None]:
    row = _media_row(
        db,
        workspace_id=workspace_id,
        provider_shop_id=str(shop.shop_id),
        video_id=video_id,
    )
    asset_id, fingerprint = media_identity(row)
    return row, asset_id, fingerprint


def serialize_analysis(row: TikTokShopVideoContentAnalysis) -> dict[str, Any]:
    input_summary = dict(row.input_summary_json or {})
    return {
        "id": int(row.id),
        "video_id": row.video_id,
        "status": row.status,
        "model": row.provider_model,
        "model_alias": row.model_alias,
        "prompt_version": row.prompt_version,
        "metric_start_date": row.metric_start_date.isoformat(),
        "metric_end_date_exclusive": row.metric_end_date_exclusive.isoformat(),
        "source_asset_id": int(row.source_asset_id) if row.source_asset_id else None,
        "video": input_summary.get("video"),
        "analysis": row.analysis_json,
        "metrics": {
            "shop": input_summary.get("shop_official_metrics"),
            "paid": input_summary.get("gmv_max_paid_metrics"),
        },
        "evidence": row.evidence_json,
        "transcript": {
            "status": row.transcript_status,
            "source": row.transcript_source,
            "language": row.transcript_language,
            "text": row.transcript_text,
            "segments": row.transcript_segments_json or [],
            "reason": row.transcript_reason,
            "error_message": row.transcript_error_message,
            "attempts": int(row.transcript_attempts or 0),
            "started_at": row.transcript_started_at.isoformat() if row.transcript_started_at else None,
            "completed_at": row.transcript_completed_at.isoformat() if row.transcript_completed_at else None,
        },
        "usage": row.usage_json,
        "error": {"code": row.error_code, "message": row.error_message} if row.error_code else None,
        "attempts": int(row.attempts or 0),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "/opt/apps/bin/ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=min(30, int(settings.HERMES_VIDEO_ANALYSIS_FFMPEG_TIMEOUT_SECONDS)),
    )
    duration = float(result.stdout.strip())
    if duration <= 0 or duration > 60 * 60:
        raise ValueError("video duration is outside the supported range")
    return duration


def _render_contact_sheet(video_path: Path, output_path: Path) -> tuple[list[float], int]:
    frame_count = max(1, min(8, int(settings.HERMES_VIDEO_ANALYSIS_MAX_FRAMES)))
    duration = _probe_duration(video_path)
    interval = max(0.25, duration / frame_count)
    columns = 4 if frame_count > 4 else frame_count
    rows = 2 if frame_count > 4 else 1
    vf = (
        f"fps=1/{interval:.6f},"
        "scale=320:320:force_original_aspect_ratio=decrease,"
        "pad=320:320:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"tile={columns}x{rows}:padding=4:margin=4"
    )
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-threads", "1", "-i", str(video_path), "-vf", vf,
            "-frames:v", "1", "-q:v", "5", "-y", str(output_path),
        ],
        check=True,
        capture_output=True,
        timeout=int(settings.HERMES_VIDEO_ANALYSIS_FFMPEG_TIMEOUT_SECONDS),
    )
    timestamps = [round(min(duration, interval * index), 2) for index in range(frame_count)]
    return timestamps, output_path.stat().st_size


def _normalize_cover(cover_path: Path, output_path: Path) -> int:
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-threads", "1", "-i", str(cover_path),
            "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease",
            "-frames:v", "1", "-q:v", "5", "-y", str(output_path),
        ],
        check=True,
        capture_output=True,
        timeout=int(settings.HERMES_VIDEO_ANALYSIS_FFMPEG_TIMEOUT_SECONDS),
    )
    return output_path.stat().st_size


def _data_url(path: Path) -> str:
    payload = path.read_bytes()
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("normalized visual evidence exceeds the bounded input size")
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def _extract_json(text_value: str) -> dict[str, Any]:
    raw = str(text_value or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        value = json.loads(raw)
        return dict(value) if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(raw[start : end + 1])
            return dict(value) if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}


def _bounded_list(value: Any, *, limit: int, chars: int = 500) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            result.append({str(key)[:80]: str(val)[:chars] for key, val in list(item.items())[:12]})
        else:
            result.append(str(item)[:chars])
    return result


def _bounded_mapping(value: Any, *, limit: int = 16, chars: int = 500) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:80]: str(item)[:chars]
        for key, item in list(value.items())[:limit]
    }


def _normalize_result(value: dict[str, Any]) -> dict[str, Any]:
    confidence = max(0.0, min(1.0, _number(value.get("confidence"))))
    return {
        "summary": str(value.get("summary") or "")[:1200],
        "content_profile": _bounded_mapping(value.get("content_profile")),
        "hook_analysis": _bounded_mapping(value.get("hook_analysis")),
        "product_analysis": _bounded_mapping(value.get("product_analysis")),
        "spoken_copy_analysis": _bounded_mapping(value.get("spoken_copy_analysis")),
        "pacing_analysis": _bounded_mapping(value.get("pacing_analysis")),
        "cta_analysis": _bounded_mapping(value.get("cta_analysis")),
        "timeline": _bounded_list(value.get("timeline"), limit=12),
        "metric_interpretation": _bounded_mapping(value.get("metric_interpretation")),
        "strengths": _bounded_list(value.get("strengths"), limit=8),
        "problems": _bounded_list(value.get("problems"), limit=8),
        "actions": _bounded_list(value.get("actions"), limit=8),
        "next_experiment": _bounded_mapping(value.get("next_experiment")),
        "confidence": round(confidence, 3),
        "limitations": _bounded_list(value.get("limitations"), limit=8),
        "source": "HERMES_VIDEO_ANALYST",
        "prompt_version": PROMPT_VERSION,
    }


ANALYST_INSTRUCTIONS = """
You are the isolated video content analyst for a TikTok Shop and GMV Max operations system.
Analyze only the supplied ordered visual contact sheet or cover, Whisper transcript evidence, and structured metrics packet.
Never invent dialogue, audio, claims, products, causal attribution, or unseen scenes. A transcript status of NO_SPEECH
means the video has no reliable spoken copy; do not convert music into dialogue. A FAILED or UNAVAILABLE transcript is
missing evidence, not proof that the video is silent. Shop analytics and paid GMV Max
metrics are separate official sources; compare them, but do not claim one caused the other. Missing paid metrics mean
not observed, never zero. The contact-sheet cells are chronological, left-to-right then top-to-bottom.

Return exactly one JSON object with these keys:
- summary: concise operational conclusion
- content_profile: object with hook, format, audience, product_visibility, pacing, cta, production_style
- hook_analysis: object with first_2_seconds, spoken_hook, visual_hook, promise, risk
- product_analysis: object with products, first_reveal_time, demonstration, proof, offer_handoff
- spoken_copy_analysis: object with status, structure, selling_points, objections, claims, clarity
- pacing_analysis: object with opening, middle, ending, likely_drop_points, edit_rhythm
- cta_analysis: object with spoken_cta, visual_cta, timing, clarity, friction
- timeline: array of objects with time_or_cell, visual, spoken_copy, product_exposure, purpose, operational_meaning
- metric_interpretation: object with organic_shop, paid_playback_funnel, commerce_handoff
- strengths: array of evidence-grounded strings
- problems: array of objects with severity, issue, visual_evidence, metric_evidence, why_it_matters
- actions: array of objects with priority, action, expected_metric, validation_window
- next_experiment: object with hypothesis, change_one_variable, control, success_metric
- confidence: number from 0 to 1
- limitations: array of explicit missing evidence

Prioritize decisions a content operator can execute: first-2-second hook, 6-second retention, product reveal timing,
demonstration clarity, offer/CTA handoff, and where viewers drop through 25/50/75/100 percent completion. If a metric
is absent, say so. Do not prescribe pausing or scaling an ad; this role advises content production only.
All human-readable JSON values must be written in concise Simplified Chinese. Keep the required JSON keys unchanged.
Keep the complete JSON under 2200 Chinese characters: timeline at most 8 entries; strengths, problems, and actions at
most 3 entries each; limitations at most 4 entries; each string at most 80 Chinese characters. Do not add markdown.
""".strip()


async def run_analysis(
    db: Session,
    row: TikTokShopVideoContentAnalysis,
) -> TikTokShopVideoContentAnalysis:
    shop = db.get(OAuthTikTokShopShop, int(row.shop_row_id))
    if not shop or int(shop.workspace_id) != int(row.workspace_id):
        raise APIError("TIKTOK_SHOP_NOT_FOUND", "TikTok Shop not found.", 404)
    media_row = _media_row(
        db,
        workspace_id=int(row.workspace_id),
        provider_shop_id=str(shop.shop_id),
        video_id=str(row.video_id),
    )
    video = resolve_creative_media(media_row, "video") if media_row else None
    cover = resolve_creative_media(media_row, "cover") if media_row else None
    if not video and not cover:
        row.status = "UNAVAILABLE"
        row.error_code = "VIDEO_MEDIA_UNAVAILABLE"
        row.error_message = "Local video and cover are unavailable; no model request was charged."
        row.evidence_json = {
            "visual_status": "UNAVAILABLE",
            "media_cache_status": (media_row or {}).get("media_cache_status"),
            "temporary_artifacts_retained": False,
        }
        row.completed_at = utcnow()
        row.lease_expires_at = None
        db.add(row)
        db.commit()
        return row

    # Release the read transaction before ffmpeg or the model API can block.
    # The analysis row has already been leased by the worker and all remaining
    # inputs are immutable JSON or resolved local paths.
    db.commit()
    visual_kind = "CONTACT_SHEET" if video else "COVER_ONLY"
    timestamps: list[float] = []
    normalized_bytes = 0
    with tempfile.TemporaryDirectory(prefix=f"gmv-video-analysis-{row.id}-") as directory:
        output_path = Path(directory) / "visual.jpg"
        try:
            if video:
                timestamps, normalized_bytes = _render_contact_sheet(video[0], output_path)
            else:
                normalized_bytes = _normalize_cover(cover[0], output_path)  # type: ignore[index]
            image_url = _data_url(output_path)
            packet = dict(row.input_summary_json or {})
            transcript_status = str(row.transcript_status or "UNAVAILABLE").upper()
            transcript_segments = list(row.transcript_segments_json or [])[:120]
            transcript_text = str(row.transcript_text or "")[:16000]
            packet["visual_evidence"] = {
                "kind": visual_kind,
                "sampled_timestamps_seconds": timestamps,
            }
            packet["audio_evidence"] = {
                "source": row.transcript_source or "WHISPER_LOCAL",
                "status": transcript_status,
                "language": row.transcript_language,
                "reason": row.transcript_reason,
                "transcript": transcript_text if transcript_status == "READY" else None,
                "segments": transcript_segments if transcript_status == "READY" else [],
            }
            input_text = json.dumps(packet, ensure_ascii=False, separators=(",", ":"), default=str)
            response, latency_ms = await HermesVideoAnalystClient().create_response(
                input_text=input_text,
                input_items=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": input_text},
                        {"type": "input_image", "image_url": image_url, "detail": "low"},
                    ],
                }],
                instructions=ANALYST_INSTRUCTIONS,
                metadata={
                    "source": "tiktok_shop_video_analysis",
                    "prompt_version": PROMPT_VERSION,
                    "analysis_id": str(row.id),
                },
                idempotency_key=f"gmv-video-analysis-{row.cache_key}",
            )
            parsed = _extract_json(extract_output_text(response))
            if not parsed:
                raise APIError("HERMES_BAD_RESPONSE", "Hermes returned invalid analysis JSON.", 502)
            row.analysis_json = _normalize_result(parsed)
            row.usage_json = {**extract_usage(response), "latency_ms": latency_ms}
            row.evidence_json = {
                "visual_status": "READY",
                "visual_kind": visual_kind,
                "sampled_timestamps_seconds": timestamps,
                "normalized_image_bytes": normalized_bytes,
                "image_detail": "low",
                "audio_transcript_status": transcript_status,
                "audio_transcript_source": row.transcript_source,
                "audio_transcript_language": row.transcript_language,
                "audio_transcript_segments": len(transcript_segments),
                "temporary_artifacts_retained": False,
            }
            row.status = "SUCCEEDED"
            row.error_code = None
            row.error_message = None
            row.completed_at = utcnow()
            row.lease_expires_at = None
            db.add(row)
            db.commit()
            return row
        finally:
            # TemporaryDirectory removes the normalized visual even for timeout,
            # cancellation, invalid JSON, or database failure. The base64 value
            # stays local to this stack frame and is never persisted.
            if "image_url" in locals():
                del image_url


__all__ = [
    "FINAL_STATUSES",
    "PROMPT_VERSION",
    "PROVIDER_MODEL",
    "TRANSCRIPT_PIPELINE_VERSION",
    "analysis_cache_key",
    "build_metric_packet",
    "find_media_identity",
    "run_analysis",
    "serialize_analysis",
    "utcnow",
]
