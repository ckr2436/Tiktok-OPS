"""Backfill GMV Max data into restructured tables.

Revision ID: 0043_gmv_restructure_backfill
Revises: 0042_gmv_restructure_schema
Create Date: 2024-06-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0043_gmv_restructure_backfill"
down_revision = "0042_gmv_restructure_schema"
branch_labels = None
depends_on = None


PROMOTION_PRODUCT = sa.literal("PRODUCT")


def _reflect(bind, table_name: str) -> sa.Table:
    metadata = sa.MetaData(bind=bind)
    return sa.Table(table_name, metadata, autoload_with=bind)


def _numeric_to_cents(column: sa.Column) -> sa.sql.expression.ColumnElement | None:
    """Convert a decimal column to integer cents (rounded) if present."""

    if column is None:
        return None
    return sa.func.round(column * 100)


def upgrade():
    bind = op.get_bind()

    ttb_campaigns = _reflect(bind, "ttb_gmvmax_campaigns")
    ttb_campaign_products = _reflect(bind, "ttb_gmvmax_campaign_products")
    ttb_campaign_metrics_daily = _reflect(bind, "ttb_gmvmax_metrics_daily")
    ttb_campaign_metrics_hourly = _reflect(bind, "ttb_gmvmax_metrics_hourly")
    ttb_creative_metrics_daily = _reflect(bind, "ttb_gmvmax_creative_metrics_daily")
    ttb_creative_metrics_10min = _reflect(bind, "ttb_gmvmax_creative_metrics_10min")

    gmv_campaigns = _reflect(bind, "gmv_campaigns")
    gmv_campaign_products = _reflect(bind, "gmv_campaign_products")
    gmv_campaign_metrics_daily = _reflect(bind, "gmv_campaign_metrics_daily")
    gmv_campaign_metrics_hourly = _reflect(bind, "gmv_campaign_metrics_hourly")
    gmv_creatives = _reflect(bind, "gmv_creatives")
    gmv_campaign_creatives = _reflect(bind, "gmv_campaign_creatives")
    gmv_creative_metrics_daily = _reflect(bind, "gmv_creative_metrics_daily")
    gmv_creative_metrics_10min = _reflect(bind, "gmv_creative_metrics_10min")

    # 1) Campaigns
    op.execute(
        gmv_campaigns.insert().from_select(
            [
                "workspace_id",
                "auth_id",
                "advertiser_id",
                "campaign_id",
                "promotion_type",
                "name",
                "status",
                "schedule_type",
                "schedule_start_time",
                "schedule_end_time",
                "shopping_ads_type",
                "optimization_goal",
                "bid_type",
                "roas_bid",
                "target_roi_budget",
                "max_delivery_budget",
                "daily_budget_cents",
                "currency",
                "ext_created_time",
                "ext_updated_time",
                "raw_json",
                "is_deleted",
                "deleted_at",
                "created_at",
                "updated_at",
            ],
            sa.select(
                ttb_campaigns.c.workspace_id,
                ttb_campaigns.c.auth_id,
                ttb_campaigns.c.advertiser_id,
                ttb_campaigns.c.campaign_id,
                PROMOTION_PRODUCT,
                ttb_campaigns.c.name,
                ttb_campaigns.c.status,
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                ttb_campaigns.c.shopping_ads_type,
                ttb_campaigns.c.optimization_goal,
                sa.literal(None),
                ttb_campaigns.c.roas_bid,
                sa.literal(None),
                sa.literal(None),
                ttb_campaigns.c.daily_budget_cents,
                ttb_campaigns.c.currency,
                ttb_campaigns.c.ext_created_time,
                ttb_campaigns.c.ext_updated_time,
                ttb_campaigns.c.raw_json,
                ttb_campaigns.c.is_deleted,
                ttb_campaigns.c.deleted_at,
                ttb_campaigns.c.created_at,
                ttb_campaigns.c.updated_at,
            ),
        )
    )

    # 2) Campaign products
    op.execute(
        gmv_campaign_products.insert().from_select(
            [
                "campaign_id",
                "item_group_id",
                "promotion_type",
                "store_id",
                "operation_status",
                "created_at",
                "updated_at",
            ],
            sa.select(
                ttb_campaign_products.c.campaign_id,
                ttb_campaign_products.c.item_group_id,
                PROMOTION_PRODUCT,
                ttb_campaign_products.c.store_id,
                ttb_campaign_products.c.operation_status,
                ttb_campaign_products.c.created_at,
                ttb_campaign_products.c.updated_at,
            ),
        )
    )

    # 3) Campaign metrics (daily/hourly) using string campaign_id
    campaign_lookup = sa.select(
        ttb_campaigns.c.id.label("pk"),
        ttb_campaigns.c.campaign_id.label("campaign_id"),
    ).subquery()

    op.execute(
        gmv_campaign_metrics_daily.insert().from_select(
            [
                "campaign_id",
                "promotion_type",
                "stat_time_day",
                "live_views",
                "live_10s_views",
                "live_follows",
                "impressions",
                "clicks",
                "product_clicks",
                "cost_cents",
                "net_cost_cents",
                "orders",
                "gross_revenue_cents",
                "roi",
                "ad_click_rate",
                "conversion_rate",
                "video_view_rate_2s",
                "video_view_rate_6s",
                "video_view_rate_25",
                "video_view_rate_50",
                "video_view_rate_75",
                "video_view_rate_100",
            ],
            sa.select(
                campaign_lookup.c.campaign_id,
                PROMOTION_PRODUCT,
                ttb_campaign_metrics_daily.c.date,
                ttb_campaign_metrics_daily.c.live_views,
                sa.literal(None),
                ttb_campaign_metrics_daily.c.live_follows,
                ttb_campaign_metrics_daily.c.impressions,
                ttb_campaign_metrics_daily.c.clicks,
                ttb_campaign_metrics_daily.c.product_clicks,
                ttb_campaign_metrics_daily.c.cost_cents,
                ttb_campaign_metrics_daily.c.net_cost_cents,
                ttb_campaign_metrics_daily.c.orders,
                ttb_campaign_metrics_daily.c.gross_revenue_cents,
                ttb_campaign_metrics_daily.c.roi,
                ttb_campaign_metrics_daily.c.ad_click_rate,
                ttb_campaign_metrics_daily.c.ad_conversion_rate,
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
            ).select_from(
                ttb_campaign_metrics_daily.join(
                    campaign_lookup, ttb_campaign_metrics_daily.c.campaign_id == campaign_lookup.c.pk
                )
            ),
        )
    )

    op.execute(
        gmv_campaign_metrics_hourly.insert().from_select(
            [
                "campaign_id",
                "promotion_type",
                "stat_time_hour",
                "live_views",
                "live_10s_views",
                "live_follows",
                "impressions",
                "clicks",
                "product_clicks",
                "cost_cents",
                "net_cost_cents",
                "orders",
                "gross_revenue_cents",
                "roi",
                "ad_click_rate",
                "conversion_rate",
                "video_view_rate_2s",
                "video_view_rate_6s",
                "video_view_rate_25",
                "video_view_rate_50",
                "video_view_rate_75",
                "video_view_rate_100",
            ],
            sa.select(
                campaign_lookup.c.campaign_id,
                PROMOTION_PRODUCT,
                ttb_campaign_metrics_hourly.c.interval_start,
                ttb_campaign_metrics_hourly.c.live_views,
                sa.literal(None),
                ttb_campaign_metrics_hourly.c.live_follows,
                ttb_campaign_metrics_hourly.c.impressions,
                ttb_campaign_metrics_hourly.c.clicks,
                ttb_campaign_metrics_hourly.c.product_clicks,
                ttb_campaign_metrics_hourly.c.cost_cents,
                ttb_campaign_metrics_hourly.c.net_cost_cents,
                ttb_campaign_metrics_hourly.c.orders,
                ttb_campaign_metrics_hourly.c.gross_revenue_cents,
                ttb_campaign_metrics_hourly.c.roi,
                ttb_campaign_metrics_hourly.c.ad_click_rate,
                ttb_campaign_metrics_hourly.c.ad_conversion_rate,
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
                sa.literal(None),
            ).select_from(
                ttb_campaign_metrics_hourly.join(
                    campaign_lookup, ttb_campaign_metrics_hourly.c.campaign_id == campaign_lookup.c.pk
                )
            ),
        )
    )

    # 4) Creative entities and relationships from daily metrics
    creative_base_query = sa.select(
        ttb_creative_metrics_daily.c.creative_id,
        ttb_creative_metrics_daily.c.campaign_id,
        ttb_creative_metrics_daily.c.product_id,
        ttb_creative_metrics_daily.c.creative_name,
        ttb_creative_metrics_daily.c.creative_status,
    ).group_by(
        ttb_creative_metrics_daily.c.creative_id,
        ttb_creative_metrics_daily.c.campaign_id,
        ttb_creative_metrics_daily.c.product_id,
        ttb_creative_metrics_daily.c.creative_name,
        ttb_creative_metrics_daily.c.creative_status,
    )

    op.execute(
        gmv_creatives.insert().from_select(
            [
                "creative_id",
                "campaign_id",
                "item_group_id",
                "creative_name",
                "creative_delivery_status",
            ],
            creative_base_query,
        )
    )

    op.execute(
        gmv_campaign_creatives.insert().from_select(
            ["campaign_id", "creative_id", "promotion_type", "item_group_id"],
            sa.select(
                creative_base_query.c.campaign_id,
                creative_base_query.c.creative_id,
                PROMOTION_PRODUCT,
                creative_base_query.c.product_id,
            ),
        )
    )

    # 5) Creative metrics daily
    op.execute(
        gmv_creative_metrics_daily.insert().from_select(
            [
                "campaign_id",
                "creative_id",
                "item_group_id",
                "stat_time_day",
                "impressions",
                "clicks",
                "product_clicks",
                "cost_cents",
                "net_cost_cents",
                "orders",
                "gross_revenue_cents",
                "roi",
                "ad_click_rate",
                "conversion_rate",
                "video_view_rate_2s",
                "video_view_rate_6s",
                "video_view_rate_25",
                "video_view_rate_50",
                "video_view_rate_75",
                "video_view_rate_100",
            ],
            sa.select(
                ttb_creative_metrics_daily.c.campaign_id,
                ttb_creative_metrics_daily.c.creative_id,
                ttb_creative_metrics_daily.c.product_id,
                sa.cast(ttb_creative_metrics_daily.c.stat_time_day, sa.Date),
                ttb_creative_metrics_daily.c.impressions,
                ttb_creative_metrics_daily.c.clicks,
                ttb_creative_metrics_daily.c.product_clicks
                if "product_clicks" in ttb_creative_metrics_daily.c
                else sa.literal(None),
                _numeric_to_cents(ttb_creative_metrics_daily.c.cost),
                _numeric_to_cents(ttb_creative_metrics_daily.c.net_cost),
                ttb_creative_metrics_daily.c.orders,
                _numeric_to_cents(ttb_creative_metrics_daily.c.gross_revenue),
                ttb_creative_metrics_daily.c.roi,
                ttb_creative_metrics_daily.c.ad_click_rate,
                ttb_creative_metrics_daily.c.ad_conversion_rate,
                ttb_creative_metrics_daily.c.ad_video_view_rate_2s,
                ttb_creative_metrics_daily.c.ad_video_view_rate_6s,
                ttb_creative_metrics_daily.c.ad_video_view_rate_p25,
                ttb_creative_metrics_daily.c.ad_video_view_rate_p50,
                ttb_creative_metrics_daily.c.ad_video_view_rate_p75,
                ttb_creative_metrics_daily.c.ad_video_view_rate_p100,
            ),
        )
    )

    # 6) Creative metrics 10min snapshots
    op.execute(
        gmv_creative_metrics_10min.insert().from_select(
            [
                "campaign_id",
                "creative_id",
                "stat_time_day",
                "snapshot_at",
                "impressions",
                "clicks",
                "product_clicks",
                "cost_cents",
                "net_cost_cents",
                "orders",
                "gross_revenue_cents",
                "roi",
                "ad_click_rate",
                "conversion_rate",
                "video_view_rate_2s",
                "video_view_rate_6s",
                "video_view_rate_25",
                "video_view_rate_50",
                "video_view_rate_75",
                "video_view_rate_100",
            ],
            sa.select(
                ttb_creative_metrics_10min.c.campaign_id,
                ttb_creative_metrics_10min.c.creative_id,
                ttb_creative_metrics_10min.c.stat_time_day,
                ttb_creative_metrics_10min.c.snapshot_at,
                ttb_creative_metrics_10min.c.impressions,
                ttb_creative_metrics_10min.c.clicks,
                ttb_creative_metrics_10min.c.product_clicks,
                ttb_creative_metrics_10min.c.cost_cents,
                ttb_creative_metrics_10min.c.net_cost_cents,
                ttb_creative_metrics_10min.c.orders,
                ttb_creative_metrics_10min.c.gross_revenue_cents,
                ttb_creative_metrics_10min.c.roi,
                ttb_creative_metrics_10min.c.ad_click_rate,
                ttb_creative_metrics_10min.c.ad_conversion_rate,
                ttb_creative_metrics_10min.c.ad_video_view_rate_2s,
                ttb_creative_metrics_10min.c.ad_video_view_rate_6s,
                ttb_creative_metrics_10min.c.ad_video_view_rate_p25,
                ttb_creative_metrics_10min.c.ad_video_view_rate_p50,
                ttb_creative_metrics_10min.c.ad_video_view_rate_p75,
                ttb_creative_metrics_10min.c.ad_video_view_rate_p100,
            ),
        )
    )


def downgrade():
    bind = op.get_bind()

    # Clear data written by this migration. Structural tables remain.
    for table_name in [
        "gmv_creative_metrics_10min",
        "gmv_creative_metrics_daily",
        "gmv_campaign_creatives",
        "gmv_creatives",
        "gmv_campaign_metrics_hourly",
        "gmv_campaign_metrics_daily",
        "gmv_campaign_products",
        "gmv_campaigns",
    ]:
        table = _reflect(bind, table_name)
        op.execute(table.delete())
