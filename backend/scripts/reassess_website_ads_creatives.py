from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json

from sqlalchemy import select

from app.data.db import SessionLocal
from app.data.models.website_ads import (
    WebsiteAdsAd,
    WebsiteAdsAdGroup,
    WebsiteAdsCampaign,
    WebsiteAdsCreativeAsset,
)
from app.services.website_ads_creative_policy import assess_website_ads_creative_policy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reassess Website Ads creatives after changing internal risk labels to advisory."
    )
    parser.add_argument("--workspace-id", type=int)
    parser.add_argument("--landing-page-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        active_keys = {
            (int(workspace_id), int(auth_id), str(advertiser_id), str(video_id))
            for workspace_id, auth_id, advertiser_id, video_id in db.execute(
                select(
                    WebsiteAdsCampaign.workspace_id,
                    WebsiteAdsCampaign.auth_id,
                    WebsiteAdsCampaign.advertiser_id,
                    WebsiteAdsAd.video_id,
                )
                .join(WebsiteAdsAdGroup, WebsiteAdsAdGroup.campaign_local_id == WebsiteAdsCampaign.id)
                .join(WebsiteAdsAd, WebsiteAdsAd.adgroup_local_id == WebsiteAdsAdGroup.id)
                .where(
                    WebsiteAdsCampaign.local_status == "ACTIVE",
                    WebsiteAdsCampaign.operation_status == "ENABLE",
                    WebsiteAdsAdGroup.operation_status == "ENABLE",
                    WebsiteAdsAd.operation_status == "ENABLE",
                )
            ).all()
        }
        query = select(WebsiteAdsCreativeAsset).where(WebsiteAdsCreativeAsset.is_active.is_(True))
        if args.workspace_id is not None:
            query = query.where(WebsiteAdsCreativeAsset.workspace_id == int(args.workspace_id))
        if args.landing_page_id is not None:
            query = query.where(WebsiteAdsCreativeAsset.landing_page_id == int(args.landing_page_id))
        assets = list(db.scalars(query.order_by(WebsiteAdsCreativeAsset.id)).all())
        counts: Counter[str] = Counter()
        examples: dict[str, list[int]] = {}
        for asset in assets:
            analysis = dict(asset.hermes_analysis_json or {})
            official_rejected = (
                str(asset.auto_launch_status or "").upper() == "AUDIT_REJECTED"
                or str(analysis.get("platform_review_readiness") or "").upper() == "REJECTED"
            )
            if official_rejected:
                counts["official_rejection_preserved"] += 1
                continue

            if (
                str(asset.analysis_status or "").upper() == "FAILED"
                and "not json serializable" in str(asset.analysis_error or "").lower()
            ):
                counts["analysis_requeued"] += 1
                examples.setdefault("analysis_requeued", []).append(int(asset.id))
                if args.apply:
                    asset.analysis_status = "NOT_ANALYZED"
                    asset.analysis_error = None
                    asset.analysis_next_retry_at = None
                    asset.auto_launch_status = "PENDING"
                    asset.auto_launch_next_retry_at = None
                    asset.auto_launch_error = None
                    db.add(asset)
                continue

            if str(asset.analysis_status or "").upper() != "READY":
                counts["not_ready_unchanged"] += 1
                continue
            policy = assess_website_ads_creative_policy(analysis)
            if not policy["eligible_for_automatic_launch"]:
                counts["missing_analysis_unchanged"] += 1
                continue
            key = (
                int(asset.workspace_id),
                int(asset.auth_id),
                str(asset.advertiser_id),
                str(asset.video_id),
            )
            desired = "DEPLOYED" if key in active_keys else "PENDING"
            if str(asset.auto_launch_status or "").upper() != desired:
                counts[f"reclassified_{desired.lower()}"] += 1
                examples.setdefault(f"reclassified_{desired.lower()}", []).append(int(asset.id))
            else:
                counts[f"already_{desired.lower()}"] += 1
            counts[f"risk_{str(policy['readiness']).lower()}"] += 1
            if args.apply:
                asset.auto_launch_status = desired
                asset.auto_launch_next_retry_at = None
                asset.auto_launch_error = None
                asset.auto_launch_decision_json = {
                    "status": desired,
                    "reason": "RISK_LABELS_REASSESSED_AS_ADVISORY",
                    "policy": policy,
                    "reassessed_at": _utcnow().isoformat(timespec="seconds") + "Z",
                }
                db.add(asset)
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(
            json.dumps(
                {
                    "applied": bool(args.apply),
                    "assets_scanned": len(assets),
                    "counts": dict(sorted(counts.items())),
                    "examples": {key: value[:20] for key, value in examples.items()},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
