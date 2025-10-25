from __future__ import annotations

from sqlalchemy import select

from app.core.deps import SessionUser, require_platform_admin, require_session
from app.data.models.audit_logs import AuditLog
from app.data.models.providers import PlatformPolicy, PolicyMode


def _make_admin_user() -> SessionUser:
    return SessionUser(
        id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="000000001",
        is_platform_admin=True,
        workspace_id=1,
        role="owner",
        is_active=True,
    )
def test_policy_crud_flow(app_client, db_session) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    create_payload = {
        "provider_key": "tiktok-business",
        "mode": PolicyMode.WHITELIST.value,
        "domain": "api.example.com",
        "description": "Allow API domain",
    }
    resp = client.post("/api/admin/platform/policies", json=create_payload)
    assert resp.status_code == 201, resp.text
    policy = resp.json()
    assert policy["provider_key"] == "tiktok-business"
    assert policy["mode"] == PolicyMode.WHITELIST.value
    assert policy["domain"] == "api.example.com"
    policy_id = policy["id"]

    # duplicate should conflict
    resp = client.post("/api/admin/platform/policies", json=create_payload)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "POLICY_EXISTS"

    # list without filters
    resp = client.get("/api/admin/platform/policies")
    assert resp.status_code == 200
    listing = resp.json()
    assert listing["total"] == 1
    assert listing["items"][0]["domain"] == "api.example.com"

    # filter by provider and domain substring
    resp = client.get(
        "/api/admin/platform/policies",
        params={"provider_key": "tiktok-business", "domain": "api"},
    )
    assert resp.status_code == 200
    filtered = resp.json()
    assert filtered["total"] == 1

    # update policy
    update_payload = {
        "mode": PolicyMode.BLACKLIST.value,
        "domain": "shop.example.com",
        "description": "Block shop",
    }
    resp = client.patch(
        f"/api/admin/platform/policies/{policy_id}",
        json=update_payload,
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["mode"] == PolicyMode.BLACKLIST.value
    assert updated["domain"] == "shop.example.com"
    assert updated["description"] == "Block shop"

    # toggle enable flag
    resp = client.post(
        f"/api/admin/platform/policies/{policy_id}/toggle",
        json={"is_enabled": False},
    )
    assert resp.status_code == 200
    toggled = resp.json()
    assert toggled["is_enabled"] is False

    # ensure enabled filter works
    resp = client.get(
        "/api/admin/platform/policies",
        params={"enabled": "disabled"},
    )
    assert resp.status_code == 200
    disabled = resp.json()
    assert disabled["total"] == 1

    # delete policy
    resp = client.delete(f"/api/admin/platform/policies/{policy_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.get("/api/admin/platform/policies")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # audit logs should be persisted
    logs = db_session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    actions = [log.action for log in logs]
    assert actions == [
        "policy.create",
        "policy.update",
        "policy.toggle",
        "policy.delete",
    ]
    for log in logs:
        assert log.resource_type == "platform_policy"
        assert log.workspace_id is None
        assert log.actor_user_id == admin_user.id
        assert log.details is not None

    create_details = logs[0].details
    assert create_details["new"]["domain"] == "api.example.com"
    update_details = logs[1].details
    assert update_details["old"]["domain"] == "api.example.com"
    assert update_details["new"]["domain"] == "shop.example.com"
    toggle_details = logs[2].details
    assert toggle_details["old"]["is_enabled"] is True
    assert toggle_details["new"]["is_enabled"] is False


def test_create_policy_unknown_provider_returns_400(app_client) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    resp = client.post(
        "/api/admin/platform/policies",
        json={
            "provider_key": "unknown-provider",
            "mode": PolicyMode.WHITELIST.value,
            "domain": "example.com",
        },
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "PROVIDER_NOT_FOUND"


def test_list_providers_returns_seeded_provider(app_client) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    resp = client.get("/api/admin/platform/policies/providers")

    assert resp.status_code == 200
    payload = resp.json()
    assert any(item["key"] == "tiktok-business" for item in payload)

def test_list_policies_pagination(app_client, db_session) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    for idx in range(35):
        policy = PlatformPolicy(
            provider_key="tiktok-business",
            mode=PolicyMode.WHITELIST.value,
            domain=f"domain{idx}.example.com",
            is_enabled=True,
            description=None,
            created_by_user_id=admin_user.id,
            updated_by_user_id=admin_user.id,
        )
        db_session.add(policy)
    db_session.commit()

    resp = client.get(
        "/api/admin/platform/policies",
        params={"page": 2, "page_size": 20},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 2
    assert data["page_size"] == 20
    assert data["total"] == 35
    assert len(data["items"]) == 15
    assert data["items"][0]["domain"].endswith(".example.com")
