from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "0024_ttb_gmvmax_creative_heating"
down_revision = "0023_ttb_gmvmax_creative_metrics"
branch_labels = None
depends_on = None


UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_gmvmax_creative_heating",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, nullable=False),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'tiktok-business'"),
        ),
        sa.Column("auth_id", UBigInt, nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("creative_name", sa.String(length=255), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column("target_daily_budget", sa.Numeric(18, 4), nullable=True),
        sa.Column("budget_delta", sa.Numeric(18, 4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("max_duration_minutes", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("last_action_type", sa.String(length=64), nullable=True),
        sa.Column("last_action_time", _dt6(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_action_request", sa.JSON(), nullable=True),
        sa.Column("last_action_response", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["auth_id"],
            ["oauth_accounts_ttb.id"],
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
            "creative_id",
            name="uk_ttb_gmvmax_creative_heating_scope",
        ),
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_heating_campaign",
        "ttb_gmvmax_creative_heating",
        ["workspace_id", "provider", "auth_id", "campaign_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_heating_creative",
        "ttb_gmvmax_creative_heating",
        ["workspace_id", "provider", "auth_id", "creative_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_heating_status",
        "ttb_gmvmax_creative_heating",
        ["workspace_id", "provider", "auth_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ttb_gmvmax_creative_heating_status",
        table_name="ttb_gmvmax_creative_heating",
    )
    op.drop_index(
        "idx_ttb_gmvmax_creative_heating_creative",
        table_name="ttb_gmvmax_creative_heating",
    )
    op.drop_index(
        "idx_ttb_gmvmax_creative_heating_campaign",
        table_name="ttb_gmvmax_creative_heating",
    )
    op.drop_table("ttb_gmvmax_creative_heating")
