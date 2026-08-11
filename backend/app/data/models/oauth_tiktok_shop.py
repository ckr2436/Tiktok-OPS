from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Index, JSON, LargeBinary, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import BINARY as MySQL_BINARY
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base


UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class OAuthTikTokShopAuthzSession(Base):
    __tablename__ = "oauth_tiktok_shop_authz_sessions"
    __table_args__ = (
        UniqueConstraint("state", name="uk_tiktok_shop_state"),
        Index("idx_tiktok_shop_session_workspace_status", "workspace_id", "status"),
        Index("idx_tiktok_shop_session_expires", "expires_at"),
        Index("idx_tiktok_shop_session_app", "provider_app_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    provider_app_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_provider_apps.id"),
        nullable=False,
    )
    return_to: Mapped[str | None] = mapped_column(String(512), default=None)
    alias: Mapped[str | None] = mapped_column(String(128), default=None)
    authorization_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'seller'"),
    )
    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id"), default=None)
    ip_address: Mapped[bytes | None] = mapped_column(LargeBinary(16), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(
        Enum("pending", "consumed", "expired", "failed", name="oauth_tiktok_shop_session_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(String(512), default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)


class OAuthTikTokShopAccount(Base):
    __tablename__ = "oauth_tiktok_shop_accounts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider_app_id",
            "open_id",
            name="uk_tiktok_shop_account_open_id",
        ),
        Index("idx_tiktok_shop_account_workspace_status", "workspace_id", "status"),
        Index("idx_tiktok_shop_account_app", "provider_app_id"),
        Index("idx_tiktok_shop_account_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    provider_app_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_provider_apps.id"),
        nullable=False,
    )
    alias: Mapped[str | None] = mapped_column(String(128), default=None)
    open_id: Mapped[str] = mapped_column(String(192), nullable=False)
    seller_name: Mapped[str | None] = mapped_column(String(255), default=None)
    user_type: Mapped[int | None] = mapped_column(default=None)

    access_token_cipher: Mapped[bytes] = mapped_column(LargeBinary(4096), nullable=False)
    refresh_token_cipher: Mapped[bytes] = mapped_column(LargeBinary(4096), nullable=False)
    key_version: Mapped[int] = mapped_column(nullable=False, default=1)
    token_fingerprint: Mapped[bytes] = mapped_column(MySQL_BINARY(32), nullable=False)
    granted_scopes_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)

    status: Mapped[str] = mapped_column(
        Enum("active", "revoked", "invalid", "expired", name="oauth_tiktok_shop_account_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    last_error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    last_error_message: Mapped[str | None] = mapped_column(String(512), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id"), default=None)
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


class OAuthTikTokShopShop(Base):
    __tablename__ = "oauth_tiktok_shop_shops"
    __table_args__ = (
        UniqueConstraint("account_id", "shop_id", name="uk_tiktok_shop_account_shop"),
        Index("idx_tiktok_shop_shop_workspace", "workspace_id"),
        Index("idx_tiktok_shop_shop_status", "status"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    shop_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shop_code: Mapped[str | None] = mapped_column(String(128), default=None)
    shop_cipher: Mapped[str] = mapped_column(String(512), nullable=False)
    shop_name: Mapped[str | None] = mapped_column(String(255), default=None)
    region: Mapped[str | None] = mapped_column(String(32), default=None)
    timezone_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default=text("'Etc/GMT+8'"),
    )
    timezone_source: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default=text("'platform_default'"),
    )
    timezone_verified_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )
    timezone_locked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )
    seller_type: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
