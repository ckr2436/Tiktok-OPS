"""Add whole-batch completeness watermarks for creative 10-minute metrics.

Revision ID: 0102_gmv_creative_10min_batch_manifest
Revises: 0101_ttb_product_advertiser_eligibility
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0102_gmv_creative_10min_batch_manifest"
down_revision = "0101_ttb_product_advertiser_eligibility"
branch_labels = None
depends_on = None


TABLE_NAME = "gmv_creative_10min_batch_manifests"
UNIQUE_NAME = "uk_gmv_creative_10min_batch_scope"
LATEST_INDEX = "idx_gmv_creative_10min_batch_latest"
SCOPE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "campaign_id",
    "stat_time_day",
    "snapshot_at",
]
LATEST_INDEX_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "campaign_id",
    "stat_time_day",
    "complete",
    "snapshot_at",
]
UBIGINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _named_columns(items: list[dict], name: str) -> list[str] | None:
    for item in items:
        if str(item.get("name") or "") == name:
            return [str(value) for value in item.get("column_names") or []]
    return None


def _repair_contract() -> None:
    inspector = _inspector()
    actual_columns = {
        str(column["name"]) for column in inspector.get_columns(TABLE_NAME)
    }
    required_columns = {
        "id",
        *SCOPE_COLUMNS,
        "complete",
        "row_count",
        "source_observed_at",
        "created_at",
        "updated_at",
    }
    missing = sorted(required_columns - actual_columns)
    if missing:
        raise RuntimeError(
            f"{TABLE_NAME} exists with an incomplete schema; "
            f"missing columns: {', '.join(missing)}"
        )

    unique_columns = _named_columns(
        inspector.get_unique_constraints(TABLE_NAME),
        UNIQUE_NAME,
    )
    if unique_columns is not None and unique_columns != SCOPE_COLUMNS:
        raise RuntimeError(
            f"{UNIQUE_NAME} has unexpected columns: {unique_columns!r}"
        )
    if unique_columns is None:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(TABLE_NAME) as batch:
                batch.create_unique_constraint(UNIQUE_NAME, SCOPE_COLUMNS)
        else:
            op.create_unique_constraint(UNIQUE_NAME, TABLE_NAME, SCOPE_COLUMNS)

    actual_index = _named_columns(
        _inspector().get_indexes(TABLE_NAME),
        LATEST_INDEX,
    )
    if actual_index != LATEST_INDEX_COLUMNS:
        if actual_index is not None:
            op.drop_index(LATEST_INDEX, table_name=TABLE_NAME)
        op.create_index(LATEST_INDEX, TABLE_NAME, LATEST_INDEX_COLUMNS)


def upgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("campaign_id", sa.String(length=64), nullable=False),
            sa.Column("stat_time_day", sa.Date(), nullable=False),
            sa.Column(
                "snapshot_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
            ),
            sa.Column(
                "complete",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "row_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "source_observed_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
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
            sa.UniqueConstraint(*SCOPE_COLUMNS, name=UNIQUE_NAME),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
    _repair_contract()


def downgrade() -> None:
    if TABLE_NAME in set(_inspector().get_table_names()):
        op.drop_table(TABLE_NAME)
