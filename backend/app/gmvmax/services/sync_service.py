from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.gmv_restructured import (
    GmvMonitoringStrategy,
    GmvOverviewMetricsDaily,
    GmvOverviewMetricsHourly,
    PromotionTypeEnum,
)
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxLiveCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
    GmvmaxProductCampaignCatalog,
)
from app.data.models.gmvmax_creative_metrics import GmvmaxProductCreativeMetricsDaily
from app.data.models.gmvmax_sync_state import GmvSyncSelectionCursor
from app.data.models.ttb_entities import TTBAdvertiser, TTBAdvertiserStoreLink
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign
from app.gmvmax.domain.monitoring_strategy import MonitoringStrategy
from app.gmvmax.services.campaign_report_sync import (
    SyncIdentifiers,
    sync_campaign_metrics as sync_catalog_campaign_metrics,
)
from app.gmvmax.services.creative_report_sync import sync_product_creative_metrics
from app.services.ttb_client_factory import build_ttb_gmvmax_client
from app.services.ttb_api import TTBHttpError, TTBRateLimitBudgetError
from app.services.ttb_gmvmax import (
    fetch_overview_summary_rows,
    normalize_overview_metrics,
    sync_gmvmax_duration_metrics_daily,
    sync_gmvmax_duration_metrics_hourly,
    sync_gmvmax_livestream_metrics_daily,
    sync_gmvmax_livestream_metrics_hourly,
    sync_gmvmax_overview_metrics,
    sync_gmvmax_product_metrics_daily,
    sync_gmvmax_product_metrics_hourly,
    upsert_overview_snapshot,
)

logger = logging.getLogger("gmv.services.gmvmax.sync")


_ACTIVE_LIFECYCLE_STATUSES = {"ACTIVE"}
_HISTORICAL_LIFECYCLE_STATUSES = {"ACTIVE", "DISABLED", "ENDED"}


def _selection_scope_key(prefix: str, campaign_ids: Optional[list[str]]) -> str:
    normalized = sorted(_dedupe_strings(list(campaign_ids or [])))
    if not normalized:
        return f"{prefix}:all"
    digest = hashlib.sha256("\x00".join(normalized).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:ids:{digest}"


def _strategy_cursor_rows(
    session: Session,
    strategy: MonitoringStrategy,
    cursor_key: str,
) -> tuple[GmvMonitoringStrategy, GmvSyncSelectionCursor | None] | None:
    if int(strategy.id or 0) <= 0:
        return None
    strategy_row = session.scalar(
        select(GmvMonitoringStrategy)
        .where(GmvMonitoringStrategy.id == int(strategy.id))
        .with_for_update()
    )
    if strategy_row is None or int(strategy_row.workspace_id) != int(
        strategy.workspace_id
    ):
        return None
    cursor_row = session.scalar(
        select(GmvSyncSelectionCursor).where(
            GmvSyncSelectionCursor.strategy_id == int(strategy.id),
            GmvSyncSelectionCursor.cursor_key == str(cursor_key),
        )
    )
    return strategy_row, cursor_row


def _selection_cursor_state(
    row: GmvSyncSelectionCursor | None,
) -> dict[str, int]:
    if row is None:
        return {}
    return {
        "high_water_id": max(0, int(row.high_water_id or 0)),
        "last_id": max(0, int(row.last_id or 0)),
    }


def _persist_selection_cursor(
    session: Session,
    row: GmvSyncSelectionCursor | None,
    *,
    strategy_id: int,
    cursor_key: str,
    high_water_id: int,
    last_id: int,
) -> None:
    if row is None:
        row = GmvSyncSelectionCursor(
            strategy_id=int(strategy_id),
            cursor_key=str(cursor_key),
        )
    row.high_water_id = max(0, int(high_water_id))
    row.last_id = max(0, int(last_id))
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row)


def _max_candidate_id(session: Session, stmt: Any, id_column: Any) -> int:
    candidate_ids = (
        stmt.with_only_columns(id_column.label("candidate_id"))
        .order_by(None)
        .subquery()
    )
    return int(session.scalar(select(func.max(candidate_ids.c.candidate_id))) or 0)


def _select_persistently_fair_batch(
    session: Session,
    *,
    stmt: Any,
    id_column: Any,
    strategy: MonitoringStrategy,
    cursor_key: str,
    limit: int,
    execution_guard: Callable[[Session], None] | None = None,
) -> list[Any]:
    """Select a capped batch without starving the set that existed at cycle start.

    A high-water mark freezes each round. Newly inserted rows wait for the next
    round instead of keeping the cursor forever above older rows.
    """

    bounded_limit = max(1, int(limit))
    cursor_rows = _strategy_cursor_rows(session, strategy, cursor_key)
    if cursor_rows is None:
        return list(
            session.scalars(
                stmt.order_by(id_column.desc()).limit(bounded_limit)
            ).all()
        )
    _, cursor_row = cursor_rows

    current_max = _max_candidate_id(session, stmt, id_column)
    if current_max <= 0:
        return []

    state = _selection_cursor_state(cursor_row)
    high_water_id = int(state.get("high_water_id") or 0)
    last_id = int(state.get("last_id") or 0)
    if high_water_id <= 0 or last_id > high_water_id + 1:
        high_water_id = current_max
        last_id = high_water_id + 1

    selected = list(
        session.scalars(
            stmt.where(
                id_column <= high_water_id,
                id_column < last_id,
            )
            .order_by(id_column.desc())
            .limit(bounded_limit)
        ).all()
    )

    if len(selected) < bounded_limit:
        # The frozen round is exhausted. Start the next round at the current
        # maximum, and use any spare capacity without duplicating this batch.
        high_water_id = current_max
        remaining = bounded_limit - len(selected)
        selected_ids = [int(getattr(item, "id")) for item in selected]
        refill_stmt = stmt.where(id_column <= high_water_id)
        if selected_ids:
            refill_stmt = refill_stmt.where(id_column.notin_(selected_ids))
        refill = list(
            session.scalars(
                refill_stmt.order_by(id_column.desc()).limit(remaining)
            ).all()
        )
        selected.extend(refill)
        last_id = int(getattr(refill[-1], "id")) if refill else 0
    elif selected:
        last_id = int(getattr(selected[-1], "id"))

    _persist_selection_cursor(
        session,
        cursor_row,
        strategy_id=int(strategy.id),
        cursor_key=cursor_key,
        high_water_id=high_water_id,
        last_id=last_id,
    )
    # Persist the selection claim before upstream work. A failed/zero-row
    # campaign must not roll the cursor back and monopolize the next run.
    if execution_guard is not None:
        execution_guard(session)
    session.commit()
    return selected


def _as_advertiser_local_datetime(now: datetime, timezone_name: str | None) -> datetime:
    """Convert scheduler UTC time to the advertiser's reporting timezone."""
    aware_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if not timezone_name:
        return aware_utc
    try:
        return aware_utc.astimezone(ZoneInfo(str(timezone_name)))
    except (ZoneInfoNotFoundError, ValueError):
        return aware_utc


def _advertiser_timezone_for_scope(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str | None:
    row = session.execute(
        select(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .where(TTBAdvertiser.workspace_id == int(workspace_id))
        .where(TTBAdvertiser.auth_id == int(auth_id))
        .where(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return str(row.display_timezone or row.timezone or "").strip() or None


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _extract_item_group_ids(raw: Any) -> list[str]:
    ids: list[str] = []

    def _walk(value: Any, key_hint: str | None = None) -> None:
        if value is None:
            return
        if isinstance(value, (str, int)):
            if key_hint in {"item_group_id", "item_group_ids", "item_id", "item_ids"}:
                ids.append(str(value))
            return
        if isinstance(value, list):
            for item in value:
                _walk(item, key_hint=key_hint)
            return
        if not isinstance(value, Mapping):
            return

        for key, child in value.items():
            key_text = str(key)
            if key_text in {"item_group_id", "item_group_ids", "item_id", "item_ids"}:
                _walk(child, key_hint=key_text)
                continue
            if key_text in {"item_group", "item_groups", "item_group_list", "item_list"}:
                _walk(child, key_hint="item_group_id")
                continue
            _walk(child, key_hint=None)

    _walk(raw)
    return _dedupe_strings(ids)


def _backfill_campaign_item_groups_from_catalog(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    campaign_ids: list[str],
) -> list[str]:
    if not campaign_ids:
        return []

    rows = (
        session.execute(
            select(
                GmvmaxProductCampaignCatalog.campaign_id,
                GmvmaxProductCampaignCatalog.detail_raw_json,
            )
            .where(GmvmaxProductCampaignCatalog.workspace_id == int(workspace_id))
            .where(GmvmaxProductCampaignCatalog.auth_id == int(auth_id))
            .where(GmvmaxProductCampaignCatalog.advertiser_id == str(advertiser_id))
            .where(GmvmaxProductCampaignCatalog.store_id == str(store_id))
            .where(GmvmaxProductCampaignCatalog.campaign_id.in_(campaign_ids))
        )
        .all()
    )
    if not rows:
        return []

    existing = set(
        session.execute(
            select(
                GmvmaxProductCampaignItemGroup.campaign_id,
                GmvmaxProductCampaignItemGroup.item_group_id,
            )
            .where(GmvmaxProductCampaignItemGroup.workspace_id == int(workspace_id))
            .where(GmvmaxProductCampaignItemGroup.auth_id == int(auth_id))
            .where(GmvmaxProductCampaignItemGroup.advertiser_id == str(advertiser_id))
            .where(GmvmaxProductCampaignItemGroup.store_id == str(store_id))
            .where(GmvmaxProductCampaignItemGroup.campaign_id.in_(campaign_ids))
        ).all()
    )

    resolved: list[str] = []
    inserted = 0
    for campaign_id, raw_json in rows:
        for item_group_id in _extract_item_group_ids(raw_json):
            key = (str(campaign_id), str(item_group_id))
            resolved.append(str(item_group_id))
            if key in existing:
                continue
            session.add(
                GmvmaxProductCampaignItemGroup(
                    workspace_id=int(workspace_id),
                    auth_id=int(auth_id),
                    advertiser_id=str(advertiser_id),
                    store_id=str(store_id),
                    campaign_id=str(campaign_id),
                    item_group_id=str(item_group_id),
                )
            )
            existing.add(key)
            inserted += 1

    if inserted:
        session.flush()
        logger.info(
            "gmvmax campaign item groups backfilled from catalog detail",
            extra={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "advertiser_id": advertiser_id,
                "store_id": store_id,
                "campaign_count": len(campaign_ids),
                "inserted": inserted,
            },
        )
    return _dedupe_strings(resolved)


def _report_date_windows(start_date: date, end_date: date, granularity: str) -> list[tuple[date, date]]:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    max_days = 1 if granularity.upper() == "HOUR" else 30
    windows: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        window_end = min(end_date, current + timedelta(days=max_days - 1))
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


def _catalog_promotion_types_for_account(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
) -> list[str]:
    promotions: list[str] = []
    product_exists = (
        session.execute(
            select(GmvmaxProductCampaignCatalog.id)
            .where(GmvmaxProductCampaignCatalog.workspace_id == int(workspace_id))
            .where(GmvmaxProductCampaignCatalog.auth_id == int(auth_id))
            .where(GmvmaxProductCampaignCatalog.advertiser_id == str(advertiser_id))
            .where(GmvmaxProductCampaignCatalog.store_id == str(store_id))
            .limit(1)
        ).first()
        is not None
    )
    if product_exists:
        promotions.append("PRODUCT")

    live_exists = (
        session.execute(
            select(GmvmaxLiveCampaignCatalog.id)
            .where(GmvmaxLiveCampaignCatalog.workspace_id == int(workspace_id))
            .where(GmvmaxLiveCampaignCatalog.auth_id == int(auth_id))
            .where(GmvmaxLiveCampaignCatalog.advertiser_id == str(advertiser_id))
            .where(GmvmaxLiveCampaignCatalog.store_id == str(store_id))
            .limit(1)
        ).first()
        is not None
    )
    if live_exists:
        promotions.append("LIVE")

    return promotions or ["PRODUCT"]


class GmvMaxSyncService:
    """Dispatch GMV Max sync work according to monitoring strategies."""

    def __init__(
        self,
        session_factory: callable = SessionLocal,  # type: ignore[type-arg]
        *,
        execution_guard: Callable[[Session], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._execution_guard = execution_guard

    def _assert_execution_guard(self, session: Session) -> None:
        if self._execution_guard is not None:
            self._execution_guard(session)

    def sync_strategy(self, strategy: MonitoringStrategy, now: datetime) -> None:
        handlers = {
            "OVERVIEW_DAILY": self._sync_overview_daily,
            "OVERVIEW_HOURLY": self._sync_overview_hourly,
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

        if not strategy.advertiser_id or not strategy.store_id:
            with self._session_factory() as session:
                accounts = self._select_overview_accounts(session, strategy)
            if len(accounts) > 1:
                for account in accounts:
                    scoped_strategy = replace(
                        strategy,
                        auth_id=int(account["auth_id"]),
                        advertiser_id=str(account["advertiser_id"]),
                        store_id=str(account["store_id"]),
                    )
                    handler(
                        scoped_strategy,
                        self._advertiser_local_now(scoped_strategy, now),
                    )
                return

        handler(strategy, self._advertiser_local_now(strategy, now))

    def _advertiser_local_now(
        self,
        strategy: MonitoringStrategy,
        now: datetime,
    ) -> datetime:
        with self._session_factory() as session:
            statement = (
                select(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
                .where(TTBAdvertiser.workspace_id == int(strategy.workspace_id))
                .order_by(TTBAdvertiser.last_seen_at.desc())
                .limit(1)
            )
            if strategy.auth_id is not None:
                statement = statement.where(TTBAdvertiser.auth_id == int(strategy.auth_id))
            if strategy.advertiser_id:
                statement = statement.where(
                    TTBAdvertiser.advertiser_id == str(strategy.advertiser_id)
                )
            row = session.execute(statement).first()

        timezone_name = None
        if row is not None:
            timezone_name = row.display_timezone or row.timezone
        localized = _as_advertiser_local_datetime(now, timezone_name)
        if timezone_name and str(getattr(localized.tzinfo, "key", "")) != str(timezone_name):
            logger.warning(
                "gmvmax sync ignored invalid advertiser timezone",
                extra={
                    "strategy_id": strategy.id,
                    "advertiser_id": strategy.advertiser_id,
                    "timezone_name": timezone_name,
                },
            )
        return localized

    def sync_levels_for_account(
        self,
        *,
        workspace_id: int,
        auth_id: int,
        advertiser_id: str | None = None,
        store_id: str | None = None,
        levels: list[str],
        start_date: date,
        end_date: date,
        campaign_ids: Optional[list[str]] = None,
        item_group_ids: Optional[list[str]] = None,
        require_active_campaigns: bool = True,
        refresh_creative_assets: bool = False,
        backfill_missing_creative_assets: bool = False,
    ) -> dict[str, dict[str, int]]:
        """Manually trigger GMV Max sync for specific levels scoped to an auth account."""

        now = datetime.now(timezone.utc)
        strategy = MonitoringStrategy(
            id=0,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            level="CAMPAIGN_HOURLY",
            category="GMVMAX",
            task_name="gmvmax.manual_sync",
            interval_minutes=0,
            enabled=True,
            promotion_type=None,
            # A manual request is an explicit completeness operation. The old
            # hidden 200-row cap had no persistent strategy cursor and silently
            # omitted the rest of large accounts.
            max_campaigns_per_run=None,
            params_json={},
            input_schema_json=None,
        )

        def _merge_day_hour(
            daily: Mapping[str, Any],
            hourly: Mapping[str, Any],
        ) -> dict[str, int]:
            daily_rows = int(daily.get("synced_rows", 0) or 0)
            hourly_rows = int(hourly.get("synced_rows", 0) or 0)
            return {
                "synced_rows": daily_rows + hourly_rows,
                "daily_rows": daily_rows,
                "hourly_rows": hourly_rows,
            }

        def _sync_campaign_pair() -> dict[str, int]:
            common = {
                "start_date": start_date,
                "end_date": end_date,
                "campaign_ids": campaign_ids,
                "require_active_campaigns": require_active_campaigns,
            }
            return _merge_day_hour(
                self._sync_campaign_metrics(
                    strategy,
                    granularity="DAY",
                    **common,
                ),
                self._sync_campaign_metrics(
                    strategy,
                    granularity="HOUR",
                    **common,
                ),
            )

        def _sync_product_pair() -> dict[str, int]:
            common = {
                "start_date": start_date,
                "end_date": end_date,
                "campaign_ids": campaign_ids,
                "require_active_campaigns": require_active_campaigns,
            }
            return _merge_day_hour(
                self._sync_product_metrics(
                    strategy,
                    granularity="DAY",
                    **common,
                ),
                self._sync_product_metrics(
                    strategy,
                    granularity="HOUR",
                    **common,
                ),
            )

        def _sync_livestream_pair() -> dict[str, int]:
            common = {
                "start_date": start_date,
                "end_date": end_date,
                "campaign_ids": campaign_ids,
                "require_active_campaigns": require_active_campaigns,
            }
            return _merge_day_hour(
                self._sync_livestream_metrics(
                    strategy,
                    granularity="DAY",
                    **common,
                ),
                self._sync_livestream_metrics(
                    strategy,
                    granularity="HOUR",
                    **common,
                ),
            )

        def _sync_duration_pair() -> dict[str, int]:
            common = {
                "start_date": start_date,
                "end_date": end_date,
                "campaign_ids": campaign_ids,
                "require_active_campaigns": require_active_campaigns,
            }
            return _merge_day_hour(
                self._sync_duration_metrics(
                    strategy,
                    granularity="DAY",
                    **common,
                ),
                self._sync_duration_metrics(
                    strategy,
                    granularity="HOUR",
                    **common,
                ),
            )

        def _sync_overview_pair() -> dict[str, int]:
            snapshot = self._sync_overview_manual(
                strategy,
                start_date=start_date,
                end_date=end_date,
            )
            result = _merge_day_hour(
                self._sync_overview_metrics(
                    strategy,
                    start_date=start_date,
                    end_date=end_date,
                    granularity="DAY",
                ),
                self._sync_overview_metrics(
                    strategy,
                    start_date=start_date,
                    end_date=end_date,
                    granularity="HOUR",
                ),
            )
            snapshot_rows = int(snapshot.get("synced_rows", 0) or 0)
            result["snapshot_rows"] = snapshot_rows
            result["synced_rows"] += snapshot_rows
            return result

        handlers: dict[str, Callable[[], dict[str, int]]] = {
            "OVERVIEW": _sync_overview_pair,
            "CAMPAIGN": _sync_campaign_pair,
            "PRODUCT": _sync_product_pair,
            "CREATIVE": lambda: self._sync_creative_10min(
                strategy,
                now,
                start_date=start_date,
                end_date=end_date,
                campaign_ids=campaign_ids,
                item_group_ids=item_group_ids,
                refresh_creative_assets=bool(refresh_creative_assets),
                backfill_missing_creative_assets=bool(backfill_missing_creative_assets),
            ),
            "LIVESTREAM": _sync_livestream_pair,
            "DURATION": _sync_duration_pair,
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
        start = now.date() - timedelta(days=1)
        self._sync_campaign_metrics(strategy, start_date=start, end_date=now.date(), granularity="HOUR")

    def _sync_campaign_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_campaign_metrics(strategy, start_date=start, end_date=now.date(), granularity="DAY")

    def _sync_product_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=1)
        self._sync_product_metrics(strategy, start_date=start, end_date=now.date(), granularity="HOUR")

    def _sync_product_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_product_metrics(strategy, start_date=start, end_date=now.date(), granularity="DAY")

    def _sync_livestream_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=1)
        self._sync_livestream_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="HOUR"
        )

    def _sync_livestream_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_livestream_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="DAY"
        )

    def _sync_duration_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=1)
        self._sync_duration_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="HOUR"
        )

    def _sync_duration_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        start = now.date() - timedelta(days=2)
        self._sync_duration_metrics(
            strategy, start_date=start, end_date=now.date(), granularity="DAY"
        )

    def _sync_overview_daily(self, strategy: MonitoringStrategy, now: datetime) -> None:
        target = now.date() - timedelta(days=1)
        self._sync_overview_metrics(
            strategy,
            start_date=target,
            end_date=target,
            granularity="DAILY",
        )
        self._sync_overview_manual(
            strategy,
            start_date=target,
            end_date=target,
            snapshot_type="SCHEDULED",
        )

    def _sync_overview_hourly(self, strategy: MonitoringStrategy, now: datetime) -> None:
        current_hour_start = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        target_start = current_hour_start - timedelta(hours=1)
        target_end = current_hour_start - timedelta(microseconds=1)
        self._sync_overview_metrics(
            strategy,
            start_date=target_start.date(),
            end_date=target_end.date(),
            granularity="HOURLY",
            hour_window=(target_start, target_end),
        )
        today = now.date()
        self._sync_overview_manual(
            strategy,
            start_date=today,
            end_date=today,
            snapshot_type="SCHEDULED",
        )

    def _select_overview_accounts(
        self, session: Session, strategy: MonitoringStrategy
    ) -> list[dict[str, str | int]]:
        deduped: dict[tuple[int, str, str], dict[str, str | int]] = {}

        def add_account(auth_id: int | None, advertiser_id: str | None, store_id: str | None) -> None:
            if not (auth_id and advertiser_id and store_id):
                return
            key = (int(auth_id), str(advertiser_id), str(store_id))
            deduped.setdefault(
                key,
                {
                    "auth_id": int(auth_id),
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(store_id),
                },
            )

        link_stmt = (
            select(
                TTBAdvertiserStoreLink.auth_id,
                TTBAdvertiserStoreLink.advertiser_id,
                TTBAdvertiserStoreLink.store_id,
            )
            .where(TTBAdvertiserStoreLink.workspace_id == int(strategy.workspace_id))
            .order_by(TTBAdvertiserStoreLink.last_seen_at.desc())
        )

        if strategy.auth_id is not None:
            link_stmt = link_stmt.where(TTBAdvertiserStoreLink.auth_id == int(strategy.auth_id))
        if strategy.advertiser_id:
            link_stmt = link_stmt.where(
                TTBAdvertiserStoreLink.advertiser_id == str(strategy.advertiser_id)
            )
        if strategy.store_id:
            link_stmt = link_stmt.where(TTBAdvertiserStoreLink.store_id == str(strategy.store_id))

        for auth_id, advertiser_id, store_id in session.execute(link_stmt).all():
            add_account(auth_id, advertiser_id, store_id)

        for catalog_model in (GmvmaxProductCampaignCatalog, GmvmaxLiveCampaignCatalog):
            catalog_stmt = (
                select(
                    catalog_model.auth_id,
                    catalog_model.advertiser_id,
                    catalog_model.store_id,
                )
                .where(catalog_model.workspace_id == int(strategy.workspace_id))
                .distinct()
            )
            if strategy.auth_id is not None:
                catalog_stmt = catalog_stmt.where(catalog_model.auth_id == int(strategy.auth_id))
            if strategy.advertiser_id:
                catalog_stmt = catalog_stmt.where(catalog_model.advertiser_id == str(strategy.advertiser_id))
            if strategy.store_id:
                catalog_stmt = catalog_stmt.where(catalog_model.store_id == str(strategy.store_id))
            for auth_id, advertiser_id, store_id in session.execute(catalog_stmt).all():
                add_account(auth_id, advertiser_id, store_id)

        if not deduped and all(
            [strategy.auth_id is not None, strategy.advertiser_id, strategy.store_id]
        ):
            add_account(int(strategy.auth_id), str(strategy.advertiser_id), str(strategy.store_id))

        return list(deduped.values())

    def _select_campaigns_by_status(
        self,
        session: Session,
        strategy: MonitoringStrategy,
        *,
        campaign_ids: Optional[list[str]] = None,
        lifecycle_statuses: set[str],
    ) -> list[TTBGmvMaxCampaign]:
        stmt = (
            select(TTBGmvMaxCampaign)
            .where(TTBGmvMaxCampaign.workspace_id == strategy.workspace_id)
            .where(TTBGmvMaxCampaign.is_deleted.is_(False))
            .where(TTBGmvMaxCampaign.lifecycle_status.in_(lifecycle_statuses))
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

        # 可选过滤：按 auth_id / advertiser_id / store_id 进一步缩小范围
        if strategy.auth_id is not None:
            stmt = stmt.where(TTBGmvMaxCampaign.auth_id == strategy.auth_id)
        if strategy.advertiser_id:
            stmt = stmt.where(
                TTBGmvMaxCampaign.advertiser_id == str(strategy.advertiser_id)
            )
        if strategy.store_id:
            stmt = stmt.where(TTBGmvMaxCampaign.store_id == strategy.store_id)
        if campaign_ids:
            stmt = stmt.where(TTBGmvMaxCampaign.campaign_id.in_(campaign_ids))

        limit_value: int | None = None
        if strategy.max_campaigns_per_run is not None:
            try:
                candidate = int(strategy.max_campaigns_per_run)
                if candidate > 0:
                    limit_value = candidate
            except (TypeError, ValueError):
                logger.warning(
                    "gmvmax monitoring strategy has invalid max_campaigns_per_run",
                    extra={"strategy_id": strategy.id, "max_campaigns_per_run": strategy.max_campaigns_per_run},
                )

        if limit_value is not None:
            lifecycle_key = ",".join(sorted(str(value) for value in lifecycle_statuses))
            return _select_persistently_fair_batch(
                session,
                stmt=stmt,
                id_column=TTBGmvMaxCampaign.id,
                strategy=strategy,
                cursor_key=_selection_scope_key(
                    f"campaign:{lifecycle_key}",
                    campaign_ids,
                ),
                limit=limit_value,
                execution_guard=self._assert_execution_guard,
            )
        else:
            logger.info(
                "gmvmax active campaign query without limit",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "task_name": strategy.task_name,
                    "campaign_filter_ids": campaign_ids,
                },
            )

        return list(
            session.execute(
                stmt.order_by(
                    TTBGmvMaxCampaign.ext_updated_time.desc(),
                    TTBGmvMaxCampaign.id.desc(),
                )
            ).scalars().all()
        )

    def _select_active_campaigns(
        self,
        session: Session,
        strategy: MonitoringStrategy,
        *,
        campaign_ids: Optional[list[str]] = None,
    ) -> list[TTBGmvMaxCampaign]:
        return self._select_campaigns_by_status(
            session,
            strategy,
            campaign_ids=campaign_ids,
            lifecycle_statuses=_ACTIVE_LIFECYCLE_STATUSES,
        )

    def _select_campaigns_for_sync(
        self,
        session: Session,
        strategy: MonitoringStrategy,
        *,
        campaign_ids: Optional[list[str]] = None,
        require_active_campaigns: bool = True,
    ) -> list[TTBGmvMaxCampaign]:
        if require_active_campaigns:
            return self._select_active_campaigns(
                session,
                strategy,
                campaign_ids=campaign_ids,
            )

        # Manual completeness syncs must include historical rows even when an
        # active campaign also exists. The former fallback only selected
        # historical campaigns when the active query was empty, silently
        # omitting disabled/ended campaigns from mixed accounts.
        return self._select_campaigns_by_status(
            session,
            strategy,
            campaign_ids=campaign_ids,
            lifecycle_statuses=_HISTORICAL_LIFECYCLE_STATUSES,
        )

    def _select_product_catalog_campaigns(
        self,
        session: Session,
        strategy: MonitoringStrategy,
        *,
        campaign_ids: Optional[list[str]] = None,
        require_active_campaigns: bool = True,
    ) -> list[GmvmaxProductCampaignCatalog]:
        stmt = select(GmvmaxProductCampaignCatalog).where(
            GmvmaxProductCampaignCatalog.workspace_id == int(strategy.workspace_id)
        )
        if strategy.auth_id is not None:
            stmt = stmt.where(
                GmvmaxProductCampaignCatalog.auth_id == int(strategy.auth_id)
            )
        if strategy.advertiser_id:
            stmt = stmt.where(
                GmvmaxProductCampaignCatalog.advertiser_id
                == str(strategy.advertiser_id)
            )
        if strategy.store_id:
            stmt = stmt.where(
                GmvmaxProductCampaignCatalog.store_id == str(strategy.store_id)
            )
        if campaign_ids:
            stmt = stmt.where(
                GmvmaxProductCampaignCatalog.campaign_id.in_(campaign_ids)
            )
        if require_active_campaigns:
            stmt = stmt.where(
                GmvmaxProductCampaignCatalog.operation_status == "ENABLE"
            )

        limit_value: int | None = None
        if strategy.max_campaigns_per_run is not None:
            try:
                candidate = int(strategy.max_campaigns_per_run)
                if candidate > 0:
                    limit_value = candidate
            except (TypeError, ValueError):
                logger.warning(
                    "gmvmax product strategy has invalid max_campaigns_per_run",
                    extra={
                        "strategy_id": strategy.id,
                        "max_campaigns_per_run": strategy.max_campaigns_per_run,
                    },
                )

        if limit_value is not None:
            return _select_persistently_fair_batch(
                session,
                stmt=stmt,
                id_column=GmvmaxProductCampaignCatalog.id,
                strategy=strategy,
                cursor_key=_selection_scope_key(
                    f"product_catalog:active:{int(bool(require_active_campaigns))}",
                    campaign_ids,
                ),
                limit=limit_value,
                execution_guard=self._assert_execution_guard,
            )

        return list(
            session.scalars(
                stmt.order_by(
                    GmvmaxProductCampaignCatalog.updated_at.desc(),
                    GmvmaxProductCampaignCatalog.id.desc(),
                )
            ).all()
        )

    def _sync_campaign_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
        require_active_campaigns: bool = True,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            accounts = self._select_overview_accounts(session, strategy)
            if not accounts:
                logger.info(
                    "gmvmax campaign metrics sync skipped: no advertiser/store bound",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                        "granularity": granularity,
                    },
                )
                return {"synced_rows": 0}

            async def _run() -> dict[str, int]:
                totals: Dict[str, int] = {"campaign_rows": 0, "failed_requests": 0}
                grouped: Dict[int, list[dict[str, str | int]]] = {}
                for account in accounts:
                    grouped.setdefault(int(account["auth_id"]), []).append(account)

                granularity_arg = "DAILY" if granularity.upper() == "DAY" else "HOURLY"
                for auth_id, auth_accounts in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for account in auth_accounts:
                            identifiers = SyncIdentifiers(
                                workspace_id=int(strategy.workspace_id),
                                auth_id=int(auth_id),
                                advertiser_id=str(account["advertiser_id"]),
                                store_id=str(account["store_id"]),
                                advertiser_timezone=_advertiser_timezone_for_scope(
                                    session,
                                    workspace_id=int(strategy.workspace_id),
                                    auth_id=int(auth_id),
                                    advertiser_id=str(account["advertiser_id"]),
                                ),
                            )
                            promotion_types = _catalog_promotion_types_for_account(
                                session,
                                workspace_id=int(strategy.workspace_id),
                                auth_id=int(auth_id),
                                advertiser_id=str(account["advertiser_id"]),
                                store_id=str(account["store_id"]),
                            )
                            for promotion_type in promotion_types:
                                for window_start, window_end in _report_date_windows(start_date, end_date, granularity):
                                    try:
                                        rows_synced = await sync_catalog_campaign_metrics(
                                            session,
                                            client,
                                            identifiers=identifiers,
                                            promotion_type=promotion_type,
                                            granularity=granularity_arg,
                                            start_date=window_start,
                                            end_date=window_end,
                                            campaign_ids=campaign_ids,
                                        )
                                        totals["campaign_rows"] += int(rows_synced or 0)
                                    except (TTBRateLimitBudgetError, TTBHttpError):
                                        raise
                                    except Exception:  # noqa: BLE001
                                        totals["failed_requests"] += 1
                                        logger.warning(
                                            "gmvmax campaign metrics sync skipped promotion type after upstream error",
                                            exc_info=True,
                                            extra={
                                                "strategy_id": strategy.id,
                                                "workspace_id": strategy.workspace_id,
                                                "promotion_type": promotion_type,
                                                "advertiser_id": account["advertiser_id"],
                                                "store_id": account["store_id"],
                                                "start_date": window_start,
                                                "end_date": window_end,
                                            },
                                        )
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            if int(result.get("failed_requests", 0) or 0) > 0:
                raise RuntimeError(
                    f"GMV Max campaign sync had {result['failed_requests']} failed report requests"
                )
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax campaign metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "accounts": len(accounts),
                    "campaign_ids": campaign_ids,
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
        require_active_campaigns: bool = True,
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
            campaigns = self._select_product_catalog_campaigns(
                session,
                strategy,
                campaign_ids=campaign_ids,
                require_active_campaigns=require_active_campaigns,
            )
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
                totals: Dict[str, int] = {
                    "product_rows": 0,
                    "order_event_rows": 0,
                    "failed_requests": 0,
                }
                grouped: Dict[int, List[GmvmaxProductCampaignCatalog]] = {}
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

                            advertiser_timezone = _advertiser_timezone_for_scope(
                                session,
                                workspace_id=int(strategy.workspace_id),
                                auth_id=int(auth_id),
                                advertiser_id=str(advertiser_id),
                            )
                            if granularity.upper() == "DAY":
                                try:
                                    result = await sync_gmvmax_product_metrics_daily(
                                        session,
                                        client,
                                        workspace_id=strategy.workspace_id,
                                        auth_id=auth_id,
                                        advertiser_id=str(advertiser_id),
                                        campaign=campaign,
                                        start_date=start_date,
                                        end_date=end_date,
                                        advertiser_timezone=advertiser_timezone,
                                    )
                                    totals["product_rows"] += int(result.get("synced_rows", 0) or 0)
                                except (TTBRateLimitBudgetError, TTBHttpError):
                                    raise
                                except Exception:  # noqa: BLE001
                                    totals["failed_requests"] += 1
                                    logger.warning(
                                        "gmvmax product metrics sync skipped after upstream error",
                                        exc_info=True,
                                        extra={
                                            "strategy_id": strategy.id,
                                            "workspace_id": strategy.workspace_id,
                                            "advertiser_id": advertiser_id,
                                            "campaign_id": getattr(campaign, "campaign_id", None),
                                            "start_date": start_date,
                                            "end_date": end_date,
                                        },
                                    )
                                continue

                            for window_start, window_end in _report_date_windows(start_date, end_date, granularity):
                                try:
                                    result = await sync_gmvmax_product_metrics_hourly(
                                        session,
                                        client,
                                        workspace_id=strategy.workspace_id,
                                        auth_id=auth_id,
                                        advertiser_id=str(advertiser_id),
                                        campaign=campaign,
                                        start_date=window_start,
                                        end_date=window_end,
                                        advertiser_timezone=advertiser_timezone,
                                    )
                                    totals["product_rows"] += int(result.get("synced_rows", 0) or 0)
                                    totals["order_event_rows"] += int(result.get("order_event_rows", 0) or 0)
                                except (TTBRateLimitBudgetError, TTBHttpError):
                                    raise
                                except Exception:  # noqa: BLE001
                                    totals["failed_requests"] += 1
                                    logger.warning(
                                        "gmvmax product hourly metrics sync skipped after upstream error",
                                        exc_info=True,
                                        extra={
                                            "strategy_id": strategy.id,
                                            "workspace_id": strategy.workspace_id,
                                            "advertiser_id": advertiser_id,
                                            "campaign_id": getattr(campaign, "campaign_id", None),
                                            "start_date": window_start,
                                            "end_date": window_end,
                                        },
                                    )
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            if int(result.get("failed_requests", 0) or 0) > 0:
                raise RuntimeError(
                    f"GMV Max product sync had {result['failed_requests']} failed report requests"
                )
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax product metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "requested_campaign_ids": campaign_ids,
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
        hour_window: tuple[datetime, datetime] | None = None,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            accounts = self._select_overview_accounts(session, strategy)
            if not accounts:
                logger.info(
                    "gmvmax overview sync skipped: no advertiser/store bound",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                        "granularity": granularity,
                    },
                )
                return {"synced_rows": 0}

            granularity_normalized = str(granularity or "").upper()
            granularity_arg = "HOUR" if granularity_normalized in {"HOURLY", "HOUR"} else "DAY"

            async def _run() -> dict[str, int]:
                rows_written = 0
                grouped: Dict[int, list[dict[str, str | int]]] = {}
                for account in accounts:
                    auth_id = int(account["auth_id"])
                    grouped.setdefault(auth_id, []).append(account)

                for auth_id, auth_accounts in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for account in auth_accounts:
                            advertiser_id = str(account["advertiser_id"])
                            store_id = str(account["store_id"])

                            result = await sync_gmvmax_overview_metrics(
                                session,
                                client,
                                workspace_id=strategy.workspace_id,
                                auth_id=auth_id,
                                advertiser_id=advertiser_id,
                                store_ids=[store_id],
                                start_date=start_date,
                                end_date=end_date,
                                granularity=granularity_arg,
                                hour_window=hour_window,
                                advertiser_timezone=_advertiser_timezone_for_scope(
                                    session,
                                    workspace_id=int(strategy.workspace_id),
                                    auth_id=int(auth_id),
                                    advertiser_id=advertiser_id,
                                ),
                            )
                            rows_written += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return {"synced_rows": rows_written}

            result = asyncio.run(_run())
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax overview metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "hour_window": hour_window,
                    "accounts": len(accounts),
                    "rows_written": result.get("synced_rows", 0),
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            return {"synced_rows": int(result.get("synced_rows", 0) or 0)}

    def _sync_overview_manual(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date: date,
        end_date: date,
        snapshot_type: str = "MANUAL",
    ) -> dict[str, int]:
        days_inclusive = (end_date - start_date).days + 1
        if days_inclusive > 365:
            logger.warning(
                "gmvmax overview manual sync aborted: date range too long",
                extra={
                    "workspace_id": strategy.workspace_id,
                    "auth_id": strategy.auth_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "days": days_inclusive,
                },
            )
            return {
                "synced_rows": 0,
                "error": {
                    "code": "OVERVIEW_DATE_RANGE_TOO_LONG",
                    "message": "OVERVIEW date range must not exceed 365 days.",
                },
            }
        with self._session_factory() as session:
            accounts = self._select_overview_accounts(session, strategy)
            if not accounts:
                logger.info(
                    "gmvmax overview manual sync skipped: no advertiser/store bound",
                    extra={
                        "workspace_id": strategy.workspace_id,
                        "auth_id": strategy.auth_id,
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                )
                return {
                    "synced_rows": 0,
                    "error": {
                        "code": "NO_ADVERTISER",
                        "message": "no advertiser/store bound for this account",
                    },
                }

            async def _run() -> int:
                snapshots_written = 0
                grouped: Dict[int, list[dict[str, str | int]]] = {}
                for account in accounts:
                    auth_id = int(account["auth_id"])
                    grouped.setdefault(auth_id, []).append(account)

                for auth_id, auth_accounts in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for account in auth_accounts:
                            advertiser_id = str(account["advertiser_id"])
                            store_id = str(account["store_id"])
                            rows = await fetch_overview_summary_rows(
                                client,
                                advertiser_id=advertiser_id,
                                store_id=store_id,
                                start_date=start_date,
                                end_date=end_date,
                                dimensions=["advertiser_id"],
                            )
                            metrics_rows = [normalize_overview_metrics(row) for row in rows]

                            upsert_overview_snapshot(
                                session,
                                workspace_id=strategy.workspace_id,
                                auth_id=auth_id,
                                advertiser_id=advertiser_id,
                                store_id=store_id,
                                snapshot_type=snapshot_type,
                                start_date=start_date,
                                end_date=end_date,
                                metrics_rows=metrics_rows,
                            )
                            snapshots_written += 1
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return snapshots_written

            snapshots = asyncio.run(_run())
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax overview manual sync done",
                extra={
                    "workspace_id": strategy.workspace_id,
                    "auth_id": strategy.auth_id,
                    "accounts": len(accounts),
                    "start_date": start_date,
                    "end_date": end_date,
                    "snapshot_type": snapshot_type,
                    "snapshots": snapshots,
                },
            )
            return {"synced_rows": snapshots}

    def _sync_livestream_metrics(
        self,
        strategy: MonitoringStrategy,
        *,
        start_date,
        end_date,
        granularity: str,
        campaign_ids: Optional[list[str]] = None,
        require_active_campaigns: bool = True,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            campaigns = self._select_campaigns_for_sync(
                session,
                strategy,
                campaign_ids=campaign_ids,
                require_active_campaigns=require_active_campaigns,
            )
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
                                    campaign_ids=[str(campaign.campaign_id)] if campaign_ids else None,
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
                                    campaign_ids=[str(campaign.campaign_id)] if campaign_ids else None,
                                )

                            totals["livestream_rows"] += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax livestream metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "requested_campaign_ids": campaign_ids,
                    "auth_ids": sorted({int(auth_id) for auth_id in (getattr(c, "auth_id", None) for c in campaigns) if auth_id is not None}),
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
        require_active_campaigns: bool = True,
    ) -> dict[str, int]:
        with self._session_factory() as session:
            campaigns = self._select_campaigns_for_sync(
                session,
                strategy,
                campaign_ids=campaign_ids,
                require_active_campaigns=require_active_campaigns,
            )
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
                                    campaign_ids=[str(campaign.campaign_id)] if campaign_ids else None,
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
                                    campaign_ids=[str(campaign.campaign_id)] if campaign_ids else None,
                                )

                            totals["duration_rows"] += int(result.get("synced_rows", 0) or 0)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                return totals

            result = asyncio.run(_run())
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax duration metrics sync done",
                extra={
                    "strategy_id": strategy.id,
                    "workspace_id": strategy.workspace_id,
                    "granularity": granularity,
                    "campaigns": len(campaigns),
                    "campaign_ids": [getattr(campaign, "campaign_id", None) for campaign in campaigns[:5]],
                    "requested_campaign_ids": campaign_ids,
                    "auth_ids": sorted({int(auth_id) for auth_id in (getattr(c, "auth_id", None) for c in campaigns) if auth_id is not None}),
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
        item_group_ids: Optional[list[str]] = None,
        refresh_creative_assets: bool = False,
        backfill_missing_creative_assets: bool = False,
    ) -> dict[str, int]:
        end = end_date or now.date()
        start = start_date or (end - timedelta(days=1))
        logger.info(
            "gmvmax creative metrics sync target mapping",
            extra={
                "strategy_id": strategy.id,
                "workspace_id": strategy.workspace_id,
                "level": "CREATIVE",
                "tables": ["gmvmax_product_creative_metrics_daily"],
            },
        )
        with self._session_factory() as session:
            accounts = self._select_overview_accounts(session, strategy)
            if not accounts:
                logger.info(
                    "gmvmax creative sync skipped: no advertiser/store bound",
                    extra={
                        "strategy_id": strategy.id,
                        "workspace_id": strategy.workspace_id,
                    },
                )
                return {"synced_rows": 0}

            def _catalog_campaign_ids(account: dict[str, str | int]) -> list[str]:
                requested = [str(item) for item in (campaign_ids or []) if item]
                if requested:
                    return requested
                rows = session.execute(
                    select(GmvmaxProductCampaignCatalog.campaign_id)
                    .where(GmvmaxProductCampaignCatalog.workspace_id == int(strategy.workspace_id))
                    .where(GmvmaxProductCampaignCatalog.auth_id == int(account["auth_id"]))
                    .where(GmvmaxProductCampaignCatalog.advertiser_id == str(account["advertiser_id"]))
                    .where(GmvmaxProductCampaignCatalog.store_id == str(account["store_id"]))
                    .where(GmvmaxProductCampaignCatalog.operation_status == "ENABLE")
                ).scalars()
                return [str(item) for item in rows if item]

            def _item_group_ids(account: dict[str, str | int], account_campaign_ids: list[str]) -> list[str]:
                requested = [str(item) for item in (item_group_ids or []) if item]
                if requested:
                    return requested
                if not account_campaign_ids:
                    return []
                rows = session.execute(
                    select(GmvmaxProductCampaignItemGroup.item_group_id)
                    .where(GmvmaxProductCampaignItemGroup.workspace_id == int(strategy.workspace_id))
                    .where(GmvmaxProductCampaignItemGroup.auth_id == int(account["auth_id"]))
                    .where(GmvmaxProductCampaignItemGroup.advertiser_id == str(account["advertiser_id"]))
                    .where(GmvmaxProductCampaignItemGroup.store_id == str(account["store_id"]))
                    .where(GmvmaxProductCampaignItemGroup.campaign_id.in_(account_campaign_ids))
                ).scalars()
                resolved = _dedupe_strings([str(item) for item in rows if item])
                if resolved:
                    return resolved

                catalog_resolved = _backfill_campaign_item_groups_from_catalog(
                    session,
                    workspace_id=int(strategy.workspace_id),
                    auth_id=int(account["auth_id"]),
                    advertiser_id=str(account["advertiser_id"]),
                    store_id=str(account["store_id"]),
                    campaign_ids=account_campaign_ids,
                )
                if catalog_resolved:
                    return catalog_resolved

                metric_rows = session.execute(
                    select(GmvmaxProductCreativeMetricsDaily.item_group_id)
                    .where(GmvmaxProductCreativeMetricsDaily.workspace_id == int(strategy.workspace_id))
                    .where(GmvmaxProductCreativeMetricsDaily.auth_id == int(account["auth_id"]))
                    .where(GmvmaxProductCreativeMetricsDaily.advertiser_id == str(account["advertiser_id"]))
                    .where(GmvmaxProductCreativeMetricsDaily.store_id == str(account["store_id"]))
                    .where(GmvmaxProductCreativeMetricsDaily.campaign_id.in_(account_campaign_ids))
                    .distinct()
                ).scalars()
                return _dedupe_strings([str(item) for item in metric_rows if item])

            async def _run() -> int:
                written = 0
                failed_windows = 0
                attempted_windows = 0

                grouped: Dict[int, list[dict[str, str | int]]] = {}
                for account in accounts:
                    grouped.setdefault(int(account["auth_id"]), []).append(account)

                for auth_id, auth_accounts in grouped.items():
                    client = build_ttb_gmvmax_client(session, auth_id=auth_id)
                    try:
                        for account in auth_accounts:
                            account_campaign_ids = _catalog_campaign_ids(account)
                            account_item_group_ids = _item_group_ids(account, account_campaign_ids)
                            if not account_campaign_ids or not account_item_group_ids:
                                logger.info(
                                    "gmvmax creative sync skipped: missing campaign or item group filters",
                                    extra={
                                        "workspace_id": strategy.workspace_id,
                                        "auth_id": auth_id,
                                        "campaign_ids": account_campaign_ids[:5],
                                        "item_group_ids": account_item_group_ids[:5],
                                    },
                                )
                                continue
                            identifiers = SyncIdentifiers(
                                workspace_id=int(strategy.workspace_id),
                                auth_id=auth_id,
                                advertiser_id=str(account["advertiser_id"]),
                                store_id=str(account["store_id"]),
                                advertiser_timezone=_advertiser_timezone_for_scope(
                                    session,
                                    workspace_id=int(strategy.workspace_id),
                                    auth_id=int(auth_id),
                                    advertiser_id=str(account["advertiser_id"]),
                                ),
                            )
                            current = start
                            while current <= end:
                                attempted_windows += 1
                                try:
                                    rows = await sync_product_creative_metrics(
                                        session,
                                        client,
                                        identifiers=identifiers,
                                        campaign_ids=account_campaign_ids,
                                        item_group_ids=account_item_group_ids,
                                        start_date=current,
                                        end_date=current,
                                        include_current_statuses=(current == end),
                                        refresh_creative_assets=bool(refresh_creative_assets),
                                        backfill_missing_creative_assets=bool(
                                            backfill_missing_creative_assets and current == end
                                        ),
                                    )
                                    written += int(rows or 0)
                                except (TTBRateLimitBudgetError, TTBHttpError):
                                    raise
                                except Exception:  # noqa: BLE001
                                    failed_windows += 1
                                    logger.warning(
                                        "gmvmax creative metrics sync skipped date after upstream error",
                                        exc_info=True,
                                        extra={
                                            "strategy_id": strategy.id,
                                            "workspace_id": strategy.workspace_id,
                                            "auth_id": auth_id,
                                            "advertiser_id": account["advertiser_id"],
                                            "store_id": account["store_id"],
                                            "campaign_ids": account_campaign_ids[:5],
                                            "item_group_ids": account_item_group_ids[:5],
                                            "date": current.isoformat(),
                                        },
                                    )
                                current = current + timedelta(days=1)
                    finally:
                        try:
                            await client.aclose()
                        except Exception:  # noqa: BLE001
                            logger.warning("gmvmax client close failed", exc_info=True)

                if failed_windows:
                    logger.warning(
                        "gmvmax creative sync completed with failed windows",
                        extra={
                            "strategy_id": strategy.id,
                            "workspace_id": strategy.workspace_id,
                            "failed_windows": failed_windows,
                            "written": written,
                        },
                    )
                    raise RuntimeError(
                        f"GMV Max creative sync failed {failed_windows} of {attempted_windows} report windows"
                    )
                return written

            rows_written = asyncio.run(_run())
            self._assert_execution_guard(session)
            session.commit()
            logger.info(
                "gmvmax creative sync done",
                extra={
                    "strategy_id": strategy.id,
                    "accounts": len(accounts),
                    "workspace_id": strategy.workspace_id,
                    "campaign_ids": campaign_ids,
                    "item_group_ids": item_group_ids,
                    "rows_written": rows_written,
                    "start_date": start,
                    "end_date": end,
                },
            )

            return {"synced_rows": int(rows_written or 0)}
