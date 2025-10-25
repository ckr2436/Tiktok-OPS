"""Normalize policy mode casing and defaults."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_normalize_policy_mode"
down_revision = "0004_seed_platform_providers"
branch_labels = None
depends_on = None


POLICY_MODE_ENUM_UPPER = sa.Enum("WHITELIST", "BLACKLIST", name="ttb_policy_mode")


def upgrade() -> None:
    bind = op.get_bind()

    op.execute(sa.text("UPDATE ttb_platform_policies SET mode = UPPER(TRIM(mode)) WHERE mode IS NOT NULL"))

    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE ttb_platform_policies "
            "MODIFY COLUMN mode ENUM('WHITELIST','BLACKLIST') NOT NULL DEFAULT 'WHITELIST'"
        )
    else:
        op.alter_column(
            "ttb_platform_policies",
            "mode",
            existing_type=POLICY_MODE_ENUM_UPPER,
            nullable=False,
            server_default=sa.text("'WHITELIST'"),
        )


def downgrade() -> None:
    bind = op.get_bind()

    op.execute(sa.text("UPDATE ttb_platform_policies SET mode = LOWER(TRIM(mode)) WHERE mode IS NOT NULL"))

    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE ttb_platform_policies "
            "MODIFY COLUMN mode ENUM('whitelist','blacklist') NOT NULL"
        )
    else:
        op.alter_column(
            "ttb_platform_policies",
            "mode",
            existing_type=POLICY_MODE_ENUM_UPPER,
            server_default=None,
        )
