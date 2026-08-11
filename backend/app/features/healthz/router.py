from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.data.db import engine


router = APIRouter(prefix="/api", tags=["health"])

_REQUIRED_TABLES = (
    "users",
    "workspaces",
    "oauth_accounts_ttb",
)


def _assert_database_ready() -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            for table in _REQUIRED_TABLES:
                connection.execute(text(f"SELECT 1 FROM `{table}` LIMIT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SERVICE_NOT_READY",
                "message": "Database schema is unavailable.",
            },
        ) from exc


@router.get("/healthz")
@router.get("/readyz")
def healthz() -> dict[str, bool]:
    _assert_database_ready()
    return {"ok": True}
