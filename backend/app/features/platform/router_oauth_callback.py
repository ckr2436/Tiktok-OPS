# app/features/platform/router_oauth_callback.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthAuthzSession, OAuthTikTokAccountAuthzSession
from app.services.oauth_ttb import (
    ensure_tiktok_account_oauth_tables,
    handle_callback_and_bind_token,
    handle_tiktok_account_callback_and_bind_token,
)

router = APIRouter(
    prefix="/api/oauth",
    tags=["OAuth Callback"],
)

@router.get("/tiktok-business/callback", response_model=None)
async def ttb_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    TikTok Business OAuth 回调：
      - 兼容 code / auth_code
      - 校验 state
      - 通过官方 v1.3 /oauth2/access_token/ 交换 access_token 并落库
      - 根据会话中的 return_to 302 返回前端，否则返回 JSON
    """
    q = request.query_params
    code = q.get("code") or q.get("auth_code")
    state = q.get("state") or request.cookies.get("gmv_tiktok_account_oauth_state")
    if not code or not state:
        raise APIError("INVALID_CALLBACK", "missing code/auth_code or state", 400)

    try:
        sess = db.query(OAuthAuthzSession).filter(OAuthAuthzSession.state == state).first()
        subject = "advertiser"
        if sess:
            account, sess = await handle_callback_and_bind_token(db, code=code, state=state)
        else:
            ensure_tiktok_account_oauth_tables(db)
            tik_sess = (
                db.query(OAuthTikTokAccountAuthzSession)
                .filter(OAuthTikTokAccountAuthzSession.state == state)
                .first()
            )
            if not tik_sess:
                raise APIError("INVALID_STATE", "Invalid or consumed state.", 400)
            subject = "tiktok_account"
            account, sess = await handle_tiktok_account_callback_and_bind_token(db, code=code, state=state)
    except APIError as e:
        sess = db.query(OAuthAuthzSession).filter(OAuthAuthzSession.state == state).first()
        if not sess:
            ensure_tiktok_account_oauth_tables(db)
            sess = (
                db.query(OAuthTikTokAccountAuthzSession)
                .filter(OAuthTikTokAccountAuthzSession.state == state)
                .first()
            )
        if sess and sess.return_to:
            from urllib.parse import urlencode, quote_plus
            url = f"{sess.return_to.rstrip('/')}/?" + urlencode(
                {"ok": 0, "code": e.code, "msg": e.message},
                quote_via=quote_plus
            )
            resp = RedirectResponse(url=url, status_code=302)
            resp.delete_cookie("gmv_tiktok_account_oauth_state", path="/api/oauth/tiktok-business/callback")
            return resp
        raise

    if sess and sess.return_to:
        from urllib.parse import urlencode
        url = f"{sess.return_to.rstrip('/')}/?" + urlencode(
            {"ok": 1, "auth_id": int(account.id), "subject": subject}
        )
        resp = RedirectResponse(url=url, status_code=302)
        resp.delete_cookie("gmv_tiktok_account_oauth_state", path="/api/oauth/tiktok-business/callback")
        return resp

    resp = JSONResponse({"ok": True, "auth_id": int(account.id), "subject": subject})
    resp.delete_cookie("gmv_tiktok_account_oauth_state", path="/api/oauth/tiktok-business/callback")
    return resp
