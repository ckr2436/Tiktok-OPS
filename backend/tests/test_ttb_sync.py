import asyncio
import base64
import importlib
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Dict

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
    TTBGmvMaxCampaign,
    TTBGmvMaxCampaignProduct,
)
from app.data.models.scheduling import Schedule, ScheduleRun
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBBindingConfig,
    TTBProduct,
    TTBBCAdvertiserLink,
    TTBAdvertiserStoreLink,
)
# Stub the optional whisper dependency to allow router imports without the package installed.
_dummy_whisper = types.ModuleType("whisper")
_dummy_tokenizer = types.ModuleType("whisper.tokenizer")
_dummy_tokenizer.LANGUAGES = {}
_dummy_tokenizer.TO_LANGUAGE_CODE = {}
_dummy_whisper.tokenizer = _dummy_tokenizer
_dummy_whisper.load_model = lambda name="small": object()
sys.modules.setdefault("whisper", _dummy_whisper)
sys.modules.setdefault("whisper.tokenizer", _dummy_tokenizer)

from app.features.tenants.ttb.router import router as ttb_router
from app.services import ttb_sync
from app.services.policy_engine import PolicyLimits
from app.services.providers.tiktok_business import TiktokBusinessProvider
from app.services.ttb_sync import TTBSyncService

ttb_router_module = importlib.import_module("app.features.tenants.ttb.router")
from app.services.crypto import encrypt_text_to_blob
from app.services.ttb_meta import MetaSyncEnqueueResult
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
        bc_id=None,
        name="Advertiser",
        status="ENABLE",
        currency="USD",
        timezone="Asia/Shanghai",
        country_code="CN",
    )
    db_session.add(advertiser)

    adv_link = TTBBCAdvertiserLink(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        bc_id="BC1",
        advertiser_id="ADV1",
        relation_type="OWNER",
    )
    db_session.add(adv_link)

    store = TTBStore(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        store_id="STORE1",
        bc_id="BC1",
        name="Store",
        store_type="TIKTOK_SHOP",
        store_code="CN001",
        store_authorized_bc_id="BC1",
        region_code="CN",
    )
    db_session.add(store)

    store_link = TTBAdvertiserStoreLink(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        advertiser_id="ADV1",
        store_id="STORE1",
        relation_type="AUTHORIZER",
        store_authorized_bc_id="BC1",
        bc_id_hint="BC1",
    )
    db_session.add(store_link)

    product = TTBProduct(
        workspace_id=int(ws.id),
        auth_id=int(account.id),
        product_id="PROD1",
        store_id="STORE1",
        title="Test Product",
        status="ON_SALE",
        currency="USD",
        price=19.9,
        stock=5,
    )
    db_session.add(product)

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
    resp = client.get("/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/config")
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
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/config",
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
    advertiser = resp.json()["items"][0]
    assert advertiser["advertiser_id"] == "ADV1"
    assert advertiser["currency"]
    assert advertiser["timezone"]
    assert advertiser["country_code"]
    assert advertiser["bc_id"] == "BC1"

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/advertisers",
        params={"owner_bc_id": "BC1"},
    )
    assert resp.status_code == 200
    filtered_advertisers = resp.json()["items"]
    assert filtered_advertisers
    assert all(item["bc_id"] == "BC1" for item in filtered_advertisers)

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/advertisers",
        params={"bc_id": "BC1"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/stores",
        params={"advertiser_id": "ADV1"},
    )
    assert resp.status_code == 200
    store = resp.json()["items"][0]
    assert store["store_id"] == "STORE1"
    assert store["store_type"] == "TIKTOK_SHOP"
    assert store["store_authorized_bc_id"] == "BC1"

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/stores",
        params={"advertiser_id": "ADV1", "owner_bc_id": "BC1"},
    )
    assert resp.status_code == 200
    stores_filtered = resp.json()["items"]
    assert stores_filtered
    assert all(item["bc_id"] == "BC1" for item in stores_filtered)

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/stores",
        params={"advertiser_id": "ADV1", "bc_id": "BC1"},
    )
    assert resp.status_code == 422

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={"store_id": "STORE1"},
    )
    assert resp.status_code == 200
    products = resp.json()
    assert products["total"] >= 1
    assert products["page"] == 1
    assert products["page_size"] == 200
    assert products["items"][0]["product_id"] == "PROD1"
    assert "sku_count" in products["items"][0]
    assert "price_range" in products["items"][0]
    assert "updated_time" in products["items"][0]


def test_list_accounts_triggers_meta_sync_for_incomplete_account(monkeypatch, tenant_app):
    client, db_session = tenant_app

    account = OAuthAccountTTB(
        id=2,
        workspace_id=1,
        provider_app_id=1,
        alias="empty",
        access_token_cipher=encrypt_text_to_blob("token2", key_version=1, aad_text="token2"),
        key_version=1,
        token_fingerprint=b"1" * 32,
        status="active",
    )
    db_session.add(account)
    db_session.commit()

    triggered: list[tuple[int, int]] = []

    def _fake_enqueue_meta_sync(*, workspace_id: int, auth_id: int, now=None):  # noqa: ANN001
        triggered.append((workspace_id, auth_id))
        return MetaSyncEnqueueResult(idempotency_key="auto", task_name="ttb.sync.all")

    monkeypatch.setattr(ttb_router_module, "enqueue_meta_sync", _fake_enqueue_meta_sync)

    resp = client.get("/api/v1/tenants/1/providers/tiktok-business/accounts")
    assert resp.status_code == 200
    assert triggered == [(1, int(account.id))]


def test_store_and_product_filters_require_ids(tenant_app):
    client, _ = tenant_app

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/stores"
    )
    assert resp.status_code == 422

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products"
    )
    assert resp.status_code == 422


def test_account_products_returns_items_and_validates_scope(tenant_app):
    client, _ = tenant_app

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={"store_id": "STORE1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["product_id"] == "PROD1"

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={"store_id": "STORE1", "owner_bc_id": "BC2"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE"

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={"store_id": "STORE1", "advertiser_id": "ADV2"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ADVERTISER_NOT_FOUND"


def test_account_products_requires_link_between_store_and_advertiser(tenant_app):
    client, db_session = tenant_app

    # remove advertiser-store link to trigger validation error
    link = db_session.query(TTBAdvertiserStoreLink).first()
    db_session.delete(link)
    db_session.commit()

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={"store_id": "STORE1", "advertiser_id": "ADV1"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ADVERTISER_STORE_LINK_NOT_FOUND"


def test_product_assignment_respects_enabled_campaigns(tenant_app):
    client, db_session = tenant_app

    product_enabled = TTBProduct(
        workspace_id=1,
        auth_id=1,
        product_id="PROD_ENABLED",
        store_id="STORE1",
        title="Alpha",
        status="ON_SALE",
        currency="USD",
        price=9.9,
    )
    product_disabled = TTBProduct(
        workspace_id=1,
        auth_id=1,
        product_id="PROD_DISABLED",
        store_id="STORE1",
        title="Beta",
        status="ON_SALE",
        currency="USD",
        price=19.9,
    )

    campaign_enabled = TTBGmvMaxCampaign(
        id=100,
        workspace_id=1,
        auth_id=1,
        advertiser_id="ADV1",
        campaign_id="CMP_ENABLED",
        store_id="STORE1",
        status="enable",
    )
    campaign_disabled = TTBGmvMaxCampaign(
        id=101,
        workspace_id=1,
        auth_id=1,
        advertiser_id="ADV1",
        campaign_id="CMP_DISABLED",
        store_id="STORE1",
        status="disable",
    )

    db_session.add_all(
        [product_enabled, product_disabled, campaign_enabled, campaign_disabled]
    )
    db_session.flush()

    db_session.add_all(
        [
            TTBGmvMaxCampaignProduct(
                id=200,
                workspace_id=1,
                auth_id=1,
                campaign_pk=campaign_enabled.id,
                campaign_id=campaign_enabled.campaign_id,
                store_id="STORE1",
                item_group_id=product_enabled.product_id,
            ),
            TTBGmvMaxCampaignProduct(
                id=201,
                workspace_id=1,
                auth_id=1,
                campaign_pk=campaign_disabled.id,
                campaign_id=campaign_disabled.campaign_id,
                store_id="STORE1",
                item_group_id=product_disabled.product_id,
            ),
        ]
    )
    db_session.commit()

    resp = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={"store_id": "STORE1"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    status_map = {item["product_id"]: item["gmv_max_ads_status"] for item in items}

    assert status_map["PROD_ENABLED"] == "OCCUPIED"
    assert status_map["PROD_DISABLED"] is None

def test_legacy_routes_removed(tenant_app):
    client, _ = tenant_app
    resp = client.get("/api/v1/tenants/1/providers/tiktok-business/business-centers")
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("detail") == "Not Found"


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
        json={"scope": "products", "store_id": "STORE1"},
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
            "advertiser_id": "ADV1",
            "store_id": "STORE1",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BC_MISMATCH_BETWEEN_ADVERTISER_AND_STORE"

    store.bc_id = "BC1"
    db_session.add(store)
    db_session.commit()


def test_product_sync_requires_link_between_advertiser_and_store(tenant_app):
    client, db_session = tenant_app

    orphan_adv = TTBAdvertiser(
        workspace_id=1,
        auth_id=1,
        advertiser_id="ADV2",
        bc_id="BC1",
        name="Orphan Advertiser",
        status="ENABLE",
        currency="USD",
        timezone="Asia/Shanghai",
        country_code="CN",
    )
    db_session.add(orphan_adv)
    db_session.commit()

    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={
            "scope": "products",
            "advertiser_id": "ADV2",
            "store_id": "STORE1",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ADVERTISER_NOT_LINKED_TO_STORE"


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
        json={"scope": "products", "advertiser_id": "ADV1", "store_id": "STORE1"},
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
            "advertiser_id": "ADV1",
            "store_id": "STORE1",
            "product_eligibility": "gmv_max",
        },
    )
    assert resp.status_code == 202
    assert dispatched["params"]["advertiser_id"] == "ADV1"
    assert dispatched["params"]["bc_id"] == "BC1"
    assert dispatched["params"]["product_eligibility"] == "gmv_max"
    assert dispatched["params"]["mode"] == "full"
    body = resp.json()
    assert body["idempotency_key"] == "fake-key"


def test_provider_passes_advertiser_id_to_product_sync(monkeypatch, tenant_app):
    _, db_session = tenant_app
    provider = TiktokBusinessProvider()

    recorded: Dict[str, object] = {}

    async def fake_sync_products(
        self, *, page_size, store_id, advertiser_id=None, product_eligibility=None
    ):  # noqa: ANN001
        recorded.update(
            page_size=page_size,
            store_id=store_id,
            advertiser_id=advertiser_id,
            product_eligibility=product_eligibility,
        )
        return {"resource": "products", "fetched": 0, "upserts": 0, "skipped": 0, "cursor": {}}

    class DummyClient:
        async def aclose(self):  # noqa: ANN001
            return None

    monkeypatch.setattr(TTBSyncService, "sync_products", fake_sync_products)
    monkeypatch.setattr(
        TiktokBusinessProvider,
        "_build_client",
        lambda self, db, auth_id, limits: DummyClient(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        TiktokBusinessProvider,
        "_policy_limits",
        lambda self, db, workspace_id, auth_id: PolicyLimits(),  # noqa: ARG005
    )

    async def _run():
        return await provider.run_scope(
            db=db_session,
            envelope={
                "workspace_id": 1,
                "auth_id": 1,
                "options": {
                    "advertiser_id": "ADV1",
                    "store_id": "STORE1",
                    "product_eligibility": "gmv_max",
                    "page_size": 25,
                },
            },
            scope="products",
            logger=logging.getLogger("test"),
        )

    result = asyncio.run(_run())

    assert recorded["advertiser_id"] == "ADV1"
    assert recorded["store_id"] == "STORE1"
    assert recorded["product_eligibility"] == "gmv_max"
    assert result["phases"][0]["stats"]["resource"] == "products"


def test_provider_defaults_product_eligibility(monkeypatch, tenant_app):
    _, db_session = tenant_app
    provider = TiktokBusinessProvider()

    recorded: Dict[str, object] = {}

    async def fake_sync_products(
        self, *, page_size, store_id, advertiser_id=None, product_eligibility=None
    ):  # noqa: ANN001
        recorded.update(
            page_size=page_size,
            store_id=store_id,
            advertiser_id=advertiser_id,
            product_eligibility=product_eligibility,
        )
        return {"resource": "products", "fetched": 0, "upserts": 0, "skipped": 0, "cursor": {}}

    class DummyClient:
        async def aclose(self):  # noqa: ANN001
            return None

    monkeypatch.setattr(TTBSyncService, "sync_products", fake_sync_products)
    monkeypatch.setattr(
        TiktokBusinessProvider,
        "_build_client",
        lambda self, db, auth_id, limits: DummyClient(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        TiktokBusinessProvider,
        "_policy_limits",
        lambda self, db, workspace_id, auth_id: PolicyLimits(),  # noqa: ARG005
    )

    async def _run():
        return await provider.run_scope(
            db=db_session,
            envelope={
                "workspace_id": 1,
                "auth_id": 1,
                "options": {
                    "advertiser_id": "ADV1",
                    "store_id": "STORE1",
                    "page_size": 25,
                },
            },
            scope="products",
            logger=logging.getLogger("test"),
        )

    asyncio.run(_run())

    assert recorded["product_eligibility"] == "gmv_max"


def test_sync_advertisers_hydrates_info(monkeypatch, tenant_app):
    _, db_session = tenant_app

    class DummyClient:
        async def iter_advertisers(self, *, page_size):  # noqa: ANN001
            assert page_size == 50
            yield {"advertiser_id": "ADV1", "version": 1}

        async def fetch_advertiser_info(self, advertiser_ids, fields=None):  # noqa: ANN001
            assert advertiser_ids == ["ADV1"]
            assert fields is not None
            assert "owner_bc_id" in fields
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
