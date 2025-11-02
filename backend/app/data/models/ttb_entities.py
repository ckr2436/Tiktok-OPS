# app/data/models/ttb_entities.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    String,
    Integer,
    JSON,
    UniqueConstraint,
    Index,
    text,
    ForeignKey,
    Numeric,
    Boolean,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base

# 统一：BigInt + MySQL 无符号变体
UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer, "sqlite")
)


# --------------------------- 同步游标 ---------------------------
class TTBSyncCursor(Base):
    """
    以 (workspace_id, provider, auth_id, resource_type) 唯一，保存每类资源的增量游标/时间窗/版本。
    provider 固定 'tiktok-business'（存储层直接按迁移的 server_default）。
    """
    __tablename__ = "ttb_sync_cursors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "auth_id", "resource_type", name="uk_ttb_cursor_scope"),
        Index("idx_ttb_cursor_scope", "workspace_id", "auth_id", "resource_type"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'tiktok-business'"))
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "bc" | "advertiser" | "store" | "product"

    cursor_token: Mapped[str | None] = mapped_column(String(256), default=None)
    since_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    until_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_rev: Mapped[str | None] = mapped_column(String(64), default=None)

    extra_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


# --------------------------- 商务中心 ---------------------------
class TTBBusinessCenter(Base):
    __tablename__ = "ttb_business_centers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "bc_id", name="uk_ttb_bc_scope"),
        Index("idx_ttb_bc_scope", "workspace_id", "auth_id", "bc_id"),
        Index("idx_ttb_bc_updated", "ext_updated_time"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    bc_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    country_code: Mapped[str | None] = mapped_column(String(8), default=None)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), default=None)

    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    sync_rev: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# --------------------------- 广告主 ---------------------------
class TTBAdvertiser(Base):
    __tablename__ = "ttb_advertisers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "advertiser_id", name="uk_ttb_adv_scope"),
        Index("idx_ttb_adv_scope", "workspace_id", "auth_id", "advertiser_id"),
        Index("idx_ttb_adv_bc", "bc_id"),
        Index("idx_ttb_adv_updated", "ext_updated_time"),
        Index("idx_ttb_adv_status", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bc_id: Mapped[str | None] = mapped_column(String(64), default=None)

    name: Mapped[str | None] = mapped_column(String(255), default=None)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    industry: Mapped[str | None] = mapped_column(String(64), default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    display_timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    country_code: Mapped[str | None] = mapped_column(String(8), default=None)

    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    sync_rev: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# --------------------------- 店铺 ---------------------------
class TTBStore(Base):
    __tablename__ = "ttb_stores"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "store_id", name="uk_ttb_store_scope"),
        Index("idx_ttb_store_scope", "workspace_id", "auth_id", "store_id"),
        Index("idx_ttb_store_adv", "advertiser_id"),
        Index("idx_ttb_store_updated", "ext_updated_time"),
        Index("idx_ttb_store_status", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    store_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 官方字段：store_id
    advertiser_id: Mapped[str | None] = mapped_column(String(64), default=None)
    bc_id: Mapped[str | None] = mapped_column(String(64), default=None)
    store_type: Mapped[str | None] = mapped_column(String(32), default=None)
    store_code: Mapped[str | None] = mapped_column(String(64), default=None)
    store_authorized_bc_id: Mapped[str | None] = mapped_column(String(64), default=None)

    name: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    region_code: Mapped[str | None] = mapped_column(String(8), default=None)

    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    sync_rev: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# --------------------------- 商品 ---------------------------
class TTBProduct(Base):
    __tablename__ = "ttb_products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "product_id", name="uk_ttb_product_scope"),
        Index("idx_ttb_product_scope", "workspace_id", "auth_id", "product_id"),
        Index("idx_ttb_product_store", "store_id"),
        Index("idx_ttb_product_updated", "ext_updated_time"),
        Index("idx_ttb_product_status", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), default=None)

    title: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str | None] = mapped_column(String(32), default=None)

    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    price: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    stock: Mapped[int | None] = mapped_column(Integer, default=None)

    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    sync_rev: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# --------------------------- BC ↔ Advertiser 关系表 ---------------------------
class TTBBCAdvertiserLink(Base):
    __tablename__ = "ttb_bc_advertiser_links"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "bc_id",
            "advertiser_id",
            name="uk_ttb_bc_adv_link_scope",
        ),
        Index("idx_ttb_bc_adv_link_adv", "advertiser_id"),
        Index("idx_ttb_bc_adv_link_bc", "bc_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    bc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    relation_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNKNOWN'"), default="UNKNOWN"
    )
    source: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# --------------------------- Advertiser ↔ Store 关系表 ---------------------------
class TTBAdvertiserStoreLink(Base):
    __tablename__ = "ttb_advertiser_store_links"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "auth_id",
            "advertiser_id",
            "store_id",
            name="uk_ttb_adv_store_link_scope",
        ),
        Index("idx_ttb_adv_store_link_adv", "advertiser_id"),
        Index("idx_ttb_adv_store_link_store", "store_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    advertiser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    relation_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNKNOWN'"), default="UNKNOWN"
    )
    store_authorized_bc_id: Mapped[str | None] = mapped_column(String(64), default=None)
    bc_id_hint: Mapped[str | None] = mapped_column(String(64), default=None)
    source: Mapped[str | None] = mapped_column(String(64), default=None)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


# --------------------------- 绑定配置 ---------------------------
class TTBBindingConfig(Base):
    __tablename__ = "ttb_binding_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", name="uk_ttb_binding_scope"),
        Index("idx_ttb_binding_scope", "workspace_id", "auth_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    bc_id: Mapped[str | None] = mapped_column(String(64), default=None)
    advertiser_id: Mapped[str | None] = mapped_column(String(64), default=None)
    store_id: Mapped[str | None] = mapped_column(String(64), default=None)

    auto_sync_products: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    auto_sync_schedule_id: Mapped[int | None] = mapped_column(
        UBigInt, ForeignKey("schedules.id", onupdate="RESTRICT", ondelete="SET NULL"), default=None
    )

    last_manual_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_manual_sync_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    last_auto_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_auto_sync_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )

