from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.providers.tiktok_business.gmvmax_client import TikTokBusinessGMVMaxClient
from app.services.oauth_ttb import (
    get_access_token_plain,
    get_credentials_for_auth_id,
)
from app.services.ttb_api import TTBApiClient


def build_ttb_client(
    db: Session,
    auth_id: int,
    *,
    qps: Optional[float] = None,
) -> TTBApiClient:
    """Construct a :class:`TTBApiClient` using tenant OAuth credentials."""
    token, _ = get_access_token_plain(db, int(auth_id))
    app_id, app_secret, _ = get_credentials_for_auth_id(db, int(auth_id))
    kwargs: dict[str, object] = {
        "access_token": token,
        "app_id": app_id,
        "app_secret": app_secret,
    }
    if qps is not None:
        kwargs["qps"] = qps
    return TTBApiClient(**kwargs)


def build_ttb_gmvmax_client(
    db: Session,
    auth_id: int,
    *,
    qps: Optional[float] = None,
    timeout: Optional[float] = None,
) -> TikTokBusinessGMVMaxClient:
    """Construct a :class:`TikTokBusinessGMVMaxClient` using tenant OAuth credentials."""

    token, _ = get_access_token_plain(db, int(auth_id))
    app_id, app_secret, _ = get_credentials_for_auth_id(db, int(auth_id))
    kwargs: dict[str, object] = {
        "access_token": token,
        "app_id": app_id,
        "app_secret": app_secret,
    }
    if qps is not None:
        kwargs["qps"] = qps
    if timeout is not None:
        kwargs["timeout"] = timeout
    return TikTokBusinessGMVMaxClient(**kwargs)
