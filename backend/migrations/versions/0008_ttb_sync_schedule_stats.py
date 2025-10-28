"""add schedule run stats and seed ttb sync tasks

Revision ID: 0008_ttb_sync_schedule_stats
Revises: 0007_platform_policy_v1
Create Date: 2025-10-23 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0008_ttb_sync_schedule_stats"
down_revision = "0007_platform_policy_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "mysql":
        json_type = mysql_dialect.JSON()
    else:
        json_type = sa.JSON()

    op.add_column("schedule_runs", sa.Column("stats_json", json_type, nullable=True))

    # Seed task catalog entries for TikTok Business sync tasks
    task_rows = [
        {
            "task_name": name,
            "impl_version": 1,
            "input_schema_json": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "integer", "minimum": 1},
                    "auth_id": {"type": "integer", "minimum": 1},
                    "scope": {
                        "type": "string",
                        "enum": ["bc", "advertisers", "shops", "products", "all"],
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["full", "incremental"],
                        "default": "incremental",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                },
                "required": ["workspace_id", "auth_id"],
                "additionalProperties": True,
            },
            "default_queue": "gmv.tasks.events",
            "visibility": "tenant",
            "is_enabled": True,
        }
        for name in (
            "ttb.sync.bc",
            "ttb.sync.advertisers",
            "ttb.sync.shops",
            "ttb.sync.products",
            "ttb.sync.all",
        )
    ]

    for row in task_rows:
        op.execute(
            sa.text(
                "INSERT INTO task_catalog (task_name, impl_version, input_schema_json, default_queue, visibility, is_enabled) "
                "SELECT :task_name, :impl_version, :input_schema_json, :default_queue, :visibility, :is_enabled "
                "WHERE NOT EXISTS (SELECT 1 FROM task_catalog WHERE task_name = :task_name)"
            ),
            row,
        )


def downgrade() -> None:
    op.drop_column("schedule_runs", "stats_json")
    for name in ("ttb.sync.all", "ttb.sync.products", "ttb.sync.shops", "ttb.sync.advertisers", "ttb.sync.bc"):
        op.execute(sa.text("DELETE FROM task_catalog WHERE task_name = :name"), {"name": name})
