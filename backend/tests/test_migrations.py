from __future__ import annotations

import pathlib

import alembic.command
import alembic.config
import pytest
import sqlalchemy as sa
from sqlalchemy import text


BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
BASE_REVISION = "0007_platform_policy_v1"


def _make_config(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> alembic.config.Config:
    cfg = alembic.config.Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    monkeypatch.setenv("ALEMBIC_DB_URL", f"sqlite:///{db_path}")
    return cfg


def _prepare_schema(db_path: pathlib.Path, include_stats: bool = False) -> None:
    if db_path.exists():
        db_path.unlink()

    engine = sa.create_engine(f"sqlite:///{db_path}")
    schedule_columns = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "schedule_id BIGINT NOT NULL",
        "workspace_id BIGINT NOT NULL",
        "scheduled_for TIMESTAMP NOT NULL",
        "enqueued_at TIMESTAMP",
        "broker_msg_id VARCHAR(64)",
        "status VARCHAR(32) NOT NULL",
        "duration_ms INTEGER",
        "error_code VARCHAR(64)",
        "error_message VARCHAR(512)",
    ]
    if include_stats:
        schedule_columns.append("stats_json TEXT")
    schedule_columns.extend(
        [
            "idempotency_key VARCHAR(64) NOT NULL",
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        ]
    )

    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE schedule_runs (" + ",".join(schedule_columns) + ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE task_catalog ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "task_name VARCHAR(128) NOT NULL UNIQUE,"
                "impl_version INTEGER NOT NULL,"
                "input_schema_json TEXT,"
                "default_queue VARCHAR(128) NOT NULL,"
                "visibility VARCHAR(32) NOT NULL,"
                "is_enabled BOOLEAN NOT NULL DEFAULT 1"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
            {"rev": BASE_REVISION},
        )
    engine.dispose()


def _get_columns(db_path: pathlib.Path) -> list[dict[str, object]]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        inspector = sa.inspect(engine)
        return inspector.get_columns("schedule_runs")
    finally:
        engine.dispose()


def _fetch_task_names(db_path: pathlib.Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT task_name FROM task_catalog")).fetchall()
        return {row[0] for row in rows}
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", ["head"])
def test_migration_0008_creates_stats_column(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    db_path = tmp_path / "upgrade.db"
    _prepare_schema(db_path)
    cfg = _make_config(db_path, monkeypatch)

    alembic.command.upgrade(cfg, target)

    columns = _get_columns(db_path)
    column_names = {col["name"] for col in columns}
    assert "stats_json" in column_names

    seeded = _fetch_task_names(db_path)
    assert {"ttb.sync.bc", "ttb.sync.advertisers", "ttb.sync.stores", "ttb.sync.products", "ttb.sync.all"}.issubset(seeded)


def test_migration_0008_skips_existing_column(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "existing.db"
    _prepare_schema(db_path, include_stats=True)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO schedule_runs (schedule_id, workspace_id, scheduled_for, status, idempotency_key, created_at, updated_at, stats_json) "
                    "VALUES (:schedule_id, :workspace_id, :scheduled_for, :status, :idempotency_key, :created_at, :updated_at, :stats_json)"
                ),
                {
                    "schedule_id": 1,
                    "workspace_id": 1,
                    "scheduled_for": "2024-01-01 00:00:00",
                    "status": "scheduled",
                    "idempotency_key": "idem-1",
                    "created_at": "2024-01-01 00:00:00",
                    "updated_at": "2024-01-01 00:00:00",
                    "stats_json": '{"existing": true}',
                },
            )
    finally:
        engine.dispose()

    cfg = _make_config(db_path, monkeypatch)
    alembic.command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            payload = conn.execute(
                text("SELECT stats_json FROM schedule_runs WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert payload == '{"existing": true}'


def test_migration_0008_downgrade_and_reupgrade(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "downgrade.db"
    _prepare_schema(db_path)
    cfg = _make_config(db_path, monkeypatch)

    alembic.command.upgrade(cfg, "head")

    columns = _get_columns(db_path)
    assert any(col["name"] == "stats_json" for col in columns)

    alembic.command.downgrade(cfg, BASE_REVISION)

    columns = _get_columns(db_path)
    assert not any(col["name"] == "stats_json" for col in columns)

    alembic.command.upgrade(cfg, "head")
    columns = _get_columns(db_path)
    assert any(col["name"] == "stats_json" for col in columns)
