from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_content_production_plan_migration_is_idempotent(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0109_hermes_content_production_plan_audit"
    )
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'production-plan-audit.db'}"
    )
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        table = "hermes_content_production_plan_audits"
        assert table in inspector.get_table_names()
        uniques = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table)
        }
        assert ("project_id", "plan_sha256") in uniques
        assert ("stage_id",) in uniques

        migration.downgrade()
        assert table not in sa.inspect(connection).get_table_names()
