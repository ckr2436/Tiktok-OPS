from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.data.models.gmv_restructured import PromotionTypeEnum
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.gmvmax.services.campaign_catalog_freshness import (
    catalog_observation_now,
    stamp_catalog_row_observation,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCampaign,
    GMVMaxCampaignInfoData,
    GMVMaxCampaignListData,
    GMVMaxResponse,
    PageInfo,
)
from app.services import ttb_gmvmax
from app.services.ttb_gmvmax import sync_gmvmax_campaigns, upsert_campaign_from_api


def _payload(
    status: str,
    *,
    store_id: str = "store-1",
    item_group_ids: list[str] | None = None,
) -> dict:
    payload = {
        "campaign_id": "campaign-shared",
        "campaign_name": f"campaign-{status.lower()}",
        "shopping_ads_type": "PRODUCT",
        "store_id": store_id,
        "operation_status": status,
        "secondary_status": f"CAMPAIGN_STATUS_{status}",
    }
    if item_group_ids is not None:
        payload["item_group_ids"] = item_group_ids
    return payload


def _upsert(
    db_session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    payload: dict,
    observed_at: datetime,
    campaign_details: dict | None = None,
    campaign_details_complete: bool = False,
):
    details = dict(payload) if campaign_details is None else campaign_details
    return upsert_campaign_from_api(
        db_session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        payload=payload,
        store_id_hint=payload.get("store_id"),
        campaign_details=details,
        campaign_details_complete=campaign_details_complete,
        promotion_type=PromotionTypeEnum.PRODUCT,
        source_observed_at=observed_at,
    )


def test_old_sync_response_cannot_overwrite_later_local_disable_or_add_items(
    db_session,
) -> None:
    """A request that started first must lose even when its response arrives last."""

    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    old_sync_started_at = baseline + timedelta(seconds=10)
    mutation_completed_at = baseline + timedelta(seconds=20)

    row = _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload("ENABLE"),
        observed_at=baseline,
    )
    db_session.commit()

    # The official DISABLE succeeds while the older ENABLE request is in
    # flight.  Manual/Guard paths stamp this completion boundary.
    row.operation_status = "DISABLE"
    row.secondary_status = "CAMPAIGN_STATUS_DISABLE"
    row.modify_time_utc = mutation_completed_at.replace(tzinfo=None)
    stamp_catalog_row_observation(row, mutation_completed_at)
    db_session.commit()

    stale = _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload(
            "ENABLE",
            store_id="stale-store",
            item_group_ids=["stale-item"],
        ),
        observed_at=old_sync_started_at,
    )
    db_session.commit()

    # DATETIME(6) can collapse two boundaries to the same microsecond.  The
    # already persisted authority must win that tie as well.
    stale = _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload(
            "ENABLE",
            store_id="equal-time-stale-store",
            item_group_ids=["equal-time-stale-item"],
        ),
        observed_at=mutation_completed_at,
    )
    db_session.commit()

    db_session.refresh(stale)
    assert stale.operation_status == "DISABLE"
    assert stale.secondary_status == "CAMPAIGN_STATUS_DISABLE"
    assert stale.store_id == "store-1"
    assert stale.list_synced_at == mutation_completed_at.replace(tzinfo=None)
    assert stale.detail_synced_at == mutation_completed_at.replace(tzinfo=None)
    assert (
        db_session.query(GmvmaxProductCampaignItemGroup)
        .filter(
            GmvmaxProductCampaignItemGroup.workspace_id == 1,
            GmvmaxProductCampaignItemGroup.auth_id == 2,
            GmvmaxProductCampaignItemGroup.advertiser_id == "adv-1",
            GmvmaxProductCampaignItemGroup.campaign_id == "campaign-shared",
        )
        .count()
        == 0
    )


def test_in_flight_sync_started_before_disable_cannot_win_on_late_arrival(
    db_session,
    monkeypatch,
) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload("ENABLE"),
        observed_at=baseline,
    )
    db_session.commit()

    class _LateOldSnapshotClient:
        mutation_applied = False

        async def gmv_max_campaign_get(self, request):  # noqa: ANN001
            primary_status = (
                request.filtering.primary_status if request.filtering else None
            )
            if primary_status == "STATUS_DELETE":
                data = GMVMaxCampaignListData(
                    list=[],
                    page_info=PageInfo(page=1, total_page=1),
                )
                return GMVMaxResponse(code=0, message="OK", data=data)

            # The sync request is already in flight.  Complete a later
            # successful local DISABLE before releasing its old ENABLE body.
            if not self.mutation_applied:
                row = db_session.query(GmvmaxProductCampaignCatalog).one()
                row.operation_status = "DISABLE"
                row.secondary_status = "CAMPAIGN_STATUS_DISABLE"
                completed_at = catalog_observation_now()
                row.modify_time_utc = completed_at
                stamp_catalog_row_observation(row, completed_at)
                db_session.commit()
                self.mutation_applied = True

            data = GMVMaxCampaignListData(
                list=[
                    GMVMaxCampaign(
                        campaign_id="campaign-shared",
                        advertiser_id="adv-1",
                        store_id="store-1",
                        shopping_ads_type="PRODUCT",
                        operation_status="ENABLE",
                        secondary_status="CAMPAIGN_STATUS_ENABLE",
                        item_group_ids=["late-stale-item"],
                    )
                ],
                page_info=PageInfo(page=1, total_page=1),
            )
            return GMVMaxResponse(code=0, message="OK", data=data)

        async def gmv_max_campaign_info(self, request):  # noqa: ANN001
            return GMVMaxResponse(
                code=0,
                message="OK",
                data=GMVMaxCampaignInfoData(
                    advertiser_id=request.advertiser_id,
                    campaign_id=request.campaign_id,
                    store_id="store-1",
                    shopping_ads_type="PRODUCT",
                    operation_status="ENABLE",
                    secondary_status="CAMPAIGN_STATUS_ENABLE",
                    item_group_ids=["late-stale-item"],
                ),
            )

    client = _LateOldSnapshotClient()
    reconciled: list[dict] = []
    monkeypatch.setattr(
        ttb_gmvmax,
        "reconcile_manual_pause_from_official_catalog",
        lambda *_, **kwargs: reconciled.append(kwargs),
    )
    asyncio.run(
        sync_gmvmax_campaigns(
            db_session,
            client,
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_ids=["store-1"],
        )
    )
    db_session.commit()

    row = db_session.query(GmvmaxProductCampaignCatalog).one()
    assert client.mutation_applied is True
    assert row.operation_status == "DISABLE"
    assert row.secondary_status == "CAMPAIGN_STATUS_DISABLE"
    assert db_session.query(GmvmaxProductCampaignItemGroup).count() == 0
    assert reconciled == []


def test_newer_sync_updates_catalog_and_item_groups(db_session) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    row = _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload("DISABLE"),
        observed_at=baseline,
    )
    db_session.commit()

    newer = baseline + timedelta(minutes=1)
    row = _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload("ENABLE", item_group_ids=["fresh-item"]),
        observed_at=newer,
    )
    db_session.commit()

    db_session.refresh(row)
    assert row.operation_status == "ENABLE"
    assert row.list_synced_at == newer.replace(tzinfo=None)
    relation = db_session.query(GmvmaxProductCampaignItemGroup).one()
    assert relation.workspace_id == 1
    assert relation.auth_id == 2
    assert relation.advertiser_id == "adv-1"
    assert relation.store_id == "store-1"
    assert relation.item_group_id == "fresh-item"


def test_create_placeholder_store_uses_validated_hint_and_removes_stale_relations(
    db_session,
) -> None:
    baseline = datetime(2026, 7, 18, 1, 18, 15, tzinfo=timezone.utc)
    db_session.add(
        GmvmaxProductCampaignItemGroup(
            workspace_id=1,
            auth_id=2,
            advertiser_id="adv-1",
            store_id="0",
            campaign_id="campaign-shared",
            item_group_id="item-a",
        )
    )
    db_session.flush()

    payload = _payload(
        "ENABLE",
        store_id="0",
        item_group_ids=["item-a"],
    )
    row = upsert_campaign_from_api(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=payload,
        store_id_hint="store-validated",
        campaign_details=dict(payload),
        promotion_type=PromotionTypeEnum.PRODUCT,
        source_observed_at=baseline,
    )
    db_session.commit()

    assert row.store_id == "store-validated"
    relations = db_session.query(GmvmaxProductCampaignItemGroup).all()
    assert [
        (relation.store_id, relation.campaign_id, relation.item_group_id)
        for relation in relations
    ] == [("store-validated", "campaign-shared", "item-a")]


def test_placeholder_without_hint_preserves_existing_canonical_store(
    db_session,
) -> None:
    baseline = datetime(2026, 7, 18, 1, 18, 15, tzinfo=timezone.utc)
    initial = _payload(
        "ENABLE",
        store_id="store-canonical",
        item_group_ids=["item-a"],
    )
    row = _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=initial,
        observed_at=baseline,
    )
    db_session.commit()

    placeholder = _payload(
        "ENABLE",
        store_id="0",
        item_group_ids=["item-a"],
    )
    row = upsert_campaign_from_api(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=placeholder,
        store_id_hint=None,
        campaign_details=dict(placeholder),
        promotion_type=PromotionTypeEnum.PRODUCT,
        source_observed_at=baseline + timedelta(minutes=1),
    )
    db_session.commit()

    assert row.store_id == "store-canonical"
    assert {
        relation.store_id
        for relation in db_session.query(GmvmaxProductCampaignItemGroup).all()
    } == {"store-canonical"}


def test_trusted_create_store_scope_wins_over_mismatched_response(
    db_session,
) -> None:
    payload = _payload(
        "ENABLE",
        store_id="store-unexpected",
        item_group_ids=["item-a"],
    )
    row = ttb_gmvmax._upsert_campaign_catalog_from_api(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=payload,
        store_id_hint="store-validated",
        trusted_store_id_hint=True,
        campaign_details=dict(payload),
        promotion_type=PromotionTypeEnum.PRODUCT,
        source_observed_at=datetime(2026, 7, 18, 1, 18, 15, tzinfo=timezone.utc),
    )
    db_session.commit()

    assert row.store_id == "store-validated"
    relation = db_session.query(GmvmaxProductCampaignItemGroup).one()
    assert relation.store_id == "store-validated"


def test_catalog_freshness_fence_is_exactly_tenant_scoped(db_session) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    future = baseline + timedelta(hours=1)
    for workspace_id, status, observed_at in (
        (1, "DISABLE", future),
        (2, "DISABLE", baseline),
    ):
        _upsert(
            db_session,
            workspace_id=workspace_id,
            auth_id=2,
            advertiser_id="adv-1",
            payload=_payload(status),
            observed_at=observed_at,
        )
    db_session.commit()

    # Workspace 1's future fence must not block the same official identifiers
    # in workspace 2.
    _upsert(
        db_session,
        workspace_id=2,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload("ENABLE"),
        observed_at=baseline + timedelta(minutes=1),
    )
    db_session.commit()

    rows = (
        db_session.query(GmvmaxProductCampaignCatalog)
        .order_by(GmvmaxProductCampaignCatalog.workspace_id)
        .all()
    )
    assert [(row.workspace_id, row.operation_status) for row in rows] == [
        (1, "DISABLE"),
        (2, "ENABLE"),
    ]


def test_partial_campaign_detail_never_deletes_existing_item_groups(
    db_session,
) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    initial = _payload("ENABLE", item_group_ids=["item-a", "item-b"])
    initial["product_specific_type"] = "CUSTOMIZED_PRODUCTS"
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=initial,
        observed_at=baseline,
        campaign_details=dict(initial),
        campaign_details_complete=True,
    )
    db_session.commit()

    # campaign/info promises item_group_ids for CUSTOMIZED_PRODUCTS.  Its
    # absence therefore means the body is incomplete, not an empty snapshot.
    partial_details = {
        "campaign_id": "campaign-shared",
        "store_id": "store-1",
        "product_specific_type": "CUSTOMIZED_PRODUCTS",
    }
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=_payload("ENABLE"),
        observed_at=baseline + timedelta(minutes=1),
        campaign_details=partial_details,
        campaign_details_complete=True,
    )
    db_session.commit()

    assert {
        row.item_group_id
        for row in db_session.query(GmvmaxProductCampaignItemGroup).all()
    } == {"item-a", "item-b"}


def test_complete_explicit_empty_item_group_snapshot_clears_relations(
    db_session,
) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    initial = _payload("ENABLE", item_group_ids=["item-a", "item-b"])
    initial["product_specific_type"] = "CUSTOMIZED_PRODUCTS"
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=initial,
        observed_at=baseline,
        campaign_details=dict(initial),
        campaign_details_complete=True,
    )
    db_session.commit()

    complete_empty = _payload("ENABLE", item_group_ids=[])
    complete_empty["product_specific_type"] = "CUSTOMIZED_PRODUCTS"
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=complete_empty,
        observed_at=baseline + timedelta(minutes=1),
        campaign_details=dict(complete_empty),
        campaign_details_complete=True,
    )
    db_session.commit()

    assert db_session.query(GmvmaxProductCampaignItemGroup).count() == 0


def test_complete_all_products_detail_clears_explicit_relations(db_session) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    initial = _payload("ENABLE", item_group_ids=["formerly-explicit"])
    initial["product_specific_type"] = "CUSTOMIZED_PRODUCTS"
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=initial,
        observed_at=baseline,
        campaign_details=dict(initial),
        campaign_details_complete=True,
    )
    db_session.commit()

    # The official campaign/info contract omits item_group_ids for ALL.
    all_products = _payload("ENABLE")
    all_products["product_specific_type"] = "ALL"
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-1",
        payload=all_products,
        observed_at=baseline + timedelta(minutes=1),
        campaign_details=dict(all_products),
        campaign_details_complete=True,
    )
    db_session.commit()

    assert db_session.query(GmvmaxProductCampaignItemGroup).count() == 0


def test_complete_item_group_cleanup_does_not_cross_compound_scope(
    db_session,
) -> None:
    baseline = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    for advertiser_id, store_id, item_group_id in (
        ("adv-target", "store-target", "target-item"),
        ("adv-other", "store-other", "other-item"),
    ):
        initial = _payload(
            "ENABLE",
            store_id=store_id,
            item_group_ids=[item_group_id],
        )
        initial["product_specific_type"] = "CUSTOMIZED_PRODUCTS"
        _upsert(
            db_session,
            workspace_id=1,
            auth_id=2,
            advertiser_id=advertiser_id,
            payload=initial,
            observed_at=baseline,
            campaign_details=dict(initial),
            campaign_details_complete=True,
        )
    db_session.commit()

    complete_empty = _payload(
        "ENABLE",
        store_id="store-target",
        item_group_ids=[],
    )
    complete_empty["product_specific_type"] = "CUSTOMIZED_PRODUCTS"
    _upsert(
        db_session,
        workspace_id=1,
        auth_id=2,
        advertiser_id="adv-target",
        payload=complete_empty,
        observed_at=baseline + timedelta(minutes=1),
        campaign_details=dict(complete_empty),
        campaign_details_complete=True,
    )
    db_session.commit()

    remaining = db_session.query(GmvmaxProductCampaignItemGroup).one()
    assert remaining.advertiser_id == "adv-other"
    assert remaining.store_id == "store-other"
    assert remaining.item_group_id == "other-item"
