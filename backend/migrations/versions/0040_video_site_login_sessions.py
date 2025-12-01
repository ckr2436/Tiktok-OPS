"""
Add video_site_login_sessions table

Revision ID: 0040_video_site_login_sessions
Revises: 0039_video_site_cookies
Create Date: 2025-03-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0040_video_site_login_sessions"
down_revision = "0039_video_site_cookies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_site_login_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("site", sa.String(length=32), nullable=False, index=True),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, index=True),
        sa.Column("qrcode_image_base64", sa.Text(), nullable=True),
        sa.Column("account", sa.JSON(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "idx_video_site_login_sessions_site_status",
        "video_site_login_sessions",
        ["site", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_video_site_login_sessions_site_status",
        table_name="video_site_login_sessions",
    )
    op.drop_table("video_site_login_sessions")
