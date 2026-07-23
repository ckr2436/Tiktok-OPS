from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.ttb_entities import TTBBindingConfig
from app.data.models.website_ads import WebsiteAdsCreativeAsset
from app.features.tenants.openai_whisper import storage, transcriber
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.ttb_client_factory import build_ttb_client
from app.services.website_ads_media_cache import ensure_asset_media_cache, resolve_asset_media


ASSET_ANALYSIS_VERSION = "website_ads_asset_v2"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def asset_job_id(asset: WebsiteAdsCreativeAsset) -> str:
    return f"website-ads-asset-{int(asset.id)}"


def asset_contact_sheet_path(asset: WebsiteAdsCreativeAsset) -> Path:
    return storage.contact_sheet_path(storage.job_dir(int(asset.workspace_id), asset_job_id(asset)))


def resolve_asset_contact_sheet(asset: WebsiteAdsCreativeAsset) -> Path | None:
    path = asset_contact_sheet_path(asset)
    return path if path.exists() and path.is_file() else None


def _source_url(asset: WebsiteAdsCreativeAsset) -> str:
    raw = asset.raw_json if isinstance(asset.raw_json, dict) else {}
    for value in (
        asset.preview_url,
        raw.get("preview_url"),
        raw.get("video_url"),
        raw.get("play_url"),
        raw.get("download_url"),
    ):
        text = str(value or "").strip()
        if text.startswith(("https://", "http://")):
            return text
    return ""


def _transcript(result: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get("text") or "").strip()
        for item in result.get("segments") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )[:50000]


async def run_asset_analysis_pipeline(db: Session, asset_id: int) -> WebsiteAdsCreativeAsset:
    asset = db.get(WebsiteAdsCreativeAsset, int(asset_id))
    if not asset or not asset.is_active:
        raise ValueError("Creative asset is unavailable")

    asset.analysis_status = "EXTRACTING"
    asset.analysis_error = None
    asset.analysis_attempts = int(asset.analysis_attempts or 0) + 1
    asset.analysis_next_retry_at = None
    db.add(asset)
    db.commit()

    source_url = _source_url(asset)
    try:
        cache_result = await ensure_asset_media_cache(db, int(asset.id))
    except Exception as exc:
        cache_result = {
            "video_cached": False,
            "cover_cached": False,
            "errors": {"cache": f"{type(exc).__name__}: {exc}"[:1000]},
        }
    db.refresh(asset)
    cached_video = resolve_asset_media(asset, "video")
    evidence: dict[str, Any] = {
        "analysis_version": ASSET_ANALYSIS_VERSION,
        "source_url_available": bool(source_url),
        "permanent_media_cache": cache_result,
        "transcript_status": "unavailable",
        "contact_sheet_status": "unavailable",
    }
    video_path = cached_video[0] if cached_video else None
    evidence["download_status"] = "success" if video_path else "unavailable"

    contact_sheet_path: Path | None = None
    if video_path and video_path.exists():
        try:
            result = await asyncio.to_thread(transcriber.transcribe, video_path, translate=False)
            asset.transcript_text = _transcript(result)
            asset.transcript_language = str(result.get("detected_language") or result.get("source_language") or "")[:32] or None
            evidence["transcript_status"] = "success"
            evidence["transcript_segments"] = len(result.get("segments") or [])
        except Exception as exc:
            evidence["transcript_status"] = "failed"
            evidence["transcript_error"] = f"{type(exc).__name__}: {exc}"[:1000]

        try:
            from app.features.tenants.openai_whisper.tasks import _render_contact_sheet

            contact_sheet_path = await asyncio.to_thread(
                _render_contact_sheet,
                video_path,
                int(asset.workspace_id),
                asset_job_id(asset),
                2.0,
            )
            asset.contact_sheet_url = (
                f"/api/v1/tenants/{int(asset.workspace_id)}/tiktok-business/{int(asset.auth_id)}"
                f"/website-ads/creative-assets/{int(asset.id)}/contact-sheet"
            )
            evidence["contact_sheet_status"] = "success"
        except Exception as exc:
            evidence["contact_sheet_status"] = "failed"
            evidence["contact_sheet_error"] = f"{type(exc).__name__}: {exc}"[:1000]

    asset.analysis_inputs_json = evidence
    asset.analysis_version = ASSET_ANALYSIS_VERSION
    db.add(asset)
    db.commit()
    db.refresh(asset)

    from app.services.website_ads_hermes_planner import analyze_creative_asset

    return await analyze_creative_asset(
        db,
        asset,
        transcript=asset.transcript_text,
        contact_sheet_path=contact_sheet_path,
        evidence=evidence,
    )


async def sync_asset_libraries(db: Session, *, workspace_id: int | None = None) -> dict[str, int]:
    query = select(TTBBindingConfig).where(TTBBindingConfig.advertiser_id.is_not(None))
    if workspace_id is not None:
        query = query.where(TTBBindingConfig.workspace_id == int(workspace_id))
    bindings = list(db.scalars(query.order_by(TTBBindingConfig.id)).all())
    synced = 0
    failed = 0
    discovered = 0
    for binding in bindings:
        api = TikTokWebsiteAdsClient(build_ttb_client(db, int(binding.auth_id)))
        try:
            payload = await api.list_all_videos(str(binding.advertiser_id))
            spark_payloads = await api.list_all_spark_videos(str(binding.advertiser_id))
            from app.services.website_ads_hermes_planner import sync_creative_assets, sync_spark_creative_assets

            rows = sync_creative_assets(
                db,
                workspace_id=int(binding.workspace_id),
                auth_id=int(binding.auth_id),
                advertiser_id=str(binding.advertiser_id),
                videos_payload=payload.get("data", {}),
                complete_snapshot=True,
            )
            spark_rows = sync_spark_creative_assets(
                db,
                workspace_id=int(binding.workspace_id),
                auth_id=int(binding.auth_id),
                advertiser_id=str(binding.advertiser_id),
                spark_payloads=[item.get("data", {}) for item in spark_payloads],
                complete_snapshot=True,
            )
            synced += 1
            discovered += len(rows) + len(spark_rows)
        except Exception:
            db.rollback()
            failed += 1
        finally:
            await api.aclose()
    return {"bindings": len(bindings), "synced": synced, "failed": failed, "assets": discovered}
