from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.gmv_restructured import PromotionTypeEnum
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.services.gmvmax_creative_metrics import sync_creative_metrics_10min_for_campaign
from app.services.ttb_client_factory import build_ttb_gmvmax_client
from app.services.ttb_gmvmax import (
    sync_gmvmax_metrics_daily,
    sync_gmvmax_metrics_hourly,
    sync_gmvmax_overview_metrics,
    sync_gmvmax_product_metrics_daily,
    sync_gmvmax_product_metrics_hourly,
    sync_gmvmax_livestream_metrics_daily,
    sync_gmvmax_livestream_metrics_hourly,
    sync_gmvmax_duration_metrics_daily,
    sync_gmvmax_duration_metrics_hourly,
)

logger = logging.getLogger("gmv.services.gmvmax.sync")


_ACTIVE_LIFECYCLE_STATUSES = {"ACTIVE"}


class GmvMaxSyncService:
    """Dispatch GMV Max sync work according to monitoring strategies."""

    def __init__(self, session_factory: callable = SessionLocal) -> None:  # type: ignore[type-arg]
        self._session_factory = session_factory

    def sync_strategy(self, strategy: MonitoringStrategy, now: datetime) -> None:
        handlers = {
            "OVERVIEW_DAILY": self._sync_overview_daily,
            "CAMPAIGN_DAILY": self._sync_campaign_daily,
            "CAMPAIGN_HOURLY": self._sync_campaign_hourly,
            "PRODUCT_DAILY": self._sync_product_daily,
            "PRODUCT_HOURLY": self._sync_product_hourly,
            "CREATIVE_10MIN": self._sync_creative_10min,
            "LIVESTREAM_DAILY": self._sync_livestream_daily,
            "LIVESTREAM_HOURLY": self._sync_livestream_hourly,
            "DURATION_DAILY": self._sync_duration_daily,
            "DURATION_HOURLY": self._sync_duration_hourly,
        }

        handler = handlers.get(strategy.level)
        if handler is None:
            logger.warning("gmvmax sync strategy level not recognized", extra={"level": strategy.level})
            return

        handler(strategy, now)

    def sync_levels_for_account(
        self,
        *,
        workspace_id: int,
        auth_id: int,
        levels: list[str],
        start_date: date,
        end_date: date,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, dict[str, int]]:
        """Manually trigger GMV Max sync for specific levels scoped to an auth account."""

        now = datetime.utcnow()
        strategy = MonitoringStrategy(
            id=0,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=None,
            store_id=None,
            level="CAMPAIGN_HOURLY",
            category="GMVMAX",
            task_name="gmvmax.manual_sync",
            interval_minutes=0,
            enabled=True,
            promotion_type=None,
            max_campaigns_per_run=None,
            params_json={},
            input_schema_json=None,
        )

        handlers: dict[str, Callable[[], dict[str, int]]] = {
            "OVERVIEW": lambda: self._sync_overview_metrics(
                strategy,
                start_date=start_date,
                end_date=end_date,
                granularity="HOURLY",
                campaign_ids=campaign_ids,
            ),
            "CAMPAIGN": lambda: self._sync_campaign_metrics(
                strategy,
                start_date=start_date,
                end_date=end_date,
                granularity="HOUR",
                campaign_ids=campaign_ids,
            ),
            "PRODUCT": lambda: self._sync_product_metrics(
                strategy,
                start_date=start_date,
                end_date=end_date,
                granularity="HOUR",
                campaign_ids=campaign_ids,
            ),
            "CREATIVE": lambda: self._sync_creative_10min(
                strategy,
                now,
                start_date=start_date,
                end_date=end_date,
                campaign_ids=campaign_ids,
            ),
            "LIVESTREAM": lambda: self._sync_livestream_metrics(
                strategy,
                start_date=start_date,
                end_date=end_date,
                granularity="HOUR",
                campaign_ids=campaign_ids,
            ),
            "DURATION": lambda: self._sync_duration_metrics(
                strategy,
                start_date=start_date,
                end_date=end_date,
                granularity="HOUR",
                campaign_ids=campaign_ids,
            ),
        }

        results: dict[str, dict[str, int]] = {}
        for level in levels:
            normalized = level.upper()
            handler = handlers.get(normalized)
            if handler is None:
                logger.warning(
                    "gmvmax manual sync skipped unknown level",
                    extra={"workspace_id": workspace_id, "auth_id": auth_id, "level": level},
                )
                continue

            results[normalized] = handler()

        return results

    def _noop(self, strategy: MonitoringStrategy, now: datetime) -> None:  # noqa: ARG002
        logger.info(
            "gmvmax sync placeholder - extend with concrete logic",
            extra={"strategy_id": strategy.id, "level": strategy.level},
        )

    def _sync_campaign_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_campaign_metrics(strategy, start_date=start, end_date=now.date(), granularity="HOUR")

    def _sync_campaign_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_campaign_metrics(strategy, start_date=start, end_date=now.date(), granularity="DAY")

    def _sync_product_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_product_metrics(strategy, start_date=start, end_date=now.date(), granularity="HOUR")

    def _sync_product_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_product_metrics(strategy, start_date=start, end_date=now.date(), granularity="DAY")

    def _sync_livestream_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_livestream_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="HOUR"
        )

    def _sync_livestream_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_livestream_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="DAY"
        )

    def _sync_duration_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_duration_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="HOUR"
        )

    def _sync_duration_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_duration_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="DAY"
        )

    def _sync_overview_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_overview_metrics(
            strategy,
            start_date=start,
            end_date=now.date(),
            granularity="DAILY",
        )

    def _select_active_campaigns(
        self,
        session: Session,
        strategy: MonitoringStrategy,
        *,
        campaign_ids: Optional[list[str]] = None,
    ) -> list[TTBGmvMaxCampaign]:
        stmt = (
            select(TTBGmvMaxCampaign)
            .where(TTBGmvMaxCampaign.workspace_id == strategy.workspace_id)
            .where(TTBGmvMaxCampaign.is_deleted.is_(False))
            .where(TTBGmvMaxCampaign.lifecycle_status.in_(_ACTIVE_LIFECYCLE_STATUSES))
            .order_by(TTBGmvMaxCampaign.ext_updated_time.desc())
        )

        if strategy.promotion_type:
            try:
                stmt = stmt.where(
                    TTBGmvMaxCampaign.promotion_type
                    == PromotionTypeEnum(strategy.promotion_type)
                )
            except ValueError:
                logger.warning(
                    "gmvmax monitoring strategy has invalid promotion type",
                    extra={"strategy_id": strategy.id, "promotion_type": strategy.promotion_type},
                )

        # 可选过滤：按 auth_id / store_id 进一步缩小范围
        if strategy.auth_id is not None:
            stmt = stmt.where(TTBGmvMaxCampaign.auth_id == strategy.auth_id)
        if strategy.store_id:
            stmt = stmt.where(TTBGmvMaxCampaign.store_id == strategy.store_id)
        if campaign_ids:
            stmt = stmt.where(TTBGmvMaxCampaign.campaign_id.in_(campaign_ids))

        limit_value = 30
        if strategy.max_campaigns_per_run:
            try:
                candidate = int(strategy.max_campaigns_per_run)
                if candidate > 0:
                    limit_value = candidate
            except (TypeError, ValueError):
                logger.warning(
                    "gmvmax monitoring strategy has invalid max_campaigns_per_run",
                    extra={"strategy_id": strategy.id, "max_campaigns_per_run": strategy.max_campaigns_per_run},
                )

        stmt = stmt.limit(limit_value)

        return list(session.execute(stmt).scalars().all())

    def _sync_campaign_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy, campaign_ids=campaign_ids)
            if not campaigns:
                logger.info(
                    "gmvmax campaign metrics sync skipped: no active campaigns",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                        "granularity": granularity,
                    },
                )
                return {"synced_rows": 0}

            async def _run() -> dict[str, int]:
                totals: Dict[str, int] = {"campaign_rows": 0, "overview_rows": 0}

                grouped: Dict[int, List[TTBGmvMaxCampaign]] = {}
                for campaign in campaigns:
                    auth_id = getattr(campaign, "auth_id", None)
                    if auth_id is None:
                        logger.warning(
                            "gmvmax campaign metrics sync skipped campaign without auth_id",
                            extra={"campaign_id": getattr(campaign, "id", None)},
                        )
                        continue
                    grouped.setdefault(int(auth_id), []).append(campaign)

                for auth_id, auth_campaigns in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for campaign in auth_campaigns:
                            advertiser_id = strategy.advertiser_id or getattr(campaign, "advertiser_id", None)
                            if not advertiser_id:
                                logger.warning(
                                    "gmvmax campaign metrics sync skipped: missing advertiser_id",
                                    extra={"campaign_id": getattr(campaign, "id", None)},
                                )
                                continue

                            if granularity.upper() == "DAY":
                                result = await sync_gmvmax_metrics_daily(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                            else:
                                result = await sync_gmvmax_metrics_hourly(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                )

                            totals["campaign_rows"] += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            session.commit()
            logger.info(
                "gmvmax campaign metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "result": result,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            return {"synced_rows": int(result.get("campaign_rows", 0) or 0)}

    def _sync_product_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, int]:
        if strategy.promotion_type and strategy.promotion_type != PromotionTypeEnum.PRODUCT.value:
            logger.warning(
                "gmvmax product metrics sync skipped: unsupported promotion_type",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "promotion_type": strategy.promotion_type,
                    "granularity": granularity,
                },
            )
            return {"synced_rows": 0}

        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy, campaign_ids=campaign_ids)
            if not campaigns:
                logger.info(
                    "gmvmax product metrics sync skipped: no active campaigns",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                        "granularity": granularity,
                    },
                )
                return {"synced_rows": 0}

            async def _run() -> dict[str, int]:
                totals: Dict[str, int] = {"product_rows": 0}

                grouped: Dict[int, List[TTBGmvMaxCampaign]] = {}
                for campaign in campaigns:
                    auth_id = getattr(campaign, "auth_id", None)
                    if auth_id is None:
                        logger.warning(
                            "gmvmax product metrics sync skipped campaign without auth_id",
                            extra={"campaign_id": getattr(campaign, "id", None)},
                        )
                        continue
                    grouped.setdefault(int(auth_id), []).append(campaign)

                for auth_id, auth_campaigns in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for campaign in auth_campaigns:
                            advertiser_id = strategy.advertiser_id or getattr(campaign, "advertiser_id", None)
                            if not advertiser_id:
                                logger.warning(
                                    "gmvmax product metrics sync skipped: missing advertiser_id",
                                    extra={"campaign_id": getattr(campaign, "id", None)},
                                )
                                continue

                            if granularity.upper() == "DAY":
                                result = await sync_gmvmax_product_metrics_daily(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                            else:
                                result = await sync_gmvmax_product_metrics_hourly(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                )

                            totals["product_rows"] += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            session.commit()
            logger.info(
                "gmvmax product metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "result": result,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            return {"synced_rows": int(result.get("product_rows", 0) or 0)}

    def _sync_overview_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy, campaign_ids=campaign_ids)
            if not campaigns:
                logger.info(
                    "gmvmax overview sync skipped: no active campaigns",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                        "granularity": granularity,
                    },
                )
                return {"synced_rows": 0}

            async def _run() -> int:
                written = 0
                grouped: Dict[int, List[TTBGmvMaxCampaign]] = {}
                for campaign in campaigns:
                    auth_id = getattr(campaign, "auth_id", None)
                    if auth_id is None:
                        logger.warning(
                            "gmvmax overview sync skipped campaign without auth_id",
                            extra={"campaign_id": getattr(campaign, "id", None)},
                        )
                        continue
                    grouped.setdefault(int(auth_id), []).append(campaign)

                for auth_id, auth_campaigns in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        advertiser_id = strategy.advertiser_id or getattr(auth_campaigns[0], "advertiser_id", None)
                        if not advertiser_id:
                            logger.warning(
                                "gmvmax overview sync skipped: missing advertiser_id",
                                extra={"strategy_id": strategy.id, "auth_id": auth_id},
                            )
                            continue

                        store_ids = {
                            str(c.store_id)
                            for c in auth_campaigns
                            if getattr(c, "store_id", None)
                        }
                        if strategy.store_id:
                            store_ids.add(str(strategy.store_id))

                        if not store_ids:
                            logger.warning(
                                "gmvmax overview sync skipped: no store_id available",
                                extra={"strategy_id": strategy.id, "auth_id": auth_id},
                            )
                            continue

                        result = await sync_gmvmax_overview_metrics(
                            session,
                            client,
                            workspace_id=strategy.workspace_id,
                            auth_id=auth_id,
                            advertiser_id=str(advertiser_id),
                            store_ids=sorted(store_ids),
                            start_date=start_date,
                            end_date=end_date,
                            granularity=granularity,
                        )
                        written += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return written

            rows_written = asyncio.run(_run())
            session.commit()
            logger.info(
                "gmvmax overview metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "rows_written": rows_written,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            return {"synced_rows": int(rows_written or 0)}

    def _sync_livestream_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy, campaign_ids=campaign_ids)
            if not campaigns:
                logger.info(
                    "gmvmax livestream metrics sync skipped: no active campaigns",
                    extra={"strategy_id": strategy.id, "workspace_id": strategy.workspace_id},
                )
                return {"synced_rows": 0}

            async def _run() -> dict[str, int]:
                totals: Dict[str, int] = {"livestream_rows": 0}
                grouped: Dict[int, List[TTBGmvMaxCampaign]] = {}
                for campaign in campaigns:
                    auth_id = getattr(campaign, "auth_id", None)
                    if auth_id is None:
                        logger.warning(
                            "gmvmax livestream metrics sync skipped campaign without auth_id",
                            extra={"campaign_id": getattr(campaign, "id", None)},
                        )
                        continue
                    grouped.setdefault(int(auth_id), []).append(campaign)

                for auth_id, auth_campaigns in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for campaign in auth_campaigns:
                            advertiser_id = strategy.advertiser_id or getattr(campaign, "advertiser_id", None)
                            if not advertiser_id:
                                logger.warning(
                                    "gmvmax livestream metrics sync skipped: missing advertiser_id",
                                    extra={"campaign_id": getattr(campaign, "id", None)},
                                )
                                continue

                            if granularity.upper() == "DAY":
                                result = await sync_gmvmax_livestream_metrics_daily(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                    campaign_ids=campaign_ids,
                                )
                            else:
                                result = await sync_gmvmax_livestream_metrics_hourly(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                    campaign_ids=campaign_ids,
                                )

                            totals["livestream_rows"] += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            session.commit()
            logger.info(
                "gmvmax livestream metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "result": result,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            return {"synced_rows": int(result.get("livestream_rows", 0) or 0)}

    def _sync_duration_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy, campaign_ids=campaign_ids)
            if not campaigns:
                logger.info(
                    "gmvmax duration metrics sync skipped: no active campaigns",
                    extra={"strategy_id": strategy.id, "workspace_id": strategy.workspace_id},
                )
                return {"synced_rows": 0}

            async def _run() -> dict[str, int]:
                totals: Dict[str, int] = {"duration_rows": 0}
                grouped: Dict[int, List[TTBGmvMaxCampaign]] = {}
                for campaign in campaigns:
                    auth_id = getattr(campaign, "auth_id", None)
                    if auth_id is None:
                        logger.warning(
                            "gmvmax duration metrics sync skipped campaign without auth_id",
                            extra={"campaign_id": getattr(campaign, "id", None)},
                        )
                        continue
                    grouped.setdefault(int(auth_id), []).append(campaign)

                for auth_id, auth_campaigns in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for campaign in auth_campaigns:
                            advertiser_id = strategy.advertiser_id or getattr(campaign, "advertiser_id", None)
                            if not advertiser_id:
                                logger.warning(
                                    "gmvmax duration metrics sync skipped: missing advertiser_id",
                                    extra={"campaign_id": getattr(campaign, "id", None)},
                                )
                                continue

                            if granularity.upper() == "DAY":
                                result = await sync_gmvmax_duration_metrics_daily(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                    campaign_ids=campaign_ids,
                                )
                            else:
                                result = await sync_gmvmax_duration_metrics_hourly(
                                    session,
                                    client,
                                    workspace_id=strategy.workspace_id,
                                    auth_id=auth_id,
                                    advertiser_id=str(advertiser_id),
                                    campaign=campaign,
                                    start_date=start_date,
                                    end_date=end_date,
                                    campaign_ids=campaign_ids,
                                )

                            totals["duration_rows"] += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            session.commit()
            logger.info(
                "gmvmax duration metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "result": result,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            return {"synced_rows": int(result.get("duration_rows", 0) or 0)}

    def _sync_creative_10min(
        self,
        strategy: MonitoringStrategy,
        now: datetime,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        campaign_ids: Optional[list[str]] = None,
    ) -> dict[str, int]:
        start = start_date or now.date()
        end = end_date or start
        with self._session_factory() as session:
            campaigns = self._select_active_campaigns(session, strategy, campaign_ids=campaign_ids)
            if not campaigns:
                logger.info(
                    "gmvmax creative 10min sync skipped: no active campaigns",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                    },
                )
                return {"synced_rows": 0}

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
                                start_date=start,
                                end_date=end,
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
                    "workspace_id": strategy.workspace_id,
                    "campaign_ids": [
                        getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]
                    ],
                    "rows_written": rows_written,
                    "start_date": start,
                    "end_date": end,
                },
            )

            return {"synced_rows": int(rows_written or 0)}
