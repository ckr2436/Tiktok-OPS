from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopShop
from app.data.models.tiktok_shop import (
    TikTokShopFlashSalePolicy,
    TikTokShopFlashSaleRun,
)
from app.services.tiktok_shop_api import TikTokShopAPIClient


logger = logging.getLogger("gmv.tiktok_shop.flash_sale")

ACTIVE_STATUSES = {"ONGOING", "NOT_START"}
CONFLICT_PROVIDER_CODES = {17029022, 17029079, 17029103}
MAX_PRODUCTS_PER_REQUEST = 300
LockOwnershipVerifier = Callable[[], bool]


def _lock_is_owned(verify_lock_ownership: LockOwnershipVerifier | None) -> bool:
    if verify_lock_ownership is None:
        return True
    try:
        return bool(verify_lock_ownership())
    except Exception:  # noqa: BLE001 - inability to prove ownership is lock loss
        logger.exception("TikTok Shop flash-sale lock ownership check failed")
        return False


def _assert_lock_owned(verify_lock_ownership: LockOwnershipVerifier | None) -> None:
    """Fence provider mutations and commits when the distributed lock is lost."""

    if not _lock_is_owned(verify_lock_ownership):
        raise APIError(
            "TIKTOK_SHOP_FLASH_SALE_LOCK_LOST",
            "Flash-sale reconciliation lost its execution lock; retrying safely.",
            409,
            data={"retryable": True},
        )


def _is_lock_lost_error(exc: Exception) -> bool:
    return isinstance(exc, APIError) and exc.code == "TIKTOK_SHOP_FLASH_SALE_LOCK_LOST"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_timestamp(value: datetime) -> int:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return int(aware.astimezone(timezone.utc).timestamp())


def _provider_datetime(value: Any) -> datetime | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)


def _money(value: Any) -> Decimal | None:
    if isinstance(value, Mapping):
        value = value.get("amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _data_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any, key: str) -> list[dict[str, Any]]:
    source = _data_mapping(value).get(key)
    return [dict(item) for item in source or [] if isinstance(item, Mapping)]


def _activity_id(value: Mapping[str, Any]) -> str:
    return str(value.get("id") or value.get("activity_id") or "").strip()


def _activity_products(value: Mapping[str, Any]) -> dict[str, Decimal]:
    products: dict[str, Decimal] = {}
    for item in value.get("products") or []:
        if not isinstance(item, Mapping):
            continue
        product_id = str(item.get("id") or item.get("product_id") or "").strip()
        price = _money(
            item.get("activity_price_amount")
            if item.get("activity_price_amount") is not None
            else item.get("activity_price")
        )
        if product_id and price is not None:
            products[product_id] = price
    return products


def _next_page_token(value: Any) -> str:
    data = _data_mapping(value)
    return str(data.get("next_page_token") or data.get("page_token") or "").strip()


def _error_provider_code(exc: APIError) -> int | None:
    if not isinstance(exc.data, Mapping):
        return None
    try:
        return int(exc.data.get("provider_code"))
    except (TypeError, ValueError):
        return None


def build_product_payload(prices: Mapping[str, Decimal]) -> dict[str, Any]:
    return {
        "products": [
            {
                "id": str(product_id),
                "activity_price_amount": format(Decimal(price), "f"),
                "quantity_limit": -1,
                "quantity_per_user": -1,
                "skus": [],
            }
            for product_id, price in sorted(prices.items())
        ]
    }


def coverage_end(
    intervals: Iterable[tuple[datetime, datetime]],
    *,
    now: datetime,
    max_gap_seconds: int,
) -> datetime | None:
    cursor = now
    covered = False
    for begin_at, end_at in sorted(intervals):
        if end_at <= cursor:
            continue
        if begin_at > cursor + timedelta(seconds=max_gap_seconds):
            break
        cursor = max(cursor, end_at)
        covered = True
    return cursor if covered else None


def activity_title(shop: OAuthTikTokShopShop, begin_at: datetime) -> str:
    zone = ZoneInfo(str(shop.timezone_name or settings.TT_SHOP_DEFAULT_TIMEZONE))
    local = begin_at.replace(tzinfo=timezone.utc).astimezone(zone)
    digest = hashlib.sha1(
        f"{int(shop.id)}:{_utc_timestamp(begin_at)}".encode("utf-8")
    ).hexdigest()[:6]
    return f"MYUPONA Flash {local:%m%d-%H%M}-{digest}"[:50]


@dataclass(slots=True)
class LiveActivity:
    activity_id: str
    title: str
    status: str
    begin_at: datetime
    end_at: datetime
    products: dict[str, Decimal]
    request_id: str | None = None


async def _live_flash_sales(client: TikTokShopAPIClient) -> list[LiveActivity]:
    summaries: dict[str, dict[str, Any]] = {}
    request_ids: dict[str, str | None] = {}
    for status in sorted(ACTIVE_STATUSES):
        token = ""
        seen_tokens: set[str] = set()
        while True:
            result = await client.promotion_activities(
                status=status,
                activity_type="FLASHSALE",
                page_token=token,
            )
            for item in _rows(result.data, "activities"):
                activity_id = _activity_id(item)
                if activity_id:
                    summaries[activity_id] = item
                    request_ids[activity_id] = result.request_id
            next_token = _next_page_token(result.data)
            if not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            token = next_token

    activities: list[LiveActivity] = []
    for activity_id, summary in summaries.items():
        detail_result = await client.get_activity(activity_id)
        detail = _data_mapping(detail_result.data)
        if isinstance(detail.get("activity"), Mapping):
            detail = _data_mapping(detail.get("activity"))
        merged = {**summary, **detail}
        begin_at = _provider_datetime(merged.get("begin_time"))
        end_at = _provider_datetime(merged.get("end_time"))
        status = str(merged.get("status") or "").upper()
        if (
            not begin_at
            or not end_at
            or status not in ACTIVE_STATUSES
            or str(merged.get("activity_type") or "FLASHSALE").upper() != "FLASHSALE"
        ):
            continue
        activities.append(
            LiveActivity(
                activity_id=activity_id,
                title=str(merged.get("title") or "")[:50],
                status=status,
                begin_at=begin_at,
                end_at=end_at,
                products=_activity_products(merged),
                request_id=detail_result.request_id or request_ids.get(activity_id),
            )
        )
    return sorted(activities, key=lambda item: (item.begin_at, item.activity_id))


def _run_key(
    *,
    shop_row_id: int,
    action: str,
    revision: int,
    begin_at: datetime | None,
) -> str:
    stamp = _utc_timestamp(begin_at) if begin_at else int(utc_now().timestamp() // 900)
    return f"flash-sale:{shop_row_id}:{action}:{revision}:{stamp}"


def _new_run(
    db: Session,
    *,
    shop: OAuthTikTokShopShop,
    trigger: str,
    action: str,
    revision: int,
    begin_at: datetime | None,
) -> TikTokShopFlashSaleRun:
    key = _run_key(
        shop_row_id=int(shop.id),
        action=action,
        revision=revision,
        begin_at=begin_at,
    )
    existing = db.scalar(
        select(TikTokShopFlashSaleRun).where(
            TikTokShopFlashSaleRun.idempotency_key == key
        )
    )
    if existing:
        return existing
    row = TikTokShopFlashSaleRun(
        workspace_id=int(shop.workspace_id),
        account_id=int(shop.account_id),
        shop_row_id=int(shop.id),
        trigger=str(trigger)[:32],
        action=action,
        status="running",
        idempotency_key=key,
        started_at=utc_now(),
    )
    db.add(row)
    db.flush()
    return row


def _policy_intervals(
    activities: Sequence[LiveActivity],
    product_id: str,
) -> list[tuple[datetime, datetime]]:
    return [
        (activity.begin_at, activity.end_at)
        for activity in activities
        if product_id in activity.products
    ]


def _latest_activity(
    activities: Sequence[LiveActivity],
    product_id: str,
) -> LiveActivity | None:
    matches = [item for item in activities if product_id in item.products]
    return max(matches, key=lambda item: item.end_at) if matches else None


def _mark_policies(
    policies: Sequence[TikTokShopFlashSalePolicy],
    activities: Sequence[LiveActivity],
    *,
    now: datetime,
    applied: bool,
) -> None:
    coverage_threshold = timedelta(
        seconds=int(settings.TT_SHOP_FLASH_SALE_MIN_COVERAGE_SECONDS)
    )
    for policy in policies:
        current = _latest_activity(activities, str(policy.product_id))
        covered_until = coverage_end(
            _policy_intervals(activities, str(policy.product_id)),
            now=now,
            max_gap_seconds=int(settings.TT_SHOP_FLASH_SALE_GAP_SECONDS) + 5,
        )
        policy.status = "active"
        policy.current_activity_id = current.activity_id if current else None
        policy.current_activity_status = current.status if current else None
        policy.current_begin_at = current.begin_at if current else None
        policy.current_end_at = covered_until
        policy.next_renewal_at = (
            covered_until - coverage_threshold if covered_until else now
        )
        policy.last_checked_at = now
        if applied:
            policy.applied_revision = int(policy.policy_revision)
            policy.last_applied_at = now
        policy.last_error_code = None
        policy.last_error_message = None


def _mark_error(
    policies: Sequence[TikTokShopFlashSalePolicy],
    exc: Exception,
    *,
    now: datetime,
) -> None:
    code = exc.code if isinstance(exc, APIError) else type(exc).__name__
    message = str(getattr(exc, "message", None) or exc)[:1000]
    for policy in policies:
        policy.status = "error"
        policy.last_checked_at = now
        policy.last_error_code = str(code)[:128]
        policy.last_error_message = message


async def _deactivate(
    client: TikTokShopAPIClient,
    activities: Sequence[LiveActivity],
    *,
    verify_lock_ownership: LockOwnershipVerifier | None = None,
) -> list[str]:
    request_ids: list[str] = []
    for activity in activities:
        _assert_lock_owned(verify_lock_ownership)
        result = await client.deactivate_activity(activity.activity_id)
        _assert_lock_owned(verify_lock_ownership)
        if result.request_id:
            request_ids.append(result.request_id)
    return request_ids


async def _create_with_products(
    client: TikTokShopAPIClient,
    *,
    shop: OAuthTikTokShopShop,
    begin_at: datetime,
    end_at: datetime,
    prices: Mapping[str, Decimal],
    verify_lock_ownership: LockOwnershipVerifier | None = None,
) -> tuple[str, list[str]]:
    request_ids: list[str] = []
    _assert_lock_owned(verify_lock_ownership)
    result = await client.create_activity(
        {
            "title": activity_title(shop, begin_at),
            "activity_type": "FLASHSALE",
            "product_level": "PRODUCT",
            "duration_type": "NORMAL",
            "begin_time": _utc_timestamp(begin_at),
            "end_time": _utc_timestamp(end_at),
            "participation_limit": [{"type": "BUYER_NO_LIMIT"}],
        }
    )
    _assert_lock_owned(verify_lock_ownership)
    if result.request_id:
        request_ids.append(result.request_id)
    activity_id = _activity_id(_data_mapping(result.data))
    if not activity_id:
        raise APIError(
            "TIKTOK_SHOP_INVALID_RESPONSE",
            "TikTok Shop did not return the created flash-sale activity ID.",
            502,
            data={"request_id": result.request_id},
        )
    try:
        items = list(prices.items())
        for offset in range(0, len(items), MAX_PRODUCTS_PER_REQUEST):
            _assert_lock_owned(verify_lock_ownership)
            payload = build_product_payload(dict(items[offset : offset + MAX_PRODUCTS_PER_REQUEST]))
            update_result = await client.update_activity_products(activity_id, payload)
            _assert_lock_owned(verify_lock_ownership)
            if update_result.request_id:
                request_ids.append(update_result.request_id)
        verify_result = await client.get_activity(activity_id)
        _assert_lock_owned(verify_lock_ownership)
        if verify_result.request_id:
            request_ids.append(verify_result.request_id)
        detail = _data_mapping(verify_result.data)
        if isinstance(detail.get("activity"), Mapping):
            detail = _data_mapping(detail.get("activity"))
        actual = _activity_products(detail)
        missing = {
            product_id
            for product_id, price in prices.items()
            if actual.get(product_id) != Decimal(price)
        }
        if missing:
            raise APIError(
                "TIKTOK_SHOP_FLASH_SALE_VERIFY_FAILED",
                "TikTok Shop did not confirm all flash-sale product prices.",
                502,
                data={"activity_id": activity_id, "missing_product_ids": sorted(missing)},
            )
    except Exception:
        # Cleanup is another provider mutation. If ownership is already lost,
        # leave official-state recovery to the next fenced reconciliation.
        if _lock_is_owned(verify_lock_ownership):
            try:
                _assert_lock_owned(verify_lock_ownership)
                await client.deactivate_activity(activity_id)
            except Exception:
                logger.exception(
                    "Failed to deactivate incomplete flash-sale activity activity_id=%s",
                    activity_id,
                )
        raise
    return activity_id, request_ids


async def reconcile_flash_sales(
    db: Session,
    *,
    workspace_id: int,
    account_id: int,
    shop_row_id: int,
    trigger: str = "scheduled",
    force_replace: bool = False,
    verify_lock_ownership: LockOwnershipVerifier | None = None,
) -> dict[str, Any]:
    now = utc_now()
    _assert_lock_owned(verify_lock_ownership)
    shop = db.get(OAuthTikTokShopShop, int(shop_row_id))
    if (
        not shop
        or int(shop.workspace_id) != int(workspace_id)
        or int(shop.account_id) != int(account_id)
        or not bool(shop.is_active)
    ):
        raise APIError("TIKTOK_SHOP_NOT_FOUND", "Active TikTok Shop not found.", 404)
    policies = list(
        db.scalars(
            select(TikTokShopFlashSalePolicy)
            .where(
                TikTokShopFlashSalePolicy.workspace_id == int(workspace_id),
                TikTokShopFlashSalePolicy.shop_row_id == int(shop_row_id),
                TikTokShopFlashSalePolicy.enabled.is_(True),
            )
            .order_by(TikTokShopFlashSalePolicy.product_id.asc())
        )
    )
    if not policies:
        return {"status": "skipped", "reason": "no_enabled_policies", "shop_id": int(shop.id)}

    revision = max(int(policy.policy_revision) for policy in policies)
    replace_requested = force_replace or any(
        int(policy.applied_revision) < int(policy.policy_revision)
        for policy in policies
    )
    run: TikTokShopFlashSaleRun | None = None
    client = await TikTokShopAPIClient.create(
        db,
        workspace_id=int(workspace_id),
        account_id=int(account_id),
        shop_row_id=int(shop_row_id),
    )
    try:
        async with client:
            activities = await _live_flash_sales(client)
            threshold = now + timedelta(
                seconds=int(settings.TT_SHOP_FLASH_SALE_MIN_COVERAGE_SECONDS)
            )
            coverage = {
                str(policy.product_id): coverage_end(
                    _policy_intervals(activities, str(policy.product_id)),
                    now=now,
                    max_gap_seconds=int(settings.TT_SHOP_FLASH_SALE_GAP_SECONDS) + 5,
                )
                for policy in policies
            }
            if not replace_requested and all(
                end_at is not None and end_at >= threshold
                for end_at in coverage.values()
            ):
                _assert_lock_owned(verify_lock_ownership)
                run = _new_run(
                    db,
                    shop=shop,
                    trigger=trigger,
                    action="hold",
                    revision=revision,
                    begin_at=None,
                )
                _mark_policies(policies, activities, now=now, applied=False)
                run.status = "succeeded"
                run.details_json = {
                    "reason": "coverage_sufficient",
                    "minimum_coverage_until": min(coverage.values()).isoformat(),
                    "required_until": threshold.isoformat(),
                }
                run.completed_at = utc_now()
                _assert_lock_owned(verify_lock_ownership)
                db.commit()
                return {
                    "status": "succeeded",
                    "action": "hold",
                    "coverage_until": min(coverage.values()).isoformat(),
                }

            due_ids = {
                str(policy.product_id)
                for policy in policies
                if replace_requested
                or coverage[str(policy.product_id)] is None
                or coverage[str(policy.product_id)] < threshold
            }
            conflicts = [
                activity
                for activity in activities
                if due_ids.intersection(activity.products)
            ]
            preserved_prices: dict[str, Decimal] = {}
            for activity in conflicts:
                preserved_prices.update(activity.products)
            for policy in policies:
                if replace_requested or str(policy.product_id) in due_ids:
                    preserved_prices[str(policy.product_id)] = Decimal(
                        policy.activity_price_amount
                    )

            if replace_requested:
                begin_at = now + timedelta(
                    seconds=int(settings.TT_SHOP_FLASH_SALE_START_DELAY_SECONDS)
                )
                activities_to_deactivate = conflicts
                action = "replace"
            else:
                due_ends = [coverage[product_id] for product_id in due_ids if coverage[product_id]]
                begin_at = max(
                    [now + timedelta(seconds=int(settings.TT_SHOP_FLASH_SALE_START_DELAY_SECONDS))]
                    + [
                        end_at + timedelta(seconds=int(settings.TT_SHOP_FLASH_SALE_GAP_SECONDS))
                        for end_at in due_ends
                    ]
                )
                activities_to_deactivate = []
                action = "renew"
                preserved_prices = {
                    str(policy.product_id): Decimal(policy.activity_price_amount)
                    for policy in policies
                    if str(policy.product_id) in due_ids
                }

            end_at = begin_at + timedelta(
                seconds=int(settings.TT_SHOP_FLASH_SALE_DURATION_SECONDS)
            )
            _assert_lock_owned(verify_lock_ownership)
            run = _new_run(
                db,
                shop=shop,
                trigger=trigger,
                action=action,
                revision=revision,
                begin_at=begin_at,
            )
            if run.status == "succeeded":
                return {
                    "status": "skipped",
                    "reason": "idempotent_replay",
                    "run_id": int(run.id),
                }
            request_ids = await _deactivate(
                client,
                activities_to_deactivate,
                verify_lock_ownership=verify_lock_ownership,
            )
            run.previous_activity_ids_json = [
                item.activity_id for item in activities_to_deactivate
            ]
            try:
                new_activity_id, create_request_ids = await _create_with_products(
                    client,
                    shop=shop,
                    begin_at=begin_at,
                    end_at=end_at,
                    prices=preserved_prices,
                    verify_lock_ownership=verify_lock_ownership,
                )
            except APIError as exc:
                provider_code = _error_provider_code(exc)
                if provider_code not in CONFLICT_PROVIDER_CODES or activities_to_deactivate:
                    raise
                conflicts = [
                    activity
                    for activity in activities
                    if set(preserved_prices).intersection(activity.products)
                ]
                request_ids.extend(
                    await _deactivate(
                        client,
                        conflicts,
                        verify_lock_ownership=verify_lock_ownership,
                    )
                )
                run.previous_activity_ids_json = [
                    item.activity_id for item in conflicts
                ]
                begin_at = utc_now() + timedelta(
                    seconds=int(settings.TT_SHOP_FLASH_SALE_START_DELAY_SECONDS)
                )
                end_at = begin_at + timedelta(
                    seconds=int(settings.TT_SHOP_FLASH_SALE_DURATION_SECONDS)
                )
                for activity in conflicts:
                    for product_id, price in activity.products.items():
                        preserved_prices.setdefault(product_id, price)
                new_activity_id, create_request_ids = await _create_with_products(
                    client,
                    shop=shop,
                    begin_at=begin_at,
                    end_at=end_at,
                    prices=preserved_prices,
                    verify_lock_ownership=verify_lock_ownership,
                )
            request_ids.extend(create_request_ids)
            synthetic = LiveActivity(
                activity_id=new_activity_id,
                title=activity_title(shop, begin_at),
                status="NOT_START",
                begin_at=begin_at,
                end_at=end_at,
                products=dict(preserved_prices),
            )
            remaining = [
                item
                for item in activities
                if item.activity_id not in set(run.previous_activity_ids_json or [])
            ]
            _mark_policies(policies, [*remaining, synthetic], now=now, applied=True)
            run.new_activity_id = new_activity_id
            run.provider_request_ids_json = request_ids
            run.details_json = {
                "begin_at": begin_at.isoformat(),
                "end_at": end_at.isoformat(),
                "product_ids": sorted(preserved_prices),
                "price_count": len(preserved_prices),
            }
            run.status = "succeeded"
            run.completed_at = utc_now()
            _assert_lock_owned(verify_lock_ownership)
            db.commit()
            return {
                "status": "succeeded",
                "action": action,
                "activity_id": new_activity_id,
                "begin_at": begin_at.isoformat(),
                "end_at": end_at.isoformat(),
                "products": len(preserved_prices),
            }
    except Exception as exc:
        db.rollback()
        if _is_lock_lost_error(exc):
            # This worker is no longer authorized to publish state, including
            # an error marker. The next lock owner reconciles official state.
            raise
        policies = list(
            db.scalars(
                select(TikTokShopFlashSalePolicy).where(
                    TikTokShopFlashSalePolicy.workspace_id == int(workspace_id),
                    TikTokShopFlashSalePolicy.shop_row_id == int(shop_row_id),
                    TikTokShopFlashSalePolicy.enabled.is_(True),
                )
            )
        )
        _mark_error(policies, exc, now=utc_now())
        failed_run = _new_run(
            db,
            shop=shop,
            trigger=trigger,
            action="replace" if replace_requested else "renew",
            revision=revision,
            begin_at=None,
        )
        failed_run.status = "failed"
        failed_run.error_code = (
            str(exc.code) if isinstance(exc, APIError) else type(exc).__name__
        )[:128]
        failed_run.error_message = str(getattr(exc, "message", None) or exc)[:1000]
        failed_run.completed_at = utc_now()
        db.commit()
        raise
