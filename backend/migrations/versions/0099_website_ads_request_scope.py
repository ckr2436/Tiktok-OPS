"""Scope Website Ads request idempotency keys to one authorized account.

Revision ID: 0099_website_ads_request_scope
Revises: 0098_website_ads_action_auth
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0099_website_ads_request_scope"
down_revision = "0098_website_ads_action_auth"
branch_labels = None
depends_on = None


TABLE_NAME = "website_ads_campaigns"
CONSTRAINT_NAME = "uk_web_ads_campaign_request"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _constraint_columns() -> list[str] | None:
    for constraint in _inspector().get_unique_constraints(TABLE_NAME):
        if constraint.get("name") == CONSTRAINT_NAME:
            return [str(value) for value in constraint.get("column_names") or []]
    return None


def _replace_constraint(columns: list[str]) -> None:
    existing = _constraint_columns()
    if existing == columns:
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(TABLE_NAME) as batch:
            if existing is not None:
                batch.drop_constraint(CONSTRAINT_NAME, type_="unique")
            batch.create_unique_constraint(CONSTRAINT_NAME, columns)
        return
    if existing is not None:
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="unique")
    op.create_unique_constraint(CONSTRAINT_NAME, TABLE_NAME, columns)


def upgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        return
    # The former (workspace_id, request_key) key is stricter, so existing
    # production rows cannot conflict when auth_id is added to the key.
    _replace_constraint(["workspace_id", "auth_id", "request_key"])


def downgrade() -> None:
    if TABLE_NAME not in set(_inspector().get_table_names()):
        return
    _replace_constraint(["workspace_id", "request_key"])
