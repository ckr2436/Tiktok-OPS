"""Platform policy v1 schema adjustments."""

from __future__ import annotations

import json
from typing import Iterable

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "0007_platform_policy_v1"
down_revision = "0006_policy_business_controls"
branch_labels = None
depends_on = None


JSON_TYPE = sa.JSON().with_variant(mysql.JSON(), "mysql")


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_constraint(table: str, name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(cons.get("name") == name for cons in inspector.get_unique_constraints(table))


def _drop_constraint(table: str, name: str) -> None:
    if _has_constraint(table, name):
        op.drop_constraint(name, table, type_="unique")


def _create_constraint(table: str, name: str, columns: Iterable[str]) -> None:
    if not _has_constraint(table, name):
        op.create_unique_constraint(name, table, list(columns))


def _ensure_enum(bind, table: str, column: str) -> None:
    if bind.dialect.name != "mysql":
        return
    enum_values = ["ENFORCE", "DRYRUN", "OFF"]
    enum_def = ",".join(f"'{value}'" for value in enum_values)
    op.execute(
        sa.text(
            f"ALTER TABLE {table} MODIFY {column} "
            f"ENUM({enum_def}) NOT NULL DEFAULT 'ENFORCE'"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column("ttb_platform_policies", "name"):
        op.add_column(
            "ttb_platform_policies",
            sa.Column("name", sa.String(length=128), nullable=True),
        )
    if not _has_column("ttb_platform_policies", "name_normalized"):
        op.add_column(
            "ttb_platform_policies",
            sa.Column("name_normalized", sa.String(length=128), nullable=True),
        )
    if not _has_column("ttb_platform_policies", "domains_json"):
        op.add_column(
            "ttb_platform_policies",
            sa.Column("domains_json", JSON_TYPE, nullable=True),
        )
    if not _has_column("ttb_platform_policies", "business_scopes_json"):
        op.add_column(
            "ttb_platform_policies",
            sa.Column("business_scopes_json", JSON_TYPE, nullable=True),
        )

    _drop_constraint("ttb_platform_policies", "uq_platform_policy_provider_mode_domain")

    result = bind.execute(
        sa.text(
            "SELECT id, domain, scope_bc_ids_json, scope_advertiser_ids_json, "
            "scope_shop_ids_json, scope_product_id_patterns_json "
            "FROM ttb_platform_policies ORDER BY id"
        )
    )
    for row in result:
        raw_domain = (row.domain or "").strip().lower()
        domains = [raw_domain] if raw_domain else []
        base_name = raw_domain or f"policy-{row.id}"
        normalized_name = base_name.strip()
        include_scopes: dict[str, list[str]] = {}
        if row.scope_bc_ids_json:
            include_scopes["bc_ids"] = [
                str(value).strip()
                for value in row.scope_bc_ids_json
                if str(value).strip()
            ]
        if row.scope_advertiser_ids_json:
            include_scopes["advertiser_ids"] = [
                str(value).strip()
                for value in row.scope_advertiser_ids_json
                if str(value).strip()
            ]
        if row.scope_shop_ids_json:
            include_scopes["shop_ids"] = [
                str(value).strip()
                for value in row.scope_shop_ids_json
                if str(value).strip()
            ]
        if row.scope_product_id_patterns_json:
            include_scopes["product_ids"] = [
                str(value).strip()
                for value in row.scope_product_id_patterns_json
                if str(value).strip()
            ]
        payload = {"include": include_scopes, "exclude": {}}
        bind.execute(
            sa.text(
                "UPDATE ttb_platform_policies "
                "SET name = :name, name_normalized = :normalized, domains_json = :domains_json, "
                "business_scopes_json = COALESCE(business_scopes_json, :scopes) "
                "WHERE id = :id"
            ),
            {
                "id": row.id,
                "name": base_name[:128],
                "normalized": normalized_name[:128].lower(),
                "domains_json": json.dumps(domains),
                "scopes": json.dumps(payload),
            },
        )

    op.execute(
        sa.text(
            "UPDATE ttb_platform_policies SET enforcement_mode = 'DRYRUN' "
            "WHERE enforcement_mode = 'OBSERVE'"
        )
    )

    _ensure_enum(bind, "ttb_platform_policies", "enforcement_mode")

    with op.batch_alter_table("ttb_platform_policies") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.alter_column(
            "name_normalized",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.alter_column(
            "domains_json",
            existing_type=JSON_TYPE,
            nullable=False,
        )
        batch_op.alter_column(
            "business_scopes_json",
            existing_type=JSON_TYPE,
            nullable=False,
        )

    _create_constraint(
        "ttb_platform_policies",
        "uq_platform_policy_provider_name",
        ("provider_key", "name_normalized"),
    )


def downgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table("ttb_platform_policies") as batch_op:
        if _has_constraint("ttb_platform_policies", "uq_platform_policy_provider_name"):
            batch_op.drop_constraint(
                "uq_platform_policy_provider_name", type_="unique"
            )
        batch_op.alter_column(
            "business_scopes_json",
            existing_type=JSON_TYPE,
            nullable=True,
            server_default=None,
        )
        batch_op.alter_column(
            "domains_json",
            existing_type=JSON_TYPE,
            nullable=True,
            server_default=None,
        )
        batch_op.alter_column(
            "name_normalized",
            existing_type=sa.String(length=128),
            nullable=True,
        )
        batch_op.alter_column(
            "name",
            existing_type=sa.String(length=128),
            nullable=True,
        )

    op.execute(
        sa.text(
            "UPDATE ttb_platform_policies SET enforcement_mode = 'OBSERVE' "
            "WHERE enforcement_mode = 'DRYRUN'"
        )
    )

    if bind.dialect.name == "mysql":
        op.execute(
            sa.text(
                "ALTER TABLE ttb_platform_policies MODIFY enforcement_mode "
                "ENUM('ENFORCE','OBSERVE') NOT NULL DEFAULT 'ENFORCE'"
            )
        )

    if _has_column("ttb_platform_policies", "business_scopes_json"):
        op.drop_column("ttb_platform_policies", "business_scopes_json")
    if _has_column("ttb_platform_policies", "domains_json"):
        op.drop_column("ttb_platform_policies", "domains_json")
    if _has_column("ttb_platform_policies", "name_normalized"):
        op.drop_column("ttb_platform_policies", "name_normalized")
    if _has_column("ttb_platform_policies", "name"):
        op.drop_column("ttb_platform_policies", "name")

    _create_constraint(
        "ttb_platform_policies",
        "uq_platform_policy_provider_mode_domain",
        ("provider_key", "mode", "domain"),
    )
