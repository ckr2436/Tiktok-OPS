"""Create TikTok Business advertiser linkage tables"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0015_ttb_link_tables"
down_revision = "0014_ttb_store_extended_fields"
branch_labels = None
depends_on = None


UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")

def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_bc_advertiser_links",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, nullable=False),
        sa.Column("auth_id", UBigInt, nullable=False),
        sa.Column("bc_id", sa.String(length=64), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("relation_type", sa.String(length=32), nullable=False, server_default=sa.text("'UNKNOWN'")),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "last_seen_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "bc_id",
            "advertiser_id",
            name="uk_ttb_bc_adv_link_scope",
        ),
    )
    op.create_index(
        "idx_ttb_bc_adv_link_adv",
        "ttb_bc_advertiser_links",
        ["advertiser_id"],
    )
    op.create_index(
        "idx_ttb_bc_adv_link_bc",
        "ttb_bc_advertiser_links",
        ["bc_id"],
    )

    op.create_table(
        "ttb_advertiser_store_links",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, nullable=False),
        sa.Column("auth_id", UBigInt, nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("relation_type", sa.String(length=32), nullable=False, server_default=sa.text("'UNKNOWN'")),
        sa.Column("store_authorized_bc_id", sa.String(length=64), nullable=True),
        sa.Column("bc_id_hint", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "last_seen_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            name="uk_ttb_adv_store_link_scope",
        ),
    )
    op.create_index(
        "idx_ttb_adv_store_link_adv",
        "ttb_advertiser_store_links",
        ["advertiser_id"],
    )
    op.create_index(
        "idx_ttb_adv_store_link_store",
        "ttb_advertiser_store_links",
        ["store_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ttb_adv_store_link_store", table_name="ttb_advertiser_store_links")
    op.drop_index("idx_ttb_adv_store_link_adv", table_name="ttb_advertiser_store_links")
    op.drop_table("ttb_advertiser_store_links")

    op.drop_index("idx_ttb_bc_adv_link_bc", table_name="ttb_bc_advertiser_links")
    op.drop_index("idx_ttb_bc_adv_link_adv", table_name="ttb_bc_advertiser_links")
    op.drop_table("ttb_bc_advertiser_links")
