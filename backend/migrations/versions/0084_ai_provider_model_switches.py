"""add platform provider model switches

Revision ID: 0084_ai_provider_model_switches
Revises: 0083_gmv_guard_runtime_state
Create Date: 2026-07-11 15:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0084_ai_provider_model_switches"
down_revision = "0083_gmv_guard_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_provider_model_settings" not in set(inspector.get_table_names()):
        op.create_table(
            "ai_provider_model_settings",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("provider_key", sa.String(length=32), nullable=False),
            sa.Column("model_id", sa.String(length=128), nullable=False),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("provider_key", "model_id", name="uk_ai_provider_model_setting"),
        )
        op.create_index(
            "idx_ai_provider_model_enabled",
            "ai_provider_model_settings",
            ["model_id", "is_enabled"],
        )
    table = sa.table(
        "ai_provider_model_settings",
        sa.column("provider_key", sa.String()),
        sa.column("model_id", sa.String()),
        sa.column("is_enabled", sa.Boolean()),
    )
    existing = bind.execute(
        sa.select(table.c.provider_key).where(
            table.c.provider_key == "toapis",
            table.c.model_id == "omni_flash",
        )
    ).first()
    if existing is None:
        op.bulk_insert(table, [{"provider_key": "toapis", "model_id": "omni_flash", "is_enabled": False}])


def downgrade() -> None:
    bind = op.get_bind()
    if "ai_provider_model_settings" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("ai_provider_model_settings")
