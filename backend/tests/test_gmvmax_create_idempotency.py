from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.features.tenants.ttb.gmv_max import service
from app.features.tenants.ttb.gmv_max.schemas import CreateCampaignRequest
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaignCreateBody,
    GMVMaxCampaign,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignListData,
    GMVMaxResponse,
    PageInfo,
)
from app.services import gmvmax_creative_guard
from app.services.ttb_api import TTBBusinessError, TTBHttpError


pytestmark = pytest.mark.anyio


class _FakeClient:
    def __init__(self) -> None:
        self.listed_campaigns: list[GMVMaxCampaign] = []
        self.info: GMVMaxCampaignInfoData | None = None

    async def gmv_max_campaign_get(self, request):  # noqa: ANN001
        _ = request
        return GMVMaxResponse(
            code=0,
            message="OK",
            data=GMVMaxCampaignListData(
                list=self.listed_campaigns,
                page_info=PageInfo(
                    page=1,
                    page_size=100,
                    total_number=len(self.listed_campaigns),
                    total_page=1,
                    has_more=False,
                    has_next=False,
                ),
            ),
        )

    async def gmv_max_campaign_info(self, request):  # noqa: ANN001
        assert self.info is not None
        assert request.campaign_id == self.info.campaign_id
        return GMVMaxResponse(code=0, message="OK", data=self.info)

    async def aclose(self) -> None:
        return None


def _payload(
    *,
    budget: int = 200,
    idempotency_key: str = "123456789012345678",
) -> CreateCampaignRequest:
    return CreateCampaignRequest(
        request_id=idempotency_key,
        idempotency_key=idempotency_key,
        campaign_name="stable-create",
        store_id="store-1",
        store_authorized_bc_id="bc-1",
        budget=budget,
        roas_bid=1.2,
        schedule_type="SCHEDULE_FROM_NOW",
        schedule_start_time=datetime(2026, 7, 18, 9, 18),
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["product-1"],
        product_video_specific_type="AUTO_SELECTION",
        automation={"enabled": True},
    )


@pytest.fixture(autouse=True)
def _isolate_create_dependencies(monkeypatch):
    monkeypatch.setattr(service, "ensure_ttb_auth_in_workspace", lambda *a, **k: None)
    monkeypatch.setattr(service, "_ensure_provider", lambda value: value)
    monkeypatch.setattr(
        service,
        "_get_advertiser_timezone",
        lambda *args, **kwargs: None,
    )

    async def _conflict_free(*args, **kwargs):
        return None

    async def _identities(*args, **kwargs):
        return []

    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _conflict_free,
    )
    monkeypatch.setattr(service, "_resolve_product_identity_list", _identities)


def _install_successful_create(monkeypatch, *, calls: list[str]) -> None:
    async def _create(db, **kwargs):  # noqa: ANN001
        body = kwargs["body"]
        calls.append(str(body.request_id))
        row = GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id=str(body.store_id),
            campaign_id="campaign-1",
            campaign_name=str(body.campaign_name),
            operation_status="ENABLE",
            secondary_status="CAMPAIGN_STATUS_ENABLE",
            shopping_ads_type="PRODUCT",
            detail_raw_json={
                "campaign_id": "campaign-1",
                "campaign_name": str(body.campaign_name),
                "store_id": str(body.store_id),
                "item_group_ids": ["product-1"],
            },
        )
        db.add(row)
        db.add(
            GmvmaxProductCampaignItemGroup(
                workspace_id=1,
                auth_id=2,
                advertiser_id="advertiser-1",
                store_id=str(body.store_id),
                campaign_id="campaign-1",
                item_group_id="product-1",
            )
        )
        db.flush()
        return row

    monkeypatch.setattr(service, "svc_create_campaign", _create)


async def _create(
    db_session,
    client,
    payload=None,
    *,
    client_payload_sha256: str | None = None,
):
    return await service.create_gmvmax_campaign(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload or _payload(),
        store_authorized_bc_id="bc-1",
        client_payload_sha256=client_payload_sha256,
        client=client,
    )


def _seed_intent(
    db_session,
    *,
    payload: CreateCampaignRequest,
    state: str,
    client_payload_sha256: str | None = None,
) -> GmvmaxCampaignCreateIntent:
    payload_sha256 = service.gmvmax_create_payload_sha256(
        payload,
        advertiser_id="advertiser-1",
    )
    intent = GmvmaxCampaignCreateIntent(
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        idempotency_key=str(payload.idempotency_key),
        client_payload_sha256=client_payload_sha256 or payload_sha256,
        payload_sha256=payload_sha256,
        official_request_id=str(payload.request_id),
        campaign_name=service._intent_campaign_name(
            payload.campaign_name,
            str(payload.idempotency_key),
        ),
        state=state,
        request_json=payload.model_dump(mode="json", exclude_none=True),
    )
    db_session.add(intent)
    db_session.commit()
    return intent


async def test_same_idempotency_key_returns_original_campaign_without_second_create(
    monkeypatch,
    db_session,
):
    calls: list[str] = []
    _install_successful_create(monkeypatch, calls=calls)
    client = _FakeClient()

    first = await _create(db_session, client)
    second = await _create(db_session, client)

    assert first.campaign_id == second.campaign_id == "campaign-1"
    assert calls == ["123456789012345678"]
    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert intent.state == "REMOTE_CREATED"
    assert intent.campaign_id == "campaign-1"
    assert intent.campaign_name.startswith("stable-create_i")
    assert len(intent.campaign_name.rsplit("_i", 1)[1]) == 8


async def test_same_idempotency_key_rejects_a_different_payload(
    monkeypatch,
    db_session,
):
    calls: list[str] = []
    _install_successful_create(monkeypatch, calls=calls)
    client = _FakeClient()
    await _create(db_session, client)

    with pytest.raises(TTBBusinessError) as caught:
        await _create(db_session, client, _payload(budget=300))

    assert caught.value.code == "GMVMAX_CREATE_IDEMPOTENCY_CONFLICT"
    assert calls == ["123456789012345678"]


async def test_same_idempotency_key_rejects_a_different_client_payload_hash(
    monkeypatch,
    db_session,
):
    calls: list[str] = []
    _install_successful_create(monkeypatch, calls=calls)
    client = _FakeClient()

    await _create(
        db_session,
        client,
        client_payload_sha256="a" * 64,
    )

    with pytest.raises(TTBBusinessError) as caught:
        await _create(
            db_session,
            client,
            client_payload_sha256="b" * 64,
        )

    assert caught.value.code == "GMVMAX_CREATE_IDEMPOTENCY_CONFLICT"
    assert calls == ["123456789012345678"]


async def test_conflicting_caller_cannot_terminalize_an_existing_prepared_intent(
    monkeypatch,
    db_session,
):
    post_calls = 0

    async def _must_not_post(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("a payload conflict must not reach CREATE")

    monkeypatch.setattr(service, "svc_create_campaign", _must_not_post)
    original_payload = _payload()
    intent = _seed_intent(
        db_session,
        payload=original_payload,
        state="PREPARED",
    )

    with pytest.raises(TTBBusinessError) as caught:
        await _create(
            db_session,
            _FakeClient(),
            original_payload.model_copy(update={"budget": 300}),
        )

    assert caught.value.code == "GMVMAX_CREATE_IDEMPOTENCY_CONFLICT"
    assert post_calls == 0
    db_session.refresh(intent)
    assert intent.state == "PREPARED"
    assert intent.error_json is None


@pytest.mark.parametrize(
    ("intent_state", "expected_code"),
    [
        ("UNKNOWN", "GMVMAX_CREATE_PENDING_CONFIRMATION"),
        ("CORRUPTED_STATE", "GMVMAX_CREATE_INVALID_INTENT_STATE"),
    ],
)
async def test_unresolved_or_corrupted_intent_state_never_resubmits_create(
    monkeypatch,
    db_session,
    intent_state,
    expected_code,
):
    calls = 0

    async def _must_not_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("an existing unsafe intent must never be resubmitted")

    monkeypatch.setattr(service, "svc_create_campaign", _must_not_create)
    payload = _payload()
    intent = _seed_intent(
        db_session,
        payload=payload,
        state=intent_state,
    )

    with pytest.raises(TTBBusinessError) as caught:
        await _create(db_session, _FakeClient(), payload)

    assert caught.value.code == expected_code
    assert calls == 0
    db_session.refresh(intent)
    assert intent.state == intent_state


async def test_different_key_is_blocked_by_unfinished_intent_for_same_product(
    monkeypatch,
    db_session,
):
    calls = 0

    async def _must_not_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("a second intent must not submit the same product")

    monkeypatch.setattr(service, "svc_create_campaign", _must_not_create)
    blocking_payload = _payload(idempotency_key="111111111111111111")
    blocking_intent = _seed_intent(
        db_session,
        payload=blocking_payload,
        state="SUBMITTING",
    )
    competing_payload = _payload(idempotency_key="222222222222222222")

    with pytest.raises(TTBBusinessError) as caught:
        await _create(db_session, _FakeClient(), competing_payload)

    assert caught.value.code == "GMVMAX_CREATE_ACTIVE_INTENT_EXISTS"
    assert caught.value.payload["idempotency_key"] == blocking_intent.idempotency_key
    assert calls == 0
    assert db_session.query(GmvmaxCampaignCreateIntent).count() == 1


async def test_definite_pre_submission_rejection_terminalizes_and_releases_product(
    monkeypatch,
    db_session,
):
    precheck_calls = 0

    async def _reject_once(*args, **kwargs):
        nonlocal precheck_calls
        _ = args, kwargs
        precheck_calls += 1
        if precheck_calls == 1:
            raise TTBBusinessError(
                "selected product is occupied",
                code="GMVMAX_PRODUCT_OCCUPIED",
                payload={"item_group_ids": ["product-1"]},
            )
        return None

    post_calls: list[str] = []
    _install_successful_create(monkeypatch, calls=post_calls)
    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _reject_once,
    )
    first_payload = _payload(idempotency_key="111111111111111111")

    with pytest.raises(TTBBusinessError) as caught:
        await _create(db_session, _FakeClient(), first_payload)

    assert caught.value.code == "GMVMAX_PRODUCT_OCCUPIED"
    first_intent = (
        db_session.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.idempotency_key
            == "111111111111111111"
        )
        .one()
    )
    assert first_intent.state == "FAILED_TERMINAL"
    assert first_intent.submitted_at is None
    assert first_intent.error_json["phase"] == "PRE_SUBMISSION"
    assert first_intent.error_json["request_sent"] is False
    assert post_calls == []

    corrected_payload = _payload(idempotency_key="222222222222222222")
    created = await _create(
        db_session,
        _FakeClient(),
        corrected_payload,
    )

    assert created.campaign_id == "campaign-1"
    assert precheck_calls == 2
    assert post_calls == ["222222222222222222"]
    corrected_intent = (
        db_session.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.idempotency_key
            == "222222222222222222"
        )
        .one()
    )
    assert corrected_intent.state == "REMOTE_CREATED"


async def test_rebuild_marker_without_replacement_does_not_bypass_terminal_rejection(
    monkeypatch,
    db_session,
):
    async def _occupied(*args, **kwargs):
        _ = args, kwargs
        raise TTBBusinessError(
            "selected product is occupied",
            code="GMVMAX_PRODUCT_OCCUPIED",
            payload={"item_group_ids": ["product-1"]},
        )

    post_calls = 0

    async def _must_not_post(*args, **kwargs):
        nonlocal post_calls
        _ = args, kwargs
        post_calls += 1
        raise AssertionError("an occupied precheck must not reach CREATE")

    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _occupied,
    )
    monkeypatch.setattr(service, "svc_create_campaign", _must_not_post)
    payload = _payload().model_copy(
        update={
            "automation": {"source": "creative_guard_rebuild"},
            "replacement_campaign_id": None,
        }
    )

    with pytest.raises(TTBBusinessError) as caught:
        await _create(db_session, _FakeClient(), payload)

    assert caught.value.code == "GMVMAX_PRODUCT_OCCUPIED"
    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert intent.state == "FAILED_TERMINAL"
    assert intent.submitted_at is None
    assert intent.error_json["request_sent"] is False
    assert post_calls == 0


async def test_transient_pre_submission_failure_keeps_prepared_for_same_key_retry(
    monkeypatch,
    db_session,
):
    precheck_calls = 0

    async def _timeout_once(*args, **kwargs):
        nonlocal precheck_calls
        _ = args, kwargs
        precheck_calls += 1
        if precheck_calls == 1:
            raise TTBHttpError(503, "occupancy precheck unavailable")
        return None

    post_calls: list[str] = []
    _install_successful_create(monkeypatch, calls=post_calls)
    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _timeout_once,
    )
    payload = _payload()

    with pytest.raises(TTBHttpError):
        await _create(db_session, _FakeClient(), payload)

    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert intent.state == "PREPARED"
    assert intent.submitted_at is None
    assert post_calls == []

    created = await _create(db_session, _FakeClient(), payload)

    assert created.campaign_id == "campaign-1"
    assert precheck_calls == 2
    assert post_calls == ["123456789012345678"]
    assert db_session.query(GmvmaxCampaignCreateIntent).count() == 1
    db_session.refresh(intent)
    assert intent.state == "REMOTE_CREATED"


async def test_same_key_concurrent_submit_is_rechecked_before_post(
    monkeypatch,
    db_session,
):
    payload = _payload()
    intent = _seed_intent(
        db_session,
        payload=payload,
        state="PREPARED",
    )
    post_calls = 0

    async def _concurrent_submit_wins(*_args, **_kwargs):
        intent.state = "SUBMITTING"
        intent.submitted_at = datetime(2026, 7, 18, 9, 18)
        db_session.add(intent)
        db_session.commit()
        return None

    async def _must_not_post(*_args, **_kwargs):
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("a concurrent same-key submit must fence this POST")

    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _concurrent_submit_wins,
    )
    monkeypatch.setattr(service, "svc_create_campaign", _must_not_post)

    with pytest.raises(TTBBusinessError) as caught:
        await _create(db_session, _FakeClient(), payload)

    assert caught.value.code == "GMVMAX_CREATE_PENDING_CONFIRMATION"
    assert post_calls == 0
    db_session.refresh(intent)
    assert intent.state == "SUBMITTING"


async def test_prepare_create_intent_persists_without_network_or_post(
    monkeypatch,
    db_session,
):
    async def _forbidden_network(*_args, **_kwargs):
        raise AssertionError("prepare must not perform network work")

    monkeypatch.setattr(service, "svc_create_campaign", _forbidden_network)
    monkeypatch.setattr(
        service,
        "ensure_gmvmax_store_authorized",
        _forbidden_network,
    )
    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _forbidden_network,
    )
    monkeypatch.setattr(
        service,
        "_resolve_product_identity_list",
        _forbidden_network,
    )
    guard_calls = 0

    def execution_guard(_db):
        nonlocal guard_calls
        guard_calls += 1

    prepared = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=_payload(),
        store_authorized_bc_id="bc-1",
        execution_guard=execution_guard,
    )

    assert prepared.created is True
    assert prepared.intent.state == "PREPARED"
    assert prepared.intent.submitted_at is None
    assert prepared.intent.campaign_id is None
    assert prepared.frozen_payload.advertiser_id == "advertiser-1"
    assert prepared.body.request_id == prepared.intent.official_request_id
    assert prepared.body.campaign_name == prepared.intent.campaign_name
    assert prepared.body.campaign_name.startswith("stable-create_i")
    assert guard_calls == 1
    persisted = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert persisted.id == prepared.intent.id
    assert persisted.request_json["advertiser_id"] == "advertiser-1"


async def test_prepare_locks_tenant_parent_before_active_intent_check(
    monkeypatch,
    db_session,
):
    account = object()
    refresh_calls: list[tuple[object, bool | None]] = []
    original_refresh = db_session.refresh

    monkeypatch.setattr(
        service,
        "ensure_ttb_auth_in_workspace",
        lambda *args, **kwargs: account,
    )

    def tracked_refresh(instance, *args, **kwargs):
        if instance is account:
            refresh_calls.append((instance, kwargs.get("with_for_update")))
            return None
        return original_refresh(instance, *args, **kwargs)

    monkeypatch.setattr(db_session, "refresh", tracked_refresh)

    service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=_payload(),
        store_authorized_bc_id="bc-1",
    )

    assert refresh_calls == [(account, True)]


async def test_prepare_existing_intent_returns_frozen_request_without_state_change(
    db_session,
):
    payload = _payload()
    first = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )
    first.intent.state = "QUARANTINED"
    first.intent.result_json = {
        "rebuild_workflow": {"phase": "QUARANTINED"},
    }
    db_session.commit()

    resumed = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )

    assert resumed.created is False
    assert resumed.intent.id == first.intent.id
    assert resumed.intent.state == "QUARANTINED"
    assert resumed.frozen_payload.model_dump(
        mode="json", exclude_none=True
    ) == first.frozen_payload.model_dump(mode="json", exclude_none=True)
    assert resumed.body.request_id == first.body.request_id
    assert db_session.query(GmvmaxCampaignCreateIntent).count() == 1


async def test_prepared_intent_blocks_different_key_for_overlapping_product(
    db_session,
):
    first = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=_payload(idempotency_key="111111111111111111"),
        store_authorized_bc_id="bc-1",
    )

    with pytest.raises(TTBBusinessError) as caught:
        service.prepare_gmvmax_create_intent(
            db_session,
            workspace_id=1,
            provider="tiktok-business",
            auth_id=2,
            advertiser_id="advertiser-1",
            payload=_payload(idempotency_key="222222222222222222"),
            store_authorized_bc_id="bc-1",
        )

    assert first.intent.state == "PREPARED"
    assert caught.value.code == "GMVMAX_CREATE_ACTIVE_INTENT_EXISTS"
    assert caught.value.payload["idempotency_key"] == first.intent.idempotency_key
    assert db_session.query(GmvmaxCampaignCreateIntent).count() == 1


async def test_prepare_same_key_isolated_by_full_tenant_scope(db_session):
    payload = _payload()
    first = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )
    second = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload.model_copy(update={"store_id": "store-2"}),
        store_authorized_bc_id="bc-1",
    )
    third = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-2",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )
    fourth = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=3,
        advertiser_id="advertiser-1",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )
    fifth = service.prepare_gmvmax_create_intent(
        db_session,
        workspace_id=2,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )

    assert {
        first.intent.id,
        second.intent.id,
        third.intent.id,
        fourth.intent.id,
        fifth.intent.id,
    } == {
        row.id
        for row in db_session.query(GmvmaxCampaignCreateIntent).all()
    }
    assert {
        (
            row.workspace_id,
            row.auth_id,
            row.advertiser_id,
            row.store_id,
        )
        for row in db_session.query(GmvmaxCampaignCreateIntent).all()
    } == {
        (1, 2, "advertiser-1", "store-1"),
        (1, 2, "advertiser-1", "store-2"),
        (1, 2, "advertiser-2", "store-1"),
        (1, 3, "advertiser-1", "store-1"),
        (2, 2, "advertiser-1", "store-1"),
    }


async def test_prepare_recovers_same_key_unique_insert_race(
    monkeypatch,
    db_session,
):
    payload = _payload()
    existing = _seed_intent(
        db_session,
        payload=payload,
        state="PREPARED",
    )
    load_calls = 0

    def raced_load(*_args, **_kwargs):
        nonlocal load_calls
        load_calls += 1
        return None if load_calls == 1 else existing

    class _ActiveQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def with_for_update(self, *_args, **_kwargs):
            return self

        def all(self):
            return []

    class _RaceSession:
        def __init__(self):
            self.rollbacks = 0
            self.added = []

        def query(self, *_args, **_kwargs):
            return _ActiveQuery()

        def add(self, value):
            self.added.append(value)

        def commit(self):
            raise IntegrityError(
                "insert create intent",
                {},
                RuntimeError("duplicate unique key"),
            )

        def rollback(self):
            self.rollbacks += 1

    race_db = _RaceSession()
    monkeypatch.setattr(service, "_load_create_intent", raced_load)

    prepared = service.prepare_gmvmax_create_intent(
        race_db,
        workspace_id=1,
        provider="tiktok-business",
        auth_id=2,
        advertiser_id="advertiser-1",
        payload=payload,
        store_authorized_bc_id="bc-1",
    )

    assert prepared.created is False
    assert prepared.intent is existing
    assert load_calls == 2
    assert race_db.rollbacks == 1
    assert len(race_db.added) == 1


async def test_mark_create_intent_merges_result_unless_replace_is_explicit(
    db_session,
):
    intent = _seed_intent(
        db_session,
        payload=_payload(),
        state="PREPARED",
    )
    intent.result_json = {
        "rebuild_workflow": {"phase": "OLD_PAUSED"},
        "wire_request": {"request_id": "123"},
        "context": {"source": "creative_guard"},
    }
    db_session.commit()

    merged = service.mark_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        idempotency_key=intent.idempotency_key,
        state="REMOTE_CREATED",
        campaign_id="campaign-1",
        result_json={"campaign_id": "campaign-1"},
    )

    assert merged is intent
    assert merged.result_json == {
        "rebuild_workflow": {"phase": "OLD_PAUSED"},
        "wire_request": {"request_id": "123"},
        "context": {"source": "creative_guard"},
        "campaign_id": "campaign-1",
    }

    replaced = service.mark_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        idempotency_key=intent.idempotency_key,
        state="QUARANTINED",
        result_json={"replacement": True},
        replace_result_json=True,
    )
    assert replaced is intent
    assert replaced.result_json == {"replacement": True}


async def test_create_service_delegates_durable_prepare(
    monkeypatch,
    db_session,
):
    calls: list[str] = []
    _install_successful_create(monkeypatch, calls=calls)
    original_prepare = service.prepare_gmvmax_create_intent
    prepare_calls = 0

    def tracked_prepare(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(
        service,
        "prepare_gmvmax_create_intent",
        tracked_prepare,
    )

    row = await _create(db_session, _FakeClient())

    assert row.campaign_id == "campaign-1"
    assert prepare_calls == 1
    assert calls == ["123456789012345678"]


async def test_mark_create_intent_advances_updated_at(db_session):
    payload = _payload()
    intent = _seed_intent(
        db_session,
        payload=payload,
        state="PREPARED",
    )
    old_updated_at = datetime(2020, 1, 1, 0, 0, 0)
    intent.updated_at = old_updated_at
    db_session.commit()

    marked = service.mark_gmvmax_create_intent(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        idempotency_key=str(payload.idempotency_key),
        state="SUBMITTING",
    )
    assert marked is not None
    db_session.commit()
    db_session.refresh(intent)

    assert intent.state == "SUBMITTING"
    assert intent.updated_at > old_updated_at
    assert intent.submitted_at == intent.updated_at


async def test_unknown_remote_outcome_is_never_resubmitted(
    monkeypatch,
    db_session,
):
    calls = 0

    async def _ambiguous_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TTBHttpError(503, "timeout after submission")

    monkeypatch.setattr(service, "svc_create_campaign", _ambiguous_create)
    client = _FakeClient()

    with pytest.raises(TTBBusinessError) as first_error:
        await _create(db_session, client)
    assert first_error.value.code == "GMVMAX_CREATE_OUTCOME_UNKNOWN"

    with pytest.raises(TTBBusinessError) as retry_error:
        await _create(db_session, client)
    assert retry_error.value.code == "GMVMAX_CREATE_PENDING_CONFIRMATION"
    assert calls == 1
    assert db_session.query(GmvmaxCampaignCreateIntent).one().state == "UNKNOWN"


async def test_creative_guard_rebuild_timeout_reuses_intent_without_second_post(
    monkeypatch,
    db_session,
):
    post_calls = 0

    async def _ambiguous_create(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        raise TTBHttpError(503, "timeout after creative guard rebuild submission")

    monkeypatch.setattr(service, "svc_create_campaign", _ambiguous_create)
    scope = gmvmax_creative_guard.CampaignScope(
        strategy_id=9,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-old",
        campaign_name="Old campaign",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_cents=20_000,
        roas_bid=None,
        config={},
        monitor_state={},
        smart_guard_state={},
    )
    body = GMVMaxCampaignCreateBody(
        store_id=scope.store_id,
        store_authorized_bc_id="bc-1",
        shopping_ads_type="PRODUCT",
        optimization_goal="VALUE",
        campaign_name="Creative guard replacement",
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["product-1"],
        budget=200,
        roas_bid=1.2,
    )
    client = _FakeClient()

    first_payload = gmvmax_creative_guard._durable_rebuild_create_request(
        db_session,
        scope,
        body,
    )
    with pytest.raises(TTBBusinessError) as first_error:
        await _create(db_session, client, first_payload)
    assert first_error.value.code == "GMVMAX_CREATE_OUTCOME_UNKNOWN"

    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert intent.state == "UNKNOWN"
    assert intent.replacement_campaign_id == scope.campaign_id
    assert intent.request_json["automation"]["source"] == "creative_guard_rebuild"

    retry_payload = gmvmax_creative_guard._durable_rebuild_create_request(
        db_session,
        scope,
        body,
    )
    assert retry_payload.model_dump(mode="json", exclude_none=True) == (
        first_payload.model_dump(mode="json", exclude_none=True)
    )

    with pytest.raises(TTBBusinessError) as retry_error:
        await _create(db_session, client, retry_payload)
    assert retry_error.value.code == "GMVMAX_CREATE_PENDING_CONFIRMATION"
    assert post_calls == 1
    assert db_session.query(GmvmaxCampaignCreateIntent).count() == 1
    db_session.refresh(intent)
    assert intent.state == "UNKNOWN"


async def test_creative_guard_retries_occupied_precheck_then_posts_create_once(
    monkeypatch,
    db_session,
):
    """A disabled source may need several official occupancy reads to converge."""

    scope = gmvmax_creative_guard.CampaignScope(
        strategy_id=9,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        campaign_id="campaign-old",
        campaign_name="Old campaign",
        operation_status="DISABLE",
        secondary_status="CAMPAIGN_STATUS_DISABLE",
        budget_cents=20_000,
        roas_bid=None,
        config={},
        monitor_state={},
        smart_guard_state={},
    )
    db_session.add(
        GmvmaxProductCampaignCatalog(
            workspace_id=1,
            auth_id=2,
            advertiser_id="advertiser-1",
            store_id="store-1",
            campaign_id="campaign-old",
            campaign_name="Old campaign",
            operation_status="DISABLE",
            secondary_status="CAMPAIGN_STATUS_DISABLE",
            shopping_ads_type="PRODUCT",
            product_specific_type="CUSTOMIZED_PRODUCTS",
            detail_raw_json={"item_group_ids": ["product-1"]},
        )
    )
    db_session.commit()

    body = GMVMaxCampaignCreateBody(
        store_id=scope.store_id,
        store_authorized_bc_id="bc-1",
        shopping_ads_type="PRODUCT",
        optimization_goal="VALUE",
        campaign_name="Creative guard replacement",
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["product-1"],
        budget=200,
        roas_bid=1.2,
    )
    payload = gmvmax_creative_guard._durable_rebuild_create_request(
        db_session,
        scope,
        body,
    )
    assert payload.automation["source"] == "creative_guard_rebuild"
    assert payload.replacement_campaign_id == "campaign-old"
    precheck_states: list[str] = []

    async def _official_occupancy_precheck(*args, **kwargs):
        _ = args, kwargs
        old = (
            db_session.query(GmvmaxProductCampaignCatalog)
            .filter(
                GmvmaxProductCampaignCatalog.campaign_id == "campaign-old",
            )
            .one()
        )
        assert old.operation_status == "DISABLE"
        intent = db_session.query(GmvmaxCampaignCreateIntent).one()
        precheck_states.append(str(intent.state))
        if len(precheck_states) <= 2:
            raise TTBBusinessError(
                "product is still occupied while TikTok converges",
                code="GMVMAX_PRODUCT_OCCUPIED",
                payload={"item_group_ids": ["product-1"]},
            )
        return None

    post_calls: list[str] = []
    _install_successful_create(monkeypatch, calls=post_calls)
    monkeypatch.setattr(
        service,
        "_ensure_create_payload_conflict_free",
        _official_occupancy_precheck,
    )

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(
        gmvmax_creative_guard,
        "asyncio",
        type("_Asyncio", (), {"sleep": staticmethod(_no_sleep)})(),
    )

    row = await gmvmax_creative_guard._create_rebuild_after_occupancy_converges(
        db_session,
        scope,
        request=payload,
        store_authorized_bc_id="bc-1",
        client=_FakeClient(),
        mutation=type(
            "_Mutation",
            (),
            {"assert_current": staticmethod(lambda db: None)},
        )(),
    )

    assert row.campaign_id == "campaign-1"
    assert precheck_states == ["PREPARED", "PREPARED", "PREPARED"]
    assert post_calls == [str(payload.request_id)]
    assert db_session.query(GmvmaxCampaignCreateIntent).count() == 1
    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert intent.state == "REMOTE_CREATED"
    assert intent.campaign_id == "campaign-1"


async def test_ambiguous_create_immediately_reconciles_marker_without_second_post(
    monkeypatch,
    db_session,
):
    calls = 0

    async def _ambiguous_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TTBHttpError(503, "timeout after submission")

    monkeypatch.setattr(service, "svc_create_campaign", _ambiguous_create)
    payload = _payload()
    marker_name = service._intent_campaign_name(
        payload.campaign_name,
        str(payload.idempotency_key),
    )
    client = _FakeClient()
    client.listed_campaigns = [
        GMVMaxCampaign(
            campaign_id="campaign-immediate",
            campaign_name=marker_name,
            operation_status="ENABLE",
        )
    ]
    client.info = GMVMaxCampaignInfoData(
        campaign_id="campaign-immediate",
        campaign_name=marker_name,
        advertiser_id="advertiser-1",
        store_id="store-1",
        shopping_ads_type="PRODUCT",
        operation_status="ENABLE",
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["product-1"],
    )

    recovered = await _create(db_session, client, payload)

    assert recovered.campaign_id == "campaign-immediate"
    assert calls == 1
    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    assert intent.state == "REMOTE_CREATED"
    assert intent.campaign_id == "campaign-immediate"
    assert intent.result_json["reconciled_after_ambiguous_create"] is True


async def test_unknown_outcome_recovers_by_unique_official_campaign_marker(
    monkeypatch,
    db_session,
):
    calls = 0

    async def _ambiguous_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TTBHttpError(503, "timeout after submission")

    monkeypatch.setattr(service, "svc_create_campaign", _ambiguous_create)
    client = _FakeClient()

    with pytest.raises(TTBBusinessError):
        await _create(db_session, client)

    intent = db_session.query(GmvmaxCampaignCreateIntent).one()
    client.listed_campaigns = [
        GMVMaxCampaign(
            campaign_id="campaign-recovered",
            campaign_name=intent.campaign_name,
            operation_status="ENABLE",
        )
    ]
    client.info = GMVMaxCampaignInfoData(
        campaign_id="campaign-recovered",
        campaign_name=intent.campaign_name,
        advertiser_id="advertiser-1",
        store_id="store-1",
        shopping_ads_type="PRODUCT",
        operation_status="ENABLE",
        product_specific_type="CUSTOMIZED_PRODUCTS",
        item_group_ids=["product-1"],
    )

    recovered = await _create(db_session, client)

    assert recovered.campaign_id == "campaign-recovered"
    assert calls == 1
    db_session.refresh(intent)
    assert intent.state == "REMOTE_CREATED"
    assert intent.campaign_id == "campaign-recovered"
