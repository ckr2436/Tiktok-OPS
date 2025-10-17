# app/data/models/audit_logs.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, JSON, text, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME  # ← 关键

from app.data.db import Base

# 通用 BigInt + MySQL 无符号 BIGINT 变体
UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    event_time: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )

    actor_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    actor_workspace_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    actor_ip: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(UBigInt, default=None)

    target_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    workspace_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )

    details: Mapped[dict | None] = mapped_column(JSON, default=None)

# 索引（和 DDL 对齐）
Index("idx_audit_time", AuditLog.event_time)
Index("idx_audit_action", AuditLog.action)
Index("idx_audit_workspace", AuditLog.workspace_id)

