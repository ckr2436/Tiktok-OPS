"""Repair the OAuth provider schema after restoring an older production backup.

Revision ID: 0091_oauth_provider_service_id_repair
Revises: 0090_website_ads_asset_auto_expansion
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0091_oauth_provider_service_id_repair"
down_revision = "0090_website_ads_asset_auto_expansion"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("oauth_provider_apps")
    }


def upgrade() -> None:
    if "service_id" not in _columns():
        op.add_column(
            "oauth_provider_apps",
            sa.Column("service_id", sa.String(length=128), nullable=True),
        )


def downgrade() -> None:
    if "service_id" in _columns():
        op.drop_column("oauth_provider_apps", "service_id")
