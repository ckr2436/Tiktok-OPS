"""add gmvmax campaign sync snapshots

Revision ID: 0035_ttb_gmvmax_sync_snapshots
Revises: 0034_ttb_advertiser_balances
Create Date: 2024-09-20 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "0035_ttb_gmvmax_sync_snapshots"
down_revision = "0034_ttb_advertiser_balances"
branch_labels = None
depends_on = None

TABLE_NAME = "ttb_gmvmax_campaign_sync_snapshots"


def _has_table(name: str) -> bool:
    insp = inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade() -> None:
    if _has_table(TABLE_NAME):
        return
    op.create_table(
        TABLE_NAME,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("auth_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "synced_at",
            name="uk_ttb_gmvmax_sync_snapshot",
        ),
    )
    op.create_index(
        "idx_ttb_gmvmax_sync_snapshot_scope",
        TABLE_NAME,
        ["workspace_id", "auth_id", "advertiser_id", "store_id"],
    )
    op.create_index("idx_ttb_gmvmax_sync_snapshot_time", TABLE_NAME, ["synced_at"])


def downgrade() -> None:
    if not _has_table(TABLE_NAME):
        return
    op.drop_index("idx_ttb_gmvmax_sync_snapshot_scope", table_name=TABLE_NAME)
    op.drop_index("idx_ttb_gmvmax_sync_snapshot_time", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
