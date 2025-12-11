"""Expand GMV overview snapshot unique key and prune old rows.

Revision ID: 0059_gmv_overview_snapshot_range_and_ttl
Revises: 0058_gmv_overview_snapshots_and_cleanup
Create Date: 2025-05-30 00:00:00.000000
"""

from __future__ import annotations

from datetime import date, timedelta

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0059_gmv_overview_snapshot_range_and_ttl"
down_revision = "0058_gmv_overview_snapshots_and_cleanup"
branch_labels = None
depends_on = None


SNAPSHOT_TABLE = "gmv_overview_snapshots"
OLD_UNIQUE_NAME = "uk_gmv_overview_snapshot_scope"
NEW_UNIQUE_NAME = "uk_gmv_overview_snapshots_range"
DEFAULT_TTL_DAYS = 90


def _drop_unique_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    inspector = inspect(op.get_bind())
    existing = {uc["name"] for uc in inspector.get_unique_constraints(table_name)}
    if constraint_name in existing:
        op.drop_constraint(constraint_name, table_name, type_="unique")


def _create_unique_constraint_if_missing(
    table_name: str, constraint_name: str, columns: list[str]
) -> None:
    inspector = inspect(op.get_bind())
    existing = {uc["name"] for uc in inspector.get_unique_constraints(table_name)}
    if constraint_name not in existing:
        op.create_unique_constraint(constraint_name, table_name, columns)


def _cleanup_historical_rows(ttl_days: int) -> None:
    cutoff = date.today() - timedelta(days=int(ttl_days))
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM gmv_overview_snapshots WHERE end_date < :cutoff"
        ),
        {"cutoff": cutoff},
    )


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if SNAPSHOT_TABLE not in set(inspector.get_table_names()):
        return

    _drop_unique_constraint_if_exists(SNAPSHOT_TABLE, OLD_UNIQUE_NAME)
    _drop_unique_constraint_if_exists(SNAPSHOT_TABLE, NEW_UNIQUE_NAME)

    _create_unique_constraint_if_missing(
        SNAPSHOT_TABLE,
        NEW_UNIQUE_NAME,
        [
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "snapshot_type",
            "start_date",
            "end_date",
        ],
    )

    _cleanup_historical_rows(DEFAULT_TTL_DAYS)


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if SNAPSHOT_TABLE not in set(inspector.get_table_names()):
        return

    _drop_unique_constraint_if_exists(SNAPSHOT_TABLE, NEW_UNIQUE_NAME)
    _create_unique_constraint_if_missing(
        SNAPSHOT_TABLE,
        OLD_UNIQUE_NAME,
        [
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "snapshot_type",
            "end_date",
        ],
    )
