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
        (client.get, "/api/admin/platform/policies"),
        (client.get, "/api/admin/platform/policies/providers"),
        (
            client.post,
            "/api/admin/platform/policies",
            {
                "json": {
                    "provider_key": "tiktok-business",
                    "mode": PolicyMode.WHITELIST.value,
                    "domain": "example.com",
                }
            },
        ),
        (
            client.patch,
            "/api/admin/platform/policies/1",
            {"json": {"description": "noop"}},
        ),
        (
            client.post,
            "/api/admin/platform/policies/1/toggle",
            {"json": {"is_enabled": False}},
        ),
        (client.delete, "/api/admin/platform/policies/1"),
    ]

    for entry in endpoints:
        func, path, *rest = entry
        kwargs = rest[0] if rest else {}
        resp = func(path, **kwargs)
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "FORBIDDEN"
