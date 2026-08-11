"""add KIE task list pagination indexes

Revision ID: 0073_kie_task_list_indexes
Revises: 0072_hermes_browser_bridges
Create Date: 2026-07-04 14:15:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0073_kie_task_list_indexes"
down_revision = "0072_hermes_browser_bridges"
branch_labels = None
depends_on = None


INDEXES = (
    ("idx_kie_task_ws_id", ("workspace_id", "id")),
    ("idx_kie_task_ws_user_id", ("workspace_id", "created_by_user_id", "id")),
    ("idx_kie_task_ws_model_id", ("workspace_id", "model", "id")),
    ("idx_kie_task_ws_user_model_id", ("workspace_id", "created_by_user_id", "model", "id")),
    ("idx_kie_task_ws_state_model_id", ("workspace_id", "state", "model", "id")),
    ("idx_kie_task_ws_user_state_model_id", ("workspace_id", "created_by_user_id", "state", "model", "id")),
)


def _existing_index_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {str(index["name"]) for index in inspector.get_indexes("kie_api_tasks")}


def upgrade() -> None:
    existing = _existing_index_names()
    for name, columns in INDEXES:
        if name not in existing:
            op.create_index(name, "kie_api_tasks", list(columns))


def downgrade() -> None:
    existing = _existing_index_names()
    for name, _columns in reversed(INDEXES):
        if name in existing:
            op.drop_index(name, table_name="kie_api_tasks")
