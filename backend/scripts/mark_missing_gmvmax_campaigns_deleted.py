"""Mark GMV Max campaigns as deleted when missing from the latest sync snapshots.

This one-off utility compares ``ttb_gmvmax_campaigns`` against
``ttb_gmvmax_campaign_sync_snapshots`` and soft-deletes any campaign that no
longer appears in the snapshot table for the same workspace/auth/advertiser
and store scope. The update is idempotent and can be scoped by workspace or
account if desired.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

# Ensure app.* imports work when executed from repository root
ROOT_HINTS = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    os.getcwd(),
]
for path in ROOT_HINTS:
    if path not in sys.path:
        sys.path.insert(0, path)

from app.data.db import get_db  # noqa: E402
from app.data.models.ttb_gmvmax import (  # noqa: E402
    TTBGmvMaxCampaign,
    TTBGmvMaxCampaignSyncSnapshot,
)


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
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def _build_scope_filters(
    *, workspace_id: int | None, auth_id: int | None, advertiser_id: str | None
) -> Iterable:
    conditions = []
    if workspace_id is not None:
        conditions.append(TTBGmvMaxCampaign.workspace_id == int(workspace_id))
    if auth_id is not None:
        conditions.append(TTBGmvMaxCampaign.auth_id == int(auth_id))
    if advertiser_id is not None:
        conditions.append(TTBGmvMaxCampaign.advertiser_id == str(advertiser_id))
    return conditions


def mark_missing_campaigns(
    session: Session,
    *,
    workspace_id: int | None,
    auth_id: int | None,
    advertiser_id: str | None,
    dry_run: bool = False,
) -> int:
    """Soft-delete campaigns that no longer exist in the snapshot table."""

    scope_filters = list(
        _build_scope_filters(
            workspace_id=workspace_id, auth_id=auth_id, advertiser_id=advertiser_id
        )
    )

    snapshot_exists = (
        select(TTBGmvMaxCampaignSyncSnapshot.id)
        .where(
            TTBGmvMaxCampaignSyncSnapshot.workspace_id
            == TTBGmvMaxCampaign.workspace_id
        )
        .where(TTBGmvMaxCampaignSyncSnapshot.auth_id == TTBGmvMaxCampaign.auth_id)
        .where(
            TTBGmvMaxCampaignSyncSnapshot.advertiser_id
            == TTBGmvMaxCampaign.advertiser_id
        )
        .where(TTBGmvMaxCampaignSyncSnapshot.store_id == TTBGmvMaxCampaign.store_id)
        .where(
            TTBGmvMaxCampaignSyncSnapshot.campaign_id == TTBGmvMaxCampaign.campaign_id
        )
    )

    delete_stmt = (
        update(TTBGmvMaxCampaign)
        .where(~exists(snapshot_exists))
        .where(TTBGmvMaxCampaign.is_deleted.is_(False))
        .values(
            status="DELETE",
            operation_status="DELETE",
            secondary_status="CAMPAIGN_STATUS_DELETE",
            is_deleted=True,
            deleted_at=datetime.now(timezone.utc),
        )
    )

    for condition in scope_filters:
        delete_stmt = delete_stmt.where(condition)

    result = session.execute(delete_stmt)

    if dry_run:
        session.rollback()
    else:
        session.commit()

    return int(result.rowcount or 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", type=int, default=None)
    parser.add_argument("--auth-id", type=int, default=None)
    parser.add_argument("--advertiser-id", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Print affected rows without committing")
    args = parser.parse_args()

    session = _open_db()
    try:
        affected = mark_missing_campaigns(
            session,
            workspace_id=args.workspace_id,
            auth_id=args.auth_id,
            advertiser_id=args.advertiser_id,
            dry_run=args.dry_run,
        )
        action = "would mark" if args.dry_run else "marked"
        print(f"{action} {affected} campaign(s) as deleted.")
    finally:
        _close_db(session)


if __name__ == "__main__":
    main()
