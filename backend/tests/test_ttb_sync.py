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
)
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.data.models.scheduling import Schedule, ScheduleRun
from app.data.models.ttb_entities import (
    TTBBusinessCenter,
    TTBAdvertiser,
    TTBStore,
    TTBBindingConfig,
    TTBProduct,
    TTBProductAdvertiserEligibility,
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

ttb_sync_router_module = importlib.import_module("app.features.tenants.ttb.router.sync")
from app.services.crypto import encrypt_text_to_blob
from app.services import ttb_meta
from app.services.ttb_sync_dispatch import DispatchResult
from app.tasks import ttb_sync_tasks


def test_product_eligibility_uses_official_shopping_ads_enum():
    assert ttb_sync._eligibility_to_api("gmv_max") == "GMV_MAX"
    assert ttb_sync._eligibility_to_api("ads") == "CUSTOM_SHOP_ADS"
    assert ttb_sync._eligibility_to_api("all") is None


def test_sync_task_never_reports_provider_errors_as_success():
    assert ttb_sync_tasks._completion_status([]) == "success"
    assert (
        ttb_sync_tasks._completion_status(
            [{"stage": "products", "code": "PAGINATION_METADATA_CONFLICT"}]
        )
        == "partial"
    )


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


def _create_workspace_and_auth(db_session):
    if not getattr(settings, "CRYPTO_MASTER_KEY_B64", ""):
        settings.CRYPTO_MASTER_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode()

    workspace = Workspace(id=1, name="Acme", company_code="1001")
    db_session.add(workspace)
    db_session.flush()

    provider = OAuthProviderApp(
        id=1,
        provider="tiktok_business",
        name="Default",
        client_id="client",
        client_secret_cipher=encrypt_text_to_blob("secret", key_version=1, aad_text="secret"),
        client_secret_key_version=1,
        redirect_uri="https://example.com/callback",
        is_enabled=True,
    )
    db_session.add(provider)
    db_session.flush()

    account = OAuthAccountTTB(
        id=1,
        workspace_id=int(workspace.id),
        provider_app_id=int(provider.id),
        alias="binding",
        access_token_cipher=encrypt_text_to_blob("token", key_version=1, aad_text="token"),
        key_version=1,
        token_fingerprint=b"1" * 32,
        status="active",
    )
    db_session.add(account)
    db_session.flush()

    return workspace, account


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


def test_list_accounts_is_read_only_for_incomplete_account(monkeypatch, tenant_app):
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

    def _unexpected_enqueue(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("account listing must not enqueue a metadata sync")

    monkeypatch.setattr(ttb_meta, "enqueue_meta_sync", _unexpected_enqueue)

    resp = client.get("/api/v1/tenants/1/providers/tiktok-business/accounts")
    assert resp.status_code == 200
    assert any(item["auth_id"] == int(account.id) for item in resp.json()["items"])


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

    campaign_enabled = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=1,
        advertiser_id="ADV1",
        campaign_id="CMP_ENABLED",
        store_id="STORE1",
        operation_status="ENABLE",
    )
    campaign_disabled = GmvmaxProductCampaignCatalog(
        workspace_id=1,
        auth_id=1,
        advertiser_id="ADV1",
        campaign_id="CMP_DISABLED",
        store_id="STORE1",
        operation_status="DISABLE",
    )

    db_session.add_all(
        [product_enabled, product_disabled, campaign_enabled, campaign_disabled]
    )
    db_session.flush()

    db_session.add_all(
        [
            GmvmaxProductCampaignItemGroup(
                workspace_id=1,
                auth_id=1,
                advertiser_id="ADV1",
                campaign_id=campaign_enabled.campaign_id,
                store_id="STORE1",
                item_group_id=product_enabled.product_id,
            ),
            GmvmaxProductCampaignItemGroup(
                workspace_id=1,
                auth_id=1,
                advertiser_id="ADV1",
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

    def fake_dispatch(db, **kwargs):  # noqa: ANN001
        class _Run:
            id = 123
            schedule_id = 456
            idempotency_key = "meta-key"
            stats_json = {
                "processed": {
                    "summary": {
                        "bc": {"added": 1, "removed": 0, "unchanged": 0},
                        "advertisers": {"added": 0, "removed": 0, "unchanged": 1},
                        "stores": {"added": 0, "removed": 0, "unchanged": 1},
                    }
                }
            }

        return DispatchResult(run=_Run(), task_id="task", status="enqueued", idempotent=False)

    monkeypatch.setattr(ttb_sync_router_module, "dispatch_sync", fake_dispatch)

    resp = client.post(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/sync",
        json={"scope": "meta"},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["summary"]["bc"]["added"] == 1
    assert data["run_id"] == 123


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

    monkeypatch.setattr(ttb_sync_router_module, "dispatch_sync", fake_dispatch)

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


def test_product_sync_limits_pairs_to_selected_advertiser(db_session):
    _seed_data(db_session)

    class DummyClient:
        def __init__(self):
            self.calls: list[dict] = []

        async def iter_products(  # noqa: ANN001
            self, *, store_id, bc_id=None, advertiser_id, page_size, eligibility
        ):
            self.calls.append(
                {
                    "store_id": store_id,
                    "bc_id": bc_id,
                    "advertiser_id": advertiser_id,
                    "page_size": page_size,
                    "eligibility": eligibility,
                }
            )
            if False:
                yield None

        async def aclose(self):  # noqa: ANN001
            return None

    # seed an extra advertiser/store link unrelated to the requested advertiser
    adv2 = TTBAdvertiser(workspace_id=1, auth_id=1, advertiser_id="ADV2", bc_id="BC2")
    store2 = TTBStore(workspace_id=1, auth_id=1, store_id="STORE2", bc_id="BC2")
    db_session.add_all(
        [
            adv2,
            store2,
            TTBAdvertiserStoreLink(
                workspace_id=1,
                auth_id=1,
                advertiser_id="ADV2",
                store_id="STORE2",
                bc_id_hint="BC2",
            ),
        ]
    )
    db_session.commit()

    client = DummyClient()
    service = TTBSyncService(db_session, client, workspace_id=1, auth_id=1)

    asyncio.run(service.sync_products(advertiser_id="ADV1"))

    assert len(client.calls) == 1
    assert client.calls[0]["advertiser_id"] == "ADV1"
    assert client.calls[0]["store_id"] == "STORE1"


def test_gmv_product_sync_tombstones_only_previously_tracked_absences(db_session):
    _seed_data(db_session)

    class SnapshotClient:
        def __init__(self):
            self.snapshots = [
                [
                    {
                        "item_group_id": "PRODUCT-CURRENT",
                        "store_id": "STORE1",
                        "title": "Current",
                        "status": "AVAILABLE",
                        "gmv_max_ads_status": "UNOCCUPIED",
                    },
                    {
                        "item_group_id": "PRODUCT-STALE",
                        "store_id": "STORE1",
                        "title": "Stale",
                        "status": "AVAILABLE",
                        "gmv_max_ads_status": "UNOCCUPIED",
                    },
                ],
                [
                    {
                        "item_group_id": "PRODUCT-CURRENT",
                        "store_id": "STORE1",
                        "title": "Current",
                        "status": "AVAILABLE",
                        "gmv_max_ads_status": "UNOCCUPIED",
                    }
                ],
            ]

        async def iter_products(self, **_kwargs):  # noqa: ANN003
            for item in self.snapshots.pop(0):
                yield item

    client = SnapshotClient()
    service = TTBSyncService(db_session, client, workspace_id=1, auth_id=1)
    asyncio.run(service.sync_products(store_id="STORE1", advertiser_id="ADV1"))
    asyncio.run(service.sync_products(store_id="STORE1", advertiser_id="ADV1"))

    current = db_session.query(TTBProduct).filter_by(product_id="PRODUCT-CURRENT").one()
    stale = db_session.query(TTBProduct).filter_by(product_id="PRODUCT-STALE").one()
    legacy = db_session.query(TTBProduct).filter_by(product_id="PROD1").one()
    current_evidence = db_session.query(TTBProductAdvertiserEligibility).filter_by(
        advertiser_id="ADV1",
        product_id="PRODUCT-CURRENT",
    ).one()
    stale_evidence = db_session.query(TTBProductAdvertiserEligibility).filter_by(
        advertiser_id="ADV1",
        product_id="PRODUCT-STALE",
    ).one()

    assert current.status == "AVAILABLE"
    assert current.gmv_max_ads_status == "UNOCCUPIED"
    assert stale.status == "AVAILABLE"
    assert current_evidence.is_eligible is True
    assert stale_evidence.is_eligible is False
    assert stale_evidence.absent_at is not None
    # The pre-existing row has no trustworthy eligibility-set provenance, so
    # the first deployment must not guess and invalidate it.
    assert legacy.status == "ON_SALE"
    assert (
        db_session.query(TTBProductAdvertiserEligibility)
        .filter_by(advertiser_id="ADV1", product_id="PROD1")
        .count()
        == 0
    )


def test_failed_gmv_product_pagination_never_tombstones(db_session):
    _seed_data(db_session)

    class SnapshotClient:
        fail = False

        async def iter_products(self, **_kwargs):  # noqa: ANN003
            if self.fail:
                raise RuntimeError("official pagination failed")
            yield {
                "item_group_id": "PRODUCT-TRACKED",
                "store_id": "STORE1",
                "title": "Tracked",
                "status": "AVAILABLE",
                "gmv_max_ads_status": "UNOCCUPIED",
            }

    client = SnapshotClient()
    service = TTBSyncService(db_session, client, workspace_id=1, auth_id=1)
    asyncio.run(service.sync_products(store_id="STORE1", advertiser_id="ADV1"))

    client.fail = True
    with pytest.raises(RuntimeError, match="pagination failed"):
        asyncio.run(service.sync_products(store_id="STORE1", advertiser_id="ADV1"))

    tracked = db_session.query(TTBProduct).filter_by(product_id="PRODUCT-TRACKED").one()
    evidence = db_session.query(TTBProductAdvertiserEligibility).filter_by(
        advertiser_id="ADV1",
        product_id="PRODUCT-TRACKED",
    ).one()
    assert tracked.gmv_max_ads_status == "UNOCCUPIED"
    assert evidence.is_eligible is True
    assert evidence.absent_at is None


def test_product_listing_uses_exact_advertiser_eligibility_evidence(
    tenant_app,
    monkeypatch,
):
    http_client, db_session = tenant_app
    meta_router_module = importlib.import_module(
        "app.features.tenants.ttb.router.meta"
    )
    monkeypatch.setattr(
        meta_router_module,
        "_load_product_automation_stats",
        lambda *_args, **_kwargs: {},
    )
    db_session.add(
        TTBAdvertiser(
            workspace_id=1,
            auth_id=1,
            advertiser_id="ADV2",
            bc_id="BC1",
            name="Advertiser 2",
            status="ENABLE",
        )
    )
    db_session.add(
        TTBAdvertiserStoreLink(
            workspace_id=1,
            auth_id=1,
            advertiser_id="ADV2",
            store_id="STORE1",
            relation_type="AUTHORIZER",
            store_authorized_bc_id="BC1",
            bc_id_hint="BC1",
        )
    )
    db_session.commit()

    class SnapshotClient:
        phase = 1

        async def iter_products(self, *, advertiser_id, **_kwargs):  # noqa: ANN003
            if advertiser_id == "ADV1" or self.phase == 1:
                yield {
                    "item_group_id": "PRODUCT-SHARED",
                    "store_id": "STORE1",
                    "title": "Shared",
                    "status": "AVAILABLE",
                    "gmv_max_ads_status": "UNOCCUPIED",
                }
            if advertiser_id == "ADV1":
                yield {
                    "item_group_id": "PRODUCT-A2",
                    "store_id": "STORE1",
                    "title": "Advertiser A second product",
                    "status": "AVAILABLE",
                    "gmv_max_ads_status": "UNOCCUPIED",
                }

    snapshot_client = SnapshotClient()
    service = TTBSyncService(
        db_session,
        snapshot_client,
        workspace_id=1,
        auth_id=1,
    )
    asyncio.run(service.sync_products(store_id="STORE1"))
    snapshot_client.phase = 2
    asyncio.run(service.sync_products(store_id="STORE1"))
    db_session.commit()

    first_page = http_client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={
            "store_id": "STORE1",
            "advertiser_id": "ADV1",
            "page": 1,
            "page_size": 1,
        },
    )
    second_page = http_client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={
            "store_id": "STORE1",
            "advertiser_id": "ADV1",
            "page": 2,
            "page_size": 1,
        },
    )
    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.json()["total"] == 2
    assert second_page.json()["total"] == 2
    assert (
        first_page.json()["items"][0]["product_id"]
        != second_page.json()["items"][0]["product_id"]
    )

    response = http_client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/products",
        params={
            "store_id": "STORE1",
            "advertiser_id": "ADV2",
        },
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0

    product = db_session.query(TTBProduct).filter_by(
        product_id="PRODUCT-SHARED"
    ).one()
    evidence = {
        row.advertiser_id: row
        for row in db_session.query(TTBProductAdvertiserEligibility)
        .filter_by(product_id="PRODUCT-SHARED")
        .all()
    }
    assert evidence["ADV1"].is_eligible is True
    assert evidence["ADV2"].is_eligible is False
    # The compatibility projection remains AVAILABLE/UNOCCUPIED for ADV1;
    # ADV2 is excluded by its own evidence rather than this shared column.
    assert product.status == "AVAILABLE"
    assert product.gmv_max_ads_status == "UNOCCUPIED"


def test_validate_options_preserves_advertiser_id_for_products():
    provider = TiktokBusinessProvider()

    opts = provider.validate_options(
        scope="products",
        options={"advertiser_id": "123", "store_id": "456"},
    )

    assert opts["advertiser_id"] == "123"
    assert opts["store_id"] == "456"


def test_validate_options_strips_advertiser_outside_product_scopes():
    provider = TiktokBusinessProvider()

    opts = provider.validate_options(scope="meta", options={"advertiser_id": "123"})

    assert "advertiser_id" not in opts


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


def test_sync_advertisers_does_not_treat_page_size_as_total_limit(tenant_app):
    _, db_session = tenant_app

    class DummyClient:
        async def iter_advertisers(self, *, page_size):  # noqa: ANN001
            assert page_size == 50
            for advertiser_id in ("ADV1", "ADV2", "ADV3"):
                yield {"advertiser_id": advertiser_id, "version": 1}

        async def fetch_advertiser_info(self, advertiser_ids, fields=None):  # noqa: ANN001
            assert set(advertiser_ids) == {"ADV1", "ADV2", "ADV3"}
            assert fields is not None
            return []

    service = TTBSyncService(db_session, DummyClient(), workspace_id=1, auth_id=1)

    stats = asyncio.run(service.sync_advertisers(page_size=2))

    assert stats["fetched"] == 3
    assert {
        row.advertiser_id
        for row in db_session.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == 1, TTBAdvertiser.auth_id == 1)
        .all()
    } >= {"ADV1", "ADV2", "ADV3"}


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


def test_upsert_inserts_with_last_seen(db_session):
    workspace, account = _create_workspace_and_auth(db_session)

    values = dict(
        workspace_id=int(workspace.id),
        auth_id=int(account.id),
        bc_id="BC-1",
        advertiser_id="ADV-1",
        relation_type="OWNER",
    )

    ttb_sync._upsert(
        db_session,
        TTBBCAdvertiserLink,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "bc_id", "advertiser_id"),
        update_columns=("relation_type", "source", "raw_json"),
    )

    row = db_session.query(TTBBCAdvertiserLink).one()
    assert row.relation_type == "OWNER"
    assert row.last_seen_at is not None


def test_upsert_updates_existing_row(db_session):
    workspace, account = _create_workspace_and_auth(db_session)

    values = dict(
        workspace_id=int(workspace.id),
        auth_id=int(account.id),
        bc_id="BC-1",
        advertiser_id="ADV-1",
        relation_type="OWNER",
    )

    ttb_sync._upsert(
        db_session,
        TTBBCAdvertiserLink,
        values=values,
        conflict_columns=("workspace_id", "auth_id", "bc_id", "advertiser_id"),
        update_columns=("relation_type", "source", "raw_json"),
    )

    initial = db_session.query(TTBBCAdvertiserLink).one()
    first_seen_at = initial.last_seen_at

    updated_values = dict(values)
    updated_values["relation_type"] = "PARTNER"
    updated_values["source"] = "sync"

    ttb_sync._upsert(
        db_session,
        TTBBCAdvertiserLink,
        values=updated_values,
        conflict_columns=("workspace_id", "auth_id", "bc_id", "advertiser_id"),
        update_columns=("relation_type", "source", "raw_json"),
    )

    db_session.expire_all()
    row = db_session.query(TTBBCAdvertiserLink).one()
    assert row.relation_type == "PARTNER"
    assert row.source == "sync"
    assert row.last_seen_at >= first_seen_at


def test_upsert_handles_integrity_conflict(db_session):
    workspace, account = _create_workspace_and_auth(db_session)

    values = dict(
        workspace_id=int(workspace.id),
        auth_id=int(account.id),
        bc_id="BC-1",
        advertiser_id="ADV-1",
        relation_type="OWNER",
    )

    db_session.execute(TTBBCAdvertiserLink.__table__.insert().values(**values))
    db_session.flush()

    conflicting_values = dict(values)
    conflicting_values["relation_type"] = "PARTNER"

    ttb_sync._upsert(
        db_session,
        TTBBCAdvertiserLink,
        values=conflicting_values,
        conflict_columns=("workspace_id", "auth_id", "bc_id", "advertiser_id"),
        update_columns=("relation_type", "source", "raw_json"),
    )

    db_session.expire_all()
    rows = db_session.query(TTBBCAdvertiserLink).all()
    assert len(rows) == 1
    assert rows[0].relation_type == "PARTNER"
