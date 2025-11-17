import base64
from decimal import Decimal
from importlib import import_module

import base64
from decimal import Decimal
from importlib import import_module

import pytest

from app.core.config import settings
from app.data.models import OAuthAccountTTB, OAuthProviderApp, Workspace
from app.data.models.ttb_entities import TTBBindingConfig
from app.data.models.ttb_gmvmax import TTBGmvMaxCampaign, TTBGmvMaxStrategyConfig
from app.features.tenants.ttb.gmv_max.service import get_strategy, update_strategy
from app.services.crypto import encrypt_text_to_blob


@pytest.fixture()
def gmv_strategy_db(db_session):
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

    binding = TTBBindingConfig(
        workspace_id=1,
        auth_id=1,
        bc_id="7508663838649384976",
        advertiser_id="7492997033645637633",
        store_id="7496202240253986992",
        auto_sync_products=False,
    )
    db_session.add(binding)

    campaign = TTBGmvMaxCampaign(
        id=1,
        workspace_id=1,
        auth_id=1,
        advertiser_id=str(binding.advertiser_id),
        campaign_id="cmp-partial",
        store_id=str(binding.store_id),
        name="Partial Update",
    )
    db_session.add(campaign)

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
    db_session.add(config)

    db_session.commit()
    return db_session


def test_strategy_routes_registered():
    mod = import_module("app.features.tenants.ttb.gmv_max.router_provider")
    router = getattr(mod, "router", None)
    paths = {route.path for route in router.routes}
    assert any("/{campaign_id}/strategy" in path for path in paths)
    assert any("/{campaign_id}/strategies/preview" in path for path in paths)


def test_update_strategy_partial_patch_keeps_other_fields(gmv_strategy_db):
    db = gmv_strategy_db

    cfg = update_strategy(
        db,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        campaign_id="cmp-partial",
        payload={"target_roi": "1.5"},
    )
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.min_roi == Decimal("0.80")
    assert cfg.max_roi == Decimal("2.00")
    assert cfg.min_impressions == 1000
    assert cfg.min_clicks == 50
    assert cfg.max_budget_raise_pct_per_day == Decimal("12.5")
    assert cfg.max_budget_cut_pct_per_day == Decimal("7.5")
    assert cfg.max_roas_step_per_adjust == Decimal("0.40")
    assert cfg.cooldown_minutes == 45
    assert cfg.min_runtime_minutes_before_first_change == 120
    assert cfg.target_roi == Decimal("1.5")


def test_update_strategy_noop_returns_none(gmv_strategy_db):
    db = gmv_strategy_db

    cfg = update_strategy(
        db,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        campaign_id="cmp-partial",
        payload={},
    )
    assert cfg is None


def test_get_strategy_returns_existing_record(gmv_strategy_db):
    db = gmv_strategy_db

    cfg = get_strategy(
        db,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=1,
        campaign_id="cmp-partial",
    )
    assert cfg.campaign_id == "cmp-partial"
    assert cfg.target_roi == Decimal("1.10")
