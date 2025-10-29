from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

try:  # pragma: no cover - testing shim
    import email_validator  # type: ignore # noqa: F401
except ImportError:  # pragma: no cover
    from pydantic import networks as _pydantic_networks

    def _noop_import_email_validator() -> None:
        _pydantic_networks.email_validator = object()

    _pydantic_networks.import_email_validator = _noop_import_email_validator  # type: ignore[attr-defined]


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


TEST_DB_PATH = ROOT / "test_platform.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"


@event.listens_for(Engine, "before_cursor_execute", retval=True)
def _sqlite_timestamp_precision_fix(
    conn, cursor, statement, parameters, context, executemany
):
    if conn.dialect.name == "sqlite":
        if "CURRENT_TIMESTAMP(6)" in statement:
            statement = statement.replace("CURRENT_TIMESTAMP(6)", "CURRENT_TIMESTAMP")
        stripped = statement.lstrip().upper()
        if stripped.startswith("CREATE INDEX ") and " IF NOT EXISTS " not in stripped:
            statement = statement.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
    return statement, parameters


@pytest.fixture(autouse=True)
def _reset_database() -> Generator[None, None, None]:
    from app.data.db import Base, engine, SessionLocal
    import app.data.models  # noqa: F401 - ensure models registered

    SessionLocal.close_all()
    engine.dispose()
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()

    Base.metadata.create_all(bind=engine)

    yield

    SessionLocal.close_all()
    engine.dispose()
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest.fixture()
def db_session() -> Generator:
    from app.data.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app_client() -> Generator[tuple[FastAPI, TestClient], None, None]:
    from fastapi import FastAPI

    from app.core.errors import install_exception_handlers
    from app.features.platform.router_platform_policies import router as policies_router
    from app.services.provider_registry import load_builtin_providers

    app = FastAPI()
    install_exception_handlers(app)
    load_builtin_providers()
    app.include_router(policies_router)
    with TestClient(app) as client:
        yield app, client
    app.dependency_overrides.clear()
