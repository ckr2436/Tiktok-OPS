from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_creative_product_migration_backfills_all_spus_and_legacy_fallback(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0097_gmvmax_creative_products"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'creative-products.db'}")
    metadata = sa.MetaData()
    cache = sa.Table(
        "gmvmax_creative_asset_cache",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("advertiser_id", sa.String(64), nullable=False),
        sa.Column("store_id", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("item_group_id", sa.String(64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            cache.insert(),
            [
                {
                    "workspace_id": 7,
                    "auth_id": 11,
                    "advertiser_id": "adv-1",
                    "store_id": "store-1",
                    "item_id": "creative-multi",
                    "item_group_id": "product-a",
                    "raw_json": {
                        "spu_id_list": [
                            "product-a",
                            "product-b",
                            "product-a",
                            "",
                        ]
                    },
                },
                {
                    "workspace_id": 7,
                    "auth_id": 11,
                    "advertiser_id": "adv-1",
                    "store_id": "store-1",
                    "item_id": "creative-legacy",
                    "item_group_id": "product-legacy",
                    "raw_json": {},
                },
                {
                    "workspace_id": 7,
                    "auth_id": 11,
                    "advertiser_id": "adv-1",
                    "store_id": "store-1",
                    "item_id": "creative-unassigned",
                    "item_group_id": None,
                    "raw_json": {"spu_id_list": []},
                },
            ],
        )
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()

        rows = connection.execute(
            sa.text(
                """
                select item_id, item_group_id
                from gmvmax_creative_asset_products
                order by item_id, item_group_id
                """
            )
        ).all()

    assert rows == [
        ("creative-legacy", "product-legacy"),
        ("creative-multi", "product-a"),
        ("creative-multi", "product-b"),
    ]

    inspector = sa.inspect(engine)
    expected_unique = [
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "item_id",
        "item_group_id",
    ]
    assert any(
        list(unique.get("column_names") or []) == expected_unique
        for unique in inspector.get_unique_constraints(
            "gmvmax_creative_asset_products"
        )
    )
    assert any(
        index.get("name") == "idx_gmvmax_creative_asset_product_lookup"
        for index in inspector.get_indexes("gmvmax_creative_asset_products")
    )

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.downgrade()

    assert (
        "gmvmax_creative_asset_products"
        not in sa.inspect(engine).get_table_names()
    )


def test_creative_product_migration_repairs_missing_lookup_index(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0097_gmvmax_creative_products"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'partial-products.db'}")

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        connection.execute(
            sa.text(
                "drop index idx_gmvmax_creative_asset_product_lookup"
            )
        )
        migration.upgrade()

    assert any(
        index.get("name") == "idx_gmvmax_creative_asset_product_lookup"
        for index in sa.inspect(engine).get_indexes(
            "gmvmax_creative_asset_products"
        )
    )


def test_creative_product_relation_is_registered_in_base_metadata():
    from app.data.db import Base
    from app.data.models import GmvmaxCreativeAssetProduct

    assert (
        Base.metadata.tables["gmvmax_creative_asset_products"]
        is GmvmaxCreativeAssetProduct
    )
