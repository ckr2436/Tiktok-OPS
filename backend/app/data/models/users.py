# app/data/models/users.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    String,
    Enum,
    Boolean,
    text,
    Computed,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME  # ← 关键

from app.data.db import Base
from app.data.models.workspaces import Workspace

# 通用 BigInt + MySQL 无符号 BIGINT 变体
UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # “未删除维度”的唯一约束（见 active_until 生成列）
        UniqueConstraint(
            "workspace_id", "username", "active_until",
            name="uq_users_ws_username_active",
        ),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)

    # pbkdf2_sha256$<iters>$<salt>$<hash>
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    # 平台层管理员标记（平台 owner / admin）
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))

    role: Mapped[str] = mapped_column(
        Enum("owner", "admin", "member", name="user_role"),
        nullable=False,
    )

    # 9 位用户码：company_code(4) + 5位序号（在应用层生成 & 唯一约束）
    usercode: Mapped[str] = mapped_column(String(9), nullable=False, unique=True)

    created_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    # 生成列：未删除时取 9999-12-31...，用于形成“活跃态”唯一键
    # ★ 修正点：不要用 TIMESTAMP('9999-...')，会溢出
    #   改为 CAST('9999-12-31 23:59:59.999999' AS DATETIME(6))
    active_until: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        Computed(
            "COALESCE(`deleted_at`, CAST('9999-12-31 23:59:59.999999' AS DATETIME(6)))",
            persisted=True,
        ),
    )

    # 关系（可选）
    workspace: Mapped[Workspace] = relationship("Workspace", backref="users", lazy="joined")
    created_by: Mapped["User | None"] = relationship("User", remote_side="User.id", lazy="selectin")

