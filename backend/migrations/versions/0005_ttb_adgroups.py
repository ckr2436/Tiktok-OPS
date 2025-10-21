"""
Revision ID: 0005_ttb_adgroups
Revises: 0004_ttb_core_entities
Create Date: 2025-10-17 01:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0005_ttb_adgroups"
down_revision = "0004_ttb_core_entities"
branch_labels = None
depends_on = None


def _dt6():
    return mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_adgroups",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("workspace_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("auth_id", sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"), nullable=False),
        sa.Column("adgroup_id", sa.String(length=64), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=True),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("operation_status", sa.String(length=32), nullable=True),
        sa.Column("primary_status", sa.String(length=32), nullable=True),
        sa.Column("secondary_status", sa.String(length=64), nullable=True),
        sa.Column("budget", sa.Numeric(18, 4), nullable=True),
        sa.Column("budget_mode", sa.String(length=32), nullable=True),
        sa.Column("optimization_goal", sa.String(length=64), nullable=True),
        sa.Column("promotion_type", sa.String(length=64), nullable=True),
        sa.Column("bid_type", sa.String(length=32), nullable=True),
        sa.Column("bid_strategy", sa.String(length=32), nullable=True),
        sa.Column("schedule_start_time", _dt6(), nullable=True),
        sa.Column("schedule_end_time", _dt6(), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "last_seen_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_unique_constraint("uk_ttb_adgroup_scope", "ttb_adgroups", ["workspace_id", "auth_id", "adgroup_id"])
    op.create_index("idx_ttb_adgroup_scope", "ttb_adgroups", ["workspace_id", "auth_id", "adgroup_id"])
    op.create_index("idx_ttb_adgroup_advertiser", "ttb_adgroups", ["advertiser_id"])
    op.create_index("idx_ttb_adgroup_campaign", "ttb_adgroups", ["campaign_id"])
    op.create_index("idx_ttb_adgroup_operation_status", "ttb_adgroups", ["operation_status"])
    op.create_index("idx_ttb_adgroup_primary_status", "ttb_adgroups", ["primary_status"])
    op.create_index("idx_ttb_adgroup_secondary_status", "ttb_adgroups", ["secondary_status"])
    op.create_index("idx_ttb_adgroup_updated", "ttb_adgroups", ["ext_updated_time"])


def downgrade() -> None:
    op.execute("SET FOREIGN_KEY_CHECKS=0")
    op.execute("DROP TABLE IF EXISTS ttb_adgroups")
    op.execute("SET FOREIGN_KEY_CHECKS=1")
