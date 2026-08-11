"""widen content-factory stage responses for compiled production packets

Revision ID: 0126_content_stage_response_mediumtext
Revises: 0125_tiktok_shop_content_posting
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0126_content_stage_response_mediumtext"
down_revision = "0125_tiktok_shop_content_posting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "hermes_content_factory_stages",
        "response_text",
        existing_type=sa.Text(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "hermes_content_factory_stages",
        "response_text",
        existing_type=mysql.MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )
