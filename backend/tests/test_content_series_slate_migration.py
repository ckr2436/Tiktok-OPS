from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_content_series_slate_migration_is_idempotent(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0107_hermes_content_series_slate"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'series-slate.db'}")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "hermes_content_series_slates" in inspector.get_table_names()
        uniques = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(
                "hermes_content_series_slates"
            )
        }
        assert (
            "project_id",
            "series_id",
            "series_version",
        ) in uniques
        assert ("project_id", "slate_sha256") in uniques

        migration.downgrade()
        assert (
            "hermes_content_series_slates"
            not in sa.inspect(connection).get_table_names()
        )
