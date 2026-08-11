from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.features.tenants.ttb.gmv_max import service
from app.features.tenants.ttb.gmv_max.schemas import (
    CreateCampaignRequest,
    GMVMaxIdentityRequest,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignCreateBody,
    GMVMaxIdentityInfo,
)

pytestmark = pytest.mark.anyio


async def test_selected_product_identities_are_preserved_without_a_second_lookup(
    monkeypatch,
) -> None:
    calls = 0
    eligible = [
        GMVMaxIdentityInfo(identity_id=f"identity-{index}", identity_type="TT_USER")
        for index in range(25)
    ]

    async def _load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return eligible

    monkeypatch.setattr(service, "_load_product_identity_sources", _load)

    selected = [eligible[4], eligible[8]]
    result = await service._resolve_product_identity_list(
        object(),
        advertiser_id="adv-1",
        store_id="store-1",
        store_authorized_bc_id="bc-1",
        requested_identities=selected,
    )

    assert [identity.identity_id for identity in result] == [
        "identity-4",
        "identity-8",
    ]
    assert calls == 1


async def test_implicit_product_identity_selection_respects_official_limit(
    monkeypatch,
) -> None:
    eligible = [
        GMVMaxIdentityInfo(identity_id=f"identity-{index}", identity_type="TT_USER")
        for index in range(25)
    ]

    async def _load(*_args, **_kwargs):
        return eligible

    monkeypatch.setattr(service, "_load_product_identity_sources", _load)

    result = await service._resolve_product_identity_list(
        object(),
        advertiser_id="adv-1",
        store_id="store-1",
        store_authorized_bc_id="bc-1",
        requested_identities=None,
    )

    assert len(result) == 20
    assert result == eligible[:20]


async def test_product_campaign_can_use_product_images_without_an_identity(
    monkeypatch,
) -> None:
    async def _load(*_args, **_kwargs):
        return []

    monkeypatch.setattr(service, "_load_product_identity_sources", _load)

    result = await service._resolve_product_identity_list(
        object(),
        advertiser_id="adv-1",
        store_id="store-1",
        store_authorized_bc_id="bc-1",
        requested_identities=None,
    )

    assert result == []


async def test_same_identity_id_with_different_types_is_not_conflated(
    monkeypatch,
) -> None:
    eligible = [
        GMVMaxIdentityInfo(identity_id="shared", identity_type="TT_USER"),
        GMVMaxIdentityInfo(
            identity_id="shared",
            identity_type="BC_AUTH_TT",
            identity_authorized_bc_id="bc-1",
        ),
    ]

    async def _load(*_args, **_kwargs):
        return eligible

    monkeypatch.setattr(service, "_load_product_identity_sources", _load)

    result = await service._resolve_product_identity_list(
        object(),
        advertiser_id="adv-1",
        store_id="store-1",
        store_authorized_bc_id="bc-1",
        requested_identities=[
            GMVMaxIdentityInfo(
                identity_id="shared",
                identity_type="BC_AUTH_TT",
            )
        ],
    )

    assert len(result) == 1
    assert result[0].identity_type == "BC_AUTH_TT"
    assert result[0].identity_authorized_bc_id == "bc-1"


def test_create_contract_rejects_more_than_twenty_identities() -> None:
    identities = [
        GMVMaxIdentityRequest(
            identity_id=f"identity-{index}",
            identity_type="TT_USER",
        )
        for index in range(21)
    ]

    with pytest.raises(ValidationError):
        CreateCampaignRequest(
            campaign_name="campaign",
            store_id="store-1",
            identity_list=identities,
        )

    with pytest.raises(ValidationError):
        GMVMaxCampaignCreateBody(
            request_id="123456789",
            store_id="store-1",
            store_authorized_bc_id="bc-1",
            shopping_ads_type="PRODUCT",
            optimization_goal="VALUE",
            deep_bid_type="VO_MIN_ROAS",
            campaign_name="campaign",
            identity_list=[
                GMVMaxIdentityInfo(
                    identity_id=f"identity-{index}",
                    identity_type="TT_USER",
                )
                for index in range(21)
            ],
        )


def test_create_contract_accepts_auth_code_identity() -> None:
    payload = CreateCampaignRequest(
        campaign_name="campaign",
        store_id="store-1",
        identity_list=[
            GMVMaxIdentityRequest(
                identity_id="authorized-post-owner",
                identity_type="AUTH_CODE",
            )
        ],
    )

    assert payload.identity_list[0].identity_type == "AUTH_CODE"
