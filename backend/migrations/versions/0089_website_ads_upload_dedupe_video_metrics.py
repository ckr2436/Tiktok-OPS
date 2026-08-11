"""Deduplicate Website Ads uploads and persist video metrics.

Revision ID: 0089_website_ads_upload_dedupe_video_metrics
Revises: 0088_website_ads_asset_intelligence
"""

from alembic import op


revision = "0089_website_ads_upload_dedupe_video_metrics"
down_revision = "0088_website_ads_asset_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists website_ads_upload_fingerprints (
          id bigint unsigned not null auto_increment,
          workspace_id bigint unsigned not null,
          auth_id bigint unsigned not null,
          advertiser_id varchar(64) not null,
          fingerprint_type varchar(32) not null default 'FILE_SHA256',
          content_sha256 char(64) not null,
          file_size_bytes bigint unsigned not null default 0,
          file_name varchar(512) null,
          status varchar(32) not null default 'UPLOADING',
          video_id varchar(128) null,
          response_json json null,
          error_message text null,
          created_at datetime(6) not null default current_timestamp(6),
          updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
          primary key (id),
          unique key uk_web_ads_upload_fingerprint (workspace_id, auth_id, advertiser_id, content_sha256),
          key idx_web_ads_upload_scope (workspace_id, auth_id, advertiser_id),
          key idx_web_ads_upload_status (status, updated_at),
          constraint fk_web_ads_upload_workspace foreign key (workspace_id) references workspaces(id) on delete cascade,
          constraint fk_web_ads_upload_auth foreign key (auth_id) references oauth_accounts_ttb(id) on delete cascade
        ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
        """
    )
    op.execute(
        "alter table website_ads_metrics_hourly "
        "add column video_play_actions bigint unsigned not null default 0 after clicks, "
        "add column video_watched_2s bigint unsigned not null default 0 after video_play_actions, "
        "add column video_watched_6s bigint unsigned not null default 0 after video_watched_2s, "
        "add column video_views_p25 bigint unsigned not null default 0 after video_watched_6s, "
        "add column video_views_p50 bigint unsigned not null default 0 after video_views_p25, "
        "add column video_views_p75 bigint unsigned not null default 0 after video_views_p50, "
        "add column video_views_p100 bigint unsigned not null default 0 after video_views_p75, "
        "add column average_video_play decimal(18,4) null after video_views_p100"
    )


def downgrade() -> None:
    op.execute(
        "alter table website_ads_metrics_hourly "
        "drop column average_video_play, "
        "drop column video_views_p100, "
        "drop column video_views_p75, "
        "drop column video_views_p50, "
        "drop column video_views_p25, "
        "drop column video_watched_6s, "
        "drop column video_watched_2s, "
        "drop column video_play_actions"
    )
    op.execute("drop table if exists website_ads_upload_fingerprints")
