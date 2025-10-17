"""fix users datetime defaults and active_until expression

Revision ID: b3f2e1d9c003
Revises: 0002_scheduling
Create Date: 2025-10-16 21:55:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

# revision identifiers, used by Alembic.
revision = "b3f2e1d9c003"
down_revision = "0002_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    inspector = sa.inspect(conn)
    cols = {c["name"] for c in inspector.get_columns("users")}

    # ---- created_at ----
    if "created_at" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "created_at",
                mysql_dialect.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
        )
    else:
        if dialect == "mysql":
            op.execute(
                "ALTER TABLE `users` "
                "MODIFY COLUMN `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"
            )

    # ---- updated_at ----
    if "updated_at" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "updated_at",
                mysql_dialect.DATETIME(fsp=6),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            ),
        )
    else:
        if dialect == "mysql":
            op.execute(
                "ALTER TABLE `users` "
                "MODIFY COLUMN `updated_at` DATETIME(6) NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP(6) "
                "ON UPDATE CURRENT_TIMESTAMP(6)"
            )

    # ---- last_login_at ----
    if "last_login_at" not in cols:
        op.add_column(
            "users",
            sa.Column("last_login_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        )
    else:
        if dialect == "mysql":
            op.execute(
                "ALTER TABLE `users` "
                "MODIFY COLUMN `last_login_at` DATETIME(6) NULL"
            )

    # ---- deleted_at ----
    if "deleted_at" not in cols:
        op.add_column(
            "users",
            sa.Column("deleted_at", mysql_dialect.DATETIME(fsp=6), nullable=True),
        )
    else:
        if dialect == "mysql":
            op.execute(
                "ALTER TABLE `users` "
                "MODIFY COLUMN `deleted_at` DATETIME(6) NULL"
            )

    # ---- 布尔默认值兜底（避免严格模式插入出错） ----
    if dialect == "mysql":
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `is_active` TINYINT(1) NOT NULL DEFAULT 1"
        )
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `is_platform_admin` TINYINT(1) NOT NULL DEFAULT 0"
        )

        # ---- 关键：重建 active_until 生成列，避免 TIMESTAMP 溢出 ----
        # 使用 CAST('9999-12-31 23:59:59.999999' AS DATETIME(6)) 而非 TIMESTAMP(...)
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `active_until` DATETIME(6) "
            "GENERATED ALWAYS AS ("
            "  COALESCE(`deleted_at`, CAST('9999-12-31 23:59:59.999999' AS DATETIME(6)))"
            ") STORED"
        )


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "mysql":
        # 回退到较宽松的定义（不删除列）
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `created_at` DATETIME NULL"
        )
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `updated_at` DATETIME NULL"
        )
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `last_login_at` DATETIME NULL"
        )
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `deleted_at` DATETIME NULL"
        )
        # active_until 用 TIMESTAMP() 写回（仅供回退；不建议继续使用）
        op.execute(
            "ALTER TABLE `users` "
            "MODIFY COLUMN `active_until` DATETIME(6) "
            "GENERATED ALWAYS AS ("
            "  COALESCE(`deleted_at`, TIMESTAMP('9999-12-31 23:59:59.999999'))"
            ") STORED"
        )

