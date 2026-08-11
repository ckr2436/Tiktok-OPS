"""Add GMV Max metric scope and cutoff indexes.

Revision ID: 0062_gmvmax_metric_indexes
Revises: 0061_rebuild_gmvmax_campaign_tables
Create Date: 2025-06-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "0062_gmvmax_metric_indexes"
down_revision = "0061_rebuild_gmvmax_campaign_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "idx_prod_campaign_day_scope",
        "gmvmax_product_campaign_metrics_daily",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_day"],
    )
    op.create_index(
        "idx_prod_campaign_day_cutoff",
        "gmvmax_product_campaign_metrics_daily",
        ["stat_time_day", "id"],
    )
    op.create_index(
        "idx_prod_campaign_hour_scope",
        "gmvmax_product_campaign_metrics_hourly",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_hour"],
    )
    op.create_index(
        "idx_prod_campaign_hour_cutoff",
        "gmvmax_product_campaign_metrics_hourly",
        ["stat_time_hour", "id"],
    )
    op.create_index(
        "idx_live_campaign_day_scope",
        "gmvmax_live_campaign_metrics_daily",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_day"],
    )
    op.create_index(
        "idx_live_campaign_day_cutoff",
        "gmvmax_live_campaign_metrics_daily",
        ["stat_time_day", "id"],
    )
    op.create_index(
        "idx_live_campaign_hour_scope",
        "gmvmax_live_campaign_metrics_hourly",
        ["workspace_id", "auth_id", "advertiser_id", "store_id", "stat_time_hour"],
    )
    op.create_index(
        "idx_live_campaign_hour_cutoff",
        "gmvmax_live_campaign_metrics_hourly",
        ["stat_time_hour", "id"],
    )


def downgrade():
    op.drop_index("idx_live_campaign_hour_cutoff", table_name="gmvmax_live_campaign_metrics_hourly")
    op.drop_index("idx_live_campaign_hour_scope", table_name="gmvmax_live_campaign_metrics_hourly")
    op.drop_index("idx_live_campaign_day_cutoff", table_name="gmvmax_live_campaign_metrics_daily")
    op.drop_index("idx_live_campaign_day_scope", table_name="gmvmax_live_campaign_metrics_daily")
    op.drop_index("idx_prod_campaign_hour_cutoff", table_name="gmvmax_product_campaign_metrics_hourly")
    op.drop_index("idx_prod_campaign_hour_scope", table_name="gmvmax_product_campaign_metrics_hourly")
    op.drop_index("idx_prod_campaign_day_cutoff", table_name="gmvmax_product_campaign_metrics_daily")
    op.drop_index("idx_prod_campaign_day_scope", table_name="gmvmax_product_campaign_metrics_daily")
