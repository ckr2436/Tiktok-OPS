"""Add domain column to platform policies"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_platform_policy_domain"
down_revision = "0002_provider_and_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ttb_platform_policies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("domain", sa.String(length=255), nullable=True))
        batch_op.create_unique_constraint(
            "uq_platform_policy_provider_mode_domain",
            ["provider_key", "mode", "domain"],
        )


def downgrade() -> None:
    with op.batch_alter_table("ttb_platform_policies", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_platform_policy_provider_mode_domain", type_="unique"
        )
        batch_op.drop_column("domain")
