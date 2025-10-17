# app/services/db_locks.py
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session


def _lock_key(*parts: object) -> str:
    return "ttb:" + ":".join(str(p) for p in parts)


@contextmanager
def mysql_advisory_lock(db: Session, key: str, wait_seconds: int = 1) -> Generator[bool, None, None]:
    """
    使用 MySQL GET_LOCK/RELEASE_LOCK。
    :return: True 表示获得锁，False 表示未获得。
    """
    got = False
    try:
        got = bool(db.execute(text("SELECT GET_LOCK(:k, :t)"), {"k": key, "t": int(wait_seconds)}).scalar())
        yield got
    finally:
        if got:
            try:
                db.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": key})
            except Exception:
                pass


def binding_action_lock_key(workspace_id: int, auth_id: int, action: str) -> str:
    return _lock_key("binding", workspace_id, auth_id, action)

