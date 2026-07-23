"""Normalize GMV Max creative-to-product associations.

Revision ID: 0097_gmvmax_creative_products
Revises: 0096_gmvmax_control_plane
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0097_gmvmax_creative_products"
down_revision = "0096_gmvmax_control_plane"
branch_labels = None
depends_on = None


UBIGINT = mysql.BIGINT(unsigned=True)
TABLE_NAME = "gmvmax_creative_asset_products"
CACHE_TABLE_NAME = "gmvmax_creative_asset_cache"
UNIQUE_NAME = "uq_gmvmax_creative_asset_product_scope"
LOOKUP_INDEX_NAME = "idx_gmvmax_creative_asset_product_lookup"
SCOPE_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "item_id",
    "item_group_id",
]
LOOKUP_COLUMNS = [
    "workspace_id",
    "auth_id",
    "advertiser_id",
    "store_id",
    "item_group_id",
    "item_id",
]


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _named_columns(items: Sequence[Mapping[str, Any]], name: str) -> list[str] | None:
    for item in items:
        if str(item.get("name") or "") == name:
            return [str(value) for value in item.get("column_names") or []]
    return None


def _repair_table_contract() -> None:
    """Finish a partially applied MySQL DDL migration before backfilling.

    MySQL commits CREATE TABLE and CREATE INDEX separately.  A failed index
    statement must therefore be repairable on the next Alembic run instead of
    being skipped merely because the table already exists.
    """

    inspector = _inspector()
    actual_columns = {
        str(column["name"]) for column in inspector.get_columns(TABLE_NAME)
    }
    required_columns = {*SCOPE_COLUMNS, "created_at", "updated_at"}
    missing_columns = sorted(required_columns - actual_columns)
    if missing_columns:
        raise RuntimeError(
            f"{TABLE_NAME} exists with an incomplete schema; "
            f"missing columns: {', '.join(missing_columns)}"
        )

    unique_columns = _named_columns(
        inspector.get_unique_constraints(TABLE_NAME),
        UNIQUE_NAME,
    )
    index_columns = _named_columns(
        inspector.get_indexes(TABLE_NAME),
        LOOKUP_INDEX_NAME,
    )

    if unique_columns is not None and unique_columns != SCOPE_COLUMNS:
        raise RuntimeError(
            f"{UNIQUE_NAME} has unexpected columns: {unique_columns!r}"
        )
    if index_columns is not None and index_columns != LOOKUP_COLUMNS:
        op.drop_index(LOOKUP_INDEX_NAME, table_name=TABLE_NAME)
        index_columns = None

    if unique_columns is None:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(TABLE_NAME) as batch:
                batch.create_unique_constraint(UNIQUE_NAME, SCOPE_COLUMNS)
        else:
            op.create_unique_constraint(UNIQUE_NAME, TABLE_NAME, SCOPE_COLUMNS)

    # SQLite batch table recreation can discard non-constraint indexes, so
    # inspect again after repairing the unique key.
    if (
        _named_columns(_inspector().get_indexes(TABLE_NAME), LOOKUP_INDEX_NAME)
        != LOOKUP_COLUMNS
    ):
        op.create_index(LOOKUP_INDEX_NAME, TABLE_NAME, LOOKUP_COLUMNS)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _normalized_spu_ids(raw_json: Any, fallback_item_group_id: Any) -> list[str]:
    payload = _json_mapping(raw_json)
    raw_ids = payload.get("spu_id_list")
    if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes)):
        candidates = list(raw_ids)
    elif raw_ids is None:
        candidates = []
    else:
        candidates = [raw_ids]

    normalized: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if value is None:
            continue
        item_group_id = str(value).strip()
        if not item_group_id or item_group_id in seen:
            continue
        seen.add(item_group_id)
        normalized.append(item_group_id)

    if not normalized and fallback_item_group_id is not None:
        fallback = str(fallback_item_group_id).strip()
        if fallback:
            normalized.append(fallback)
    return normalized


def _backfill_creative_products() -> None:
    bind = op.get_bind()
    if CACHE_TABLE_NAME not in _table_names():
        return

    existing_keys = {
        (
            int(row.workspace_id),
            int(row.auth_id),
            str(row.advertiser_id),
            str(row.store_id),
            str(row.item_id),
            str(row.item_group_id),
        )
        for row in bind.execute(
            sa.text(
                f"""
                select workspace_id, auth_id, advertiser_id, store_id,
                       item_id, item_group_id
                from {TABLE_NAME}
                """
            )
        )
    }
    rows = bind.execute(
        sa.text(
            f"""
            select workspace_id, auth_id, advertiser_id, store_id,
                   item_id, item_group_id, raw_json
            from {CACHE_TABLE_NAME}
            """
        )
    ).mappings()

    inserts: list[dict[str, Any]] = []
    for row in rows:
        for item_group_id in _normalized_spu_ids(
            row.get("raw_json"),
            row.get("item_group_id"),
        ):
            key = (
                int(row["workspace_id"]),
                int(row["auth_id"]),
                str(row["advertiser_id"]),
                str(row["store_id"]),
                str(row["item_id"]),
                item_group_id,
            )
            if key in existing_keys:
                continue
            existing_keys.add(key)
            inserts.append(
                {
                    "workspace_id": key[0],
                    "auth_id": key[1],
                    "advertiser_id": key[2],
                    "store_id": key[3],
                    "item_id": key[4],
                    "item_group_id": key[5],
                }
            )

    if inserts:
        bind.execute(
            sa.text(
                f"""
                insert into {TABLE_NAME} (
                    workspace_id, auth_id, advertiser_id, store_id,
                    item_id, item_group_id
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id,
                    :item_id, :item_group_id
                )
                """
            ),
            inserts,
        )


def upgrade() -> None:
    if TABLE_NAME not in _table_names():
        timestamp_default = (
            sa.text("CURRENT_TIMESTAMP(6)")
            if op.get_bind().dialect.name == "mysql"
            else sa.text("CURRENT_TIMESTAMP")
        )
        op.create_table(
            TABLE_NAME,
            sa.Column("workspace_id", UBIGINT, nullable=False),
            sa.Column("auth_id", UBIGINT, nullable=False),
            sa.Column("advertiser_id", sa.String(length=64), nullable=False),
            sa.Column("store_id", sa.String(length=64), nullable=False),
            sa.Column("item_id", sa.String(length=64), nullable=False),
            sa.Column("item_group_id", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=timestamp_default,
            ),
            sa.Column(
                "updated_at",
                mysql.DATETIME(fsp=6),
                nullable=False,
                server_default=timestamp_default,
            ),
            sa.UniqueConstraint(
                *SCOPE_COLUMNS,
                name=UNIQUE_NAME,
            ),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_0900_ai_ci",
        )

    _repair_table_contract()
    _backfill_creative_products()


def downgrade() -> None:
    if TABLE_NAME in _table_names():
        op.drop_table(TABLE_NAME)
