"""Add durable, user-scoped AI producer intake attachments.

Revision ID: 0115_content_producer_attachments
Revises: 0114_hermes_director_execution
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.data.models.hermes_agent import HermesContentProducerAttachment


revision = "0115_content_producer_attachments"
down_revision = "0114_hermes_director_execution"
branch_labels = None
depends_on = None


TABLE = HermesContentProducerAttachment.__table__


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if TABLE.name not in existing:
        TABLE.create(bind=bind, checkfirst=True)
    else:
        actual = {
            str(item["name"])
            for item in sa.inspect(bind).get_columns(TABLE.name)
        }
        expected = {str(column.name) for column in TABLE.columns}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(f"{TABLE.name} exists with missing columns: {missing}")

    message_unique_names = {
        str(item.get("name") or "")
        for item in sa.inspect(bind).get_unique_constraints("hermes_agent_messages")
    }
    if "uq_hermes_message_turn_role" not in message_unique_names:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("hermes_agent_messages") as batch:
                batch.create_unique_constraint(
                    "uq_hermes_message_turn_role",
                    ["conversation_id", "role", "run_id"],
                )
        else:
            op.create_unique_constraint(
                "uq_hermes_message_turn_role",
                "hermes_agent_messages",
                ["conversation_id", "role", "run_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    message_unique_names = {
        str(item.get("name") or "")
        for item in sa.inspect(bind).get_unique_constraints("hermes_agent_messages")
    }
    if "uq_hermes_message_turn_role" in message_unique_names:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("hermes_agent_messages") as batch:
                batch.drop_constraint(
                    "uq_hermes_message_turn_role",
                    type_="unique",
                )
        else:
            op.drop_constraint(
                "uq_hermes_message_turn_role",
                "hermes_agent_messages",
                type_="unique",
            )
    if TABLE.name in set(sa.inspect(bind).get_table_names()):
        TABLE.drop(bind=bind, checkfirst=True)
