"""Create table for OpenAI Whisper jobs"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0030_openai_whisper_jobs"
down_revision = "0029_ttb_gmvmax_status_lengths"
branch_labels = None
depends_on = None


MYSQL_DATETIME = sa.dialects.mysql.DATETIME
MYSQL_BIGINT = sa.dialects.mysql.BIGINT


def upgrade() -> None:
    sqlite_integer = sa.Integer()
    op.create_table(
        "openai_whisper_jobs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql").with_variant(sqlite_integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("job_id", sa.String(length=32), nullable=False, unique=True),
        sa.Column(
            "workspace_id",
            sa.BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql").with_variant(sqlite_integer, "sqlite"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql").with_variant(sqlite_integer, "sqlite"),
            nullable=True,
        ),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column(
            "file_size",
            sa.BigInteger().with_variant(MYSQL_BIGINT(unsigned=True), "mysql").with_variant(sqlite_integer, "sqlite"),
            nullable=True,
        ),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("video_path", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("source_language", sa.String(length=32), nullable=True),
        sa.Column("detected_language", sa.String(length=32), nullable=True),
        sa.Column("target_language", sa.String(length=32), nullable=True),
        sa.Column("translation_language", sa.String(length=32), nullable=True),
        sa.Column("translate", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("show_bilingual", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("segments_count", sa.Integer(), nullable=True),
        sa.Column("translation_segments_count", sa.Integer(), nullable=True),
        sa.Column("started_at", MYSQL_DATETIME(fsp=6), nullable=True),
        sa.Column("completed_at", MYSQL_DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", MYSQL_DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column(
            "updated_at",
            MYSQL_DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], onupdate="RESTRICT", ondelete="SET NULL"),
        sa.Index("ix_openai_whisper_jobs_workspace", "workspace_id"),
        sa.Index("ix_openai_whisper_jobs_user", "user_id"),
        sa.Index("ix_openai_whisper_jobs_job_id", "job_id", unique=True),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("openai_whisper_jobs")
