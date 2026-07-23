from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_director_execution_identity_migration_upgrades_legacy_constraints(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0114_hermes_director_execution_identity"
    )
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'director-execution.db'}"
    )
    metadata = sa.MetaData()
    briefs = sa.Table(
        "hermes_content_director_briefs",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("brief_key", sa.String(128), nullable=False),
        sa.Column("variant_index", sa.Integer(), nullable=False),
        sa.Column("brief_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "brief_key",
            name="uq_hermes_content_director_brief_key",
        ),
        sa.UniqueConstraint(
            "project_id",
            "variant_index",
            "brief_version",
            name="uq_hermes_content_director_brief_version",
        ),
    )
    artifacts = sa.Table(
        "hermes_content_director_artifacts",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brief_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "artifact_sha256",
            name="uq_hermes_content_director_artifact_sha",
        ),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            briefs.insert().values(
                id=1,
                project_id=168,
                brief_key="series.variant-006.v7",
                variant_index=6,
                brief_version=7,
            )
        )
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        columns = {
            item["name"]
            for item in inspector.get_columns(briefs.name)
        }
        assert "execution_key" in columns
        brief_uniques = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(briefs.name)
        }
        assert (
            "project_id",
            "brief_key",
            "execution_key",
        ) in brief_uniques
        assert (
            "project_id",
            "variant_index",
            "brief_version",
            "execution_key",
        ) in brief_uniques
        artifact_uniques = {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(artifacts.name)
        }
        assert ("brief_id", "artifact_sha256") in artifact_uniques

        connection.execute(
            sa.text(
                "INSERT INTO hermes_content_director_briefs "
                "(id, project_id, brief_key, variant_index, "
                "brief_version, execution_key) VALUES "
                "(2, 168, 'series.variant-006.v7', 6, 7, "
                "'director-stage-2295')"
            )
        )
        connection.execute(
            artifacts.insert(),
            [
                {
                    "id": 1,
                    "brief_id": 1,
                    "project_id": 168,
                    "artifact_sha256": "a" * 64,
                },
                {
                    "id": 2,
                    "brief_id": 2,
                    "project_id": 168,
                    "artifact_sha256": "a" * 64,
                },
            ],
        )
