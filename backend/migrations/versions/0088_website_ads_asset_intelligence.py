"""Persist automatic Website Ads creative analysis evidence.

Revision ID: 0088_website_ads_asset_intelligence
Revises: 0087_website_ads_content_product_tracking
"""

from alembic import op


revision = "0088_website_ads_asset_intelligence"
down_revision = "0087_website_ads_content_product_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table website_ads_creative_assets "
        "add column analysis_inputs_json json null after hermes_analysis_json, "
        "add column analysis_version varchar(64) null after analysis_inputs_json, "
        "add column analysis_attempts int not null default 0 after analysis_error, "
        "add column analysis_next_retry_at datetime(6) null after analysis_attempts, "
        "add column transcript_text longtext null after analysis_next_retry_at, "
        "add column transcript_language varchar(32) null after transcript_text, "
        "add column contact_sheet_url varchar(1024) null after transcript_language"
    )
    op.execute(
        "create index idx_web_ads_asset_analysis_queue "
        "on website_ads_creative_assets (analysis_status, analysis_next_retry_at)"
    )


def downgrade() -> None:
    op.execute("drop index idx_web_ads_asset_analysis_queue on website_ads_creative_assets")
    op.execute(
        "alter table website_ads_creative_assets "
        "drop column contact_sheet_url, "
        "drop column transcript_language, "
        "drop column transcript_text, "
        "drop column analysis_next_retry_at, "
        "drop column analysis_attempts, "
        "drop column analysis_version, "
        "drop column analysis_inputs_json"
    )
