"""Add business scope and control fields to platform policies."""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql


revision = "0006_policy_business_controls"
down_revision = "0005_normalize_policy_mode"
branch_labels = None
depends_on = None


POLICY_MODE_ENUM = sa.Enum("WHITELIST", "BLACKLIST", name="ttb_policy_mode")
POLICY_ENFORCEMENT_ENUM = sa.Enum("ENFORCE", "OBSERVE", name="ttb_policy_enforcement_mode")
JSON_TYPE = sa.JSON().with_variant(mysql.JSON(), "mysql")


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(table: str, name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(idx.get("name") == name for idx in inspector.get_indexes(table))


def _add_columns(table: str, columns: Iterable[sa.Column]) -> None:
    with op.batch_alter_table(table) as batch_op:
        for column in columns:
            if not _has_column(table, column.name):
                batch_op.add_column(column)


def _drop_columns(table: str, column_names: Iterable[str]) -> None:
    with op.batch_alter_table(table) as batch_op:
        for column in column_names:
            if _has_column(table, column):
                batch_op.drop_column(column)


def upgrade() -> None:
    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"

    if not is_mysql:
        POLICY_ENFORCEMENT_ENUM.create(bind, checkfirst=True)

    _add_columns(
        "ttb_platform_policies",
        [
            sa.Column("scope_bc_ids_json", JSON_TYPE, nullable=True),
            sa.Column("scope_advertiser_ids_json", JSON_TYPE, nullable=True),
            sa.Column("scope_shop_ids_json", JSON_TYPE, nullable=True),
            sa.Column("scope_region_codes_json", JSON_TYPE, nullable=True),
            sa.Column("scope_product_id_patterns_json", JSON_TYPE, nullable=True),
            sa.Column("rate_limit_rps", sa.Integer(), nullable=True),
            sa.Column("rate_burst", sa.Integer(), nullable=True),
            sa.Column(
                "cooldown_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("window_cron", sa.String(length=64), nullable=True),
            sa.Column("max_concurrency", sa.Integer(), nullable=True),
            sa.Column("max_entities_per_run", sa.Integer(), nullable=True),
            sa.Column(
                "enforcement_mode",
                POLICY_ENFORCEMENT_ENUM
                if not is_mysql
                else sa.Enum("ENFORCE", "OBSERVE", name="ttb_policy_enforcement_mode"),
                nullable=False,
                server_default=sa.text("'ENFORCE'"),
            ),
            sa.Column("extra_json", JSON_TYPE, nullable=True),
        ],
    )

    op.execute(
        sa.text(
            "UPDATE ttb_platform_policies SET mode = UPPER(TRIM(mode)) WHERE mode IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ttb_platform_policies SET enforcement_mode = 'ENFORCE' "
            "WHERE enforcement_mode IS NULL"
        )
    )

    if not _has_index(
        "ttb_platform_policies", "idx_policies_workspace_provider_enabled"
    ):
        op.create_index(
            "idx_policies_workspace_provider_enabled",
            "ttb_platform_policies",
            ["workspace_id", "provider_key", "is_enabled"],
        )


def downgrade() -> None:
    if _has_index("ttb_platform_policies", "idx_policies_workspace_provider_enabled"):
        op.drop_index(
            "idx_policies_workspace_provider_enabled",
            table_name="ttb_platform_policies",
        )

    _drop_columns(
        "ttb_platform_policies",
        [
            "scope_bc_ids_json",
            "scope_advertiser_ids_json",
            "scope_shop_ids_json",
            "scope_region_codes_json",
            "scope_product_id_patterns_json",
            "rate_limit_rps",
            "rate_burst",
            "cooldown_seconds",
            "window_cron",
            "max_concurrency",
            "max_entities_per_run",
            "enforcement_mode",
            "extra_json",
        ],
    )

    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        POLICY_ENFORCEMENT_ENUM.drop(bind, checkfirst=True)
