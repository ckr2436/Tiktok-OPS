"""Expand GMV Max status columns."""

from alembic import op
import sqlalchemy as sa


revision = "0029_ttb_gmvmax_status_lengths"
down_revision = "0028_ttb_gmvmax_campaign_products"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ttb_gmvmax_campaigns") as batch_op:
        for column in ("status", "operation_status", "secondary_status", "shopping_ads_type"):
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=32),
                type_=sa.String(length=128),
                existing_nullable=True,
            )
    with op.batch_alter_table("ttb_gmvmax_campaign_products") as batch_op:
        batch_op.alter_column(
            "operation_status",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("ttb_gmvmax_campaign_products") as batch_op:
        batch_op.alter_column(
            "operation_status",
            existing_type=sa.String(length=128),
            type_=sa.String(length=32),
            existing_nullable=True,
        )
    with op.batch_alter_table("ttb_gmvmax_campaigns") as batch_op:
        for column in ("status", "operation_status", "secondary_status", "shopping_ads_type"):
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=128),
                type_=sa.String(length=32),
                existing_nullable=True,
            )
