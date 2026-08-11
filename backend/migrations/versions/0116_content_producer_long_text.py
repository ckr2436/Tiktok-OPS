"""widen content producer and project briefs without silent truncation

Revision ID: 0116_content_producer_long_text
Revises: 0115_content_producer_attachments
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0116_content_producer_long_text"
down_revision = "0115_content_producer_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "hermes_agent_messages",
        "content_text",
        existing_type=sa.Text(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=True,
    )
    op.alter_column(
        "hermes_content_factory_projects",
        "product_brief",
        existing_type=sa.Text(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "hermes_content_factory_projects",
        "product_brief",
        existing_type=mysql.MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "hermes_agent_messages",
        "content_text",
        existing_type=mysql.MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )
