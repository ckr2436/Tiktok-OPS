"""Add platform email settings table.

This script is safe to re-run (checks existence before creating/dropping).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "0011_platform_email_settings"
down_revision = "0010_scheduler_idempotency_and_indexes"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    insp = inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade() -> None:
    if not _has_table("platform_email_settings"):
        op.create_table(
            "platform_email_settings",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("send_mode", sa.Enum("SMTP", name="mail_send_mode"), nullable=False),
            sa.Column(
                "encryption",
                sa.Enum("SSL", "STARTTLS", "NONE", name="mail_encryption"),
                nullable=False,
                server_default="SSL",
            ),
            sa.Column("from_address", sa.String(length=255), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("auth_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("username", sa.String(length=255), nullable=True),
            sa.Column("password", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _has_table("platform_email_settings"):
        op.drop_table("platform_email_settings")
    bind = op.get_bind()
    try:
        enums = inspect(bind).get_enums()
    except NotImplementedError:
        enums = []
    names = {e.get("name") for e in enums if isinstance(e, dict)}
    if "mail_send_mode" in names:
        op.execute(sa.text("DROP TYPE IF EXISTS mail_send_mode"))
    if "mail_encryption" in names:
        op.execute(sa.text("DROP TYPE IF EXISTS mail_encryption"))
