from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.gmv_restructured import GmvMonitoringStrategy

logger = logging.getLogger("gmv.domain.gmvmax.strategy")


@dataclass
class MonitoringStrategy:
    id: int
    workspace_id: int
    auth_id: int
    advertiser_id: str
    store_id: str
    level: str
    interval_minutes: int
    enabled: bool
    promotion_type: str | None = None
    max_campaigns_per_run: int | None = None


class GmvMaxMonitoringStrategyRepository:
    """Persistence layer for GMV Max monitoring strategies."""

    def __init__(self, session_factory: callable = SessionLocal) -> None:  # type: ignore[type-arg]
        self._session_factory = session_factory

    def _row_to_strategy(self, row: GmvMonitoringStrategy) -> MonitoringStrategy:
        return MonitoringStrategy(
            id=int(row.id),
            workspace_id=int(row.workspace_id),
            auth_id=int(row.auth_id),
            advertiser_id=str(row.advertiser_id),
            store_id=str(row.store_id),
            level=str(row.level),
            interval_minutes=int(row.interval_minutes),
            enabled=bool(row.enabled),
            promotion_type=row.promotion_type.value if row.promotion_type else None,
            max_campaigns_per_run=int(row.max_campaigns_per_run)
            if row.max_campaigns_per_run is not None
            else None,
        )

    def _session(self) -> Session:
        return self._session_factory()

    def get_due_strategies(self, now: datetime) -> List[MonitoringStrategy]:
        """Return enabled strategies that are ready to run."""

        with self._session() as session:
            stmt = select(GmvMonitoringStrategy).where(GmvMonitoringStrategy.enabled.is_(True))
            rows = session.execute(stmt).scalars().all()

            due: list[MonitoringStrategy] = []
            for row in rows:
                if not row.last_run_at:
                    due.append(self._row_to_strategy(row))
                    continue

                last_run = row.last_run_at
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                else:
                    last_run = last_run.astimezone(timezone.utc)

                elapsed = now - last_run
                if elapsed.total_seconds() >= (row.interval_minutes * 60):
                    due.append(self._row_to_strategy(row))

            return due

    def get_by_id(self, strategy_id: int) -> Optional[MonitoringStrategy]:
        with self._session() as session:
            row = session.get(GmvMonitoringStrategy, int(strategy_id))
            return self._row_to_strategy(row) if row else None

    def mark_started(self, strategy_id: int, now: datetime) -> None:
        with self._session() as session:
            session.execute(
                update(GmvMonitoringStrategy)
                .where(GmvMonitoringStrategy.id == int(strategy_id))
                .values(last_run_at=now)
            )
            session.commit()

    def mark_success(self, strategy_id: int, now: datetime) -> None:
        with self._session() as session:
            session.execute(
                update(GmvMonitoringStrategy)
                .where(GmvMonitoringStrategy.id == int(strategy_id))
                .values(last_success_at=now, last_run_at=now, last_error=None)
            )
            session.commit()

    def mark_error(self, strategy_id: int, now: datetime, error_msg: str) -> None:
        with self._session() as session:
            session.execute(
                update(GmvMonitoringStrategy)
                .where(GmvMonitoringStrategy.id == int(strategy_id))
                .values(last_error_at=now, last_error=error_msg, last_run_at=now)
            )
            session.commit()
