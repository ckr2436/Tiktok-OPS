"""add Website Ads domain

Revision ID: 0085_website_ads
Revises: 0084_ai_provider_model_switches
Create Date: 2026-07-12 23:20:00.000000
"""

from alembic import op


revision = "0085_website_ads"
down_revision = "0084_ai_provider_model_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        """
        create table if not exists website_ads_magento_connections (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          name varchar(128) not null, base_url varchar(512) not null,
          access_token_cipher varbinary(4096) not null, key_version int not null default 1,
          is_enabled tinyint(1) not null default 1, last_sync_at datetime(6) null, last_error text null,
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id), unique key uk_web_ads_magento_workspace_url (workspace_id, base_url),
          key idx_web_ads_magento_workspace (workspace_id, is_enabled),
          constraint fk_web_ads_magento_workspace foreign key (workspace_id) references workspaces(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
        """
        create table if not exists website_ads_landing_pages (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          connection_id bigint unsigned null, external_id varchar(64) not null, website_id int null,
          identifier varchar(128) not null, title varchar(512) not null, landing_url varchar(2048) not null,
          product_id varchar(128) null, content_name varchar(512) null, content_category varchar(255) null,
          brand varchar(128) null, description text null, reference_price decimal(18,4) null,
          currency varchar(8) not null default 'USD', image_url varchar(2048) null,
          is_active tinyint(1) not null default 1, raw_json json null, external_updated_at varchar(64) null,
          last_synced_at datetime(6) not null default current_timestamp(6),
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id), unique key uk_web_ads_landing_external (connection_id, external_id),
          key idx_web_ads_landing_workspace (workspace_id, is_active),
          key idx_web_ads_landing_product (workspace_id, product_id),
          constraint fk_web_ads_landing_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_landing_connection foreign key (connection_id) references website_ads_magento_connections(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
        """
        create table if not exists website_ads_campaigns (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          auth_id bigint unsigned not null, advertiser_id varchar(64) not null,
          landing_page_id bigint unsigned not null, request_key varchar(64) not null,
          campaign_id varchar(64) null, name varchar(512) not null,
          objective_type varchar(32) not null default 'WEB_CONVERSIONS',
          local_status varchar(32) not null default 'CREATING', operation_status varchar(32) null,
          secondary_status varchar(128) null, raw_json json null, error_message text null,
          last_synced_at datetime(6) null, created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id), unique key uk_web_ads_campaign_remote (workspace_id, auth_id, campaign_id),
          unique key uk_web_ads_campaign_request (workspace_id, request_key),
          key idx_web_ads_campaign_scope (workspace_id, auth_id, advertiser_id),
          key idx_web_ads_campaign_status (local_status, operation_status),
          constraint fk_web_ads_campaign_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_campaign_auth foreign key (auth_id) references oauth_accounts_ttb(id) on delete cascade,
          constraint fk_web_ads_campaign_landing foreign key (landing_page_id) references website_ads_landing_pages(id) on delete restrict
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
        """
        create table if not exists website_ads_adgroups (
          id bigint unsigned not null auto_increment, campaign_local_id bigint unsigned not null,
          adgroup_id varchar(64) null, name varchar(512) not null, pixel_id varchar(64) not null,
          targeting_json json not null, budget_mode varchar(64) not null, budget decimal(18,4) not null,
          bid_type varchar(64) not null, conversion_bid_price decimal(18,4) null,
          schedule_start_time varchar(32) not null, operation_status varchar(32) null,
          secondary_status varchar(128) null, raw_json json null,
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id), unique key uk_web_ads_adgroup_remote (campaign_local_id, adgroup_id),
          key idx_web_ads_adgroup_campaign (campaign_local_id),
          constraint fk_web_ads_adgroup_campaign foreign key (campaign_local_id) references website_ads_campaigns(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
        """
        create table if not exists website_ads_ads (
          id bigint unsigned not null auto_increment, campaign_local_id bigint unsigned not null,
          adgroup_local_id bigint unsigned not null, ad_id varchar(64) null, ad_id_v2 varchar(64) null,
          name varchar(512) not null, video_id varchar(128) not null, identity_type varchar(32) not null,
          identity_id varchar(128) not null, landing_page_url varchar(4096) not null,
          operation_status varchar(32) null, secondary_status varchar(128) null,
          guard_enabled tinyint(1) not null default 1, target_roas decimal(18,4) null,
          max_unprofitable_spend decimal(18,4) null, guard_config_json json null, raw_json json null,
          last_checked_at datetime(6) null, created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id), unique key uk_web_ads_ad_remote (adgroup_local_id, ad_id),
          key idx_web_ads_ad_campaign (campaign_local_id), key idx_web_ads_ad_status (guard_enabled, operation_status),
          constraint fk_web_ads_ad_campaign foreign key (campaign_local_id) references website_ads_campaigns(id) on delete cascade,
          constraint fk_web_ads_ad_adgroup foreign key (adgroup_local_id) references website_ads_adgroups(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
        """
        create table if not exists website_ads_metrics_hourly (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          advertiser_id varchar(64) not null, ad_local_id bigint unsigned not null, stat_hour datetime(6) not null,
          spend decimal(18,4) not null default 0, impressions bigint unsigned not null default 0,
          clicks bigint unsigned not null default 0, conversions decimal(18,4) not null default 0,
          conversion_value decimal(18,4) not null default 0, cpc decimal(18,4) null, cpm decimal(18,4) null,
          ctr decimal(18,6) null, cpa decimal(18,4) null, roas decimal(18,6) null,
          raw_json json null, synced_at datetime(6) not null default current_timestamp(6),
          primary key (id), unique key uk_web_ads_metric_hour (ad_local_id, stat_hour),
          key idx_web_ads_metric_scope (workspace_id, advertiser_id, stat_hour),
          constraint fk_web_ads_metric_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_metric_ad foreign key (ad_local_id) references website_ads_ads(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
        """
        create table if not exists website_ads_action_logs (
          id bigint unsigned not null auto_increment, workspace_id bigint unsigned not null,
          ad_local_id bigint unsigned null, actor_type varchar(32) not null, action varchar(64) not null,
          reason varchar(1024) null, result varchar(32) not null, request_json json null,
          response_json json null, metrics_json json null,
          created_at datetime(6) not null default current_timestamp(6), primary key (id),
          key idx_web_ads_action_ad (ad_local_id, created_at),
          key idx_web_ads_action_scope (workspace_id, action, created_at),
          constraint fk_web_ads_action_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_action_ad foreign key (ad_local_id) references website_ads_ads(id) on delete set null
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """,
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for table in (
        "website_ads_action_logs",
        "website_ads_metrics_hourly",
        "website_ads_ads",
        "website_ads_adgroups",
        "website_ads_campaigns",
        "website_ads_landing_pages",
        "website_ads_magento_connections",
    ):
        op.execute(f"drop table if exists {table}")
