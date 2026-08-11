from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def test_0102_batch_manifest_migration_is_idempotent_and_scoped(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0102_gmv_creative_10min_batch_manifest"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'manifest.db'}")

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        migration.upgrade()
        connection.execute(
            sa.text(
                """
                insert into gmv_creative_10min_batch_manifests (
                    workspace_id, auth_id, advertiser_id, store_id,
                    campaign_id, stat_time_day, snapshot_at,
                    complete, row_count
                ) values
                    (1, 10, 'adv-a', 'store-1', 'campaign-1',
                     '2026-07-17', '2026-07-17 10:00:00', 1, 2),
                    (2, 20, 'adv-b', 'store-1', 'campaign-1',
                     '2026-07-17', '2026-07-17 10:00:00', 1, 0)
                """
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    """
                    insert into gmv_creative_10min_batch_manifests (
                        workspace_id, auth_id, advertiser_id, store_id,
                        campaign_id, stat_time_day, snapshot_at,
                        complete, row_count
                    ) values (
                        1, 10, 'adv-a', 'store-1', 'campaign-1',
                        '2026-07-17', '2026-07-17 10:00:00', 1, 1
                    )
                    """
                )
            )

    inspector = sa.inspect(engine)
    unique_columns = {
        tuple(item.get("column_names") or [])
        for item in inspector.get_unique_constraints(
            "gmv_creative_10min_batch_manifests"
        )
    }
    assert (
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "campaign_id",
        "stat_time_day",
        "snapshot_at",
    ) in unique_columns
    indexes = {
        str(item.get("name") or ""): tuple(item.get("column_names") or [])
        for item in inspector.get_indexes(
            "gmv_creative_10min_batch_manifests"
        )
    }
    assert indexes["idx_gmv_creative_10min_batch_latest"] == (
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "campaign_id",
        "stat_time_day",
        "complete",
        "snapshot_at",
    )


def test_batch_manifest_model_is_registered():
    from app.data.db import Base
    from app.data.models import GmvCreative10MinBatchManifest

    assert (
        Base.metadata.tables["gmv_creative_10min_batch_manifests"]
        is GmvCreative10MinBatchManifest.__table__
    )
