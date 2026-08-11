"""Persist recoverable user pause intents for GMV Max campaigns.

Revision ID: 0104_gmvmax_campaign_pause_intents
Revises: 0103_gmvmax_campaign_create_intents
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0104_gmvmax_campaign_pause_intents"
down_revision = "0103_gmvmax_campaign_create_intents"
branch_labels = None
depends_on = None


TABLE_NAME = "gmvmax_campaign_pause_intents"
UNIQUE_NAME = "uq_gmvmax_pause_intent_active"
DUE_INDEX = "idx_gmvmax_pause_intent_due"
SCOPE_INDEX = "idx_gmvmax_pause_intent_scope"
UNIQUE_COLUMNS = ["active_key"]
DUE_COLUMNS = ["status", "next_attempt_at", "lease_expires_at"]
SCOPE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "campaign_id",
]
REQUIRED_COLUMNS = {
    "id",
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "campaign_id",
    "active_key",
    "status",
    "actor",
    "reason",
    "attempt_count",
    "next_attempt_at",
    "lease_owner",
    "lease_expires_at",
    "last_error",
    "completed_at",
    "created_at",
    "updated_at",
}
UBIGINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _named_columns(items: list[dict], name: str) -> list[str] | None:
    for item in items:
        if str(item.get("name") or "") == name:
            return [str(value) for value in item.get("column_names") or []]
    return None


def _ensure_unique_constraint() -> None:
    actual = _named_columns(_inspector().get_unique_constraints(TABLE_NAME), UNIQUE_NAME)
    if actual == UNIQUE_COLUMNS:
        return
    if actual is not None:
        raise RuntimeError(f"{UNIQUE_NAME} has unexpected columns: {actual!r}")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(TABLE_NAME) as batch:
            batch.create_unique_constraint(UNIQUE_NAME, UNIQUE_COLUMNS)
    else:
        op.create_unique_constraint(UNIQUE_NAME, TABLE_NAME, UNIQUE_COLUMNS)


def _ensure_index(name: str, columns: list[str]) -> None:
    actual = _named_columns(_inspector().get_indexes(TABLE_NAME), name)
    if actual == columns:
        return
    if actual is not None:
        op.drop_index(name, table_name=TABLE_NAME)
    op.create_index(name, TABLE_NAME, columns)


def _repair_contract() -> None:
    columns = {str(item["name"]) for item in _inspector().get_columns(TABLE_NAME)}
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            f"{TABLE_NAME} exists with an incomplete schema; missing columns: "
            + ", ".join(missing)
        )
    _ensure_unique_constraint()
    _ensure_index(DUE_INDEX, DUE_COLUMNS)
    _ensure_index(SCOPE_INDEX, SCOPE_COLUMNS)


def upgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=True),
            sa.Column("campaign_id", sa.String(length=64), nullable=False),
            sa.Column("active_key", sa.String(length=64), nullable=True),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'PENDING'"),
            ),
            sa.Column("actor", sa.String(length=191), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("next_attempt_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column(
                "created_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column(
                "updated_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.UniqueConstraint(*UNIQUE_COLUMNS, name=UNIQUE_NAME),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
    _repair_contract()


def downgrade() -> None:
    if TABLE_NAME in set(_inspector().get_table_names()):
        op.drop_table(TABLE_NAME)
