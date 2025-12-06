"""Drop primary_status from gmv_campaigns

Revision ID: 0053_drop_gmv_campaign_primary_status
Revises: 0052_drop_gmv_campaign_sync_snapshots
Create Date: 2025-06-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0053_drop_gmv_campaign_primary_status"
down_revision = "0052_drop_gmv_campaign_sync_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gmv_campaigns") as batch_op:
        batch_op.drop_column("primary_status")


def downgrade() -> None:
    with op.batch_alter_table("gmv_campaigns") as batch_op:
        batch_op.add_column(
            sa.Column(
                "primary_status",
                sa.String(length=64),
                nullable=True,
                comment="Official primary_status filter value from campaign/get",
            )
        )
