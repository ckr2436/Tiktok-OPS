from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, List

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.gmv_restructured import PromotionTypeEnum
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.services.gmvmax_creative_metrics import sync_creative_metrics_10min_for_campaign
from app.services.ttb_client_factory import build_ttb_gmvmax_client

logger = logging.getLogger("gmv.services.gmvmax.sync")


_ACTIVE_STATUSES = {
    "ENABLE",
    "ENABLED",
    "ACTIVE",
    "DELIVERING",
    "RUNNING",
}


class GmvMaxSyncService:
    """Dispatch GMV Max sync work according to monitoring strategies."""

    def __init__(self, session_factory: callable = SessionLocal) -> None:  # type: ignore[type-arg]
        self._session_factory = session_factory

    def sync_strategy(self, strategy: MonitoringStrategy, now: datetime) -> None:
        handlers = {
            "OVERVIEW_DAILY": self._noop,
            "CAMPAIGN_DAILY": self._noop,
            "CAMPAIGN_HOURLY": self._noop,
            "PRODUCT_DAILY": self._noop,
            "PRODUCT_HOURLY": self._noop,
            "CREATIVE_10MIN": self._sync_creative_10min,
            "LIVESTREAM_DAILY": self._noop,
            "LIVESTREAM_HOURLY": self._noop,
            "DURATION_DAILY": self._noop,
            "DURATION_HOURLY": self._noop,
        }

        handler = handlers.get(strategy.level)
        if handler is None:
            logger.warning("gmvmax sync strategy level not recognized", extra={"level": strategy.level})
            return

        handler(strategy, now)

    def _noop(self, strategy: MonitoringStrategy, now: datetime) -> None:  # noqa: ARG002
        logger.info(
            "gmvmax sync placeholder - extend with concrete logic",
            extra={"strategy_id": strategy.id, "level": strategy.level},
        )

    def _select_active_campaigns(self, session: Session, strategy: MonitoringStrategy) -> list[TTBGmvMaxCampaign]:
        promotion_filter = TTBGmvMaxCampaign.promotion_type == PromotionTypeEnum.PRODUCT
        if strategy.promotion_type:
            try:
                promotion_filter = TTBGmvMaxCampaign.promotion_type == PromotionTypeEnum(
                    strategy.promotion_type
                )
            except ValueError:
                logger.warning(
                    "gmvmax monitoring strategy has invalid promotion type",
                    extra={"strategy_id": strategy.id, "promotion_type": strategy.promotion_type},
                )

        stmt = (
            select(TTBGmvMaxCampaign)
            .where(TTBGmvMaxCampaign.workspace_id == strategy.workspace_id)
            .where(TTBGmvMaxCampaign.is_deleted.is_(False))
            .where(promotion_filter)
            .where(TTBGmvMaxCampaign.status.in_(_ACTIVE_STATUSES))
            .where(
                or_(
                    TTBGmvMaxCampaign.operation_status.is_(None),
                    TTBGmvMaxCampaign.operation_status.notin_(("DELETE", "STATUS_DISABLE")),
                )
            )
            .order_by(TTBGmvMaxCampaign.ext_updated_time.desc())
        )

        # 可选过滤：按 auth_id / store_id 进一步缩小范围
        if strategy.auth_id is not None:
            stmt = stmt.where(TTBGmvMaxCampaign.auth_id == strategy.auth_id)
        if strategy.store_id:
            stmt = stmt.where(TTBGmvMaxCampaign.store_id == strategy.store_id)

        if strategy.max_campaigns_per_run:
            stmt = stmt.limit(int(strategy.max_campaigns_per_run))

        return list(session.execute(stmt).scalars().all())

    def _sync_creative_10min(self, strategy: MonitoringStrategy, now: datetime) -> None:
        today = now.date()
        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy)
            if not campaigns:
                logger.info(
                    "gmvmax creative 10min sync skipped: no active campaigns",
                    extra={"strategy_id": strategy.id},
                )
                return

            async def _run() -> int:
                written = 0

                # 按 auth_id 分组，保证每个授权只建一个 client
                grouped: Dict[int, List[TTBGmvMaxCampaign]] = {}
                for campaign in campaigns:
                    auth_id = getattr(campaign, "auth_id", None)
                    if auth_id is None:
                        logger.warning(
                            "gmvmax campaign without auth_id skipped in creative 10min sync",
                            extra={"campaign_id": getattr(campaign, "id", None)},
                        )
                        continue
                    grouped.setdefault(int(auth_id), []).append(campaign)

                for auth_id, auth_campaigns in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for campaign in auth_campaigns:
                            provider = getattr(campaign, "provider", "tiktok-business")
                            advertiser_id = strategy.advertiser_id or getattr(
                                campaign, "advertiser_id", None
                            )
                            if not advertiser_id:
                                logger.warning(
                                    "gmvmax creative 10min sync skipped: missing advertiser_id",
                                    extra={"campaign_id": getattr(campaign, "id", None)},
                                )
                                continue

                            result = await sync_creative_metrics_10min_for_campaign(
                                session,
                                client,
                                workspace_id=strategy.workspace_id,
                                provider=provider,
                                auth_id=auth_id,
                                advertiser_id=str(advertiser_id),
                                campaign=campaign,
                                start_date=today,
                                end_date=today,
                            )
                            written += int(result.get("rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return written

            rows_written = asyncio.run(_run())
            session.commit()
            logger.info(
                "gmvmax creative 10min sync done",
                extra={
                    "strategy_id": strategy.id,
                    "campaigns": len(campaigns),
                    "rows_written": rows_written,
                    "date": today.isoformat(),
                },
            )
