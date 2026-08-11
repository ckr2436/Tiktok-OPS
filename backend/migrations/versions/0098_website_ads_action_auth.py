"""Scope Website Ads action logs to one authorized account.

Revision ID: 0098_website_ads_action_auth
Revises: 0097_gmvmax_creative_products
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0098_website_ads_action_auth"
down_revision = "0097_gmvmax_creative_products"
branch_labels = None
depends_on = None


TABLE_NAME = "website_ads_action_logs"
INDEX_NAME = "idx_web_ads_action_auth_scope"
FK_NAME = "fk_web_ads_action_auth"
UBIGINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in _inspector().get_columns(TABLE_NAME)
    }


def _index_names() -> set[str]:
    return {
        str(index["name"])
        for index in _inspector().get_indexes(TABLE_NAME)
        if index.get("name")
    }


def _foreign_key_names() -> set[str]:
    return {
        str(foreign_key["name"])
        for foreign_key in _inspector().get_foreign_keys(TABLE_NAME)
        if foreign_key.get("name")
    }


def _add_auth_scope() -> None:
    dialect = op.get_bind().dialect.name
    column_missing = "auth_id" not in _column_names()
    fk_missing = FK_NAME not in _foreign_key_names()
    if dialect == "sqlite" and (column_missing or fk_missing):
        with op.batch_alter_table(TABLE_NAME) as batch:
            if column_missing:
                batch.add_column(sa.Column("auth_id", UBIGINT, nullable=True))
            if fk_missing:
                batch.create_foreign_key(
                    FK_NAME,
                    "oauth_accounts_ttb",
                    ["auth_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
    else:
        if column_missing:
            op.add_column(TABLE_NAME, sa.Column("auth_id", UBIGINT, nullable=True))
        if fk_missing:
            op.create_foreign_key(
                FK_NAME,
                TABLE_NAME,
                "oauth_accounts_ttb",
                ["auth_id"],
                ["id"],
                ondelete="SET NULL",
            )
    if INDEX_NAME not in _index_names():
        op.create_index(
            INDEX_NAME,
            TABLE_NAME,
            ["workspace_id", "auth_id", "created_at"],
        )


def _backfill_auth_id() -> None:
    # First use the exact ad -> campaign relationship. The workspace predicate
    # prevents a malformed legacy reference from crossing tenant boundaries.
    op.execute(
        sa.text(
            """
            update website_ads_action_logs
               set auth_id = (
                   select c.auth_id
                     from website_ads_ads a
                     join website_ads_campaigns c
                       on c.id = a.campaign_local_id
                    where a.id = website_ads_action_logs.ad_local_id
                      and c.workspace_id = website_ads_action_logs.workspace_id
                    limit 1
               )
             where auth_id is null
               and ad_local_id is not null
               and exists (
                   select 1
                     from website_ads_ads a
                     join website_ads_campaigns c
                       on c.id = a.campaign_local_id
                    where a.id = website_ads_action_logs.ad_local_id
                      and c.workspace_id = website_ads_action_logs.workspace_id
               )
            """
        )
    )
    # Campaign-level legacy logs have no ad link. They are attributable only
    # when every Website Ads campaign in that workspace belongs to one auth.
    # Ambiguous and otherwise unresolvable rows intentionally remain NULL and
    # are excluded from all account-scoped queries.
    op.execute(
        sa.text(
            """
            update website_ads_action_logs
               set auth_id = (
                   select min(c.auth_id)
                     from website_ads_campaigns c
                    where c.workspace_id = website_ads_action_logs.workspace_id
               )
             where auth_id is null
               and (
                   select count(distinct c.auth_id)
                     from website_ads_campaigns c
                    where c.workspace_id = website_ads_action_logs.workspace_id
               ) = 1
            """
        )
    )


def upgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        return
    _add_auth_scope()
    _backfill_auth_id()


def downgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        return
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table(TABLE_NAME) as batch:
            if INDEX_NAME in _index_names():
                batch.drop_index(INDEX_NAME)
            if FK_NAME in _foreign_key_names():
                batch.drop_constraint(FK_NAME, type_="foreignkey")
            if "auth_id" in _column_names():
                batch.drop_column("auth_id")
        return
    if INDEX_NAME in _index_names():
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    if FK_NAME in _foreign_key_names():
        op.drop_constraint(FK_NAME, TABLE_NAME, type_="foreignkey")
    if "auth_id" in _column_names():
        op.drop_column(TABLE_NAME, "auth_id")
