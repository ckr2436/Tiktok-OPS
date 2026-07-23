"""Add durable idempotency intents for GMV Max campaign creation.

Revision ID: 0103_gmvmax_campaign_create_intents
Revises: 0102_gmv_creative_10min_batch_manifest
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0103_gmvmax_campaign_create_intents"
down_revision = "0102_gmv_creative_10min_batch_manifest"
branch_labels = None
depends_on = None


TABLE_NAME = "gmvmax_campaign_create_intents"
UNIQUE_NAME = "uk_gmvmax_create_intent_idem"
SCOPE_INDEX = "idx_gmvmax_create_intent_scope"
STATE_INDEX = "idx_gmvmax_create_intent_state"
UNIQUE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "idempotency_key",
]
SCOPE_COLUMNS = ["workspace_id", "auth_id", "advertiser_id", "store_id"]
STATE_COLUMNS = ["workspace_id", "auth_id", "state", "updated_at"]
REQUIRED_COLUMNS = {
    "id",
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "idempotency_key",
    "client_payload_sha256",
    "payload_sha256",
    "official_request_id",
    "campaign_name",
    "replacement_campaign_id",
    "campaign_id",
    "state",
    "request_json",
    "result_json",
    "error_json",
    "submitted_at",
    "remote_created_at",
    "finalized_at",
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
    actual = _named_columns(
        _inspector().get_unique_constraints(TABLE_NAME),
        UNIQUE_NAME,
    )
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
    actual_columns = {
        str(column["name"]) for column in _inspector().get_columns(TABLE_NAME)
    }
    missing = sorted(REQUIRED_COLUMNS - actual_columns)
    if missing:
        raise RuntimeError(
            f"{TABLE_NAME} exists with an incomplete schema; "
            f"missing columns: {', '.join(missing)}"
        )
    _ensure_unique_constraint()
    _ensure_index(SCOPE_INDEX, SCOPE_COLUMNS)
    _ensure_index(STATE_INDEX, STATE_COLUMNS)


def upgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("idempotency_key", sa.String(length=64), nullable=False),
            sa.Column("client_payload_sha256", sa.String(length=64), nullable=False),
            sa.Column("payload_sha256", sa.String(length=64), nullable=False),
            sa.Column("official_request_id", sa.String(length=64), nullable=False),
            sa.Column("campaign_name", sa.String(length=255), nullable=False),
            sa.Column("replacement_campaign_id", sa.String(length=64)),
            sa.Column("campaign_id", sa.String(length=64)),
            sa.Column(
                "state",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'PREPARED'"),
            ),
            sa.Column("request_json", sa.JSON(), nullable=False),
            sa.Column("result_json", sa.JSON()),
            sa.Column("error_json", sa.JSON()),
            sa.Column("submitted_at", mysql.DATETIME(fsp=6)),
            sa.Column("remote_created_at", mysql.DATETIME(fsp=6)),
            sa.Column("finalized_at", mysql.DATETIME(fsp=6)),
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
