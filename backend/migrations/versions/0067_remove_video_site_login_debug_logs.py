"""Remove debug logs from video site login sessions.

Revision ID: 0067_remove_video_site_login_debug_logs
Revises: 0066_video_site_login_debug_logs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0067_remove_video_site_login_debug_logs"
down_revision = "0066_video_site_login_debug_logs"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    if _column_exists("video_site_login_sessions", "debug_logs"):
        op.drop_column("video_site_login_sessions", "debug_logs")


def downgrade() -> None:
    if not _column_exists("video_site_login_sessions", "debug_logs"):
        op.add_column("video_site_login_sessions", sa.Column("debug_logs", sa.JSON(), nullable=True))
