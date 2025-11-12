"""Create GMV Max core tables"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0019_ttb_gmvmax_core"
down_revision = "0018_ttb_product_gmv_fields"
branch_labels = None
depends_on = None


UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_gmvmax_campaigns",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, nullable=False),
        sa.Column("auth_id", UBigInt, nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("shopping_ads_type", sa.String(length=32), nullable=True),
        sa.Column("optimization_goal", sa.String(length=64), nullable=True),
        sa.Column("roas_bid", sa.Numeric(18, 4), nullable=True),
        sa.Column("daily_budget_cents", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("ext_created_time", _dt6(), nullable=True),
        sa.Column("ext_updated_time", _dt6(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
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
        sa.UniqueConstraint("workspace_id", "auth_id", "campaign_id", name="uk_ttb_gmvmax_campaign_scope"),
    )
    op.create_index(
        "idx_ttb_gmvmax_campaign_advertiser",
        "ttb_gmvmax_campaigns",
        ["advertiser_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_campaign_status",
        "ttb_gmvmax_campaigns",
        ["status"],
    )

    op.create_table(
        "ttb_gmvmax_metrics_hourly",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("campaign_id", UBigInt, nullable=False),
        sa.Column("interval_start", _dt6(), nullable=False),
        sa.Column("interval_end", _dt6(), nullable=True),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.Integer(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("product_impressions", sa.BigInteger(), nullable=True),
        sa.Column("product_clicks", sa.BigInteger(), nullable=True),
        sa.Column("product_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_conversion_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("video_views_2s", sa.BigInteger(), nullable=True),
        sa.Column("video_views_6s", sa.BigInteger(), nullable=True),
        sa.Column("video_views_p25", sa.BigInteger(), nullable=True),
        sa.Column("video_views_p50", sa.BigInteger(), nullable=True),
        sa.Column("video_views_p75", sa.BigInteger(), nullable=True),
        sa.Column("video_views_p100", sa.BigInteger(), nullable=True),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["ttb_gmvmax_campaigns.id"],
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("campaign_id", "interval_start", name="uk_ttb_gmvmax_metrics_hourly"),
    )
    op.create_index(
        "idx_ttb_gmvmax_metrics_hourly_campaign",
        "ttb_gmvmax_metrics_hourly",
        ["campaign_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_metrics_hourly_interval",
        "ttb_gmvmax_metrics_hourly",
        ["interval_start"],
    )

    op.create_table(
        "ttb_gmvmax_metrics_daily",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("campaign_id", UBigInt, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("net_cost_cents", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.Integer(), nullable=True),
        sa.Column("gross_revenue_cents", sa.BigInteger(), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("product_impressions", sa.BigInteger(), nullable=True),
        sa.Column("product_clicks", sa.BigInteger(), nullable=True),
        sa.Column("product_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_conversion_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("live_views", sa.BigInteger(), nullable=True),
        sa.Column("live_follows", sa.BigInteger(), nullable=True),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            _dt6(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["ttb_gmvmax_campaigns.id"],
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("campaign_id", "date", name="uk_ttb_gmvmax_metrics_daily"),
    )
    op.create_index(
        "idx_ttb_gmvmax_metrics_daily_date",
        "ttb_gmvmax_metrics_daily",
        ["date"],
    )
    op.create_index(
        "idx_ttb_gmvmax_metrics_daily_campaign",
        "ttb_gmvmax_metrics_daily",
        ["campaign_id"],
    )

    op.create_table(
        "ttb_gmvmax_action_logs",
        sa.Column("id", UBigInt, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBigInt, nullable=False),
        sa.Column("auth_id", UBigInt, nullable=False),
        sa.Column("campaign_id", UBigInt, nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("performed_by", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", _dt6(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["auth_id"], ["oauth_accounts_ttb.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["ttb_gmvmax_campaigns.id"],
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "idx_ttb_gmvmax_action_workspace",
        "ttb_gmvmax_action_logs",
        ["workspace_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_action_auth",
        "ttb_gmvmax_action_logs",
        ["auth_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_action_campaign",
        "ttb_gmvmax_action_logs",
        ["campaign_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_action_created",
        "ttb_gmvmax_action_logs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_ttb_gmvmax_action_created", table_name="ttb_gmvmax_action_logs")
    op.drop_index("idx_ttb_gmvmax_action_campaign", table_name="ttb_gmvmax_action_logs")
    op.drop_index("idx_ttb_gmvmax_action_auth", table_name="ttb_gmvmax_action_logs")
    op.drop_index("idx_ttb_gmvmax_action_workspace", table_name="ttb_gmvmax_action_logs")
    op.drop_table("ttb_gmvmax_action_logs")

    op.drop_index("idx_ttb_gmvmax_metrics_daily_campaign", table_name="ttb_gmvmax_metrics_daily")
    op.drop_index("idx_ttb_gmvmax_metrics_daily_date", table_name="ttb_gmvmax_metrics_daily")
    op.drop_table("ttb_gmvmax_metrics_daily")

    op.drop_index("idx_ttb_gmvmax_metrics_hourly_interval", table_name="ttb_gmvmax_metrics_hourly")
    op.drop_index("idx_ttb_gmvmax_metrics_hourly_campaign", table_name="ttb_gmvmax_metrics_hourly")
    op.drop_table("ttb_gmvmax_metrics_hourly")

    op.drop_index("idx_ttb_gmvmax_campaign_status", table_name="ttb_gmvmax_campaigns")
    op.drop_index("idx_ttb_gmvmax_campaign_advertiser", table_name="ttb_gmvmax_campaigns")
    op.drop_table("ttb_gmvmax_campaigns")
