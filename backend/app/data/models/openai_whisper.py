from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, text
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


class OpenAIWhisperJob(Base):
    __tablename__ = "openai_whisper_jobs"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    workspace_id: Mapped[int] = mapped_column(
        UBigInt,
        ForeignKey("workspaces.id", onupdate="RESTRICT", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        UBigInt,
        ForeignKey("users.id", onupdate="RESTRICT", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    filename: Mapped[str | None] = mapped_column(String(255), default=None)
    file_size: Mapped[int | None] = mapped_column(UBigInt, default=None)
    content_type: Mapped[str | None] = mapped_column(String(128), default=None)
    video_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'pending'"))
    error: Mapped[str | None] = mapped_column(Text, default=None)
    source_language: Mapped[str | None] = mapped_column(String(32), default=None)
    detected_language: Mapped[str | None] = mapped_column(String(32), default=None)
    target_language: Mapped[str | None] = mapped_column(String(32), default=None)
    translation_language: Mapped[str | None] = mapped_column(String(32), default=None)
    translate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    show_bilingual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    celery_task_id: Mapped[str | None] = mapped_column(String(64), default=None)
    segments_count: Mapped[int | None] = mapped_column(Integer, default=None)
    translation_segments_count: Mapped[int | None] = mapped_column(Integer, default=None)
    started_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), default=None)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )
