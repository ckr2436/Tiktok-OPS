from sqlalchemy import select

from app.core.deps import SessionUser, require_platform_admin, require_session
from app.data.models.audit_logs import AuditLog
from app.data.models.providers import (
    PlatformPolicy,
    PolicyEnforcementMode,
    PolicyMode,
)


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


def test_policy_v1_crud_flow(app_client, db_session) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    create_payload = {
        "provider_key": "tiktok-business",
        "name": "Lead Ads Allow",
        "mode": "whitelist",
        "enforcement_mode": "enforce",
        "domains": ["ads.example.com", "ADS.EXAMPLE.com"],
        "business_scopes": {
            "include": {
                "bc_ids": ["123", "123"],
                "advertiser_ids": ["a1"],
            },
            "exclude": {
                "advertiser_ids": ["a1", "a2"],
            },
        },
        "description": "Allow lead ads flow",
        "is_enabled": True,
        "rate_limit_rps": 10,
        "rate_burst": 25,
        "cooldown_seconds": 5,
        "max_concurrency": 3,
        "max_entities_per_run": 100,
        "window_cron": None,
    }

    resp = client.post(
        "/api/v1/admin/platform/policies",
        json=create_payload,
        headers={"x-policy-domain": "ads.example.com"},
    )
    assert resp.status_code == 201, resp.text
    policy = resp.json()
    assert policy["provider_key"] == "tiktok-business"
    assert policy["mode"] == PolicyMode.WHITELIST.value
    assert policy["enforcement_mode"] == PolicyEnforcementMode.ENFORCE.value
    assert policy["name"] == "Lead Ads Allow"
    assert policy["domains"] == ["ads.example.com"]
    assert policy["business_scopes"]["include"]["bc_ids"] == ["123"]
    assert policy["business_scopes"]["exclude"]["advertiser_ids"] == ["a2"]
    assert policy["limits"]["rate_limit_rps"] == 10
    assert policy["limits"]["max_concurrency"] == 3
    assert policy["limits"]["cooldown_seconds"] == 5
    policy_id = policy["id"]

    # duplicate name (case-insensitive) should conflict
    resp = client.post(
        "/api/v1/admin/platform/policies",
        json={**create_payload, "name": "lead ads allow"},
        headers={"x-policy-domain": "ads.example.com"},
    )
    assert resp.status_code == 409, resp.json()
    body = resp.json()
    assert body["error"]["code"] == "DUPLICATE_NAME"

    # list and filter
    resp = client.get(
        "/api/v1/admin/platform/policies",
        params={
            "provider_key": "tiktok-business",
            "mode": "whitelist",
            "domain": "ads",
            "name": "Lead",
            "status": "enabled",
        },
    )
    assert resp.status_code == 200
    listing = resp.json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == policy_id

    # ensure legacy path is gone
    resp = client.get("/api/admin/platform/policies")
    assert resp.status_code == 404

    update_payload = {
        "provider_key": "tiktok-business",
        "name": "Lead Ads Block",
        "mode": "blacklist",
        "enforcement_mode": "dryrun",
        "domains": ["blocked.example.com"],
        "business_scopes": {
            "include": {
                "shop_ids": ["S1"],
                "product_ids": ["SKU-1"],
            },
            "exclude": {},
        },
        "description": "Block specific shop",
        "is_enabled": True,
        "rate_limit_rps": 5,
        "rate_burst": 10,
        "cooldown_seconds": 0,
        "max_concurrency": None,
        "max_entities_per_run": None,
        "window_cron": None,
    }

    resp = client.put(
        f"/api/v1/admin/platform/policies/{policy_id}",
        json=update_payload,
        headers={"x-policy-domain": "ads.example.com"},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["mode"] == PolicyMode.BLACKLIST.value
    assert updated["enforcement_mode"] == PolicyEnforcementMode.DRYRUN.value
    assert updated["status"] == "ENABLED"
    assert updated["business_scopes"]["include"]["shop_ids"] == ["S1"]
    assert updated["limits"]["rate_limit_rps"] == 5

    # disable is idempotent
    resp = client.post(
        f"/api/v1/admin/platform/policies/{policy_id}/disable",
        headers={"x-policy-domain": "blocked.example.com"},
    )
    assert resp.status_code == 200
    disabled = resp.json()
    assert disabled["status"] == "DISABLED"

    resp = client.post(
        f"/api/v1/admin/platform/policies/{policy_id}/disable",
        headers={"x-policy-domain": "blocked.example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "DISABLED"

    # enable
    resp = client.post(
        f"/api/v1/admin/platform/policies/{policy_id}/enable",
        headers={"x-policy-domain": "blocked.example.com"},
    )
    assert resp.status_code == 200
    enabled = resp.json()
    assert enabled["status"] == "ENABLED"

    # delete
    resp = client.delete(
        f"/api/v1/admin/platform/policies/{policy_id}",
        headers={"x-policy-domain": "blocked.example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.get("/api/v1/admin/platform/policies")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    logs = db_session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    actions = [log.action for log in logs]
    assert actions == [
        "policy.create",
        "policy.update",
        "policy.disable",
        "policy.enable",
        "policy.delete",
    ]
    assert actions.count("policy.disable") == 1
    for log in logs:
        assert log.resource_type == "platform_policy"
        assert log.actor_user_id == admin_user.id
        assert log.details is not None


def test_policy_v1_validation_errors(app_client) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    resp = client.post(
        "/api/v1/admin/platform/policies",
        json={
            "provider_key": "unknown",
            "name": "Invalid",
            "mode": "block",
            "enforcement_mode": "off",
            "domains": ["http://bad"],
            "business_scopes": {"include": {"invalid_key": ["1"]}},
        },
        headers={"x-policy-domain": "admin.ops.local"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    fields = body["error"]["data"]["fields"]
    assert "mode" in fields
    assert "domains[0]" in fields
    assert "business_scopes.include.invalid_key" in fields

    resp = client.post(
        "/api/v1/admin/platform/policies",
        json={
            "provider_key": "unknown",
            "name": "Valid",
            "mode": "whitelist",
            "enforcement_mode": "enforce",
            "domains": ["valid.example"],
            "business_scopes": {},
        },
        headers={"x-policy-domain": "admin.ops.local"},
    )
    assert resp.status_code == 422
    provider_error = resp.json()["error"]["data"]["fields"]
    assert provider_error["provider_key"] == "provider is not registered."


def test_policy_v1_listing_filters(app_client, db_session) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    client.post(
        "/api/v1/admin/platform/policies",
        json={
            "provider_key": "tiktok-business",
            "name": "Policy 1",
            "mode": "whitelist",
            "enforcement_mode": "enforce",
            "domains": ["a.example.com"],
            "business_scopes": {},
        },
        headers={"x-policy-domain": "admin.ops.local"},
    )

    client.post(
        "/api/v1/admin/platform/policies",
        json={
            "provider_key": "tiktok-business",
            "name": "Policy 2",
            "mode": "blacklist",
            "enforcement_mode": "dryrun",
            "domains": ["b.example.com"],
            "business_scopes": {},
            "is_enabled": False,
        },
        headers={"x-policy-domain": "admin.ops.local"},
    )

    resp = client.get(
        "/api/v1/admin/platform/policies",
        params={
            "provider_key": "tiktok-business",
            "mode": "whitelist",
            "status": "enabled",
        },
    )
    assert resp.status_code == 200
    assert {item["name"] for item in resp.json()["items"]} == {"Policy 1"}


def test_policy_enforcement_denies_blocked_domain(app_client, db_session) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    blocked = PlatformPolicy(
        provider_key="tiktok-business",
        name="Block admin",
        name_normalized="block admin",
        mode=PolicyMode.BLACKLIST.value,
        enforcement_mode=PolicyEnforcementMode.ENFORCE.value,
        domains_json=["blocked.example.com"],
        domain="blocked.example.com",
        business_scopes_json={"include": {}, "exclude": {}},
        is_enabled=True,
    )
    db_session.add(blocked)
    db_session.commit()

    resp = client.post(
        "/api/v1/admin/platform/policies",
        json={
            "provider_key": "tiktok-business",
            "name": "New Policy",
            "mode": "whitelist",
            "enforcement_mode": "enforce",
            "domains": ["allowed.example.com"],
            "business_scopes": {},
        },
        headers={"x-policy-domain": "blocked.example.com"},
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "POLICY_DENIED"
    decision = body["error"]["data"]["decision"]
    assert blocked.id in decision["matched_policy_ids"]

    logs = db_session.scalars(select(AuditLog)).all()
    deny_logs = [log for log in logs if log.action == "policy.enforce.deny"]
    assert deny_logs, "Expected audit log for policy enforcement deny"
    assert deny_logs[0].details["provider_key"] == "tiktok-business"


def test_policy_dry_run_endpoint(app_client, db_session) -> None:
    app, client = app_client
    admin_user = _make_admin_user()
    app.dependency_overrides[require_session] = lambda: admin_user
    app.dependency_overrides[require_platform_admin] = lambda: admin_user

    resp = client.post(
        "/api/v1/admin/platform/policies",
        json={
            "provider_key": "tiktok-business",
            "name": "Dryrun Policy",
            "mode": "blacklist",
            "enforcement_mode": "enforce",
            "domains": ["blocked.example.com"],
            "business_scopes": {},
            "rate_limit_rps": 15,
            "rate_burst": 30,
            "cooldown_seconds": 7,
        },
        headers={"x-policy-domain": "admin.ops.local"},
    )
    assert resp.status_code == 201
    policy_id = resp.json()["id"]

    resp = client.post(
        f"/api/v1/admin/platform/policies/{policy_id}/dry-run",
        json={
            "resource_type": "admin.platform.policy.test",
            "candidates": [
                {"domain": "blocked.example.com"},
                {"domain": "other.example.com", "shop_id": "s1"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider_key"] == "tiktok-business"
    assert body["limits"]["rate_limit_rps"] == 15
    assert isinstance(body["trace"], list)
    assert body["trace"][0]["policy_id"] == policy_id
    assert resp.headers["X-Policy-Decision"] in {"allow", "deny"}
