# app/features/platform/router_oauth_callback.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.db import get_db
from app.data.models.oauth_ttb import OAuthAuthzSession
from app.services.oauth_ttb import handle_callback_and_bind_token

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
      - 交换 access_token 并落库（先 /oauth/token，失败兜底 /oauth2/access_token）
      - 根据会话中的 return_to 302 返回前端，否则返回 JSON
    """
    q = request.query_params
    code = q.get("code") or q.get("auth_code")
    state = q.get("state")
    if not code or not state:
        raise APIError("INVALID_CALLBACK", "missing code/auth_code or state", 400)

    try:
        account, sess = await handle_callback_and_bind_token(db, code=code, state=state)
    except APIError as e:
        sess = db.query(OAuthAuthzSession).filter(OAuthAuthzSession.state == state).first()
        if sess and sess.return_to:
            from urllib.parse import urlencode, quote_plus
            url = f"{sess.return_to.rstrip('/')}/?" + urlencode(
                {"ok": 0, "code": e.code, "msg": e.message},
                quote_via=quote_plus
            )
            return RedirectResponse(url=url, status_code=302)
        raise

    if sess and sess.return_to:
        from urllib.parse import urlencode
        url = f"{sess.return_to.rstrip('/')}/?" + urlencode({"ok": 1, "auth_id": int(account.id)})
        return RedirectResponse(url=url, status_code=302)

    return JSONResponse({"ok": True, "auth_id": int(account.id)})

