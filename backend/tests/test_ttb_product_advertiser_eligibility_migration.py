from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


def test_product_eligibility_migration_creates_advertiser_partitioned_table(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0101_ttb_product_advertiser_eligibility"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'eligibility.db'}")

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        connection.execute(
            sa.text(
                """
                insert into ttb_product_advertiser_eligibility (
                    workspace_id, auth_id, advertiser_id, store_id, product_id,
                    is_eligible, gmv_max_ads_status
                ) values
                    (1, 2, 'adv-a', 'store-1', 'product-1', 1, 'UNOCCUPIED'),
                    (1, 2, 'adv-b', 'store-1', 'product-1', 0, 'UNOCCUPIED')
                """
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    """
                    insert into ttb_product_advertiser_eligibility (
                        workspace_id, auth_id, advertiser_id, store_id,
                        product_id, is_eligible
                    ) values (1, 2, 'adv-a', 'store-1', 'product-1', 1)
                    """
                )
            )

    inspector = sa.inspect(engine)
    assert "ttb_product_advertiser_eligibility" in inspector.get_table_names()
    unique_columns = {
        tuple(item.get("column_names") or [])
        for item in inspector.get_unique_constraints(
            "ttb_product_advertiser_eligibility"
        )
    }
    assert (
        "workspace_id",
        "auth_id",
        "advertiser_id",
        "store_id",
        "product_id",
    ) in unique_columns
    index_names = {
        str(item.get("name") or "")
        for item in inspector.get_indexes(
            "ttb_product_advertiser_eligibility"
        )
    }
    assert {
        "idx_ttb_product_advertiser_eligible",
        "idx_ttb_product_eligibility_product",
    }.issubset(index_names)


def test_product_eligibility_model_is_registered():
    from app.data.db import Base
    from app.data.models import TTBProductAdvertiserEligibility

    assert (
        Base.metadata.tables["ttb_product_advertiser_eligibility"]
        is TTBProductAdvertiserEligibility.__table__
    )
