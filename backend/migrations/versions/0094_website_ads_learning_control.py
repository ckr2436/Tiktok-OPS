"""Add Website Ads cross-channel guard state and daily learning reports.

Revision ID: 0094_website_ads_learning_control
Revises: 0093_gmvmax_creative_local_media
"""

from alembic import op


revision = "0094_website_ads_learning_control"
down_revision = "0093_gmvmax_creative_local_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists website_ads_conversion_guard_states (
            id bigint unsigned not null auto_increment primary key,
            workspace_id bigint unsigned not null,
            auth_id bigint unsigned not null,
            advertiser_id varchar(64) not null,
            campaign_local_id bigint unsigned not null,
            product_id varchar(128) not null,
            control_enabled tinyint(1) not null default 1,
            status varchar(32) not null default 'OBSERVING',
            observation_started_at datetime(6) null,
            source_window_start_hour datetime(6) null,
            baseline_website_spend decimal(18,4) not null default 0,
            baseline_website_clicks bigint unsigned not null default 0,
            baseline_order_count bigint unsigned not null default 0,
            last_observed_order_count bigint unsigned not null default 0,
            last_order_at datetime(6) null,
            last_order_detected_at datetime(6) null,
            pause_count int not null default 0,
            paused_at datetime(6) null,
            resume_at datetime(6) null,
            last_evaluated_at datetime(6) null,
            last_source_hour datetime(6) null,
            policy_json json null,
            state_json json null,
            created_at datetime(6) not null default current_timestamp(6),
            updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
            unique key uk_web_ads_conversion_guard_campaign (campaign_local_id),
            key idx_web_ads_conversion_guard_due (status, resume_at),
            key idx_web_ads_conversion_guard_product (
                workspace_id, auth_id, advertiser_id, product_id
            ),
            constraint fk_web_ads_conversion_guard_workspace
                foreign key (workspace_id) references workspaces(id) on delete cascade,
            constraint fk_web_ads_conversion_guard_auth
                foreign key (auth_id) references oauth_accounts_ttb(id) on delete cascade,
            constraint fk_web_ads_conversion_guard_campaign
                foreign key (campaign_local_id) references website_ads_campaigns(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute(
        """
        create table if not exists website_ads_daily_reports (
            id bigint unsigned not null auto_increment primary key,
            workspace_id bigint unsigned not null,
            auth_id bigint unsigned not null,
            advertiser_id varchar(64) not null,
            campaign_local_id bigint unsigned not null,
            landing_page_id bigint unsigned not null,
            report_date date not null,
            advertiser_timezone varchar(64) not null,
            status varchar(32) not null default 'GENERATED',
            metrics_json json not null,
            audience_performance_json json not null,
            gmv_signal_json json null,
            action_summary_json json null,
            hermes_report_json json null,
            report_text text null,
            source_freshness_json json null,
            generated_at datetime(6) not null,
            created_at datetime(6) not null default current_timestamp(6),
            updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
            unique key uk_web_ads_daily_report_campaign_date (campaign_local_id, report_date),
            key idx_web_ads_daily_report_scope (workspace_id, advertiser_id, report_date),
            key idx_web_ads_daily_report_product (landing_page_id, report_date),
            constraint fk_web_ads_daily_report_workspace
                foreign key (workspace_id) references workspaces(id) on delete cascade,
            constraint fk_web_ads_daily_report_auth
                foreign key (auth_id) references oauth_accounts_ttb(id) on delete cascade,
            constraint fk_web_ads_daily_report_campaign
                foreign key (campaign_local_id) references website_ads_campaigns(id) on delete cascade,
            constraint fk_web_ads_daily_report_landing
                foreign key (landing_page_id) references website_ads_landing_pages(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )


def downgrade() -> None:
    op.execute("drop table if exists website_ads_daily_reports")
    op.execute("drop table if exists website_ads_conversion_guard_states")
