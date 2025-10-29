from __future__ import annotations

import asyncio
import threading
import logging
import time
import sys
import types
from datetime import datetime, timezone
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
from app.core.config import settings
from app.data.models import Workspace, TaskCatalog, ScheduleRun, AuditLog, OAuthProviderApp, OAuthAccountTTB
from app.data.models.scheduling import Schedule
from app.data.models.ttb_entities import TTBBusinessCenter, TTBAdvertiser, TTBShop, TTBProduct, TTBSyncCursor
from app.services.ttb_sync import TTBSyncService
from app.services.provider_registry import provider_registry
from app.features.tenants.ttb.router import router as ttb_router
from app.features.tenants.oauth_ttb.router import router as oauth_router
from app.features.tenants.oauth_ttb.router_sync import router as deprecated_sync_router
from app.features.tenants.oauth_ttb.router_sync_all import router as deprecated_sync_all_router
from app.features.tenants.oauth_ttb.router_cursors import router as deprecated_cursors_router
from app.features.tenants.oauth_ttb.router_jobs import router as deprecated_jobs_router
from app.services.redis_locks import RedisDistributedLock, _RELEASE_SCRIPT, _REFRESH_SCRIPT
from app.services import redis_client
from app.tasks.ttb_sync_tasks import _execute_task

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


class _InMemoryAsyncRedis:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._expiry: dict[str, float | None] = {}
        self._lock = threading.Lock()
        self._now = 0.0

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += float(seconds)
            self._cleanup_locked()

    def _cleanup_locked(self) -> None:
        expired = [key for key, expiry in self._expiry.items() if expiry is not None and expiry <= self._now]
        for key in expired:
            self._store.pop(key, None)
            self._expiry.pop(key, None)

    @staticmethod
    def _normalize_key(key: object) -> str:
        if isinstance(key, bytes):
            return key.decode()
        return str(key)

    @staticmethod
    def _normalize_value(value: object) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return str(value).encode()

    @staticmethod
    def _normalize_script(script: object) -> str:
        return "\n".join(line.strip() for line in str(script).strip().splitlines())

    async def set(self, key, value, nx=False, ex=None, px=None):  # noqa: ANN001
        normalized_key = self._normalize_key(key)
        normalized_value = self._normalize_value(value)
        ttl = None
        if px is not None:
            ttl = float(px) / 1000.0
        elif ex is not None:
            ttl = float(ex)
        with self._lock:
            self._cleanup_locked()
            if nx and normalized_key in self._store:
                return False
            self._store[normalized_key] = normalized_value
            self._expiry[normalized_key] = self._now + ttl if ttl is not None else None
        return True

    async def eval(self, script, numkeys, *keys_and_args):  # noqa: ANN001
        normalized_script = self._normalize_script(script)
        release_script = self._normalize_script(_RELEASE_SCRIPT)
        refresh_script = self._normalize_script(_REFRESH_SCRIPT)
        key = self._normalize_key(keys_and_args[0]) if keys_and_args else ""
        owner = self._normalize_value(keys_and_args[1]) if len(keys_and_args) > 1 else b""
        ttl_ms = int(keys_and_args[2]) if len(keys_and_args) > 2 else 0
        with self._lock:
            self._cleanup_locked()
            current = self._store.get(key)
            if normalized_script == release_script:
                if current == owner:
                    self._store.pop(key, None)
                    self._expiry.pop(key, None)
                    return 1
                return 0
            if normalized_script == refresh_script:
                if current == owner:
                    self._expiry[key] = self._now + (ttl_ms / 1000.0)
                    return 1
                return 0
        raise NotImplementedError(normalized_script)

    async def pexpire(self, key, ttl_ms):  # noqa: ANN001
        normalized_key = self._normalize_key(key)
        with self._lock:
            self._cleanup_locked()
            if normalized_key not in self._store:
                return 0
            self._expiry[normalized_key] = self._now + (int(ttl_ms) / 1000.0)
            return 1

    async def get(self, key):  # noqa: ANN001
        normalized_key = self._normalize_key(key)
        with self._lock:
            self._cleanup_locked()
            return self._store.get(normalized_key)

    async def exists(self, key):  # noqa: ANN001
        normalized_key = self._normalize_key(key)
        with self._lock:
            self._cleanup_locked()
            return 1 if normalized_key in self._store else 0

    async def ttl(self, key):  # noqa: ANN001
        normalized_key = self._normalize_key(key)
        with self._lock:
            self._cleanup_locked()
            if normalized_key not in self._store:
                return -2
            expiry = self._expiry.get(normalized_key)
            if expiry is None:
                return -1
            remaining = max(expiry - self._now, 0)
            return int(remaining)

    async def close(self) -> None:  # pragma: no cover - interface compatibility
        return None


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _InMemoryAsyncRedis()
    monkeypatch.setattr(redis_client, "_redis", fake, raising=False)
    monkeypatch.setattr(settings, "TTB_SYNC_USE_DB_LOCKS", False)
    yield fake
    asyncio.run(fake.close())
    monkeypatch.setattr(redis_client, "_redis", None, raising=False)


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
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{account.id}/sync",
        json={
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
    assert requested["auth_id"] == int(account.id)
    assert requested["workspace_id"] == int(ws.id)
    assert requested["lock_env"] == settings.LOCK_ENV
    assert requested["run_id"] == int(data["run_id"])
    assert requested["idempotency_key"] == payload["idempotency_key"]
    assert requested["options"]["mode"] == "incremental"
    assert requested["options"]["limit"] == 5
    assert requested["actor"]["user_id"] == 1
    assert run.stats_json.get("errors") == []
    assert run.idempotency_key

    run_detail = client.get(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{account.id}/sync-runs/{data['run_id']}"
    )
    assert run_detail.status_code == 200
    run_payload = run_detail.json()
    assert run_payload["status"] in {"enqueued", "running", "success", "failed", "partial"}
    assert run_payload["stats"]["requested"]["auth_id"] == int(account.id)
    assert run_payload["stats"]["requested"]["provider"] == "tiktok-business"
    assert run_payload["stats"]["requested"]["workspace_id"] == int(ws.id)
    assert run_payload["stats"]["requested"]["lock_env"] == settings.LOCK_ENV

    audit_count = db_session.query(AuditLog).count()
    assert audit_count == 1


def test_sync_run_lookup_requires_matching_auth(monkeypatch, tenant_app):
    client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    other = OAuthAccountTTB(
        id=account.id + 1,
        workspace_id=int(ws.id),
        provider_app_id=account.provider_app_id,
        alias="other",
        access_token_cipher=b"token2",
        key_version=1,
        token_fingerprint=b"g" * 32,
        scope_json=None,
        status="active",
    )
    db_session.add(other)
    db_session.commit()

    class DummyResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    def fake_send_task(name: str, kwargs: dict, queue: str):  # noqa: ANN001
        return DummyResult("task-lookup")

    monkeypatch.setattr("app.services.ttb_sync_dispatch.celery_app.send_task", fake_send_task)

    trigger = client.post(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{account.id}/sync",
        json={"scope": "bc", "mode": "incremental"},
    )
    assert trigger.status_code == 202
    run_id = trigger.json()["run_id"]

    ok = client.get(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{account.id}/sync-runs/{run_id}"
    )
    assert ok.status_code == 200

    mismatch = client.get(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{other.id}/sync-runs/{run_id}"
    )
    assert mismatch.status_code == 404


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
        "scope": "advertisers",
        "mode": "incremental",
        "idempotency_key": "same-key",
    }
    first = client.post(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{account.id}/sync",
        json=body,
    )
    assert first.status_code == 202
    second = client.post(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts/{account.id}/sync",
        json=body,
    )
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

    accounts = client.get(
        f"/api/v1/tenants/{ws.id}/providers/tiktok-business/accounts"
    )
    assert accounts.status_code == 200
    acc_body = accounts.json()
    assert acc_body["total"] == 1
    assert acc_body["items"][0]["auth_id"] == account.id


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


def _create_schedule_run(db_session, ws, scope: str, auth_id: int, *, idempotency_key: str) -> ScheduleRun:
    schedule = Schedule(
        workspace_id=int(ws.id),
        task_name=f"ttb.sync.{scope}",
        schedule_type="oneoff",
        params_json={},
        timezone="UTC",
        enabled=False,
    )
    db_session.add(schedule)
    db_session.flush()

    run = ScheduleRun(
        schedule_id=int(schedule.id),
        workspace_id=int(ws.id),
        scheduled_for=datetime.now(timezone.utc),
        enqueued_at=datetime.now(timezone.utc),
        status="enqueued",
        idempotency_key=idempotency_key,
        stats_json={
            "requested": {
                "provider": "tiktok-business",
                "auth_id": int(auth_id),
                "scope": scope,
                "idempotency_key": idempotency_key,
            },
            "errors": [],
        },
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_schedule_run_status_accepts_expected_values(db_session):
    ws, account = _seed_workspace_and_binding(db_session)
    run = _create_schedule_run(db_session, ws, "bc", int(account.id), idempotency_key="enum-check")

    for value in ["enqueued", "running", "success", "failed", "partial"]:
        run.status = value
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        assert run.status == value


def _run_sync_task(ws, account, run: ScheduleRun, scope: str, request_id: str):
    task = types.SimpleNamespace(
        request=types.SimpleNamespace(id=request_id, retries=0),
        name=f"ttb.sync.{scope}",
    )
    envelope = {
        "envelope": {
            "envelope_version": 1,
            "provider": "tiktok-business",
            "scope": scope,
            "workspace_id": int(ws.id),
            "auth_id": int(account.id),
            "options": {},
            "meta": {
                "run_id": int(run.id),
                "schedule_id": int(run.schedule_id),
                "idempotency_key": run.idempotency_key,
            },
        }
    }
    return _execute_task(
        task,
        expected_scope=scope,
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        scope=scope,
        params=envelope,
        run_id=int(run.id),
        idempotency_key=run.idempotency_key,
    )


class _DummyProviderHandler:
    provider_id = "tiktok-business"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    def validate_options(self, *, scope: str, options: dict) -> dict:  # noqa: ANN001
        return options

    async def run_scope(self, *, db, envelope: dict, scope: str, logger):  # noqa: ANN001
        await asyncio.sleep(self.delay)
        return {
            "phases": [
                {
                    "scope": scope,
                    "stats": {"fetched": 0, "upserts": 0, "skipped": 0},
                    "duration_ms": int(self.delay * 1000),
                }
            ],
            "errors": [],
        }


def test_redis_lock_prevents_concurrent_runs(monkeypatch, tenant_app, fake_redis):
    _client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    handler = _DummyProviderHandler(delay=0.2)
    monkeypatch.setitem(provider_registry._handlers, "tiktok-business", handler)

    run1 = _create_schedule_run(db_session, ws, "bc", int(account.id), idempotency_key="run-1")
    run2 = _create_schedule_run(db_session, ws, "bc", int(account.id), idempotency_key="run-2")

    key = (
        f"{settings.TTB_SYNC_LOCK_PREFIX}{settings.LOCK_ENV}:sync:"
        f"tiktok-business:{ws.id}:{account.id}"
    )

    results: list[dict] = []

    thread = threading.Thread(target=lambda: results.append(_run_sync_task(ws, account, run1, "bc", "task-1")))
    thread.start()

    for _ in range(200):
        if asyncio.run(fake_redis.exists(key)):
            break
        time.sleep(0.01)
    else:  # pragma: no cover - defensive
        thread.join(timeout=1)
        pytest.fail("lock not acquired in time")

    ttl_before = asyncio.run(fake_redis.ttl(key))
    assert 0 < ttl_before <= settings.TTB_SYNC_LOCK_TTL_SECONDS

    conflict = _run_sync_task(ws, account, run2, "bc", "task-2")
    thread.join()

    db_session.expire_all()
    run1_db = db_session.get(ScheduleRun, int(run1.id))
    run2_db = db_session.get(ScheduleRun, int(run2.id))

    assert results and results[0]["status"] == "success"
    assert conflict == {"error": "another sync job running for this binding"}
    assert run1_db and run1_db.status == "success"
    assert run2_db and run2_db.status == "failed"
    assert run2_db.error_code == "lock_not_acquired"
    error_payload = run2_db.stats_json["errors"][0]
    assert error_payload["code"] == "lock_not_acquired"
    assert error_payload["stage"] == "bc"
    assert error_payload["lock_key"] == key
    assert run2_db.stats_json["requested"]["run_id"] == int(run2.id)
    assert run2_db.stats_json["requested"]["workspace_id"] == int(ws.id)
    assert run2_db.stats_json["requested"]["lock_env"] == settings.LOCK_ENV
    assert run2_db.stats_json["processed"]["summary"]["skipped"] is True
    assert asyncio.run(fake_redis.ttl(key)) == -2
    assert asyncio.run(fake_redis.exists(key)) == 0


def test_redis_lock_adjusts_misconfigured_heartbeat(monkeypatch, tenant_app, fake_redis, caplog):
    _client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    handler = _DummyProviderHandler(delay=0.05)
    monkeypatch.setitem(provider_registry._handlers, "tiktok-business", handler)

    run = _create_schedule_run(db_session, ws, "bc", int(account.id), idempotency_key="hb-check")

    monkeypatch.setattr(
        settings,
        "TTB_SYNC_LOCK_HEARTBEAT_SECONDS",
        settings.TTB_SYNC_LOCK_TTL_SECONDS,
    )

    caplog.set_level(logging.WARNING)
    result = _run_sync_task(ws, account, run, "bc", "task-hb")

    assert result["status"] == "success"
    assert any("heartbeat interval" in record.message for record in caplog.records)

    demo_lock = RedisDistributedLock(
        key=(
            f"{settings.TTB_SYNC_LOCK_PREFIX}{settings.LOCK_ENV}:sync:"
            f"test:{ws.id}:{account.id}"
        ),
        owner_token="demo",
        ttl_seconds=30,
        heartbeat_interval=30,
    )
    assert demo_lock.heartbeat_interval < demo_lock.ttl_seconds


def test_redis_lock_expires_after_ttl(fake_redis):
    key = (
        f"{settings.TTB_SYNC_LOCK_PREFIX}{settings.LOCK_ENV}:sync:"
        "tiktok-business:42:24"
    )
    primary = RedisDistributedLock(key=key, owner_token="owner-1", ttl_seconds=5, heartbeat_interval=0)
    assert primary.acquire()

    ttl_primary = asyncio.run(fake_redis.ttl(key))
    assert 0 < ttl_primary <= 5

    contender = RedisDistributedLock(key=key, owner_token="owner-2", ttl_seconds=5, heartbeat_interval=0)
    assert contender.acquire() is False

    primary.force_stop()
    fake_redis.advance(6)

    assert asyncio.run(fake_redis.ttl(key)) == -2

    retry = RedisDistributedLock(key=key, owner_token="owner-3", ttl_seconds=5, heartbeat_interval=0)
    assert retry.acquire()
    assert retry.release()
    assert asyncio.run(fake_redis.ttl(key)) == -2


def test_redis_lock_rejects_non_owner_release(fake_redis):
    key = (
        f"{settings.TTB_SYNC_LOCK_PREFIX}{settings.LOCK_ENV}:sync:"
        "tiktok-business:88:99"
    )
    holder = RedisDistributedLock(key=key, owner_token="owner-primary", ttl_seconds=30, heartbeat_interval=0)
    assert holder.acquire()

    ttl_holder = asyncio.run(fake_redis.ttl(key))
    assert 0 < ttl_holder <= 30

    wrong = asyncio.run(fake_redis.eval(_RELEASE_SCRIPT, 1, key, "owner-other"))
    assert wrong == 0
    assert asyncio.run(fake_redis.get(key)) == b"owner-primary"

    assert holder.release()
    assert asyncio.run(fake_redis.exists(key)) == 0
    assert asyncio.run(fake_redis.ttl(key)) == -2


def test_sync_lock_shared_across_scopes(monkeypatch, tenant_app, fake_redis):
    _client, db_session = tenant_app
    ws, account = _seed_workspace_and_binding(db_session)

    handler = _DummyProviderHandler(delay=0.2)
    monkeypatch.setitem(provider_registry._handlers, "tiktok-business", handler)

    run_bc = _create_schedule_run(db_session, ws, "bc", int(account.id), idempotency_key="run-bc")
    run_all = _create_schedule_run(db_session, ws, "all", int(account.id), idempotency_key="run-all")

    key = (
        f"{settings.TTB_SYNC_LOCK_PREFIX}{settings.LOCK_ENV}:sync:"
        f"tiktok-business:{ws.id}:{account.id}"
    )

    results: list[dict] = []
    thread = threading.Thread(target=lambda: results.append(_run_sync_task(ws, account, run_bc, "bc", "task-bc")))
    thread.start()

    for _ in range(200):
        if asyncio.run(fake_redis.exists(key)):
            break
        time.sleep(0.01)
    else:  # pragma: no cover
        thread.join(timeout=1)
        pytest.fail("lock not acquired for bc scope")

    ttl_before = asyncio.run(fake_redis.ttl(key))
    assert 0 < ttl_before <= settings.TTB_SYNC_LOCK_TTL_SECONDS

    conflict = _run_sync_task(ws, account, run_all, "all", "task-all")
    thread.join()

    db_session.expire_all()
    run_bc_db = db_session.get(ScheduleRun, int(run_bc.id))
    run_all_db = db_session.get(ScheduleRun, int(run_all.id))

    assert results and results[0]["status"] == "success"
    assert conflict == {"error": "another sync job running for this binding"}
    assert run_bc_db and run_bc_db.status == "success"
    assert run_all_db and run_all_db.status == "failed"
    assert run_all_db.error_code == "lock_not_acquired"
    conflict_error = run_all_db.stats_json["errors"][0]
    assert conflict_error["code"] == "lock_not_acquired"
    assert conflict_error["stage"] == "all"
    assert conflict_error["lock_key"] == key
    assert run_all_db.stats_json["requested"]["run_id"] == int(run_all.id)
    assert run_all_db.stats_json["requested"]["workspace_id"] == int(ws.id)
    assert run_all_db.stats_json["requested"]["lock_env"] == settings.LOCK_ENV
    assert run_all_db.stats_json["processed"]["summary"]["skipped"] is True
    assert asyncio.run(fake_redis.ttl(key)) == -2
    assert asyncio.run(fake_redis.exists(key)) == 0


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
