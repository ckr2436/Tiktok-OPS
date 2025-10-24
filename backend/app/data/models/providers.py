"""Models for platform providers and policies."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base


UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)


class PolicyMode(str, Enum):
    WHITELIST = "whitelist"
    BLACKLIST = "blacklist"


class PolicyDomain(str, Enum):
    BUSINESS_CENTER = "bc"
    ADVERTISER = "advertiser"
    SHOP = "shop"
    PRODUCT = "product"


class PlatformProvider(Base):
    __tablename__ = "platform_providers"
    __table_args__ = (
        UniqueConstraint("key", name="uq_platform_providers_key"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )

    policies: Mapped[list["PlatformPolicy"]] = relationship(
        "PlatformPolicy", back_populates="provider", cascade="all,delete-orphan"
    )


class PlatformPolicy(Base):
    __tablename__ = "ttb_platform_policies"
    __table_args__ = (
        Index("idx_policies_provider_enabled", "provider_key", "is_enabled"),
        Index("idx_policies_workspace_enabled", "workspace_id", "is_enabled"),
        UniqueConstraint(
            "provider_key",
            "mode",
            "domain",
            name="uq_platform_policy_provider_mode_domain",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    provider_key: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("platform_providers.key", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    workspace_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
    )
    mode: Mapped[str] = mapped_column(SAEnum(PolicyMode, name="ttb_policy_mode"), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), default=None)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    description: Mapped[str | None] = mapped_column(Text, default=None)
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

    provider: Mapped[PlatformProvider] = relationship("PlatformProvider", back_populates="policies")
    items: Mapped[list["PlatformPolicyItem"]] = relationship(
        "PlatformPolicyItem", back_populates="policy", cascade="all,delete-orphan"
    )


class PlatformPolicyItem(Base):
    __tablename__ = "ttb_policy_items"
    __table_args__ = (
        UniqueConstraint("policy_id", "domain", "item_id", name="uq_policy_item"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("ttb_platform_policies.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(SAEnum(PolicyDomain, name="ttb_policy_domain"), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)

    policy: Mapped[PlatformPolicy] = relationship("PlatformPolicy", back_populates="items")

