"""add gmv max creative 10 minute metrics snapshot

Revision ID: 0041_add_ttb_gmvmax_creative_metrics_10min
Revises: 0040_video_site_login_sessions
Create Date: 2025-01-24 00:00:00.000000
"""

from alembic import op
from sqlalchemy.dialects import mysql
import sqlalchemy as sa


revision = "0041_add_ttb_gmvmax_creative_metrics_10min"
down_revision = "0040_video_site_login_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ttb_gmvmax_creative_metrics_10min",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "workspace_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE", onupdate="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="tiktok-business"),
        sa.Column(
            "auth_id",
            sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
            sa.ForeignKey("oauth_accounts_ttb.id", ondelete="CASCADE", onupdate="RESTRICT"),
            nullable=False,
        ),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("creative_id", sa.String(length=64), nullable=False),
        sa.Column("stat_time_day", sa.Date(), nullable=False),
        sa.Column("snapshot_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("product_impressions", sa.BigInteger(), nullable=True),
        sa.Column("product_clicks", sa.BigInteger(), nullable=True),
        sa.Column("product_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_conversion_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p25", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p50", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p75", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p100", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_2s", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_6s", sa.Numeric(18, 4), nullable=True),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("cost_per_order_cents", sa.BigInteger(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("creative_delivery_status", sa.String(length=64), nullable=True),
        sa.Column("raw_metrics", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", sa.dialects.mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), server_onupdate=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "auth_id",
            "campaign_id",
            "creative_id",
            "stat_time_day",
            "snapshot_at",
            name="uk_ttb_gmvmax_creative_metrics_10min_scope",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_metrics_10min_campaign",
        "ttb_gmvmax_creative_metrics_10min",
        ["workspace_id", "provider", "auth_id", "campaign_id", "stat_time_day"],
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_metrics_10min_creative",
        "ttb_gmvmax_creative_metrics_10min",
        ["workspace_id", "provider", "auth_id", "creative_id", "stat_time_day"],
    )


def downgrade() -> None:
    op.drop_index("idx_ttb_gmvmax_creative_metrics_10min_campaign", table_name="ttb_gmvmax_creative_metrics_10min")
    op.drop_index("idx_ttb_gmvmax_creative_metrics_10min_creative", table_name="ttb_gmvmax_creative_metrics_10min")
    op.drop_table("ttb_gmvmax_creative_metrics_10min")
