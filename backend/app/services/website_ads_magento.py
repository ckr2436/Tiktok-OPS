from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models.website_ads import WebsiteAdsLandingPage, WebsiteAdsMagentoConnection
from app.services.crypto import decrypt_blob_to_text, encrypt_text_to_blob
from app.services.oauth_ttb import get_or_bootstrap_key_version
from app.services.website_ads_products import auto_bind_content_product


class MagentoSyncError(RuntimeError):
    pass


def _normalize_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Magento URL must be an absolute HTTPS URL")
    return value


def _aad(connection_id: int) -> str:
    return f"website_ads_magento:{int(connection_id)}"


def _snapshot_rows(payload: Any) -> tuple[list[Any], bool]:
    """Return raw rows plus evidence that one response is a full snapshot."""

    if isinstance(payload, list):
        return list(payload), True
    if not isinstance(payload, Mapping) or "items" not in payload:
        return [], False
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return [], False

    complete = True
    page_info = payload.get("page_info")
    if page_info is not None:
        if not isinstance(page_info, Mapping):
            return list(raw_items), False
        try:
            current_page = int(
                page_info.get("page")
                or page_info.get("current_page")
                or 1
            )
            total_pages = int(
                page_info.get("total_page")
                or page_info.get("total_pages")
                or 1
            )
        except (TypeError, ValueError):
            complete = False
        else:
            complete = current_page == 1 and total_pages <= 1
        has_more = page_info.get("has_more")
        if has_more not in (None, False, 0, "0", "false", "False"):
            complete = False

    total = payload.get("total_count", payload.get("total"))
    if total is not None:
        try:
            complete = complete and int(total) == len(raw_items)
        except (TypeError, ValueError):
            complete = False
    return list(raw_items), complete


def _parse_active(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError("is_active must be boolean")


def _validate_landing_page_item(
    item: Any,
    *,
    seen: set[str],
) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    raw = dict(item)
    external_id = str(raw.get("id") or "").strip()
    landing_url = str(raw.get("landing_url") or "").strip()
    parsed_url = urlparse(landing_url)
    if (
        not external_id
        or external_id in seen
        or parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
    ):
        return None

    website_id = None
    if raw.get("website_id") not in (None, ""):
        try:
            website_id = int(raw["website_id"])
        except (TypeError, ValueError):
            return None

    reference_price = None
    price = raw.get("reference_price")
    if price not in (None, ""):
        try:
            reference_price = Decimal(str(price))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not reference_price.is_finite() or reference_price < 0:
            return None

    try:
        is_active = _parse_active(raw.get("is_active", True))
    except ValueError:
        return None
    currency = str(raw.get("currency") or "USD").strip().upper()
    if not currency or len(currency) > 8:
        return None

    seen.add(external_id)
    return {
        "raw": raw,
        "external_id": external_id,
        "landing_url": landing_url,
        "website_id": website_id,
        "reference_price": reference_price,
        "is_active": is_active,
        "currency": currency,
    }


def create_connection(db: Session, *, workspace_id: int, name: str, base_url: str, access_token: str, is_enabled: bool) -> WebsiteAdsMagentoConnection:
    row = WebsiteAdsMagentoConnection(
        workspace_id=int(workspace_id),
        name=name.strip(),
        base_url=_normalize_base_url(base_url),
        access_token_cipher=b"pending",
        key_version=get_or_bootstrap_key_version(db),
        is_enabled=bool(is_enabled),
    )
    db.add(row)
    db.flush()
    row.access_token_cipher = encrypt_text_to_blob(access_token.strip(), key_version=int(row.key_version), aad_text=_aad(row.id))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_connection(db: Session, row: WebsiteAdsMagentoConnection, **changes) -> WebsiteAdsMagentoConnection:
    if changes.get("name") is not None:
        row.name = str(changes["name"]).strip()
    if changes.get("base_url") is not None:
        row.base_url = _normalize_base_url(str(changes["base_url"]))
    if changes.get("is_enabled") is not None:
        row.is_enabled = bool(changes["is_enabled"])
    if changes.get("access_token"):
        row.access_token_cipher = encrypt_text_to_blob(
            str(changes["access_token"]).strip(), key_version=int(row.key_version), aad_text=_aad(row.id)
        )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


async def sync_landing_pages(
    db: Session,
    row: WebsiteAdsMagentoConnection,
    *,
    complete_snapshot: bool = False,
) -> dict:
    token = decrypt_blob_to_text(row.access_token_cipher, aad_text=_aad(row.id))
    url = f"{row.base_url.rstrip('/')}/rest/V1/pynarae/tiktok-landing-pages"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.get(
                url,
                params={"activeOnly": "true"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        response.raise_for_status()
        payload = response.json()
        items, response_snapshot_complete = _snapshot_rows(payload)
        if not isinstance(payload, (list, Mapping)):
            raise MagentoSyncError("Magento landing-page response is not a list")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seen: set[str] = set()
        valid_items: list[dict[str, Any]] = []
        invalid_rows = 0
        for item in items:
            normalized = _validate_landing_page_item(item, seen=seen)
            if normalized is None:
                invalid_rows += 1
                continue
            valid_items.append(normalized)

        created = 0
        updated = 0
        for normalized in valid_items:
            item = normalized["raw"]
            external_id = normalized["external_id"]
            landing_url = normalized["landing_url"]
            existing = db.scalar(
                select(WebsiteAdsLandingPage).where(
                    WebsiteAdsLandingPage.connection_id == row.id,
                    WebsiteAdsLandingPage.external_id == external_id,
                )
            )
            target = existing or WebsiteAdsLandingPage(
                workspace_id=row.workspace_id,
                connection_id=row.id,
                external_id=external_id,
                identifier=str(item.get("identifier") or external_id),
                title=str(item.get("title") or item.get("content_name") or external_id),
                landing_url=landing_url,
            )
            target.website_id = normalized["website_id"]
            target.identifier = str(item.get("identifier") or external_id)
            target.title = str(item.get("title") or item.get("content_name") or external_id)
            target.landing_url = landing_url
            target.product_id = str(item.get("product_id") or "") or None
            target.content_name = str(item.get("content_name") or "") or None
            target.content_category = str(item.get("content_category") or "") or None
            target.brand = str(item.get("brand") or "") or None
            target.description = str(item.get("description") or "") or None
            target.reference_price = normalized["reference_price"]
            target.currency = normalized["currency"]
            target.image_url = str(item.get("hero_image_url") or item.get("promo_image_url") or "") or None
            target.is_active = normalized["is_active"]
            target.raw_json = item
            target.external_updated_at = str(item.get("updated_at") or "") or None
            target.last_synced_at = now
            auto_bind_content_product(db, target)
            db.add(target)
            created += int(existing is None)
            updated += int(existing is not None)

        reconciliation_applied = bool(
            complete_snapshot
            and response_snapshot_complete
            and invalid_rows == 0
        )
        stale: list[WebsiteAdsLandingPage] = []
        if reconciliation_applied:
            stale_query = select(WebsiteAdsLandingPage).where(
                WebsiteAdsLandingPage.connection_id == row.id,
            )
            if seen:
                stale_query = stale_query.where(
                    WebsiteAdsLandingPage.external_id.not_in(seen)
                )
            stale = list(db.scalars(stale_query).all())
            for item in stale:
                item.is_active = False
                db.add(item)
        row.last_sync_at = now
        if complete_snapshot and not reconciliation_applied:
            row.last_error = (
                "SnapshotIncomplete: landing-page absence reconciliation "
                f"skipped; response_complete={response_snapshot_complete}, "
                f"invalid_rows={invalid_rows}"
            )[:2000]
        else:
            row.last_error = None
        db.add(row)
        db.commit()
        return {
            "created": created,
            "updated": updated,
            "disabled": len(stale),
            "total": len(items),
            "valid_rows": len(valid_items),
            "invalid_rows": invalid_rows,
            "complete_snapshot": bool(
                complete_snapshot and response_snapshot_complete
            ),
            "reconciliation_applied": reconciliation_applied,
        }
    except Exception as exc:
        db.rollback()
        row = db.get(WebsiteAdsMagentoConnection, row.id)
        if row:
            row.last_error = str(exc)[:2000]
            db.add(row)
            db.commit()
        raise MagentoSyncError(f"Magento landing-page sync failed: {exc}") from exc
