# app/data/models/kie_api.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Integer,
    String,
    Boolean,
    JSON,
    ForeignKey,
    text,
    UniqueConstraint,
    Index,
    BigInteger,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base

# 通用 BigInt + MySQL 无符号 BIGINT 变体（和现有模型保持一致）
UBigInt = (
    BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)


class KieApiKey(Base):
    """
    平台侧维护的 KIE API Key（不绑定具体 workspace）.
    """

    __tablename__ = "kie_api_keys"
    __table_args__ = (
        UniqueConstraint("name", name="uk_kie_key_name"),
        Index("idx_kie_key_active_default", "is_active", "is_default"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    # 便于平台管理员区分，比如 "默认 Key" / "备用 Key-1"
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # 预留 provider key，当前固定 'kie-ai'
    provider_key: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'kie-ai'"),
    )

    # Fernet-encrypted API key. The prefix identifies the key format/version.
    api_key_ciphertext: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("1"),
    )

    # 标记平台“默认使用”的 key（业务上保证全局最多一个）
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )

    # Capabilities granted to this key, for example ["video:omni_flash"].
    # NULL is retained for backward compatibility and is interpreted from the
    # provider's default capabilities by the routing service.
    scopes_json: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # Per-model routing priorities. Lower values are preferred. Keeping the
    # value on the key allows multiple accounts from one provider to act as
    # independent production routes.
    model_priorities_json: Mapped[dict[str, int] | None] = mapped_column(JSON, default=None)

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


class AiProviderModelSetting(Base):
    """Platform-wide switch for one provider/model route."""

    __tablename__ = "ai_provider_model_settings"
    __table_args__ = (
        UniqueConstraint("provider_key", "model_id", name="uk_ai_provider_model_setting"),
        Index("idx_ai_provider_model_enabled", "model_id", "is_enabled"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    provider_key: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
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


class KieTask(Base):
    """
    记录我们在 KIE 那边发起的任务（包括 sora-2-image-to-video）。
    """

    __tablename__ = "kie_api_tasks"
    __table_args__ = (
        UniqueConstraint("task_id", name="uk_kie_task_task_id"),
        Index("idx_kie_task_ws", "workspace_id"),
        Index("idx_kie_task_ws_user", "workspace_id", "created_by_user_id"),
        Index("idx_kie_task_ws_id", "workspace_id", "id"),
        Index("idx_kie_task_ws_user_id", "workspace_id", "created_by_user_id", "id"),
        Index("idx_kie_task_ws_model_id", "workspace_id", "model", "id"),
        Index("idx_kie_task_ws_user_model_id", "workspace_id", "created_by_user_id", "model", "id"),
        Index("idx_kie_task_ws_state_model_id", "workspace_id", "state", "model", "id"),
        Index("idx_kie_task_ws_user_state_model_id", "workspace_id", "created_by_user_id", "state", "model", "id"),
        Index("idx_kie_task_key", "key_id"),
        Index("idx_kie_task_state", "state"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    # 发起任务的租户 workspace
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "workspaces.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # 使用的平台级 key
    key_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "kie_api_keys.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # 发起任务的用户；用于同一租户下的历史记录隔离。
    created_by_user_id: Mapped[int | None] = mapped_column(UBigInt, default=None)

    # model 名，比如 sora-2-image-to-video
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    # KIE 返回的 taskId
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # waiting / queuing / generating / success / fail ...
    state: Mapped[str] = mapped_column(String(32), nullable=False)

    # 方便检索的 prompt 摘要
    prompt: Mapped[str | None] = mapped_column(String(2000), default=None)

    # 我们发给 KIE 的入参（input 部分）
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    # KIE 返回的 resultJson 解析后内容
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    # Structured recovery codes intentionally describe the violated contract;
    # 32 characters was too small and could make the recovery transaction
    # itself fail (for example content_factory_continuity_frame_missing).
    fail_code: Mapped[str | None] = mapped_column(String(128), default=None)
    fail_msg: Mapped[str | None] = mapped_column(String(512), default=None)

    # 消耗的积分（如果从回调/查询里拿得到，可以填）
    credits_consumed: Mapped[int | None] = mapped_column(Integer(), default=None)

    # 外部任务时间（KIE 的毫秒时间戳转换）
    external_create_time: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )
    external_complete_time: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
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


class KieFile(Base):
    """
    统一记录 KIE 侧的文件：
    - kind='upload' / 'result' / 'result_watermark' 等
    - 方便后续统一做下载 URL 代理和过期管理
    """

    __tablename__ = "kie_api_files"
    __table_args__ = (
        Index("idx_kie_file_ws", "workspace_id"),
        Index("idx_kie_file_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)

    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "workspaces.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # 使用的平台级 key
    key_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey(
            "kie_api_keys.id",
            onupdate="RESTRICT",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    # 本地任务 id，可为空（比如纯文件上传）
    task_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey(
            "kie_api_tasks.id",
            onupdate="RESTRICT",
            ondelete="SET NULL",
        ),
        default=None,
    )

    # KIE 返回的文件 URL（resultUrls 等）
    file_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # 最近一次通过 /api/v1/common/download-url 拿到的可下载 URL（20 分钟有效）
    download_url: Mapped[str | None] = mapped_column(String(1024), default=None)

    # upload / result / result_watermark ...
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    mime_type: Mapped[str | None] = mapped_column(String(64), default=None)
    size_bytes: Mapped[int | None] = mapped_column(UBigInt, default=None)

    # download_url 的失效时间（如果我们做了换算）
    expires_at: Mapped[datetime | None] = mapped_column(
        MySQL_DATETIME(fsp=6),
        default=None,
    )

    # 预留附加元数据
    meta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

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


__all__ = ["KieApiKey", "AiProviderModelSetting", "KieTask", "KieFile"]
