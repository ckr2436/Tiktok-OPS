import base64
import importlib
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import install_exception_handlers
from app.data.models import Workspace, OAuthProviderApp, OAuthAccountTTB
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBSyncCursor,
    TTBBCAdvertiserLink,
    TTBAdvertiserStoreLink,
)
from app.features.tenants.ttb.router import router as ttb_router
from app.services.crypto import encrypt_text_to_blob
from app.services.ttb_meta import (
    MetaCursorState,
    MetaSyncEnqueueResult,
    enqueue_meta_sync,
)


@pytest.fixture()
def gmv_app(db_session):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    dummy_user = SessionUser(
        id=1,
        email="member@example.com",
        username="member",
        display_name="Member",
        usercode="U100",
        is_platform_admin=False,
        workspace_id=1,
        role="member",
        is_active=True,
    )

    def _member_override(workspace_id: int):  # noqa: ANN001
        return dummy_user

    app.dependency_overrides[require_tenant_member] = _member_override
    app.dependency_overrides[require_tenant_admin] = _member_override

    _seed_gmv_data(db_session)

    with TestClient(app) as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _seed_gmv_data(db_session) -> None:
    if not getattr(settings, "CRYPTO_MASTER_KEY_B64", ""):
        settings.CRYPTO_MASTER_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode()

    ws = Workspace(id=1, name="Acme", company_code="ACME")
    db_session.add(ws)
    db_session.flush()

    aad = "tiktok_business|client|https://example.com/callback"
    provider = OAuthProviderApp(
        id=1,
        provider="tiktok_business",
        name="Default",
        client_id="client",
        client_secret_cipher=encrypt_text_to_blob("secret", key_version=1, aad_text=aad),
        client_secret_key_version=1,
        redirect_uri="https://example.com/callback",
        is_enabled=True,
    )
    db_session.add(provider)
    db_session.flush()

    account = OAuthAccountTTB(
        id=1,
        workspace_id=int(ws.id),
        provider_app_id=int(provider.id),
        alias="binding",
        access_token_cipher=encrypt_text_to_blob("token", key_version=1, aad_text="token"),
        key_version=1,
        token_fingerprint=b"0" * 32,
        status="active",
    )
    db_session.add(account)

    seen_at = datetime(2025, 11, 1, 0, 31, 53, 268845, tzinfo=timezone.utc)
    bc = TTBBusinessCenter(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        bc_id="7508663838649384976",
        name="SLK_BC",
        timezone="America/Anchorage",
        country_code="US",
        last_seen_at=seen_at,
    )
    db_session.add(bc)

    advertiser = TTBAdvertiser(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        advertiser_id="7492997033645637633",
        bc_id=None,
        name="SLKYD01",
        last_seen_at=seen_at,
    )
    db_session.add(advertiser)

    adv_link = TTBBCAdvertiserLink(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        advertiser_id="7492997033645637633",
        bc_id="7508663838649384976",
        relation_type="OWNER",
        last_seen_at=seen_at,
    )
    db_session.add(adv_link)

    store = TTBStore(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        store_id="7496202240253986992",
        bc_id="7508663838649384976",
        name="Drafyn US",
        store_type="TIKTOK_SHOP",
        store_code="US001",
        store_authorized_bc_id="7508663838649384976",
        last_seen_at=datetime(2025, 11, 1, 0, 31, 54, 941650, tzinfo=timezone.utc),
        raw_json={"store_authorized_bc_id": "7508663838649384976"},
    )
    db_session.add(store)

    store_link = TTBAdvertiserStoreLink(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        advertiser_id="7492997033645637633",
        store_id="7496202240253986992",
        relation_type="AUTHORIZER",
        store_authorized_bc_id="7508663838649384976",
        bc_id_hint="7508663838649384976",
        last_seen_at=datetime(2025, 11, 1, 0, 31, 54, 941650, tzinfo=timezone.utc),
    )
    db_session.add(store_link)

    for resource, rev in (
        ("bc", "bc_rev_1"),
        ("advertiser", "adv_rev_1"),
        ("store", "store_rev_1"),
    ):
        cursor = TTBSyncCursor(
            workspace_id=int(ws.id),
            auth_id=int(account.id),
            provider="tiktok-business",
            resource_type=resource,
            last_rev=rev,
            updated_at=datetime(2025, 11, 1, 0, 31, 55, tzinfo=timezone.utc),
        )
        db_session.add(cursor)

    db_session.commit()


def test_options_returns_payload_and_etag(gmv_app):
    client, _ = gmv_app

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
    )
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag

    data = resp.json()
    assert data["source"] == "db"
    assert data["bcs"][0]["bc_id"] == "7508663838649384976"
    assert data["advertisers"][0]["bc_id"] == "7508663838649384976"
    assert data["stores"][0]["store_id"] == "7496202240253986992"
    assert data["stores"][0]["advertiser_id"] == "7492997033645637633"
    assert data["links"]["bc_to_advertisers"]["7508663838649384976"] == [
        "7492997033645637633"
    ]
    assert data["links"]["advertiser_to_stores"]["7492997033645637633"] == [
        "7496202240253986992"
    ]
    assert data.get("refresh") is None
    assert data["synced_at"] == "2025-11-01T00:31:54.941650+00:00"


def test_options_handles_missing_display_timezone(monkeypatch, gmv_app):
    client, _ = gmv_app

    monkeypatch.setattr(
        "app.services.ttb_meta.advertiser_display_timezone_supported", lambda db: False
    )

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["advertisers"][0]["display_timezone"] is None


def test_options_returns_304_when_etag_matches(gmv_app):
    client, _ = gmv_app

    first = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
    )
    etag = first.headers.get("ETag")
    assert etag

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options",
        headers={"If-None-Match": etag},
    )
    assert resp.status_code == 304
    assert resp.text == ""


def test_refresh_timeout_returns_body(monkeypatch, gmv_app):
    client, _ = gmv_app

    call_args = {}

    def _fake_enqueue_meta_sync(*, workspace_id: int, auth_id: int, now=None):  # noqa: ANN001
        call_args["ws"] = workspace_id
        call_args["auth"] = auth_id
        return MetaSyncEnqueueResult(idempotency_key="test-key", task_name="ttb.sync.all")

    router_module = importlib.import_module("app.features.tenants.ttb.router")
    monkeypatch.setattr(router_module, "enqueue_meta_sync", _fake_enqueue_meta_sync)

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options",
        params={"refresh": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["refresh"] == "timeout"
    assert body["idempotency_key"] == "test-key"
    assert call_args == {"ws": 1, "auth": 1}


def test_refresh_returns_new_data_when_updated(monkeypatch, gmv_app):
    client, _ = gmv_app

    first = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
    )
    initial_etag = first.headers["ETag"]

    router_module = importlib.import_module("app.features.tenants.ttb.router")

    new_state = MetaCursorState(
        revisions={"bc": "bc_rev_new", "advertiser": "adv_rev_new", "store": "store_rev_new"},
        updated_at=datetime(2025, 11, 2, 0, 0, tzinfo=timezone.utc),
    )

    async def _fake_poll(
        db, *, workspace_id, auth_id, initial_state, initial_etag, **_kwargs
    ):  # noqa: ANN001
        return new_state, True

    monkeypatch.setattr(router_module, "_poll_for_meta_refresh", _fake_poll)
    monkeypatch.setattr(
        router_module,
        "enqueue_meta_sync",
        lambda *, workspace_id, auth_id, now=None: MetaSyncEnqueueResult(
            idempotency_key="refresh-key", task_name="ttb.sync.all"
        ),
    )

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options",
        params={"refresh": 1},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "refresh" not in body
    assert "idempotency_key" not in body
    assert resp.headers["ETag"] != initial_etag


def test_enqueue_meta_sync_builds_payload(monkeypatch):
    recorded = {}

    def _fake_send_task(name, kwargs=None, queue=None):  # noqa: ANN001
        recorded["name"] = name
        recorded["kwargs"] = kwargs
        recorded["queue"] = queue

    monkeypatch.setattr("app.celery_app.celery_app.send_task", _fake_send_task)

    ts = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    result = enqueue_meta_sync(workspace_id=2, auth_id=3, now=ts)

    assert result.idempotency_key == "bind-init-meta-2-3-202501010000"
    assert recorded["name"] in {"ttb.sync.meta", "ttb.sync.all"}
    payload = recorded["kwargs"]
    assert payload["workspace_id"] == 2
    assert payload["auth_id"] == 3
    expected_scope = "meta" if recorded["name"] == "ttb.sync.meta" else "all"
    assert payload["scope"] == expected_scope
    envelope = payload["params"]["envelope"]
    assert envelope["meta"]["idempotency_key"] == result.idempotency_key
    from app.core.config import settings

    expected_queue = getattr(settings, "CELERY_DEFAULT_QUEUE", None) or "gmv.tasks.events"
    assert recorded["queue"] == expected_queue


def test_enqueue_meta_sync_falls_back_when_primary_task_fails(monkeypatch):
    calls = []

    def _fake_send_task(name, kwargs=None, queue=None):  # noqa: ANN001
        calls.append((name, queue))
        if len(calls) == 1:
            raise RuntimeError("queue missing")
        return None

    monkeypatch.setattr("app.celery_app.celery_app.send_task", _fake_send_task)

    result = enqueue_meta_sync(workspace_id=5, auth_id=6)

    assert calls[0][0] == "ttb.sync.all"
    assert calls[1][0] == "ttb.sync.meta"
    assert result.task_name == "ttb.sync.meta"


def test_options_supports_legacy_cursor_types(gmv_app):
    client, db = gmv_app

    db.query(TTBSyncCursor).delete()
    for resource, rev in (
        ("bc", "legacy_bc_rev"),
        ("advertisers", "legacy_adv_rev"),
        ("shops", "legacy_store_rev"),
    ):
        db.add(
            TTBSyncCursor(
                workspace_id=1,
                auth_id=1,
                provider="tiktok-business",
                resource_type=resource,
                last_rev=rev,
                updated_at=datetime(2025, 11, 2, 0, 0, tzinfo=timezone.utc),
            )
        )
    db.commit()

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
    )
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag

    resp304 = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options",
        headers={"If-None-Match": etag},
    )
    assert resp304.status_code == 304


def test_options_returns_etag_without_cursors(gmv_app):
    client, db = gmv_app

    db.query(TTBSyncCursor).delete()
    db.commit()

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
    )
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag

    resp304 = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options",
        headers={"If-None-Match": etag},
    )
    assert resp304.status_code == 304


def test_options_links_handle_raw_variants(gmv_app):
    client, db = gmv_app

    bc_id = "8508663838649384976"
    seen_at = datetime(2025, 11, 2, 0, 0, tzinfo=timezone.utc)
    db.add(
        TTBBusinessCenter(
            workspace_id=1,
            auth_id=1,
            bc_id=bc_id,
            name="Legacy BC",
            timezone="UTC",
            country_code="US",
            last_seen_at=seen_at,
        )
    )

    adv_json_id = "8492997033645637633"
    adv_raw_id = "8492997033645637634"
    db.add_all(
        [
            TTBAdvertiser(
                workspace_id=1,
                auth_id=1,
                advertiser_id=adv_json_id,
                bc_id=None,
                name="JsonAdv",
                last_seen_at=seen_at,
            ),
            TTBAdvertiser(
                workspace_id=1,
                auth_id=1,
                advertiser_id=adv_raw_id,
                bc_id=None,
                name="RawAdv",
                last_seen_at=seen_at,
            ),
        ]
    )

    store_json_id = "8496202240253986992"
    store_raw_id = "8496202240253986993"
    db.add_all(
        [
            TTBStore(
                workspace_id=1,
                auth_id=1,
                store_id=store_json_id,
                bc_id=None,
                name="JsonStore",
                last_seen_at=seen_at,
                raw_json={"store_authorized_bc_id": bc_id},
            ),
            TTBStore(
                workspace_id=1,
                auth_id=1,
                store_id=store_raw_id,
                bc_id=None,
                name="RawStore",
                last_seen_at=seen_at,
                raw_json=None,
            ),
        ]
    )
    db.add_all(
        [
            TTBAdvertiserStoreLink(
                workspace_id=1,
                auth_id=1,
                advertiser_id=adv_json_id,
                store_id=store_json_id,
                relation_type="AUTHORIZER",
                store_authorized_bc_id=bc_id,
                bc_id_hint=bc_id,
            ),
            TTBAdvertiserStoreLink(
                workspace_id=1,
                auth_id=1,
                advertiser_id=adv_raw_id,
                store_id=store_raw_id,
                relation_type="PARTNER",
                store_authorized_bc_id=None,
                bc_id_hint=bc_id,
            ),
        ]
    )
    db.add_all(
        [
            TTBBCAdvertiserLink(
                workspace_id=1,
                auth_id=1,
                advertiser_id=adv_json_id,
                bc_id=bc_id,
                relation_type="OWNER",
            ),
            TTBBCAdvertiserLink(
                workspace_id=1,
                auth_id=1,
                advertiser_id=adv_raw_id,
                bc_id=bc_id,
                relation_type="PARTNER",
            ),
        ]
    )
    db.commit()

    raw_payload = {"store_authorized_bc_id": bc_id}

    @event.listens_for(TTBStore, "load")
    def _inject_raw(target, _context):  # pragma: no cover - hook cleanup below
        if target.store_id == store_raw_id:
            setattr(target, "raw", raw_payload)

    try:
        resp = client.get(
            "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/options"
        )
    finally:
        event.remove(TTBStore, "load", _inject_raw)

    assert resp.status_code == 200
    data = resp.json()

    bc_links = data["links"]["bc_to_advertisers"].get(bc_id)
    assert bc_links and adv_json_id in bc_links and adv_raw_id in bc_links

    store_links = data["links"]["advertiser_to_stores"]
    assert store_links.get(adv_json_id) == [store_json_id]
    assert store_links.get(adv_raw_id) == [store_raw_id]

    adv_entries = {item["advertiser_id"]: item for item in data["advertisers"]}
    assert adv_entries[adv_json_id]["bc_id"] == bc_id
    assert adv_entries[adv_raw_id]["bc_id"] == bc_id
