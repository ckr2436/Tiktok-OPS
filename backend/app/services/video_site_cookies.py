from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional
from uuid import uuid4

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.data.models import VideoSiteCookies

SUPPORTED_SITES = {"tiktok", "douyin", "youtube"}


def upsert_video_site_cookies(
    db: Session,
    *,
    site: str,
    label: str,
    cookies_json: str,
    is_active: bool = True,
    last_login_at: datetime | None = None,
    expires_at: datetime | None = None,
    extra: dict | None = None,
) -> VideoSiteCookies:
    site = site.lower()
    if site not in SUPPORTED_SITES:
        raise ValueError(f"Unsupported site: {site}")

    stmt = select(VideoSiteCookies).where(VideoSiteCookies.site == site, VideoSiteCookies.label == label)
    existing = db.execute(stmt).scalar_one_or_none()
    now = datetime.utcnow()

    if existing:
        existing.cookies_json = cookies_json
        existing.is_active = is_active
        existing.last_login_at = last_login_at or now
        existing.expires_at = expires_at
        existing.extra = extra
        existing.updated_at = now
        db.add(existing)
        return existing

    record = VideoSiteCookies(
        id=uuid4().hex,
        site=site,
        label=label,
        cookies_json=cookies_json,
        is_active=is_active,
        last_login_at=last_login_at or now,
        expires_at=expires_at,
        extra=extra,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    return record


def list_cookies(db: Session, site: str | None = None) -> Iterable[VideoSiteCookies]:
    stmt = select(VideoSiteCookies)
    if site:
        stmt = stmt.where(VideoSiteCookies.site == site.lower())
    stmt = stmt.order_by(VideoSiteCookies.updated_at.desc())
    return db.scalars(stmt).all()


def toggle_cookie(db: Session, cookie_id: str, is_active: bool) -> Optional[VideoSiteCookies]:
    stmt = select(VideoSiteCookies).where(VideoSiteCookies.id == cookie_id)
    existing = db.execute(stmt).scalar_one_or_none()
    if not existing:
        return None
    existing.is_active = is_active
    existing.updated_at = datetime.utcnow()
    db.add(existing)
    return existing


def delete_cookie(db: Session, cookie_id: str) -> Optional[VideoSiteCookies]:
    stmt = select(VideoSiteCookies).where(VideoSiteCookies.id == cookie_id)
    existing = db.execute(stmt).scalar_one_or_none()
    if not existing:
        return None
    db.delete(existing)
    return existing


def get_active_site_cookies(db: Session, site: str) -> Optional[VideoSiteCookies]:
    # MySQL does not support ``ORDER BY ... NULLS LAST`` on common production
    # versions. Use a portable CASE expression so rows with last_login_at are
    # ordered first, then newest login time, then newest update time.
    null_rank = case((VideoSiteCookies.last_login_at.is_(None), 1), else_=0)
    stmt = (
        select(VideoSiteCookies)
        .where(VideoSiteCookies.site == site.lower(), VideoSiteCookies.is_active.is_(True))
        .order_by(
            null_rank.asc(),
            VideoSiteCookies.last_login_at.desc(),
            VideoSiteCookies.updated_at.desc(),
        )
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()
