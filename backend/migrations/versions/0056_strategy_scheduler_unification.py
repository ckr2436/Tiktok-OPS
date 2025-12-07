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
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        if column.name not in existing_columns:
            op.add_column(table_name, column)

    def _create_index_if_missing(index_name: str, table_name: str, columns: list[str]) -> None:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table_name)}
        if index_name not in existing_indexes:
            op.create_index(index_name, table_name, columns)

    _add_column_if_missing(
        "gmvmax_monitoring_strategies",
        sa.Column("category", sa.String(length=32), nullable=True, server_default=sa.text("'GMVMAX'")),
    )
    _add_column_if_missing(
        "gmvmax_monitoring_strategies",
        sa.Column("task_name", sa.String(length=128), nullable=True, server_default=sa.text("'gmvmax.strategy'")),
    )
    # MySQL 5.7+ does not allow JSON columns with a non-NULL default value.
    # Add the column as nullable first, backfill data, then enforce NOT NULL.
    _add_column_if_missing(
        "gmvmax_monitoring_strategies",
        sa.Column("params_json", sa.JSON(), nullable=True),
    )
    _add_column_if_missing(
        "gmvmax_monitoring_strategies", sa.Column("input_schema_json", sa.JSON(), nullable=True)
    )

    op.alter_column("gmvmax_monitoring_strategies", "level", existing_type=sa.String(length=32), nullable=True)

    _create_index_if_missing(
        "idx_category_enabled",
        "gmvmax_monitoring_strategies",
        ["category", "enabled", "level"],
    )
    _create_index_if_missing(
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

    op.alter_column(
        "gmvmax_monitoring_strategies",
        "params_json",
        existing_type=sa.JSON(),
        nullable=False,
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    def _drop_index_if_exists(index_name: str, table_name: str) -> None:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table_name)}
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name=table_name)

    def _drop_column_if_exists(table_name: str, column_name: str) -> None:
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        if column_name in existing_columns:
            op.drop_column(table_name, column_name)

    conn.execute(
        sa.text(
            """
            UPDATE gmvmax_monitoring_strategies
            SET level = COALESCE(level, 'OVERVIEW_DAILY')
            """
        )
    )

    _drop_index_if_exists("idx_workspace_scope", "gmvmax_monitoring_strategies")
    _drop_index_if_exists("idx_category_enabled", "gmvmax_monitoring_strategies")

    op.alter_column("gmvmax_monitoring_strategies", "level", existing_type=sa.String(length=32), nullable=False)

    _drop_column_if_exists("gmvmax_monitoring_strategies", "input_schema_json")
    _drop_column_if_exists("gmvmax_monitoring_strategies", "params_json")
    _drop_column_if_exists("gmvmax_monitoring_strategies", "task_name")
    _drop_column_if_exists("gmvmax_monitoring_strategies", "category")
