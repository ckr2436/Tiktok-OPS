from __future__ import annotations

import asyncio
import sys
import types
from typing import Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Provide a minimal AESGCM shim so crypto helpers do not require the cryptography package during tests
_aead_module = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")


class _DummyAESGCM:  # pragma: no cover - testing stub
    def __init__(self, key: bytes) -> None:
        self.key = key

    def encrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None = None) -> bytes:
        return data

    def decrypt(self, nonce: bytes, data: bytes, associated_data: bytes | None = None) -> bytes:
        return data


_aead_module.AESGCM = _DummyAESGCM
sys.modules.setdefault("cryptography", types.ModuleType("cryptography"))
sys.modules.setdefault("cryptography.hazmat", types.ModuleType("cryptography.hazmat"))
sys.modules.setdefault("cryptography.hazmat.primitives", types.ModuleType("cryptography.hazmat.primitives"))
sys.modules.setdefault(
    "cryptography.hazmat.primitives.ciphers",
    types.ModuleType("cryptography.hazmat.primitives.ciphers"),
)
sys.modules["cryptography.hazmat.primitives.ciphers.aead"] = _aead_module

from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.data.models import Workspace, TaskCatalog, ScheduleRun, AuditLog, OAuthProviderApp, OAuthAccountTTB
from app.data.models.ttb_entities import TTBBusinessCenter, TTBAdvertiser, TTBShop, TTBProduct, TTBSyncCursor
from app.services.ttb_sync import TTBSyncService
from app.features.tenants.ttb.router import router as ttb_router
from app.features.tenants.oauth_ttb.router import router as oauth_router
from app.features.tenants.oauth_ttb.router_sync import router as deprecated_sync_router
from app.features.tenants.oauth_ttb.router_sync_all import router as deprecated_sync_all_router
from app.features.tenants.oauth_ttb.router_cursors import router as deprecated_cursors_router
from app.features.tenants.oauth_ttb.router_jobs import router as deprecated_jobs_router

SYNC_TASK_NAMES = [
    "ttb.sync.bc",
    "ttb.sync.advertisers",
    "ttb.sync.shops",
    "ttb.sync.products",
    "ttb.sync.all",
]


class FakeTTBClient:
    def __init__(self) -> None:
        self._bcs = [
            {"bc_id": "BC1", "bc_name": "Main", "sync_rev": "1"},
        ]
        self._advertisers = [
            {"advertiser_id": "ADV1", "bc_id": "BC1", "name": "Adv", "sync_rev": "10"},
        ]
        self._shops: Dict[str, List[dict]] = {
            "ADV1": [
                {"shop_id": "SHOP1", "advertiser_id": "ADV1", "bc_id": "BC1", "shop_name": "Shop", "sync_rev": "5"}
            ]
        }
        self._products: Dict[tuple[str, str], List[dict]] = {
            ("BC1", "SHOP1"): [
                {"product_id": "P1", "shop_id": "SHOP1", "title": "Prod", "sync_rev": "100"}
            ]
        }

    async def iter_business_centers(self, limit: int = 200):
        for item in self._bcs:
            yield item

    async def iter_advertisers(self, limit: int = 200, app_id: str | None = None, secret: str | None = None):
        for item in self._advertisers:
            yield item

    async def iter_shops(self, advertiser_id: str, page_size: int = 1000):
        for item in self._shops.get(advertiser_id, []):
            yield item

    async def iter_products(
        self,
        *,
        bc_id: str,
        store_id: str,
        advertiser_id: str | None = None,
        page_size: int = 1000,
    ):
        for item in self._products.get((bc_id, store_id), []):
            yield item

    async def aclose(self) -> None:  # pragma: no cover - compatibility shim
        return None


@pytest.fixture()
def tenant_app(db_session):
    app = FastAPI()
    app.include_router(oauth_router)
    app.include_router(ttb_router)
    app.include_router(deprecated_sync_router)
    app.include_router(deprecated_sync_all_router)
    app.include_router(deprecated_cursors_router)
    app.include_router(deprecated_jobs_router)
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

    with TestClient(app) as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _seed_workspace_and_binding(db_session):
    ws = Workspace(id=1, name="Acme", company_code="1001")
    db_session.add(ws)
    db_session.flush()

    provider = OAuthProviderApp(
        id=1,
        provider="tiktok_business",
        name="Default",
        client_id="client",
        client_secret_cipher=b"secret",
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
        access_token_cipher=b"token",
        key_version=1,
        token_fingerprint=b"f" * 32,
        scope_json=None,
        status="active",
    )
    db_session.add(account)
    db_session.flush()

    for idx, name in enumerate(SYNC_TASK_NAMES, start=1):
        db_session.add(TaskCatalog(id=idx, task_name=name, impl_version=1))
    db_session.commit()
    return ws, account


def test_manual_sync_creates_schedule_run(monkeypatch, tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    calls: list[dict] = []

    class DummyResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    def fake_send_task(name: str, kwargs: dict, queue: str):  # noqa: ANN001
        calls.append({"name": name, "kwargs": kwargs, "queue": queue})
        return DummyResult("task-1")

    monkeypatch.setattr("app.services.ttb_sync_dispatch.celery_app.send_task", fake_send_task)

    resp = client.post(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/sync",
        json={
            "auth_id": account.id,
            "scope": "bc",
            "mode": "incremental",
            "limit": 5,
            "idempotency_key": "abc",
        },
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "enqueued"
    assert data["idempotent"] is False
    assert calls and calls[0]["name"] == "ttb.sync.bc"
    payload = calls[0]["kwargs"]
    assert payload["workspace_id"] == ws.id
    assert payload["auth_id"] == account.id
    assert payload["scope"] == "bc"
    assert payload["run_id"] == data["run_id"]
    assert payload["idempotency_key"]
    envelope = payload["params"]["envelope"]
    assert envelope["provider"] == "tiktok-business"
    assert envelope["scope"] == "bc"
    assert envelope["options"]["limit"] == 5
    assert envelope["options"]["mode"] == "incremental"
    assert envelope["meta"]["run_id"] == data["run_id"]

    run = db_session.get(ScheduleRun, int(data["run_id"]))
    assert run is not None
    assert run.status == "enqueued"
    requested = run.stats_json["requested"]
    assert requested["scope"] == "bc"
    assert requested["provider"] == "tiktok-business"
    assert requested["options"]["mode"] == "incremental"
    assert requested["options"]["limit"] == 5
    assert requested["actor"]["user_id"] == 1
    assert run.stats_json.get("errors") == []
    assert run.idempotency_key

    audit_count = db_session.query(AuditLog).count()
    assert audit_count == 1


def test_manual_sync_idempotent_reuses_run(monkeypatch, tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    class DummyResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    calls: list[dict] = []

    def fake_send_task(name: str, kwargs: dict, queue: str):  # noqa: ANN001
        calls.append({"name": name, "kwargs": kwargs, "queue": queue})
        return DummyResult("task-2")

    monkeypatch.setattr("app.services.ttb_sync_dispatch.celery_app.send_task", fake_send_task)

    body = {
        "auth_id": account.id,
        "scope": "advertisers",
        "mode": "incremental",
        "idempotency_key": "same-key",
    }
    first = client.post(f"/api/v1/tenants/{ws.id}/providers/tiktok-business/sync", json=body)
    assert first.status_code == 202
    second = client.post(f"/api/v1/tenants/{ws.id}/providers/tiktok-business/sync", json=body)
    assert second.status_code == 202

    assert len(calls) == 1
    assert first.json()["idempotent"] is False
    assert second.json()["idempotent"] is True
    assert first.json()["run_id"] == second.json()["run_id"]


def test_binding_sync_defaults_scope_all(monkeypatch, tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    class DummyResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    dispatched: list[dict] = []

    def fake_send_task(name: str, kwargs: dict, queue: str):  # noqa: ANN001
        dispatched.append(kwargs)
        return DummyResult("task-bind")

    monkeypatch.setattr("app.services.ttb_sync_dispatch.celery_app.send_task", fake_send_task)

    resp = client.post(
        f"/api/v1/tenants/{ws.id}/oauth/tiktok-business/bind",
        json={"auth_id": account.id},
    )
    assert resp.status_code == 202
    payload = dispatched[0]
    envelope = payload["params"]["envelope"]
    assert envelope["scope"] == "all"
    assert envelope["options"]["mode"] == "full"
    assert resp.json()["task_name"] == "ttb.sync.all"


def test_list_provider_accounts(tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    resp = client.get(f"/api/v1/tenants/{ws.id}/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["provider"] == "tiktok-business"
    assert body["items"][0]["auth_id"] == account.id
    assert body["items"][0]["status"] == "active"


def test_read_business_centers_endpoint(tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    bc = TTBBusinessCenter(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        bc_id="BC1",
        name="Main",
    )
    db_session.add(bc)
    db_session.commit()

    resp = client.get(f"/api/v1/tenants/{ws.id}/providers/tiktok-business/business-centers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["bc_id"] == "BC1"


def test_deprecated_routes_return_410(tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    resp_old_sync = client.post(
        f"/api/v1/tenants/{ws.id}/ttb/sync",
        json={"auth_id": account.id, "scope": "bc"},
    )
    assert resp_old_sync.status_code == 410

    resp_old_oauth = client.post(
        f"/api/v1/tenants/{ws.id}/oauth/ttb/bindings/{account.id}/sync",
        json={},
    )
    assert resp_old_oauth.status_code == 410


def test_ttbsyncservice_idempotent(db_session):
    ws, account = _seed_workspace_and_binding(db_session)
    svc = TTBSyncService(db_session, FakeTTBClient(), workspace_id=int(ws.id), auth_id=int(account.id))
    stats = asyncio.run(svc.sync_all())
    assert stats["bc"]["upserts"] == 1
    assert db_session.query(TTBBusinessCenter).count() == 1
    assert db_session.query(TTBAdvertiser).count() == 1
    assert db_session.query(TTBShop).count() == 1
    assert db_session.query(TTBProduct).count() == 1

    stats2 = asyncio.run(svc.sync_all())
    assert stats2["bc"]["upserts"] == 1
    assert db_session.query(TTBBusinessCenter).count() == 1

    cursors = db_session.query(TTBSyncCursor).all()
    assert {c.resource_type for c in cursors} == {"bc", "advertiser", "shop", "product"}
    assert all(c.last_rev for c in cursors)
