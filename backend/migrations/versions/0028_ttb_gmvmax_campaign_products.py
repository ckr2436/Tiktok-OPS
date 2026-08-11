"""gmv max campaign product assignments"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0028_ttb_gmvmax_campaign_products"
down_revision = "0027_ttb_gmvmax_metric_store_ids"
branch_labels = None
depends_on = None


UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_gmvmax_campaign_products",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, nullable=False),
        sa.Column("auth_id", UBigInt, nullable=False),
        sa.Column("campaign_pk", UBigInt, nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("item_group_id", sa.String(length=64), nullable=False),
        sa.Column("operation_status", sa.String(length=32), nullable=True),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_pk"], ["ttb_gmvmax_campaigns.id"], onupdate="RESTRICT", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "campaign_id",
            "store_id",
            "item_group_id",
            name="uk_ttb_gmvmax_campaign_product_scope",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "store_id",
            "item_group_id",
            name="uk_ttb_gmvmax_store_product_unique",
        ),
    )
    op.create_index(
        "idx_ttb_gmvmax_campaign_product_campaign",
        "ttb_gmvmax_campaign_products",
        ["campaign_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_campaign_product_store",
        "ttb_gmvmax_campaign_products",
        ["store_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ttb_gmvmax_campaign_product_store", table_name="ttb_gmvmax_campaign_products"
    )
    op.drop_index(
        "idx_ttb_gmvmax_campaign_product_campaign", table_name="ttb_gmvmax_campaign_products"
    )
    op.drop_table("ttb_gmvmax_campaign_products")
