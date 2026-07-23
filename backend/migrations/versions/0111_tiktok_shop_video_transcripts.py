"""Add reusable Whisper evidence to video content analyses.

Revision ID: 0111_ttshop_video_transcript
Revises: 0110_tiktok_shop_video_content_analysis
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0111_ttshop_video_transcript"
down_revision = "0110_tiktok_shop_video_content_analysis"
branch_labels = None
depends_on = None


_COLUMNS = (
    sa.Column("transcript_status", sa.String(24), nullable=False, server_default="PENDING"),
    sa.Column("transcript_source", sa.String(64), nullable=True),
    sa.Column("transcript_language", sa.String(32), nullable=True),
    sa.Column("transcript_text", sa.Text(), nullable=True),
    sa.Column("transcript_segments_json", sa.JSON(), nullable=True),
    sa.Column("transcript_reason", sa.String(64), nullable=True),
    sa.Column("transcript_error_message", sa.Text(), nullable=True),
    sa.Column("transcript_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("transcript_started_at", sa.DateTime(), nullable=True),
    sa.Column("transcript_completed_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "tiktok_shop_video_content_analyses"
    actual = {str(item["name"]) for item in inspector.get_columns(table)}
    for column in _COLUMNS:
        if column.name not in actual:
            op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "tiktok_shop_video_content_analyses"
    actual = {str(item["name"]) for item in inspector.get_columns(table)}
    for column in reversed(_COLUMNS):
        if column.name in actual:
            op.drop_column(table, column.name)
