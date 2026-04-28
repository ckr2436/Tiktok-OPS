from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import VideoSiteLoginSession

DEFAULT_LOGIN_SESSION_TTL = timedelta(minutes=15)


def create_login_session(
    db: Session,
    *,
    login_session_id: str,
    site: str,
    label: str,
    status: str,
    qrcode_image_base64: str | None = None,
    expires_at: datetime | None = None,
) -> VideoSiteLoginSession:
    now = datetime.utcnow()
    record = VideoSiteLoginSession(
        id=login_session_id,
        site=site,
        label=label,
        status=status,
        qrcode_image_base64=qrcode_image_base64,
        expires_at=expires_at or now + DEFAULT_LOGIN_SESSION_TTL,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    return record


def get_login_session(db: Session, login_session_id: str) -> VideoSiteLoginSession | None:
    stmt = select(VideoSiteLoginSession).where(VideoSiteLoginSession.id == login_session_id)
    return db.execute(stmt).scalar_one_or_none()


def update_login_session(
    db: Session,
    login_session_id: str,
    *,
    status: Optional[str] = None,
    account: Optional[dict] = None,
    error_msg: Optional[str] = None,
    qrcode_image_base64: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> VideoSiteLoginSession | None:
    record = get_login_session(db, login_session_id)
    if not record:
        return None

    now = datetime.utcnow()
    if status:
        record.status = status
    if account is not None:
        record.account = account
    if error_msg is not None:
        record.error_msg = error_msg
    if qrcode_image_base64 is not None:
        record.qrcode_image_base64 = qrcode_image_base64
    record.expires_at = expires_at or (now + DEFAULT_LOGIN_SESSION_TTL)
    record.updated_at = now
    db.add(record)
    return record
