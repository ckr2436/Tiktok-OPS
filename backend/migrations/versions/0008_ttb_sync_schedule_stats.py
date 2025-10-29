# backend/migrations/versions/0008_ttb_sync_schedule_stats.py
"""add schedule run stats and seed ttb sync tasks

Revision ID: 0008_ttb_sync_schedule_stats
Revises: 0007_platform_policy_v1
Create Date: 2025-10-23 00:00:00
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect


revision = "0008_ttb_sync_schedule_stats"
down_revision = "0007_platform_policy_v1"
branch_labels = None
depends_on = None


def _has_column(bind, table_name: str, column_name: str) -> bool:
    insp = sa.inspect(bind)
    return any(col["name"] == column_name for col in insp.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # choose proper JSON type
    json_type = mysql_dialect.JSON() if dialect == "mysql" else sa.JSON()

    # ---- add column if missing (idempotent) ----
    if not _has_column(bind, "schedule_runs", "stats_json"):
        op.add_column("schedule_runs", sa.Column("stats_json", json_type, nullable=True))

    # ---- seed / upsert task_catalog for TikTok Business sync tasks (idempotent) ----
    meta = sa.MetaData()
    task_catalog = sa.Table("task_catalog", meta, autoload_with=bind)

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
        for name in ("ttb.sync.bc", "ttb.sync.advertisers", "ttb.sync.shops", "ttb.sync.products", "ttb.sync.all")
    ]

    # Prefer SQLAlchemy Core so JSON/datatypes are handled automatically
    for row in task_rows:
        # exists?
        exists_q = sa.select(task_catalog.c.id).where(task_catalog.c.task_name == row["task_name"]).limit(1)
        exists = bind.execute(exists_q).first()

        if exists:
            update_payload = {
                "impl_version": row["impl_version"],
                "input_schema_json": row["input_schema_json"],
                "default_queue": row["default_queue"],
                "visibility": row["visibility"],
                "is_enabled": row["is_enabled"],
            }
            if dialect == "sqlite":
                update_payload["input_schema_json"] = json.dumps(update_payload["input_schema_json"])
            upd = (
                task_catalog.update()
                .where(task_catalog.c.task_name == row["task_name"])
                .values(**update_payload)
            )
            bind.execute(upd)
        else:
            ins = task_catalog.insert()
            payload = dict(row)
            if dialect == "sqlite":
                payload["input_schema_json"] = json.dumps(payload["input_schema_json"])
            bind.execute(ins, payload)


def downgrade() -> None:
    bind = op.get_bind()

    # drop column if exists
    if _has_column(bind, "schedule_runs", "stats_json"):
        op.drop_column("schedule_runs", "stats_json")

    # remove seeded tasks
    meta = sa.MetaData()
    task_catalog = sa.Table("task_catalog", meta, autoload_with=bind)
    names = ("ttb.sync.all", "ttb.sync.products", "ttb.sync.shops", "ttb.sync.advertisers", "ttb.sync.bc")
    del_q = task_catalog.delete().where(task_catalog.c.task_name.in_(names))
    bind.execute(del_q)

