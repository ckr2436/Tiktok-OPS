"""Refresh GMV overview snapshot unique index with date range.

Revision ID: 0060_refresh_gmv_overview_snapshot_unique
Revises: 0059_gmv_overview_snapshot_range_and_ttl
Create Date: 2025-06-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "0060_refresh_gmv_overview_snapshot_unique"
down_revision = "0059_gmv_overview_snapshot_range_and_ttl"
branch_labels = None
depends_on = None


TABLE_NAME = "gmv_overview_snapshots"
OLD_UNIQUE_NAME = "uk_gmv_overview_snapshot_scope"
NEW_UNIQUE_NAME = "uk_gmv_overview_snapshots_range"
UNIQUE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "snapshot_type",
    "start_date",
    "end_date",
]


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    if OLD_UNIQUE_NAME in existing_indexes:
        op.drop_index(OLD_UNIQUE_NAME, table_name=TABLE_NAME)

    existing_constraints = {uc["name"] for uc in inspector.get_unique_constraints(TABLE_NAME)}
    if OLD_UNIQUE_NAME in existing_constraints:
        op.drop_constraint(OLD_UNIQUE_NAME, TABLE_NAME, type_="unique")
    if NEW_UNIQUE_NAME not in existing_constraints:
        op.create_unique_constraint(NEW_UNIQUE_NAME, TABLE_NAME, UNIQUE_COLUMNS)


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return

    existing_constraints = {uc["name"] for uc in inspector.get_unique_constraints(TABLE_NAME)}
    if NEW_UNIQUE_NAME in existing_constraints:
        op.drop_constraint(NEW_UNIQUE_NAME, TABLE_NAME, type_="unique")
    if OLD_UNIQUE_NAME not in existing_constraints:
        op.create_unique_constraint(
            OLD_UNIQUE_NAME,
            TABLE_NAME,
            [
                "workspace_id",
                "auth_id",
                "advertiser_id",
                "store_id",
                "snapshot_type",
                "end_date",
            ],
        )
