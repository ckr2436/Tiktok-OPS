from alembic import op
import sqlalchemy as sa


revision = "0022_ttb_gmvmax_campaign_store_fields"
down_revision = "0021_ttb_gmvmax_strategy_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ttb_gmvmax_campaigns",
        sa.Column("store_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ttb_gmvmax_campaigns",
        sa.Column("operation_status", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "idx_ttb_gmvmax_campaign_store",
        "ttb_gmvmax_campaigns",
        ["store_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ttb_gmvmax_campaign_store", table_name="ttb_gmvmax_campaigns")
    op.drop_column("ttb_gmvmax_campaigns", "operation_status")
    op.drop_column("ttb_gmvmax_campaigns", "store_id")
