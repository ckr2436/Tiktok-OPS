# app/services/oauth_ttb.py
from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, DataError

from app.celery_app import celery_app
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


def _enqueue_bootstrap_after_binding(*, workspace_id: int, auth_id: int, state: str | None = None) -> None:
    """
    在自动绑定完成后，后台直接触发全量引导同步（含商品），减少前端确认步骤。

    - 使用 OAuth state 作为幂等提示，避免重复回调时重复入队
    - countdown 留出事务提交时间，避免任务抢先读取不到新记录
    """
    try:
        idem = (state or f"binding-{workspace_id}-{auth_id}")[:255]
        celery_app.send_task(
            "tenant.ttb.sync.bootstrap_orchestrator",
            kwargs={
                "workspace_id": workspace_id,
                "auth_id": auth_id,
                "idempotency_key": idem,
            },
            queue="gmv.tasks.default",
            countdown=3,
        )
        logger.info(
            "TTB binding bootstrap queued workspace=%s auth_id=%s idem=%s", workspace_id, auth_id, idem
        )
    except Exception:
        logger.exception("failed to enqueue bootstrap sync for binding auth_id=%s", auth_id)


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
    expires_at = datetime.utcnow() + timedelta(seconds=ttl)

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

    # ★ 官方 Portal 授权入口：/portal/auth
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


# ---------- token exchange (v1.3 /oauth/token，失败兜底 /oauth2/access_token) ----------
def _decrypt_app_secret(app: OAuthProviderApp) -> str:
    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    return decrypt_blob_to_text(app.client_secret_cipher, aad_text=aad)


def _parse_token_response(payload: dict) -> tuple[str | None, Any]:
    """
    兼容两种返回结构：
    - 新版 /oauth/token 顶层: {"code":0,"access_token":"...","scope":[...],"advertiser_ids":[...]}
    - 旧版 /oauth2/access_token data: {"code":0,"data":{"access_token":"...","scope":[...],"advertiser_ids":[...]}}
    """
    if not isinstance(payload, dict):
        return None, None
    # 优先新版
    token = payload.get("access_token")
    scope = payload.get("scope")
    if token:
        return str(token), scope
    # 兼容旧版
    data = payload.get("data") or {}
    token = data.get("access_token")
    scope = data.get("scope")
    return (str(token) if token else None), scope


async def handle_callback_and_bind_token(
    db: Session, *, code: str, state: str
) -> tuple[OAuthAccountTTB, OAuthAuthzSession]:
    sess = db.scalar(select(OAuthAuthzSession).where(OAuthAuthzSession.state == state))
    if not sess or sess.status != "pending":
        raise APIError("INVALID_STATE", "Invalid or consumed state.", 400)

    # 过期检查（统一 UTC 无时区）
    if getattr(sess, "expires_at", None) and datetime.utcnow() > sess.expires_at:  # type: ignore[operator]
        sess.status = "expired"
        db.add(sess)
        raise APIError("SESSION_EXPIRED", "Auth session expired.", 400)

    app = db.get(OAuthProviderApp, int(sess.provider_app_id))
    if not app or not app.is_enabled:
        raise APIError("APP_NOT_FOUND", "Provider app not found or disabled.", 404)

    client_secret = _decrypt_app_secret(app)

    base = settings.TT_BIZ_TOKEN_URL.rstrip("/")  # 例：https://business-api.tiktok.com/open_api/v1.3
    timeout = float(getattr(settings, "HTTP_CLIENT_TIMEOUT_SECONDS", 15))

    # ---- 首选 v1.3 新接口 /oauth/token/ ----
    payload_token = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app.client_id,
        "client_secret": client_secret,
    }
    http = await _http_post_json(f"{base}/oauth/token/", payload_token, timeout=timeout)

    js = http.get("json") or {}
    if http["status_code"] >= 400 or int(js.get("code", -1)) != 0:
        # ---- 兜底到 v1.3 旧结构 /oauth2/access_token/（参数名是 auth_code）----
        payload_access_token = {
            "app_id": app.client_id,
            "secret": client_secret,
            "auth_code": code,
        }
        http2 = await _http_post_json(f"{base}/oauth2/access_token/", payload_access_token, timeout=timeout)
        js2 = http2.get("json") or {}
        if http2["status_code"] >= 400 or int(js2.get("code", -1)) != 0:
            # 失败：记录并报错（不在服务层 commit）
            sess.status = "failed"
            sess.error_code = str(js.get("code") or js2.get("code") or "token_exchange_failed")
            sess.error_message = (js.get("message") or js2.get("message") or "token exchange error")[:512]
            db.add(sess)
            raise APIError("TOKEN_EXCHANGE_FAILED", sess.error_message, 502)
        js = js2  # 用兜底成功的结果

    token, scope = _parse_token_response(js)
    logger.info("TTB token exchange ok state=%s has_token=%s", state, bool(token))
    if not token:
        sess.status = "failed"
        sess.error_code = "no_token"
        sess.error_message = "no access_token in response"
        db.add(sess)
        raise APIError("TOKEN_EXCHANGE_FAILED", "no access_token", 502)

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
    sess.consumed_at = datetime.utcnow()
    db.add(sess)
    db.flush()

    # 触发后端引导同步（含商品），无需等待前端手动确认
    _enqueue_bootstrap_after_binding(
        workspace_id=int(sess.workspace_id), auth_id=int(account.id), state=getattr(sess, "state", None)
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


# ---------- revoke (NEW: /oauth2/revoke_token/) ----------
async def revoke_remote_token(*, access_token: str, app: OAuthProviderApp, timeout: float) -> None:
    """
    TikTok Business 撤销长期令牌：
      POST /open_api/v1.3/oauth2/revoke_token/
      要求：
        - Header: Access-Token: <要撤销的那个 access_token>
        - Content-Type: application/json
        - Body(首选): { "app_id": "...", "secret": "...", "access_token": "..." }
        - Body(兼容): { "client_id": "...", "client_secret": "...", "token": "..." }
    """
    secret = _decrypt_app_secret(app)
    url = settings.TT_BIZ_TOKEN_URL.rstrip("/") + "/oauth2/revoke_token/"

    headers_json = {
        "Content-Type": "application/json",
        "Access-Token": access_token,  # ★ 必填：要撤销的 token 也要放在 Header
    }

    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        # 1) 官方首选字段名：app_id / secret / access_token
        payload1 = {
            "app_id": app.client_id,
            "secret": secret,
            "access_token": access_token,
        }
        r1 = await client.post(url, json=payload1, headers=headers_json)
        try:
            js1 = r1.json()
        except Exception:
            js1 = {}
        logger.debug(
            "TTB REVOKE JSON#1 status=%s json=%s",
            getattr(r1, "status_code", "?"),
            {k: ("***" if k in {"access_token", "secret"} else v) for k, v in (js1 or {}).items()},
        )
        if (r1.status_code < 400) and isinstance(js1, dict) and int(js1.get("code", -1)) == 0:
            return

        # 2) 兼容字段名：client_id / client_secret / token （仍然用 JSON）
        payload2 = {
            "client_id": app.client_id,
            "client_secret": secret,
            "token": access_token,
        }
        r2 = await client.post(url, json=payload2, headers=headers_json)
        try:
            js2 = r2.json()
        except Exception:
            js2 = {}
        logger.debug(
            "TTB REVOKE JSON#2 status=%s json=%s",
            getattr(r2, "status_code", "?"),
            {k: ("***" if k in {"token", "client_secret"} else v) for k, v in (js2 or {}).items()},
        )
        if (r2.status_code < 400) and isinstance(js2, dict) and int(js2.get("code", -1)) == 0:
            return

    msg = (
        (isinstance(js2, dict) and js2.get("message"))
        or (isinstance(js1, dict) and js1.get("message"))
        or "revoke failed"
    )
    raise APIError("REVOKE_FAILED", msg, 502)


def _mark_local_revoked(db: Session, *, workspace_id: int, auth_id: int) -> None:
    """
    本地软撤销：将账户标记为 revoked，并清空别名 alias。
    （已移除广告主表，无需清理关联）
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
            revoked_at=datetime.utcnow(),
            alias=None,  # 清空名称，避免展示冲突
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


# =========================
#  统一的 Access-Token 解析入口（唯一对外）
# =========================

def _pick_token_blob_from_account(acc: OAuthAccountTTB) -> bytes | None:
    """
    从常见密文字段中择优返回密文（bytes）。如不存在则返回 None。
    """
    for fname in (
        "access_token_cipher",
        "token_cipher",
        "access_token_encrypted",
        "access_token_blob",
    ):
        if hasattr(acc, fname):
            val = getattr(acc, fname)
            if isinstance(val, (bytes, memoryview)) and val:
                return bytes(val)
    return None


def _try_decrypt_with_app(blob: bytes, app: OAuthProviderApp) -> str:
    """
    使用 provider app 的 AAD 解密 token 密文。
    """
    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    return decrypt_blob_to_text(blob, aad_text=aad)


def get_access_token_for_auth_id(db: Session, auth_id: int) -> str:
    """
    统一权威入口：根据绑定 ID 解析出 Access-Token（优先密文解密，兜底明文）。
    - 使用 ProviderApp 的 (provider|client_id|redirect_uri) 作为 AAD 解密；
    - 兼容多字段名；
    - 若均不可用，抛 APIError 500。
    """
    acc = db.get(OAuthAccountTTB, int(auth_id))
    if not acc:
        raise APIError("NOT_FOUND", "oauth account not found", 404)

    app = db.get(OAuthProviderApp, int(acc.provider_app_id))
    if not app:
        raise APIError("NOT_FOUND", "provider app not found", 404)

    blob = _pick_token_blob_from_account(acc)
    if blob:
        try:
            return _try_decrypt_with_app(blob, app)
        except Exception as e:
            logger.debug("decrypt token blob failed for auth_id=%s: %s", auth_id, e)

    for fname in ("access_token_plain", "access_token_decrypted", "access_token"):
        if hasattr(acc, fname):
            val = getattr(acc, fname)
            if isinstance(val, str) and val:
                return val

    raise APIError("TOKEN_NOT_FOUND", "cannot resolve access_token for this binding", 500)


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

# === 追加到 app/services/oauth_ttb.py 末尾，作为公共取凭据的辅助 ===

def get_credentials_for_auth_id(db: Session, auth_id: int) -> tuple[str, str, str]:
    """
    返回 (app_id, app_secret_plain, redirect_uri)
    - 严格密文解密（使用 provider|client_id|redirect_uri 作为 AAD）
    - 不兜底明文
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

