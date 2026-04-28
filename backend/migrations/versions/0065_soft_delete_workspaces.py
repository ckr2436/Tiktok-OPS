"""Add soft delete support to workspaces.

Revision ID: 0065_soft_delete_workspaces
Revises: 0064_drop_legacy_gmv_campaign_tables
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

revision = "0065_soft_delete_workspaces"
down_revision = "0064_drop_legacy_gmv_campaign_tables"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _column_exists("workspaces", "deleted_at"):
        op.add_column(
            "workspaces",
            sa.Column(
                "deleted_at",
                sa.DateTime(timezone=False).with_variant(mysql.DATETIME(fsp=6), "mysql"),
                nullable=True,
            ),
        )

    if not _index_exists("workspaces", "idx_workspaces_deleted_at"):
        op.create_index("idx_workspaces_deleted_at", "workspaces", ["deleted_at"])


def downgrade() -> None:
    if _index_exists("workspaces", "idx_workspaces_deleted_at"):
        op.drop_index("idx_workspaces_deleted_at", table_name="workspaces")

    if _column_exists("workspaces", "deleted_at"):
        op.drop_column("workspaces", "deleted_at")
