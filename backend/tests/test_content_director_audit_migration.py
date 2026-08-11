from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_content_director_audit_migration_is_idempotent_and_scoped(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0106_hermes_content_director_audit"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'director-audit.db'}")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        expected = {
            "hermes_content_director_briefs",
            "hermes_content_director_artifacts",
            "hermes_content_director_attempts",
            "hermes_content_director_reviews",
        }
        assert expected <= set(inspector.get_table_names())
        artifact_uniques = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(
                "hermes_content_director_artifacts"
            )
        }
        assert ("brief_id", "artifact_key", "revision") in artifact_uniques
        assert ("brief_id", "artifact_sha256") in artifact_uniques

        migration.downgrade()
        assert not expected & set(sa.inspect(connection).get_table_names())
