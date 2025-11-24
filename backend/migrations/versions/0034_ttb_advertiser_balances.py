"""add advertiser balance table and schedule link

Revision ID: 0034_ttb_advertiser_balances
Revises: 0033_platform_email_settings
Create Date: 2024-06-05 00:00:00.000000

"""
from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "0034_ttb_advertiser_balances"
down_revision = "0033_platform_email_settings"
branch_labels = None
depends_on = None


BALANCE_TABLE = "ttb_advertiser_balances"
BINDING_TABLE = "ttb_binding_configs"
TASK_CATALOG = "task_catalog"
TASK_NAME = "gmvmax.sync_advertiser_balance"


def _has_table(name: str) -> bool:
    insp = inspect(op.get_bind())
    return name in insp.get_table_names()


def _table(name: str) -> sa.Table:
    meta = sa.MetaData()
    meta.bind = op.get_bind()
    return sa.Table(name, meta, autoload_with=op.get_bind())


def _seed_task_catalog() -> None:
    if not _has_table(TASK_CATALOG):
        return
    task_catalog = _table(TASK_CATALOG)
    bind = op.get_bind()
    dialect = bind.dialect.name

    payload: dict[str, Any] = {
        "task_name": TASK_NAME,
        "impl_version": 1,
        "input_schema_json": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "integer"},
                "auth_id": {"type": "integer"},
                "advertiser_id": {"type": "string"},
                "bc_id": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["workspace_id", "auth_id", "advertiser_id", "bc_id"],
            "additionalProperties": True,
        },
        "default_queue": "gmvmax",
        "visibility": "tenant",
        "is_enabled": True,
    }
    exists = bind.execute(
        sa.select(task_catalog.c.id).where(task_catalog.c.task_name == payload["task_name"]).limit(1)
    ).first()
    serialized_payload = dict(payload)
    if dialect == "sqlite":
        serialized_payload["input_schema_json"] = json.dumps(serialized_payload["input_schema_json"])
    if exists:
        bind.execute(
            task_catalog.update()
            .where(task_catalog.c.task_name == payload["task_name"])
            .values(**serialized_payload)
        )
    else:
        bind.execute(task_catalog.insert().values(**serialized_payload))


def upgrade() -> None:
    if not _has_table(BALANCE_TABLE):
        op.create_table(
            BALANCE_TABLE,
            sa.Column(
                "id",
                sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                primary_key=True,
                autoincrement=True,
            ),
            sa.Column("workspace_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
            sa.Column("auth_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=True),
            sa.Column("account_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("valid_account_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("cash_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("valid_cash_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("credit_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("valid_credit_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("workspace_id", "auth_id", "advertiser_id", name="uk_ttb_balance_scope"),
        )
        op.create_index("idx_ttb_balance_scope", BALANCE_TABLE, ["workspace_id", "auth_id", "advertiser_id"])
        op.create_index("idx_ttb_balance_time", BALANCE_TABLE, ["fetched_at"])

    if _has_table(BINDING_TABLE):
        op.add_column(
            BINDING_TABLE,
            sa.Column("balance_sync_schedule_id", sa.BigInteger(), nullable=True),
        )
        try:
            op.create_foreign_key(
                None,
                BINDING_TABLE,
                "schedules",
                ["balance_sync_schedule_id"],
                ["id"],
                onupdate="RESTRICT",
                ondelete="SET NULL",
            )
        except Exception:
            # Some databases (sqlite) may not support adding FK after table creation; ignore gracefully.
            pass

    _seed_task_catalog()


def downgrade() -> None:
    if _has_table(TASK_CATALOG):
        task_catalog = _table(TASK_CATALOG)
        op.get_bind().execute(task_catalog.delete().where(task_catalog.c.task_name == TASK_NAME))

    if _has_table(BINDING_TABLE):
        with op.batch_alter_table(BINDING_TABLE) as batch:
            batch.drop_column("balance_sync_schedule_id")

    if _has_table(BALANCE_TABLE):
        op.drop_index("idx_ttb_balance_scope", table_name=BALANCE_TABLE)
        op.drop_index("idx_ttb_balance_time", table_name=BALANCE_TABLE)
        op.drop_table(BALANCE_TABLE)
