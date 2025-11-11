"""KIE API key / task / file models

Revision ID: 0017_kie_api_models
Revises: 0016_ttb_store_adv_to_link
Create Date: 2025-xx-xx
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0017_kie_api_models"
down_revision = "0016_ttb_store_adv_to_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- KIE API keys（平台级，全局，不绑定 workspace）----
    op.create_table(
        "kie_api_keys",
        sa.Column(
            "id",
            mysql.BIGINT(unsigned=True),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "provider_key",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'kie-ai'"),
        ),
        sa.Column("api_key_ciphertext", sa.String(length=512), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
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
        sa.UniqueConstraint("name", name="uk_kie_key_name"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_index(
        "idx_kie_key_active",
        "kie_api_keys",
        ["is_active"],
        unique=False,
    )
    op.create_index(
        "idx_kie_key_default",
        "kie_api_keys",
        ["is_default"],
        unique=False,
    )

    # ---- KIE 任务（按租户 workspace 记录任务，但 key 是平台级）----
    op.create_table(
        "kie_api_tasks",
        sa.Column(
            "id",
            mysql.BIGINT(unsigned=True),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "workspace_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey(
                "workspaces.id",
                onupdate="RESTRICT",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "key_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey(
                "kie_api_keys.id",
                onupdate="RESTRICT",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.String(length=2000), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("fail_code", sa.String(length=32), nullable=True),
        sa.Column("fail_msg", sa.String(length=512), nullable=True),
        sa.Column("credits_consumed", sa.Integer(), nullable=True),
        sa.Column(
            "external_create_time",
            mysql.DATETIME(fsp=6),
            nullable=True,
        ),
        sa.Column(
            "external_complete_time",
            mysql.DATETIME(fsp=6),
            nullable=True,
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
        sa.UniqueConstraint("task_id", name="uk_kie_task_task_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_index(
        "idx_kie_task_ws",
        "kie_api_tasks",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "idx_kie_task_key",
        "kie_api_tasks",
        ["key_id"],
        unique=False,
    )
    op.create_index(
        "idx_kie_task_state",
        "kie_api_tasks",
        ["state"],
        unique=False,
    )

    # ---- KIE 文件（上传 / 结果 / 带水印结果等）----
    op.create_table(
        "kie_api_files",
        sa.Column(
            "id",
            mysql.BIGINT(unsigned=True),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "workspace_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey(
                "workspaces.id",
                onupdate="RESTRICT",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "key_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey(
                "kie_api_keys.id",
                onupdate="RESTRICT",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey(
                "kie_api_tasks.id",
                onupdate="RESTRICT",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column("file_url", sa.String(length=1024), nullable=False),
        sa.Column("download_url", sa.String(length=1024), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=True),
        sa.Column(
            "size_bytes",
            mysql.BIGINT(unsigned=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            mysql.DATETIME(fsp=6),
            nullable=True,
        ),
        sa.Column("meta_json", sa.JSON(), nullable=True),
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
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_index(
        "idx_kie_file_ws",
        "kie_api_files",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "idx_kie_file_task",
        "kie_api_files",
        ["task_id"],
        unique=False,
    )


def downgrade() -> None:
    """
    安全降级：
    - 只按依赖顺序 drop_table，交给 MySQL 自动清理索引和外键
    - 先 drop 依赖方（files -> tasks），再 drop keys
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    # 1) 先删文件表（依赖 tasks、keys）
    if "kie_api_files" in existing_tables:
        op.drop_table("kie_api_files")
        existing_tables = set(inspector.get_table_names())

    # 2) 再删任务表（依赖 keys）
    if "kie_api_tasks" in existing_tables:
        op.drop_table("kie_api_tasks")
        existing_tables = set(inspector.get_table_names())

    # 3) 最后删 key 表
    if "kie_api_keys" in existing_tables:
        op.drop_table("kie_api_keys")

