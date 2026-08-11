from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0023_ttb_gmvmax_creative_metrics"
down_revision = "0022_ttb_gmvmax_campaign_store_fields"
branch_labels = None
depends_on = None


UBigInt = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")


def _dt6():
    return mysql_dialect.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "ttb_gmvmax_creative_metrics_daily",
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
        sa.Column("adgroup_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("stat_time_day", _dt6(), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("cost", sa.Numeric(18, 4), nullable=True),
        sa.Column("net_cost", sa.Numeric(18, 4), nullable=True),
        sa.Column("orders", sa.Integer(), nullable=True),
        sa.Column("gross_revenue", sa.Numeric(18, 4), nullable=True),
        sa.Column("roi", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_click_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_conversion_rate", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_2s", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_6s", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p25", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p50", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p75", sa.Numeric(18, 4), nullable=True),
        sa.Column("ad_video_view_rate_p100", sa.Numeric(18, 4), nullable=True),
        sa.Column("raw_metrics", sa.JSON(), nullable=True),
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
            "stat_time_day",
            name="uk_ttb_gmvmax_creative_metrics_scope",
        ),
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_metrics_campaign",
        "ttb_gmvmax_creative_metrics_daily",
        ["workspace_id", "provider", "auth_id", "campaign_id"],
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_metrics_day",
        "ttb_gmvmax_creative_metrics_daily",
        ["workspace_id", "provider", "auth_id", "stat_time_day"],
    )
    op.create_index(
        "idx_ttb_gmvmax_creative_metrics_creative",
        "ttb_gmvmax_creative_metrics_daily",
        ["workspace_id", "provider", "auth_id", "creative_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ttb_gmvmax_creative_metrics_creative",
        table_name="ttb_gmvmax_creative_metrics_daily",
    )
    op.drop_index(
        "idx_ttb_gmvmax_creative_metrics_day",
        table_name="ttb_gmvmax_creative_metrics_daily",
    )
    op.drop_index(
        "idx_ttb_gmvmax_creative_metrics_campaign",
        table_name="ttb_gmvmax_creative_metrics_daily",
    )
    op.drop_table("ttb_gmvmax_creative_metrics_daily")
