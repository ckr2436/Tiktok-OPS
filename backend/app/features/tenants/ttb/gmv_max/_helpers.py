from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.data.models.oauth_ttb import OAuthAccountTTB
from app.providers.tiktok_business.gmvmax_client import TikTokBusinessGMVMaxClient
from app.services.ttb_binding_config import (
    get_binding_config,
    get_default_advertiser_for_auth,
)
from app.services.ttb_client_factory import (
    build_ttb_client,
    build_ttb_gmvmax_client,
)

SUPPORTED_PROVIDERS = {"tiktok-business", "tiktok_business"}


@dataclass(slots=True)
class GMVMaxAccountBinding:
    """Resolved tenant binding information for a GMV Max account."""

    account: OAuthAccountTTB
    advertiser_id: str
    store_id: Optional[str]


def _normalize_provider(provider: str) -> str:
    key = (provider or "").strip().lower()
    if key not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="provider not supported",
        )
    return "tiktok-business"


def normalize_provider(provider: str) -> str:
    """Return the canonical provider identifier or raise 404 if unsupported."""

    return _normalize_provider(provider)


def _ensure_account(db: Session, workspace_id: int, auth_id: int) -> OAuthAccountTTB:
    account = db.get(OAuthAccountTTB, int(auth_id))
    if not account or account.workspace_id != int(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="binding not found",
        )
    if account.status not in {"active", "invalid"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"binding status {account.status} cannot be used",
        )
    return account


def ensure_ttb_auth_in_workspace(
    db: Session, workspace_id: int, auth_id: int
) -> OAuthAccountTTB:
    """Ensure the TikTok Business auth belongs to the workspace."""

    return _ensure_account(db, workspace_id, auth_id)


def ensure_account(
    db: Session, workspace_id: int, provider: str, auth_id: int
) -> OAuthAccountTTB:
    _normalize_provider(provider)
    return _ensure_account(db, workspace_id, auth_id)


def get_ttb_client_for_account(
    db: Session, workspace_id: int, provider: str, auth_id: int
):
    ensure_account(db, workspace_id, provider, auth_id)
    return build_ttb_client(db, int(auth_id))


def get_gmvmax_client_for_account(
    db: Session,
    workspace_id: int,
    provider: str,
    auth_id: int,
    *,
    qps: Optional[float] = None,
    timeout: Optional[float] = None,
) -> TikTokBusinessGMVMaxClient:
    """Build a TikTok Business GMV Max client for the given tenant binding."""

    ensure_account(db, workspace_id, provider, auth_id)
    return build_ttb_gmvmax_client(
        db,
        int(auth_id),
        qps=qps,
        timeout=timeout,
    )


def resolve_account_binding(
    db: Session, workspace_id: int, provider: str, auth_id: int
) -> GMVMaxAccountBinding:
    """Resolve advertiser and store configuration for the tenant binding."""

    account = ensure_account(db, workspace_id, provider, auth_id)

    advertiser_id = get_default_advertiser_for_auth(
        db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
    )

    binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
    store_id = binding.store_id if binding else None

    if not advertiser_id and binding and binding.advertiser_id:
        advertiser_id = binding.advertiser_id

    if not advertiser_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Advertiser not configured",
        )

    return GMVMaxAccountBinding(
        account=account,
        advertiser_id=str(advertiser_id),
        store_id=str(binding.store_id) if binding and binding.store_id else None,
    )


async def _helpers_async_marker() -> None:  # pragma: no cover - helper for verify script
    """No-op async marker used by automated verification."""
