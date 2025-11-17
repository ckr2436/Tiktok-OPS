"""enforce store id and secondary status"""

from alembic import op
import sqlalchemy as sa


revision = "0026_ttb_gmvmax_store_constraints"
down_revision = "0025_ttb_gmvmax_creative_heating_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ttb_gmvmax_campaigns") as batch_op:
        batch_op.add_column(sa.Column("secondary_status", sa.String(length=32), nullable=True))
    op.execute("UPDATE ttb_gmvmax_campaigns SET store_id = '' WHERE store_id IS NULL")
    with op.batch_alter_table("ttb_gmvmax_campaigns") as batch_op:
        batch_op.alter_column(
            "store_id",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default="",
        )
    with op.batch_alter_table("ttb_gmvmax_campaigns") as batch_op:
        batch_op.alter_column(
            "store_id",
            existing_type=sa.String(length=64),
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("ttb_gmvmax_campaigns") as batch_op:
        batch_op.alter_column(
            "store_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.drop_column("secondary_status")
