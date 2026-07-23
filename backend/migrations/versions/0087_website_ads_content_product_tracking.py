"""Link Website Ads products to Content Factory products.

Revision ID: 0087_website_ads_content_product_tracking
Revises: 0086_website_ads_hermes_planner
"""

from alembic import op


revision = "0087_website_ads_content_product_tracking"
down_revision = "0086_website_ads_hermes_planner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table website_ads_landing_pages "
        "add column content_product_id bigint unsigned null after connection_id"
    )
    op.execute(
        "alter table website_ads_landing_pages "
        "add constraint fk_web_ads_landing_content_product "
        "foreign key (content_product_id) references hermes_content_products(id) on delete set null"
    )
    op.execute(
        "create index idx_web_ads_landing_content_product "
        "on website_ads_landing_pages (workspace_id, content_product_id)"
    )


def downgrade() -> None:
    op.execute("drop index idx_web_ads_landing_content_product on website_ads_landing_pages")
    op.execute("alter table website_ads_landing_pages drop foreign key fk_web_ads_landing_content_product")
    op.execute("alter table website_ads_landing_pages drop column content_product_id")
