# app/data/models/oauth_ttb.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    String, Boolean, Enum, text, ForeignKey, UniqueConstraint, Index, JSON, LargeBinary
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.dialects.mysql import BINARY as MySQL_BINARY  # ★ 引入 BINARY

from app.data.db import Base

# 通用 BigInt + MySQL 无符号 BIGINT 变体
UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


# ---------- 密钥环（仅元数据） ----------
class CryptoKeyring(Base):
    __tablename__ = "crypto_keyrings"

    id: Mapped[int] = mapped_column(_BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql"), primary_key=True, autoincrement=True)
    key_version: Mapped[int] = mapped_column(nullable=False, unique=True)  # 建议小整数；这里用 Integer 兼容
    key_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    rotated_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )


# ---------- 平台级应用 ----------
class OAuthProviderApp(Base):
    __tablename__ = "oauth_provider_apps"
    __table_args__ = (
        UniqueConstraint("provider", "client_id", name="uk_provider_clientid"),
        Index("idx_provider_enabled", "provider", "is_enabled"),
        Index("idx_name", "name"),
        Index("fk_opa_keyring", "client_secret_key_version"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # 固定 tiktok_business
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_secret_cipher: Mapped[bytes] = mapped_column(LargeBinary(4096), nullable=False)
    client_secret_key_version: Mapped[int] = mapped_column(nullable=False, default=1)

    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))

    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id"))
    updated_by_user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id"))

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


class OAuthProviderAppRedirect(Base):
    __tablename__ = "oauth_provider_app_redirects"
    __table_args__ = (UniqueConstraint("provider_app_id", "redirect_uri", name="uk_app_redirect"),)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    provider_app_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_provider_apps.id"), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )


# ---------- 授权会话 ----------
class OAuthAuthzSession(Base):
    __tablename__ = "oauth_authz_sessions"
    __table_args__ = (
        UniqueConstraint("state", name="uk_state"),
        Index("idx_wid_status", "workspace_id", "status"),
        Index("idx_expires_at", "expires_at"),
        Index("idx_provider_app", "provider_app_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(36), nullable=False)  # UUID
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    provider_app_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_provider_apps.id"), nullable=False)
    return_to: Mapped[str | None] = mapped_column(String(512), default=None)

    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id"), default=None)
    ip_address: Mapped[bytes | None] = mapped_column(LargeBinary(16), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)

    status: Mapped[str] = mapped_column(
        Enum("pending", "consumed", "expired", "failed", name="oauth_session_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(String(512), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(MySQL_DATETIME(fsp=6), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    # 会话别名
    alias: Mapped[str | None] = mapped_column(String(128), default=None)


# ---------- 公司级长期令牌 ----------
class OAuthAccountTTB(Base):
    __tablename__ = "oauth_accounts_ttb"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider_app_id", "token_fingerprint", name="uk_wid_app_fp"),
        Index("idx_wid_status", "workspace_id", "status"),
        Index("idx_app", "provider_app_id"),
        Index("idx_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)  # auth_id
    workspace_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("workspaces.id"), nullable=False)
    provider_app_id: Mapped[int] = mapped_column(UBigInt, ForeignKey("oauth_provider_apps.id"), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(128), default=None)

    access_token_cipher: Mapped[bytes] = mapped_column(LargeBinary(4096), nullable=False)
    key_version: Mapped[int] = mapped_column(nullable=False, default=1)
    # ★★ 关键：定长 32 字节，MySQL 可索引
    token_fingerprint: Mapped[bytes] = mapped_column(MySQL_BINARY(32), nullable=False)
    scope_json: Mapped[dict | None] = mapped_column(JSON, default=None)

    status: Mapped[str] = mapped_column(
        Enum("active", "revoked", "invalid", name="oauth_account_status"),
        nullable=False,
        server_default=text("'active'"),
    )

    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )

