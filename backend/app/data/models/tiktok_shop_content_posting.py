from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import BINARY as MySQL_BINARY
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger

from app.data.db import Base


UBigInt = _BigInteger().with_variant(MySQL_BIGINT(unsigned=True), "mysql")


class TikTokShopContentPost(Base):
    """Durable, idempotent creator-video posting workflow."""

    __tablename__ = "tiktok_shop_content_posts"
    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key", name="uq_ttshop_content_post_idempotency"),
        Index("idx_ttshop_content_post_workspace_status", "workspace_id", "workflow_status"),
        Index("idx_ttshop_content_post_account_created", "account_id", "created_at"),
        Index("idx_ttshop_content_post_video", "workspace_id", "video_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id"),
        nullable=False,
    )
    account_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("oauth_tiktok_shop_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("users.id"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(MySQL_BINARY(32), nullable=False)

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    local_file_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128), default=None)
    file_size: Mapped[int] = mapped_column(UBigInt, nullable=False)
    sha256_digest: Mapped[bytes] = mapped_column(MySQL_BINARY(32), nullable=False)
    official_file_id: Mapped[str | None] = mapped_column(String(192), default=None)
    upload_md5: Mapped[str | None] = mapped_column(String(64), default=None)

    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_link_title: Mapped[str] = mapped_column(String(64), nullable=False)
    video_title: Mapped[str | None] = mapped_column(Text, default=None)
    cover_uri: Mapped[str | None] = mapped_column(Text, default=None)
    cover_timestamp_ms: Mapped[int | None] = mapped_column(UBigInt, default=None)
    music_id: Mapped[str | None] = mapped_column(String(128), default=None)

    precheck_task_id: Mapped[str | None] = mapped_column(String(192), default=None)
    precheck_status: Mapped[str | None] = mapped_column(String(32), default=None)
    precheck_issues_json: Mapped[list | None] = mapped_column(JSON, default=None)

    video_id: Mapped[str | None] = mapped_column(String(192), default=None)
    post_status: Mapped[str | None] = mapped_column(String(32), default=None)
    post_time: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    workflow_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'QUEUED'"),
    )
    publish_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )
    poll_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_poll_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)

    provider_request_ids_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    api_versions_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    last_error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    last_error_message: Mapped[str | None] = mapped_column(Text, default=None)
    last_error_request_id: Mapped[str | None] = mapped_column(String(128), default=None)

    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
