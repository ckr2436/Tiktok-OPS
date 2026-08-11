from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Iterator, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.ttb_entities import TTBBindingConfig
from app.providers.tiktok_business.website_ads_client import TikTokWebsiteAdsClient
from app.services.ttb_client_factory import build_ttb_client


logger = logging.getLogger(__name__)

CATALOG_SCHEMA_VERSION = 1
CATALOG_API_VERSION = "v1.3"
LISTABLE_SUB_TARGETING_TYPES = (
    "GENERAL_INTEREST",
    "PURCHASE_INTENTION",
    "VIDEO_INTERACTION",
    "CREATOR_INTERACTION",
    "HASHTAG_INTERACTION",
)
_TYPE_KEYS = {
    "general_interest": "GENERAL_INTEREST",
    "purchase_intention": "PURCHASE_INTENTION",
    "video_interaction": "VIDEO_INTERACTION",
    "creator_interaction": "CREATOR_INTERACTION",
    "hashtag_interaction": "HASHTAG_INTERACTION",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(root: Path | str | None = None) -> Path:
    configured = root or settings.WEBSITE_ADS_TARGETING_CATALOG_DIR
    return Path(configured).expanduser().resolve()


def _safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))[:128]


def catalog_path(advertiser_id: str, language: str = "en", *, root: Path | str | None = None) -> Path:
    return _root(root) / _safe_segment(advertiser_id) / f"official_targeting_{_safe_segment(language)}.json"


def discoveries_path(advertiser_id: str, language: str = "en", *, root: Path | str | None = None) -> Path:
    return _root(root) / _safe_segment(advertiser_id) / f"hermes_discoveries_{_safe_segment(language)}.json"


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _payload_data(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    return dict(data) if isinstance(data, Mapping) else {}


def _canonical_item(item: Mapping[str, Any], targeting_type: str, *, source: str) -> dict[str, Any] | None:
    item_id = str(item.get("id") or item.get("interest_category_id") or "").strip()
    if not item_id:
        return None
    name = str(item.get("name") or item.get("interest_category_name") or "").strip()
    children = item.get("children_ids")
    if not isinstance(children, list):
        children = item.get("sub_category_ids")
    return {
        "id": item_id,
        "name": name,
        "targeting_type": str(item.get("sub_targeting_type") or targeting_type),
        "level": int(item.get("level") or 0),
        "children_ids": [str(value) for value in (children or []) if str(value)],
        "supported_special_industries": list(
            item.get("supported_special_industries") or item.get("special_industries") or []
        ),
        "placements": list(item.get("placements") or []),
        "hashtag_type": item.get("hashtag_type"),
        "source": source,
    }


def normalize_targeting_catalog(
    *,
    advertiser_id: str,
    language: str,
    interest_categories_payload: Mapping[str, Any],
    targeting_search_payload: Mapping[str, Any],
) -> dict[str, Any]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    legacy_items = _payload_data(interest_categories_payload).get("interest_categories")
    for raw in legacy_items if isinstance(legacy_items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        item = _canonical_item(raw, "GENERAL_INTEREST", source="tool/interest_category")
        if item:
            merged[("GENERAL_INTEREST", item["id"])] = item

    search_data = _payload_data(targeting_search_payload)
    for response_key, targeting_type in _TYPE_KEYS.items():
        group = search_data.get(response_key)
        rows = group.get("list_result") if isinstance(group, Mapping) else None
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            item = _canonical_item(raw, targeting_type, source="targeting/search")
            if not item:
                continue
            key = (str(item["targeting_type"]), item["id"])
            existing = merged.get(key, {})
            merged[key] = {
                **existing,
                **{field: value for field, value in item.items() if value not in (None, "", [], 0)},
                "id": item["id"],
                "targeting_type": str(item["targeting_type"]),
                "source": "targeting/search",
            }

    parent_ids: dict[tuple[str, str], list[str]] = {}
    for (targeting_type, item_id), item in merged.items():
        for child_id in item.get("children_ids") or []:
            parent_ids.setdefault((targeting_type, str(child_id)), []).append(item_id)
    for key, item in merged.items():
        item["parent_ids"] = sorted(set(parent_ids.get(key, [])))

    categories = sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("targeting_type") or ""),
            int(item.get("level") or 0),
            str(item.get("name") or "").casefold(),
            str(item.get("id") or ""),
        ),
    )
    counts: dict[str, int] = {}
    for item in categories:
        key = str(item.get("targeting_type") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "api_version": CATALOG_API_VERSION,
        "advertiser_id": str(advertiser_id),
        "language": str(language),
        "placements": ["PLACEMENT_TIKTOK"],
        "generated_at": _iso_now(),
        "counts": counts,
        "categories": categories,
        "raw_responses": {
            "interest_category_v2": dict(interest_categories_payload),
            "targeting_search": dict(targeting_search_payload),
        },
    }


def load_targeting_catalog(
    advertiser_id: str,
    language: str = "en",
    *,
    root: Path | str | None = None,
) -> dict[str, Any] | None:
    path = catalog_path(advertiser_id, language, root=root)
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def catalog_is_fresh(
    advertiser_id: str,
    language: str = "en",
    *,
    root: Path | str | None = None,
    max_age_seconds: int | None = None,
) -> bool:
    path = catalog_path(advertiser_id, language, root=root)
    if not path.is_file():
        return False
    max_age = int(max_age_seconds or settings.WEBSITE_ADS_TARGETING_CATALOG_MAX_AGE_SECONDS)
    return datetime.fromtimestamp(path.stat().st_mtime) >= datetime.now() - timedelta(seconds=max_age)


async def sync_targeting_catalog(
    api: TikTokWebsiteAdsClient,
    advertiser_id: str,
    *,
    language: str = "en",
    root: Path | str | None = None,
) -> dict[str, Any]:
    official_payload = await api.list_interest_categories(
        advertiser_id,
        version=2,
        language=language,
        placements=["PLACEMENT_TIKTOK"],
    )
    search_payload = await api.search_targeting(
        advertiser_id,
        "INTEREST_AND_BEHAVIOR",
        [],
        sub_targeting_types=list(LISTABLE_SUB_TARGETING_TYPES),
        language=language,
    )
    catalog = normalize_targeting_catalog(
        advertiser_id=advertiser_id,
        language=language,
        interest_categories_payload=official_payload,
        targeting_search_payload=search_payload,
    )
    path = catalog_path(advertiser_id, language, root=root)
    with _exclusive_file_lock(path.with_suffix(".lock")):
        _atomic_json_write(path, catalog)
    return {"path": str(path), "counts": catalog["counts"], "generated_at": catalog["generated_at"]}


async def sync_all_targeting_catalogs(
    db: Session,
    *,
    workspace_id: int | None = None,
    force: bool = False,
    language: str = "en",
) -> dict[str, Any]:
    query = select(TTBBindingConfig).where(TTBBindingConfig.advertiser_id.is_not(None))
    if workspace_id is not None:
        query = query.where(TTBBindingConfig.workspace_id == int(workspace_id))
    bindings = list(db.scalars(query.order_by(TTBBindingConfig.id)).all())
    synced: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for binding in bindings:
        advertiser_id = str(binding.advertiser_id or "")
        if not advertiser_id:
            continue
        if not force and catalog_is_fresh(advertiser_id, language):
            skipped.append(advertiser_id)
            continue
        api = TikTokWebsiteAdsClient(build_ttb_client(db, int(binding.auth_id)))
        try:
            result = await sync_targeting_catalog(api, advertiser_id, language=language)
            synced.append({"advertiser_id": advertiser_id, **result})
        except Exception as exc:
            logger.exception("TikTok targeting catalog sync failed", extra={"advertiser_id": advertiser_id})
            errors.append({"advertiser_id": advertiser_id, "error": f"{type(exc).__name__}: {exc}"[:1000]})
        finally:
            await api.aclose()
    return {
        "bindings": len(bindings),
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
    }


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


_AMBIGUOUS_INTEREST_TOKENS = {
    "care",
    "daily",
    "online",
    "personal",
    "product",
    "products",
    "routine",
    "shopping",
}


def _contains_token_phrase(left: str, right: str) -> bool:
    """Return true only for whole-token phrase containment."""

    return f" {left} " in f" {right} " or f" {right} " in f" {left} "


def _category_context_is_compatible(name: str, keywords: list[str]) -> bool:
    name_tokens = set(_normalize_text(name).split())
    keyword_tokens = {
        token
        for keyword in keywords
        for token in _normalize_text(keyword).split()
    }
    if name_tokens & {"appliance", "appliances"} and not keyword_tokens & {
        "appliance",
        "appliances",
        "device",
        "devices",
        "electric",
        "equipment",
        "tool",
        "tools",
    }:
        return False
    return True


def _keyword_score(keyword: str, name: str, level: int) -> float:
    keyword_text = _normalize_text(keyword)
    name_text = _normalize_text(name)
    if not keyword_text or not name_text:
        return 0.0
    if keyword_text == name_text:
        return 10.0 + min(max(level, 0), 5) * 0.05
    phrase_match = _contains_token_phrase(keyword_text, name_text)
    if phrase_match and len(keyword_text.split()) >= 2:
        return 6.0 + min(max(level, 0), 5) * 0.05
    keyword_tokens = {
        token
        for token in keyword_text.split()
        if len(token) > 2 and token not in _AMBIGUOUS_INTEREST_TOKENS
    }
    name_tokens = {
        token
        for token in name_text.split()
        if len(token) > 2 and token not in _AMBIGUOUS_INTEREST_TOKENS
    }
    if not keyword_tokens or not name_tokens:
        return 0.0
    overlap = len(keyword_tokens & name_tokens)
    if overlap == 0:
        return 0.0
    coverage = overlap / len(keyword_tokens)
    precision = overlap / len(name_tokens)
    return coverage * 3.0 + precision + float(phrase_match) * 2.0 + min(max(level, 0), 5) * 0.05


def _load_discoveries(
    advertiser_id: str,
    language: str,
    *,
    root: Path | str | None = None,
) -> dict[str, Any]:
    path = discoveries_path(advertiser_id, language, root=root)
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def match_general_interest_ids(
    advertiser_id: str,
    keywords: list[str],
    *,
    language: str = "en",
    limit: int = 8,
    root: Path | str | None = None,
) -> list[str]:
    found: list[str] = []
    discoveries = _load_discoveries(advertiser_id, language, root=root).get("keywords")
    if isinstance(discoveries, Mapping):
        for keyword in keywords:
            entry = discoveries.get(_normalize_text(keyword))
            rows = entry.get("results") if isinstance(entry, Mapping) else None
            for item in rows if isinstance(rows, list) else []:
                if str(item.get("targeting_type") or "") != "GENERAL_INTEREST":
                    continue
                item_id = str(item.get("id") or "")
                if item_id and item_id not in found:
                    found.append(item_id)
                if len(found) >= limit:
                    return found

    catalog = load_targeting_catalog(advertiser_id, language, root=root) or {}
    categories = catalog.get("categories")
    general = [
        item for item in (categories if isinstance(categories, list) else [])
        if isinstance(item, Mapping) and str(item.get("targeting_type")) == "GENERAL_INTEREST"
    ]
    candidates: list[tuple[float, str]] = []
    for keyword in keywords:
        ranked: list[tuple[float, str]] = []
        for item in general:
            item_id = str(item.get("id") or "")
            score = _keyword_score(keyword, str(item.get("name") or ""), int(item.get("level") or 0))
            if item_id and score >= 3.0:
                ranked.append((score, item_id))
        candidates.extend(sorted(ranked, reverse=True)[:2])
    for _score, item_id in sorted(candidates, reverse=True):
        if item_id not in found:
            found.append(item_id)
        if len(found) >= limit:
            break
    return found


def rank_general_interest_categories(
    advertiser_id: str,
    keywords: list[str],
    *,
    exclude_ids: set[str] | None = None,
    language: str = "en",
    limit: int = 20,
    root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Rank verified official interests for an untested audience challenger."""

    excluded = {str(value) for value in (exclude_ids or set()) if str(value)}
    catalog = load_targeting_catalog(advertiser_id, language, root=root) or {}
    categories = catalog.get("categories")
    general = [
        item
        for item in (categories if isinstance(categories, list) else [])
        if isinstance(item, Mapping)
        and str(item.get("targeting_type") or "") == "GENERAL_INTEREST"
        and str(item.get("id") or "") not in excluded
    ]
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for item in general:
        item_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if not _category_context_is_compatible(name, keywords):
            continue
        level = int(item.get("level") or 0)
        score = max((_keyword_score(keyword, name, level) for keyword in keywords), default=0.0)
        if item_id and score >= 2.0:
            ranked.append((score, name.casefold(), dict(item)))
    ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "level": int(item.get("level") or 0),
            "score": round(float(score), 4),
            "source": str(item.get("source") or "official_catalog"),
        }
        for score, _name, item in ranked[: max(1, int(limit))]
    ]


def _discovery_results(payload: Mapping[str, Any], keyword: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for response_key, default_type in _TYPE_KEYS.items():
        group = _payload_data(payload).get(response_key)
        search_result = group.get("search_result") if isinstance(group, Mapping) else None
        if not isinstance(search_result, Mapping):
            continue
        rows = search_result.get(keyword)
        if not isinstance(rows, list):
            rows = next((value for value in search_result.values() if isinstance(value, list)), [])
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            item = _canonical_item(raw, default_type, source="targeting/search:keyword")
            if item and not any(
                existing["id"] == item["id"] and existing["targeting_type"] == item["targeting_type"]
                for existing in results
            ):
                results.append(item)
    return results


def record_targeting_discovery(
    advertiser_id: str,
    keyword: str,
    payload: Mapping[str, Any],
    *,
    language: str = "en",
    root: Path | str | None = None,
) -> None:
    normalized_keyword = _normalize_text(keyword)
    if not normalized_keyword:
        return
    path = discoveries_path(advertiser_id, language, root=root)
    with _exclusive_file_lock(path.with_suffix(".lock")):
        document = _load_discoveries(advertiser_id, language, root=root)
        keywords = document.get("keywords")
        if not isinstance(keywords, dict):
            keywords = {}
        previous = keywords.get(normalized_keyword)
        now = _iso_now()
        keywords[normalized_keyword] = {
            "query": keyword,
            "first_seen_at": previous.get("first_seen_at") if isinstance(previous, Mapping) else now,
            "last_verified_at": now,
            "verification_count": int(previous.get("verification_count") or 0) + 1 if isinstance(previous, Mapping) else 1,
            "results": _discovery_results(payload, keyword),
            "request_id": payload.get("request_id"),
        }
        document = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "api_version": CATALOG_API_VERSION,
            "advertiser_id": advertiser_id,
            "language": language,
            "updated_at": now,
            "keywords": keywords,
        }
        _atomic_json_write(path, document)
