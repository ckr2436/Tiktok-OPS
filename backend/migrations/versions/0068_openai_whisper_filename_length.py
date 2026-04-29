"""Widen OpenAI Whisper job filename.

Revision ID: 0068_openai_whisper_filename_length
Revises: 0067_remove_video_site_login_debug_logs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0068_openai_whisper_filename_length"
down_revision = "0067_remove_video_site_login_debug_logs"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    if _column_exists("openai_whisper_jobs", "filename"):
        op.alter_column(
            "openai_whisper_jobs",
            "filename",
            existing_type=sa.String(length=255),
            type_=sa.String(length=1024),
            existing_nullable=True,
        )


def downgrade() -> None:
    if _column_exists("openai_whisper_jobs", "filename"):
        op.execute("UPDATE openai_whisper_jobs SET filename = LEFT(filename, 255) WHERE CHAR_LENGTH(filename) > 255")
        op.alter_column(
            "openai_whisper_jobs",
            "filename",
            existing_type=sa.String(length=1024),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
