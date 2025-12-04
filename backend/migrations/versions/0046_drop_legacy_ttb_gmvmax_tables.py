"""Drop legacy ttb_gmvmax tables now replaced by gmv_* schema.

Revision ID: 0046_drop_legacy_ttb_gmvmax_tables
Revises: 0045_gmv_campaign_sync_snapshots
Create Date: 2025-03-09 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect


revision = "0046_drop_legacy_ttb_gmvmax_tables"
down_revision = "0045_gmv_campaign_sync_snapshots"
branch_labels = None
depends_on = None


LEGACY_TABLES = [
    "ttb_gmvmax_creative_metrics_10min",
    "ttb_gmvmax_creative_metrics_daily",
    "ttb_gmvmax_metrics_hourly",
    "ttb_gmvmax_metrics_daily",
    "ttb_gmvmax_campaign_sync_snapshots",
    "ttb_gmvmax_campaign_products",
    "ttb_gmvmax_campaigns",
    "ttb_gmvmax_creative_heating",
    "ttb_gmvmax_action_logs",
    "ttb_gmvmax_strategy_config",
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in LEGACY_TABLES:
        if table in existing_tables:
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError("Downgrade not supported after dropping legacy GMV Max tables.")

