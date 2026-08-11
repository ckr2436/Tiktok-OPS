from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.flow_account_proxy import FlowAccountProxy


PROXY_ENCRYPTION_PREFIX = "enc:v1:"
ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def normalize_proxy_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理地址或端口格式无效") from exc
    scheme = str(parsed.scheme or "").lower()
    host = str(parsed.hostname or "").strip().lower()
    if scheme not in ALLOWED_PROXY_SCHEMES:
        raise ValueError("代理必须使用 HTTP、HTTPS、SOCKS5 或 SOCKS5H 协议")
    if not host or port is None or not (1 <= int(port) <= 65535):
        raise ValueError("代理必须包含有效主机和端口")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    display_host = f"[{host}]" if ":" in host else host
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(parsed.password, safe="")
        userinfo += "@"
    return urlunsplit((scheme, f"{userinfo}{display_host}:{int(port)}", "", "", ""))


def proxy_display_url(value: str) -> str:
    parsed = urlsplit(normalize_proxy_url(value))
    host = str(parsed.hostname or "")
    display_host = f"[{host}]" if ":" in host else host
    return urlunsplit((parsed.scheme, f"{display_host}:{parsed.port}", "", "", ""))


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_proxy_url(value: str) -> str:
    normalized = normalize_proxy_url(value)
    token = _fernet().encrypt(normalized.encode("utf-8")).decode("ascii")
    return f"{PROXY_ENCRYPTION_PREFIX}{token}"


def decrypt_proxy_url(value: str) -> str:
    ciphertext = str(value or "").strip()
    if not ciphertext.startswith(PROXY_ENCRYPTION_PREFIX):
        raise ValueError("代理凭据格式无效")
    try:
        plaintext = _fernet().decrypt(
            ciphertext[len(PROXY_ENCRYPTION_PREFIX) :].encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("代理凭据无法解密") from exc
    return normalize_proxy_url(plaintext)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(normalize_proxy_url(value).encode("utf-8")).hexdigest()


def serialize_proxy(row: FlowAccountProxy, *, in_use_count: int = 0) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "name": row.name,
        "display_url": row.display_url,
        "is_active": bool(row.is_active),
        "in_use_count": max(0, int(in_use_count)),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def list_flow_proxies(db: Session, *, active_only: bool = False) -> list[FlowAccountProxy]:
    query = db.query(FlowAccountProxy)
    if active_only:
        query = query.filter(FlowAccountProxy.is_active.is_(True))
    return query.order_by(FlowAccountProxy.is_active.desc(), FlowAccountProxy.id.asc()).all()


def get_flow_proxy(db: Session, proxy_id: int, *, require_active: bool = False) -> FlowAccountProxy:
    row = db.get(FlowAccountProxy, int(proxy_id))
    if row is None:
        raise ValueError("代理不存在")
    if require_active and not row.is_active:
        raise ValueError("代理已停用")
    return row


def resolve_flow_proxy_url(db: Session, proxy_id: int, *, require_active: bool = True) -> str:
    return decrypt_proxy_url(
        get_flow_proxy(db, proxy_id, require_active=require_active).proxy_url_ciphertext
    )


def find_proxy_by_url(db: Session, value: str) -> FlowAccountProxy | None:
    try:
        fingerprint = _fingerprint(value)
    except ValueError:
        return None
    return (
        db.query(FlowAccountProxy)
        .filter(FlowAccountProxy.proxy_url_fingerprint == fingerprint)
        .one_or_none()
    )


def create_flow_proxy(
    db: Session,
    *,
    name: str,
    proxy_url: str,
    is_active: bool = True,
    actor_user_id: int | None = None,
) -> FlowAccountProxy:
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        raise ValueError("代理名称不能为空")
    normalized = normalize_proxy_url(proxy_url)
    row = FlowAccountProxy(
        name=cleaned_name,
        proxy_url_ciphertext=encrypt_proxy_url(normalized),
        proxy_url_fingerprint=_fingerprint(normalized),
        display_url=proxy_display_url(normalized),
        is_active=bool(is_active),
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def update_flow_proxy(
    db: Session,
    *,
    row: FlowAccountProxy,
    name: str | None = None,
    proxy_url: str | None = None,
    is_active: bool | None = None,
    actor_user_id: int | None = None,
) -> FlowAccountProxy:
    if name is not None:
        cleaned_name = str(name).strip()
        if not cleaned_name:
            raise ValueError("代理名称不能为空")
        row.name = cleaned_name
    if proxy_url is not None:
        normalized = normalize_proxy_url(proxy_url)
        row.proxy_url_ciphertext = encrypt_proxy_url(normalized)
        row.proxy_url_fingerprint = _fingerprint(normalized)
        row.display_url = proxy_display_url(normalized)
    if is_active is not None:
        row.is_active = bool(is_active)
    row.updated_by_user_id = actor_user_id
    db.add(row)
    db.flush()
    return row


__all__ = [
    "create_flow_proxy",
    "decrypt_proxy_url",
    "find_proxy_by_url",
    "get_flow_proxy",
    "list_flow_proxies",
    "normalize_proxy_url",
    "proxy_display_url",
    "resolve_flow_proxy_url",
    "serialize_proxy",
    "update_flow_proxy",
]
