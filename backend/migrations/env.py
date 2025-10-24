# /opt/gmv/backend/migrations/env.py
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, event
from sqlalchemy.engine import Connection

# 让 "app.*" 能被 import（基于 /opt/gmv/backend 为根）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 读取 alembic.ini 的日志配置
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 应用配置 & 元数据
from app.core.config import settings
from app.data.db import Base  # Base.metadata
# 为将来 autogenerate 留好口子：务必确保导入模型模块（如果 __init__ 空的，请把子模块显式 import 到 __init__）
import app.data.models  # noqa: F401

target_metadata = Base.metadata


def get_url() -> str:
    # 优先环境变量（临时切库），否则走应用配置
    return os.getenv("ALEMBIC_DB_URL", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """离线模式：生成 SQL（不连库）"""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _sqlite_before_cursor_execute(
    conn: Connection, cursor, statement: str, parameters, context, executemany
):
    if "CURRENT_TIMESTAMP(6)" in statement:
        statement = statement.replace("CURRENT_TIMESTAMP(6)", "CURRENT_TIMESTAMP")
    stripped = statement.lstrip().upper()
    if stripped.startswith("CREATE INDEX ") and " IF NOT EXISTS " not in stripped:
        statement = statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
    return statement, parameters


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行"""
    connectable = engine_from_config(
        {"sqlalchemy.url": get_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    target_engine = getattr(connectable, "sync_engine", connectable)
    if target_engine.dialect.name == "sqlite":
        event.listen(
            target_engine,
            "before_cursor_execute",
            _sqlite_before_cursor_execute,
            retval=True,
        )

    with connectable.connect() as connection:
        dialect = connection.dialect.name  # 'sqlite' / 'mysql' / 'postgresql' ...
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=(dialect == "sqlite"),  # SQLite 的 ALTER 能力有限，启用 batch
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

