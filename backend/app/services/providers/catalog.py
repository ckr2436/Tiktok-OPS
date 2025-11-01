"""Utilities for persisting provider registry metadata."""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.providers import PlatformProvider
from app.services.providers.base import registry


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    """Static metadata for a provider implementation."""

    key: str
    display_name: str
    capabilities: Mapping[str, object]


def _normalize_key(key: str) -> str:
    return key.strip().lower()


def iter_registry_definitions() -> Iterable[ProviderDefinition]:
    """Yield provider definitions declared in the in-memory registry."""

    for provider in registry.list():
        yield ProviderDefinition(
            key=_normalize_key(provider.key()),
            display_name=provider.display_name(),
            capabilities=dict(provider.capabilities()),
        )


def _ensure_builtin_providers_loaded() -> None:
    """Import bundled provider modules so registry definitions are available."""

    importlib.import_module("app.providers.tiktok_business.service")


def sync_registry_with_session(db: Session) -> list[PlatformProvider]:
    """Ensure all registry providers have corresponding DB records.

    Returns a list of PlatformProvider rows that were created or updated.
    """

    changed: list[PlatformProvider] = []

    _ensure_builtin_providers_loaded()

    existing = {
        row.key: row
        for row in db.scalars(select(PlatformProvider)).all()
    }

    for definition in iter_registry_definitions():
        key = definition.key
        if not key:
            continue
        provider = existing.get(key)
        if provider is None:
            provider = PlatformProvider(
                key=key,
                display_name=definition.display_name or key,
                is_enabled=True,
            )
            db.add(provider)
            existing[key] = provider
            changed.append(provider)
        else:
            display_name = definition.display_name or provider.display_name or key
            if provider.display_name != display_name:
                provider.display_name = display_name
                db.add(provider)
                changed.append(provider)

    if changed:
        db.flush()

    return changed


def sync_registry() -> None:
    """Synchronise provider registry with the database using a new session."""

    session = SessionLocal()
    try:
        sync_registry_with_session(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_provider(db: Session, key: str) -> PlatformProvider | None:
    """Fetch a provider row by key (case insensitive)."""

    normalized = _normalize_key(key)
    if not normalized:
        return None
    return db.scalar(
        select(PlatformProvider).where(PlatformProvider.key == normalized)
    )


def list_configured_providers(db: Session) -> list[PlatformProvider]:
    """Return configured providers ordered by display name."""

    return db.scalars(
        select(PlatformProvider).order_by(PlatformProvider.display_name)
    ).all()


__all__ = [
    "ProviderDefinition",
    "iter_registry_definitions",
    "sync_registry_with_session",
    "sync_registry",
    "get_provider",
    "list_configured_providers",
]
