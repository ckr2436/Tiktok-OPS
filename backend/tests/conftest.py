from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import close_all_sessions

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


TEST_DB_PATH = pathlib.Path(
    os.environ.get("GMV_TEST_DB_PATH")
    or (pathlib.Path(tempfile.gettempdir()) / "gmv-ops-test_platform.db")
)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
TEST_CONTENT_FACTORY_STORAGE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="gmv-content-factory-tests-"))
os.environ["CONTENT_FACTORY_STORAGE_ROOT"] = str(TEST_CONTENT_FACTORY_STORAGE_ROOT)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_content_factory_test_storage() -> Generator[None, None, None]:
    yield
    shutil.rmtree(TEST_CONTENT_FACTORY_STORAGE_ROOT, ignore_errors=True)


def _assert_isolated_test_database(engine: Engine) -> None:
    """Refuse destructive test setup unless it targets the dedicated SQLite file."""

    url = engine.url
    database = str(url.database or "")
    is_expected_sqlite = url.get_backend_name() == "sqlite" and database not in {"", ":memory:"}
    if is_expected_sqlite:
        is_expected_sqlite = pathlib.Path(database).resolve() == TEST_DB_PATH.resolve()
    if not is_expected_sqlite:
        raise RuntimeError(
            "Refusing to reset a non-test database. "
            f"Expected sqlite:///{TEST_DB_PATH}, got {url.render_as_string(hide_password=True)}"
        )


@compiles(BigInteger, "sqlite")
def _compile_bigint_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ARG001
    """SQLite only autoincrements a primary key declared exactly as INTEGER."""

    return "INTEGER"


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
    from app.data.db import Base, engine
    import app.data.models  # noqa: F401 - ensure models registered

    _assert_isolated_test_database(engine)
    close_all_sessions()
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    yield

    close_all_sessions()
    engine.dispose()


@pytest.fixture()
def anyio_backend() -> str:
    """The production stack runs asyncio; do not synthesize an unavailable Trio run."""

    return "asyncio"


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
