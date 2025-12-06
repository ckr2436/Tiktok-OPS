"""Backfill lifecycle_status for GMV campaigns.

Revision ID: 0055_gmv_campaign_lifecycle_backfill
Revises: 0054_add_lifecycle_status_to_gmv_campaigns
Create Date: 2025-07-15 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0055_gmv_campaign_lifecycle_backfill"
down_revision = "0054_add_lifecycle_status_to_gmv_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            """
            UPDATE gmv_campaigns
            SET lifecycle_status = 'DELETED',
                is_deleted = 1
            WHERE secondary_status = 'CAMPAIGN_STATUS_DELETE'
            """
        )
    )

    conn.execute(
        sa.text(
            """
            UPDATE gmv_campaigns
            SET lifecycle_status = 'ACTIVE',
                is_deleted = 0
            WHERE secondary_status = 'CAMPAIGN_STATUS_ENABLE'
              AND (lifecycle_status IS NULL OR lifecycle_status != 'DELETED')
            """
        )
    )

    conn.execute(
        sa.text(
            """
            UPDATE gmv_campaigns
            SET lifecycle_status = 'INACTIVE',
                is_deleted = 0
            WHERE lifecycle_status IS NULL
            """
        )
    )

    conn.execute(
        sa.text(
            """
            UPDATE gmv_campaigns
            SET operation_status = NULL
            WHERE operation_status = 'DELETE'
            """
        )
    )


def downgrade() -> None:
    """No data rollback for lifecycle backfill."""
    pass
