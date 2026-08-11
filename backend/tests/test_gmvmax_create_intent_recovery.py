from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.data.models.gmv_restructured import GmvStrategyConfig
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxProductCampaignCatalog,
)
from app.gmvmax.services.create_intent_recovery import (
    recover_one_gmvmax_create_intent,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaign,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignListData,
    GMVMaxResponse,
    PageInfo,
)
from app.services.ttb_api import TTBHttpError


pytestmark = pytest.mark.anyio


class _Mutation:
    def __init__(self) -> None:
        self.assertions = 0
        self.commits = 0

    def assert_current(self, db) -> None:  # noqa: ANN001
        _ = db
        self.assertions += 1

    def commit(self, db) -> None:  # noqa: ANN001
        self.assert_current(db)
        db.commit()
        self.commits += 1


class _ReadOnlyRecoveryClient:
    def __init__(
        self,
        *,
        campaign_name: str,
        campaign_ids: list[str] | None = None,
        pause_error: Exception | None = None,
    ) -> None:
        self.campaign_name = campaign_name
        self.campaign_ids = campaign_ids or ["campaign-recovered"]
        self.pause_error = pause_error
        self.list_calls = 0
        self.info_calls = 0
        self.pause_requests = []

    async def gmv_max_campaign_get(self, request):  # noqa: ANN001
        self.list_calls += 1
        assert request.filtering.campaign_name == self.campaign_name
        rows = [
            GMVMaxCampaign(
                campaign_id=campaign_id,
                campaign_name=self.campaign_name,
                operation_status="ENABLE",
            )
            for campaign_id in self.campaign_ids
        ]
        return GMVMaxResponse(
            code=0,
            message="OK",
            data=GMVMaxCampaignListData(
                list=rows,
                page_info=PageInfo(
                    page=1,
                    page_size=100,
                    total_number=len(rows),
                    total_page=1,
                    has_more=False,
                    has_next=False,
                ),
            ),
        )

    async def gmv_max_campaign_info(self, request):  # noqa: ANN001
        self.info_calls += 1
        assert request.campaign_id in self.campaign_ids
        return GMVMaxResponse(
            code=0,
            message="OK",
            data=GMVMaxCampaignInfoData(
                campaign_id=request.campaign_id,
                campaign_name=self.campaign_name,
                advertiser_id="advertiser-1",
                store_id="store-1",
                shopping_ads_type="PRODUCT",
                operation_status="ENABLE",
                product_specific_type="CUSTOMIZED_PRODUCTS",
                item_group_ids=["product-1"],
            ),
        )

    async def campaign_status_update(self, request):  # noqa: ANN001
        self.pause_requests.append(request)
        if self.pause_error is not None:
            raise self.pause_error
        return SimpleNamespace(request_id="pause-request-1")


def _seed_intent(db_session, *, state: str = "UNKNOWN"):
    intent = GmvmaxCampaignCreateIntent(
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        idempotency_key="123456789012345678",
        client_payload_sha256="a" * 64,
        payload_sha256="b" * 64,
        official_request_id="123456789012345678",
        campaign_name="stable-create_i1234abcd",
        state=state,
        request_json={
            "campaign_name": "stable-create",
            "store_id": "store-1",
            "item_group_ids": ["product-1"],
        },
    )
    db_session.add(intent)
    db_session.commit()
    return intent


def _seed_creative_rebuild_intent(
    db_session,
    *,
    phase: str,
    source_enabled: bool,
    creative_guard_enabled: bool = True,
):
    state = "REMOTE_CREATED" if phase == "QUARANTINE_PENDING" else "FINALIZING"
    intent = _seed_intent(db_session, state=state)
    request_json = dict(intent.request_json)
    request_json["advertiser_id"] = "advertiser-1"
    request_json["idempotency_key"] = intent.idempotency_key
    request_json["request_id"] = intent.official_request_id
    request_json["replacement_campaign_id"] = "campaign-old"
    request_json["automation"] = {"source": "creative_guard_rebuild"}
    intent.request_json = request_json
    intent.replacement_campaign_id = "campaign-old"
    intent.campaign_id = "campaign-recovered"
    intent.result_json = {
        "campaign_id": "campaign-recovered",
        "rebuild_workflow": {
            "source": "creative_guard_rebuild",
            "phase": phase,
        },
    }
    db_session.add(
        GmvStrategyConfig(
            workspace_id=1,
            auth_id=2,
            campaign_id="campaign-old",
            enabled=source_enabled,
            config_json={
                "creative_guard": {"enabled": creative_guard_enabled}
            },
        )
    )
    db_session.add(
        GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-old",
            campaign_name="Old campaign",
            operation_status="ENABLE" if source_enabled else "DISABLE",
            secondary_status=(
                "CAMPAIGN_STATUS_ENABLE"
                if source_enabled
                else "CAMPAIGN_STATUS_DISABLE"
            ),
            shopping_ads_type="PRODUCT",
            product_specific_type="CUSTOMIZED_PRODUCTS",
            detail_raw_json={"item_group_ids": ["product-1"]},
        )
    )
    db_session.commit()
    return intent


async def test_recovery_uses_get_marker_then_pauses_and_quarantines(
    db_session,
):
    intent = _seed_intent(db_session)
    client = _ReadOnlyRecoveryClient(campaign_name=intent.campaign_name)
    mutation = _Mutation()

    result = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=1,
        auth_id=2,
        client=client,
        mutation=mutation,
    )

    assert result == {
        "status": "quarantined",
        "state": "QUARANTINED",
        "campaign_id": "campaign-recovered",
    }
    assert client.list_calls == 1
    assert client.info_calls == 1
    assert len(client.pause_requests) == 1
    assert client.pause_requests[0].operation_status == "DISABLE"
    assert client.pause_requests[0].campaign_ids == ["campaign-recovered"]
    db_session.refresh(intent)
    assert intent.state == "QUARANTINED"
    assert intent.campaign_id == "campaign-recovered"
    assert intent.result_json["remote_pause_confirmed"] is True

    campaign = db_session.query(GmvmaxProductCampaignCatalog).one()
    assert campaign.operation_status == "DISABLE"
    assert campaign.secondary_status == "CAMPAIGN_STATUS_DISABLE"
    strategy = db_session.query(GmvStrategyConfig).one()
    assert strategy.enabled is False
    assert strategy.config_json["creation_quarantine"]["enabled"] is True
    assert (
        strategy.config_json["creation_quarantine"]["remote_pause_confirmed"]
        is True
    )
    assert mutation.commits == 2


async def test_pause_failure_stays_recoverable_and_never_resubmits_create(
    db_session,
):
    intent = _seed_intent(db_session)
    client = _ReadOnlyRecoveryClient(
        campaign_name=intent.campaign_name,
        pause_error=TTBHttpError(503, "temporary status timeout"),
    )
    mutation = _Mutation()

    first = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=1,
        auth_id=2,
        client=client,
        mutation=mutation,
    )

    assert first["status"] == "pause_pending"
    db_session.refresh(intent)
    assert intent.state == "REMOTE_CREATED"
    assert intent.campaign_id == "campaign-recovered"
    strategy = db_session.query(GmvStrategyConfig).one()
    assert strategy.enabled is False
    assert (
        strategy.config_json["creation_quarantine"]["remote_pause_confirmed"]
        is False
    )

    client.pause_error = None
    second = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=1,
        auth_id=2,
        client=client,
        mutation=mutation,
    )

    assert second["status"] == "quarantined"
    assert client.list_calls == 1
    assert client.info_calls == 2
    assert len(client.pause_requests) == 2
    db_session.refresh(intent)
    assert intent.state == "QUARANTINED"


async def test_ambiguous_marker_match_remains_pending_without_remote_mutation(
    db_session,
):
    intent = _seed_intent(db_session, state="SUBMITTING")
    client = _ReadOnlyRecoveryClient(
        campaign_name=intent.campaign_name,
        campaign_ids=["campaign-1", "campaign-2"],
    )

    result = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=1,
        auth_id=2,
        client=client,
        mutation=_Mutation(),
    )

    assert result == {"status": "pending", "state": "SUBMITTING"}
    assert client.list_calls == 1
    assert client.info_calls == 0
    assert client.pause_requests == []
    db_session.refresh(intent)
    assert intent.state == "SUBMITTING"
    assert db_session.query(GmvStrategyConfig).count() == 0


async def test_recovery_refuses_cross_workspace_intent_lookup(db_session):
    intent = _seed_intent(db_session)
    client = _ReadOnlyRecoveryClient(campaign_name=intent.campaign_name)

    result = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=999,
        auth_id=2,
        client=client,
        mutation=_Mutation(),
    )

    assert result == {"status": "stale"}
    assert client.list_calls == 0
    assert client.info_calls == 0
    assert client.pause_requests == []
    db_session.refresh(intent)
    assert intent.state == "UNKNOWN"


@pytest.mark.parametrize(
    (
        "phase",
        "source_enabled",
        "creative_guard_enabled",
        "generic_takes_over",
    ),
    [
        ("FINALIZING", True, True, False),
        ("FINALIZING", True, False, False),
        ("QUARANTINE_PENDING", True, True, True),
        ("FINALIZING", False, True, True),
    ],
)
async def test_generic_recovery_respects_creative_rebuild_ownership_fail_closed(
    db_session,
    phase,
    source_enabled,
    creative_guard_enabled,
    generic_takes_over,
):
    intent = _seed_creative_rebuild_intent(
        db_session,
        phase=phase,
        source_enabled=source_enabled,
        creative_guard_enabled=creative_guard_enabled,
    )
    client = _ReadOnlyRecoveryClient(campaign_name=intent.campaign_name)

    result = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=1,
        auth_id=2,
        client=client,
        mutation=_Mutation(),
    )

    if not generic_takes_over:
        assert result == {"status": "stale"}
        assert client.list_calls == 0
        assert client.info_calls == 0
        assert client.pause_requests == []
        db_session.refresh(intent)
        assert intent.state == "FINALIZING"
        assert (
            intent.result_json["rebuild_workflow"]["phase"]
            == "FINALIZING"
        )
        assert (
            db_session.query(GmvStrategyConfig)
            .filter(GmvStrategyConfig.campaign_id == "campaign-recovered")
            .count()
            == 0
        )
        return

    assert result == {
        "status": "quarantined",
        "state": "QUARANTINED",
        "campaign_id": "campaign-recovered",
    }
    assert client.list_calls == 0
    assert client.info_calls == 1
    assert len(client.pause_requests) == 1
    assert client.pause_requests[0].campaign_ids == ["campaign-recovered"]
    assert client.pause_requests[0].operation_status == "DISABLE"
    db_session.refresh(intent)
    assert intent.state == "QUARANTINED"
    replacement_strategy = (
        db_session.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.campaign_id == "campaign-recovered")
        .one()
    )
    assert replacement_strategy.enabled is False
    assert (
        replacement_strategy.config_json["creation_quarantine"][
            "remote_pause_confirmed"
        ]
        is True
    )


async def test_stale_creative_owner_claim_survives_its_own_reconcile_heartbeat(
    db_session,
):
    intent = _seed_creative_rebuild_intent(
        db_session,
        phase="FINALIZING",
        source_enabled=True,
    )
    intent.updated_at = (
        datetime.now(timezone.utc) - timedelta(minutes=6)
    ).replace(tzinfo=None)
    db_session.add(intent)
    db_session.commit()
    client = _ReadOnlyRecoveryClient(campaign_name=intent.campaign_name)

    result = await recover_one_gmvmax_create_intent(
        db_session,
        intent_id=int(intent.id),
        workspace_id=1,
        auth_id=2,
        client=client,
        mutation=_Mutation(),
    )

    assert result == {
        "status": "quarantined",
        "state": "QUARANTINED",
        "campaign_id": "campaign-recovered",
    }
    assert len(client.pause_requests) == 1
    db_session.refresh(intent)
    assert intent.state == "QUARANTINED"
    replacement = (
        db_session.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.campaign_id
            == "campaign-recovered"
        )
        .one()
    )
    assert replacement.operation_status == "DISABLE"
    replacement_strategy = (
        db_session.query(GmvStrategyConfig)
        .filter(GmvStrategyConfig.campaign_id == "campaign-recovered")
        .one()
    )
    assert replacement_strategy.enabled is False
    assert (
        replacement_strategy.config_json["creation_quarantine"][
            "remote_pause_confirmed"
        ]
        is True
    )
