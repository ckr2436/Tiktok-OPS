"""gmv action and strategy tables plus store fields

Revision ID: 0043_gmv_action_strategy
Revises: 0042_gmv_restructure_schema
Create Date: 2024-06-04 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0043_gmv_action_strategy"
down_revision = "0042_gmv_restructure_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "gmv_campaigns",
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "gmv_campaigns",
        sa.Column("operation_status", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "gmv_campaigns",
        sa.Column("secondary_status", sa.String(length=128), nullable=True),
    )

    op.add_column(
        "gmv_campaign_products",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gmv_campaign_products",
        sa.Column("auth_id", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gmv_campaign_products",
        sa.Column(
            "campaign_pk",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "gmv_campaign_products",
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.alter_column("gmv_campaign_products", "store_id", server_default=None)
    op.alter_column("gmv_campaign_products", "workspace_id", server_default=None)
    op.alter_column("gmv_campaign_products", "auth_id", server_default=None)
    op.alter_column("gmv_campaign_products", "campaign_pk", server_default=None)

    with op.batch_alter_table("gmv_campaign_products") as batch:
        batch.create_foreign_key(
            "fk_gmv_campaign_products_campaign_pk",
            "gmv_campaigns",
            ["campaign_pk"],
            ["id"],
            ondelete="CASCADE",
            onupdate="RESTRICT",
        )
        batch.drop_constraint("uk_gmv_campaign_product", type_="unique")
        batch.create_unique_constraint(
            "uk_gmv_campaign_product",
            ["workspace_id", "auth_id", "campaign_id", "store_id", "item_group_id"],
        )
        batch.create_unique_constraint(
            "uk_gmv_store_product_unique",
            ["workspace_id", "auth_id", "store_id", "item_group_id"],
        )
        batch.create_index("idx_gmv_campaign_product_store", ["store_id"], unique=False)
        batch.create_index("idx_gmv_campaign_product_workspace", ["workspace_id", "auth_id"], unique=False)

    op.add_column(
        "gmv_campaign_metrics_daily",
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "gmv_campaign_metrics_hourly",
        sa.Column("store_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index(
        "idx_campaign_daily_store",
        "gmv_campaign_metrics_daily",
        ["store_id"],
    )
    op.create_index(
        "idx_campaign_hourly_store",
        "gmv_campaign_metrics_hourly",
        ["store_id"],
    )
    op.alter_column("gmv_campaign_metrics_daily", "store_id", server_default=None)
    op.alter_column("gmv_campaign_metrics_hourly", "store_id", server_default=None)

    op.create_table(
        "gmv_action_logs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            primary_key=True,
        ),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "campaign_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            sa.ForeignKey("gmv_campaigns.id", onupdate="RESTRICT", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("performed_by", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Index("idx_gmv_action_workspace", "workspace_id"),
        sa.Index("idx_gmv_action_auth", "auth_id"),
        sa.Index("idx_gmv_action_campaign", "campaign_id"),
        sa.Index("idx_gmv_action_created", "created_at"),
    )

    op.create_table(
        "gmv_strategy_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("auth_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("target_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("min_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("max_roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("min_impressions", sa.Integer(), nullable=True),
        sa.Column("min_clicks", sa.Integer(), nullable=True),
        sa.Column("max_budget_raise_pct_per_day", sa.Numeric(5, 2), nullable=True),
        sa.Column("max_budget_cut_pct_per_day", sa.Numeric(5, 2), nullable=True),
        sa.Column("max_roas_step_per_adjust", sa.Numeric(10, 4), nullable=True),
        sa.Column("cooldown_minutes", sa.Integer(), nullable=True),
        sa.Column("min_runtime_minutes_before_first_change", sa.Integer(), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
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
            "campaign_id",
            name="uq_gmv_strategy_workspace_auth_campaign",
        ),
        sa.Index("idx_gmv_strategy_workspace", "workspace_id"),
        sa.Index("idx_gmv_strategy_auth", "auth_id"),
        sa.Index("idx_gmv_strategy_campaign", "campaign_id"),
    )

    op.alter_column("gmv_campaigns", "store_id", server_default=None)


def downgrade():
    op.alter_column("gmv_campaigns", "store_id", server_default="")
    op.drop_column("gmv_campaigns", "secondary_status")
    op.drop_column("gmv_campaigns", "operation_status")
    op.drop_column("gmv_campaigns", "store_id")

    op.drop_index("idx_campaign_hourly_store", table_name="gmv_campaign_metrics_hourly")
    op.drop_index("idx_campaign_daily_store", table_name="gmv_campaign_metrics_daily")
    op.drop_column("gmv_campaign_metrics_hourly", "store_id")
    op.drop_column("gmv_campaign_metrics_daily", "store_id")

    with op.batch_alter_table("gmv_campaign_products") as batch:
        batch.drop_index("idx_gmv_campaign_product_workspace")
        batch.drop_index("idx_gmv_campaign_product_store")
        batch.drop_constraint("uk_gmv_store_product_unique", type_="unique")
        batch.drop_constraint("uk_gmv_campaign_product", type_="unique")
        batch.create_unique_constraint(
            "uk_gmv_campaign_product",
            ["campaign_id", "item_group_id", "promotion_type"],
        )
        batch.drop_constraint("fk_gmv_campaign_products_campaign_pk", type_="foreignkey")
        batch.drop_column("campaign_pk")
        batch.drop_column("store_id")
        batch.drop_column("auth_id")
        batch.drop_column("workspace_id")

    op.drop_table("gmv_strategy_configs")
    op.drop_table("gmv_action_logs")
