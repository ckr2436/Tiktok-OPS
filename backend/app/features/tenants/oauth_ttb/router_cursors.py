# app/features/tenants/oauth_ttb/router_cursors.py
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy.orm import Session

from app.data.db import get_db
from app.data.models.ttb_entities import TTBSyncCursor

router = APIRouter(tags=["tenant.tiktok-business.cursors"])

BASE_PREFIX = "/api/v1/tenants/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync"


def _norm_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in ("tiktok-business", "tiktok_business"):
        return "tiktok-business"
    raise HTTPException(status_code=400, detail="unsupported provider")


@router.get(f"{BASE_PREFIX}/cursors")
def get_cursors(
    workspace_id: int,
    provider: str,
    auth_id: int,
    db: Session = Depends(get_db),
):
    _norm_provider(provider)
    rows = (
        db.query(TTBSyncCursor)
        .filter(
            TTBSyncCursor.workspace_id == int(workspace_id),
            TTBSyncCursor.auth_id == int(auth_id),
        )
        .all()
    )
    out: Dict[str, Dict] = {}
    for r in rows:
        out[r.resource_type] = {
            "cursor": r.cursor_token,
            "since": r.since_time.isoformat() if r.since_time else None,
            "until": r.until_time.isoformat() if r.until_time else None,
            "last_rev": r.last_rev,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
    return out

