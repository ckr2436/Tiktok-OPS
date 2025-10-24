from __future__ import annotations

from sqlalchemy import select

from app.core.deps import SessionUser, require_platform_admin, require_session
from app.data.models.audit_logs import AuditLog
from sqlalchemy import text

from app.data.models.providers import PolicyDomain, PolicyMode
from app.data.models.users import User
from app.data.models.workspaces import Workspace


def _create_workspace_and_user(db) -> tuple[Workspace, User]:
    now = "2024-01-01 00:00:00"
    db.execute(
        text(
            "INSERT INTO workspaces (id, name, company_code, created_at, updated_at)"
            " VALUES (:id, :name, :code, :created, :updated)"
        ),
        {"id": 1, "name": "Platform", "code": "0000", "created": now, "updated": now},
    )
    db.execute(
        text(
            "INSERT INTO users (id, workspace_id, email, username, display_name, password_hash, is_active,"
            " is_platform_admin, role, usercode, created_at, updated_at)"
            " VALUES (:id, :workspace_id, :email, :username, :display_name, :password_hash, :is_active,"
            " :is_platform_admin, :role, :usercode, :created, :updated)"
        ),
        {
            "id": 1,
            "workspace_id": 1,
            "email": "admin@example.com",
            "username": "admin",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": 1,
            "is_platform_admin": 1,
            "role": "owner",
            "usercode": "000000001",
            "created": now,
            "updated": now,
        },
    )
    db.commit()
    ws_obj = Workspace(id=1, name="Platform", company_code="0000")
    user_obj = User(
        id=1,
        workspace_id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        password_hash="hash",
        is_active=True,
        is_platform_admin=True,
        role="owner",
        usercode="000000001",
    )
    return ws_obj, user_obj


def _make_session_user(user: User) -> SessionUser:
    return SessionUser(
        id=int(user.id),
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        usercode=user.usercode,
        is_platform_admin=bool(user.is_platform_admin),
        workspace_id=int(user.workspace_id),
        role=str(user.role),
        is_active=bool(user.is_active),
    )


def _override_admin(app, user: SessionUser) -> None:
    def _current_user() -> SessionUser:
        return user

    app.dependency_overrides[require_session] = _current_user
    app.dependency_overrides[require_platform_admin] = _current_user


def test_policy_crud_flow(app_client, db_session) -> None:
    app, client = app_client
    ws, user = _create_workspace_and_user(db_session)
    admin_user = _make_session_user(user)
    _override_admin(app, admin_user)

    db_session.execute(text("DELETE FROM platform_providers"))
    db_session.commit()

    resp = client.post(
        "/api/admin/providers",
        json={"key": "tiktok-business", "display_name": None, "is_enabled": True},
    )
    assert resp.status_code == 200, resp.json()
    provider_id = resp.json()["id"]
    assert provider_id

    resp = client.post(
        "/api/admin/policies",
        json={
            "provider_key": "tiktok-business",
            "workspace_id": None,
            "mode": PolicyMode.WHITELIST.value,
            "is_enabled": True,
            "description": "Global allow list",
        },
    )
    assert resp.status_code == 200, resp.json()
    policy = resp.json()
    assert policy["provider_key"] == "tiktok-business"
    assert policy["mode"] == PolicyMode.WHITELIST.value
    policy_id = policy["id"]

    resp = client.post(
        f"/api/admin/policies/{policy_id}/items",
        json={"domain": PolicyDomain.ADVERTISER.value, "item_id": "123"},
    )
    assert resp.status_code == 200, resp.json()
    item = resp.json()
    assert item["domain"] == PolicyDomain.ADVERTISER.value

    resp = client.post(
        f"/api/admin/policies/{policy_id}/items",
        json={"domain": PolicyDomain.ADVERTISER.value, "item_id": "123"},
    )
    assert resp.status_code == 409

    resp = client.get("/api/admin/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["items"][0]["item_id"] == "123"

    resp = client.patch(
        f"/api/admin/policies/{policy_id}",
        json={"mode": PolicyMode.BLACKLIST.value, "description": "Block"},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["mode"] == PolicyMode.BLACKLIST.value
    assert updated["description"] == "Block"

    resp = client.delete(f"/api/admin/policies/{policy_id}/items/123")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.delete(f"/api/admin/policies/{policy_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.get("/api/admin/policies")
    assert resp.status_code == 200
    assert resp.json() == []


def test_policy_requires_platform_admin(app_client) -> None:
    app, client = app_client

    non_admin = SessionUser(
        id=1,
        email="user@example.com",
        username="user",
        display_name="User",
        usercode="000000002",
        is_platform_admin=False,
        workspace_id=1,
        role="member",
        is_active=True,
    )

    def _current_user() -> SessionUser:
        return non_admin

    app.dependency_overrides[require_session] = _current_user

    resp = client.post("/api/admin/providers", json={"key": "tiktok-business", "display_name": "T", "is_enabled": True})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "FORBIDDEN"


def test_audit_logs_created(app_client, db_session) -> None:
    app, client = app_client
    ws, user = _create_workspace_and_user(db_session)
    admin_user = _make_session_user(user)
    _override_admin(app, admin_user)

    db_session.execute(text("DELETE FROM platform_providers"))
    db_session.commit()

    resp = client.post(
        "/api/admin/providers",
        json={"key": "tiktok-business", "display_name": "TikTok", "is_enabled": True},
    )
    assert resp.status_code == 200, resp.json()
    resp = client.post(
        "/api/admin/policies",
        json={
            "provider_key": "tiktok-business",
            "workspace_id": int(ws.id),
            "mode": PolicyMode.WHITELIST.value,
            "is_enabled": True,
            "description": "Scoped",
        },
    )
    policy_id = resp.json()["id"]

    client.post(
        f"/api/admin/policies/{policy_id}/items",
        json={"domain": PolicyDomain.SHOP.value, "item_id": "shop-1"},
    )
    client.patch(
        f"/api/admin/policies/{policy_id}",
        json={"is_enabled": False},
    )
    client.delete(f"/api/admin/policies/{policy_id}/items/shop-1")
    client.delete(f"/api/admin/policies/{policy_id}")

    logs = db_session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    actions = [log.action for log in logs]
    assert actions == [
        "platform.provider.create",
        "platform.policy.create",
        "platform.policy_item.create",
        "platform.policy.update",
        "platform.policy_item.delete",
        "platform.policy.delete",
    ]
