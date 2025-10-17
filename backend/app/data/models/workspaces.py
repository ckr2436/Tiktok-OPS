# app/data/models/workspaces.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME  # ← 关键：使用 MySQL 方言

from app.data.db import Base

# 通用 BigInt + MySQL 无符号 BIGINT 变体
UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("company_code", name="uq_workspaces_company_code"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 4 位公司编号；平台固定为 "0000"
    company_code: Mapped[str] = mapped_column(String(4), nullable=False, unique=True)

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

