from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.db import get_db
from app.services.oauth_tiktok_shop import (
    callback_redirect_url,
    fail_session,
    get_session,
    handle_callback,
    oauth_callback_error_reason,
    sync_authorized_shops,
)


router = APIRouter(prefix="/api/oauth", tags=["OAuth Callback"])


@router.get("/tiktok-shop/callback", response_model=None)
async def tiktok_shop_callback(request: Request, db: Session = Depends(get_db)):
    query = request.query_params
    state = str(query.get("state") or "").strip()
    code = str(query.get("code") or "").strip()
    error = str(query.get("error") or "").strip()
    callback_app_key = str(query.get("app_key") or "").strip()
    cookie_state = str(request.cookies.get("gmv_tiktok_shop_oauth_state") or "").strip()
    if not state:
        raise APIError("INVALID_CALLBACK", "Missing state.", 400)
    if cookie_state and cookie_state != state:
        raise APIError("STATE_MISMATCH", "OAuth state cookie does not match.", 400)
    session = get_session(db, state)
    if not session:
        raise APIError("INVALID_STATE", "Unknown TikTok Shop OAuth state.", 400)

    if error or not code or code.lower() == "null":
        session = fail_session(
            db,
            state=state,
            error_code=error or "AUTH_DENIED",
            error_message="TikTok Shop authorization was denied or cancelled.",
        ) or session
        redirect_url = callback_redirect_url(
            session,
            {
                "shop_oauth": "error",
                "code": error or "AUTH_DENIED",
                "reason": oauth_callback_error_reason(error or "AUTH_DENIED"),
            },
        )
        response = RedirectResponse(url=redirect_url, status_code=302)
        response.delete_cookie(
            "gmv_tiktok_shop_oauth_state",
            path=str(getattr(settings, "TT_SHOP_CALLBACK_PATH", "/api/oauth/tiktok-shop/callback")),
        )
        return response

    try:
        account, session = await handle_callback(
            db,
            code=code,
            state=state,
            callback_app_key=callback_app_key,
        )
        shop_count = 0
        if account.user_type != 1:
            try:
                shops = await sync_authorized_shops(
                    db,
                    workspace_id=int(session.workspace_id),
                    account_id=int(account.id),
                )
                shop_count = len(shops)
            except APIError:
                # Authorization is valid even if the first metadata sync is delayed.
                shop_count = 0
    except APIError as exc:
        session = fail_session(
            db,
            state=state,
            error_code=exc.code,
            error_message=exc.message,
        ) or session
        redirect_url = callback_redirect_url(
            session,
            {
                "shop_oauth": "error",
                "code": exc.code,
                "reason": oauth_callback_error_reason(exc.code, exc.message),
            },
        )
    else:
        redirect_url = callback_redirect_url(
            session,
            {
                "shop_oauth": "success",
                "account_id": int(account.id),
                "shop_count": shop_count,
                "account_type": "creator" if account.user_type == 1 else "seller",
            },
        )

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(
        "gmv_tiktok_shop_oauth_state",
        path=str(getattr(settings, "TT_SHOP_CALLBACK_PATH", "/api/oauth/tiktok-shop/callback")),
    )
    return response
