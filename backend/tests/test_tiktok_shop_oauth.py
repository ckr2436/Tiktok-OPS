from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.services.oauth_tiktok_shop import (
    _apply_shop_timezone_policy,
    oauth_callback_error_reason,
    parse_token_response,
    sign_api_request,
)


def test_sign_api_request_uses_official_canonical_order() -> None:
    signature = sign_api_request(
        path="/authorization/202309/shops",
        params={
            "timestamp": 1234567890,
            "app_key": "123456",
            "sign": "ignored",
            "access_token": "ignored",
        },
        app_secret="abc000def111",
    )

    assert signature == "c8767e4fd7a1b48a0ce3fd23197f058159ce11b53f1ae1ad6d1ec931a1da5540"


def test_parse_token_response_accepts_absolute_expiry_and_scopes() -> None:
    parsed = parse_token_response(
        {
            "code": 0,
            "message": "Success",
            "data": {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "open_id": "seller-open-id",
                "access_token_expire_in": 1_900_000_000,
                "refresh_token_expire_in": 1_910_000_000,
                "granted_scopes": ["seller.product.basic", "seller.authorization.info"],
            },
        }
    )

    assert parsed["open_id"] == "seller-open-id"
    assert parsed["granted_scopes"] == [
        "seller.product.basic",
        "seller.authorization.info",
    ]
    assert parsed["expires_at"] == datetime.fromtimestamp(1_900_000_000, timezone.utc).replace(tzinfo=None)
    assert parsed["raw"]["access_token"] == "***"
    assert parsed["raw"]["refresh_token"] == "***"


def test_parse_token_response_rejects_incomplete_credentials() -> None:
    with pytest.raises(APIError) as exc:
        parse_token_response(
            {
                "code": 0,
                "data": {
                    "access_token": "access-secret",
                    "open_id": "seller-open-id",
                },
            }
        )

    assert exc.value.code == "TOKEN_EXCHANGE_FAILED"


def test_shop_order_timezone_policy_is_fixed_and_locked() -> None:
    verified_at = datetime(2026, 7, 20, 12)
    shop = OAuthTikTokShopShop(
        workspace_id=3,
        account_id=1,
        shop_id="shop-1",
        shop_cipher="cipher",
        timezone_name="America/New_York",
        timezone_source="provider",
        timezone_locked=False,
    )

    _apply_shop_timezone_policy(shop, verified_at=verified_at)

    assert shop.timezone_name == "Etc/GMT+8"
    assert shop.timezone_source == "merchant_confirmed_fixed_utc_minus_8"
    assert shop.timezone_verified_at == verified_at
    assert shop.timezone_locked is True


def test_parse_token_response_preserves_safe_provider_diagnostics() -> None:
    with pytest.raises(APIError) as exc:
        parse_token_response(
            {
                "code": 10003,
                "message": "invalid client_key",
                "request_id": "request-123",
            }
        )

    assert exc.value.code == "TOKEN_EXCHANGE_FAILED"
    assert exc.value.data == {
        "provider_code": 10003,
        "request_id": "request-123",
    }


@pytest.mark.parametrize(
    ("error_code", "error_message", "expected"),
    [
        ("TOKEN_EXCHANGE_FAILED", "invalid client_key", "APP_CREDENTIALS_INVALID"),
        ("APP_KEY_MISMATCH", "", "APP_CREDENTIALS_INVALID"),
        ("TOKEN_EXCHANGE_FAILED", "auth_code is invalid", "AUTH_CODE_INVALID_OR_EXPIRED"),
        ("SESSION_EXPIRED", "", "AUTH_SESSION_EXPIRED"),
        ("AUTH_DENIED", "", "AUTH_DENIED"),
        ("TIKTOK_SHOP_UNAVAILABLE", "", "TIKTOK_SHOP_UNAVAILABLE"),
        ("TOKEN_EXCHANGE_FAILED", "unexpected provider response", "TOKEN_EXCHANGE_FAILED"),
    ],
)
def test_oauth_callback_error_reason(
    error_code: str,
    error_message: str,
    expected: str,
) -> None:
    assert oauth_callback_error_reason(error_code, error_message) == expected
