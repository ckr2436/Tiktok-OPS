from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


TABLE_NAME = "gmvmax_campaign_create_intents"


def _intent_values(**overrides):
    values = {
        "workspace_id": 3,
        "auth_id": 7,
        "advertiser_id": "adv-1",
        "store_id": "store-1",
        "idempotency_key": "intent-key-0001",
        "client_payload_sha256": "b" * 64,
        "payload_sha256": "a" * 64,
        "official_request_id": "official-request-1",
        "campaign_name": "campaign-1",
        "request_json": {"campaign_name": "campaign-1"},
    }
    values.update(overrides)
    return values


def test_0103_create_intent_migration_fields_indexes_and_unique_scope(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0103_gmvmax_campaign_create_intents"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'create-intents.db'}")

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        migration.upgrade()

    inspector = sa.inspect(engine)
    columns = {
        str(column["name"]): column
        for column in inspector.get_columns(TABLE_NAME)
    }
    assert {
        "id",
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "idempotency_key",
        "client_payload_sha256",
        "payload_sha256",
        "official_request_id",
        "campaign_name",
        "replacement_campaign_id",
        "campaign_id",
        "state",
        "request_json",
        "result_json",
        "error_json",
        "submitted_at",
        "remote_created_at",
        "finalized_at",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert columns["campaign_id"]["nullable"] is True
    assert columns["replacement_campaign_id"]["nullable"] is True
    assert columns["request_json"]["nullable"] is False

    unique_columns = {
        tuple(item.get("column_names") or [])
        for item in inspector.get_unique_constraints(TABLE_NAME)
    }
    assert (
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "idempotency_key",
    ) in unique_columns

    indexes = {
        str(item.get("name") or ""): tuple(item.get("column_names") or [])
        for item in inspector.get_indexes(TABLE_NAME)
    }
    assert indexes["idx_gmvmax_create_intent_scope"] == (
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
    )
    assert indexes["idx_gmvmax_create_intent_state"] == (
        "workspace_id",
        "auth_id",
        "state",
        "updated_at",
    )

    metadata = sa.MetaData()
    intents = sa.Table(TABLE_NAME, metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            intents.insert(),
            [
                _intent_values(),
                _intent_values(
                    advertiser_id="adv-2",
                    store_id="store-2",
                    idempotency_key="intent-key-0001",
                ),
                _intent_values(
                    advertiser_id="adv-3",
                    store_id="store-3",
                    idempotency_key="intent-key-0001",
                ),
            ],
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                intents.insert(),
                _intent_values(
                    advertiser_id="adv-1",
                    store_id="store-1",
                ),
            )


def test_create_intent_model_is_registered_with_matching_contract():
    from app.data.db import Base
    from app.data.models import GmvmaxCampaignCreateIntent

    table = Base.metadata.tables[TABLE_NAME]
    assert table is GmvmaxCampaignCreateIntent.__table__
    assert table.c.campaign_id.nullable is True
    assert table.c.replacement_campaign_id.nullable is True
    assert table.c.request_json.nullable is False
    assert {
        "submitted_at",
        "remote_created_at",
        "finalized_at",
    }.issubset(table.c.keys())
