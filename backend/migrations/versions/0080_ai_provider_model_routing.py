"""add scoped multi-provider video model routing

Revision ID: 0080_ai_provider_model_routing
Revises: 0079_align_gmv_metric_columns
Create Date: 2026-07-10 20:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0080_ai_provider_model_routing"
down_revision = "0079_align_gmv_metric_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("kie_api_keys")}
    if "scopes_json" not in columns:
        op.add_column("kie_api_keys", sa.Column("scopes_json", sa.JSON(), nullable=True))
    if "model_priorities_json" not in columns:
        op.add_column("kie_api_keys", sa.Column("model_priorities_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("kie_api_keys")}
    if "model_priorities_json" in columns:
        op.drop_column("kie_api_keys", "model_priorities_json")
    if "scopes_json" in columns:
        op.drop_column("kie_api_keys", "scopes_json")
