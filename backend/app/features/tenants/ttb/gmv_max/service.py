from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import logging
"""Provider-scoped service helpers bridging GMV Max routers to core services."""

from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.gmvmax_campaign_catalog import (
    GmvmaxCampaignCreateIntent,
    GmvmaxProductCampaignCatalog,
    GmvmaxProductCampaignItemGroup,
)
from app.data.models.ttb_gmvmax import (
    TTBGmvMaxCampaign,
    TTBGmvMaxMetricsDaily,
    TTBGmvMaxMetricsHourly,
)
from app.data.models.ttb_entities import TTBAdvertiser
from app.data.repositories.tiktok_business.gmvmax import list_gmvmax_campaigns
from app.services.ttb_gmvmax import (
    create_gmvmax_campaign as svc_create_campaign,
    ensure_gmvmax_store_authorized,
    sync_gmvmax_campaigns as svc_sync_campaigns,
    upsert_campaign_from_api,
)
from app.services.ttb_client_factory import (
    build_ttb_client,
    build_ttb_gmvmax_client,
)
from app.services.ttb_api import (
    TTBApiClient,
    TTBApiError,
    TTBBusinessError,
    TTBHttpError,
    TTBRateLimitBudgetError,
)
from app.services.gmvmax_creative_assets import (
    build_gmvmax_identity_filter,
    iter_gmvmax_custom_anchor_entries,
    iter_gmvmax_video_entries,
)
from app.gmvmax.services.report_pagination import (
    NumberedPaginationError,
    iter_numbered_pages,
)
from app.gmvmax.services.sync_execution_lock import (
    acquire_account_sync_fence,
    build_account_sync_lock,
    release_account_sync_fence,
)

from ._helpers import (
    ensure_ttb_auth_in_workspace,
    get_advertiser_id_for_account,
    get_gmvmax_client_for_account,
    get_ttb_client_for_account,
    normalize_provider,
)
from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxBidRecommendRequest,
    GMVMaxCampaignCreateBody,
    GMVMaxCampaignFiltering,
    GMVMaxCampaignGetRequest,
    GMVMaxCampaignInfoRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxIdentityInfo,
    GMVMaxResponse,
    GMVMaxStoreAdUsageCheckRequest,
    GMVMaxStoreListRequest,
    TikTokBusinessGMVMaxClient,
    fetch_all_occupied_custom_shop_ads,
)

from .schemas import (
    CreateCampaignRequest,
    CustomAnchorVideoSummary,
    GMVMaxPrecheckRequest,
    GMVMaxPrecheckResponse,
    IdentitySummary,
    OccupiedAdSummary,
    VideoSummary,
)

logger = logging.getLogger("gmv.tenants.gmvmax.precheck")
PRODUCT_GMV_MAX_IDENTITY_LIMIT = 20


def _get_advertiser_timezone(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
) -> str | None:
    row = (
        db.query(TTBAdvertiser.display_timezone, TTBAdvertiser.timezone)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .filter(TTBAdvertiser.advertiser_id == str(advertiser_id))
        .order_by(TTBAdvertiser.last_seen_at.desc())
        .first()
    )
    if not row:
        return None
    return row.display_timezone or row.timezone


def _ensure_provider(provider: str) -> str:
    return normalize_provider(provider)


def _order_desc_nulls_last(col):
    """
    Vendor-agnostic 等价实现：ORDER BY col DESC NULLS LAST
    在 MySQL/MariaDB 上编译为：
      ORDER BY (col IS NULL) ASC, col DESC
    在支持 NULLS LAST 的方言上也安全。
    """
    return [
        case((col.is_(None), 1), else_=0).asc(),
        col.desc(),
    ]


async def _check_customized_products_conflict(
    *,
    ttb_client: TTBApiClient,
    campaign: TTBGmvMaxCampaign,
    advertiser_id: str,
    bc_id: str | None,
    item_group_ids: list[str],
) -> None:
    products = await ttb_client.get_store_products_for_gmvmax_item_group_ids(
        bc_id=bc_id,
        store_id=str(campaign.store_id or ""),
        advertiser_id=str(advertiser_id),
        item_group_ids=item_group_ids,
    )

    status_map = {
        str(product.get("item_group_id")): {
            "status": product.get("status"),
            "gmv_max_ads_status": product.get("gmv_max_ads_status"),
        }
        for product in products
        if product.get("item_group_id")
    }

    conflicting: list[str] = []
    for spu_id in item_group_ids:
        info = status_map.get(spu_id)
        if not info:
            conflicting.append(spu_id)
            continue

        if not (
            info.get("status") == "AVAILABLE"
            and info.get("gmv_max_ads_status") == "UNOCCUPIED"
        ):
            conflicting.append(spu_id)

    if conflicting:
        raise TTBBusinessError(
            "Some SPUs are occupied by other Product GMV Max campaigns",
            code="GMVMAX_PRODUCT_OCCUPIED",
            payload={"item_group_ids": conflicting},
        )


def _ensure_campaign(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = None,
) -> TTBGmvMaxCampaign:
    query = (
        select(TTBGmvMaxCampaign)
        .where(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
        .where(TTBGmvMaxCampaign.campaign_id == str(campaign_id))
    )
    if advertiser_id:
        query = query.where(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))

    instance = db.execute(query).scalars().first()
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="campaign not found")
    return instance


async def _gather_store_entry(
    client: TikTokBusinessGMVMaxClient, *, advertiser_id: str, store_id: str
) -> dict[str, Any] | None:
    request = GMVMaxStoreListRequest(advertiser_id=str(advertiser_id))
    response: GMVMaxResponse[Any] = await client.gmv_max_store_list(request)
    store_list = getattr(response.data, "store_list", []) if response.data else []
    for entry in store_list:
        raw = entry.model_dump(exclude_none=False) if hasattr(entry, "model_dump") else entry
        if isinstance(raw, dict) and str(raw.get("store_id")) == str(store_id):
            return raw
    return None


async def _list_products_for_gmvmax(
    ttb_client: TTBApiClient,
    *,
    advertiser_id: str,
    store_id: str,
    bc_id: str | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async for product in ttb_client.iter_products(
        bc_id=bc_id,
        store_id=str(store_id),
        advertiser_id=str(advertiser_id),
        eligibility="GMV_MAX",
    ):
        if isinstance(product, dict):
            results.append(product)
    return results


def _dump_model(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _normalize_item_group_ids(value: Sequence[str] | None) -> list[str]:
    result: list[str] = []
    for item in value or []:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _identity_from_raw_entry(entry: Any, *, store_id: str | None = None) -> GMVMaxIdentityInfo | None:
    info = getattr(entry, "identity_info", None)
    if isinstance(entry, Mapping):
        raw = dict(entry)
    else:
        raw = _dump_model(entry)

    identity_id = str(
        getattr(info, "identity_id", None)
        or raw.get("identity_id")
        or ""
    ).strip()
    if not identity_id:
        return None

    identity_type = (
        getattr(info, "identity_type", None)
        or raw.get("identity_type")
        or None
    )
    if not identity_type:
        return None

    user_name = (
        getattr(info, "user_name", None)
        or raw.get("user_name")
        or raw.get("username")
        or raw.get("display_name")
    )
    profile_image = getattr(info, "profile_image", None) or raw.get("profile_image")
    payload: dict[str, Any] = {
        "identity_id": identity_id,
        "identity_type": identity_type,
        "user_name": user_name,
        "profile_image": profile_image,
    }
    identity_authorized_bc_id = raw.get("identity_authorized_bc_id")
    if identity_authorized_bc_id:
        payload["identity_authorized_bc_id"] = str(identity_authorized_bc_id)
    identity_authorized_shop_id = raw.get("identity_authorized_shop_id")
    if identity_authorized_shop_id:
        payload["identity_authorized_shop_id"] = str(identity_authorized_shop_id)
    raw_store_id = raw.get("store_id") or store_id
    if raw_store_id:
        payload["store_id"] = str(raw_store_id)
    return GMVMaxIdentityInfo(**{k: v for k, v in payload.items() if v is not None})


def _product_identity_key(identity: GMVMaxIdentityInfo) -> tuple[str, str] | None:
    identity_id = str(getattr(identity, "identity_id", None) or "").strip()
    identity_type = str(getattr(identity, "identity_type", None) or "").strip().upper()
    if not identity_id or not identity_type:
        return None
    return identity_id, identity_type


async def _load_product_identity_sources(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
) -> list[GMVMaxIdentityInfo]:
    response = await client.gmv_max_identity_get(
        GMVMaxIdentityGetRequest(
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            store_authorized_bc_id=str(store_authorized_bc_id),
        )
    )
    identities: list[GMVMaxIdentityInfo] = []
    seen: set[tuple[str, str]] = set()
    for entry in getattr(getattr(response, "data", None), "identity_list", []) or []:
        if getattr(entry, "product_gmv_max_available", None) is False:
            continue
        identity = _identity_from_raw_entry(entry, store_id=store_id)
        if identity is None:
            continue
        identity_key = _product_identity_key(identity)
        if identity_key is None or identity_key in seen:
            continue
        seen.add(identity_key)
        identities.append(identity)
    return identities


async def _validate_product_identity_list(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    requested_identities: Sequence[GMVMaxIdentityInfo] | None,
    eligible_identities: Sequence[GMVMaxIdentityInfo] | None = None,
) -> list[GMVMaxIdentityInfo] | None:
    if not requested_identities:
        return None

    eligible = (
        list(eligible_identities)
        if eligible_identities is not None
        else await _load_product_identity_sources(
            client,
            advertiser_id=advertiser_id,
            store_id=store_id,
            store_authorized_bc_id=store_authorized_bc_id,
        )
    )
    eligible_by_key = {
        identity_key: identity
        for identity in eligible
        if (identity_key := _product_identity_key(identity)) is not None
    }
    normalized: list[GMVMaxIdentityInfo] = []
    invalid: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for identity in requested_identities:
        identity_id = str(getattr(identity, "identity_id", None) or "").strip()
        identity_type = str(
            getattr(identity, "identity_type", None) or ""
        ).strip().upper()
        identity_key = _product_identity_key(identity)
        if identity_key is None:
            invalid.append(
                {
                    "identity_id": identity_id or None,
                    "identity_type": identity_type or None,
                    "reason": "MISSING_ID_OR_TYPE",
                }
            )
            continue
        if identity_key in seen:
            continue
        seen.add(identity_key)
        eligible_identity = eligible_by_key.get(identity_key)
        if eligible_identity is None:
            invalid.append(
                {
                    "identity_id": identity_id,
                    "identity_type": identity_type,
                    "reason": "NOT_AVAILABLE_FOR_PRODUCT_GMV_MAX",
                }
            )
            continue
        normalized.append(eligible_identity)

    if invalid:
        raise TTBBusinessError(
            "Some TikTok identities are not available for Product GMV Max",
            code="GMVMAX_IDENTITY_UNAVAILABLE",
            payload={
                "store_id": str(store_id),
                "advertiser_id": str(advertiser_id),
                "invalid_identities": invalid,
                "eligible_identities": [
                    {
                        "identity_id": identity_id,
                        "identity_type": identity_type,
                    }
                    for identity_id, identity_type in sorted(eligible_by_key)
                ],
            },
        )
    return normalized


async def _resolve_product_identity_list(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    requested_identities: Sequence[GMVMaxIdentityInfo] | None,
) -> list[GMVMaxIdentityInfo]:
    eligible = await _load_product_identity_sources(
        client,
        advertiser_id=advertiser_id,
        store_id=store_id,
        store_authorized_bc_id=store_authorized_bc_id,
    )
    if not eligible and requested_identities:
        raise TTBBusinessError(
            "No eligible TikTok identity sources found for Product GMV Max",
            code="GMVMAX_IDENTITY_MISSING",
            payload={
                "store_id": str(store_id),
                "advertiser_id": str(advertiser_id),
            },
        )

    if requested_identities:
        selected = await _validate_product_identity_list(
            client,
            advertiser_id=advertiser_id,
            store_id=store_id,
            store_authorized_bc_id=store_authorized_bc_id,
            requested_identities=requested_identities,
            eligible_identities=eligible,
        )
        return list(selected or [])

    return eligible[:PRODUCT_GMV_MAX_IDENTITY_LIMIT]


async def _ensure_create_payload_conflict_free(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    product_specific_type: str,
    item_group_ids: list[str],
    client: TikTokBusinessGMVMaxClient,
) -> TTBApiClient | None:
    usage_resp = await client.gmv_max_store_shop_ad_usage_check(
        GMVMaxStoreAdUsageCheckRequest(
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
        )
    )
    store_usage = getattr(usage_resp, "data", None)

    if product_specific_type == "ALL":
        allowed = getattr(store_usage, "promote_all_products_allowed", None)
        if allowed is False:
            raise TTBBusinessError(
                "All-products GMV Max promotion is not allowed for this store",
                code="GMVMAX_PROMOTE_ALL_CONFLICT",
                payload={"store_id": str(store_id), "advertiser_id": str(advertiser_id)},
            )
        return None

    if product_specific_type != "CUSTOMIZED_PRODUCTS" or not item_group_ids:
        return None

    occupancy_resp = await fetch_all_occupied_custom_shop_ads(
        client,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        occupied_asset_type="SPU",
        asset_ids=item_group_ids,
    )
    occupied_entries = [
        _dump_model(entry)
        for entry in getattr(getattr(occupancy_resp, "data", None), "occupied_custom_shop_ads", []) or []
    ]
    occupied_ids = sorted(
        {
            str(entry.get("item_group_id") or entry.get("asset_id") or "").strip()
            for entry in occupied_entries
            if str(entry.get("item_group_id") or entry.get("asset_id") or "").strip()
        }
    )
    if occupied_ids:
        raise TTBBusinessError(
            "Some SPUs are occupied by other Product GMV Max campaigns",
            code="GMVMAX_PRODUCT_OCCUPIED",
            payload={
                "item_group_ids": occupied_ids,
                "occupied_custom_shop_ads": occupied_entries,
                "request_id": getattr(occupancy_resp, "request_id", None),
            },
        )

    ttb_client = get_ttb_client_for_account(db, workspace_id, provider, auth_id)
    try:
        await _check_customized_products_conflict(
            ttb_client=ttb_client,
            campaign=type(
                "CampaignPrecheckShim",
                (),
                {"store_id": str(store_id)},
            )(),
            advertiser_id=str(advertiser_id),
            bc_id=str(store_authorized_bc_id),
            item_group_ids=item_group_ids,
        )
    except Exception:
        await ttb_client.aclose()
        raise
    return ttb_client


_CREATE_INTENT_PENDING_STATES = {"SUBMITTING", "UNKNOWN"}
_CREATE_INTENT_REMOTE_STATES = {
    "REMOTE_CREATED",
    "FINALIZING",
    "QUARANTINED",
    "SUCCEEDED",
}
_CREATE_INTENT_ACTIVE_PRODUCT_STATES = {
    "PREPARED",
    "SUBMITTING",
    "UNKNOWN",
    "REMOTE_CREATED",
    "FINALIZING",
    "QUARANTINE_PENDING",
    "QUARANTINED",
    "COMPENSATION_PENDING",
}


@dataclass(frozen=True, slots=True)
class PreparedGmvmaxCreateIntent:
    """A durable checkpoint plus the current preflight create candidate.

    ``body`` has the stable request ID and campaign-name marker, but it is not
    the submitted wire record: schedule-from-now and network-resolved identity
    fields are finalized later and persisted as ``result_json.wire_request``.
    """

    intent: GmvmaxCampaignCreateIntent
    frozen_payload: CreateCampaignRequest
    body: GMVMaxCampaignCreateBody
    created: bool


def _create_intent_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def gmvmax_create_payload_sha256(
    payload: CreateCampaignRequest,
    *,
    advertiser_id: str,
) -> str:
    canonical = payload.model_dump(mode="json", exclude_none=False)
    canonical["advertiser_id"] = str(advertiser_id)
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _official_request_id(idempotency_key: str, explicit_request_id: str | None) -> str:
    if explicit_request_id:
        return str(explicit_request_id)
    value = int.from_bytes(
        hashlib.sha256(str(idempotency_key).encode("utf-8")).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    return str(value or 1)


def _intent_campaign_name(campaign_name: str, idempotency_key: str) -> str:
    marker = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:8]
    suffix = f"_i{marker}"
    normalized = str(campaign_name).strip()
    if normalized.endswith(suffix):
        return normalized[:255]
    return f"{normalized[: 255 - len(suffix)]}{suffix}"


def _load_create_intent(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    idempotency_key: str,
    for_update: bool = False,
) -> GmvmaxCampaignCreateIntent | None:
    query = (
        db.query(GmvmaxCampaignCreateIntent)
        .filter(GmvmaxCampaignCreateIntent.workspace_id == int(workspace_id))
        .filter(GmvmaxCampaignCreateIntent.auth_id == int(auth_id))
        .filter(
            GmvmaxCampaignCreateIntent.advertiser_id
            == str(advertiser_id)
        )
        .filter(GmvmaxCampaignCreateIntent.store_id == str(store_id))
        .filter(
            GmvmaxCampaignCreateIntent.idempotency_key
            == str(idempotency_key)
        )
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def get_gmvmax_create_intent(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    idempotency_key: str | None,
) -> GmvmaxCampaignCreateIntent | None:
    if not idempotency_key:
        return None
    intent = _load_create_intent(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        idempotency_key=str(idempotency_key),
    )
    if (
        intent is None
        or str(intent.advertiser_id) != str(advertiser_id)
        or str(intent.store_id) != str(store_id)
    ):
        return None
    return intent


def mark_gmvmax_create_intent(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    idempotency_key: str | None,
    state: str,
    campaign_id: str | None = None,
    result_json: Mapping[str, Any] | None = None,
    replace_result_json: bool = False,
    error_json: Mapping[str, Any] | None = None,
) -> GmvmaxCampaignCreateIntent | None:
    intent = get_gmvmax_create_intent(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        idempotency_key=idempotency_key,
    )
    if intent is None:
        return None
    now = _create_intent_now()
    intent.state = str(state).upper()
    if campaign_id:
        intent.campaign_id = str(campaign_id)
    if replace_result_json:
        intent.result_json = (
            dict(result_json) if result_json is not None else None
        )
    elif result_json is not None:
        merged_result = (
            dict(intent.result_json)
            if isinstance(intent.result_json, Mapping)
            else {}
        )
        merged_result.update(dict(result_json))
        intent.result_json = merged_result
    intent.error_json = dict(error_json) if error_json is not None else None
    intent.updated_at = now
    if intent.state in _CREATE_INTENT_REMOTE_STATES and intent.remote_created_at is None:
        intent.remote_created_at = now
    if intent.state == "SUBMITTING" and intent.submitted_at is None:
        intent.submitted_at = now
    if intent.state == "SUCCEEDED":
        intent.finalized_at = now
    db.add(intent)
    return intent


def _create_intent_expected_item_groups(
    intent: GmvmaxCampaignCreateIntent,
) -> set[str]:
    payload = intent.request_json if isinstance(intent.request_json, Mapping) else {}
    return {
        str(value).strip()
        for value in (
            payload.get("item_group_ids")
            or payload.get("product_item_group_ids")
            or []
        )
        if str(value).strip()
    }


def _catalog_matches_create_intent(
    db: Session,
    *,
    intent: GmvmaxCampaignCreateIntent,
    row: GmvmaxProductCampaignCatalog,
) -> bool:
    if str(row.store_id or "") != str(intent.store_id):
        return False
    status_value = " ".join(
        [
            str(row.operation_status or ""),
            str(row.secondary_status or ""),
        ]
    ).upper()
    if "DELETE" in status_value:
        return False
    expected = _create_intent_expected_item_groups(intent)
    if not expected:
        return True
    actual = {
        str(value)
        for (value,) in (
            db.query(GmvmaxProductCampaignItemGroup.item_group_id)
            .filter(
                GmvmaxProductCampaignItemGroup.workspace_id
                == int(intent.workspace_id)
            )
            .filter(
                GmvmaxProductCampaignItemGroup.auth_id == int(intent.auth_id)
            )
            .filter(
                GmvmaxProductCampaignItemGroup.advertiser_id
                == str(intent.advertiser_id)
            )
            .filter(
                GmvmaxProductCampaignItemGroup.store_id == str(intent.store_id)
            )
            .filter(
                GmvmaxProductCampaignItemGroup.campaign_id
                == str(row.campaign_id)
            )
            .all()
        )
    }
    return actual == expected


def _find_local_campaign_for_create_intent(
    db: Session,
    *,
    intent: GmvmaxCampaignCreateIntent,
) -> GmvmaxProductCampaignCatalog | None:
    query = (
        db.query(GmvmaxProductCampaignCatalog)
        .filter(
            GmvmaxProductCampaignCatalog.workspace_id
            == int(intent.workspace_id)
        )
        .filter(GmvmaxProductCampaignCatalog.auth_id == int(intent.auth_id))
        .filter(
            GmvmaxProductCampaignCatalog.advertiser_id
            == str(intent.advertiser_id)
        )
        .filter(
            GmvmaxProductCampaignCatalog.store_id == str(intent.store_id)
        )
    )
    if intent.campaign_id:
        query = query.filter(
            GmvmaxProductCampaignCatalog.campaign_id == str(intent.campaign_id)
        )
    else:
        query = query.filter(
            GmvmaxProductCampaignCatalog.campaign_name
            == str(intent.campaign_name)
        )
    matches = [
        row
        for row in query.order_by(
            GmvmaxProductCampaignCatalog.id.desc()
        ).limit(3)
        if _catalog_matches_create_intent(db, intent=intent, row=row)
    ]
    return matches[0] if len(matches) == 1 else None


async def _reconcile_create_intent(
    db: Session,
    *,
    client: TikTokBusinessGMVMaxClient,
    intent: GmvmaxCampaignCreateIntent,
    execution_guard: Callable[[Session], None] | None = None,
    require_official_confirmation: bool = False,
) -> GmvmaxProductCampaignCatalog | None:
    local_row = _find_local_campaign_for_create_intent(db, intent=intent)
    if local_row is not None and not require_official_confirmation:
        return local_row

    candidate_ids: list[str] = []
    if intent.campaign_id:
        candidate_ids.append(str(intent.campaign_id))
    else:
        async def _fetch_page(page: int):
            if execution_guard is not None:
                execution_guard(db)
            return await client.gmv_max_campaign_get(
                GMVMaxCampaignGetRequest(
                    advertiser_id=str(intent.advertiser_id),
                    filtering=GMVMaxCampaignFiltering(
                        gmv_max_promotion_types=["PRODUCT_GMV_MAX"],
                        store_ids=[str(intent.store_id)],
                        campaign_name=str(intent.campaign_name),
                    ),
                    page=page,
                    page_size=100,
                )
            )

        async for fetched_page in iter_numbered_pages(
            _fetch_page,
            rows_from_data=lambda data: data.list,
            item_key=lambda item: getattr(item, "campaign_id", None),
            requested_page_size=100,
            max_pages=5,
        ):
            for item in fetched_page.rows:
                campaign_id = str(getattr(item, "campaign_id", None) or "").strip()
                campaign_name = str(
                    getattr(item, "campaign_name", None) or ""
                ).strip()
                if campaign_id and campaign_name == str(intent.campaign_name):
                    candidate_ids.append(campaign_id)

    candidate_ids = list(dict.fromkeys(candidate_ids))
    if len(candidate_ids) != 1:
        return None

    source_observed_at = datetime.now(timezone.utc)
    if execution_guard is not None:
        execution_guard(db)
    info_response = await client.gmv_max_campaign_info(
        GMVMaxCampaignInfoRequest(
            advertiser_id=str(intent.advertiser_id),
            campaign_id=candidate_ids[0],
        )
    )
    if execution_guard is not None:
        execution_guard(db)
    info_payload = info_response.data.model_dump(exclude_none=True)
    info_name = str(info_payload.get("campaign_name") or "").strip()
    if info_name and info_name != str(intent.campaign_name):
        return None
    info_store = str(info_payload.get("store_id") or "").strip()
    if info_store and set(info_store) != {"0"} and info_store != str(intent.store_id):
        return None
    expected_item_groups = _create_intent_expected_item_groups(intent)
    info_item_groups = {
        str(value).strip()
        for value in info_payload.get("item_group_ids", [])
        if str(value).strip()
    }
    if expected_item_groups and info_item_groups != expected_item_groups:
        return None

    row = upsert_campaign_from_api(
        db,
        workspace_id=int(intent.workspace_id),
        auth_id=int(intent.auth_id),
        advertiser_id=str(intent.advertiser_id),
        payload=info_payload,
        store_id_hint=str(intent.store_id),
        campaign_details=info_payload,
        campaign_details_complete=True,
        source_observed_at=source_observed_at,
    )
    return row if _catalog_matches_create_intent(db, intent=intent, row=row) else None


async def reconcile_gmvmax_create_intent(
    db: Session,
    *,
    client: TikTokBusinessGMVMaxClient,
    intent: GmvmaxCampaignCreateIntent,
    execution_guard: Callable[[Session], None] | None = None,
    require_official_confirmation: bool = True,
) -> GmvmaxProductCampaignCatalog | None:
    """Resolve one durable create intent without ever submitting a create call."""

    return await _reconcile_create_intent(
        db,
        client=client,
        intent=intent,
        execution_guard=execution_guard,
        require_official_confirmation=require_official_confirmation,
    )


def _create_intent_conflict(
    message: str,
    *,
    code: str,
    intent: GmvmaxCampaignCreateIntent,
) -> TTBBusinessError:
    return TTBBusinessError(
        message,
        code=code,
        status=409,
        payload={
            "idempotency_key": str(intent.idempotency_key),
            "state": str(intent.state),
            "campaign_id": str(intent.campaign_id) if intent.campaign_id else None,
            "campaign_name": str(intent.campaign_name),
        },
    )


def _validate_existing_prepared_intent(
    intent: GmvmaxCampaignCreateIntent,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    idempotency_key: str,
    client_payload_sha256: str,
    payload_sha256: str,
    official_request_id: str,
    campaign_name: str,
) -> None:
    if (
        int(intent.workspace_id) != int(workspace_id)
        or int(intent.auth_id) != int(auth_id)
        or str(intent.advertiser_id) != str(advertiser_id)
        or str(intent.store_id) != str(store_id)
        or str(intent.idempotency_key) != str(idempotency_key)
        or str(intent.client_payload_sha256) != str(client_payload_sha256)
        or str(intent.payload_sha256) != str(payload_sha256)
        or str(intent.official_request_id) != str(official_request_id)
    ):
        raise _create_intent_conflict(
            "The idempotency key is already bound to a different GMV Max "
            "create request.",
            code="GMVMAX_CREATE_IDEMPOTENCY_CONFLICT",
            intent=intent,
        )
    if str(intent.campaign_name) != str(campaign_name):
        raise _create_intent_conflict(
            "The idempotency key campaign marker does not match.",
            code="GMVMAX_CREATE_IDEMPOTENCY_CONFLICT",
            intent=intent,
        )


def _prepared_create_result_from_intent(
    *,
    intent: GmvmaxCampaignCreateIntent,
    advertiser_id: str,
    store_authorized_bc_id: str | None,
    advertiser_timezone: str | None,
    created: bool,
) -> PreparedGmvmaxCreateIntent:
    if not isinstance(intent.request_json, Mapping):
        raise _create_intent_conflict(
            "The stored GMV Max create request is invalid.",
            code="GMVMAX_CREATE_INVALID_INTENT_STATE",
            intent=intent,
        )
    try:
        frozen_payload = CreateCampaignRequest.model_validate(
            dict(intent.request_json)
        )
    except Exception as exc:  # noqa: BLE001 - persisted JSON is untrusted
        raise _create_intent_conflict(
            "The stored GMV Max create request is invalid.",
            code="GMVMAX_CREATE_INVALID_INTENT_STATE",
            intent=intent,
        ) from exc

    frozen_advertiser_id = str(
        frozen_payload.advertiser_id or advertiser_id
    )
    if (
        frozen_advertiser_id != str(advertiser_id)
        or str(frozen_payload.store_id) != str(intent.store_id)
        or str(frozen_payload.idempotency_key or "")
        != str(intent.idempotency_key)
        or gmvmax_create_payload_sha256(
            frozen_payload,
            advertiser_id=str(advertiser_id),
        )
        != str(intent.payload_sha256)
    ):
        raise _create_intent_conflict(
            "The stored GMV Max create request no longer matches its durable "
            "scope or payload hash.",
            code="GMVMAX_CREATE_IDEMPOTENCY_CONFLICT",
            intent=intent,
        )
    if frozen_payload.advertiser_id is None:
        frozen_payload = frozen_payload.model_copy(
            update={"advertiser_id": str(advertiser_id)}
        )

    frozen_store_authorized_bc_id = (
        frozen_payload.store_authorized_bc_id
        or store_authorized_bc_id
    )
    body = frozen_payload.to_client_body(
        store_authorized_bc_id=frozen_store_authorized_bc_id,
        advertiser_timezone=advertiser_timezone,
        official_request_id=str(intent.official_request_id),
    ).model_copy(update={"campaign_name": str(intent.campaign_name)})
    if (
        str(body.store_id) != str(intent.store_id)
        or str(body.request_id or "") != str(intent.official_request_id)
        or str(body.campaign_name) != str(intent.campaign_name)
    ):
        raise _create_intent_conflict(
            "The stored GMV Max create request cannot reproduce its official "
            "request identity.",
            code="GMVMAX_CREATE_IDEMPOTENCY_CONFLICT",
            intent=intent,
        )
    return PreparedGmvmaxCreateIntent(
        intent=intent,
        frozen_payload=frozen_payload,
        body=body,
        created=bool(created),
    )


def prepare_gmvmax_create_intent(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    payload: CreateCampaignRequest,
    store_authorized_bc_id: str | None = None,
    client_payload_sha256: str | None = None,
    execution_guard: Callable[[Session], None] | None = None,
) -> PreparedGmvmaxCreateIntent:
    """Persist a fully scoped PREPARED create intent without network I/O."""

    _ensure_provider(provider)
    account = ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)
    if account is not None:
        # Lock a stable parent row before checking for active product intents.
        # Locking only the (possibly empty) intent result set leaves a phantom
        # window where two different idempotency keys can both become PREPARED.
        # The auth row is tenant-scoped and makes that check/insert atomic
        # without serializing creation work for unrelated accounts.
        db.refresh(account, with_for_update=True)
    normalized_advertiser_id = str(advertiser_id)
    if (
        payload.advertiser_id is not None
        and str(payload.advertiser_id) != normalized_advertiser_id
    ):
        raise TTBBusinessError(
            "The GMV Max create payload advertiser is outside the requested "
            "tenant scope.",
            code="GMVMAX_CREATE_SCOPE_MISMATCH",
            status=403,
            payload={
                "workspace_id": int(workspace_id),
                "auth_id": int(auth_id),
            },
        )

    frozen_payload = payload.model_copy(
        update={"advertiser_id": normalized_advertiser_id}
    )
    idempotency_key = str(
        frozen_payload.idempotency_key or ""
    ).strip()
    if not idempotency_key:
        raise TTBBusinessError(
            "A durable GMV Max create intent requires an idempotency key.",
            code="GMVMAX_CREATE_IDEMPOTENCY_KEY_REQUIRED",
            status=422,
        )

    payload_sha256 = gmvmax_create_payload_sha256(
        frozen_payload,
        advertiser_id=normalized_advertiser_id,
    )
    bound_client_payload_sha256 = str(
        client_payload_sha256 or payload_sha256
    )
    official_request_id = _official_request_id(
        idempotency_key,
        frozen_payload.request_id,
    )
    advertiser_timezone = _get_advertiser_timezone(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=normalized_advertiser_id,
    )
    candidate_body = frozen_payload.to_client_body(
        store_authorized_bc_id=store_authorized_bc_id,
        advertiser_timezone=advertiser_timezone,
        official_request_id=official_request_id,
    )
    campaign_name = _intent_campaign_name(
        str(candidate_body.campaign_name),
        idempotency_key,
    )
    candidate_body = candidate_body.model_copy(
        update={"campaign_name": campaign_name}
    )
    store_id = str(candidate_body.store_id)

    intent = _load_create_intent(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=normalized_advertiser_id,
        store_id=store_id,
        idempotency_key=idempotency_key,
        for_update=True,
    )
    if intent is not None:
        _validate_existing_prepared_intent(
            intent,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_advertiser_id,
            store_id=store_id,
            idempotency_key=idempotency_key,
            client_payload_sha256=bound_client_payload_sha256,
            payload_sha256=payload_sha256,
            official_request_id=official_request_id,
            campaign_name=campaign_name,
        )
        return _prepared_create_result_from_intent(
            intent=intent,
            advertiser_id=normalized_advertiser_id,
            store_authorized_bc_id=store_authorized_bc_id,
            advertiser_timezone=advertiser_timezone,
            created=False,
        )

    expected_item_groups = {
        str(value)
        for value in (candidate_body.item_group_ids or [])
        if str(value).strip()
    }
    active_intents = (
        db.query(GmvmaxCampaignCreateIntent)
        .filter(
            GmvmaxCampaignCreateIntent.workspace_id == int(workspace_id)
        )
        .filter(GmvmaxCampaignCreateIntent.auth_id == int(auth_id))
        .filter(
            GmvmaxCampaignCreateIntent.advertiser_id
            == normalized_advertiser_id
        )
        .filter(GmvmaxCampaignCreateIntent.store_id == store_id)
        .filter(
            GmvmaxCampaignCreateIntent.state.in_(
                sorted(_CREATE_INTENT_ACTIVE_PRODUCT_STATES)
            )
        )
        .order_by(GmvmaxCampaignCreateIntent.id.desc())
        .with_for_update()
        .all()
    )
    blocking_intent = next(
        (
            candidate
            for candidate in active_intents
            if (
                not _create_intent_expected_item_groups(candidate)
                or not expected_item_groups
                or bool(
                    _create_intent_expected_item_groups(candidate)
                    & expected_item_groups
                )
            )
        ),
        None,
    )
    if blocking_intent is not None:
        raise _create_intent_conflict(
            "Another unfinished create intent already owns this product "
            "scope. Resume that intent instead of creating a duplicate "
            "campaign.",
            code="GMVMAX_CREATE_ACTIVE_INTENT_EXISTS",
            intent=blocking_intent,
        )

    intent = GmvmaxCampaignCreateIntent(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        advertiser_id=normalized_advertiser_id,
        store_id=store_id,
        idempotency_key=idempotency_key,
        client_payload_sha256=bound_client_payload_sha256,
        payload_sha256=payload_sha256,
        official_request_id=str(official_request_id),
        campaign_name=campaign_name,
        replacement_campaign_id=(
            str(frozen_payload.replacement_campaign_id)
            if frozen_payload.replacement_campaign_id
            else None
        ),
        state="PREPARED",
        request_json=frozen_payload.model_dump(
            mode="json",
            exclude_none=True,
        ),
    )
    db.add(intent)
    try:
        if execution_guard is not None:
            execution_guard(db)
        db.commit()
    except IntegrityError:
        db.rollback()
        raced_intent = _load_create_intent(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_advertiser_id,
            store_id=store_id,
            idempotency_key=idempotency_key,
            for_update=True,
        )
        if raced_intent is None:
            raise
        _validate_existing_prepared_intent(
            raced_intent,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=normalized_advertiser_id,
            store_id=store_id,
            idempotency_key=idempotency_key,
            client_payload_sha256=bound_client_payload_sha256,
            payload_sha256=payload_sha256,
            official_request_id=official_request_id,
            campaign_name=campaign_name,
        )
        return _prepared_create_result_from_intent(
            intent=raced_intent,
            advertiser_id=normalized_advertiser_id,
            store_authorized_bc_id=store_authorized_bc_id,
            advertiser_timezone=advertiser_timezone,
            created=False,
        )
    except Exception:
        db.rollback()
        raise
    return PreparedGmvmaxCreateIntent(
        intent=intent,
        frozen_payload=frozen_payload,
        body=candidate_body,
        created=True,
    )


async def create_gmvmax_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    payload: CreateCampaignRequest,
    store_authorized_bc_id: str | None = None,
    client_payload_sha256: str | None = None,
    client: TikTokBusinessGMVMaxClient | None = None,
    execution_guard: Callable[[Session], None] | None = None,
) -> TTBGmvMaxCampaign:
    """Create a *Product* GMV Max campaign with normalized TikTok payload mapping.

    NOTE:
    - This helper currently only supports Product GMV Max campaigns.
    - Live GMV Max (LIVE shopping) should be handled by a dedicated helper or
      an explicit branch once the payload semantics are defined, to avoid
      overloading the PRODUCT-specific assumptions here.
    """

    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    client_provided = client is not None
    client = client or build_ttb_gmvmax_client(db, auth_id=auth_id)

    idempotency_key: str | None = None
    prepared_intent_id: int | None = None
    prepared_store_id: str | None = None
    prepared_client_payload_sha256: str | None = None
    prepared_payload_sha256: str | None = None
    prepared_replacement_campaign_id: str | None = None
    create_post_started = False
    try:
        idempotency_key = str(payload.idempotency_key or "").strip() or None
        intent: GmvmaxCampaignCreateIntent | None = None
        if idempotency_key:
            prepared = prepare_gmvmax_create_intent(
                db,
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                payload=payload,
                store_authorized_bc_id=store_authorized_bc_id,
                client_payload_sha256=client_payload_sha256,
                execution_guard=execution_guard,
            )
            intent = prepared.intent
            payload = prepared.frozen_payload
            body = prepared.body
            prepared_intent_id = int(intent.id)
            prepared_store_id = str(intent.store_id)
            prepared_client_payload_sha256 = str(intent.client_payload_sha256)
            prepared_payload_sha256 = str(intent.payload_sha256)
            prepared_replacement_campaign_id = (
                str(intent.replacement_campaign_id).strip()
                if intent.replacement_campaign_id
                else None
            )
            state = str(intent.state or "").upper()
            if state in (
                _CREATE_INTENT_PENDING_STATES
                | _CREATE_INTENT_REMOTE_STATES
            ):
                try:
                    recovered_row = await _reconcile_create_intent(
                        db,
                        client=client,
                        intent=intent,
                        execution_guard=execution_guard,
                    )
                except Exception as reconcile_exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning(
                        "GMV Max create outcome reconciliation is not yet available",
                        exc_info=True,
                        extra={
                            "workspace_id": workspace_id,
                            "auth_id": auth_id,
                            "advertiser_id": str(advertiser_id),
                            "store_id": str(body.store_id),
                            "idempotency_key": idempotency_key,
                            "intent_state": state,
                        },
                    )
                    persisted_intent = _load_create_intent(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        store_id=str(body.store_id),
                        idempotency_key=idempotency_key,
                    )
                    if persisted_intent is None:
                        raise
                    raise _create_intent_conflict(
                        "Campaign creation is still being confirmed. Do not "
                        "submit a new create request; retry this same request shortly.",
                        code="GMVMAX_CREATE_PENDING_CONFIRMATION",
                        intent=persisted_intent,
                    ) from reconcile_exc
                if recovered_row is None:
                    raise _create_intent_conflict(
                        "Campaign creation is still being confirmed. Do not "
                        "submit a new create request; retry this same request shortly.",
                        code="GMVMAX_CREATE_PENDING_CONFIRMATION",
                        intent=intent,
                    )
                if state in _CREATE_INTENT_PENDING_STATES:
                    mark_gmvmax_create_intent(
                        db,
                        workspace_id=workspace_id,
                        auth_id=auth_id,
                        advertiser_id=str(advertiser_id),
                        store_id=str(body.store_id),
                        idempotency_key=idempotency_key,
                        state="REMOTE_CREATED",
                        campaign_id=str(recovered_row.campaign_id),
                        result_json={
                            "reconciled": True,
                            "campaign_id": str(recovered_row.campaign_id),
                        },
                    )
                if execution_guard is not None:
                    execution_guard(db)
                db.commit()
                return recovered_row
            if state == "FAILED_TERMINAL":
                raise _create_intent_conflict(
                    "The previous create request was rejected by TikTok. "
                    "Start a new logical create after correcting the request.",
                    code="GMVMAX_CREATE_FAILED_TERMINAL",
                    intent=intent,
                )
            if state != "PREPARED":
                raise _create_intent_conflict(
                    "The stored GMV Max create intent has an invalid state. "
                    "It will not be submitted again until an operator repairs it.",
                    code="GMVMAX_CREATE_INVALID_INTENT_STATE",
                    intent=intent,
                )
        else:
            body = payload.to_client_body(
                store_authorized_bc_id=store_authorized_bc_id,
                advertiser_timezone=_get_advertiser_timezone(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=advertiser_id,
                ),
                official_request_id=payload.request_id,
            )

        resolved_store_authorized_bc_id = str(
            body.store_authorized_bc_id or store_authorized_bc_id or ""
        ).strip()
        if not resolved_store_authorized_bc_id:
            resolved_store_authorized_bc_id = await ensure_gmvmax_store_authorized(
                client,
                advertiser_id=str(advertiser_id),
                target_store_id=str(body.store_id),
                execution_guard=(
                    (lambda: execution_guard(db))
                    if execution_guard is not None
                    else None
                ),
            )
            body = body.model_copy(update={"store_authorized_bc_id": resolved_store_authorized_bc_id})

        product_specific_type = str(body.product_specific_type or "ALL").upper()
        item_group_ids = _normalize_item_group_ids(body.item_group_ids)
        ttb_client_for_precheck = await _ensure_create_payload_conflict_free(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=str(advertiser_id),
            store_id=str(body.store_id),
            store_authorized_bc_id=resolved_store_authorized_bc_id,
            product_specific_type=product_specific_type,
            item_group_ids=item_group_ids,
            client=client,
        )
        if ttb_client_for_precheck is not None:
            await ttb_client_for_precheck.aclose()

        identities = await _resolve_product_identity_list(
            client,
            advertiser_id=str(advertiser_id),
            store_id=str(body.store_id),
            store_authorized_bc_id=resolved_store_authorized_bc_id,
            requested_identities=body.identity_list,
        )
        body = body.model_copy(update={"identity_list": identities or None})

        if body.affiliate_posts_enabled is None:
            body = body.model_copy(update={"affiliate_posts_enabled": True})

        if idempotency_key:
            intent = _load_create_intent(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                store_id=str(body.store_id),
                idempotency_key=idempotency_key,
                for_update=True,
            )
            if intent is None:
                raise RuntimeError("GMV Max create intent disappeared before submission")
            submit_state = str(intent.state or "").upper()
            if submit_state == "SUCCEEDED":
                completed_row = _find_local_campaign_for_create_intent(
                    db,
                    intent=intent,
                )
                if completed_row is not None:
                    db.commit()
                    return completed_row
            if submit_state in (
                _CREATE_INTENT_PENDING_STATES
                | _CREATE_INTENT_REMOTE_STATES
                | {"QUARANTINE_PENDING", "COMPENSATION_PENDING"}
            ):
                raise _create_intent_conflict(
                    "Campaign creation is already being submitted or finalized. "
                    "Do not send a second create request; retry this same logical "
                    "request shortly.",
                    code="GMVMAX_CREATE_PENDING_CONFIRMATION",
                    intent=intent,
                )
            if submit_state == "FAILED_TERMINAL":
                raise _create_intent_conflict(
                    "The previous create request was rejected by TikTok. "
                    "Start a new logical create after correcting the request.",
                    code="GMVMAX_CREATE_FAILED_TERMINAL",
                    intent=intent,
                )
            if submit_state != "PREPARED":
                raise _create_intent_conflict(
                    "The stored GMV Max create intent has an invalid state. "
                    "It will not be submitted until an operator repairs it.",
                    code="GMVMAX_CREATE_INVALID_INTENT_STATE",
                    intent=intent,
                )
            intent.state = "SUBMITTING"
            intent.submitted_at = intent.submitted_at or _create_intent_now()
            intent.updated_at = _create_intent_now()
            intent.result_json = {
                **(intent.result_json or {}),
                "wire_request": body.model_dump(mode="json", exclude_none=True),
            }
            intent.error_json = None
            db.add(intent)
            if execution_guard is not None:
                execution_guard(db)
            db.commit()

        try:
            create_post_started = True
            row = await svc_create_campaign(
                db,
                workspace_id=workspace_id,
                provider=provider,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                client=client,
                body=body,
                execution_guard=execution_guard,
            )
        except Exception as create_exc:  # noqa: BLE001
            if not idempotency_key:
                raise
            db.rollback()
            persisted_intent = _load_create_intent(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                store_id=str(body.store_id),
                idempotency_key=idempotency_key,
                for_update=True,
            )
            if persisted_intent is None:
                raise
            request_not_sent = isinstance(
                create_exc,
                TTBRateLimitBudgetError,
            )
            definite_rejection = (
                isinstance(create_exc, TTBApiError)
                and not isinstance(create_exc, TTBHttpError)
                and create_exc.code is not None
                and not request_not_sent
            )
            persisted_intent.state = (
                "PREPARED"
                if request_not_sent
                else "FAILED_TERMINAL"
                if definite_rejection
                else "UNKNOWN"
            )
            persisted_intent.updated_at = _create_intent_now()
            persisted_intent.error_json = {
                "type": type(create_exc).__name__,
                "message": str(create_exc),
                "code": getattr(create_exc, "code", None),
                "status": getattr(create_exc, "status", None),
            }
            db.add(persisted_intent)
            if execution_guard is not None:
                execution_guard(db)
            db.commit()
            if request_not_sent:
                raise
            if definite_rejection:
                raise
            try:
                recovered_row = await _reconcile_create_intent(
                    db,
                    client=client,
                    intent=persisted_intent,
                    execution_guard=execution_guard,
                    require_official_confirmation=True,
                )
            except Exception:  # noqa: BLE001
                # UNKNOWN was committed before reconciliation.  A failed GET
                # must never roll that durable no-resubmit boundary back.
                db.rollback()
                logger.warning(
                    "GMV Max create outcome could not be reconciled immediately",
                    exc_info=True,
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": str(advertiser_id),
                        "store_id": str(body.store_id),
                        "idempotency_key": idempotency_key,
                    },
                )
                recovered_row = None
                persisted_intent = _load_create_intent(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    store_id=str(body.store_id),
                    idempotency_key=idempotency_key,
                )
                if persisted_intent is None:
                    raise
            if recovered_row is not None:
                recovered_result = dict(persisted_intent.result_json or {})
                recovered_result.update(
                    {
                        "reconciled": True,
                        "reconciled_after_ambiguous_create": True,
                        "campaign_id": str(recovered_row.campaign_id),
                    }
                )
                mark_gmvmax_create_intent(
                    db,
                    workspace_id=workspace_id,
                    auth_id=auth_id,
                    advertiser_id=str(advertiser_id),
                    store_id=str(body.store_id),
                    idempotency_key=idempotency_key,
                    state="REMOTE_CREATED",
                    campaign_id=str(recovered_row.campaign_id),
                    result_json=recovered_result,
                )
                if execution_guard is not None:
                    execution_guard(db)
                db.commit()
                return recovered_row
            raise _create_intent_conflict(
                "TikTok may already have created this campaign, but the response "
                "could not be confirmed. The request will not be sent again; "
                "retry the same logical request to reconcile it.",
                code="GMVMAX_CREATE_OUTCOME_UNKNOWN",
                intent=persisted_intent,
            ) from create_exc
        if row is None:
            row = (
                db.execute(
                    select(GmvmaxProductCampaignCatalog)
                    .where(GmvmaxProductCampaignCatalog.workspace_id == int(workspace_id))
                    .where(GmvmaxProductCampaignCatalog.auth_id == int(auth_id))
                    .where(GmvmaxProductCampaignCatalog.advertiser_id == str(advertiser_id))
                    .where(GmvmaxProductCampaignCatalog.store_id == str(body.store_id))
                    .where(GmvmaxProductCampaignCatalog.campaign_name == str(body.campaign_name))
                    .order_by(GmvmaxProductCampaignCatalog.id.desc())
                )
                .scalars()
                .first()
            )
        if row is None:
            raise TTBBusinessError(
                "GMV Max campaign was submitted but local campaign catalog row was not found",
                code="GMVMAX_CREATE_CATALOG_MISSING",
                payload={
                    "campaign_name": str(body.campaign_name),
                    "store_id": str(body.store_id),
                    "advertiser_id": str(advertiser_id),
                },
            )
        if idempotency_key:
            mark_gmvmax_create_intent(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                store_id=str(body.store_id),
                idempotency_key=idempotency_key,
                state="REMOTE_CREATED",
                campaign_id=str(row.campaign_id),
                result_json={
                    "campaign_id": str(row.campaign_id),
                    "request_id": str(body.request_id or ""),
                },
            )
        if execution_guard is not None:
            execution_guard(db)
        db.commit()
        return row
    except Exception as exc:
        automation = (
            payload.automation
            if isinstance(getattr(payload, "automation", None), Mapping)
            else {}
        )
        frozen_replacement_campaign_id = (
            str(
                getattr(payload, "replacement_campaign_id", "") or ""
            ).strip()
            or None
        )
        retryable_rebuild_occupancy = (
            isinstance(exc, TTBBusinessError)
            and str(getattr(exc, "code", "") or "")
            == "GMVMAX_PRODUCT_OCCUPIED"
            and str(automation.get("source") or "")
            == "creative_guard_rebuild"
            and frozen_replacement_campaign_id is not None
            and prepared_replacement_campaign_id
            == frozen_replacement_campaign_id
        )
        if (
            isinstance(exc, TTBBusinessError)
            and not retryable_rebuild_occupancy
            and not create_post_started
            and idempotency_key
            and prepared_intent_id is not None
            and prepared_store_id
        ):
            # PREPARED was committed before network preflight. A non-retryable
            # business rejection at this point proves that CREATE was never
            # submitted, so release the product scope before returning the 4xx
            # that tells the client it may correct the payload and use a new
            # logical key. Never apply this transition after submission starts:
            # ambiguous POST outcomes must remain UNKNOWN/recoverable.
            db.rollback()
            persisted_intent = _load_create_intent(
                db,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=str(advertiser_id),
                store_id=prepared_store_id,
                idempotency_key=idempotency_key,
                for_update=True,
            )
            if (
                persisted_intent is not None
                and int(persisted_intent.id) == prepared_intent_id
                and str(persisted_intent.state or "").upper() == "PREPARED"
                and str(persisted_intent.client_payload_sha256)
                == prepared_client_payload_sha256
                and str(persisted_intent.payload_sha256)
                == prepared_payload_sha256
            ):
                persisted_intent.state = "FAILED_TERMINAL"
                persisted_intent.updated_at = _create_intent_now()
                persisted_intent.error_json = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "code": getattr(exc, "code", None),
                    "status": getattr(exc, "status", None),
                    "phase": "PRE_SUBMISSION",
                    "request_sent": False,
                }
                db.add(persisted_intent)
                if execution_guard is not None:
                    execution_guard(db)
                db.commit()
                raise
        db.rollback()
        raise
    finally:
        if not client_provided:
            await client.aclose()


async def gmvmax_precheck(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: str,
    payload: GMVMaxPrecheckRequest,
    client: TikTokBusinessGMVMaxClient | None = None,
    ttb_client: TTBApiClient | None = None,
) -> GMVMaxPrecheckResponse:
    """Run comprehensive Product GMV Max precheck for a tenant binding."""

    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    client_provided = client is not None
    ttb_client_provided = ttb_client is not None
    client = client or build_ttb_gmvmax_client(db, auth_id=auth_id)
    ttb_client = ttb_client or build_ttb_client(db, auth_id=auth_id)

    try:
        store_entry = await _gather_store_entry(
            client, advertiser_id=advertiser_id, store_id=payload.store_id
        )
        is_gmv_max_available = bool(store_entry.get("is_gmv_max_available")) if store_entry else False
        current_authorized_advertiser_id: str | None = None
        needs_exclusive_auth = False

        if store_entry:
            exclusive_info = store_entry.get("exclusive_authorized_advertiser_info")
            if isinstance(exclusive_info, dict):
                current_authorized_advertiser_id = exclusive_info.get("advertiser_id")
                if current_authorized_advertiser_id and str(current_authorized_advertiser_id) != str(advertiser_id):
                    needs_exclusive_auth = True
            elif store_entry.get("advertiser_id") and str(store_entry.get("advertiser_id")) != str(advertiser_id):
                current_authorized_advertiser_id = str(store_entry.get("advertiser_id"))
                needs_exclusive_auth = True

        resolved_store_authorized_bc_id = str(
            payload.store_authorized_bc_id
            or (store_entry or {}).get("store_authorized_bc_id")
            or ""
        ).strip()

        if not is_gmv_max_available:
            return GMVMaxPrecheckResponse(
                is_gmv_max_available=False,
                needs_exclusive_auth=False,
                current_authorized_advertiser_id=current_authorized_advertiser_id,
                promote_all_products_allowed=False,
                has_running_custom_shop_ads=False,
                occupied_custom_shop_ads=[],
                unoccupied_item_group_ids=[],
                occupied_item_group_ids=[],
                available_identities=[],
                available_videos=[],
                available_custom_anchor_videos=[],
                recommended_roas_bid=None,
                recommended_budget=None,
                store_usage=None,
                identities=[],
                occupancy=None,
                request_ids={
                    "store_usage": None,
                    "identities": None,
                    "occupancy": None,
                },
            )

        usage_resp = await client.gmv_max_store_shop_ad_usage_check(
            GMVMaxStoreAdUsageCheckRequest(
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
            )
        )
        store_usage = usage_resp.data
        promote_all_products_allowed = bool(
            getattr(store_usage, "promote_all_products_allowed", False)
        )
        has_running_custom_shop_ads = bool(
            getattr(store_usage, "is_running_custom_shop_ads", False)
        )

        occupied_ads: list[OccupiedAdSummary] = []
        occupancy_resp = None
        occupancy_data = None
        spu_ids = list(payload.item_group_ids or payload.product_item_group_ids or [])
        if spu_ids or has_running_custom_shop_ads:
            occupancy_resp = await fetch_all_occupied_custom_shop_ads(
                client,
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                occupied_asset_type="SPU",
                asset_ids=[str(item) for item in spu_ids],
            )
            occupancy_data = occupancy_resp.data
            if occupancy_data:
                for entry in getattr(occupancy_data, "occupied_custom_shop_ads", []) or []:
                    occupied_ads.append(
                        OccupiedAdSummary(
                            ad_id=getattr(entry, "ad_id", None),
                            campaign_id=getattr(entry, "campaign_id", None),
                            advertiser_id=getattr(entry, "advertiser_id", None),
                            item_group_id=getattr(entry, "item_group_id", None),
                            create_time=getattr(entry, "create_time", None),
                        )
                    )
        products = await _list_products_for_gmvmax(
            ttb_client,
            advertiser_id=advertiser_id,
            store_id=payload.store_id,
            bc_id=resolved_store_authorized_bc_id or None,
        )
        unoccupied_item_group_ids: list[str] = []
        occupied_item_group_ids: list[str] = []
        requested_item_group_ids = {str(item) for item in spu_ids if str(item)}
        for product in products:
            if product.get("status") != "AVAILABLE":
                continue
            group_id = str(product.get("item_group_id")) if product.get("item_group_id") else None
            if not group_id:
                continue
            if requested_item_group_ids and group_id not in requested_item_group_ids:
                continue
            gmv_status = product.get("gmv_max_ads_status")
            if gmv_status == "UNOCCUPIED":
                unoccupied_item_group_ids.append(group_id)
            else:
                occupied_item_group_ids.append(group_id)
        for entry in occupied_ads:
            if entry.item_group_id and entry.item_group_id not in occupied_item_group_ids:
                occupied_item_group_ids.append(str(entry.item_group_id))

        identity_resp = await client.gmv_max_identity_get(
            GMVMaxIdentityGetRequest(
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                store_authorized_bc_id=resolved_store_authorized_bc_id,
            )
        )
        identity_data = identity_resp.data if identity_resp else None
        identities = []
        for entry in getattr(identity_data, "identity_list", []) or []:
            info = getattr(entry, "identity_info", None)
            summary = IdentitySummary(
                identity_id=getattr(info, "identity_id", None) or getattr(entry, "identity_id", None),
                identity_type=getattr(info, "identity_type", None) or getattr(entry, "identity_type", None),
                user_name=(
                    getattr(info, "user_name", None)
                    or getattr(entry, "user_name", None)
                    or getattr(entry, "display_name", None)
                ),
                profile_image=getattr(info, "profile_image", None) or getattr(entry, "profile_image", None),
                product_gmv_max_available=getattr(entry, "product_gmv_max_available", None),
            )
            if summary.product_gmv_max_available is False:
                continue
            identities.append(summary)

        videos: list[VideoSummary] = []
        video_identity_filter = build_gmvmax_identity_filter(
            getattr(identity_data, "identity_list", []) or [],
            store_id=str(payload.store_id),
        )
        try:
            async for entry in iter_gmvmax_video_entries(
                client,
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                store_authorized_bc_id=resolved_store_authorized_bc_id,
                identities=video_identity_filter,
                item_group_ids=spu_ids,
            ):
                video_info = getattr(entry, "video_info", None)
                videos.append(
                    VideoSummary(
                        item_id=getattr(entry, "item_id", None),
                        video_id=getattr(video_info, "video_id", None),
                        preview_url=getattr(video_info, "preview_url", None),
                        video_cover_url=getattr(video_info, "video_cover_url", None),
                        duration=getattr(video_info, "duration", None),
                    )
                )
        except NumberedPaginationError:
            raise
        except Exception:
            logger.exception(
                "GMV Max precheck video listing failed",
                extra={
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(payload.store_id),
                },
            )
            videos = []

        custom_anchor_videos: list[CustomAnchorVideoSummary] = []
        try:
            async for entry in iter_gmvmax_custom_anchor_entries(
                client,
                advertiser_id=str(advertiser_id),
                store_id=str(payload.store_id),
                store_authorized_bc_id=resolved_store_authorized_bc_id,
                identities=video_identity_filter,
                item_group_ids=spu_ids,
            ):
                video_info = getattr(entry, "video_info", None)
                custom_anchor_videos.append(
                    CustomAnchorVideoSummary(
                        custom_anchor_video_id=getattr(
                            entry, "custom_anchor_video_id", None
                        ),
                        item_id=getattr(entry, "item_id", None),
                        video_id=getattr(video_info, "video_id", None),
                        preview_url=getattr(video_info, "preview_url", None),
                        video_cover_url=getattr(video_info, "video_cover_url", None),
                        duration=getattr(video_info, "duration", None),
                    )
                )
        except NumberedPaginationError:
            raise
        except Exception:
            logger.exception(
                "GMV Max precheck customized-post listing failed",
                extra={
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(payload.store_id),
                },
            )
            custom_anchor_videos = []

        recommended_roas_bid = None
        recommended_budget = None
        try:
            recommendation_item_ids = list(
                payload.item_group_ids
                or payload.product_item_group_ids
                or []
            )
            recommend_resp = await client.gmv_max_bid_recommend(
                GMVMaxBidRecommendRequest(
                    advertiser_id=str(advertiser_id),
                    store_id=str(payload.store_id),
                    shopping_ads_type="PRODUCT",
                    optimization_goal="VALUE",
                    item_group_ids=recommendation_item_ids or None,
                )
            )
            recommended_roas_bid = recommend_resp.data.roas_bid
            recommended_budget = recommend_resp.data.budget
        except Exception:
            recommended_roas_bid = None
            recommended_budget = None

        return GMVMaxPrecheckResponse(
            is_gmv_max_available=is_gmv_max_available,
            needs_exclusive_auth=needs_exclusive_auth,
            current_authorized_advertiser_id=current_authorized_advertiser_id,
            promote_all_products_allowed=promote_all_products_allowed,
            has_running_custom_shop_ads=has_running_custom_shop_ads,
            occupied_custom_shop_ads=occupied_ads,
            unoccupied_item_group_ids=unoccupied_item_group_ids,
            occupied_item_group_ids=occupied_item_group_ids,
            available_identities=identities,
            available_videos=videos,
            available_custom_anchor_videos=custom_anchor_videos,
            recommended_roas_bid=recommended_roas_bid,
            recommended_budget=recommended_budget,
            store_usage=store_usage,
            identities=getattr(identity_data, "identity_list", []) if identity_data else [],
            occupancy=occupancy_data,
            request_ids={
                "store_usage": usage_resp.request_id,
                "identities": getattr(identity_resp, "request_id", None),
                "occupancy": getattr(occupancy_resp, "request_id", None) if occupancy_resp else None,
            },
        )
    finally:
        if not client_provided:
            try:
                await client.aclose()
            except Exception:
                pass
        if not ttb_client_provided:
            try:
                await ttb_client.aclose()
            except Exception:
                pass


async def sync_campaigns(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    campaign_ids: Optional[Iterable[str]] = None,
) -> int:
    """Sync campaigns for a binding using ``ttb_gmvmax.sync_gmvmax_campaigns`` and TikTok client."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    owner_token = f"http-campaign-sync:{uuid4()}"
    lock = build_account_sync_lock(
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
        owner_token=owner_token,
    )
    acquired = await asyncio.to_thread(
        lock.acquire,
        timeout=30.0,
        retry_interval=0.2,
    )
    if not acquired:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "code": "GMVMAX_ACCOUNT_SYNC_BUSY",
                "message": "Another GMV Max account sync is still running.",
            },
        )

    fence = None
    with SessionLocal() as fence_db:
        try:
            fence = acquire_account_sync_fence(
                fence_db,
                redis_lock=lock,
                workspace_id=int(workspace_id),
                auth_id=int(auth_id),
                owner_token=owner_token,
            )
            if fence is None:
                fence_db.rollback()
            else:
                fence_db.commit()
        except Exception:
            fence_db.rollback()
            lock.release()
            raise
    if fence is None:
        lock.release()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "code": "GMVMAX_ACCOUNT_SYNC_BUSY",
                "message": "Another GMV Max account sync is still running.",
            },
        )

    # The request session may have opened a REPEATABLE READ snapshot while
    # waiting for the account lease.  Start the mutable read/write phase from
    # a fresh snapshot owned by this execution.
    db.rollback()
    db.expire_all()
    client = None
    try:
        client = get_gmvmax_client_for_account(db, workspace_id, provider, auth_id)
        result = await svc_sync_campaigns(
            db,
            client,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=str(resolved_advertiser),
            status=status_filter,
            campaign_ids=[str(cid) for cid in campaign_ids] if campaign_ids else None,
        )
        fence.assert_current(db)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        if client is not None:
            await client.aclose()
        with SessionLocal() as release_db:
            try:
                release_account_sync_fence(release_db, fence=fence)
                release_db.commit()
            except Exception:  # noqa: BLE001
                release_db.rollback()
                logger.exception(
                    "HTTP GMV Max campaign sync durable fence release failed",
                    extra={
                        "workspace_id": workspace_id,
                        "auth_id": auth_id,
                        "advertiser_id": resolved_advertiser,
                    },
                )
        lock.release()

    return int(result.get("synced", 0))


async def list_campaigns(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    advertiser_id: Optional[str] = None,
    store_id: Optional[str] = None,
    business_center_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    sync: bool = False,
) -> dict[str, Any]:
    """Return cached GMV Max campaigns with optional pre-sync; relies on repository filters."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    synced: Optional[int] = None
    if sync:
        synced = await sync_campaigns(
            db,
            workspace_id=workspace_id,
            provider=provider,
            auth_id=auth_id,
            advertiser_id=resolved_advertiser,
            status_filter=status_filter,
        )

    if not store_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_store", "message": "store_id is required"},
        )

    items, total = list_gmvmax_campaigns(
        db,
        workspace_id=workspace_id,
        advertiser_id=str(resolved_advertiser),
        store_id=str(store_id),
        status_filter=status_filter,
        search=search,
        page=page,
        page_size=page_size,
    )

    payload: dict[str, Any] = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if synced is not None:
        payload["synced"] = synced
    return payload


async def get_campaign(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    advertiser_id: Optional[str] = None,
    refresh: bool = False,
) -> TTBGmvMaxCampaign:
    """Load a single campaign, optionally triggering a targeted sync when missing."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    resolved_advertiser = (
        advertiser_id
        if advertiser_id is not None
        else get_advertiser_id_for_account(db, workspace_id, provider, auth_id)
    )

    try:
        instance = _ensure_campaign(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            campaign_id=campaign_id,
            advertiser_id=resolved_advertiser,
        )
        return instance
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND or not refresh:
            raise

    await sync_campaigns(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        advertiser_id=resolved_advertiser,
        campaign_ids=[campaign_id],
    )

    instance = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
        advertiser_id=resolved_advertiser,
    )
    return instance


def _query_metrics_hourly(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
    offset: int,
) -> Sequence[TTBGmvMaxMetricsHourly]:
    query = select(TTBGmvMaxMetricsHourly).where(
        TTBGmvMaxMetricsHourly.campaign_id == str(campaign.campaign_id)
    )
    if start:
        query = query.where(TTBGmvMaxMetricsHourly.stat_time_hour >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsHourly.stat_time_hour < end)
    query = (
        query.order_by(TTBGmvMaxMetricsHourly.stat_time_hour.asc())
        .limit(limit)
        .offset(offset)
    )
    return db.execute(query).scalars().all()


def _query_metrics_daily(
    db: Session,
    *,
    campaign: TTBGmvMaxCampaign,
    start: Optional[date],
    end: Optional[date],
    limit: int,
    offset: int,
) -> Sequence[TTBGmvMaxMetricsDaily]:
    query = select(TTBGmvMaxMetricsDaily).where(
        TTBGmvMaxMetricsDaily.campaign_id == str(campaign.campaign_id)
    )
    if start:
        query = query.where(TTBGmvMaxMetricsDaily.stat_time_day >= start)
    if end:
        query = query.where(TTBGmvMaxMetricsDaily.stat_time_day < end)
    query = (
        query.order_by(TTBGmvMaxMetricsDaily.stat_time_day.asc())
        .limit(limit)
        .offset(offset)
    )
    return db.execute(query).scalars().all()


def query_metrics(
    db: Session,
    *,
    workspace_id: int,
    provider: str,
    auth_id: int,
    campaign_id: str,
    granularity: str,
    start: Optional[datetime | date],
    end: Optional[datetime | date],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Query stored hourly or daily metrics for a campaign (DB only)."""
    provider = _ensure_provider(provider)
    ensure_ttb_auth_in_workspace(db, workspace_id, auth_id)

    campaign = _ensure_campaign(
        db,
        workspace_id=workspace_id,
        auth_id=auth_id,
        campaign_id=campaign_id,
    )

    gran = granularity.upper()
    if gran == "HOUR":
        rows = _query_metrics_hourly(
            db,
            campaign=campaign,
            start=start if isinstance(start, datetime) else None,
            end=end if isinstance(end, datetime) else None,
            limit=limit,
            offset=offset,
        )
        return {
            "granularity": "HOUR",
            "items": rows,
            "count": len(rows),
        }

    rows = _query_metrics_daily(
        db,
        campaign=campaign,
        start=start if isinstance(start, date) else None,
        end=end if isinstance(end, date) else None,
        limit=limit,
        offset=offset,
    )
    return {
        "granularity": "DAY",
        "items": rows,
        "count": len(rows),
    }
