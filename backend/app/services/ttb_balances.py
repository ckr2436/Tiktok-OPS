from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import logging
from sqlalchemy.orm import Session

from app.data.models.ttb_entities import TTBAdvertiserBalance
from app.services.ttb_api import TTBApiClient
from app.services.ttb_client_factory import build_ttb_client

log = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def select_latest_balance(
    db: Session, *, workspace_id: int, auth_id: int, advertiser_id: str
) -> TTBAdvertiserBalance | None:
    return (
        db.query(TTBAdvertiserBalance)
        .filter(TTBAdvertiserBalance.workspace_id == int(workspace_id))
        .filter(TTBAdvertiserBalance.auth_id == int(auth_id))
        .filter(TTBAdvertiserBalance.advertiser_id == str(advertiser_id))
        # MySQL doesn't support "NULLS LAST" syntax. Order by non-null fetched_at first,
        # then by fetched_at desc and id desc for deterministic ordering.
        .order_by(
            TTBAdvertiserBalance.fetched_at.is_(None),
            TTBAdvertiserBalance.fetched_at.desc(),
            TTBAdvertiserBalance.id.desc(),
        )
        .first()
    )


async def fetch_advertiser_balance(
    client: TTBApiClient,
    *,
    bc_id: str,
    advertiser_id: str,
) -> dict[str, Any] | None:
    balances = await client.fetch_advertiser_balances(
        bc_id=str(bc_id),
        advertiser_ids=[advertiser_id],
        page_size=1,
    )
    if not balances:
        return None
    return balances[0]


def upsert_advertiser_balance(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_id: str,
    payload: dict[str, Any],
) -> TTBAdvertiserBalance:
    row = select_latest_balance(db, workspace_id=workspace_id, auth_id=auth_id, advertiser_id=advertiser_id)
    if row is None:
        row = TTBAdvertiserBalance(
            workspace_id=int(workspace_id),
            auth_id=int(auth_id),
            advertiser_id=str(advertiser_id),
        )

    row.currency = payload.get("currency") or row.currency
    balance_value = payload.get("balance")
    row.account_balance = _to_decimal(
        payload.get("account_balance") or payload.get("valid_account_balance") or balance_value
    )
    row.valid_account_balance = _to_decimal(payload.get("valid_account_balance"))
    row.cash_balance = _to_decimal(
        payload.get("cash_balance") or payload.get("valid_cash_balance") or balance_value
    )
    row.valid_cash_balance = _to_decimal(payload.get("valid_cash_balance") or balance_value)
    row.credit_balance = _to_decimal(payload.get("credit_balance") or payload.get("valid_credit_balance"))
    row.valid_credit_balance = _to_decimal(payload.get("valid_credit_balance"))
    row.fetched_at = datetime.now(timezone.utc)
    row.raw_json = payload
    db.add(row)
    db.flush()
    return row


async def sync_advertiser_balance(
    db: Session,
    *,
    workspace_id: int,
    auth_id: int,
    bc_id: str,
    advertiser_id: str,
    qps: float | None = None,
) -> dict[str, Any]:
    client = build_ttb_client(db, auth_id=int(auth_id), qps=qps)
    try:
        payload = await fetch_advertiser_balance(client, bc_id=bc_id, advertiser_id=advertiser_id)
        if not payload:
            log.warning(
                "Advertiser balance not returned",
                extra={
                    "workspace_id": workspace_id,
                    "auth_id": auth_id,
                    "advertiser_id": advertiser_id,
                    "bc_id": bc_id,
                },
            )
            return {"status": "empty"}
        row = upsert_advertiser_balance(
            db,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_id=advertiser_id,
            payload=payload,
        )
        return {
            "status": "success",
            "currency": row.currency,
            "cash_balance": float(row.cash_balance) if row.cash_balance is not None else None,
            "credit_balance": float(row.credit_balance) if row.credit_balance is not None else None,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        }
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            log.warning("Failed to close TTB API client after balance sync", exc_info=True)
