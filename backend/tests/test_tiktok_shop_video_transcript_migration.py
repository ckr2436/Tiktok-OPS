from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0111_tiktok_shop_video_transcripts.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("ttshop_video_transcript_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_video_transcript_migration_adds_all_pipeline_state(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE tiktok_shop_video_content_analyses (id INTEGER PRIMARY KEY)"
        ))
        module = _module()
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        columns = {
            item["name"] for item in sa.inspect(connection).get_columns(
                "tiktok_shop_video_content_analyses"
            )
        }
        assert {
            "transcript_status",
            "transcript_source",
            "transcript_language",
            "transcript_text",
            "transcript_segments_json",
            "transcript_reason",
            "transcript_error_message",
            "transcript_attempts",
            "transcript_started_at",
            "transcript_completed_at",
        }.issubset(columns)
