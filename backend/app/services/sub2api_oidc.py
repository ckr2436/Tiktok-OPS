from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import settings
from app.services.redis_client import get_redis_sync


SHARED_ADMIN_SUBJECT = "gmv-platform-admin"
SHARED_ADMIN_EMAIL = "admin@sub2api.local"
_REDIS_PREFIX = "gmv:sub2api:oidc"


class OIDCProtocolError(Exception):
    def __init__(self, error: str, description: str, status_code: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SigningMaterial:
    private_key: rsa.RSAPrivateKey
    kid: str
    jwk: dict[str, str]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _uint_b64url(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return _b64url(value.to_bytes(size, "big"))


@lru_cache(maxsize=4)
def load_signing_material(path_value: str) -> SigningMaterial:
    path = Path(path_value).expanduser().resolve(strict=True)
    raw = path.read_bytes()
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise RuntimeError("Sub2API OIDC signing key must be an RSA key of at least 2048 bits")
    public = key.public_key()
    numbers = public.public_numbers()
    der = public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    kid = _b64url(hashlib.sha256(der).digest()[:18])
    return SigningMaterial(
        private_key=key,
        kid=kid,
        jwk={
            "kty": "RSA",
            "kid": kid,
            "use": "sig",
            "alg": "RS256",
            "n": _uint_b64url(numbers.n),
            "e": _uint_b64url(numbers.e),
        },
    )


def signing_material() -> SigningMaterial:
    return load_signing_material(str(settings.SUB2API_OIDC_PRIVATE_KEY_FILE))


def _redis_key(kind: str, token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{_REDIS_PREFIX}:{kind}:{digest}"


def _redis_getdel(client: Any, key: str) -> bytes | str | None:
    getdel = getattr(client, "getdel", None)
    if callable(getdel):
        return getdel(key)
    script = "local v=redis.call('GET',KEYS[1]); if v then redis.call('DEL',KEYS[1]); end; return v"
    return client.eval(script, 1, key)


def _setting_text(name: str) -> str:
    value = str(getattr(settings, name, "") or "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def validate_runtime_config() -> None:
    if not bool(settings.SUB2API_SSO_ENABLED):
        raise OIDCProtocolError("temporarily_unavailable", "Sub2API SSO is disabled", 503)
    try:
        _setting_text("SUB2API_OIDC_ISSUER")
        _setting_text("SUB2API_OIDC_CLIENT_ID")
        _setting_text("SUB2API_OIDC_REDIRECT_URI")
        signing_material()
    except (OSError, RuntimeError, ValueError) as exc:
        raise OIDCProtocolError(
            "temporarily_unavailable",
            "Sub2API SSO is not configured correctly",
            503,
        ) from exc


def validate_authorize_request(
    *,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str,
    code_challenge: str,
    code_challenge_method: str,
) -> None:
    validate_runtime_config()
    if response_type != "code":
        raise OIDCProtocolError("unsupported_response_type", "Only authorization code flow is supported")
    if not secrets.compare_digest(client_id, _setting_text("SUB2API_OIDC_CLIENT_ID")):
        raise OIDCProtocolError("unauthorized_client", "Unknown client")
    if not secrets.compare_digest(redirect_uri, _setting_text("SUB2API_OIDC_REDIRECT_URI")):
        raise OIDCProtocolError("invalid_request", "Invalid redirect_uri")
    if "openid" not in {item for item in scope.split() if item}:
        raise OIDCProtocolError("invalid_scope", "The openid scope is required")
    if not state or len(state) > 512:
        raise OIDCProtocolError("invalid_request", "A bounded state value is required")
    if not nonce or len(nonce) > 512:
        raise OIDCProtocolError("invalid_request", "A bounded nonce value is required")
    if code_challenge_method != "S256":
        raise OIDCProtocolError("invalid_request", "PKCE S256 is required")
    if len(code_challenge) != 43:
        raise OIDCProtocolError("invalid_request", "Invalid PKCE code_challenge")


def issue_authorization_code(
    *,
    actor_user_id: int,
    actor_workspace_id: int,
    client_id: str,
    redirect_uri: str,
    scope: str,
    nonce: str,
    code_challenge: str,
) -> str:
    code = secrets.token_urlsafe(32)
    payload = {
        "actor_user_id": int(actor_user_id),
        "actor_workspace_id": int(actor_workspace_id),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "auth_time": int(time.time()),
    }
    ttl = max(30, min(int(settings.SUB2API_OIDC_CODE_TTL_SECONDS), 300))
    get_redis_sync().setex(
        _redis_key("code", code),
        ttl,
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
    )
    return code


def _parse_json_object(raw: bytes | str | None) -> dict[str, Any]:
    if raw is None:
        raise OIDCProtocolError("invalid_grant", "Authorization code is invalid or already used")
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OIDCProtocolError("invalid_grant", "Authorization code is invalid") from exc
    if not isinstance(value, dict):
        raise OIDCProtocolError("invalid_grant", "Authorization code is invalid")
    return value


def _verify_pkce(verifier: str, expected_challenge: str) -> None:
    if not 43 <= len(verifier) <= 128:
        raise OIDCProtocolError("invalid_grant", "Invalid PKCE code_verifier")
    try:
        encoded_verifier = verifier.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise OIDCProtocolError("invalid_grant", "Invalid PKCE code_verifier") from exc
    actual = _b64url(hashlib.sha256(encoded_verifier).digest())
    if not secrets.compare_digest(actual, expected_challenge):
        raise OIDCProtocolError("invalid_grant", "PKCE verification failed")


def _sign_jwt(payload: dict[str, Any]) -> str:
    material = signing_material()
    header = {"alg": "RS256", "kid": material.kid, "typ": "JWT"}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    encoded_payload = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = material.private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{encoded_header}.{encoded_payload}.{_b64url(signature)}"


def exchange_authorization_code(
    *,
    grant_type: str,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    validate_runtime_config()
    if grant_type != "authorization_code":
        raise OIDCProtocolError("unsupported_grant_type", "Only authorization_code is supported")
    if not code:
        raise OIDCProtocolError("invalid_grant", "Authorization code is required")
    raw = _redis_getdel(get_redis_sync(), _redis_key("code", code))
    data = _parse_json_object(raw)
    if not secrets.compare_digest(client_id, str(data.get("client_id") or "")):
        raise OIDCProtocolError("invalid_grant", "Authorization code client mismatch")
    if not secrets.compare_digest(client_id, _setting_text("SUB2API_OIDC_CLIENT_ID")):
        raise OIDCProtocolError("unauthorized_client", "Unknown client")
    if not secrets.compare_digest(redirect_uri, str(data.get("redirect_uri") or "")):
        raise OIDCProtocolError("invalid_grant", "Authorization code redirect mismatch")
    _verify_pkce(code_verifier, str(data.get("code_challenge") or ""))

    now = int(time.time())
    ttl = max(60, min(int(settings.SUB2API_OIDC_TOKEN_TTL_SECONDS), 900))
    issuer = _setting_text("SUB2API_OIDC_ISSUER")
    claims = {
        "iss": issuer,
        "sub": SHARED_ADMIN_SUBJECT,
        "aud": client_id,
        "iat": now,
        "exp": now + ttl,
        "auth_time": int(data.get("auth_time") or now),
        "nonce": str(data.get("nonce") or ""),
        "email": SHARED_ADMIN_EMAIL,
        "email_verified": True,
        "preferred_username": "gmv_platform_admin",
        "name": "GMV Platform Admin",
    }
    access_token = secrets.token_urlsafe(36)
    userinfo = {
        key: claims[key]
        for key in ("sub", "email", "email_verified", "preferred_username", "name")
    }
    get_redis_sync().setex(
        _redis_key("access", access_token),
        ttl,
        json.dumps(userinfo, separators=(",", ":"), ensure_ascii=True),
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "scope": str(data.get("scope") or "openid email profile"),
        "id_token": _sign_jwt(claims),
    }


def userinfo_for_access_token(access_token: str) -> dict[str, Any]:
    if not access_token:
        raise OIDCProtocolError("invalid_token", "Bearer token is required", 401)
    raw = get_redis_sync().get(_redis_key("access", access_token))
    if raw is None:
        raise OIDCProtocolError("invalid_token", "Bearer token is invalid or expired", 401)
    return _parse_json_object(raw)


def discovery_document() -> dict[str, Any]:
    validate_runtime_config()
    issuer = _setting_text("SUB2API_OIDC_ISSUER").rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/userinfo",
        "jwks_uri": f"{issuer}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": [
            "iss",
            "sub",
            "aud",
            "exp",
            "iat",
            "auth_time",
            "nonce",
            "email",
            "email_verified",
            "preferred_username",
            "name",
        ],
    }
