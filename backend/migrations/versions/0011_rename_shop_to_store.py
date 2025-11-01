"""rename TikTok Business shop terminology to store"""

from __future__ import annotations

import json
from typing import Any, Tuple

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "0011_rename_shop_to_store"
down_revision = "0010_scheduler_idempotency_and_indexes"
branch_labels = None
depends_on = None


_NEW_ENUM_VALUES: Tuple[str, ...] = ("bc", "advertiser", "store", "product")
_OLD_ENUM_VALUES: Tuple[str, ...] = ("bc", "advertiser", "shop", "product")
_JSON_KEY_MAP = {
    "shop_id": "store_id",
    "shop_ids": "store_ids",
    "shops": "stores",
    "shops_count": "stores_count",
}
_JSON_VALUE_MAP = {
    "shop": "store",
    "shops": "stores",
    "shop_id": "store_id",
    "shop_ids": "store_ids",
    "shops_count": "stores_count",
    "ttb.sync.shops": "ttb.sync.stores",
}
_JSON_STRING_REPLACEMENTS = (
    ("ttb.sync.shops", "ttb.sync.stores"),
    ("\"shops\"", "\"stores\""),
    ("shops_count", "stores_count"),
)

_UPDATE_CURSOR_RESOURCE_TO_STORE = (
    "UPDATE ttb_sync_cursors SET resource_type = 'store' WHERE resource_type = 'shop'"
)
_UPDATE_CURSOR_RESOURCE_TO_SHOP = (
    "UPDATE ttb_sync_cursors SET resource_type = 'shop' WHERE resource_type = 'store'"
)
_UPDATE_POLICY_DOMAIN_TO_STORE = (
    "UPDATE ttb_policy_items SET domain = 'store' WHERE domain = 'shop'"
)
_UPDATE_POLICY_DOMAIN_TO_SHOP = (
    "UPDATE ttb_policy_items SET domain = 'shop' WHERE domain = 'store'"
)

_JSON_KEY_MAP_DOWN = {v: k for k, v in _JSON_KEY_MAP.items()}
_JSON_VALUE_MAP_DOWN = {v: k for k, v in _JSON_VALUE_MAP.items()}
_JSON_STRING_REPLACEMENTS_DOWN = tuple(
    (new, old) for old, new in _JSON_STRING_REPLACEMENTS
)


def _bind():
    return op.get_bind()


def _insp():
    return inspect(_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(col["name"] == column for col in _insp().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    return any(ix.get("name") == name for ix in _insp().get_indexes(table))


def _has_unique(table: str, name: str) -> bool:
    return any(cons.get("name") == name for cons in _insp().get_unique_constraints(table))


def _rename_column(table: str, old: str, new: str, *, existing_type: sa.types.TypeEngine | None = None, existing_nullable: bool | None = None) -> None:
    bind = _bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))
    else:
        op.alter_column(
            table,
            old,
            new_column_name=new,
            existing_type=existing_type,
            existing_nullable=existing_nullable,
        )


def _drop_index_if_exists(table: str, name: str) -> None:
    if _has_index(table, name):
        op.drop_index(name, table_name=table)


def _drop_unique_if_exists(table: str, name: str) -> None:
    if _has_unique(table, name):
        op.drop_constraint(name, table_name=table, type_="unique")


def _transform_json(
    value: Any,
    *,
    key_map: dict[str, str],
    value_map: dict[str, str],
    replacements: Tuple[Tuple[str, str], ...],
) -> Tuple[bool, Any]:
    if isinstance(value, dict):
        changed = False
        new_obj: dict[str, Any] = {}
        for key, sub in value.items():
            new_key = key_map.get(key, key)
            if new_key != key:
                changed = True
            sub_changed, new_sub = _transform_json(
                sub, key_map=key_map, value_map=value_map, replacements=replacements
            )
            if sub_changed:
                changed = True
            new_obj[new_key] = new_sub
        return changed, new_obj
    if isinstance(value, list):
        changed = False
        new_items = []
        for item in value:
            item_changed, new_item = _transform_json(
                item, key_map=key_map, value_map=value_map, replacements=replacements
            )
            if item_changed:
                changed = True
            new_items.append(new_item)
        return changed, new_items
    if isinstance(value, str):
        base = value_map.get(value, value)
        for old, new in replacements:
            if old in base:
                base = base.replace(old, new)
        return base != value, base
    return False, value


def _normalize_json_payload(
    payload: Any,
    *,
    key_map: dict[str, str],
    value_map: dict[str, str],
    replacements: Tuple[Tuple[str, str], ...],
) -> Tuple[Any, bool]:
    if payload is None:
        return payload, False
    text_mode = False
    original = payload
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        text_mode = True
        try:
            parsed = json.loads(payload)
        except Exception:
            updated = payload
            for old, new in replacements:
                if old in updated:
                    updated = updated.replace(old, new)
            return updated, updated != payload
    else:
        parsed = payload
    changed, new_value = _transform_json(
        parsed, key_map=key_map, value_map=value_map, replacements=replacements
    )
    if not changed and text_mode:
        updated = payload
        for old, new in replacements:
            if old in updated:
                updated = updated.replace(old, new)
        return updated, updated != payload
    if not changed:
        return original, False
    if text_mode:
        return json.dumps(new_value), True
    return new_value, True


def _update_json_column(
    table_name: str,
    column_name: str,
    *,
    key_map: dict[str, str],
    value_map: dict[str, str],
    replacements: Tuple[Tuple[str, str], ...],
) -> None:
    if not _has_table(table_name):
        return
    if not _has_column(table_name, column_name):
        return
    bind = _bind()
    meta = sa.MetaData()
    table = sa.Table(table_name, meta, autoload_with=bind)
    pk_cols = list(table.primary_key.columns)
    if not pk_cols:
        return
    pk_col = pk_cols[0]
    rows = bind.execute(sa.select(pk_col, table.c[column_name])).fetchall()
    for pk_value, payload in rows:
        new_payload, changed = _normalize_json_payload(
            payload, key_map=key_map, value_map=value_map, replacements=replacements
        )
        if not changed:
            continue
        bind.execute(
            table.update().where(pk_col == pk_value).values({column_name: new_payload})
        )


def _update_task_catalog() -> None:
    bind = _bind()
    if not _has_table("task_catalog"):
        return
    meta = sa.MetaData()
    task_catalog = sa.Table("task_catalog", meta, autoload_with=bind)
    schedules = sa.Table("schedules", meta, autoload_with=bind) if _has_table("schedules") else None

    row = bind.execute(
        sa.select(task_catalog).where(task_catalog.c.task_name == "ttb.sync.shops")
    ).mappings().first()
    if row:
        exists_new = bind.execute(
            sa.select(task_catalog.c.task_name).where(task_catalog.c.task_name == "ttb.sync.stores")
        ).first()
        if not exists_new:
            new_row = dict(row)
            new_row.pop("id", None)
            new_row["task_name"] = "ttb.sync.stores"
            bind.execute(task_catalog.insert().values(**new_row))
        if schedules is not None:
            bind.execute(
                schedules.update()
                .where(schedules.c.task_name == "ttb.sync.shops")
                .values(task_name="ttb.sync.stores")
            )
        bind.execute(
            task_catalog.delete().where(task_catalog.c.task_name == "ttb.sync.shops")
        )

    _update_json_column(
        "task_catalog",
        "input_schema_json",
        key_map=_JSON_KEY_MAP,
        value_map=_JSON_VALUE_MAP,
        replacements=_JSON_STRING_REPLACEMENTS,
    )


def _update_policy_domain_enum() -> None:
    bind = _bind()
    if not _has_table("ttb_policy_items"):
        return
    dialect = bind.dialect.name
    op.execute(_UPDATE_POLICY_DOMAIN_TO_STORE)
    if dialect == "postgresql":
        op.execute("ALTER TYPE ttb_policy_domain RENAME TO ttb_policy_domain_old")
        sa.Enum(*_NEW_ENUM_VALUES, name="ttb_policy_domain").create(bind, checkfirst=False)
        op.execute(
            "ALTER TABLE ttb_policy_items ALTER COLUMN domain TYPE ttb_policy_domain USING domain::text::ttb_policy_domain"
        )
        op.execute("DROP TYPE ttb_policy_domain_old")
    elif dialect == "mysql":
        enum_values = ",".join(f"'{val}'" for val in _NEW_ENUM_VALUES)
        op.execute(
            text(
                f"ALTER TABLE ttb_policy_items MODIFY domain ENUM({enum_values}) NOT NULL"
            )
        )
    else:
        op.alter_column(
            "ttb_policy_items",
            "domain",
            existing_type=sa.Enum(*_OLD_ENUM_VALUES, name="ttb_policy_domain"),
            type_=sa.Enum(*_NEW_ENUM_VALUES, name="ttb_policy_domain"),
            existing_nullable=False,
        )


def upgrade() -> None:
    bind = _bind()

    if _has_table("ttb_shops") and not _has_table("ttb_stores"):
        op.rename_table("ttb_shops", "ttb_stores")

    if _has_table("ttb_stores") and _has_column("ttb_stores", "shop_id"):
        _rename_column(
            "ttb_stores",
            "shop_id",
            "store_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
        )
        _drop_unique_if_exists("ttb_stores", "uk_ttb_shop_scope")
        op.create_unique_constraint(
            "uk_ttb_store_scope",
            "ttb_stores",
            ["workspace_id", "auth_id", "store_id"],
        )
        for old, new, columns in (
            ("idx_ttb_shop_scope", "idx_ttb_store_scope", ["workspace_id", "auth_id", "store_id"]),
            ("idx_ttb_shop_adv", "idx_ttb_store_adv", ["advertiser_id"]),
            ("idx_ttb_shop_updated", "idx_ttb_store_updated", ["ext_updated_time"]),
            ("idx_ttb_shop_status", "idx_ttb_store_status", ["status"]),
        ):
            _drop_index_if_exists("ttb_stores", old)
            op.create_index(new, "ttb_stores", columns)

    if _has_table("ttb_products") and _has_column("ttb_products", "shop_id"):
        _rename_column(
            "ttb_products",
            "shop_id",
            "store_id",
            existing_type=sa.String(length=64),
            existing_nullable=True,
        )
        _drop_index_if_exists("ttb_products", "idx_ttb_product_shop")
        op.create_index("idx_ttb_product_store", "ttb_products", ["store_id"])

    if _has_table("ttb_platform_policies") and _has_column(
        "ttb_platform_policies", "scope_shop_ids_json"
    ):
        _rename_column(
            "ttb_platform_policies",
            "scope_shop_ids_json",
            "scope_store_ids_json",
            existing_type=sa.JSON(),
            existing_nullable=True,
        )

    if _has_table("ttb_sync_cursors"):
        op.execute(_UPDATE_CURSOR_RESOURCE_TO_STORE)

    _update_policy_domain_enum()

    _update_task_catalog()

    if _has_table("schedules"):
        _update_json_column(
            "schedules",
            "params_json",
            key_map=_JSON_KEY_MAP,
            value_map=_JSON_VALUE_MAP,
            replacements=_JSON_STRING_REPLACEMENTS,
        )
    if _has_table("schedule_runs"):
        _update_json_column(
            "schedule_runs",
            "stats_json",
            key_map=_JSON_KEY_MAP,
            value_map=_JSON_VALUE_MAP,
            replacements=_JSON_STRING_REPLACEMENTS,
        )
    if _has_table("ttb_platform_policies"):
        _update_json_column(
            "ttb_platform_policies",
            "business_scopes_json",
            key_map=_JSON_KEY_MAP,
            value_map=_JSON_VALUE_MAP,
            replacements=_JSON_STRING_REPLACEMENTS,
        )
    if _has_table("ttb_sync_cursors"):
        _update_json_column(
            "ttb_sync_cursors",
            "extra_json",
            key_map=_JSON_KEY_MAP,
            value_map=_JSON_VALUE_MAP,
            replacements=_JSON_STRING_REPLACEMENTS,
        )


def downgrade() -> None:
    bind = _bind()

    if _has_table("ttb_sync_cursors"):
        op.execute(_UPDATE_CURSOR_RESOURCE_TO_SHOP)
        _update_json_column(
            "ttb_sync_cursors",
            "extra_json",
            key_map=_JSON_KEY_MAP_DOWN,
            value_map=_JSON_VALUE_MAP_DOWN,
            replacements=_JSON_STRING_REPLACEMENTS_DOWN,
        )

    if _has_table("ttb_platform_policies"):
        if _has_column("ttb_platform_policies", "scope_store_ids_json"):
            _rename_column(
                "ttb_platform_policies",
                "scope_store_ids_json",
                "scope_shop_ids_json",
                existing_type=sa.JSON(),
                existing_nullable=True,
            )
        _update_json_column(
            "ttb_platform_policies",
            "business_scopes_json",
            key_map=_JSON_KEY_MAP_DOWN,
            value_map=_JSON_VALUE_MAP_DOWN,
            replacements=_JSON_STRING_REPLACEMENTS_DOWN,
        )

    if _has_table("schedules"):
        _update_json_column(
            "schedules",
            "params_json",
            key_map=_JSON_KEY_MAP_DOWN,
            value_map=_JSON_VALUE_MAP_DOWN,
            replacements=_JSON_STRING_REPLACEMENTS_DOWN,
        )
    if _has_table("schedule_runs"):
        _update_json_column(
            "schedule_runs",
            "stats_json",
            key_map=_JSON_KEY_MAP_DOWN,
            value_map=_JSON_VALUE_MAP_DOWN,
            replacements=_JSON_STRING_REPLACEMENTS_DOWN,
        )

    if _has_table("ttb_policy_items"):
        op.execute(_UPDATE_POLICY_DOMAIN_TO_SHOP)
        dialect = bind.dialect.name
        if dialect == "postgresql":
            op.execute("ALTER TYPE ttb_policy_domain RENAME TO ttb_policy_domain_new")
            sa.Enum(*_OLD_ENUM_VALUES, name="ttb_policy_domain").create(bind, checkfirst=False)
            op.execute(
                "ALTER TABLE ttb_policy_items ALTER COLUMN domain TYPE ttb_policy_domain USING domain::text::ttb_policy_domain"
            )
            op.execute("DROP TYPE ttb_policy_domain_new")
        elif dialect == "mysql":
            enum_values = ",".join(f"'{val}'" for val in _OLD_ENUM_VALUES)
            op.execute(
                text(
                    f"ALTER TABLE ttb_policy_items MODIFY domain ENUM({enum_values}) NOT NULL"
                )
            )
        else:
            op.alter_column(
                "ttb_policy_items",
                "domain",
                existing_type=sa.Enum(*_NEW_ENUM_VALUES, name="ttb_policy_domain"),
                type_=sa.Enum(*_OLD_ENUM_VALUES, name="ttb_policy_domain"),
                existing_nullable=False,
            )

    if _has_table("ttb_products") and _has_column("ttb_products", "store_id"):
        _drop_index_if_exists("ttb_products", "idx_ttb_product_store")
        op.create_index("idx_ttb_product_shop", "ttb_products", ["shop_id"])
        _rename_column(
            "ttb_products",
            "store_id",
            "shop_id",
            existing_type=sa.String(length=64),
            existing_nullable=True,
        )

    if _has_table("ttb_stores") and _has_column("ttb_stores", "store_id"):
        _drop_unique_if_exists("ttb_stores", "uk_ttb_store_scope")
        op.create_unique_constraint(
            "uk_ttb_shop_scope",
            "ttb_stores",
            ["workspace_id", "auth_id", "shop_id"],
        )
        for old, new, columns in (
            ("idx_ttb_store_scope", "idx_ttb_shop_scope", ["workspace_id", "auth_id", "shop_id"]),
            ("idx_ttb_store_adv", "idx_ttb_shop_adv", ["advertiser_id"]),
            ("idx_ttb_store_updated", "idx_ttb_shop_updated", ["ext_updated_time"]),
            ("idx_ttb_store_status", "idx_ttb_shop_status", ["status"]),
        ):
            _drop_index_if_exists("ttb_stores", old)
            op.create_index(new, "ttb_stores", columns)
        _rename_column(
            "ttb_stores",
            "store_id",
            "shop_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
        )

    if _has_table("ttb_stores") and not _has_table("ttb_shops"):
        op.rename_table("ttb_stores", "ttb_shops")

    if _has_table("task_catalog"):
        bind = _bind()
        meta = sa.MetaData()
        task_catalog = sa.Table("task_catalog", meta, autoload_with=bind)
        schedules = sa.Table("schedules", meta, autoload_with=bind) if _has_table("schedules") else None

        row = bind.execute(
            sa.select(task_catalog).where(task_catalog.c.task_name == "ttb.sync.stores")
        ).mappings().first()
        if row:
            exists_old = bind.execute(
                sa.select(task_catalog.c.task_name).where(task_catalog.c.task_name == "ttb.sync.shops")
            ).first()
            if not exists_old:
                new_row = dict(row)
                new_row.pop("id", None)
                new_row["task_name"] = "ttb.sync.shops"
                bind.execute(task_catalog.insert().values(**new_row))
            if schedules is not None:
                bind.execute(
                    schedules.update()
                    .where(schedules.c.task_name == "ttb.sync.stores")
                    .values(task_name="ttb.sync.shops")
                )
            bind.execute(
                task_catalog.delete().where(task_catalog.c.task_name == "ttb.sync.stores")
            )
        _update_json_column(
            "task_catalog",
            "input_schema_json",
            key_map=_JSON_KEY_MAP_DOWN,
            value_map=_JSON_VALUE_MAP_DOWN,
            replacements=_JSON_STRING_REPLACEMENTS_DOWN,
        )
