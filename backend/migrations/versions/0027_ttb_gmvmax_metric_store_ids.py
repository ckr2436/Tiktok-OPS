from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0027_ttb_gmvmax_metric_store_ids"
down_revision = "0026_ttb_gmvmax_store_constraints"
branch_labels = None
depends_on = None


def _backfill_store_id(table_name: str) -> None:
    op.execute(
        text(
            f"""
            UPDATE {table_name}
            SET store_id = (
                SELECT COALESCE(c.store_id, '')
                FROM ttb_gmvmax_campaigns AS c
                WHERE c.id = {table_name}.campaign_id
            )
            WHERE store_id IS NULL
            """
        )
    )
    op.execute(
        text(
            f"UPDATE {table_name} SET store_id = '' WHERE store_id IS NULL"
        )
    )


def upgrade() -> None:
    for table in ("ttb_gmvmax_metrics_daily", "ttb_gmvmax_metrics_hourly"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("store_id", sa.String(length=64), nullable=True))
    _backfill_store_id("ttb_gmvmax_metrics_daily")
    _backfill_store_id("ttb_gmvmax_metrics_hourly")
    for table in ("ttb_gmvmax_metrics_daily", "ttb_gmvmax_metrics_hourly"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "store_id",
                existing_type=sa.String(length=64),
                nullable=False,
                server_default="",
            )
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "store_id",
                existing_type=sa.String(length=64),
                server_default=None,
            )
    op.create_index(
        "idx_ttb_gmvmax_metrics_daily_store",
        "ttb_gmvmax_metrics_daily",
        ["store_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_metrics_hourly_store",
        "ttb_gmvmax_metrics_hourly",
        ["store_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ttb_gmvmax_metrics_hourly_store", table_name="ttb_gmvmax_metrics_hourly")
    op.drop_index("idx_ttb_gmvmax_metrics_daily_store", table_name="ttb_gmvmax_metrics_daily")
    for table in ("ttb_gmvmax_metrics_daily", "ttb_gmvmax_metrics_hourly"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("store_id")
