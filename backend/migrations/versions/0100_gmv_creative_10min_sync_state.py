"""Add persistent fair-dispatch state for GMV creative 10-minute sync.

Revision ID: 0100_gmv_creative_10min_sync_state
Revises: 0099_website_ads_request_scope
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0100_gmv_creative_10min_sync_state"
down_revision = "0099_website_ads_request_scope"
branch_labels = None
depends_on = None


TABLE_NAME = "gmv_creative_10min_sync_state"
CURSOR_TABLE_NAME = "gmv_sync_selection_cursors"
UBIGINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")
ATTEMPT_UNIQUE_NAME = "uk_gmv_creative_10min_sync_scope"
ATTEMPT_DUE_INDEX = "idx_gmv_creative_10min_sync_due"
ATTEMPT_ORDER_INDEX = "idx_gmv_creative_10min_sync_attempt"
CURSOR_UNIQUE_NAME = "uk_gmv_sync_selection_cursor"
CURSOR_FK_NAME = "fk_gmv_sync_selection_strategy"
CURSOR_INDEX = "idx_gmv_sync_selection_strategy"
ATTEMPT_SCOPE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "campaign_id",
]
ATTEMPT_DUE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "next_attempt_at",
    "last_attempt_at",
]
ATTEMPT_ORDER_COLUMNS = ["last_attempt_at", "id"]
CURSOR_UNIQUE_COLUMNS = ["strategy_id", "cursor_key"]
CURSOR_INDEX_COLUMNS = ["strategy_id", "updated_at"]


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_names() -> set[str]:
    return set(_inspector().get_table_names())


def _named_columns(items: list[dict], name: str) -> list[str] | None:
    for item in items:
        if str(item.get("name") or "") == name:
            return [str(value) for value in item.get("column_names") or []]
    return None


def _validate_columns(table_name: str, required: set[str]) -> None:
    actual = {
        str(column["name"]) for column in _inspector().get_columns(table_name)
    }
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            f"{table_name} exists with an incomplete schema; "
            f"missing columns: {', '.join(missing)}"
        )


def _repair_index(
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    actual = _named_columns(_inspector().get_indexes(table_name), index_name)
    if actual == expected_columns:
        return
    if actual is not None:
        op.drop_index(index_name, table_name=table_name)
    op.create_index(index_name, table_name, expected_columns)


def _repair_attempt_table() -> None:
    _validate_columns(
        TABLE_NAME,
        {
            "id",
            *ATTEMPT_SCOPE_COLUMNS,
            "store_id",
            "last_attempt_at",
            "next_attempt_at",
            "last_success_at",
            "last_error_at",
            "last_status",
            "last_error",
            "last_result_rows",
            "consecutive_failures",
            "attempt_count",
            "attempt_token",
            "created_at",
            "updated_at",
        },
    )
    unique_columns = _named_columns(
        _inspector().get_unique_constraints(TABLE_NAME),
        ATTEMPT_UNIQUE_NAME,
    )
    if unique_columns is not None and unique_columns != ATTEMPT_SCOPE_COLUMNS:
        raise RuntimeError(
            f"{ATTEMPT_UNIQUE_NAME} has unexpected columns: {unique_columns!r}"
        )
    if unique_columns is None:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(TABLE_NAME) as batch:
                batch.create_unique_constraint(
                    ATTEMPT_UNIQUE_NAME,
                    ATTEMPT_SCOPE_COLUMNS,
                )
        else:
            op.create_unique_constraint(
                ATTEMPT_UNIQUE_NAME,
                TABLE_NAME,
                ATTEMPT_SCOPE_COLUMNS,
            )
    # A SQLite batch alteration recreates the table and can remove indexes.
    _repair_index(TABLE_NAME, ATTEMPT_DUE_INDEX, ATTEMPT_DUE_COLUMNS)
    _repair_index(TABLE_NAME, ATTEMPT_ORDER_INDEX, ATTEMPT_ORDER_COLUMNS)


def _repair_cursor_table() -> None:
    _validate_columns(
        CURSOR_TABLE_NAME,
        {
            "id",
            "strategy_id",
            "cursor_key",
            "high_water_id",
            "last_id",
            "created_at",
            "updated_at",
        },
    )
    unique_columns = _named_columns(
        _inspector().get_unique_constraints(CURSOR_TABLE_NAME),
        CURSOR_UNIQUE_NAME,
    )
    if unique_columns is not None and unique_columns != CURSOR_UNIQUE_COLUMNS:
        raise RuntimeError(
            f"{CURSOR_UNIQUE_NAME} has unexpected columns: {unique_columns!r}"
        )

    foreign_keys = {
        str(item.get("name") or ""): item
        for item in _inspector().get_foreign_keys(CURSOR_TABLE_NAME)
    }
    foreign_key = foreign_keys.get(CURSOR_FK_NAME)
    if foreign_key is not None:
        constrained = [
            str(value)
            for value in foreign_key.get("constrained_columns") or []
        ]
        referred = [
            str(value) for value in foreign_key.get("referred_columns") or []
        ]
        referred_table = str(foreign_key.get("referred_table") or "")
        if (
            constrained != ["strategy_id"]
            or referred != ["id"]
            or referred_table != "gmvmax_monitoring_strategies"
        ):
            raise RuntimeError(
                f"{CURSOR_FK_NAME} has an unexpected definition"
            )

    unique_missing = unique_columns is None
    foreign_key_missing = foreign_key is None
    if op.get_bind().dialect.name == "sqlite" and (
        unique_missing or foreign_key_missing
    ):
        with op.batch_alter_table(CURSOR_TABLE_NAME) as batch:
            if unique_missing:
                batch.create_unique_constraint(
                    CURSOR_UNIQUE_NAME,
                    CURSOR_UNIQUE_COLUMNS,
                )
            if foreign_key_missing:
                batch.create_foreign_key(
                    CURSOR_FK_NAME,
                    "gmvmax_monitoring_strategies",
                    ["strategy_id"],
                    ["id"],
                    ondelete="CASCADE",
                )
    else:
        if unique_missing:
            op.create_unique_constraint(
                CURSOR_UNIQUE_NAME,
                CURSOR_TABLE_NAME,
                CURSOR_UNIQUE_COLUMNS,
            )
        if foreign_key_missing:
            op.create_foreign_key(
                CURSOR_FK_NAME,
                CURSOR_TABLE_NAME,
                "gmvmax_monitoring_strategies",
                ["strategy_id"],
                ["id"],
                ondelete="CASCADE",
            )
    _repair_index(CURSOR_TABLE_NAME, CURSOR_INDEX, CURSOR_INDEX_COLUMNS)


def _create_attempt_table() -> None:
    if TABLE_NAME in _table_names():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UBIGINT, nullable=False),
        sa.Column("auth_id", UBIGINT, nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=True),
        sa.Column("last_attempt_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("next_attempt_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "last_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'NEVER'"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_result_rows", sa.Integer(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("attempt_token", sa.String(length=36), nullable=True),
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
        sa.UniqueConstraint(
            *ATTEMPT_SCOPE_COLUMNS,
            name=ATTEMPT_UNIQUE_NAME,
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def upgrade() -> None:
    _create_attempt_table()
    _repair_attempt_table()
    if CURSOR_TABLE_NAME not in _table_names():
        op.create_table(
            CURSOR_TABLE_NAME,
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column(
                "strategy_id",
                UBIGINT,
                sa.ForeignKey(
                    "gmvmax_monitoring_strategies.id",
                    name=CURSOR_FK_NAME,
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column("cursor_key", sa.String(length=191), nullable=False),
            sa.Column(
                "high_water_id",
                UBIGINT,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "last_id",
                UBIGINT,
                nullable=False,
                server_default=sa.text("0"),
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
            sa.UniqueConstraint(
                *CURSOR_UNIQUE_COLUMNS,
                name=CURSOR_UNIQUE_NAME,
            ),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
    _repair_cursor_table()


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if CURSOR_TABLE_NAME in tables:
        op.drop_table(CURSOR_TABLE_NAME)
    if TABLE_NAME in tables:
        op.drop_table(TABLE_NAME)
