"""add Hermes-managed Website Ads planning

Revision ID: 0086_website_ads_hermes_planner
Revises: 0085_website_ads
Create Date: 2026-07-13 11:20:00.000000
"""

from alembic import op


revision = "0086_website_ads_hermes_planner"
down_revision = "0085_website_ads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in (
        "alter table website_ads_landing_pages add column seller_profile text null after image_url",
        "alter table website_ads_landing_pages add column promotion_text text null after seller_profile",
        "alter table website_ads_landing_pages add column product_details text null after promotion_text",
        "alter table website_ads_landing_pages add column hermes_analysis_json json null after product_details",
        "alter table website_ads_landing_pages add column analysis_status varchar(32) not null default 'NOT_ANALYZED' after hermes_analysis_json",
        "alter table website_ads_landing_pages add column analysis_error text null after analysis_status",
        "alter table website_ads_landing_pages add column analyzed_at datetime(6) null after analysis_error",
    ):
        op.execute(statement)

    op.execute(
        """
        create table website_ads_creative_assets (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          auth_id bigint unsigned not null, advertiser_id varchar(64) not null,
          landing_page_id bigint unsigned null, video_id varchar(128) not null,
          title varchar(512) not null, file_name varchar(512) null,
          preview_url varchar(4096) null, cover_url varchar(4096) null,
          duration_seconds decimal(12,3) null, width int null, height int null,
          source varchar(32) not null default 'TIKTOK_LIBRARY', user_notes text null,
          tags_json json null, hermes_analysis_json json null,
          analysis_status varchar(32) not null default 'NOT_ANALYZED', analysis_error text null,
          analyzed_at datetime(6) null, is_active tinyint(1) not null default 1,
          raw_json json null, last_synced_at datetime(6) not null default current_timestamp(6),
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id),
          unique key uk_web_ads_asset_video (workspace_id, auth_id, advertiser_id, video_id),
          key idx_web_ads_asset_scope (workspace_id, auth_id, advertiser_id),
          key idx_web_ads_asset_product (landing_page_id, is_active),
          constraint fk_web_ads_asset_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_asset_auth foreign key (auth_id) references oauth_accounts_ttb(id) on delete cascade,
          constraint fk_web_ads_asset_product foreign key (landing_page_id) references website_ads_landing_pages(id) on delete set null
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute(
        """
        create table website_ads_media_plans (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          auth_id bigint unsigned not null, advertiser_id varchar(64) not null,
          landing_page_id bigint unsigned not null, campaign_local_id bigint unsigned null,
          name varchar(512) not null, status varchar(32) not null default 'DRAFT',
          daily_budget decimal(18,4) not null, activate_after_create tinyint(1) not null default 0,
          strategy_source varchar(32) not null default 'HERMES', confidence varchar(16) null,
          strategy_summary text null, product_snapshot_json json null,
          selected_asset_ids_json json null, execution_context_json json null,
          hermes_response_json json null, error_message text null,
          generated_at datetime(6) null, executed_at datetime(6) null,
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id), key idx_web_ads_plan_scope (workspace_id, auth_id, advertiser_id),
          key idx_web_ads_plan_status (status, created_at),
          constraint fk_web_ads_plan_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_plan_auth foreign key (auth_id) references oauth_accounts_ttb(id) on delete cascade,
          constraint fk_web_ads_plan_product foreign key (landing_page_id) references website_ads_landing_pages(id) on delete restrict,
          constraint fk_web_ads_plan_campaign foreign key (campaign_local_id) references website_ads_campaigns(id) on delete set null
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute(
        """
        create table website_ads_media_plan_groups (
          id bigint unsigned not null auto_increment, media_plan_id bigint unsigned not null,
          name varchar(512) not null, role varchar(32) not null, hypothesis text null,
          targeting_json json not null, daily_budget decimal(18,4) not null,
          bid_strategy varchar(32) not null default 'LOWEST_COST', conversion_bid_price decimal(18,4) null,
          sort_order int not null, created_at datetime(6) not null default current_timestamp(6),
          primary key (id), unique key uk_web_ads_plan_group_order (media_plan_id, sort_order),
          key idx_web_ads_plan_group_plan (media_plan_id),
          constraint fk_web_ads_plan_group_plan foreign key (media_plan_id) references website_ads_media_plans(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute(
        """
        create table website_ads_media_plan_creatives (
          id bigint unsigned not null auto_increment, media_plan_group_id bigint unsigned not null,
          creative_asset_id bigint unsigned not null, ad_name varchar(512) not null,
          ad_text varchar(100) not null, call_to_action varchar(64) not null default 'SHOP_NOW',
          rationale text null, sort_order int not null,
          created_at datetime(6) not null default current_timestamp(6), primary key (id),
          unique key uk_web_ads_plan_group_asset (media_plan_group_id, creative_asset_id),
          key idx_web_ads_plan_creative_group (media_plan_group_id, sort_order),
          constraint fk_web_ads_plan_creative_group foreign key (media_plan_group_id) references website_ads_media_plan_groups(id) on delete cascade,
          constraint fk_web_ads_plan_creative_asset foreign key (creative_asset_id) references website_ads_creative_assets(id) on delete restrict
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists website_ads_media_plan_creatives")
    op.execute("drop table if exists website_ads_media_plan_groups")
    op.execute("drop table if exists website_ads_media_plans")
    op.execute("drop table if exists website_ads_creative_assets")
    for column in (
        "analyzed_at",
        "analysis_error",
        "analysis_status",
        "hermes_analysis_json",
        "product_details",
        "promotion_text",
        "seller_profile",
    ):
        op.execute(f"alter table website_ads_landing_pages drop column {column}")
