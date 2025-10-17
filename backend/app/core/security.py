# app/core/security.py
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, Dict, Optional

from fastapi import Request, Response

from app.core.config import settings


# ---- password hashing -------------------------------------------------
def hash_password(password: str) -> str:
    """PBKDF2-SHA256 派生并编码为 django-like 形式：pbkdf2_sha256$iters$salt$hash"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, settings.PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256$%d$%s$%s" % (
        settings.PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(dk).decode().rstrip("="),
    )
def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iter_s, salt_b64, hash_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iters = int(iter_s)
        salt = base64.urlsafe_b64decode(salt_b64 + "==")
        expect = base64.urlsafe_b64decode(hash_b64 + "==")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


# ---- session cookie (HMAC) -------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


def _sign(p64: str) -> str:
    mac = hmac.new(settings.SECRET_KEY.encode(), p64.encode(), hashlib.sha256).digest()
    return _b64(mac)


def write_session(
    resp: Response,
    data: Dict[str, Any],
    *,
    remember: bool | None = None,
    max_age: Optional[int] = None,
) -> None:
    """
    写入登录会话 Cookie。

    - remember == True  → 持久化 Cookie（带 Max-Age，优先使用 SESSION_REMEMBER_MAX_AGE_SECONDS）
    - remember == False → 会话 Cookie（不带 Max-Age）
    - remember == None  → 兼容旧行为，使用 SESSION_MAX_AGE_SECONDS
    - 如果传入 max_age，则优先生效
    """
    payload = json.dumps(
        {"id": int(data["id"])}, separators=(",", ":"), ensure_ascii=False
    ).encode()
    p64 = _b64(payload)
    sig = _sign(p64)
    cookie_val = p64 + "." + sig

    if max_age is not None:
        _max_age = int(max_age)
    else:
        if remember is False:
            _max_age = None  # 会话 Cookie
        elif remember is True:
            _max_age = int(
                getattr(settings, "SESSION_REMEMBER_MAX_AGE_SECONDS", None)
                or settings.SESSION_MAX_AGE_SECONDS
            )
        else:
            _max_age = settings.SESSION_MAX_AGE_SECONDS

    resp.set_cookie(
        key=settings.COOKIE_NAME,
        value=cookie_val,
        max_age=_max_age,  # None 时不写该属性 → 会话 Cookie
        httponly=True,
        secure=bool(settings.COOKIE_SECURE),
        samesite=str(settings.COOKIE_SAMESITE).lower(),
        domain=settings.COOKIE_DOMAIN or None,
        path="/",
    )


def clear_session(resp: Response) -> None:
    resp.delete_cookie(
        key=settings.COOKIE_NAME,
        domain=settings.COOKIE_DOMAIN or None,
        path="/",
    )


def read_session_from_request(req: Request) -> Optional[Dict[str, Any]]:
    raw = req.cookies.get(settings.COOKIE_NAME)
    if not raw or "." not in raw:
        return None
    p64, sig = raw.split(".", 1)
    if not hmac.compare_digest(sig, _sign(p64)):
        return None
    try:
        payload = json.loads(_b64d(p64).decode())
        if not payload.get("id"):
            return None
        return payload
    except Exception:
        return None


def _clean_ip(value: str | None) -> str | None:
    if not value:
        return None
    ip = value.strip()
    if not ip:
        return None
    if ip.lower() in {"unknown", "null", "none"}:
        return None
    return ip


def client_ip(req: Request) -> str | None:
    for header_name in ("x-forwarded-for", "x-real-ip"):
        header_val = req.headers.get(header_name)
        if not header_val:
            continue
        first = header_val.split(",")[0]
        cleaned = _clean_ip(first)
        if cleaned:
            return cleaned

    if req.client:
        return _clean_ip(getattr(req.client, "host", None))
    return None

