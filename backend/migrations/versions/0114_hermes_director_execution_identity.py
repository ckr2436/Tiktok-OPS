"""Separate Director source-brief identity from immutable execution identity.

Revision ID: 0114_hermes_director_execution
Revises: 0113_hermes_content_execution
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0114_hermes_director_execution"
down_revision = "0113_hermes_content_execution"
branch_labels = None
depends_on = None


BRIEF_TABLE = "hermes_content_director_briefs"
ARTIFACT_TABLE = "hermes_content_director_artifacts"


def _unique_names(table_name: str) -> set[str]:
    return {
        str(item.get("name") or "")
        for item in sa.inspect(op.get_bind()).get_unique_constraints(
            table_name
        )
        if item.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if BRIEF_TABLE not in tables or ARTIFACT_TABLE not in tables:
        raise RuntimeError(
            "Director audit tables must exist before execution identity upgrade"
        )

    brief_columns = {
        str(item["name"])
        for item in sa.inspect(bind).get_columns(BRIEF_TABLE)
    }
    if "execution_key" not in brief_columns:
        op.add_column(
            BRIEF_TABLE,
            sa.Column(
                "execution_key",
                sa.String(length=128),
                nullable=False,
                server_default=sa.text("'legacy'"),
            ),
        )

    brief_uniques = _unique_names(BRIEF_TABLE)
    with op.batch_alter_table(BRIEF_TABLE) as batch:
        for old_name in (
            "uq_hermes_content_director_brief_key",
            "uq_hermes_content_director_brief_version",
        ):
            if old_name in brief_uniques:
                batch.drop_constraint(old_name, type_="unique")
        if (
            "uq_hermes_content_director_brief_execution"
            not in brief_uniques
        ):
            batch.create_unique_constraint(
                "uq_hermes_content_director_brief_execution",
                ["project_id", "brief_key", "execution_key"],
            )
        if (
            "uq_hermes_content_director_version_execution"
            not in brief_uniques
        ):
            batch.create_unique_constraint(
                "uq_hermes_content_director_version_execution",
                [
                    "project_id",
                    "variant_index",
                    "brief_version",
                    "execution_key",
                ],
            )

    artifact_uniques = _unique_names(ARTIFACT_TABLE)
    with op.batch_alter_table(ARTIFACT_TABLE) as batch:
        if (
            "uq_hermes_content_director_artifact_sha"
            in artifact_uniques
        ):
            batch.drop_constraint(
                "uq_hermes_content_director_artifact_sha",
                type_="unique",
            )
        if (
            "uq_hermes_content_director_brief_artifact_sha"
            not in artifact_uniques
        ):
            batch.create_unique_constraint(
                "uq_hermes_content_director_brief_artifact_sha",
                ["brief_id", "artifact_sha256"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if BRIEF_TABLE not in tables or ARTIFACT_TABLE not in tables:
        return

    artifact_uniques = _unique_names(ARTIFACT_TABLE)
    with op.batch_alter_table(ARTIFACT_TABLE) as batch:
        if (
            "uq_hermes_content_director_brief_artifact_sha"
            in artifact_uniques
        ):
            batch.drop_constraint(
                "uq_hermes_content_director_brief_artifact_sha",
                type_="unique",
            )
        if (
            "uq_hermes_content_director_artifact_sha"
            not in artifact_uniques
        ):
            batch.create_unique_constraint(
                "uq_hermes_content_director_artifact_sha",
                ["project_id", "artifact_sha256"],
            )

    brief_uniques = _unique_names(BRIEF_TABLE)
    with op.batch_alter_table(BRIEF_TABLE) as batch:
        for new_name in (
            "uq_hermes_content_director_brief_execution",
            "uq_hermes_content_director_version_execution",
        ):
            if new_name in brief_uniques:
                batch.drop_constraint(new_name, type_="unique")
        if "uq_hermes_content_director_brief_key" not in brief_uniques:
            batch.create_unique_constraint(
                "uq_hermes_content_director_brief_key",
                ["project_id", "brief_key"],
            )
        if (
            "uq_hermes_content_director_brief_version"
            not in brief_uniques
        ):
            batch.create_unique_constraint(
                "uq_hermes_content_director_brief_version",
                ["project_id", "variant_index", "brief_version"],
            )
        columns = {
            str(item["name"])
            for item in sa.inspect(bind).get_columns(BRIEF_TABLE)
        }
        if "execution_key" in columns:
            batch.drop_column("execution_key")
