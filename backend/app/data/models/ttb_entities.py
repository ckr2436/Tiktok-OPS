# app/data/models/ttb_entities.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    String, Integer, JSON, UniqueConstraint, Index, text, ForeignKey, Numeric
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base

# 统一：BigInt + MySQL 无符号变体
UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


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

    resource_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # "bc" | "advertiser" | "shop" | "product" | "adgroup"

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
class TTBShop(Base):
    __tablename__ = "ttb_shops"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "shop_id", name="uk_ttb_shop_scope"),
        Index("idx_ttb_shop_scope", "workspace_id", "auth_id", "shop_id"),
        Index("idx_ttb_shop_adv", "advertiser_id"),
        Index("idx_ttb_shop_updated", "ext_updated_time"),
        Index("idx_ttb_shop_status", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)  # store_id / shop_id
    advertiser_id: Mapped[str | None] = mapped_column(String(64), default=None)
    bc_id: Mapped[str | None] = mapped_column(String(64), default=None)

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


# --------------------------- 广告组 ---------------------------
class TTBAdgroup(Base):
    __tablename__ = "ttb_adgroups"
    __table_args__ = (
        UniqueConstraint("workspace_id", "auth_id", "adgroup_id", name="uk_ttb_adgroup_scope"),
        Index("idx_ttb_adgroup_scope", "workspace_id", "auth_id", "adgroup_id"),
        Index("idx_ttb_adgroup_advertiser", "advertiser_id"),
        Index("idx_ttb_adgroup_campaign", "campaign_id"),
        Index("idx_ttb_adgroup_operation_status", "operation_status"),
        Index("idx_ttb_adgroup_primary_status", "primary_status"),
        Index("idx_ttb_adgroup_secondary_status", "secondary_status"),
        Index("idx_ttb_adgroup_updated", "ext_updated_time"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )
    auth_id: Mapped[int] = mapped_column(
        UBigInt, ForeignKey("oauth_accounts_ttb.id", onupdate="RESTRICT", ondelete="CASCADE"), nullable=False
    )

    adgroup_id: Mapped[str] = mapped_column(String(64), nullable=False)
    advertiser_id: Mapped[str | None] = mapped_column(String(64), default=None)
    campaign_id: Mapped[str | None] = mapped_column(String(64), default=None)

    name: Mapped[str | None] = mapped_column(String(255), default=None)
    operation_status: Mapped[str | None] = mapped_column(String(32), default=None)
    primary_status: Mapped[str | None] = mapped_column(String(32), default=None)
    secondary_status: Mapped[str | None] = mapped_column(String(64), default=None)

    budget: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    budget_mode: Mapped[str | None] = mapped_column(String(32), default=None)
    optimization_goal: Mapped[str | None] = mapped_column(String(64), default=None)
    promotion_type: Mapped[str | None] = mapped_column(String(64), default=None)
    bid_type: Mapped[str | None] = mapped_column(String(32), default=None)
    bid_strategy: Mapped[str | None] = mapped_column(String(32), default=None)

    schedule_start_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    schedule_end_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_created_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    ext_updated_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

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
        Index("idx_ttb_product_shop", "shop_id"),
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
    shop_id: Mapped[str | None] = mapped_column(String(64), default=None)

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

