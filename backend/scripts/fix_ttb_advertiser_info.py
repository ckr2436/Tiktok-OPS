#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-off repair script for TikTok Business advertisers.

This utility rehydrates advertiser records whose critical fields are missing by
invoking the official /advertiser/info endpoint in batches. Only rows where
name, timezone or bc_id are NULL will be processed, and the operation is
idempotent.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Tuple

# Ensure app.* imports work when executed from repository root
ROOT_HINTS = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    os.getcwd(),
]
for path in ROOT_HINTS:
    if path not in sys.path:
        sys.path.insert(0, path)

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.data.db import get_db
from app.data.models.ttb_entities import TTBAdvertiser, TTBBCAdvertiserLink
from app.services.oauth_ttb import get_access_token_plain
from app.services.ttb_api import TTBApiClient
from app.services.ttb_sync import TTBSyncService


def _open_db() -> Session:
    generator = get_db()
    session = next(generator)
    setattr(session, "_gen", generator)
    return session


def _close_db(session: Session) -> None:
    generator = getattr(session, "_gen", None)
    if generator:
        try:
            generator.close()
        except Exception:  # noqa: BLE001
            pass


def _collect_targets(
    session: Session,
    *,
    workspace_id: int | None,
    auth_id: int | None,
) -> list[Tuple[Tuple[int, int], list[str]]]:
    conditions = [
        or_(
            TTBAdvertiser.name.is_(None),
            TTBAdvertiser.timezone.is_(None),
            TTBAdvertiser.bc_id.is_(None),
        )
    ]
    if workspace_id is not None:
        conditions.append(TTBAdvertiser.workspace_id == int(workspace_id))
    if auth_id is not None:
        conditions.append(TTBAdvertiser.auth_id == int(auth_id))

    stmt = (
        select(
            TTBAdvertiser.workspace_id,
            TTBAdvertiser.auth_id,
            TTBAdvertiser.advertiser_id,
        )
        .where(and_(*conditions))
        .order_by(TTBAdvertiser.workspace_id.asc(), TTBAdvertiser.auth_id.asc())
    )

    grouped: dict[Tuple[int, int], set[str]] = defaultdict(set)
    for workspace, auth, advertiser_id in session.execute(stmt):
        if not advertiser_id:
            continue
        grouped[(int(workspace), int(auth))].add(str(advertiser_id))

    ordered: list[Tuple[Tuple[int, int], list[str]]] = []
    for key in sorted(grouped.keys()):
        advertiser_ids = sorted(grouped[key])
        if advertiser_ids:
            ordered.append((key, advertiser_ids))
    return ordered


async def _repair_account(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_ids: Iterable[str],
    qps: float,
) -> dict:
    token, _ = get_access_token_plain(session, int(auth_id))
    client = TTBApiClient(access_token=token, qps=float(qps))
    service = TTBSyncService(session, client, workspace_id=workspace_id, auth_id=auth_id)
    try:
        stats = await service.repair_advertisers(advertiser_ids=advertiser_ids)
        backfilled = _backfill_bc_from_links(
            session,
            workspace_id=workspace_id,
            auth_id=auth_id,
            advertiser_ids=advertiser_ids,
        )
        if stats is None:
            stats = {}
        stats["bc_backfilled"] = backfilled
        session.flush()
        session.commit()
        session.expire_all()
        return stats
    finally:
        await service.client.aclose()


async def _run_repairs(
    session: Session,
    items: list[Tuple[Tuple[int, int], list[str]]],
    *,
    qps: float,
) -> list[Tuple[Tuple[int, int], dict, Exception | None]]:
    results: list[Tuple[Tuple[int, int], dict, Exception | None]] = []
    for (workspace_id, auth_id), advertiser_ids in items:
        try:
            stats = await _repair_account(
                session,
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_ids=advertiser_ids,
                qps=qps,
            )
            results.append(((workspace_id, auth_id), stats, None))
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append(((workspace_id, auth_id), {}, exc))
    return results


def _backfill_bc_from_links(
    session: Session,
    *,
    workspace_id: int,
    auth_id: int,
    advertiser_ids: Iterable[str],
) -> int:
    normalized_ids = {str(item).strip() for item in advertiser_ids if str(item).strip()}
    if not normalized_ids:
        return 0

    link_rows = (
        session.query(TTBBCAdvertiserLink.advertiser_id, TTBBCAdvertiserLink.bc_id)
        .filter(TTBBCAdvertiserLink.workspace_id == int(workspace_id))
        .filter(TTBBCAdvertiserLink.auth_id == int(auth_id))
        .filter(TTBBCAdvertiserLink.advertiser_id.in_(normalized_ids))
        .all()
    )

    candidates: dict[str, set[str]] = defaultdict(set)
    for adv_id, bc_id in link_rows:
        adv_key = str(adv_id).strip() if adv_id else ""
        bc_value = str(bc_id).strip() if bc_id else ""
        if not adv_key or not bc_value:
            continue
        candidates[adv_key].add(bc_value)

    if not candidates:
        return 0

    rows = (
        session.query(TTBAdvertiser)
        .filter(TTBAdvertiser.workspace_id == int(workspace_id))
        .filter(TTBAdvertiser.auth_id == int(auth_id))
        .filter(TTBAdvertiser.advertiser_id.in_(normalized_ids))
        .all()
    )

    updated = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        adv_key = str(row.advertiser_id).strip() if row and row.advertiser_id else ""
        if not adv_key or row.bc_id:
            continue
        options = candidates.get(adv_key) or set()
        if len(options) != 1:
            continue
        bc_value = next(iter(options))
        if not bc_value:
            continue
        row.bc_id = bc_value
        row.last_seen_at = now
        session.add(row)
        updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair TikTok Business advertisers missing name/timezone/bc_id by refetching advertiser info.",
    )
    parser.add_argument("--workspace-id", type=int, help="Limit to a specific workspace_id")
    parser.add_argument("--auth-id", type=int, help="Limit to a specific OAuth account (auth_id)")
    parser.add_argument(
        "--qps",
        type=float,
        default=3.0,
        help="Maximum QPS for advertiser/info requests (default: 3.0)",
    )
    args = parser.parse_args()

    session = _open_db()
    try:
        targets = _collect_targets(session, workspace_id=args.workspace_id, auth_id=args.auth_id)
        if not targets:
            print("No advertisers require repair.")
            return

        print(f"Found {len(targets)} account(s) with advertisers to repair.")
        loop_results = asyncio.run(_run_repairs(session, targets, qps=args.qps))

        repaired_accounts = 0
        repaired_advertisers = 0
        for (workspace_id, auth_id), stats, error in loop_results:
            label = f"workspace={workspace_id} auth_id={auth_id}"
            if error:
                print(f"[ERROR] {label}: {error}")
                continue
            repaired_accounts += 1
            batches = int(stats.get("batches", 0))
            updates = int(stats.get("updates", 0))
            backfilled = int(stats.get("bc_backfilled", 0))
            repaired_advertisers += updates
            print(
                f"[OK] {label}: batches={batches} updates={updates} bc_backfilled={backfilled}",
            )

        print(
            f"Completed advertiser repair: {repaired_accounts} account(s), {repaired_advertisers} advertiser(s) updated.",
        )
    finally:
        _close_db(session)


if __name__ == "__main__":
    main()
