from __future__ import annotations

from starlette.requests import Request

from app.core.deps import SessionUser
from app.data.models.audit_logs import AuditLog
from app.data.models.users import User
from app.data.models.workspaces import Workspace
from app.features.tenants.users.router import UpdateTenantUserRequest, _update_user


def _user(*, workspace_id: int, email: str, username: str, role: str, usercode: str) -> User:
    return User(
        workspace_id=workspace_id,
        email=email,
        username=username,
        display_name=username,
        password_hash="test-password-hash",
        is_active=True,
        is_platform_admin=False,
        role=role,
        usercode=usercode,
    )


def test_owner_can_disable_member_and_update_survives_commit(db_session):
    workspace = Workspace(name="Tenant", company_code="0003")
    db_session.add(workspace)
    db_session.flush()

    owner = _user(
        workspace_id=workspace.id,
        email="owner@example.com",
        username="owner",
        role="owner",
        usercode="000300001",
    )
    member = _user(
        workspace_id=workspace.id,
        email="member@example.com",
        username="member",
        role="member",
        usercode="000300002",
    )
    db_session.add_all([owner, member])
    db_session.flush()
    owner_id = owner.id
    member_id = member.id

    me = SessionUser(
        id=owner_id,
        email=owner.email,
        username=owner.username,
        display_name=owner.display_name,
        usercode=owner.usercode,
        is_platform_admin=False,
        workspace_id=workspace.id,
        role="owner",
        is_active=True,
    )
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": f"/api/v1/tenants/{workspace.id}/users/{member.id}",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )

    response = _update_user(
        workspace.id,
        member_id,
        UpdateTenantUserRequest(is_active=False),
        request,
        me,
        db_session,
    )
    assert response.is_active is False

    db_session.commit()
    db_session.expire_all()

    persisted_active = (
        db_session.query(User.is_active)
        .filter(User.id == member_id)
        .scalar()
    )
    assert persisted_active is False
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "tenant.update_user")
        .one()
    )
    assert audit.target_user_id == member_id
    assert audit.details == {"is_active": False}
