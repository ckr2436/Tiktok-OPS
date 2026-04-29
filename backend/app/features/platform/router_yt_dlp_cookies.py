from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import require_platform_admin
from app.core.errors import APIError
from app.data.db import get_db
from app.services import video_site_cookies

logger = logging.getLogger("gmv.ytdlp.cookies")

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


class VideoSiteCookieDeleteOut(BaseModel):
    deleted: bool
    id: str


class VideoSiteCookieCreate(BaseModel):
    site: str = Field(description="Site identifier, e.g. tiktok/douyin/youtube")
    label: str = Field(min_length=1, max_length=128)
    cookies_json: str = Field(description="Playwright / browser extension exported cookies JSON array")
    is_active: bool = True
    expires_at: Optional[datetime] = None
    extra: Optional[dict] = None

    @field_validator("site")
    @classmethod
    def validate_site(cls, value: str) -> str:
        site = value.lower().strip()
        if site not in video_site_cookies.SUPPORTED_SITES:
            raise ValueError(f"Unsupported site: {site}")
        return site

    @field_validator("cookies_json")
    @classmethod
    def validate_cookies_json(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("cookies_json cannot be empty")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("cookies_json must be a valid JSON array exported from browser cookies") from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("cookies_json must be a non-empty JSON array")

        normalized: list[dict] = []
        skipped_empty_name = 0
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("each cookie must be an object")

            name = item.get("name")
            name = name.strip() if isinstance(name, str) else name

            # Some Chrome cookie exporters include pseudo rows with an empty name,
            # for example {"name": "", "value": "douyin.com"}. They are not valid
            # Netscape cookie entries and yt-dlp cannot use them, so ignore them.
            if not name:
                skipped_empty_name += 1
                continue

            if item.get("value") is None:
                raise ValueError("each cookie must include value")
            domain = item.get("domain")
            domain = domain.strip() if isinstance(domain, str) else domain
            if not domain:
                raise ValueError("each cookie must include domain")

            normalized_item = dict(item)
            normalized_item["name"] = str(name)
            normalized_item["value"] = str(item.get("value"))
            normalized_item["domain"] = str(domain)
            normalized_item["path"] = normalized_item.get("path") or "/"
            normalized.append(normalized_item)

        if not normalized:
            raise ValueError("cookies_json has no valid cookie rows after filtering empty-name entries")

        if skipped_empty_name:
            logger.info("ignored empty-name cookie rows", extra={"skipped": skipped_empty_name})
        return json.dumps(normalized, ensure_ascii=False)


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


@router.get("/cookies", response_model=list[VideoSiteCookieOut])
def list_site_cookies(site: Optional[str] = None, db: Session = Depends(get_db)):
    records = video_site_cookies.list_cookies(db, site=site)
    return [_serialize(item) for item in records]


@router.post("/cookies", response_model=VideoSiteCookieOut)
def create_site_cookies(payload: VideoSiteCookieCreate, db: Session = Depends(get_db)):
    record = video_site_cookies.upsert_video_site_cookies(
        db,
        site=payload.site,
        label=payload.label.strip(),
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


@router.delete("/cookies/{cookie_id}", response_model=VideoSiteCookieDeleteOut)
def delete_site_cookie(cookie_id: str, db: Session = Depends(get_db)):
    record = video_site_cookies.delete_cookie(db, cookie_id)
    if not record:
        raise APIError("NOT_FOUND", "Cookies record not found", 404)
    return VideoSiteCookieDeleteOut(deleted=True, id=cookie_id)
