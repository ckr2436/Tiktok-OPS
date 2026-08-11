"""Add display_timezone to TTB advertisers.

Revision ID: 0013_ttb_advertiser_display_timezone
Revises: 0012_ttb_binding_config
Create Date: 2025-11-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "0013_ttb_advertiser_display_timezone"
down_revision = "0012_ttb_binding_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind else ""

    column_type = sa.String(length=64)
    if dialect == "mysql":
        column_type = mysql.VARCHAR(length=64)
    inspector = inspect(bind)
    if not inspector.has_table("ttb_advertisers"):
        return
    existing_columns = {col["name"] for col in inspector.get_columns("ttb_advertisers")}
    if "display_timezone" in existing_columns:
        return

    op.add_column("ttb_advertisers", sa.Column("display_timezone", column_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("ttb_advertisers"):
        return
    existing_columns = {col["name"] for col in inspector.get_columns("ttb_advertisers")}
    if "display_timezone" not in existing_columns:
        return
    op.drop_column("ttb_advertisers", "display_timezone")
