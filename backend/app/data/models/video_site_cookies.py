from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, JSON, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base


class VideoSiteCookies(Base):
    __tablename__ = "video_site_cookies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid4().hex)
    site: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    cookies_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    last_login_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(MySQL_DATETIME(fsp=6), nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_video_site_cookies_site_active", "site", "is_active"),
        UniqueConstraint("site", "label", name="uq_video_site_cookies_site_label"),
        {
            "mysql_charset": "utf8mb4",
        },
    )
