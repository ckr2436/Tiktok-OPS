"""Store GMV Max product eligibility at advertiser scope.

Revision ID: 0101_ttb_product_advertiser_eligibility
Revises: 0100_gmv_creative_10min_sync_state
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0101_ttb_product_advertiser_eligibility"
down_revision = "0100_gmv_creative_10min_sync_state"
branch_labels = None
depends_on = None


TABLE_NAME = "ttb_product_advertiser_eligibility"
UNIQUE_NAME = "uk_ttb_product_advertiser_eligibility"
ELIGIBLE_INDEX = "idx_ttb_product_advertiser_eligible"
PRODUCT_INDEX = "idx_ttb_product_eligibility_product"
SCOPE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "product_id",
]
ELIGIBLE_INDEX_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "is_eligible",
]
PRODUCT_INDEX_COLUMNS = [
    "workspace_id",
    "auth_id",
    "store_id",
    "product_id",
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
        "is_eligible",
        "gmv_max_ads_status",
        "last_seen_at",
        "absent_at",
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

    for index_name, expected_columns in (
        (ELIGIBLE_INDEX, ELIGIBLE_INDEX_COLUMNS),
        (PRODUCT_INDEX, PRODUCT_INDEX_COLUMNS),
    ):
        actual = _named_columns(_inspector().get_indexes(TABLE_NAME), index_name)
        if actual == expected_columns:
            continue
        if actual is not None:
            op.drop_index(index_name, table_name=TABLE_NAME)
        op.create_index(index_name, TABLE_NAME, expected_columns)


def upgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column(
                "workspace_id",
                UBIGINT,
                sa.ForeignKey(
                    "workspaces.id",
                    name="fk_ttb_product_eligibility_workspace",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "auth_id",
                UBIGINT,
                sa.ForeignKey(
                    "oauth_accounts_ttb.id",
                    name="fk_ttb_product_eligibility_auth",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("product_id", sa.String(length=64), nullable=False),
            sa.Column(
                "is_eligible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column("gmv_max_ads_status", sa.String(length=32), nullable=True),
            sa.Column(
                "last_seen_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column("absent_at", mysql.DATETIME(fsp=6), nullable=True),
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
