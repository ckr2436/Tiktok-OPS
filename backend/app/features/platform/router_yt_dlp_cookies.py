from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.services import video_site_cookies
from app.services.yt_dlp_login_sessions import manager as login_session_manager

router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/yt-dlp",
    tags=["Platform / yt-dlp"],
    dependencies=[Depends(require_platform_admin)],
)


class VideoSiteCookieOut(BaseModel):
    id: str
    site: str
    label: str
    is_active: bool
    last_login_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class VideoSiteCookieCreate(BaseModel):
    site: str = Field(description="Site identifier, e.g. tiktok/douyin/youtube")
    label: str = Field(min_length=1, max_length=128)
    cookies_json: str = Field(description="Raw Playwright cookies JSON")
    is_active: bool = True
    expires_at: Optional[datetime] = None
    extra: Optional[dict] = None


class VideoSiteCookieActivation(BaseModel):
    is_active: bool


def _serialize(record) -> VideoSiteCookieOut:
    return VideoSiteCookieOut(
        id=record.id,
        site=record.site,
        label=record.label,
        is_active=bool(record.is_active),
        last_login_at=record.last_login_at,
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class LoginSessionCreate(BaseModel):
    site: str = Field(description="Site identifier, e.g. tiktok/douyin/youtube")
    label: str = Field(min_length=1, max_length=128)


class LoginSessionAccount(BaseModel):
    id: str
    site: str
    label: str
    last_login_at: Optional[datetime] = None
    is_active: bool


class LoginSessionOut(BaseModel):
    login_session_id: str
    site: str
    status: str
    qrcode_image_base64: Optional[str] = None
    account: Optional[LoginSessionAccount] = None
    error_msg: Optional[str] = None


def _serialize_login_session(state, include_qr: bool = False) -> LoginSessionOut:
    account = state.account
    account_obj = None
    if account:
        account_obj = LoginSessionAccount(**account)

    payload = {
        "login_session_id": state.login_session_id,
        "site": state.site,
        "status": state.status,
        "account": account_obj,
        "error_msg": state.error_msg,
    }
    if include_qr and getattr(state, "qrcode_image_base64", None):
        payload["qrcode_image_base64"] = state.qrcode_image_base64
    return LoginSessionOut(**payload)


@router.get("/cookies", response_model=list[VideoSiteCookieOut])
def list_site_cookies(site: Optional[str] = None, db: Session = Depends(get_db)):
    records = video_site_cookies.list_cookies(db, site=site)
    return [_serialize(item) for item in records]


@router.post("/cookies", response_model=VideoSiteCookieOut)
def create_site_cookies(payload: VideoSiteCookieCreate, db: Session = Depends(get_db)):
    record = video_site_cookies.upsert_video_site_cookies(
        db,
        site=payload.site,
        label=payload.label,
        cookies_json=payload.cookies_json,
        is_active=payload.is_active,
        expires_at=payload.expires_at,
        extra=payload.extra,
    )
    return _serialize(record)


@router.patch("/cookies/{cookie_id}", response_model=VideoSiteCookieOut)
def patch_site_cookie(cookie_id: str, payload: VideoSiteCookieActivation, db: Session = Depends(get_db)):
    record = video_site_cookies.toggle_cookie(db, cookie_id, payload.is_active)
    if not record:
        raise APIError("NOT_FOUND", "Cookies record not found", 404)
    return _serialize(record)


@router.post("/login-sessions", response_model=LoginSessionOut)
async def create_login_session(payload: LoginSessionCreate):
    site = payload.site.lower()
    if site not in video_site_cookies.SUPPORTED_SITES:
        raise APIError("INVALID_SITE", f"Unsupported site: {site}", 400)

    session = await login_session_manager.create_session(site, payload.label)
    return _serialize_login_session(session, include_qr=True)


@router.get("/login-sessions/{login_session_id}", response_model=LoginSessionOut)
async def get_login_session(login_session_id: str):
    session = login_session_manager.get_session(login_session_id)
    if not session:
        raise APIError("NOT_FOUND", "Login session not found", 404)
    include_qr = session.status == "qrcode_ready"
    return _serialize_login_session(session, include_qr=include_qr)
