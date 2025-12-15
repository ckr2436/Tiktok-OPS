"""
Add cutoff-friendly snapshot batch indexes for GMV Max tables.

Revision ID: 0063_gmvmax_snapshot_indexes
Revises: 0062_gmvmax_metric_indexes
Create Date: 2025-06-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "0063_gmvmax_snapshot_indexes"
down_revision = "0062_gmvmax_metric_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "idx_prod_snapshot_batch_time_id",
        "gmvmax_product_campaign_snapshot_batches",
        ["snapshot_at", "id"],
    )
    op.create_index(
        "idx_live_snapshot_batch_time_id",
        "gmvmax_live_campaign_snapshot_batches",
        ["snapshot_at", "id"],
    )


def downgrade():
    op.drop_index(
        "idx_live_snapshot_batch_time_id", table_name="gmvmax_live_campaign_snapshot_batches"
    )
    op.drop_index(
        "idx_prod_snapshot_batch_time_id", table_name="gmvmax_product_campaign_snapshot_batches"
    )
