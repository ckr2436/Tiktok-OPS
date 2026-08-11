from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_ttb import OAuthProviderApp
from app.data.models.oauth_tiktok_shop import (
    OAuthTikTokShopAccount,
    OAuthTikTokShopAuthzSession,
    OAuthTikTokShopShop,
)
from app.services.crypto import decrypt_blob_to_text, encrypt_text_to_blob, sha256_fingerprint
from app.services.oauth_ttb import get_or_bootstrap_key_version


logger = logging.getLogger("gmv.oauth_tiktok_shop")
_FIXED_SHOP_ORDER_TIMEZONE = "Etc/GMT+8"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _apply_shop_timezone_policy(
    shop: OAuthTikTokShopShop,
    *,
    verified_at: datetime,
) -> None:
    """Keep source order timestamps on the merchant-confirmed fixed UTC-8 clock."""

    shop.timezone_name = _FIXED_SHOP_ORDER_TIMEZONE
    shop.timezone_source = "merchant_confirmed_fixed_utc_minus_8"
    shop.timezone_verified_at = shop.timezone_verified_at or verified_at
    shop.timezone_locked = True


def _ip_to_bytes(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value).packed
    except ValueError:
        return None


def _normalize_alias(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized[:128] or None


def _normalize_return_to(value: str | None, workspace_id: int) -> str:
    fallback = f"/tenants/{int(workspace_id)}/tiktok-shop"
    raw = str(value or "").strip()
    if not raw:
        return fallback
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or not raw.startswith("/") or raw.startswith("//"):
        raise APIError("INVALID_RETURN_TO", "return_to must be a same-origin absolute path.", 400)
    return raw[:512]


def _app_secret_aad(app: OAuthProviderApp) -> str:
    return f"{app.provider}|{app.client_id}|{app.redirect_uri}"


def _token_aad(app: OAuthProviderApp, open_id: str) -> str:
    return f"tiktok_shop|{app.client_id}|{app.service_id}|{open_id}"


def _decrypt_app_secret(app: OAuthProviderApp) -> str:
    return decrypt_blob_to_text(app.client_secret_cipher, aad_text=_app_secret_aad(app))


def _get_enabled_app(db: Session, provider_app_id: int | None = None) -> OAuthProviderApp:
    query = select(OAuthProviderApp).where(
        OAuthProviderApp.provider == "tiktok_shop",
        OAuthProviderApp.is_enabled.is_(True),
    )
    if provider_app_id:
        query = query.where(OAuthProviderApp.id == int(provider_app_id))
    app = db.scalar(query.order_by(OAuthProviderApp.id.asc()))
    if not app or not str(app.service_id or "").strip():
        raise APIError(
            "TIKTOK_SHOP_APP_NOT_CONFIGURED",
            "TikTok Shop app key, service ID, secret, and redirect URI must be configured by a platform owner.",
            409,
        )
    return app


def create_authorization_session(
    db: Session,
    *,
    workspace_id: int,
    provider_app_id: int | None,
    created_by_user_id: int | None,
    client_ip: str | None,
    user_agent: str | None,
    return_to: str | None,
    alias: str | None,
    authorization_type: str = "seller",
) -> tuple[OAuthTikTokShopAuthzSession, str]:
    app = _get_enabled_app(db, provider_app_id)
    normalized_type = str(authorization_type or "seller").strip().lower()
    if normalized_type not in {"seller", "creator"}:
        raise APIError("INVALID_AUTHORIZATION_TYPE", "authorization_type must be seller or creator.", 400)
    state = str(uuid.uuid4())
    ttl = max(300, min(int(getattr(settings, "OAUTH_SESSION_TTL_SECONDS", 3600)), 3600))
    session = OAuthTikTokShopAuthzSession(
        state=state,
        workspace_id=int(workspace_id),
        provider_app_id=int(app.id),
        return_to=_normalize_return_to(return_to, int(workspace_id)),
        alias=_normalize_alias(alias),
        authorization_type=normalized_type,
        created_by_user_id=created_by_user_id,
        ip_address=_ip_to_bytes(client_ip),
        user_agent=str(user_agent or "")[:512] or None,
        status="pending",
        expires_at=_utcnow_naive() + timedelta(seconds=ttl),
    )
    db.add(session)
    db.flush()
    if normalized_type == "creator":
        query = urlencode({"app_key": str(app.client_id), "state": state})
        auth_url = f"{settings.TT_SHOP_CREATOR_AUTH_URL.rstrip('/')}?{query}"
    else:
        query = urlencode({"service_id": str(app.service_id), "state": state})
        auth_url = f"{settings.TT_SHOP_US_AUTH_URL.rstrip('/')}?{query}"
    logger.info(
        "TikTok Shop authorization started workspace_id=%s provider_app_id=%s authorization_type=%s state=%s",
        workspace_id,
        app.id,
        normalized_type,
        state,
    )
    return session, auth_url


def get_session(db: Session, state: str) -> OAuthTikTokShopAuthzSession | None:
    return db.scalar(
        select(OAuthTikTokShopAuthzSession).where(OAuthTikTokShopAuthzSession.state == str(state))
    )


def fail_session(
    db: Session,
    *,
    state: str,
    error_code: str,
    error_message: str,
) -> OAuthTikTokShopAuthzSession | None:
    session = get_session(db, state)
    if session and session.status == "pending":
        session.status = "failed"
        session.error_code = str(error_code or "AUTHORIZATION_FAILED")[:64]
        session.error_message = str(error_message or "Authorization failed")[:512]
        session.consumed_at = _utcnow_naive()
        db.add(session)
    return session


def _parse_expiry(source: Mapping[str, Any], absolute_key: str, relative_key: str) -> datetime | None:
    raw = source.get(absolute_key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value > 1_000_000_000:
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    try:
        seconds = int(source.get(relative_key))
    except (TypeError, ValueError):
        seconds = 0
    return _utcnow_naive() + timedelta(seconds=seconds) if seconds > 0 else None


def _scope_list(source: Mapping[str, Any]) -> list[str]:
    raw = source.get("granted_scopes") or source.get("granted_permissions") or source.get("scope") or []
    if isinstance(raw, str):
        raw = raw.replace(" ", ",").split(",")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def oauth_callback_error_reason(error_code: str | None, error_message: str | None = None) -> str:
    code = str(error_code or "").strip().upper()
    message = str(error_message or "").strip().lower()
    if code == "APP_KEY_MISMATCH" or any(
        marker in message
        for marker in ("invalid client_key", "invalid client key", "invalid app_key", "invalid app key")
    ):
        return "APP_CREDENTIALS_INVALID"
    if code == "SESSION_EXPIRED":
        return "AUTH_SESSION_EXPIRED"
    if code in {"AUTH_DENIED", "ACCESS_DENIED", "USER_CANCELLED"}:
        return "AUTH_DENIED"
    if code == "TIKTOK_SHOP_UNAVAILABLE":
        return "TIKTOK_SHOP_UNAVAILABLE"
    if code == "CREATOR_TOKEN_REQUIRED":
        return "CREATOR_TOKEN_REQUIRED"
    if code == "CREATOR_VIDEO_SCOPE_REQUIRED":
        return "CREATOR_VIDEO_SCOPE_REQUIRED"
    if code == "TOKEN_EXCHANGE_FAILED" and (
        "auth_code" in message
        or "auth code" in message
        or "authorization code" in message
        or "authorisation code" in message
    ):
        return "AUTH_CODE_INVALID_OR_EXPIRED"
    if code == "TOKEN_EXCHANGE_FAILED":
        return "TOKEN_EXCHANGE_FAILED"
    return "AUTHORIZATION_FAILED"


def parse_token_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise APIError("TOKEN_EXCHANGE_FAILED", "TikTok Shop returned invalid JSON.", 502)
    try:
        code = int(payload.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        message = str(payload.get("message") or payload.get("msg") or "Token exchange failed")[:512]
        request_id = str(payload.get("request_id") or payload.get("requestId") or "").strip()[:128] or None
        raise APIError(
            "TOKEN_EXCHANGE_FAILED",
            message,
            502,
            data={"provider_code": code, "request_id": request_id},
        )
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    access_token = str(source.get("access_token") or "").strip()
    refresh_token = str(source.get("refresh_token") or "").strip()
    open_id = str(source.get("open_id") or "").strip()
    if not access_token or not refresh_token or not open_id:
        raise APIError("TOKEN_EXCHANGE_FAILED", "Token response is missing required fields.", 502)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "open_id": open_id,
        "seller_name": str(source.get("seller_name") or source.get("name") or "").strip() or None,
        "user_type": source.get("user_type"),
        "granted_scopes": _scope_list(source),
        "expires_at": _parse_expiry(source, "access_token_expire_in", "expires_in"),
        "refresh_expires_at": _parse_expiry(
            source,
            "refresh_token_expire_in",
            "refresh_token_expires_in",
        ),
        "raw": {
            key: ("***" if key in {"access_token", "refresh_token", "app_secret"} else value)
            for key, value in source.items()
        },
    }


async def _get_json(url: str, *, params: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> dict:
    timeout = float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15.0))
    try:
        async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
            response = await client.get(url, params=dict(params), headers=dict(headers or {}))
    except httpx.RequestError:
        raise APIError("TIKTOK_SHOP_UNAVAILABLE", "TikTok Shop API is temporarily unavailable.", 502)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code >= 400:
        message = "TikTok Shop request failed."
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("msg") or message)[:512]
        raise APIError("TIKTOK_SHOP_HTTP_ERROR", message, 502)
    if not isinstance(payload, dict):
        raise APIError("TIKTOK_SHOP_INVALID_RESPONSE", "TikTok Shop returned invalid JSON.", 502)
    return payload


async def exchange_authorization_code(app: OAuthProviderApp, code: str) -> dict[str, Any]:
    try:
        payload = await _get_json(
            settings.TT_SHOP_TOKEN_URL,
            params={
                "app_key": app.client_id,
                "app_secret": _decrypt_app_secret(app),
                "auth_code": str(code),
                "grant_type": "authorized_code",
            },
        )
        return parse_token_response(payload)
    except APIError as exc:
        safe_data = exc.data if isinstance(exc.data, dict) else {}
        safe_message = str(exc.message or "").replace("\r", " ").replace("\n", " ")[:256]
        logger.warning(
            "TikTok Shop token exchange failed provider_app_id=%s app_key=%s "
            "error_code=%s provider_code=%s request_id=%s provider_message=%s",
            app.id,
            app.client_id,
            exc.code,
            safe_data.get("provider_code"),
            safe_data.get("request_id"),
            safe_message,
        )
        raise


def _persist_token(
    db: Session,
    *,
    app: OAuthProviderApp,
    session: OAuthTikTokShopAuthzSession,
    token: Mapping[str, Any],
) -> OAuthTikTokShopAccount:
    open_id = str(token["open_id"])
    account = db.scalar(
        select(OAuthTikTokShopAccount).where(
            OAuthTikTokShopAccount.workspace_id == int(session.workspace_id),
            OAuthTikTokShopAccount.provider_app_id == int(app.id),
            OAuthTikTokShopAccount.open_id == open_id,
        )
    )
    if account is None:
        account = OAuthTikTokShopAccount(
            workspace_id=int(session.workspace_id),
            provider_app_id=int(app.id),
            open_id=open_id,
            created_by_user_id=session.created_by_user_id,
        )
    key_version = get_or_bootstrap_key_version(db)
    aad = _token_aad(app, open_id)
    access_token = str(token["access_token"])
    refresh_token = str(token["refresh_token"])
    account.alias = _normalize_alias(session.alias) or account.alias
    account.seller_name = token.get("seller_name") or account.seller_name
    try:
        account.user_type = int(token.get("user_type")) if token.get("user_type") is not None else None
    except (TypeError, ValueError):
        account.user_type = None
    account.access_token_cipher = encrypt_text_to_blob(
        access_token,
        key_version=key_version,
        aad_text=aad,
    )
    account.refresh_token_cipher = encrypt_text_to_blob(
        refresh_token,
        key_version=key_version,
        aad_text=aad,
    )
    account.key_version = key_version
    account.token_fingerprint = sha256_fingerprint(access_token)
    account.granted_scopes_json = list(token.get("granted_scopes") or [])
    account.raw_json = dict(token.get("raw") or {})
    account.status = "active"
    account.expires_at = token.get("expires_at")
    account.refresh_expires_at = token.get("refresh_expires_at")
    account.last_error_code = None
    account.last_error_message = None
    account.revoked_at = None
    db.add(account)
    db.flush()
    return account


def _validate_authorization_token(
    session: OAuthTikTokShopAuthzSession,
    token: Mapping[str, Any],
) -> None:
    authorization_type = str(getattr(session, "authorization_type", None) or "seller").lower()
    raw_user_type = token.get("user_type")
    try:
        user_type = int(raw_user_type) if raw_user_type is not None else None
    except (TypeError, ValueError):
        user_type = None
    scopes = {str(value) for value in (token.get("granted_scopes") or [])}
    if authorization_type == "creator":
        if user_type != 1:
            raise APIError(
                "CREATOR_TOKEN_REQUIRED",
                "TikTok Shop returned a non-creator token. Authorize with a creator account.",
                409,
            )
        if "creator.video.write" not in scopes:
            raise APIError(
                "CREATOR_VIDEO_SCOPE_REQUIRED",
                "Creator authorization is missing creator.video.write. Re-authorize after approving the scope.",
                409,
                data={"required_scope": "creator.video.write"},
            )
    elif user_type == 1:
        raise APIError(
            "SELLER_TOKEN_REQUIRED",
            "TikTok Shop returned a creator token for a seller authorization request.",
            409,
        )


async def handle_callback(
    db: Session,
    *,
    code: str,
    state: str,
    callback_app_key: str | None = None,
) -> tuple[OAuthTikTokShopAccount, OAuthTikTokShopAuthzSession]:
    session = get_session(db, state)
    if not session or session.status != "pending":
        raise APIError("INVALID_STATE", "Invalid or consumed TikTok Shop state.", 400)
    if session.expires_at < _utcnow_naive():
        session.status = "expired"
        session.error_code = "SESSION_EXPIRED"
        session.error_message = "Authorization session expired."
        db.add(session)
        raise APIError("SESSION_EXPIRED", "Authorization session expired.", 400)
    app = _get_enabled_app(db, int(session.provider_app_id))
    returned_app_key = str(callback_app_key or "").strip()
    if returned_app_key and returned_app_key != str(app.client_id):
        logger.warning(
            "TikTok Shop callback app key mismatch provider_app_id=%s expected_app_key=%s returned_app_key=%s",
            app.id,
            app.client_id,
            returned_app_key,
        )
        raise APIError(
            "APP_KEY_MISMATCH",
            "TikTok Shop callback app key does not match the authorization session.",
            400,
        )
    token = await exchange_authorization_code(app, code)
    _validate_authorization_token(session, token)
    account = _persist_token(db, app=app, session=session, token=token)
    session.status = "consumed"
    session.consumed_at = _utcnow_naive()
    db.add(session)
    db.flush()
    logger.info(
        "TikTok Shop authorization completed workspace_id=%s account_id=%s",
        session.workspace_id,
        account.id,
    )
    return account, session


def sign_api_request(
    *,
    path: str,
    params: Mapping[str, Any],
    app_secret: str,
    body: str = "",
    multipart: bool = False,
) -> str:
    filtered = {
        str(key): str(value)
        for key, value in params.items()
        if key not in {"sign", "access_token"} and value is not None
    }
    canonical = str(path) + "".join(f"{key}{filtered[key]}" for key in sorted(filtered))
    if body and not multipart:
        canonical += body
    wrapped = f"{app_secret}{canonical}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"),
        wrapped.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _account_credentials(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
) -> tuple[OAuthTikTokShopAccount, OAuthProviderApp, str, str]:
    account = db.get(OAuthTikTokShopAccount, int(account_id))
    if not account or int(account.workspace_id) != int(workspace_id):
        raise APIError("NOT_FOUND", "TikTok Shop authorization not found.", 404)
    app = _get_enabled_app(db, int(account.provider_app_id))
    aad = _token_aad(app, str(account.open_id))
    access_token = decrypt_blob_to_text(account.access_token_cipher, aad_text=aad)
    refresh_token = decrypt_blob_to_text(account.refresh_token_cipher, aad_text=aad)
    return account, app, access_token, refresh_token


async def refresh_account_token(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
    force: bool = False,
) -> OAuthTikTokShopAccount:
    account, app, _access_token, refresh_token = _account_credentials(
        db,
        workspace_id=workspace_id,
        account_id=account_id,
    )
    leeway = max(300, int(getattr(settings, "TT_SHOP_TOKEN_REFRESH_LEEWAY_SECONDS", 86400)))
    if not force and account.expires_at and account.expires_at > _utcnow_naive() + timedelta(seconds=leeway):
        return account
    token = parse_token_response(
        await _get_json(
            settings.TT_SHOP_REFRESH_URL,
            params={
                "app_key": app.client_id,
                "app_secret": _decrypt_app_secret(app),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    )
    if str(token["open_id"]) != str(account.open_id):
        raise APIError("TOKEN_IDENTITY_MISMATCH", "Refreshed token identity does not match.", 502)
    pseudo_session = OAuthTikTokShopAuthzSession(
        workspace_id=int(account.workspace_id),
        provider_app_id=int(account.provider_app_id),
        alias=account.alias,
        authorization_type="creator" if account.user_type == 1 else "seller",
        created_by_user_id=account.created_by_user_id,
        state=str(uuid.uuid4()),
        return_to=None,
        status="consumed",
        expires_at=_utcnow_naive(),
    )
    refreshed = _persist_token(db, app=app, session=pseudo_session, token=token)
    logger.info("TikTok Shop token refreshed account_id=%s", refreshed.id)
    return refreshed


def _api_error(payload: Mapping[str, Any]) -> APIError | None:
    try:
        code = int(payload.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    if code == 0:
        return None
    message = str(payload.get("message") or payload.get("msg") or "TikTok Shop API request failed")[:512]
    return APIError("TIKTOK_SHOP_API_ERROR", message, 502)


async def sync_authorized_shops(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
) -> list[OAuthTikTokShopShop]:
    account = await refresh_account_token(
        db,
        workspace_id=workspace_id,
        account_id=account_id,
        force=False,
    )
    account, app, access_token, _refresh_token = _account_credentials(
        db,
        workspace_id=workspace_id,
        account_id=int(account.id),
    )
    path = "/authorization/202309/shops"
    params: dict[str, Any] = {
        "app_key": app.client_id,
        "timestamp": int(time.time()),
    }
    params["sign"] = sign_api_request(
        path=path,
        params=params,
        app_secret=_decrypt_app_secret(app),
    )
    try:
        payload = await _get_json(
            f"{settings.TT_SHOP_API_BASE.rstrip('/')}{path}",
            params=params,
            headers={"x-tts-access-token": access_token},
        )
        error = _api_error(payload)
        if error:
            raise error
    except APIError as exc:
        account.last_error_code = exc.code
        account.last_error_message = exc.message[:512]
        if exc.code in {"TOKEN_EXCHANGE_FAILED", "TOKEN_IDENTITY_MISMATCH"}:
            account.status = "invalid"
        db.add(account)
        raise

    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("shops") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = []
    now = _utcnow_naive()
    db.execute(
        update(OAuthTikTokShopShop)
        .where(OAuthTikTokShopShop.account_id == int(account.id))
        .values(is_active=False)
    )
    result: list[OAuthTikTokShopShop] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        shop_id = str(item.get("id") or item.get("shop_id") or "").strip()
        cipher = str(item.get("cipher") or item.get("shop_cipher") or "").strip()
        if not shop_id or not cipher:
            continue
        shop = db.scalar(
            select(OAuthTikTokShopShop).where(
                OAuthTikTokShopShop.account_id == int(account.id),
                OAuthTikTokShopShop.shop_id == shop_id,
            )
        )
        if shop is None:
            shop = OAuthTikTokShopShop(
                workspace_id=int(workspace_id),
                account_id=int(account.id),
                shop_id=shop_id,
                shop_cipher=cipher,
                first_seen_at=now,
            )
        shop.shop_code = str(item.get("code") or item.get("shop_code") or "").strip() or None
        shop.shop_cipher = cipher
        shop.shop_name = str(item.get("name") or item.get("shop_name") or "").strip() or None
        shop.region = str(item.get("region") or item.get("region_code") or "").strip() or None
        _apply_shop_timezone_policy(shop, verified_at=now)
        shop.seller_type = str(item.get("seller_type") or "").strip() or None
        shop.status = str(item.get("status") or "active").strip().lower() or "active"
        shop.is_active = True
        shop.raw_json = dict(item)
        shop.last_seen_at = now
        db.add(shop)
        result.append(shop)
    account.last_synced_at = now
    account.last_error_code = None
    account.last_error_message = None
    db.add(account)
    db.flush()
    logger.info("TikTok Shop shops synced account_id=%s count=%s", account.id, len(result))
    return result


def disconnect_account(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
) -> OAuthTikTokShopAccount:
    account = db.get(OAuthTikTokShopAccount, int(account_id))
    if not account or int(account.workspace_id) != int(workspace_id):
        raise APIError("NOT_FOUND", "TikTok Shop authorization not found.", 404)
    account.status = "revoked"
    account.revoked_at = _utcnow_naive()
    db.add(account)
    db.execute(
        update(OAuthTikTokShopShop)
        .where(OAuthTikTokShopShop.account_id == int(account.id))
        .values(is_active=False)
    )
    return account


def callback_redirect_url(session: OAuthTikTokShopAuthzSession, params: Mapping[str, Any]) -> str:
    issuer = str(getattr(settings, "ISSUER", "") or "https://gmv.myupona.com").rstrip("/")
    path = _normalize_return_to(session.return_to, int(session.workspace_id))
    separator = "&" if "?" in path else "?"
    return f"{issuer}{path}{separator}{urlencode({k: v for k, v in params.items() if v is not None})}"
