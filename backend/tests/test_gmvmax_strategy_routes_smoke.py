import base64
from decimal import Decimal
from importlib import import_module

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.deps import SessionUser, require_tenant_admin, require_tenant_member
from app.core.errors import install_exception_handlers
from app.data.models import OAuthAccountTTB, OAuthProviderApp, Workspace
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxStrategyConfig
from app.features.tenants.ttb.router import router as ttb_router
from app.services.crypto import encrypt_text_to_blob


@pytest.fixture()
def gmv_strategy_app(db_session):
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

    def _override(workspace_id: int):  # noqa: ANN001
        return dummy_user

    app.dependency_overrides[require_tenant_member] = _override
    app.dependency_overrides[require_tenant_admin] = _override

    if not getattr(settings, "CRYPTO_MASTER_KEY_B64", ""):
        settings.CRYPTO_MASTER_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode()

    workspace = Workspace(id=1, name="Acme", company_code="ACME")
    db_session.add(workspace)

    provider = OAuthProviderApp(
        id=1,
        provider="tiktok_business",
        name="Default",
        client_id="client",
        client_secret_cipher=encrypt_text_to_blob(
            "secret",
            key_version=1,
            aad_text="tiktok_business|client|https://example.com/callback",
        ),
        client_secret_key_version=1,
        redirect_uri="https://example.com/callback",
        is_enabled=True,
    )
    db_session.add(provider)

    account = OAuthAccountTTB(
        id=1,
        workspace_id=1,
        provider_app_id=1,
        alias="binding",
        access_token_cipher=encrypt_text_to_blob("token", key_version=1, aad_text="token"),
        key_version=1,
        token_fingerprint=b"0" * 32,
        status="active",
    )
    db_session.add(account)

    db_session.commit()

    with TestClient(app) as client:
        yield client, db_session

    app.dependency_overrides.clear()


def test_strategy_routes_registered():
    mod = import_module("app.features.tenants.ttb.gmv_max.router")
    router = getattr(mod, "router", None)
    assert isinstance(router, APIRouter)
    paths = {route.path for route in router.routes}
    assert any("/{campaign_id}/strategy" in path for path in paths)
    assert any("/{campaign_id}/strategy/preview" in path for path in paths)


def test_update_strategy_partial_does_not_reset_other_fields(gmv_strategy_app):
    client, db = gmv_strategy_app

    campaign = TTBGmvMaxCampaign(
        id=1,
        workspace_id=1,
        auth_id=1,
        advertiser_id="7492997033645637633",
        campaign_id="cmp-partial",
        name="Partial Update",
    )
    db.add(campaign)
    db.flush()

    config = TTBGmvMaxStrategyConfig(
        id=1,
        workspace_id=1,
        auth_id=1,
        campaign_id=campaign.campaign_id,
        enabled=True,
        target_roi=Decimal("1.10"),
        min_roi=Decimal("0.80"),
        max_roi=Decimal("2.00"),
        min_impressions=1000,
        min_clicks=50,
        max_budget_raise_pct_per_day=Decimal("12.5"),
        max_budget_cut_pct_per_day=Decimal("7.5"),
        max_roas_step_per_adjust=Decimal("0.40"),
        cooldown_minutes=45,
        min_runtime_minutes_before_first_change=120,
    )
    db.add(config)
    db.commit()

    routes = {route.path for route in client.app.routes}
    assert any('gmvmax' in path for path in routes)

    response = client.put(
        "/api/v1/tenants/1/ttb/accounts/1/gmvmax/cmp-partial/strategy",
        json={"target_roi": "1.5"},
    )
    assert response.status_code == 200, response.text

    db.refresh(config)

    assert config.enabled is True
    assert config.min_roi == Decimal("0.80")
    assert config.max_roi == Decimal("2.00")
    assert config.min_impressions == 1000
    assert config.min_clicks == 50
    assert config.max_budget_raise_pct_per_day == Decimal("12.5")
    assert config.max_budget_cut_pct_per_day == Decimal("7.5")
    assert config.max_roas_step_per_adjust == Decimal("0.40")
    assert config.cooldown_minutes == 45
    assert config.min_runtime_minutes_before_first_change == 120
    assert config.target_roi == Decimal("1.5")

    body = response.json()
    assert body["enabled"] is True
    assert Decimal(body["min_roi"]) == Decimal("0.80")
    assert Decimal(body["max_roi"]) == Decimal("2.00")
    assert body["min_impressions"] == 1000
    assert body["min_clicks"] == 50
    assert Decimal(body["target_roi"]) == Decimal("1.5")
    assert body["max_budget_raise_pct_per_day"] == 12.5
    assert body["max_budget_cut_pct_per_day"] == 7.5
    assert body["min_runtime_minutes_before_first_change"] == 120
