# app/services/oauth_ttb.py
from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, func, update, text, delete
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, DataError

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_ttb import (
    OAuthProviderApp,
    OAuthProviderAppRedirect,
    OAuthAuthzSession,
    OAuthAccountTTB,
    OAuthTikTokAccount,
    OAuthTikTokAccountAuthzSession,
    CryptoKeyring,
)
from app.data.models.oauth_tiktok_shop import (
    OAuthTikTokShopAccount,
    OAuthTikTokShopAuthzSession,
)
from app.services.crypto import (
    encrypt_text_to_blob,
    decrypt_blob_to_text,
    sha256_fingerprint,
)
from app.services.ttb_meta import enqueue_meta_sync

# ---------- logging ----------
import logging
logger = logging.getLogger("gmv.oauth_ttb")


def _redact(val: str) -> str:
    if not isinstance(val, str):
        return val
    if len(val) > 16:
        return val[:4] + "***" + val[-4:]
    return val


# ---------- helpers ----------
def _ip_to_bytes(ip: str | None) -> bytes | None:
    if not ip:
        return None
    try:
        return ipaddress.ip_address(ip).packed
    except Exception:
        return None


def get_or_bootstrap_key_version(db: Session) -> int:
    """
    返回当前可用的 key_version。
    若库里没有任何激活密钥环，自动创建一条默认记录(key_version=1, key_alias='default', is_active=1)。
    """
    kv = db.scalar(
        select(CryptoKeyring.key_version)
        .where(CryptoKeyring.is_active.is_(True))
        .order_by(CryptoKeyring.key_version.desc())
    )
    if kv:
        return int(kv)

    default_version = 1
    exists_v1 = db.scalar(
        select(func.count()).select_from(CryptoKeyring).where(CryptoKeyring.key_version == default_version)
    )
    if not exists_v1:
        db.add(CryptoKeyring(key_version=default_version, key_alias="default", is_active=True))
        db.flush()
    else:
        row = db.scalar(select(CryptoKeyring).where(CryptoKeyring.key_version == default_version))
        if row and not row.is_active:
            row.is_active = True
            db.add(row)
            db.flush()
    return default_version


def _normalize_alias(alias: str | None) -> str | None:
    if alias is None:
        return None
    s = alias.strip()
    return s if s else None


DEFAULT_TIKTOK_ACCOUNT_SCOPES = [
    "biz.brand.insights",
    "comment.list",
    "user.info.basic",
    "user.info.username",
    "user.info.stats",
    "user.info.profile",
    "user.account.type",
    "user.insights",
    "video.list",
    "video.insights",
    "comment.list.manage",
    "video.publish",
    "video.upload",
    "biz.spark.auth",
    "discovery.search.words",
    "biz.ads.recommend",
    "biz.creator.info",
    "biz.creator.insights",
    "tto.campaign.link",
]


def _normalize_scopes(scopes: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for scope in scopes or DEFAULT_TIKTOK_ACCOUNT_SCOPES:
        item = str(scope or "").strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned or list(DEFAULT_TIKTOK_ACCOUNT_SCOPES)


def _oauth_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_from_now(seconds: Any) -> datetime | None:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return _oauth_utc_now() + timedelta(seconds=value)


def _tiktok_account_aad(app: OAuthProviderApp, open_id: str) -> str:
    return f"tiktok_account|{app.provider}|{app.client_id}|{app.redirect_uri}|{open_id}"


def _tiktok_account_client_key(app: OAuthProviderApp) -> str:
    """Return the App ID paired with the encrypted secret stored on this row.

    TikTok names this value ``client_key`` on the account-holder authorization
    URL, ``client_id`` on token exchange, and ``app_id`` on token-info. It is
    still the same developer App ID in all three places. Never override it with
    a process-wide value because that can pair one app's ID with another app's
    secret and produce ``unauthorized_client``.
    """

    return str(app.client_id).strip()


def ensure_tiktok_account_oauth_tables(db: Session) -> None:
    """Create TikTok account OAuth tables for creator/account-holder tokens."""

    db.execute(
        text(
            """
            create table if not exists oauth_tiktok_account_authz_sessions (
                id bigint unsigned not null auto_increment primary key,
                state varchar(36) not null,
                workspace_id bigint unsigned not null,
                provider_app_id bigint unsigned not null,
                return_to varchar(512) null,
                alias varchar(128) null,
                scopes_json json null,
                created_by_user_id bigint unsigned null,
                ip_address varbinary(16) null,
                user_agent varchar(512) null,
                status enum('pending','consumed','expired','failed') not null default 'pending',
                error_code varchar(64) null,
                error_message varchar(512) null,
                created_at datetime(6) not null default current_timestamp(6),
                expires_at datetime(6) not null,
                consumed_at datetime(6) null,
                unique key uk_tiktok_account_state (state),
                key idx_tiktok_account_wid_status (workspace_id, status),
                key idx_tiktok_account_expires_at (expires_at),
                key idx_tiktok_account_provider_app (provider_app_id)
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci
            """
        )
    )
    db.execute(
        text(
            """
            create table if not exists oauth_tiktok_accounts (
                id bigint unsigned not null auto_increment primary key,
                workspace_id bigint unsigned not null,
                provider_app_id bigint unsigned not null,
                alias varchar(128) null,
                open_id varchar(128) not null,
                creator_id varchar(128) null,
                access_token_cipher varbinary(4096) not null,
                refresh_token_cipher varbinary(4096) null,
                key_version int not null default 1,
                token_fingerprint binary(32) not null,
                scope_json json null,
                raw_json json null,
                token_type varchar(32) null,
                status enum('active','revoked','invalid','expired') not null default 'active',
                expires_at datetime(6) null,
                refresh_expires_at datetime(6) null,
                revoked_at datetime(6) null,
                created_by_user_id bigint unsigned null,
                created_at datetime(6) not null default current_timestamp(6),
                updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
                unique key uk_tiktok_account_open_id (workspace_id, provider_app_id, open_id),
                key idx_tiktok_accounts_wid_status (workspace_id, status),
                key idx_tiktok_accounts_app (provider_app_id),
                key idx_tiktok_accounts_created_at (created_at)
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_unicode_ci
            """
        )
    )


# ---------- provider app mgmt ----------
def upsert_provider_app(
    db: Session,
    *,
    provider: str,
    name: str,
    app_id: str,            # 兼容入参名；内部用 client_id
    app_secret: str | None,
    redirect_uri: str,
    is_enabled: bool,
    actor_user_id: int | None,
    service_id: str | None = None,
) -> OAuthProviderApp:
    # 只允许 tiktok_business
    if provider not in {"tiktok_business", "tiktok_shop"}:
        raise APIError("UNSUPPORTED_PROVIDER", "Unsupported OAuth provider.", 400)
    normalized_service_id = str(service_id or "").strip() or None
    if provider == "tiktok_shop" and not normalized_service_id:
        raise APIError("SERVICE_ID_REQUIRED", "service_id is required for TikTok Shop.", 400)

    # 注意：表结构字段是 client_id/client_secret_cipher
    row = db.scalar(
        select(OAuthProviderApp).where(
            OAuthProviderApp.provider == provider,
            OAuthProviderApp.client_id == app_id,
        )
    )

    key_version = get_or_bootstrap_key_version(db)
    aad = f"{provider}|{app_id}|{redirect_uri}"

    if row is None:
        if not app_secret:
            raise APIError("APP_SECRET_REQUIRED", "app_secret is required for creation.", 400)
        row = OAuthProviderApp(
            provider=provider,
            name=name,
            client_id=app_id,
            service_id=normalized_service_id,
            client_secret_cipher=encrypt_text_to_blob(app_secret, key_version=key_version, aad_text=aad),
            client_secret_key_version=key_version,
            redirect_uri=redirect_uri,
            is_enabled=is_enabled,
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
        )
        db.add(row)
        db.flush()
    else:
        if (
            provider == "tiktok_shop"
            and row.service_id
            and normalized_service_id != row.service_id
        ):
            linked_shop_accounts = int(
                db.scalar(
                    select(func.count())
                    .select_from(OAuthTikTokShopAccount)
                    .where(OAuthTikTokShopAccount.provider_app_id == int(row.id))
                )
                or 0
            )
            if linked_shop_accounts:
                raise APIError(
                    "APP_IN_USE",
                    "service_id cannot change while TikTok Shop authorizations exist.",
                    409,
                )
        secret_to_store = app_secret
        if row.redirect_uri != redirect_uri and not secret_to_store:
            old_aad = f"{provider}|{app_id}|{row.redirect_uri}"
            secret_to_store = decrypt_blob_to_text(row.client_secret_cipher, aad_text=old_aad)
        row.name = name
        row.redirect_uri = redirect_uri
        row.service_id = normalized_service_id
        row.is_enabled = is_enabled
        row.updated_by_user_id = actor_user_id
        if secret_to_store:
            row.client_secret_cipher = encrypt_text_to_blob(secret_to_store, key_version=key_version, aad_text=aad)
            row.client_secret_key_version = key_version
        db.add(row)
        db.flush()

    # 同步回调白名单（幂等）
    exists = db.scalar(
        select(func.count()).select_from(OAuthProviderAppRedirect).where(
            OAuthProviderAppRedirect.provider_app_id == row.id,
            OAuthProviderAppRedirect.redirect_uri == redirect_uri,
        )
    )
    if not exists:
        db.add(OAuthProviderAppRedirect(provider_app_id=int(row.id), redirect_uri=redirect_uri))

    return row


def list_provider_apps(db: Session, *, provider: str | None = None) -> list[dict]:
    q = select(OAuthProviderApp)
    if provider:
        q = q.where(OAuthProviderApp.provider == provider)
    q = q.order_by(OAuthProviderApp.provider.asc(), OAuthProviderApp.id.asc())
    items: list[dict] = []
    for x in db.execute(q).scalars().all():
        items.append(
            {
                "id": int(x.id),
                "provider": x.provider,
                "name": x.name,
                # 同时返回两套字段名（保守做法，便于前端对齐；如确定无历史依赖可后续统一）
                "client_id": x.client_id,
                "service_id": x.service_id,
                "client_secret_key_version": int(x.client_secret_key_version),
                "app_id": x.client_id,
                "app_secret_key_version": int(x.client_secret_key_version),
                "redirect_uri": x.redirect_uri,
                "is_enabled": bool(x.is_enabled),
                "updated_at": x.updated_at.isoformat() if x.updated_at else None,
            }
        )
    return items


def delete_provider_app(db: Session, *, provider_app_id: int) -> dict[str, int]:
    app = db.get(OAuthProviderApp, int(provider_app_id))
    if not app:
        raise APIError("APP_NOT_FOUND", "Provider app not found.", 404)

    linked_ad_accounts = int(
        db.scalar(
            select(func.count()).select_from(OAuthAccountTTB).where(OAuthAccountTTB.provider_app_id == int(app.id))
        )
        or 0
    )
    linked_tiktok_accounts = int(
        db.scalar(
            select(func.count())
            .select_from(OAuthTikTokAccount)
            .where(OAuthTikTokAccount.provider_app_id == int(app.id))
        )
        or 0
    )
    linked_shop_accounts = int(
        db.scalar(
            select(func.count())
            .select_from(OAuthTikTokShopAccount)
            .where(OAuthTikTokShopAccount.provider_app_id == int(app.id))
        )
        or 0
    )
    if linked_ad_accounts or linked_tiktok_accounts or linked_shop_accounts:
        raise APIError(
            "APP_IN_USE",
            "Provider app has linked OAuth accounts. Revoke or migrate those accounts before deleting it.",
            409,
        )

    redirects_deleted = db.execute(
        delete(OAuthProviderAppRedirect).where(OAuthProviderAppRedirect.provider_app_id == int(app.id))
    ).rowcount or 0
    auth_sessions_deleted = db.execute(
        delete(OAuthAuthzSession).where(OAuthAuthzSession.provider_app_id == int(app.id))
    ).rowcount or 0
    tiktok_sessions_deleted = db.execute(
        delete(OAuthTikTokAccountAuthzSession).where(
            OAuthTikTokAccountAuthzSession.provider_app_id == int(app.id)
        )
    ).rowcount or 0
    shop_sessions_deleted = db.execute(
        delete(OAuthTikTokShopAuthzSession).where(
            OAuthTikTokShopAuthzSession.provider_app_id == int(app.id)
        )
    ).rowcount or 0
    db.delete(app)
    db.flush()
    return {
        "deleted_apps": 1,
        "deleted_redirects": int(redirects_deleted),
        "deleted_auth_sessions": int(auth_sessions_deleted),
        "deleted_tiktok_account_sessions": int(tiktok_sessions_deleted),
        "deleted_tiktok_shop_sessions": int(shop_sessions_deleted),
    }


# ---------- authz session & auth url ----------
def create_authz_session(
    db: Session,
    *,
    workspace_id: int,
    provider_app_id: int,
    created_by_user_id: int | None,
    client_ip: str | None,
    user_agent: str | None,
    return_to: str | None,
    alias: str | None,
) -> tuple[OAuthAuthzSession, str]:
    app = db.get(OAuthProviderApp, int(provider_app_id))
    if not app or app.provider != "tiktok_business" or not app.is_enabled:
        raise APIError("APP_NOT_FOUND", "Provider app not found or disabled.", 404)

    # 生成 state & 过期时间
    state = str(uuid.uuid4())
    ttl = int(getattr(settings, "OAUTH_SESSION_TTL_SECONDS", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    sess = OAuthAuthzSession(
        state=state,
        workspace_id=int(workspace_id),
        provider_app_id=int(provider_app_id),
        return_to=return_to,
        created_by_user_id=created_by_user_id,
        ip_address=_ip_to_bytes(client_ip),
        user_agent=(user_agent or "")[:512],
        status="pending",
        expires_at=expires_at,
        alias=_normalize_alias(alias),
    )
    db.add(sess)
    db.flush()

    # 官方 Portal 授权入口：/portal/auth
    from urllib.parse import urlencode
    base = settings.TT_BIZ_PORTAL_AUTH_URL.rstrip("/")  # 例：https://business-api.tiktok.com/portal
    qs = {
        "app_id": app.client_id,
        "redirect_uri": app.redirect_uri,
        "state": state,
    }
    auth_url = f"{base}/auth?{urlencode(qs)}"

    logger.info("TTB auth url generated state=%s url=%s", state, auth_url)
    return sess, auth_url


def create_tiktok_account_authz_session(
    db: Session,
    *,
    workspace_id: int,
    provider_app_id: int,
    created_by_user_id: int | None,
    client_ip: str | None,
    user_agent: str | None,
    return_to: str | None,
    alias: str | None,
    scopes: list[str] | None = None,
) -> tuple[OAuthTikTokAccountAuthzSession, str]:
    """Build the TikTok account holder OAuth URL for creator/account APIs."""

    ensure_tiktok_account_oauth_tables(db)
    app = db.get(OAuthProviderApp, int(provider_app_id))
    if not app or app.provider != "tiktok_business" or not app.is_enabled:
        raise APIError("APP_NOT_FOUND", "Provider app not found or disabled.", 404)

    state = str(uuid.uuid4())
    ttl = int(getattr(settings, "OAUTH_SESSION_TTL_SECONDS", 3600))
    expires_at = _oauth_utc_now() + timedelta(seconds=ttl)
    normalized_scopes = _normalize_scopes(scopes)

    sess = OAuthTikTokAccountAuthzSession(
        state=state,
        workspace_id=int(workspace_id),
        provider_app_id=int(provider_app_id),
        return_to=return_to,
        alias=_normalize_alias(alias),
        scopes_json={"items": normalized_scopes},
        created_by_user_id=created_by_user_id,
        ip_address=_ip_to_bytes(client_ip),
        user_agent=(user_agent or "")[:512],
        status="pending",
        expires_at=expires_at,
    )
    db.add(sess)
    db.flush()

    from urllib.parse import urlencode

    account_client_key = _tiktok_account_client_key(app)
    qs = {
        "client_key": account_client_key,
        "scope": ",".join(normalized_scopes),
        "response_type": "code",
        "redirect_uri": app.redirect_uri,
    }
    auth_url = f"https://www.tiktok.com/v2/auth/authorize?{urlencode(qs)}"
    logger.info("TikTok account auth url generated state=%s", state)
    return sess, auth_url


# ---------- low-level HTTP ----------
async def _http_post_json(url: str, payload: dict, *, timeout: float, headers: dict | None = None) -> dict:
    try:
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
            r = await client.post(url, json=payload, headers=h)
            try:
                data = r.json()
            except Exception:
                data = {}
            safe_data = {}
            if isinstance(data, dict):
                safe_data = {
                    k: (_redact(v) if k in {"access_token", "refresh_token", "client_secret", "secret"} else v)
                    for k, v in data.items()
                }
            logger.debug("TTB POST %s status=%s json=%s text=%s",
                         url, r.status_code, safe_data, _redact(r.text or ""))
            return {"status_code": r.status_code, "json": data, "text": r.text}
    except httpx.RequestError as e:
        raise APIError("HTTP_REQUEST_FAILED", f"request error: {e}", 502)


# ---------- token exchange (STRICT v1.3 only) ----------
def _decrypt_app_secret(app: OAuthProviderApp) -> str:
    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    return decrypt_blob_to_text(app.client_secret_cipher, aad_text=aad)


def _parse_token_response_v13(payload: dict) -> tuple[str, Any]:
    """
    v1.3 结构：
    官方 /oauth2/access_token/ 返回 data.access_token；历史 /oauth/token/ 返回过顶层 access_token。
    """
    if not isinstance(payload, dict):
        raise APIError("TOKEN_EXCHANGE_FAILED", "invalid response json", 502)
    if int(payload.get("code", -1)) != 0:
        msg = payload.get("message") or "token exchange error"
        raise APIError("TOKEN_EXCHANGE_FAILED", str(msg)[:512], 502)
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    token = source.get("access_token") or payload.get("access_token")
    if not token:
        raise APIError("TOKEN_EXCHANGE_FAILED", "no access_token", 502)
    return str(token), source.get("scope", payload.get("scope"))


def _parse_tiktok_account_token_response(payload: dict) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise APIError("TOKEN_EXCHANGE_FAILED", "invalid response json", 502)
    if int(payload.get("code", -1)) != 0:
        msg = payload.get("message") or "token exchange error"
        raise APIError("TOKEN_EXCHANGE_FAILED", str(msg)[:512], 502)
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    access_token = source.get("access_token")
    open_id = source.get("open_id")
    if not access_token:
        raise APIError("TOKEN_EXCHANGE_FAILED", "no access_token", 502)
    if not open_id:
        raise APIError("TOKEN_EXCHANGE_FAILED", "no open_id", 502)
    return {
        "access_token": str(access_token),
        "refresh_token": source.get("refresh_token"),
        "open_id": str(open_id),
        "scope": source.get("scope"),
        "token_type": source.get("token_type"),
        "expires_in": source.get("expires_in"),
        "refresh_token_expires_in": source.get("refresh_token_expires_in"),
        "raw": source,
    }


async def _fetch_tiktok_account_token_info(
    *,
    app: OAuthProviderApp,
    access_token: str,
    timeout: float,
) -> dict[str, Any]:
    api_base = (getattr(settings, "TT_BIZ_API_BASE", "https://business-api.tiktok.com").rstrip("/"))
    url = f"{api_base}/open_api/v1.3/tt_user/token_info/get/"
    payload = {"app_id": _tiktok_account_client_key(app), "access_token": access_token}
    http = await _http_post_json(url, payload, timeout=timeout)
    js = http.get("json") or {}
    if http.get("status_code", 500) >= 400 or not isinstance(js, dict) or int(js.get("code", -1)) != 0:
        return {"error": js.get("message") if isinstance(js, dict) else "token_info_failed", "payload": js}
    data = js.get("data")
    return data if isinstance(data, dict) else {}


async def handle_callback_and_bind_token(
    db: Session, *, code: str, state: str
) -> tuple[OAuthAccountTTB, OAuthAuthzSession]:
    sess = db.scalar(select(OAuthAuthzSession).where(OAuthAuthzSession.state == state))
    if not sess or sess.status != "pending":
        raise APIError("INVALID_STATE", "Invalid or consumed state.", 400)

    # 过期检查（统一 UTC 无时区）
    now_utc = datetime.now(timezone.utc)
    expires_at_raw = getattr(sess, "expires_at", None)
    if isinstance(expires_at_raw, datetime):
        expires_at = (
            expires_at_raw.replace(tzinfo=timezone.utc)
            if expires_at_raw.tzinfo is None
            else expires_at_raw.astimezone(timezone.utc)
        )
        if now_utc > expires_at:
            sess.status = "expired"
            db.add(sess)
            raise APIError("SESSION_EXPIRED", "Auth session expired.", 400)

    app = db.get(OAuthProviderApp, int(sess.provider_app_id))
    if not app or not app.is_enabled:
        raise APIError("APP_NOT_FOUND", "Provider app not found or disabled.", 404)

    client_secret = _decrypt_app_secret(app)

    # 统一使用 v1.3 基底，避免再被错误的 env 影响
    api_base = (getattr(settings, "TT_BIZ_API_BASE", "https://business-api.tiktok.com").rstrip("/"))
    v13 = f"{api_base}/open_api/v1.3"
    url_oauth_token = f"{v13}/oauth2/access_token/"

    timeout = float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15))

    # ---- 官方 v1.3 /oauth2/access_token/ ----
    payload_token = {
        "app_id": app.client_id,
        "secret": client_secret,
        "auth_code": code,
    }
    http = await _http_post_json(url_oauth_token, payload_token, timeout=timeout)

    js = http.get("json") or {}
    if http["status_code"] >= 400:
        msg = (isinstance(js, dict) and js.get("message")) or f"http {http['status_code']}"
        sess.status = "failed"
        sess.error_code = str(js.get("code") if isinstance(js, dict) else http["status_code"])
        sess.error_message = str(msg)[:512]
        db.add(sess)
        raise APIError("TOKEN_EXCHANGE_FAILED", sess.error_message, 502)

    token, scope = _parse_token_response_v13(js)
    logger.info("TTB token exchange ok state=%s has_token=%s", state, bool(token))

    # 持久化账户（携带 alias）
    key_version = int(app.client_secret_key_version)
    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    account = OAuthAccountTTB(
        workspace_id=int(sess.workspace_id),
        provider_app_id=int(app.id),
        alias=_normalize_alias(getattr(sess, "alias", None)),
        access_token_cipher=encrypt_text_to_blob(token, key_version=key_version, aad_text=aad),
        key_version=key_version,
        token_fingerprint=sha256_fingerprint(token),
        scope_json=scope if isinstance(scope, dict) else ({"value": scope} if scope is not None else None),
        status="active",
        created_by_user_id=getattr(sess, "created_by_user_id", None),
    )
    db.add(account)

    # 标记会话 consumed（UTC）
    sess.status = "consumed"
    sess.consumed_at = now_utc
    db.add(sess)
    db.flush()

    try:
        result = enqueue_meta_sync(workspace_id=int(sess.workspace_id), auth_id=int(account.id))
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to enqueue initial meta sync",
            extra={
                "provider": "tiktok-business",
                "workspace_id": int(sess.workspace_id),
                "auth_id": int(account.id),
                "idempotency_key": None,
                "task_name": None,
            },
        )
    else:
        logger.info(
            "enqueued initial meta sync",
            extra={
                "provider": "tiktok-business",
                "workspace_id": int(sess.workspace_id),
                "auth_id": int(account.id),
                "idempotency_key": result.idempotency_key,
                "task_name": result.task_name,
            },
        )

    return account, sess


async def handle_tiktok_account_callback_and_bind_token(
    db: Session, *, code: str, state: str
) -> tuple[OAuthTikTokAccount, OAuthTikTokAccountAuthzSession]:
    """Handle TikTok account holder OAuth callback and persist creator token."""

    ensure_tiktok_account_oauth_tables(db)
    sess = db.scalar(select(OAuthTikTokAccountAuthzSession).where(OAuthTikTokAccountAuthzSession.state == state))
    if not sess or sess.status != "pending":
        raise APIError("INVALID_STATE", "Invalid or consumed TikTok account state.", 400)

    now_utc = _oauth_utc_now()
    expires_at_raw = getattr(sess, "expires_at", None)
    if isinstance(expires_at_raw, datetime):
        expires_at = (
            expires_at_raw.replace(tzinfo=timezone.utc)
            if expires_at_raw.tzinfo is None
            else expires_at_raw.astimezone(timezone.utc)
        )
        if now_utc > expires_at:
            sess.status = "expired"
            db.add(sess)
            raise APIError("SESSION_EXPIRED", "TikTok account auth session expired.", 400)

    app = db.get(OAuthProviderApp, int(sess.provider_app_id))
    if not app or not app.is_enabled:
        raise APIError("APP_NOT_FOUND", "Provider app not found or disabled.", 404)

    client_secret = _decrypt_app_secret(app)
    api_base = (getattr(settings, "TT_BIZ_API_BASE", "https://business-api.tiktok.com").rstrip("/"))
    url_oauth_token = f"{api_base}/open_api/v1.3/tt_user/oauth2/token/"
    timeout = float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15))
    payload_token = {
        "client_id": str(app.client_id),
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "auth_code": code,
        "redirect_uri": app.redirect_uri,
    }
    http = await _http_post_json(url_oauth_token, payload_token, timeout=timeout)
    js = http.get("json") or {}
    if http["status_code"] >= 400:
        msg = (isinstance(js, dict) and js.get("message")) or f"http {http['status_code']}"
        sess.status = "failed"
        sess.error_code = str(js.get("code") if isinstance(js, dict) else http["status_code"])
        sess.error_message = str(msg)[:512]
        db.add(sess)
        raise APIError("TOKEN_EXCHANGE_FAILED", sess.error_message, 502)

    token_data = _parse_tiktok_account_token_response(js)
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    open_id = token_data["open_id"]
    token_info = await _fetch_tiktok_account_token_info(app=app, access_token=access_token, timeout=timeout)
    creator_id = token_info.get("creator_id") or open_id

    key_version = int(app.client_secret_key_version)
    aad = _tiktok_account_aad(app, open_id)
    scope_value = token_info.get("scope") or token_data.get("scope")
    scope_json = {"value": scope_value} if scope_value is not None else None
    raw_json = {
        "token_response": {
            k: ("***" if k in {"access_token", "refresh_token"} else v)
            for k, v in token_data.get("raw", {}).items()
        },
        "token_info": token_info,
    }

    account = db.scalar(
        select(OAuthTikTokAccount).where(
            OAuthTikTokAccount.workspace_id == int(sess.workspace_id),
            OAuthTikTokAccount.provider_app_id == int(app.id),
            OAuthTikTokAccount.open_id == str(open_id),
        )
    )
    if account is None:
        account = OAuthTikTokAccount(
            workspace_id=int(sess.workspace_id),
            provider_app_id=int(app.id),
            open_id=str(open_id),
            created_by_user_id=getattr(sess, "created_by_user_id", None),
        )
    account.alias = _normalize_alias(getattr(sess, "alias", None)) or account.alias
    account.creator_id = str(creator_id) if creator_id else None
    account.access_token_cipher = encrypt_text_to_blob(access_token, key_version=key_version, aad_text=aad)
    account.refresh_token_cipher = (
        encrypt_text_to_blob(str(refresh_token), key_version=key_version, aad_text=aad)
        if refresh_token
        else None
    )
    account.key_version = key_version
    account.token_fingerprint = sha256_fingerprint(access_token)
    account.scope_json = scope_json
    account.raw_json = raw_json
    account.token_type = token_data.get("token_type")
    account.status = "active"
    account.expires_at = _expires_from_now(token_data.get("expires_in"))
    account.refresh_expires_at = _expires_from_now(token_data.get("refresh_token_expires_in"))
    account.revoked_at = None
    db.add(account)

    sess.status = "consumed"
    sess.consumed_at = now_utc
    db.add(sess)
    db.flush()
    logger.info("TikTok account token exchange ok state=%s open_id=%s", state, _redact(open_id))
    return account, sess


def get_tiktok_account_token_plain(db: Session, account_id: int) -> tuple[str, OAuthTikTokAccount, OAuthProviderApp]:
    ensure_tiktok_account_oauth_tables(db)
    acc = db.get(OAuthTikTokAccount, int(account_id))
    if not acc:
        raise APIError("NOT_FOUND", "tiktok account oauth record not found", 404)
    app = db.get(OAuthProviderApp, int(acc.provider_app_id))
    if not app:
        raise APIError("NOT_FOUND", "provider app not found", 404)
    aad = _tiktok_account_aad(app, str(acc.open_id))
    token = decrypt_blob_to_text(acc.access_token_cipher, aad_text=aad)
    return token, acc, app


async def get_fresh_tiktok_account_token_plain(
    db: Session,
    account_id: int,
    *,
    refresh_before_seconds: int = 600,
) -> tuple[str, OAuthTikTokAccount, OAuthProviderApp]:
    """Return an account token, refreshing TikTok's one-day token when needed."""

    token, acc, app = get_tiktok_account_token_plain(db, int(account_id))
    now_utc = _oauth_utc_now()
    expires_at = acc.expires_at
    if isinstance(expires_at, datetime):
        expires_at = (
            expires_at.replace(tzinfo=timezone.utc)
            if expires_at.tzinfo is None
            else expires_at.astimezone(timezone.utc)
        )
    if expires_at is None or expires_at > now_utc + timedelta(seconds=max(0, int(refresh_before_seconds))):
        return token, acc, app
    if not acc.refresh_token_cipher:
        acc.status = "expired"
        db.add(acc)
        raise APIError("TOKEN_EXPIRED", "TikTok account token expired; please authorize the account again.", 401)

    aad = _tiktok_account_aad(app, str(acc.open_id))
    refresh_token = decrypt_blob_to_text(acc.refresh_token_cipher, aad_text=aad)
    client_secret = _decrypt_app_secret(app)
    api_base = getattr(settings, "TT_BIZ_API_BASE", "https://business-api.tiktok.com").rstrip("/")
    url = f"{api_base}/open_api/v1.3/tt_user/oauth2/refresh_token/"
    http = await _http_post_json(
        url,
        {
            "client_id": str(app.client_id),
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15)),
    )
    js = http.get("json") or {}
    if http.get("status_code", 500) >= 400:
        raise APIError("TOKEN_REFRESH_FAILED", str(js.get("message") or "TikTok token refresh failed"), 502)
    token_data = _parse_tiktok_account_token_response(js)
    refreshed_open_id = str(token_data.get("open_id") or acc.open_id)
    if refreshed_open_id != str(acc.open_id):
        raise APIError("TOKEN_REFRESH_FAILED", "TikTok refresh returned a different account.", 502)

    new_access_token = str(token_data["access_token"])
    new_refresh_token = str(token_data.get("refresh_token") or refresh_token)
    acc.access_token_cipher = encrypt_text_to_blob(
        new_access_token,
        key_version=int(acc.key_version),
        aad_text=aad,
    )
    acc.refresh_token_cipher = encrypt_text_to_blob(
        new_refresh_token,
        key_version=int(acc.key_version),
        aad_text=aad,
    )
    acc.token_fingerprint = sha256_fingerprint(new_access_token)
    if token_data.get("scope") is not None:
        acc.scope_json = {"value": token_data.get("scope")}
    acc.token_type = token_data.get("token_type") or acc.token_type
    acc.expires_at = _expires_from_now(token_data.get("expires_in"))
    acc.refresh_expires_at = _expires_from_now(token_data.get("refresh_token_expires_in"))
    acc.status = "active"
    raw_json = dict(acc.raw_json) if isinstance(acc.raw_json, dict) else {}
    raw_json["last_refresh"] = {
        "at": now_utc.isoformat(),
        "scope": token_data.get("scope"),
    }
    acc.raw_json = raw_json
    db.add(acc)
    db.flush()
    return new_access_token, acc, app


async def revoke_tiktok_account_oauth(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
    remote: bool = True,
) -> dict[str, int]:
    ensure_tiktok_account_oauth_tables(db)
    acc = db.get(OAuthTikTokAccount, int(account_id))
    if not acc or acc.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "tiktok account oauth record not found", 404)
    if remote and acc.status == "active":
        token, acc, app = get_tiktok_account_token_plain(db, int(account_id))
        secret = _decrypt_app_secret(app)
        api_base = (getattr(settings, "TT_BIZ_API_BASE", "https://business-api.tiktok.com").rstrip("/"))
        url = f"{api_base}/open_api/v1.3/tt_user/oauth2/revoke/"
        payload = {"client_id": _tiktok_account_client_key(app), "client_secret": secret, "access_token": token}
        http = await _http_post_json(url, payload, timeout=float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15)))
        js = http.get("json") or {}
        if http.get("status_code", 500) >= 400 or not isinstance(js, dict) or int(js.get("code", -1)) != 0:
            raise APIError("REVOKE_FAILED", str(js.get("message") if isinstance(js, dict) else "revoke failed"), 502)
    acc.status = "revoked"
    acc.revoked_at = _oauth_utc_now()
    db.add(acc)
    return {"removed_accounts": 1}


def get_access_token_plain(db: Session, account_id: int) -> tuple[str, OAuthProviderApp]:
    acc = db.get(OAuthAccountTTB, int(account_id))
    if not acc:
        raise APIError("NOT_FOUND", "oauth account not found", 404)
    app = db.get(OAuthProviderApp, int(acc.provider_app_id))
    if not app:
        raise APIError("NOT_FOUND", "provider app not found", 404)

    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    token = decrypt_blob_to_text(acc.access_token_cipher, aad_text=aad)

    return token, app


# ---------- revoke (STRICT v1.3) ----------
async def revoke_remote_token(*, access_token: str, app: OAuthProviderApp, timeout: float) -> None:
    """
    TikTok Business 撤销长期令牌（严格 v1.3）：
      POST /open_api/v1.3/oauth2/revoke_token/
      Header: Access-Token: <要撤销的那个 access_token>
      Body:   { "app_id": "...", "secret": "...", "access_token": "..." }
    """
    secret = _decrypt_app_secret(app)
    api_base = (getattr(settings, "TT_BIZ_API_BASE", "https://business-api.tiktok.com").rstrip("/"))
    v13 = f"{api_base}/open_api/v1.3"
    url = f"{v13}/oauth2/revoke_token/"

    headers_json = {
        "Content-Type": "application/json",
        "Access-Token": access_token,
    }

    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        payload = {
            "app_id": app.client_id,
            "secret": secret,
            "access_token": access_token,
        }
        r = await client.post(url, json=payload, headers=headers_json)
        try:
            js = r.json()
        except Exception:
            js = {}
        logger.debug(
            "TTB REVOKE v1.3 status=%s json=%s",
            getattr(r, "status_code", "?"),
            {k: ("***" if k in {"access_token", "secret"} else v) for k, v in (js or {}).items()},
        )
        if (r.status_code < 400) and isinstance(js, dict) and int(js.get("code", -1)) == 0:
            return

    msg = (isinstance(js, dict) and js.get("message")) or "revoke failed"
    raise APIError("REVOKE_FAILED", msg, 502)


def _mark_local_revoked(db: Session, *, workspace_id: int, auth_id: int) -> None:
    """
    本地软撤销：将账户标记为 revoked，并清空别名 alias。
    """
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc or acc.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "oauth account not found", 404)

    db.execute(
        update(OAuthAccountTTB)
        .where(
            OAuthAccountTTB.id == int(auth_id),
            OAuthAccountTTB.workspace_id == int(workspace_id),
        )
        .values(
            status="revoked",
            revoked_at=datetime.now(timezone.utc),
            alias=None,
        )
    )


async def revoke_oauth_account(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    remote: bool = True,
) -> dict:
    """
    撤销长期令牌（remote=True 调 TikTok，随后本地软撤销；remote=False 仅本地软撤销）
    返回: {"removed_advertisers": 0}
    """
    if remote:
        token, app = get_access_token_plain(db, int(auth_id))
        await revoke_remote_token(
            access_token=token,
            app=app,
            timeout=float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15)),
        )
    _mark_local_revoked(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    return {"removed_advertisers": 0}


# ---------- extra: 别名更新 ----------
def update_oauth_account_alias(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    alias: str | None,
    actor_user_id: int | None,
) -> OAuthAccountTTB:
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc or acc.workspace_id != int(workspace_id):
        raise APIError("NOT_FOUND", "oauth account not found", 404)

    acc.alias = _normalize_alias(alias)
    try:
        db.add(acc)
        db.flush()  # 交由请求边界统一提交
    except IntegrityError:
        raise APIError("ALIAS_CONFLICT", "Alias already exists in this workspace.", 409)
    except DataError:
        raise APIError("ALIAS_INVALID", "Alias is invalid or too long.", 400)

    return acc


# === 公共取凭据 ===
def get_credentials_for_auth_id(db: Session, auth_id: int) -> tuple[str, str, str]:
    """
    返回 (app_id, app_secret_plain, redirect_uri)
    - 严格密文解密（使用 provider|client_id|redirect_uri 作为 AAD）
    """
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc:
        raise APIError("NOT_FOUND", "oauth account not found", 404)
    app = db.get(OAuthProviderApp, int(acc.provider_app_id))
    if not app:
        raise APIError("NOT_FOUND", "provider app not found", 404)
    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    secret = decrypt_blob_to_text(app.client_secret_cipher, aad_text=aad)
    return (str(app.client_id), str(secret), str(app.redirect_uri))
