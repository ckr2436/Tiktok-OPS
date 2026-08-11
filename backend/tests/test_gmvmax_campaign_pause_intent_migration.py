from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


TABLE_NAME = "gmvmax_campaign_pause_intents"


def test_0104_pause_intent_migration_is_idempotent_and_coalesces_active_scope(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0104_gmvmax_campaign_pause_intents"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'pause-intents.db'}")
    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        migration.upgrade()

    inspector = sa.inspect(engine)
    columns = {str(item["name"]) for item in inspector.get_columns(TABLE_NAME)}
    assert {
        "id",
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "campaign_id",
        "active_key",
        "status",
        "attempt_count",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "completed_at",
    }.issubset(columns)
    indexes = {
        str(item.get("name")): tuple(item.get("column_names") or [])
        for item in inspector.get_indexes(TABLE_NAME)
    }
    assert indexes["idx_gmvmax_pause_intent_due"] == (
        "status",
        "next_attempt_at",
        "lease_expires_at",
    )

    metadata = sa.MetaData()
    table = sa.Table(TABLE_NAME, metadata, autoload_with=engine)
    values = {
        "id": "intent-1",
        "workspace_id": 1,
        "auth_id": 2,
        "advertiser_id": "adv-1",
        "store_id": "store-1",
        "campaign_id": "campaign-1",
        "active_key": "a" * 64,
        "status": "PENDING",
    }
    with engine.begin() as connection:
        connection.execute(table.insert(), values)
    try:
        with engine.begin() as connection:
            connection.execute(table.insert(), {**values, "id": "intent-2"})
    except sa.exc.IntegrityError:
        pass
    else:  # pragma: no cover - contract assertion
        raise AssertionError("active pause intents must be coalesced")
