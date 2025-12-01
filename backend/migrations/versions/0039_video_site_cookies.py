"""add video site cookies table

Revision ID: 0039_video_site_cookies
Revises: 0038_media_task_decoupling
Create Date: 2025-03-16 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0039_video_site_cookies"
down_revision = "0038_media_task_decoupling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_site_cookies",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("site", sa.String(length=32), nullable=False, index=True),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("cookies_json", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "idx_video_site_cookies_site_active",
        "video_site_cookies",
        ["site", "is_active"],
    )
    op.create_unique_constraint(
        "uq_video_site_cookies_site_label", "video_site_cookies", ["site", "label"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_video_site_cookies_site_label", "video_site_cookies", type_="unique")
    op.drop_index("idx_video_site_cookies_site_active", table_name="video_site_cookies")
    op.drop_table("video_site_cookies")
