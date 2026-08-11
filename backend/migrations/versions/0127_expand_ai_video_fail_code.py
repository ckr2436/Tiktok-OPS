"""expand AI video structured failure codes

Revision ID: 0127_expand_ai_video_fail_code
Revises: 0126_content_stage_response_mediumtext
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0127_expand_ai_video_fail_code"
down_revision = "0126_content_stage_response_mediumtext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "kie_api_tasks",
        "fail_code",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "kie_api_tasks",
        "fail_code",
        existing_type=sa.String(length=128),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
