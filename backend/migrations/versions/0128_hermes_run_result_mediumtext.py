"""widen generic Hermes run results for complete model output

Revision ID: 0128_hermes_run_result_mediumtext
Revises: 0127_expand_ai_video_fail_code
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0128_hermes_run_result_mediumtext"
down_revision = "0127_expand_ai_video_fail_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "hermes_agent_runs",
        "result_text",
        existing_type=sa.Text(),
        type_=mysql.MEDIUMTEXT(),
        existing_nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.alter_column(
        "hermes_agent_runs",
        "result_text",
        existing_type=mysql.MEDIUMTEXT(),
        type_=sa.Text(),
        existing_nullable=True,
    )
