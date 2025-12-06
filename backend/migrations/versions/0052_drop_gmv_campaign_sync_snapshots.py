"""Drop gmv_campaign_sync_snapshots table.

Revision ID: 0052_drop_gmv_campaign_sync_snapshots
Revises: 0051_gmv_campaign_indexes
Create Date: 2025-03-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

revision = "0052_drop_gmv_campaign_sync_snapshots"
down_revision = "0051_gmv_campaign_indexes"
branch_labels = None
depends_on = None

TABLE_NAME = "gmv_campaign_sync_snapshots"
ID_TYPE = sa.Integer().with_variant(mysql.BIGINT(unsigned=True), "mysql")
promotion_enum = sa.Enum("PRODUCT", "LIVE", name="promotiontypeenum")


def _has_table(name: str) -> bool:
    inspector = inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table(TABLE_NAME):
        op.drop_table(TABLE_NAME)


def downgrade() -> None:
    if _has_table(TABLE_NAME):
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("promotion_type", promotion_enum, nullable=True),
        sa.Column(
            "snapshot_type",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'CAMPAIGN'"),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("raw_request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "synced_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
            "snapshot_type",
            "synced_at",
            name="uk_gmv_campaign_sync_snapshot",
        ),
        sa.Index(
            "idx_gmv_campaign_sync_snapshot_scope",
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            "campaign_id",
        ),
        sa.Index("idx_gmv_campaign_sync_snapshot_time", "synced_at"),
    )
