from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, Enum as SAEnum, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger as _BigInteger
from sqlalchemy.dialects.mysql import BIGINT as MySQL_BIGINT
from sqlalchemy.dialects.mysql import DATETIME as MySQL_DATETIME

from app.data.db import Base


UBigInt = (
    _BigInteger()
    .with_variant(MySQL_BIGINT(unsigned=True), "mysql")
    .with_variant(Integer(), "sqlite")
)


class MailSendMode(str, Enum):
    SMTP = "SMTP"


class MailEncryption(str, Enum):
    SSL = "SSL"
    STARTTLS = "STARTTLS"
    NONE = "NONE"


class PlatformEmailSetting(Base):
    __tablename__ = "platform_email_settings"

    id: Mapped[int] = mapped_column(UBigInt, primary_key=True, autoincrement=True)
    send_mode: Mapped[str] = mapped_column(SAEnum(MailSendMode, name="mail_send_mode"), nullable=False)
    encryption: Mapped[str] = mapped_column(
        SAEnum(MailEncryption, name="mail_encryption"), nullable=False, server_default=MailEncryption.SSL.value
    )
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        MySQL_DATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
        nullable=False,
    )


__all__ = ["PlatformEmailSetting", "MailSendMode", "MailEncryption"]
