import asyncio
import base64
from datetime import datetime, timedelta, timezone
from typing import Dict

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.data.models import (
    Workspace,
    TaskCatalog,
    OAuthProviderApp,
    OAuthAccountTTB,
)
from app.data.models.scheduling import Schedule, ScheduleRun
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBBindingConfig,
)
from app.features.tenants.ttb.router import router as ttb_router
from app.services import ttb_sync
from app.services.ttb_sync import TTBSyncService
from app.services import oauth_ttb as oauth_ttb_service

ttb_router_module = importlib.import_module("app.features.tenants.ttb.router")
from app.services.crypto import encrypt_text_to_blob
from app.services.ttb_sync_dispatch import DispatchResult


@pytest.fixture()
def tenant_app(db_session):
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)

    dummy_user = SessionUser(
        id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="U001",
        is_platform_admin=False,
        workspace_id=1,
        role="admin",
        is_active=True,
    )

    def _admin_override(workspace_id: int):  # noqa: ANN001
        return dummy_user

    def _member_override(workspace_id: int):  # noqa: ANN001
        return dummy_user

    app.dependency_overrides[require_tenant_admin] = _admin_override
    app.dependency_overrides[require_tenant_member] = _member_override

    _seed_data(db_session)

    with TestClient(app) as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _seed_data(db_session) -> None:
    if not getattr(settings, "CRYPTO_MASTER_KEY_B64", ""):
        settings.CRYPTO_MASTER_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode()

    ws = Workspace(id=1, name="Acme", company_code="1001")
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

    bc = TTBBusinessCenter(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        bc_id="BC1",
        name="Main",
    )
    db_session.add(bc)

    advertiser = TTBAdvertiser(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        advertiser_id="ADV1",
        bc_id="BC1",
        name="Advertiser",
    )
    db_session.add(advertiser)

    store = TTBStore(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        store_id="STORE1",
        advertiser_id="ADV1",
        bc_id="BC1",
        name="Store",
    )
    db_session.add(store)

    task = TaskCatalog(
        task_name="ttb.sync.products",
        impl_version=1,
        visibility="tenant",
        is_enabled=True,
    )
    db_session.merge(task)

    db_session.commit()


def test_get_binding_config_returns_default(tenant_app):
    client, _ = tenant_app
    resp = client.get("/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bc_id"] is None
    assert data["auto_sync_products"] is False


def test_update_binding_config_success(tenant_app):
    client, db_session = tenant_app
    payload = {
        "bc_id": "BC1",
        "advertiser_id": "ADV1",
        "store_id": "STORE1",
        "auto_sync_products": True,
    }
    resp = client.put(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmv-max/config",
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["bc_id"] == "BC1"
    assert data["auto_sync_products"] is True
    config = db_session.query(TTBBindingConfig).filter_by(workspace_id=1, auth_id=1).one()
    assert config.auto_sync_products is True


def test_metadata_endpoints_return_items(tenant_app):
    client, _ = tenant_app
    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/business-centers"
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["bc_id"] == "BC1"

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/advertisers"
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["advertiser_id"] == "ADV1"

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/stores"
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["store_id"] == "STORE1"


def test_legacy_routes_return_410(tenant_app):
    client, _ = tenant_app
    resp = client.get("/api/v1/tenants/1/providers/tiktok-business/business-centers")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "TTB_LEGACY_DISABLED"


def test_meta_sync_returns_summary(monkeypatch, tenant_app):
    client, _ = tenant_app

    def fake_meta_sync(db, *, workspace_id, auth_id, page_size=200):  # noqa: ANN001
        return {
            "bc": {"added": 1, "removed": 0, "unchanged": 0},
            "advertisers": {"added": 0, "removed": 0, "unchanged": 1},
            "stores": {"added": 0, "removed": 0, "unchanged": 1},
        }

    monkeypatch.setattr(ttb_router_module, "_perform_meta_sync", fake_meta_sync)

    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={"scope": "meta"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["bc"]["added"] == 1
    assert data["run_id"] is None


def test_product_sync_missing_advertiser(tenant_app):
    client, _ = tenant_app
    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={"scope": "products", "options": {"store_id": "STORE1"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ADVERTISER_REQUIRED_FOR_GMV_MAX"


def test_product_sync_bc_mismatch(tenant_app):
    client, db_session = tenant_app
    store = db_session.query(TTBStore).first()
    store.bc_id = "BC2"
    db_session.add(store)
    db_session.commit()

    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={
            "scope": "products",
            "options": {"advertiser_id": "ADV1", "store_id": "STORE1"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE"

    store.bc_id = "BC1"
    db_session.add(store)
    db_session.commit()


def test_product_sync_rate_limited(monkeypatch, tenant_app):
    client, db_session = tenant_app
    schedule = Schedule(
        workspace_id=1,
        task_name="ttb.sync.products",
        schedule_type="oneoff",
        params_json={
            "provider": "tiktok-business",
            "auth_id": 1,
            "scope": "products",
            "options": {
                "advertiser_id": "ADV1",
                "store_id": "STORE1",
                "product_eligibility": "gmv_max",
            },
        },
        timezone="UTC",
        enabled=False,
    )
    db_session.add(schedule)
    db_session.flush()
    run = ScheduleRun(
        schedule_id=int(schedule.id),
        workspace_id=1,
        scheduled_for=datetime.now(timezone.utc),
        status="success",
        idempotency_key="rate-limit-test",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()

    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={"scope": "products", "options": {"advertiser_id": "ADV1", "store_id": "STORE1"}},
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "SYNC_RATE_LIMITED"


def test_product_sync_dispatch(monkeypatch, tenant_app):
    client, _ = tenant_app

    dispatched: Dict[str, Dict] = {}

    def fake_dispatch(db, **kwargs):  # noqa: ANN001
        dispatched.update(kwargs)

        class _Run:
            id = 123
            schedule_id = 456
            idempotency_key = "fake-key"

        return DispatchResult(run=_Run(), task_id="task", status="enqueued", idempotent=False)

    monkeypatch.setattr(ttb_router_module, "dispatch_sync", fake_dispatch)

    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={
            "scope": "products",
            "mode": "full",
            "options": {
                "advertiser_id": "ADV1",
                "store_id": "STORE1",
                "eligibility": "gmv_max",
            },
        },
    )
    assert resp.status_code == 202
    assert dispatched["params"]["advertiser_id"] == "ADV1"
    assert dispatched["params"]["bc_id"] == "BC1"
    assert dispatched["params"]["product_eligibility"] == "gmv_max"
    assert dispatched["params"]["mode"] == "full"
    body = resp.json()
    assert body["idempotency_key"] == "fake-key"


def test_sync_advertisers_hydrates_info(monkeypatch, tenant_app):
    _, db_session = tenant_app

    class DummyClient:
        async def iter_advertisers(self, *, app_id, secret, page_size):  # noqa: ANN001
            yield {"advertiser_id": "ADV1", "version": 1}

        async def fetch_advertiser_info(self, advertiser_ids):  # noqa: ANN001
            assert advertiser_ids == ["ADV1"]
            return [
                {
                    "advertiser_id": "ADV1",
                    "advertiser_name": "Hydrated",
                    "display_name": "Hydrated Display",
                    "status": "ENABLE",
                    "industry": "ECOM",
                    "currency": "USD",
                    "timezone": "Asia/Shanghai",
                    "display_timezone": "UTC+08:00",
                    "country_code": "CN",
                    "owner_bc_id": "BC-HYDRATED",
                }
            ]

    monkeypatch.setattr(
        oauth_ttb_service,
        "get_credentials_for_auth_id",
        lambda db, auth_id: ("app", "secret", "https://example.com/callback"),
    )
    monkeypatch.setattr(
        "app.services.ttb_sync.get_credentials_for_auth_id",
        lambda db, auth_id: ("app", "secret", "https://example.com/callback"),
    )

    service = TTBSyncService(db_session, DummyClient(), workspace_id=1, auth_id=1)

    async def _run() -> None:
        stats = await service.sync_advertisers(page_size=10)
        assert stats["info_batches"] == 1
        assert stats["info_updates"] == 1

    asyncio.run(_run())

    advertiser = (
        db_session.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == 1, TTBAdvertiser.auth_id == 1, TTBAdvertiser.advertiser_id == "ADV1")
        .one()
    )
    assert advertiser.bc_id == "BC-HYDRATED"
    assert advertiser.display_timezone == "UTC+08:00"
    assert advertiser.currency == "USD"
    assert advertiser.raw_json["advertiser_name"] == "Hydrated"


def test_upsert_adv_without_display_timezone_support(monkeypatch):
    captured: dict[str, object] = {}

    def fake_upsert(db, model, values, conflict_columns, update_columns):  # noqa: ANN001
        captured["values"] = values
        captured["update_columns"] = update_columns
        return True

    monkeypatch.setattr(ttb_sync, "_upsert", fake_upsert)
    monkeypatch.setattr(ttb_sync, "advertiser_display_timezone_supported", lambda db: False)

    assert ttb_sync._upsert_adv(object(), workspace_id=1, auth_id=2, item={"advertiser_id": "ADV-1"})
    assert "display_timezone" not in captured["values"]
    assert "display_timezone" not in captured["update_columns"]


def test_apply_advertiser_info_skips_display_timezone_when_unsupported():
    row = TTBAdvertiser(workspace_id=1, auth_id=1, advertiser_id="ADV-1")

    changed = ttb_sync._apply_advertiser_info(
        row,
        {
            "advertiser_id": "ADV-1",
            "display_timezone": "UTC+08:00",
            "name": "Example",
        },
        allow_display_timezone=False,
    )

    assert changed is True
    assert row.name == "Example"
    assert row.display_timezone is None
