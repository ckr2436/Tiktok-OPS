"""Add GMV Max monitoring strategies table

Revision ID: 0048_gmvmax_monitoring_strategies
Revises: 0047_gmv_metrics_rework
Create Date: 2025-03-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0048_gmvmax_monitoring_strategies"
down_revision = "0047_gmv_metrics_rework"
branch_labels = None
depends_on = None


ID_TYPE = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade():
    op.create_table(
        "gmvmax_monitoring_strategies",
        sa.Column("id", ID_TYPE, primary_key=True, autoincrement=True),
        # Align with ORM UBigInt definitions (BIGINT UNSIGNED)
        sa.Column("workspace_id", ID_TYPE, nullable=False),
        sa.Column("auth_id", ID_TYPE, nullable=True),
        sa.Column("advertiser_id", sa.String(length=64), nullable=True),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column(
            "promotion_type",
            sa.Enum("PRODUCT", "LIVE", name="promotiontypeenum"),
            nullable=True,
        ),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("max_campaigns_per_run", sa.Integer(), nullable=True),
        sa.Column("last_run_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
    )

    op.create_index(
        "idx_workspace_level",
        "gmvmax_monitoring_strategies",
        ["workspace_id", "level", "enabled"],
    )
    op.create_index(
        "idx_auth_store",
        "gmvmax_monitoring_strategies",
        ["auth_id", "store_id"],
    )
    op.create_index(
        "idx_enabled_interval",
        "gmvmax_monitoring_strategies",
        ["enabled", "interval_minutes"],
    )


def downgrade():
    op.drop_index("idx_enabled_interval", table_name="gmvmax_monitoring_strategies")
    op.drop_index("idx_auth_store", table_name="gmvmax_monitoring_strategies")
    op.drop_index("idx_workspace_level", table_name="gmvmax_monitoring_strategies")
    op.drop_table("gmvmax_monitoring_strategies")
