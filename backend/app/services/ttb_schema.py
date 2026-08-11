"""Schema capability detection helpers for TikTok Business tables."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import NoSuchTableError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

_TABLE = "ttb_advertisers"
_COLUMN = "display_timezone"


def _normalize_engine(bind: Any) -> Engine | Connection | None:
    if bind is None:
        return None
    if isinstance(bind, Engine):
        return bind
    if isinstance(bind, Connection):
        return bind
    engine = getattr(bind, "engine", None)
    if isinstance(engine, Engine):
        return engine
    if isinstance(engine, Connection):
        return engine
    return None


def _cache_key(engine: Engine | Connection) -> str:
    url = getattr(engine, "url", None)
    if url is not None:
        return str(url)
    parent = getattr(engine, "engine", None)
    if parent is not None and getattr(parent, "url", None) is not None:
        return str(parent.url)
    return f"engine:{id(engine)}"


@lru_cache(maxsize=16)
def advertisers_support_display_timezone(key: str) -> bool:
    """Return whether the advertiser table exposes the display timezone column."""

    engine = _ENGINE_REGISTRY.get(key)
    if engine is None:
        return True
    inspector = inspect(engine)
    try:
        columns = inspector.get_columns(_TABLE)
    except (NoSuchTableError, OperationalError, ProgrammingError):
        return False
    return any(column.get("name") == _COLUMN for column in columns)


_ENGINE_REGISTRY: dict[str, Engine | Connection] = {}


def ensure_engine_registered(engine: Engine | Connection) -> str:
    key = _cache_key(engine)
    if key not in _ENGINE_REGISTRY:
        _ENGINE_REGISTRY[key] = engine
    return key


def advertiser_display_timezone_supported(db: Session) -> bool:
    """Check whether the current session can access advertiser.display_timezone."""

    bind = db.get_bind()
    engine = _normalize_engine(bind)
    if engine is None:
        return True
    key = ensure_engine_registered(engine)
    return advertisers_support_display_timezone(key)
