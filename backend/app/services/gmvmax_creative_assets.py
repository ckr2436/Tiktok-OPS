from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.providers.tiktok_business.gmvmax_client import (
    GMVMaxCreationCustomAnchorVideoListGetRequest,
    GMVMaxIdentityGetRequest,
    GMVMaxVideoGetRequest,
    TikTokBusinessGMVMaxClient,
)
from app.gmvmax.services.report_pagination import (
    DEFAULT_NUMBERED_PAGE_LIMIT,
    iter_numbered_pages,
)

logger = logging.getLogger("gmv.services.gmvmax.creative_assets")

PAGE_SIZE = 50
DEFAULT_MAX_PAGES = DEFAULT_NUMBERED_PAGE_LIMIT
VIDEO_SPU_FILTER_LIMIT = 50
VIDEO_IDENTITY_FILTER_LIMIT = 20


def _chunk_values(values: Sequence[Any], limit: int) -> list[list[Any]]:
    normalized = list(values)
    return [
        normalized[offset : offset + limit]
        for offset in range(0, len(normalized), limit)
    ]


def _is_lock_wait_timeout(exc: BaseException) -> bool:
    text_value = str(exc).lower()
    return "1205" in text_value or "lock wait timeout" in text_value


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _normalize_spu_ids(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        candidates = list(value)
    elif value is None:
        candidates = []
    else:
        candidates = [value]

    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        item_group_id = str(candidate).strip()
        if not item_group_id or item_group_id in seen:
            continue
        seen.add(item_group_id)
        normalized.append(item_group_id)
    return normalized


def ensure_creative_asset_cache_table(session: Session) -> None:
    session.execute(
        text(
            """
            create table if not exists gmvmax_creative_asset_cache (
                id bigint unsigned not null auto_increment primary key,
                workspace_id bigint unsigned not null,
                auth_id bigint unsigned not null,
                advertiser_id varchar(64) not null,
                store_id varchar(64) not null,
                item_id varchar(64) not null,
                item_group_id varchar(64) null,
                video_id varchar(128) null,
                title text null,
                preview_url text null,
                video_cover_url text null,
                local_preview_path text null,
                local_cover_path text null,
                preview_content_type varchar(128) null,
                cover_content_type varchar(128) null,
                media_cache_status varchar(32) not null default 'PENDING',
                media_cache_error text null,
                media_cache_attempts int not null default 0,
                media_cache_next_retry_at datetime(6) null,
                media_cached_at datetime(6) null,
                duration decimal(18,4) null,
                identity_id varchar(128) null,
                identity_type varchar(64) null,
                identity_name varchar(255) null,
                raw_json json null,
                fetched_at datetime(6) not null default current_timestamp(6),
                created_at datetime(6) not null default current_timestamp(6),
                updated_at datetime(6) not null default current_timestamp(6) on update current_timestamp(6),
                unique key uq_gmvmax_creative_asset_scope (
                    workspace_id, auth_id, advertiser_id, store_id, item_id
                ),
                key idx_gmvmax_creative_asset_product (
                    workspace_id, auth_id, advertiser_id, store_id, item_group_id
                ),
                key idx_gmvmax_creative_asset_video (video_id),
                key idx_gmvmax_creative_asset_media_cache (
                    media_cache_status, media_cache_next_retry_at
                )
            ) engine=InnoDB default charset=utf8mb4 collate=utf8mb4_0900_ai_ci
            """
        )
    )


def resolve_store_authorized_bc_id(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
) -> str | None:
    row = session.execute(
        text(
            """
            select coalesce(l.store_authorized_bc_id, l.bc_id_hint, s.store_authorized_bc_id, s.bc_id) as bc_id
            from ttb_advertiser_store_links l
            left join ttb_stores s
              on s.workspace_id=l.workspace_id
             and s.auth_id=l.auth_id
             and s.store_id=l.store_id
            where l.workspace_id=:workspace_id
              and l.auth_id=:auth_id
              and l.advertiser_id=:advertiser_id
              and l.store_id=:store_id
            order by l.last_seen_at desc
            limit 1
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
        },
    ).mappings().first()
    value = str((row or {}).get("bc_id") or "").strip()
    return value or None


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _get_nested(raw: Mapping[str, Any], *keys: str) -> Any:
    current: Any = raw
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _video_entries(data: Any) -> list[Any]:
    if data is None:
        return []
    entries: list[Any] = []
    for key in ("item_list", "video_list", "list", "videos"):
        value = getattr(data, key, None)
        if isinstance(value, list):
            entries.extend(value)
    if not entries:
        raw = _model_dump(data)
        for key in ("item_list", "video_list", "list", "videos"):
            value = raw.get(key)
            if isinstance(value, list):
                entries.extend(value)
    return entries


def _asset_entry_item_key(entry: Any) -> str | None:
    raw = _model_dump(entry)
    value = getattr(entry, "item_id", None) or raw.get("item_id")
    normalized = str(value or "").strip()
    return normalized or None


def _merge_filtered_asset_entries(current: Any, incoming: Any) -> Any:
    """Keep one asset while unioning SPUs observed through legal filter chunks."""

    current_id = _asset_entry_item_key(current)
    incoming_id = _asset_entry_item_key(incoming)
    if not current_id or current_id != incoming_id:
        raise ValueError("cannot merge creative asset entries with different item_id")
    current_raw = _model_dump(current)
    incoming_raw = _model_dump(incoming)
    spu_ids = _normalize_spu_ids(
        [
            *_normalize_spu_ids(
                current_raw.get("spu_id_list")
                if current_raw.get("spu_id_list") is not None
                else getattr(current, "spu_id_list", None)
            ),
            *_normalize_spu_ids(
                incoming_raw.get("spu_id_list")
                if incoming_raw.get("spu_id_list") is not None
                else getattr(incoming, "spu_id_list", None)
            ),
        ]
    )

    if isinstance(current, Mapping):
        merged = dict(current_raw)
        for field_name, value in incoming_raw.items():
            current_value = merged.get(field_name)
            if (
                (current_value is None or current_value == "")
                and value is not None
                and value != ""
            ):
                merged[field_name] = value
        merged["spu_id_list"] = spu_ids
        return merged

    if hasattr(current, "model_copy"):
        updates: dict[str, Any] = {"spu_id_list": spu_ids}
        for field_name, value in incoming_raw.items():
            if field_name == "spu_id_list":
                continue
            current_value = getattr(current, field_name, None)
            if (
                (current_value is None or current_value == "")
                and value is not None
                and value != ""
            ):
                updates[field_name] = value
        return current.model_copy(update=updates)

    try:
        setattr(current, "spu_id_list", spu_ids)
    except (AttributeError, TypeError):
        pass
    return current


def build_gmvmax_identity_filter(
    entries: Sequence[Any],
    *,
    store_id: str,
) -> list[dict[str, Any]]:
    """Build the complete official ``video/get`` identity filter payload."""

    identities: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for entry in entries:
        raw = _model_dump(entry)
        info = getattr(entry, "identity_info", None)
        info_raw = _model_dump(info) or dict(raw.get("identity_info") or {})
        if raw.get("product_gmv_max_available") is False:
            continue
        identity_id = str(
            getattr(info, "identity_id", None)
            or info_raw.get("identity_id")
            or raw.get("identity_id")
            or ""
        ).strip()
        identity_type = str(
            getattr(info, "identity_type", None)
            or info_raw.get("identity_type")
            or raw.get("identity_type")
            or ""
        ).strip()
        if not identity_id or not identity_type:
            continue

        payload: dict[str, Any] = {
            "identity_id": identity_id,
            "identity_type": identity_type,
        }
        for field_name in (
            "identity_authorized_bc_id",
            "identity_authorized_shop_id",
            "store_id",
        ):
            field_value = info_raw.get(field_name) or raw.get(field_name)
            if field_value:
                payload[field_name] = str(field_value)
        if identity_type == "TTS_TT" and not payload.get("store_id"):
            payload["store_id"] = str(store_id)

        dedupe_key = (
            identity_id,
            identity_type,
            str(payload.get("identity_authorized_bc_id") or ""),
            str(payload.get("identity_authorized_shop_id") or ""),
            str(payload.get("store_id") or ""),
        )
        if dedupe_key in seen:
            continue
        identities.append(payload)
        seen.add(dedupe_key)
    return identities


async def iter_gmvmax_video_entries(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    identities: Sequence[Mapping[str, Any]] | None,
    item_group_ids: Sequence[str] | None = None,
    keyword: str | None = None,
    custom_posts_eligible: bool | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> AsyncIterator[Any]:
    """Yield every matching ``video/get`` item within official filter limits."""

    clean_item_group_ids = list(
        dict.fromkeys(str(item) for item in (item_group_ids or []) if item)
    )
    spu_filter_limit = 1 if custom_posts_eligible is True else VIDEO_SPU_FILTER_LIMIT
    item_group_chunks: list[list[str] | None] = [
        list(chunk)
        for chunk in _chunk_values(clean_item_group_ids, spu_filter_limit)
    ] or [None]
    identity_values = [dict(item) for item in (identities or [])]
    identity_chunks: list[list[dict[str, Any]] | None] = [
        list(chunk)
        for chunk in _chunk_values(identity_values, VIDEO_IDENTITY_FILTER_LIMIT)
    ] or [None]
    merged_entries: dict[str, Any] = {}
    item_order: list[str] = []
    for item_group_chunk in item_group_chunks:
        for identity_chunk in identity_chunks:
            async def _fetch_video_page(
                page: int,
                *,
                spu_chunk: list[str] | None = item_group_chunk,
                identities_chunk: list[dict[str, Any]] | None = identity_chunk,
            ) -> Any:
                return await client.gmv_max_video_get(
                    GMVMaxVideoGetRequest(
                        advertiser_id=str(advertiser_id),
                        store_id=str(store_id),
                        store_authorized_bc_id=str(store_authorized_bc_id),
                        spu_id_list=spu_chunk,
                        identity_list=identities_chunk,
                        need_auth_code_video=True,
                        custom_posts_eligible=custom_posts_eligible,
                        keyword=str(keyword) if keyword else None,
                        page=page,
                        page_size=PAGE_SIZE,
                    )
                )

            async for fetched_page in iter_numbered_pages(
                _fetch_video_page,
                rows_from_data=_video_entries,
                item_key=_asset_entry_item_key,
                requested_page_size=PAGE_SIZE,
                max_pages=int(max_pages),
                probe_on_missing_metadata=True,
            ):
                for entry in fetched_page.rows:
                    item_id = _asset_entry_item_key(entry)
                    if item_id is None:  # guarded by iter_numbered_pages
                        raise RuntimeError("creative asset pagination lost item_id")
                    if item_id not in merged_entries:
                        merged_entries[item_id] = entry
                        item_order.append(item_id)
                    else:
                        merged_entries[item_id] = _merge_filtered_asset_entries(
                            merged_entries[item_id],
                            entry,
                        )
    for item_id in item_order:
        yield merged_entries[item_id]


async def iter_gmvmax_custom_anchor_entries(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    identities: Sequence[Mapping[str, Any]] | None,
    item_group_ids: Sequence[str] | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> AsyncIterator[Any]:
    """Yield the complete shop-level customized-post list."""

    clean_item_group_ids = list(
        dict.fromkeys(str(item) for item in (item_group_ids or []) if item)
    )
    item_group_chunks: list[list[str] | None] = [
        list(chunk)
        for chunk in _chunk_values(clean_item_group_ids, VIDEO_SPU_FILTER_LIMIT)
    ] or [None]
    identity_values = [dict(item) for item in (identities or [])]
    identity_chunks: list[list[dict[str, Any]] | None] = [
        list(chunk)
        for chunk in _chunk_values(identity_values, VIDEO_IDENTITY_FILTER_LIMIT)
    ] or [None]
    merged_entries: dict[str, Any] = {}
    item_order: list[str] = []
    for item_group_chunk in item_group_chunks:
        for identity_chunk in identity_chunks:
            async def _fetch_page(
                page: int,
                *,
                spu_chunk: list[str] | None = item_group_chunk,
                identities_chunk: list[dict[str, Any]] | None = identity_chunk,
            ) -> Any:
                return await client.gmv_max_creation_custom_anchor_video_list_get(
                    GMVMaxCreationCustomAnchorVideoListGetRequest(
                        advertiser_id=str(advertiser_id),
                        store_id=str(store_id),
                        store_authorized_bc_id=str(store_authorized_bc_id),
                        creative_source="CUSTOMIZED",
                        spu_id_list=spu_chunk,
                        identity_list=identities_chunk,
                        need_auth_code_video=True,
                        page=page,
                        page_size=PAGE_SIZE,
                    )
                )

            async for fetched_page in iter_numbered_pages(
                _fetch_page,
                rows_from_data=_video_entries,
                item_key=_asset_entry_item_key,
                requested_page_size=PAGE_SIZE,
                max_pages=int(max_pages),
                probe_on_missing_metadata=True,
            ):
                for entry in fetched_page.rows:
                    item_id = _asset_entry_item_key(entry)
                    if item_id is None:  # guarded by iter_numbered_pages
                        raise RuntimeError("creative asset pagination lost item_id")
                    if item_id not in merged_entries:
                        merged_entries[item_id] = entry
                        item_order.append(item_id)
                    else:
                        merged_entries[item_id] = _merge_filtered_asset_entries(
                            merged_entries[item_id],
                            entry,
                        )
    for item_id in item_order:
        yield merged_entries[item_id]


def _asset_payload_from_entry(entry: Any) -> dict[str, Any] | None:
    raw = _model_dump(entry)
    # This namespace is owned by the local cache reconciler.  Never trust or
    # retain a same-named field from an upstream payload; a fresh official
    # observation reactivates the row by omitting the local tombstone.
    raw.pop("_gmv_ops_sync", None)
    item_id = str(getattr(entry, "item_id", None) or raw.get("item_id") or "").strip()
    if not item_id or item_id in {"-1", "0"}:
        return None

    video_info = getattr(entry, "video_info", None)
    video_raw = _model_dump(video_info) or dict(raw.get("video_info") or {})
    identity_info = getattr(entry, "identity_info", None)
    identity_raw = _model_dump(identity_info) or dict(raw.get("identity_info") or {})

    title = (
        getattr(entry, "text", None)
        or getattr(entry, "title", None)
        or raw.get("text")
        or raw.get("title")
        or raw.get("video_name")
        or item_id
    )
    spu_ids = _normalize_spu_ids(
        raw.get("spu_id_list")
        if raw.get("spu_id_list") is not None
        else getattr(entry, "spu_id_list", None)
    )
    # Keep the complete official association set in the canonical payload.
    # ``item_group_id`` remains only as a compatibility projection for older
    # readers; product filtering uses gmvmax_creative_asset_products.
    raw["spu_id_list"] = list(spu_ids)
    item_group_id = spu_ids[0] if spu_ids else None
    identity_name = (
        identity_raw.get("display_name")
        or identity_raw.get("user_name")
        or identity_raw.get("username")
        or raw.get("display_name")
        or raw.get("user_name")
    )
    return {
        "item_id": item_id,
        "item_group_id": item_group_id,
        "spu_id_list": list(spu_ids),
        "video_id": video_raw.get("video_id") or raw.get("video_id"),
        "title": str(title),
        "preview_url": video_raw.get("preview_url") or raw.get("preview_url"),
        "video_cover_url": video_raw.get("video_cover_url") or video_raw.get("cover_url") or raw.get("video_cover_url"),
        "duration": video_raw.get("duration") or raw.get("duration"),
        "identity_id": identity_raw.get("identity_id") or raw.get("identity_id"),
        "identity_type": identity_raw.get("identity_type") or raw.get("identity_type"),
        "identity_name": identity_name,
        "raw_json": raw,
    }


def _merge_asset_payloads(
    current: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge one asset observed through multiple legitimate filter chunks."""

    if current is None:
        return dict(incoming)
    current_item_id = str(current.get("item_id") or "").strip()
    incoming_item_id = str(incoming.get("item_id") or "").strip()
    if not current_item_id or current_item_id != incoming_item_id:
        raise ValueError("cannot merge creative asset payloads with different item_id")

    merged = dict(current)
    for field_name, value in incoming.items():
        if field_name in {"spu_id_list", "item_group_id", "raw_json"}:
            continue
        current_value = merged.get(field_name)
        if (
            (current_value is None or current_value == "")
            and value is not None
            and value != ""
        ):
            merged[field_name] = value

    spu_ids = _normalize_spu_ids(
        [
            *_normalize_spu_ids(current.get("spu_id_list")),
            *_normalize_spu_ids(incoming.get("spu_id_list")),
        ]
    )
    merged["spu_id_list"] = spu_ids
    merged["item_group_id"] = spu_ids[0] if spu_ids else None

    current_raw = (
        dict(current.get("raw_json") or {})
        if isinstance(current.get("raw_json"), Mapping)
        else {}
    )
    incoming_raw = (
        dict(incoming.get("raw_json") or {})
        if isinstance(incoming.get("raw_json"), Mapping)
        else {}
    )
    current_raw.update(incoming_raw)
    current_raw["spu_id_list"] = list(spu_ids)
    merged["raw_json"] = current_raw
    return merged


def _upsert_asset(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    payload: Mapping[str, Any],
) -> None:
    session.execute(
        text(
            """
            insert into gmvmax_creative_asset_cache (
                workspace_id, auth_id, advertiser_id, store_id, item_id,
                item_group_id, video_id, title, preview_url, video_cover_url,
                duration, identity_id, identity_type, identity_name, raw_json,
                fetched_at, created_at, updated_at
            ) values (
                :workspace_id, :auth_id, :advertiser_id, :store_id, :item_id,
                :item_group_id, :video_id, :title, :preview_url, :video_cover_url,
                :duration, :identity_id, :identity_type, :identity_name,
                cast(:raw_json as json), current_timestamp(6), current_timestamp(6), current_timestamp(6)
            )
            on duplicate key update
                item_group_id=values(item_group_id),
                video_id=coalesce(values(video_id), video_id),
                title=coalesce(values(title), title),
                media_cache_status=case
                    when local_preview_path is not null and local_cover_path is not null then 'READY'
                    when media_cache_status in ('QUEUED', 'PROCESSING') then media_cache_status
                    when (
                        (values(preview_url) is not null and not (values(preview_url) <=> preview_url))
                        or (values(video_cover_url) is not null and not (values(video_cover_url) <=> video_cover_url))
                    ) then 'PENDING'
                    else media_cache_status
                end,
                media_cache_error=case
                    when local_preview_path is not null and local_cover_path is not null then media_cache_error
                    when media_cache_status in ('QUEUED', 'PROCESSING') then media_cache_error
                    when (
                        (values(preview_url) is not null and not (values(preview_url) <=> preview_url))
                        or (values(video_cover_url) is not null and not (values(video_cover_url) <=> video_cover_url))
                    ) then null
                    else media_cache_error
                end,
                media_cache_next_retry_at=case
                    when local_preview_path is not null and local_cover_path is not null then media_cache_next_retry_at
                    when media_cache_status in ('QUEUED', 'PROCESSING') then media_cache_next_retry_at
                    when (
                        (values(preview_url) is not null and not (values(preview_url) <=> preview_url))
                        or (values(video_cover_url) is not null and not (values(video_cover_url) <=> video_cover_url))
                    ) then null
                    else media_cache_next_retry_at
                end,
                media_cache_attempts=case
                    when local_preview_path is not null and local_cover_path is not null then media_cache_attempts
                    when media_cache_status in ('QUEUED', 'PROCESSING') then media_cache_attempts
                    when (
                        (values(preview_url) is not null and not (values(preview_url) <=> preview_url))
                        or (values(video_cover_url) is not null and not (values(video_cover_url) <=> video_cover_url))
                    ) then 0
                    else media_cache_attempts
                end,
                preview_url=coalesce(values(preview_url), preview_url),
                video_cover_url=coalesce(values(video_cover_url), video_cover_url),
                duration=coalesce(values(duration), duration),
                identity_id=coalesce(values(identity_id), identity_id),
                identity_type=coalesce(values(identity_type), identity_type),
                identity_name=coalesce(values(identity_name), identity_name),
                raw_json=values(raw_json),
                fetched_at=current_timestamp(6),
                updated_at=current_timestamp(6)
            """
        ),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
            "item_id": str(payload.get("item_id")),
            "item_group_id": payload.get("item_group_id"),
            "video_id": payload.get("video_id"),
            "title": payload.get("title"),
            "preview_url": payload.get("preview_url"),
            "video_cover_url": payload.get("video_cover_url"),
            "duration": payload.get("duration"),
            "identity_id": payload.get("identity_id"),
            "identity_type": payload.get("identity_type"),
            "identity_name": payload.get("identity_name"),
            "raw_json": _json_dumps(payload.get("raw_json")),
        },
    )

    # The official response can associate one creative with multiple SPUs.
    # Replace the full relation set inside the same transaction as the cache
    # upsert. Deleting first is intentional: an empty official list must clear
    # associations that were present in an older response.
    item_id = str(payload.get("item_id"))
    relation_scope = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "advertiser_id": str(advertiser_id),
        "store_id": str(store_id),
        "item_id": item_id,
    }
    session.execute(
        text(
            """
            delete from gmvmax_creative_asset_products
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and item_id=:item_id
            """
        ),
        relation_scope,
    )
    spu_ids = _normalize_spu_ids(payload.get("spu_id_list"))
    if spu_ids:
        session.execute(
            text(
                """
                insert into gmvmax_creative_asset_products (
                    workspace_id, auth_id, advertiser_id, store_id,
                    item_id, item_group_id, created_at, updated_at
                ) values (
                    :workspace_id, :auth_id, :advertiser_id, :store_id,
                    :item_id, :item_group_id,
                    current_timestamp(6), current_timestamp(6)
                )
                """
            ),
            [
                {
                    **relation_scope,
                    "item_group_id": item_group_id,
                }
                for item_group_id in spu_ids
            ],
        )


async def load_gmvmax_identity_filter(
    client: TikTokBusinessGMVMaxClient,
    *,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
) -> list[dict[str, Any]]:
    response = await client.gmv_max_identity_get(
        GMVMaxIdentityGetRequest(
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            store_authorized_bc_id=str(store_authorized_bc_id),
        )
    )
    return build_gmvmax_identity_filter(
        getattr(getattr(response, "data", None), "identity_list", []) or [],
        store_id=str(store_id),
    )


def _cached_ids(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    creative_ids: Sequence[str],
) -> set[str]:
    clean_ids = [str(item) for item in creative_ids if str(item or "") not in {"", "-1", "0"}]
    if not clean_ids:
        return set()
    rows = session.execute(
        text(
            """
            select item_id from gmvmax_creative_asset_cache
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and item_id in :creative_ids
            """
        ).bindparams(bindparam("creative_ids", expanding=True)),
        {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
            "creative_ids": clean_ids,
        },
    ).scalars().all()
    return {str(item) for item in rows if item}


def _deactivate_absent_scope_assets(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    seen_item_ids: set[str],
) -> int:
    """Tombstone a complete store-wide snapshot without deleting history."""

    sql = """
        update gmvmax_creative_asset_cache
        set raw_json=json_set(
                coalesce(raw_json, json_object()),
                '$._gmv_ops_sync.active', false,
                '$._gmv_ops_sync.absent_at', :absent_at
            ),
            updated_at=current_timestamp(6)
        where workspace_id=:workspace_id
          and auth_id=:auth_id
          and advertiser_id=:advertiser_id
          and store_id=:store_id
          and coalesce(
                json_unquote(json_extract(raw_json, '$._gmv_ops_sync.active')),
                'true'
              ) <> 'false'
    """
    params: dict[str, Any] = {
        "workspace_id": int(workspace_id),
        "auth_id": int(auth_id),
        "advertiser_id": str(advertiser_id),
        "store_id": str(store_id),
        "absent_at": datetime.now(timezone.utc).isoformat(),
    }
    statement = text(sql)
    if seen_item_ids:
        statement = text(sql + " and item_id not in :seen_item_ids").bindparams(
            bindparam("seen_item_ids", expanding=True)
        )
        params["seen_item_ids"] = sorted(seen_item_ids)
    result = session.execute(statement, params)
    return max(0, int(getattr(result, "rowcount", 0) or 0))


def _reconcile_asset_product_partitions(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    seen_by_item_group: Mapping[str, set[str]],
) -> int:
    """Remove only stale relations inside completely fetched SPU partitions."""

    removed = 0
    for item_group_id, seen_item_ids in seen_by_item_group.items():
        sql = """
            delete from gmvmax_creative_asset_products
            where workspace_id=:workspace_id
              and auth_id=:auth_id
              and advertiser_id=:advertiser_id
              and store_id=:store_id
              and item_group_id=:item_group_id
        """
        params: dict[str, Any] = {
            "workspace_id": int(workspace_id),
            "auth_id": int(auth_id),
            "advertiser_id": str(advertiser_id),
            "store_id": str(store_id),
            "item_group_id": str(item_group_id),
        }
        statement = text(sql)
        if seen_item_ids:
            statement = text(sql + " and item_id not in :seen_item_ids").bindparams(
                bindparam("seen_item_ids", expanding=True)
            )
            params["seen_item_ids"] = sorted(seen_item_ids)
        result = session.execute(statement, params)
        removed += max(0, int(getattr(result, "rowcount", 0) or 0))
    return removed


async def _sync_creative_assets_for_scope_unlocked(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    creative_refs: Sequence[Mapping[str, Any]] | None = None,
    item_group_ids: Sequence[str] | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, int]:
    ensure_creative_asset_cache_table(session)
    clean_item_group_ids = list(dict.fromkeys(str(item) for item in (item_group_ids or []) if item))
    refs: list[dict[str, str | None]] = []
    for ref in creative_refs or []:
        creative_id = str(ref.get("creative_id") or ref.get("item_id") or "").strip()
        if not creative_id or creative_id in {"-1", "0"}:
            continue
        item_group_id = str(ref.get("item_group_id") or "").strip() or None
        refs.append({"creative_id": creative_id, "item_group_id": item_group_id})
        if item_group_id and item_group_id not in clean_item_group_ids:
            clean_item_group_ids.append(item_group_id)

    identities = await load_gmvmax_identity_filter(
        client,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        store_authorized_bc_id=str(store_authorized_bc_id),
    )
    seen_ids: set[str] = set()
    payloads_by_item_id: dict[str, dict[str, Any]] = {}
    seen_by_item_group: dict[str, set[str]] = {
        item_group_id: set() for item_group_id in clean_item_group_ids
    }
    snapshot_valid = True

    async for entry in iter_gmvmax_video_entries(
        client,
        advertiser_id=str(advertiser_id),
        store_id=str(store_id),
        store_authorized_bc_id=str(store_authorized_bc_id),
        identities=identities,
        item_group_ids=clean_item_group_ids,
        max_pages=int(max_pages),
    ):
        payload = _asset_payload_from_entry(entry)
        if not payload:
            raw = _model_dump(entry)
            raw_item_id = str(
                getattr(entry, "item_id", None) or raw.get("item_id") or ""
            ).strip()
            if raw_item_id not in {"-1", "0"}:
                snapshot_valid = False
            continue
        item_id = str(payload["item_id"])
        payload_spu_ids = set(_normalize_spu_ids(payload.get("spu_id_list")))
        if clean_item_group_ids and not (
            payload_spu_ids & set(clean_item_group_ids)
        ):
            # A filtered response without a matching partition association is
            # not trustworthy enough to remove older relations.
            snapshot_valid = False
        for item_group_id in payload_spu_ids:
            if item_group_id in seen_by_item_group:
                seen_by_item_group[item_group_id].add(item_id)
        payloads_by_item_id[item_id] = _merge_asset_payloads(
            payloads_by_item_id.get(item_id),
            payload,
        )
        seen_ids.add(item_id)

    requested_ids = [str(ref["creative_id"]) for ref in refs]
    already_cached = _cached_ids(
        session,
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id=advertiser_id,
        store_id=store_id,
        creative_ids=requested_ids,
    ) | seen_ids
    missing_refs: list[dict[str, str | None]] = []
    queued_missing_ids: set[str] = set()
    for ref in refs:
        creative_id = str(ref["creative_id"])
        if creative_id in already_cached or creative_id in queued_missing_ids:
            continue
        missing_refs.append(ref)
        queued_missing_ids.add(creative_id)

    for ref in missing_refs:
        creative_id = str(ref["creative_id"])
        matched = 0
        fallback_groups = (
            [str(ref["item_group_id"])]
            if ref.get("item_group_id")
            else clean_item_group_ids
        )
        async for entry in iter_gmvmax_video_entries(
            client,
            advertiser_id=str(advertiser_id),
            store_id=str(store_id),
            store_authorized_bc_id=str(store_authorized_bc_id),
            identities=identities,
            item_group_ids=fallback_groups,
            keyword=creative_id,
            max_pages=int(max_pages),
        ):
            payload = _asset_payload_from_entry(entry)
            if not payload:
                raw = _model_dump(entry)
                raw_item_id = str(
                    getattr(entry, "item_id", None) or raw.get("item_id") or ""
                ).strip()
                if raw_item_id not in {"-1", "0"}:
                    snapshot_valid = False
                continue
            item_id = str(payload["item_id"])
            payload_spu_ids = set(_normalize_spu_ids(payload.get("spu_id_list")))
            if clean_item_group_ids and not (
                payload_spu_ids & set(clean_item_group_ids)
            ):
                snapshot_valid = False
            for item_group_id in payload_spu_ids:
                if item_group_id in seen_by_item_group:
                    seen_by_item_group[item_group_id].add(item_id)
            payloads_by_item_id[item_id] = _merge_asset_payloads(
                payloads_by_item_id.get(item_id),
                payload,
            )
            if item_id == creative_id:
                matched += 1
            seen_ids.add(item_id)
        if not matched:
            logger.info(
                "gmvmax creative asset not returned by video/get",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "store_id": store_id,
                    "creative_id": creative_id,
                },
            )

    for payload in payloads_by_item_id.values():
        _upsert_asset(
            session,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            payload=payload,
        )
    inserted_or_updated = len(payloads_by_item_id)

    deactivated = 0
    relations_removed = 0
    if snapshot_valid:
        if clean_item_group_ids:
            relations_removed = _reconcile_asset_product_partitions(
                session,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=store_id,
                seen_by_item_group=seen_by_item_group,
            )
        else:
            deactivated = _deactivate_absent_scope_assets(
                session,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=store_id,
                seen_item_ids=seen_ids,
            )

    return {
        "requested": len(requested_ids),
        "upserted": inserted_or_updated,
        "matched_requested": len(set(requested_ids) & (_cached_ids(
            session,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            store_id=store_id,
            creative_ids=requested_ids,
        ) | seen_ids)),
        "identities": len(identities),
        "deactivated": deactivated,
        "relations_removed": relations_removed,
        "reconciled": snapshot_valid,
    }


async def sync_creative_assets_for_scope(
    session: Session,
    client: TikTokBusinessGMVMaxClient,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    store_id: str,
    store_authorized_bc_id: str,
    creative_refs: Sequence[Mapping[str, Any]] | None = None,
    item_group_ids: Sequence[str] | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    scope = f"{workspace_id}:{auth_id}:{advertiser_id}:{store_id}"
    digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:40]
    lock_name = f"gmvmax:creative:{digest}"
    acquired = session.execute(
        text("select get_lock(:lock_name, 0)"),
        {"lock_name": lock_name},
    ).scalar()
    if int(acquired or 0) != 1:
        return {
            "skipped": True,
            "reason": "creative_sync_already_running",
            "requested": len(creative_refs or []),
            "upserted": 0,
        }
    try:
        try:
            return await _sync_creative_assets_for_scope_unlocked(
                session,
                client,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id=advertiser_id,
                store_id=store_id,
                store_authorized_bc_id=store_authorized_bc_id,
                creative_refs=creative_refs,
                item_group_ids=item_group_ids,
                max_pages=max_pages,
            )
        except OperationalError as exc:
            if not _is_lock_wait_timeout(exc):
                raise
            session.rollback()
            logger.warning(
                "Skipped GMV Max creative asset sync because cache rows were locked",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": str(advertiser_id),
                    "store_id": str(store_id),
                },
            )
            return {
                "skipped": True,
                "reason": "creative_cache_lock_wait",
                "requested": len(creative_refs or []),
                "upserted": 0,
            }
    finally:
        try:
            session.execute(
                text("select release_lock(:lock_name)"),
                {"lock_name": lock_name},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to release GMV Max creative sync lock")


__all__ = [
    "ensure_creative_asset_cache_table",
    "resolve_store_authorized_bc_id",
    "sync_creative_assets_for_scope",
    "load_gmvmax_identity_filter",
]
