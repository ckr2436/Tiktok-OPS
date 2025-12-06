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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col.get("name") for col in inspector.get_columns("gmv_campaigns")}

    if "primary_status" in columns:
        with op.batch_alter_table("gmv_campaigns") as batch_op:
            batch_op.drop_column("primary_status")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col.get("name") for col in inspector.get_columns("gmv_campaigns")}

    if "primary_status" not in columns:
        with op.batch_alter_table("gmv_campaigns") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "primary_status",
                    sa.String(length=64),
                    nullable=True,
                    comment="Official primary_status filter value from campaign/get",
                )
            )
