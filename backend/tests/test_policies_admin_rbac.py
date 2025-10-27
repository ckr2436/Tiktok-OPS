from __future__ import annotations

from app.core.deps import SessionUser, require_session
from app.data.models.providers import PolicyMode


def _non_admin() -> SessionUser:
    return SessionUser(
        id=2,
        email="user@example.com",
        username="user",
        display_name="User",
        usercode="000000002",
        is_platform_admin=False,
        workspace_id=1,
        role="member",
        is_active=True,
    )


def test_admin_endpoints_require_platform_admin(app_client) -> None:
    app, client = app_client
    app.dependency_overrides[require_session] = _non_admin

    endpoints = [
        (client.get, "/api/v1/admin/platform/policies"),
        (
            client.post,
            "/api/v1/admin/platform/policies",
            {
                "json": {
                    "provider_key": "tiktok-business",
                    "name": "Test",
                    "mode": PolicyMode.WHITELIST.value,
                    "enforcement_mode": "ENFORCE",
                    "domains": ["example.com"],
                }
            },
        ),
        (
            client.put,
            "/api/v1/admin/platform/policies/1",
            {
                "json": {
                    "provider_key": "tiktok-business",
                    "name": "Test",
                    "mode": PolicyMode.WHITELIST.value,
                    "enforcement_mode": "ENFORCE",
                    "domains": ["example.com"],
                }
            },
        ),
        (client.post, "/api/v1/admin/platform/policies/1/enable"),
        (client.post, "/api/v1/admin/platform/policies/1/disable"),
        (client.delete, "/api/v1/admin/platform/policies/1"),
    ]

    for entry in endpoints:
        func, path, *rest = entry
        kwargs = rest[0] if rest else {}
        resp = func(path, **kwargs)
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "FORBIDDEN"
