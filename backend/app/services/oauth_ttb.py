# app/services/oauth_ttb.py
from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, DataError

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_ttb import (
    OAuthProviderApp,
    OAuthProviderAppRedirect,
    OAuthAuthzSession,
    OAuthAccountTTB,
    CryptoKeyring,
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
) -> OAuthProviderApp:
    # 只允许 tiktok_business
    if provider != "tiktok_business":
        raise APIError("UNSUPPORTED_PROVIDER", "Only tiktok_business is supported.", 400)

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
        row.name = name
        row.redirect_uri = redirect_uri
        row.is_enabled = is_enabled
        row.updated_by_user_id = actor_user_id
        if app_secret:
            row.client_secret_cipher = encrypt_text_to_blob(app_secret, key_version=key_version, aad_text=aad)
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


def list_provider_apps(db: Session, *, provider: str) -> list[dict]:
    q = (
        select(OAuthProviderApp)
        .where(OAuthProviderApp.provider == provider)
        .order_by(OAuthProviderApp.id.asc())
    )
    items: list[dict] = []
    for x in db.execute(q).scalars().all():
        items.append(
            {
                "id": int(x.id),
                "provider": x.provider,
                "name": x.name,
                # 同时返回两套字段名（保守做法，便于前端对齐；如确定无历史依赖可后续统一）
                "client_id": x.client_id,
                "client_secret_key_version": int(x.client_secret_key_version),
                "app_id": x.client_id,
                "app_secret_key_version": int(x.client_secret_key_version),
                "redirect_uri": x.redirect_uri,
                "is_enabled": bool(x.is_enabled),
                "updated_at": x.updated_at.isoformat() if x.updated_at else None,
            }
        )
    return items


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
    严格 v1.3 结构：
    顶层: {"code":0,"message":"OK","access_token":"...","scope":[...], ...}
    """
    if not isinstance(payload, dict):
        raise APIError("TOKEN_EXCHANGE_FAILED", "invalid response json", 502)
    if int(payload.get("code", -1)) != 0:
        msg = payload.get("message") or "token exchange error"
        raise APIError("TOKEN_EXCHANGE_FAILED", str(msg)[:512], 502)
    token = payload.get("access_token")
    if not token:
        raise APIError("TOKEN_EXCHANGE_FAILED", "no access_token", 502)
    return str(token), payload.get("scope")


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
    url_oauth_token = f"{v13}/oauth/token/"

    timeout = float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15))

    # ---- 严格 v1.3 /oauth/token/ ----
    payload_token = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app.client_id,
        "client_secret": client_secret,
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

