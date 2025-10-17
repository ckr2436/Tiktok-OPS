from __future__ import annotations

import pathlib
import sys

from starlette.requests import Request

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.security import (  # noqa: E402
    client_ip,
    hash_password,
    verify_password,
)


def test_hash_password_roundtrip() -> None:
    secret = "s3cret!"
    encoded = hash_password(secret)

    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password(secret, encoded)
    assert verify_password("密码Pa55", hash_password("密码Pa55"))
    assert not verify_password("wrong-pass", encoded)
def _make_request(headers: dict[str, str] | None = None, client: tuple[str, int] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


def test_client_ip_prefers_forwarded_for_header() -> None:
    req = _make_request({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, client=("127.0.0.1", 12345))
    assert client_ip(req) == "203.0.113.5"


def test_client_ip_skips_placeholder_values() -> None:
    headers = {
        "x-forwarded-for": " unknown , 198.51.100.9",
        "x-real-ip": "198.51.100.9",
    }
    req = _make_request(headers, client=("192.0.2.20", 4321))
    assert client_ip(req) == "198.51.100.9"


def test_client_ip_falls_back_to_client_host() -> None:
    req = _make_request({}, client=("192.0.2.55", 8080))
    assert client_ip(req) == "192.0.2.55"


def test_client_ip_returns_none_when_unavailable() -> None:
    req = _make_request({"x-real-ip": "none"})
    assert client_ip(req) is None
