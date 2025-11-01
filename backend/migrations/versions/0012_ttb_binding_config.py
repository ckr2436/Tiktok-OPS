"""Add TikTok Business binding configuration table."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0012_ttb_binding_config"
down_revision = "0011_rename_shop_to_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind else ""

    big_int = sa.BigInteger()
    if dialect == "mysql":
        big_int = mysql.BIGINT(unsigned=True)

    datetime_type = sa.DateTime()
    if dialect == "mysql":
        datetime_type = mysql.DATETIME(fsp=6)

    json_type = sa.JSON()
    if dialect == "mysql":
        json_type = mysql.JSON()

    auto_default = sa.text("0") if dialect == "mysql" else sa.false()
    created_default = sa.text("CURRENT_TIMESTAMP(6)") if dialect == "mysql" else sa.func.current_timestamp()
    updated_default = sa.text("CURRENT_TIMESTAMP(6)") if dialect == "mysql" else sa.func.current_timestamp()
    updated_onupdate = sa.text("CURRENT_TIMESTAMP(6)") if dialect == "mysql" else None

    op.create_table(
        "ttb_binding_configs",
        sa.Column("id", big_int, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", big_int, sa.ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("auth_id", big_int, sa.ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False),
        sa.Column("bc_id", sa.String(length=64), nullable=True),
        sa.Column("advertiser_id", sa.String(length=64), nullable=True),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("auto_sync_products", sa.Boolean(), nullable=False, server_default=auto_default),
        sa.Column("auto_sync_schedule_id", big_int, sa.ForeignKey("schedules.id", onupdate="RESTRICT", ondelete="SET NULL"), nullable=True),
        sa.Column("last_manual_synced_at", datetime_type, nullable=True),
        sa.Column("last_manual_sync_summary_json", json_type, nullable=True),
        sa.Column("last_auto_synced_at", datetime_type, nullable=True),
        sa.Column("last_auto_sync_summary_json", json_type, nullable=True),
        sa.Column("created_at", datetime_type, nullable=False, server_default=created_default),
        sa.Column(
            "updated_at",
            datetime_type,
            nullable=False,
            server_default=updated_default,
            server_onupdate=updated_onupdate,
        ),
        sa.UniqueConstraint("workspace_id", "auth_id", name="uk_ttb_binding_scope"),
        mysql_engine="InnoDB" if dialect == "mysql" else None,
        mysql_charset="utf8mb4" if dialect == "mysql" else None,
    )
    op.create_index("idx_ttb_binding_scope", "ttb_binding_configs", ["workspace_id", "auth_id"])


def downgrade() -> None:
    op.drop_index("idx_ttb_binding_scope", table_name="ttb_binding_configs")
    op.drop_table("ttb_binding_configs")
