"""Add lifecycle_status to gmv_campaigns

Revision ID: 0054_add_lifecycle_status_to_gmv_campaigns
Revises: 0053_drop_gmv_campaign_primary_status
Create Date: 2025-07-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0054_add_lifecycle_status_to_gmv_campaigns"
down_revision = "0053_drop_gmv_campaign_primary_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col.get("name") for col in inspector.get_columns("gmv_campaigns")}
    indexes = {idx.get("name") for idx in inspector.get_indexes("gmv_campaigns")}

    if "lifecycle_status" not in columns:
        with op.batch_alter_table("gmv_campaigns") as batch_op:
            batch_op.add_column(
                sa.Column("lifecycle_status", sa.String(length=32), nullable=True)
            )

    if "idx_gmv_campaigns_lifecycle_status" not in indexes:
        op.create_index(
            "idx_gmv_campaigns_lifecycle_status",
            "gmv_campaigns",
            ["workspace_id", "lifecycle_status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col.get("name") for col in inspector.get_columns("gmv_campaigns")}
    indexes = {idx.get("name") for idx in inspector.get_indexes("gmv_campaigns")}

    if "idx_gmv_campaigns_lifecycle_status" in indexes:
        op.drop_index("idx_gmv_campaigns_lifecycle_status", table_name="gmv_campaigns")

    if "lifecycle_status" in columns:
        with op.batch_alter_table("gmv_campaigns") as batch_op:
            batch_op.drop_column("lifecycle_status")
