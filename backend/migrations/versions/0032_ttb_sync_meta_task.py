"""
Add TikTok Business meta sync task to task_catalog
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0032_ttb_sync_meta_task"
down_revision = "0031_ttb_gmvmax_campaign_dedupe"
branch_labels = None
depends_on = None


def _input_schema(scopes: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "workspace_id": {"type": "integer", "minimum": 1},
            "auth_id": {"type": "integer", "minimum": 1},
            "scope": {"type": "string", "enum": scopes},
            "mode": {
                "type": "string",
                "enum": ["full", "incremental"],
                "default": "incremental",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
        },
        "required": ["workspace_id", "auth_id"],
        "additionalProperties": True,
    }


def _upsert_task_rows(bind, rows: list[dict]) -> None:
    dialect = bind.dialect.name
    meta = sa.MetaData()
    task_catalog = sa.Table("task_catalog", meta, autoload_with=bind)

    for row in rows:
        exists_q = sa.select(task_catalog.c.id).where(
            task_catalog.c.task_name == row["task_name"]
        )
        exists = bind.execute(exists_q).first()

        payload = dict(row)
        if dialect == "sqlite":
            payload["input_schema_json"] = json.dumps(payload["input_schema_json"])

        if exists:
            update_payload = {
                "impl_version": payload["impl_version"],
                "input_schema_json": payload["input_schema_json"],
                "default_queue": payload["default_queue"],
                "visibility": payload["visibility"],
                "is_enabled": payload["is_enabled"],
            }
            upd = (
                task_catalog.update()
                .where(task_catalog.c.task_name == payload["task_name"])
                .values(**update_payload)
            )
            bind.execute(upd)
        else:
            ins = task_catalog.insert()
            bind.execute(ins, payload)


def upgrade() -> None:
    bind = op.get_bind()

    scopes = ["bc", "advertisers", "stores", "products", "all", "meta"]
    schema = _input_schema(scopes)

    task_rows = [
        {
            "task_name": name,
            "impl_version": 1,
            "input_schema_json": schema,
            "default_queue": "gmv.tasks.events",
            "visibility": "tenant",
            "is_enabled": True,
        }
        for name in (
            "ttb.sync.bc",
            "ttb.sync.advertisers",
            "ttb.sync.stores",
            "ttb.sync.products",
            "ttb.sync.all",
            "ttb.sync.meta",
        )
    ]

    _upsert_task_rows(bind, task_rows)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    meta = sa.MetaData()
    task_catalog = sa.Table("task_catalog", meta, autoload_with=bind)

    bind.execute(task_catalog.delete().where(task_catalog.c.task_name == "ttb.sync.meta"))

    scopes = ["bc", "advertisers", "stores", "products", "all"]
    schema = _input_schema(scopes)
    payload = schema if dialect != "sqlite" else json.dumps(schema)

    upd = (
        task_catalog.update()
        .where(task_catalog.c.task_name.in_(
            [
                "ttb.sync.bc",
                "ttb.sync.advertisers",
                "ttb.sync.stores",
                "ttb.sync.products",
                "ttb.sync.all",
            ]
        ))
        .values(input_schema_json=payload)
    )
    bind.execute(upd)
