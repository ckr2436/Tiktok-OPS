from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base


UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)


class FlowAccountProxy(Base):
    """Platform-owned proxy endpoint available to the Google Flow account pool."""

    __tablename__ = "flow_account_proxies"
    __table_args__ = (
        UniqueConstraint("name", name="uk_flow_account_proxy_name"),
        UniqueConstraint("proxy_url_fingerprint", name="uk_flow_account_proxy_endpoint"),
        Index("idx_flow_account_proxy_active", "is_active", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    proxy_url_ciphertext: Mapped[str] = mapped_column(String(2048), nullable=False)
    proxy_url_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    display_url: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


__all__ = ["FlowAccountProxy"]
