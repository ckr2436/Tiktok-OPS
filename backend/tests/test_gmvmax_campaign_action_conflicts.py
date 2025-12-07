import json
from decimal import Decimal
from typing import Any, List

import pytest

from app.data.models.gmv_restructured import GmvCampaign, PromotionTypeEnum
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_entities import TTBBindingConfig
from app.data.models.workspaces import Workspace
from app.features.tenants.ttb.gmv_max import service as tenant_service
from app.services.ttb_api import TTBBusinessError, TTBApiClient


class _StubGMVMaxClient:
    def __init__(self, *, promote_all_allowed: bool = True) -> None:
        self.promote_all_allowed = promote_all_allowed
        self.usage_checks: list[Any] = []
        self.update_calls: list[dict[str, Any]] = []

    async def gmv_max_store_shop_ad_usage_check(self, request: Any) -> Any:
        self.usage_checks.append(request)

        class _Data:
            def __init__(self, allowed: bool) -> None:
                self.promote_all_products_allowed = allowed

        class _Resp:
            def __init__(self, allowed: bool) -> None:
                self.data = _Data(allowed)

        return _Resp(self.promote_all_allowed)

    async def update_gmvmax_campaign(self, advertiser_id: str, body: dict[str, Any]) -> None:
        self.update_calls.append({"advertiser_id": advertiser_id, "body": body})

    async def aclose(self) -> None:  # pragma: no cover - used by service cleanup
        return None


class _StubTTBClient:
    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.products = products
        self.calls = 0

    async def get_store_products_for_gmvmax_item_group_ids(
        self,
        *,
        bc_id: str | None,
        store_id: str,
        advertiser_id: str,
        item_group_ids: list[str],
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        self.calls += 1
        return self.products

    async def aclose(self) -> None:  # pragma: no cover - used by service cleanup
        return None


class _BlockingTTBClient:
    def __init__(self) -> None:
        self.called = False

    async def get_store_products_for_gmvmax_item_group_ids(self, **_: Any) -> list[dict[str, Any]]:
        self.called = True
        return []

    async def aclose(self) -> None:  # pragma: no cover
        return None


def _setup_campaign(
    db_session,
    *,
    promotion_type: PromotionTypeEnum,
    shopping_ads_type: str,
    raw_json: dict[str, Any] | None = None,
) -> GmvCampaign:
    workspace = Workspace(id=1, name="Test", company_code="0001")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=1,
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    db_session.add(provider_app)
    db_session.flush()

    account = OAuthAccountTTB(
        id=1,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.flush()

    binding = TTBBindingConfig(
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-1",
        bc_id="bc-1",
        store_id="store-1",
    )
    db_session.add(binding)

    campaign = GmvCampaign(
        id=1,
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id=binding.advertiser_id,
        campaign_id="cmp-1",
        store_id=binding.store_id or "store-1",
        name="GMV Campaign",
        status="PAUSED",
        daily_budget_cents=1000,
        roas_bid=Decimal("1.00"),
        currency="USD",
        promotion_type=promotion_type,
        shopping_ads_type=shopping_ads_type,
        raw_json=raw_json,
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


@pytest.mark.anyio
async def test_all_campaign_blocks_when_store_usage_disallows(db_session):
    campaign = _setup_campaign(
        db_session,
        promotion_type=PromotionTypeEnum.PRODUCT,
        shopping_ads_type="PRODUCT",
        raw_json={"product_specific_type": "ALL"},
    )

    gmv_client = _StubGMVMaxClient(promote_all_allowed=False)
    blocker = _BlockingTTBClient()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        tenant_service,
        "get_gmvmax_client_for_account",
        lambda *_, **__: gmv_client,
    )
    monkeypatch.setattr(
        tenant_service,
        "get_ttb_client_for_account",
        lambda *_, **__: blocker,
    )

    with pytest.raises(TTBBusinessError) as exc:
        await tenant_service.apply_campaign_action(
            db_session,
            workspace_id=campaign.workspace_id,
            provider="tiktok-business",
            auth_id=campaign.auth_id,
            campaign_id=campaign.campaign_id,
            action="START",
            payload={},
            reason="test",
            performed_by="unit-test",
        )

    monkeypatch.undo()

    assert exc.value.code == "GMVMAX_PROMOTE_ALL_CONFLICT"
    assert not blocker.called
    assert not gmv_client.update_calls


@pytest.mark.anyio
async def test_customized_products_all_unoccupied(db_session):
    campaign = _setup_campaign(
        db_session,
        promotion_type=PromotionTypeEnum.PRODUCT,
        shopping_ads_type="PRODUCT",
        raw_json={
            "product_specific_type": "CUSTOMIZED_PRODUCTS",
            "item_group_ids": ["spu_1", "spu_2"],
        },
    )

    gmv_client = _StubGMVMaxClient()
    ttb_client = _StubTTBClient(
        [
            {"item_group_id": "spu_1", "status": "AVAILABLE", "gmv_max_ads_status": "UNOCCUPIED"},
            {"item_group_id": "spu_2", "status": "AVAILABLE", "gmv_max_ads_status": "UNOCCUPIED"},
        ]
    )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        tenant_service,
        "get_gmvmax_client_for_account",
        lambda *_, **__: gmv_client,
    )
    monkeypatch.setattr(
        tenant_service,
        "get_ttb_client_for_account",
        lambda *_, **__: ttb_client,
    )

    await tenant_service.apply_campaign_action(
        db_session,
        workspace_id=campaign.workspace_id,
        provider="tiktok-business",
        auth_id=campaign.auth_id,
        campaign_id=campaign.campaign_id,
        action="START",
        payload={},
        reason="test",
        performed_by="unit-test",
    )

    monkeypatch.undo()

    assert ttb_client.calls == 1
    assert gmv_client.update_calls
    assert campaign.status == "ACTIVE"


@pytest.mark.anyio
async def test_customized_products_conflict_blocks_start(db_session):
    campaign = _setup_campaign(
        db_session,
        promotion_type=PromotionTypeEnum.PRODUCT,
        shopping_ads_type="PRODUCT",
        raw_json={
            "product_specific_type": "CUSTOMIZED_PRODUCTS",
            "item_group_ids": ["spu_1", "spu_2"],
        },
    )

    gmv_client = _StubGMVMaxClient()
    ttb_client = _StubTTBClient(
        [
            {"item_group_id": "spu_1", "status": "AVAILABLE", "gmv_max_ads_status": "UNOCCUPIED"},
            {"item_group_id": "spu_2", "status": "AVAILABLE", "gmv_max_ads_status": "OCCUPIED"},
        ]
    )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        tenant_service,
        "get_gmvmax_client_for_account",
        lambda *_, **__: gmv_client,
    )
    monkeypatch.setattr(
        tenant_service,
        "get_ttb_client_for_account",
        lambda *_, **__: ttb_client,
    )

    with pytest.raises(TTBBusinessError) as exc:
        await tenant_service.apply_campaign_action(
            db_session,
            workspace_id=campaign.workspace_id,
            provider="tiktok-business",
            auth_id=campaign.auth_id,
            campaign_id=campaign.campaign_id,
            action="START",
            payload={},
            reason="test",
            performed_by="unit-test",
        )

    monkeypatch.undo()

    assert exc.value.code == "GMVMAX_PRODUCT_OCCUPIED"
    assert "spu_2" in (exc.value.payload or {}).get("item_group_ids", [])
    assert not gmv_client.update_calls


@pytest.mark.anyio
async def test_customized_products_batches_over_ten(db_session):
    item_group_ids = [f"spu_{idx}" for idx in range(1, 24)]
    campaign = _setup_campaign(
        db_session,
        promotion_type=PromotionTypeEnum.PRODUCT,
        shopping_ads_type="PRODUCT",
        raw_json={
            "product_specific_type": "CUSTOMIZED_PRODUCTS",
            "item_group_ids": item_group_ids,
        },
    )

    gmv_client = _StubGMVMaxClient()
    api_client = TTBApiClient(access_token="token")

    call_batches: List[list[str]] = []

    async def _fake_request_json(method: str, path: str, *, params=None, json_body=None):
        filtering = json.loads(params.get("filtering") or "{}")
        batch_ids = filtering.get("item_group_ids") or []
        call_batches.append(batch_ids)
        return {
            "data": {
                "store_products": [
                    {
                        "item_group_id": spu,
                        "status": "AVAILABLE",
                        "gmv_max_ads_status": "UNOCCUPIED",
                    }
                    for spu in batch_ids
                ]
            }
        }

    api_client._request_json = _fake_request_json  # type: ignore[method-assign]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        tenant_service,
        "get_gmvmax_client_for_account",
        lambda *_, **__: gmv_client,
    )
    monkeypatch.setattr(
        tenant_service,
        "get_ttb_client_for_account",
        lambda *_, **__: api_client,
    )

    await tenant_service.apply_campaign_action(
        db_session,
        workspace_id=campaign.workspace_id,
        provider="tiktok-business",
        auth_id=campaign.auth_id,
        campaign_id=campaign.campaign_id,
        action="START",
        payload={},
        reason="test",
        performed_by="unit-test",
    )

    monkeypatch.undo()

    assert len(call_batches) == 3
    assert call_batches[0] == item_group_ids[0:10]
    assert call_batches[1] == item_group_ids[10:20]
    assert call_batches[2] == item_group_ids[20:]
    assert gmv_client.update_calls


@pytest.mark.anyio
async def test_non_product_campaign_skips_conflict_checks(db_session):
    campaign = _setup_campaign(
        db_session,
        promotion_type=PromotionTypeEnum.LIVE,
        shopping_ads_type="LIVE",
        raw_json={"product_specific_type": "CUSTOMIZED_PRODUCTS"},
    )

    gmv_client = _StubGMVMaxClient()
    blocker = _BlockingTTBClient()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        tenant_service,
        "get_gmvmax_client_for_account",
        lambda *_, **__: gmv_client,
    )
    monkeypatch.setattr(
        tenant_service,
        "get_ttb_client_for_account",
        lambda *_, **__: blocker,
    )

    await tenant_service.apply_campaign_action(
        db_session,
        workspace_id=campaign.workspace_id,
        provider="tiktok-business",
        auth_id=campaign.auth_id,
        campaign_id=campaign.campaign_id,
        action="START",
        payload={},
        reason="test",
        performed_by="unit-test",
    )

    monkeypatch.undo()

    assert not blocker.called
    assert gmv_client.update_calls
    assert campaign.status == "ACTIVE"
