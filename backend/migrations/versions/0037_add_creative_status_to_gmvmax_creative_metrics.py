"""add creative_status to gmvmax creative metrics

Revision ID: 0037_add_creative_status_to_gmvmax_creative_metrics
Revises: 0036_ttb_gmvmax_soft_delete_and_snapshot_uniques
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0037_add_creative_status_to_gmvmax_creative_metrics"
down_revision = "0036_ttb_gmvmax_soft_delete_and_snapshot_uniques"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ttb_gmvmax_creative_metrics_daily",
        sa.Column("creative_status", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE ttb_gmvmax_creative_metrics_daily SET creative_status = 'UNKNOWN' WHERE creative_status IS NULL"
    )


def downgrade() -> None:
    op.drop_column("ttb_gmvmax_creative_metrics_daily", "creative_status")
