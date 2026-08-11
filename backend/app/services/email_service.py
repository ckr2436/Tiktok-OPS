from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Optional

from sqlalchemy.orm import Session

from app.data.models.email_settings import PlatformEmailSetting, MailEncryption


# RFC 8314 recommends port 465 for implicit TLS (SMTPS)
DEFAULT_SSL_PORT = 465
DEFAULT_STARTTLS_PORT = 587


def _default_port(encryption: str | MailEncryption) -> int:
    if str(encryption) == MailEncryption.STARTTLS.value:
        return DEFAULT_STARTTLS_PORT
    if str(encryption) == MailEncryption.NONE.value:
        return 25
    return DEFAULT_SSL_PORT


def get_or_create_email_setting(db: Session) -> PlatformEmailSetting:
    setting = db.query(PlatformEmailSetting).order_by(PlatformEmailSetting.id.asc()).first()
    if setting:
        return setting
    setting = PlatformEmailSetting(
        send_mode="SMTP",
        encryption=MailEncryption.SSL.value,
        from_address="",
        host="",
        port=_default_port(MailEncryption.SSL),
        auth_enabled=False,
        username=None,
        password=None,
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting


def update_email_setting(
    db: Session,
    *,
    send_mode: str,
    encryption: str,
    from_address: str,
    host: str,
    port: Optional[int],
    auth_enabled: bool,
    username: Optional[str],
    password: Optional[str] | None,
) -> PlatformEmailSetting:
    setting = get_or_create_email_setting(db)
    setting.send_mode = send_mode
    setting.encryption = encryption
    setting.from_address = from_address
    setting.host = host
    setting.port = int(port) if port else _default_port(encryption)
    setting.auth_enabled = bool(auth_enabled)
    setting.username = username if auth_enabled else None
    if not auth_enabled:
        setting.password = None
    elif password:
        setting.password = password
    db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting


def send_test_email(setting: PlatformEmailSetting, to_email: str) -> None:
    if not setting.host or not setting.from_address:
        raise ValueError("邮件服务器地址或发件人地址未配置")

    msg = EmailMessage()
    msg["Subject"] = "GMV Ops 邮件发送测试"
    msg["From"] = setting.from_address
    msg["To"] = to_email
    msg.set_content(
        "这是一封由 GMV Ops 平台发出的测试邮件。\n\n"
        "如果你收到了这封邮件，说明邮件服务器配置成功。"
    )

    if setting.encryption == MailEncryption.SSL.value:
        smtp = smtplib.SMTP_SSL(setting.host, setting.port or DEFAULT_SSL_PORT, timeout=15)
    else:
        smtp = smtplib.SMTP(setting.host, setting.port or _default_port(setting.encryption), timeout=15)
    with smtp as server:
        server.ehlo()
        if setting.encryption == MailEncryption.STARTTLS.value:
            server.starttls()
            server.ehlo()
        if setting.auth_enabled:
            if not setting.username or not setting.password:
                raise ValueError("已启用身份认证，但账号或密码未填写")
            server.login(setting.username, setting.password)
        server.send_message(msg)
