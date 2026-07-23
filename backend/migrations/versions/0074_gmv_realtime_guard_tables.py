"""add realtime GMV Max guard tables

Revision ID: 0074_gmv_realtime_guard_tables
Revises: 0073_kie_task_list_indexes
Create Date: 2026-07-07 16:10:00.000000
"""

from alembic import op


revision = "0074_gmv_realtime_guard_tables"
down_revision = "0073_kie_task_list_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists gmv_campaign_realtime_state (
          id bigint unsigned not null auto_increment,
          workspace_id bigint unsigned not null,
          auth_id bigint unsigned not null,
          advertiser_id varchar(64) not null,
          store_id varchar(64) not null,
          campaign_id varchar(64) not null,
          campaign_name varchar(255) null,
          promotion_type varchar(16) not null default 'PRODUCT',
          operation_status varchar(32) null,
          secondary_status varchar(128) null,
          strategy_id bigint unsigned null,
          daily_budget_cents bigint null,
          latest_cost_cents bigint null,
          latest_net_cost_cents bigint null,
          latest_gross_revenue_cents bigint null,
          latest_orders bigint null,
          latest_roi decimal(18,4) null,
          report_start_date date null,
          report_end_date date null,
          source varchar(64) not null default 'tiktok_report_today',
          raw_metrics_json json null,
          guard_status varchar(32) null,
          last_action varchar(32) null,
          last_reason varchar(512) null,
          paused_until datetime(6) null,
          last_report_at datetime(6) null,
          last_checked_at datetime(6) null,
          config_json json null,
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id),
          unique key uk_gmv_realtime_campaign (workspace_id, auth_id, advertiser_id, store_id, campaign_id),
          key idx_gmv_realtime_campaign (campaign_id),
          key idx_gmv_realtime_checked (last_checked_at),
          key idx_gmv_realtime_status (guard_status, last_action)
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute(
        """
        create table if not exists gmv_campaign_guard_events (
          id bigint unsigned not null auto_increment,
          workspace_id bigint unsigned not null,
          auth_id bigint unsigned not null,
          advertiser_id varchar(64) not null,
          store_id varchar(64) not null,
          campaign_id varchar(64) not null,
          strategy_id bigint unsigned null,
          event_type varchar(64) not null,
          action varchar(32) not null,
          reason varchar(512) null,
          result varchar(32) not null,
          cost_cents bigint null,
          gross_revenue_cents bigint null,
          orders bigint null,
          roi decimal(18,4) null,
          request_json json null,
          response_json json null,
          error_message text null,
          created_at datetime(6) not null default current_timestamp(6),
          primary key (id),
          key idx_gmv_guard_event_campaign (campaign_id, created_at),
          key idx_gmv_guard_event_scope (workspace_id, auth_id, advertiser_id, created_at),
          key idx_gmv_guard_event_action (action, result, created_at)
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute("alter table gmv_campaign_realtime_state convert to character set utf8mb4 collate utf8mb4_0900_ai_ci")
    op.execute("alter table gmv_campaign_guard_events convert to character set utf8mb4 collate utf8mb4_0900_ai_ci")


def downgrade() -> None:
    op.execute("drop table if exists gmv_campaign_guard_events")
    op.execute("drop table if exists gmv_campaign_realtime_state")
