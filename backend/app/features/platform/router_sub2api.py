from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Header, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import SessionUser, require_platform_admin
from app.core.security import client_ip
from app.data.db import get_db
from app.services.audit import log_event
from app.services.sub2api_oidc import (
    OIDCProtocolError,
    discovery_document,
    exchange_authorization_code,
    issue_authorization_code,
    signing_material,
    userinfo_for_access_token,
    validate_authorize_request,
    validate_runtime_config,
)


router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/sub2api",
    tags=["Platform / Sub2API SSO"],
)


def _oauth_error(exc: OIDCProtocolError) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "error_description": exc.description},
        headers=headers,
    )


@router.get("/sso", include_in_schema=False)
def enter_sub2api_admin(
    request: Request,
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    try:
        validate_runtime_config()
    except OIDCProtocolError as exc:
        return _oauth_error(exc)

    base_path = "/" + str(settings.SUB2API_PUBLIC_BASE_PATH or "/sub2api").strip("/")
    target = f"{base_path}/api/v1/auth/oauth/oidc/start?{urlencode({'redirect': '/admin'})}"
    log_event(
        db,
        action="platform.sub2api_sso.start",
        resource_type="sub2api_admin",
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        details={"target": "sub2api_admin", "protocol": "oidc_pkce"},
    )
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


@router.get("/oidc/.well-known/openid-configuration")
def oidc_discovery():
    try:
        return JSONResponse(discovery_document(), headers={"Cache-Control": "public, max-age=300"})
    except OIDCProtocolError as exc:
        return _oauth_error(exc)


@router.get("/oidc/jwks")
def oidc_jwks():
    try:
        validate_runtime_config()
        return JSONResponse(
            {"keys": [signing_material().jwk]},
            headers={"Cache-Control": "public, max-age=300"},
        )
    except OIDCProtocolError as exc:
        return _oauth_error(exc)


@router.get("/oidc/authorize")
def oidc_authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query("openid email profile"),
    state: str = Query(...),
    nonce: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    me: SessionUser = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    try:
        validate_authorize_request(
            response_type=response_type,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        code = issue_authorization_code(
            actor_user_id=int(me.id),
            actor_workspace_id=int(me.workspace_id),
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            nonce=nonce,
            code_challenge=code_challenge,
        )
    except OIDCProtocolError as exc:
        return _oauth_error(exc)

    log_event(
        db,
        action="platform.sub2api_sso.authorize",
        resource_type="sub2api_admin",
        actor_user_id=int(me.id),
        actor_workspace_id=int(me.workspace_id),
        actor_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        details={"client_id": client_id, "protocol": "oidc_pkce"},
    )
    separator = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{separator}{urlencode({'code': code, 'state': state})}"
    return RedirectResponse(location, status_code=302, headers={"Cache-Control": "no-store"})


@router.post("/oidc/token")
def oidc_token(
    grant_type: str = Form(...),
    code: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_verifier: str = Form(...),
):
    try:
        payload = exchange_authorization_code(
            grant_type=grant_type,
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except OIDCProtocolError as exc:
        return _oauth_error(exc)


@router.get("/oidc/userinfo")
def oidc_userinfo(authorization: str | None = Header(default=None)):
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        token = ""
    try:
        return JSONResponse(
            userinfo_for_access_token(token.strip()),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    except OIDCProtocolError as exc:
        return _oauth_error(exc)
