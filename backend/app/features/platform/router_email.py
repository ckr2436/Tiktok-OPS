from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.email_settings import MailEncryption, MailSendMode
from app.services.email_service import (
    get_or_create_email_setting,
    update_email_setting,
    send_test_email,
    DEFAULT_SSL_PORT,
    DEFAULT_STARTTLS_PORT,
)

router = APIRouter(prefix=f"{settings.API_PREFIX}/platform/email", tags=["Platform / Email"], dependencies=[Depends(require_platform_admin)])


class EmailSettingOut(BaseModel):
    send_mode: MailSendMode
    encryption: MailEncryption
    from_address: str
    host: str
    port: int
    auth_enabled: bool
    username: str | None = None
    has_password: bool = False


class EmailSettingIn(BaseModel):
    send_mode: MailSendMode = Field(default=MailSendMode.SMTP)
    encryption: MailEncryption = Field(default=MailEncryption.SSL)
    from_address: str = Field(min_length=3, max_length=255)
    host: str = Field(min_length=1, max_length=255)
    port: int | None = None
    auth_enabled: bool = False
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)


class TestEmailRequest(BaseModel):
    to_email: EmailStr


def _to_out(setting) -> EmailSettingOut:
    return EmailSettingOut(
        send_mode=setting.send_mode,
        encryption=setting.encryption,
        from_address=setting.from_address,
        host=setting.host,
        port=setting.port,
        auth_enabled=bool(setting.auth_enabled),
        username=setting.username,
        has_password=bool(setting.password),
    )


@router.get("/settings", response_model=EmailSettingOut)
def fetch_settings(db: Session = Depends(get_db)):
    setting = get_or_create_email_setting(db)
    return _to_out(setting)


@router.put("/settings", response_model=EmailSettingOut)
def save_settings(payload: EmailSettingIn, db: Session = Depends(get_db)):
    port = payload.port
    if not port:
        if payload.encryption == MailEncryption.SSL:
            port = DEFAULT_SSL_PORT
        elif payload.encryption == MailEncryption.STARTTLS:
            port = DEFAULT_STARTTLS_PORT
        else:
            port = 25
    setting = update_email_setting(
        db,
        send_mode=payload.send_mode.value,
        encryption=payload.encryption.value,
        from_address=payload.from_address,
        host=payload.host,
        port=port,
        auth_enabled=payload.auth_enabled,
        username=payload.username,
        password=payload.password,
    )
    return _to_out(setting)


@router.post("/test")
def test_email(payload: TestEmailRequest, db: Session = Depends(get_db)):
    setting = get_or_create_email_setting(db)
    try:
        send_test_email(setting, payload.to_email)
        return {"ok": True}
    except Exception as e:  # pragma: no cover - surface error to user
        raise APIError("SEND_FAILED", str(e), 400)
