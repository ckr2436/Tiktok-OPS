"""
Add generic scheduler fields to GMV Max monitoring strategies.

Revision ID: 0056_strategy_scheduler_unification
Revises: 0055_gmv_campaign_lifecycle_backfill
Create Date: 2025-08-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0056_strategy_scheduler_unification"
down_revision = "0055_gmv_campaign_lifecycle_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gmvmax_monitoring_strategies",
        sa.Column("category", sa.String(length=32), nullable=True, server_default=sa.text("'GMVMAX'")),
    )
    op.add_column(
        "gmvmax_monitoring_strategies",
        sa.Column("task_name", sa.String(length=128), nullable=True, server_default=sa.text("'gmvmax.strategy'")),
    )
    op.add_column(
        "gmvmax_monitoring_strategies",
        sa.Column("params_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column("gmvmax_monitoring_strategies", sa.Column("input_schema_json", sa.JSON(), nullable=True))

    op.alter_column("gmvmax_monitoring_strategies", "level", existing_type=sa.String(length=32), nullable=True)

    op.create_index(
        "idx_category_enabled",
        "gmvmax_monitoring_strategies",
        ["category", "enabled", "level"],
    )
    op.create_index(
        "idx_workspace_scope",
        "gmvmax_monitoring_strategies",
        ["workspace_id", "auth_id", "advertiser_id", "store_id"],
    )

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE gmvmax_monitoring_strategies
            SET category = 'GMVMAX'
            WHERE category IS NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE gmvmax_monitoring_strategies
            SET task_name = 'gmvmax.strategy'
            WHERE task_name IS NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE gmvmax_monitoring_strategies
            SET params_json = '{}' WHERE params_json IS NULL
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE gmvmax_monitoring_strategies
            SET level = COALESCE(level, 'OVERVIEW_DAILY')
            """
        )
    )

    op.drop_index("idx_workspace_scope", table_name="gmvmax_monitoring_strategies")
    op.drop_index("idx_category_enabled", table_name="gmvmax_monitoring_strategies")

    op.alter_column("gmvmax_monitoring_strategies", "level", existing_type=sa.String(length=32), nullable=False)

    op.drop_column("gmvmax_monitoring_strategies", "input_schema_json")
    op.drop_column("gmvmax_monitoring_strategies", "params_json")
    op.drop_column("gmvmax_monitoring_strategies", "task_name")
    op.drop_column("gmvmax_monitoring_strategies", "category")
