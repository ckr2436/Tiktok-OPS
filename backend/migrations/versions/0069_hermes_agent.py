"""Add Hermes Agent tables and feature permissions.

Revision ID: 0069_hermes_agent
Revises: 0068_openai_whisper_filename_length
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import mysql

revision = "0069_hermes_agent"
down_revision = "0068_openai_whisper_filename_length"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _fk_type():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        return mysql.BIGINT(unsigned=True)
    return sa.BigInteger()


def _dt_type():
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        return mysql.DATETIME(fsp=6)
    return sa.DateTime()


def _json_type():
    return sa.JSON()


def upgrade() -> None:
    fk_type = _fk_type()
    dt_type = _dt_type()

    if not _table_exists("user_feature_permissions"):
        op.create_table(
            "user_feature_permissions",
            sa.Column("id", fk_type, primary_key=True, autoincrement=True),
            sa.Column("workspace_id", fk_type, nullable=False),
            sa.Column("user_id", fk_type, nullable=False),
            sa.Column("feature_key", sa.String(length=128), nullable=False),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_by_user_id", fk_type, nullable=True),
            sa.Column("updated_by_user_id", fk_type, nullable=True),
            sa.Column("created_at", dt_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.Column(
                "updated_at",
                dt_type,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.Column("deleted_at", dt_type, nullable=True),
            sa.Column(
                "active_until",
                dt_type,
                sa.Computed(
                    "COALESCE(`deleted_at`, CAST('9999-12-31 23:59:59.999999' AS DATETIME(6)))",
                    persisted=True,
                ),
            ),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], onupdate="RESTRICT", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], onupdate="RESTRICT", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], onupdate="RESTRICT", ondelete="SET NULL"),
            sa.UniqueConstraint("workspace_id", "user_id", "feature_key", "active_until", name="uq_user_feature_permission_active"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        op.create_index("idx_user_feature_permission_user", "user_feature_permissions", ["workspace_id", "user_id"])
        op.create_index(
            "idx_user_feature_permission_feature",
            "user_feature_permissions",
            ["workspace_id", "feature_key", "is_enabled"],
        )

    if not _table_exists("hermes_agent_conversations"):
        op.create_table(
            "hermes_agent_conversations",
            sa.Column("id", fk_type, primary_key=True, autoincrement=True),
            sa.Column("conversation_key", sa.String(length=191), nullable=False),
            sa.Column("workspace_id", fk_type, nullable=False),
            sa.Column("user_id", fk_type, nullable=True),
            sa.Column("task_type", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("last_response_id", sa.String(length=128), nullable=True),
            sa.Column("meta_json", _json_type(), nullable=True),
            sa.Column("created_at", dt_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.Column(
                "updated_at",
                dt_type,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], onupdate="RESTRICT", ondelete="SET NULL"),
            sa.UniqueConstraint("conversation_key", name="uq_hermes_conversation_key"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        op.create_index(
            "idx_hermes_conversation_ws_user",
            "hermes_agent_conversations",
            ["workspace_id", "user_id", "task_type"],
        )

    if not _table_exists("hermes_agent_messages"):
        op.create_table(
            "hermes_agent_messages",
            sa.Column("id", fk_type, primary_key=True, autoincrement=True),
            sa.Column("conversation_id", fk_type, nullable=False),
            sa.Column("workspace_id", fk_type, nullable=False),
            sa.Column("user_id", fk_type, nullable=True),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("content_text", sa.Text(), nullable=True),
            sa.Column("content_json", _json_type(), nullable=True),
            sa.Column("run_id", sa.String(length=32), nullable=True),
            sa.Column("created_at", dt_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.ForeignKeyConstraint(["conversation_id"], ["hermes_agent_conversations.id"], onupdate="RESTRICT", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], onupdate="RESTRICT", ondelete="SET NULL"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        op.create_index("idx_hermes_message_conversation", "hermes_agent_messages", ["conversation_id", "id"])
        op.create_index("idx_hermes_message_ws_user", "hermes_agent_messages", ["workspace_id", "user_id"])
        op.create_index("ix_hermes_agent_messages_run_id", "hermes_agent_messages", ["run_id"])

    if not _table_exists("hermes_agent_runs"):
        op.create_table(
            "hermes_agent_runs",
            sa.Column("id", fk_type, primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(length=32), nullable=False),
            sa.Column("workspace_id", fk_type, nullable=False),
            sa.Column("user_id", fk_type, nullable=True),
            sa.Column("conversation_id", fk_type, nullable=True),
            sa.Column("task_type", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("input_text", sa.Text(), nullable=True),
            sa.Column("input_json", _json_type(), nullable=True),
            sa.Column("instructions", sa.Text(), nullable=True),
            sa.Column("result_text", sa.Text(), nullable=True),
            sa.Column("result_json", _json_type(), nullable=True),
            sa.Column("hermes_response_id", sa.String(length=128), nullable=True),
            sa.Column("hermes_conversation", sa.String(length=191), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("completion_tokens", sa.Integer(), nullable=True),
            sa.Column("total_tokens", sa.Integer(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("celery_task_id", sa.String(length=64), nullable=True),
            sa.Column("meta_json", _json_type(), nullable=True),
            sa.Column("created_at", dt_type, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
            sa.Column("started_at", dt_type, nullable=True),
            sa.Column("completed_at", dt_type, nullable=True),
            sa.Column(
                "updated_at",
                dt_type,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], onupdate="RESTRICT", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], onupdate="RESTRICT", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["conversation_id"], ["hermes_agent_conversations.id"], onupdate="RESTRICT", ondelete="SET NULL"),
            sa.UniqueConstraint("run_id", name="uq_hermes_run_id"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        op.create_index("idx_hermes_run_ws_created", "hermes_agent_runs", ["workspace_id", "created_at"])
        op.create_index("idx_hermes_run_ws_user", "hermes_agent_runs", ["workspace_id", "user_id"])
        op.create_index("idx_hermes_run_status", "hermes_agent_runs", ["status"])


def downgrade() -> None:
    for table_name in (
        "hermes_agent_runs",
        "hermes_agent_messages",
        "hermes_agent_conversations",
        "user_feature_permissions",
    ):
        if _table_exists(table_name):
            op.drop_table(table_name)
