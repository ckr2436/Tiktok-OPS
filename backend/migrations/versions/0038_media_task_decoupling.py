"""media task decoupling fields for whisper jobs

Revision ID: 0038_media_task_decoupling
Revises: 0037_add_creative_status_to_gmvmax_creative_metrics
Create Date: 2025-01-01 00:00:00.000001
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0038_media_task_decoupling"
down_revision = "0037_add_creative_status_to_gmvmax_creative_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "openai_whisper_jobs",
        sa.Column("do_subtitle", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "openai_whisper_jobs",
        sa.Column("do_contact_sheet", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "openai_whisper_jobs",
        sa.Column("do_download_only", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "openai_whisper_jobs", sa.Column("contact_interval", sa.Float(), nullable=True)
    )
    op.add_column(
        "openai_whisper_jobs",
        sa.Column(
            "subtitle_status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "openai_whisper_jobs",
        sa.Column(
            "contact_sheet_status", sa.String(length=32), nullable=False, server_default=sa.text("'skipped'"),
        ),
    )
    op.add_column(
        "openai_whisper_jobs",
        sa.Column(
            "download_status", sa.String(length=32), nullable=False, server_default=sa.text("'skipped'"),
        ),
    )
    op.add_column("openai_whisper_jobs", sa.Column("subtitle_error", sa.Text(), nullable=True))
    op.add_column(
        "openai_whisper_jobs", sa.Column("contact_sheet_error", sa.Text(), nullable=True)
    )
    op.add_column("openai_whisper_jobs", sa.Column("download_error", sa.Text(), nullable=True))
    op.add_column(
        "openai_whisper_jobs", sa.Column("contact_sheet_url", sa.String(length=1024), nullable=True)
    )
    op.add_column("openai_whisper_jobs", sa.Column("download_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("openai_whisper_jobs", "download_url")
    op.drop_column("openai_whisper_jobs", "contact_sheet_url")
    op.drop_column("openai_whisper_jobs", "download_error")
    op.drop_column("openai_whisper_jobs", "contact_sheet_error")
    op.drop_column("openai_whisper_jobs", "subtitle_error")
    op.drop_column("openai_whisper_jobs", "download_status")
    op.drop_column("openai_whisper_jobs", "contact_sheet_status")
    op.drop_column("openai_whisper_jobs", "subtitle_status")
    op.drop_column("openai_whisper_jobs", "contact_interval")
    op.drop_column("openai_whisper_jobs", "do_download_only")
    op.drop_column("openai_whisper_jobs", "do_contact_sheet")
    op.drop_column("openai_whisper_jobs", "do_subtitle")
