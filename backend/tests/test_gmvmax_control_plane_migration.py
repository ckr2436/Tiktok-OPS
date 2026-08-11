from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_control_plane_migration_repairs_legacy_manual_upload_table(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0096_gmvmax_control_plane"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    metadata = sa.MetaData()
    legacy = sa.Table(
        "gmvmax_manual_creative_uploads",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("auth_id", sa.Integer(), nullable=False),
        sa.Column("advertiser_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("upload_id", sa.String(64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            legacy.insert(),
            {
                "workspace_id": 7,
                "auth_id": 11,
                "advertiser_id": "adv-1",
                "store_id": "store-1",
                "upload_id": "upload-1",
                "file_path": "/private/upload-1.mp4",
            },
        )
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()

    inspector = sa.inspect(engine)
    columns = {
        str(column["name"])
        for column in inspector.get_columns("gmvmax_manual_creative_uploads")
    }
    assert {
        "tiktok_account_id",
        "tiktok_business_id",
        "publish_id",
        "tiktok_item_id",
        "identity_info_json",
        "anchor_status",
        "public_url",
        "upload_error",
        "raw_json",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert any(
        index.get("name") == "idx_gmvmax_manual_upload_product"
        for index in inspector.get_indexes("gmvmax_manual_creative_uploads")
    )
    expected_unique = [
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "upload_id",
    ]
    assert any(
        list(unique.get("column_names") or []) == expected_unique
        for unique in inspector.get_unique_constraints(
            "gmvmax_manual_creative_uploads"
        )
    )


def test_control_plane_migration_creates_fresh_manual_upload_table(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0096_gmvmax_control_plane"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration-fresh.db'}")
    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()

    inspector = sa.inspect(engine)
    assert "gmvmax_manual_creative_uploads" in inspector.get_table_names()
    assert any(
        index.get("name") == "idx_gmvmax_manual_upload_product"
        for index in inspector.get_indexes("gmvmax_manual_creative_uploads")
    )
