"""Add the Website Ads automatic creative expansion state machine.

Revision ID: 0090_website_ads_asset_auto_expansion
Revises: 0089_website_ads_upload_dedupe_video_metrics
"""

from alembic import op


revision = "0090_website_ads_asset_auto_expansion"
down_revision = "0089_website_ads_upload_dedupe_video_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table website_ads_creative_assets "
        "add column auto_launch_status varchar(32) not null default 'PENDING' after analyzed_at, "
        "add column auto_launch_attempts int not null default 0 after auto_launch_status, "
        "add column auto_launch_next_retry_at datetime(6) null after auto_launch_attempts, "
        "add column auto_launch_decision_json json null after auto_launch_next_retry_at, "
        "add column auto_launch_error text null after auto_launch_decision_json, "
        "add column auto_launched_at datetime(6) null after auto_launch_error, "
        "add key idx_web_ads_asset_auto_launch (auto_launch_status, auto_launch_next_retry_at)"
    )


def downgrade() -> None:
    op.execute(
        "alter table website_ads_creative_assets "
        "drop key idx_web_ads_asset_auto_launch, "
        "drop column auto_launched_at, "
        "drop column auto_launch_error, "
        "drop column auto_launch_decision_json, "
        "drop column auto_launch_next_retry_at, "
        "drop column auto_launch_attempts, "
        "drop column auto_launch_status"
    )
