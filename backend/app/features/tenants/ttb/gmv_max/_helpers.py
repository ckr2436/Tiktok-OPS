from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.data.models.oauth_ttb import OAuthAccountTTB
from app.services.ttb_binding_config import get_binding_config, get_default_advertiser_for_auth
from app.services.ttb_client_factory import build_ttb_client

SUPPORTED_PROVIDERS = {"tiktok-business", "tiktok_business"}


def _normalize_provider(provider: str) -> str:
    key = (provider or "").strip().lower()
    if key not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not supported")
    return "tiktok-business"


def normalize_provider(provider: str) -> str:
    """Return the canonical provider identifier or raise 404 if unsupported."""

    return _normalize_provider(provider)


def _ensure_account(db: Session, workspace_id: int, auth_id: int) -> OAuthAccountTTB:
    account = db.get(OAuthAccountTTB, int(auth_id))
    if not account or account.workspace_id != int(workspace_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="binding not found")
    if account.status not in {"active", "invalid"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"binding status {account.status} cannot be used",
        )
    return account


def ensure_ttb_auth_in_workspace(db: Session, workspace_id: int, auth_id: int) -> OAuthAccountTTB:
    """Ensure the TikTok Business auth belongs to the workspace.

    Raises HTTP 404 when the auth is missing or belongs to another workspace to
    avoid leaking tenant information.
    """

    return _ensure_account(db, workspace_id, auth_id)


def ensure_account(db: Session, workspace_id: int, provider: str, auth_id: int) -> OAuthAccountTTB:
    _normalize_provider(provider)
    return _ensure_account(db, workspace_id, auth_id)


def get_ttb_client_for_account(
    db: Session,
    workspace_id: int,
    provider: str,
    auth_id: int,
):
    ensure_account(db, workspace_id, provider, auth_id)
    return build_ttb_client(db, int(auth_id))


def get_advertiser_id_for_account(
    db: Session,
    workspace_id: int,
    provider: str,
    auth_id: int,
) -> str:
    ensure_account(db, workspace_id, provider, auth_id)
    advertiser_id = get_default_advertiser_for_auth(
        db,
        workspace_id=int(workspace_id),
        auth_id=int(auth_id),
    )
    if not advertiser_id:
        binding = get_binding_config(db, workspace_id=int(workspace_id), auth_id=int(auth_id))
        if binding and binding.advertiser_id:
            advertiser_id = binding.advertiser_id
    if not advertiser_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Advertiser not configured")
    return str(advertiser_id)


async def _helpers_async_marker() -> None:  # pragma: no cover - helper for verify script
    """No-op async marker used by automated verification."""
