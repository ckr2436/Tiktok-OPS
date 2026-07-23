from __future__ import annotations

import importlib
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
import pytest

from app.data.models.hermes_agent import (
    HermesContentDeliverable,
    HermesContentExecution,
    HermesContentFactoryAsset,
    HermesContentSegmentRun,
    HermesContentVariantRun,
)
from app.data.models.kie_api import KieFile, KieTask
from app.tasks.hermes_agent import content_factory_tasks as content_tasks
from app.tasks.hermes_agent.content_factory_tasks import (
    _dual_write_media_execution_ledger,
    _ensure_content_execution_ledger,
    _sync_content_segment_execution_ledger,
    _upsert_content_deliverable_ledger,
)


def test_content_execution_ledger_migration_is_idempotent(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0113_hermes_content_execution_ledger"
    )
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'content-execution-ledger.db'}"
    )
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

        tables = set(sa.inspect(connection).get_table_names())
        assert {
            "hermes_content_executions",
            "hermes_content_variant_runs",
            "hermes_content_segment_runs",
            "hermes_content_deliverables",
        }.issubset(tables)

        migration.downgrade()
        tables = set(sa.inspect(connection).get_table_names())
        assert "hermes_content_executions" not in tables


def test_media_submission_dual_write_is_idempotent(db_session):
    project = SimpleNamespace(
        id=168,
        project_key="project-168",
        workspace_id=12,
        user_id=34,
        config_json={"video_count": 2, "video_model": "omni_flash"},
    )
    tasks = []
    for index in (1, 2):
        task = KieTask(
            workspace_id=12,
            key_id=1,
            created_by_user_id=34,
            model="omni_flash",
            task_id=f"local-ledger-{index}",
            state="queued_local" if index == 1 else "waiting_dependency",
            input_json={
                "content_factory_project_id": 168,
                "content_factory_variant_index": 1,
                "content_factory_segment_index": index,
            },
        )
        db_session.add(task)
        tasks.append(task)
    db_session.flush()
    group = {
        "video_index": 1,
        "aspect_ratio": "9:16",
        "duration": 20,
        "segments": [
            {
                "task_id": int(task.id),
                "segment_index": index,
                "dependency_task_id": (
                    int(tasks[index - 2].id) if index > 1 else None
                ),
            }
            for index, task in enumerate(tasks, 1)
        ],
    }

    first = _dual_write_media_execution_ledger(
        db_session,
        project=project,
        groups=[group],
        tasks=tasks,
        media_manifest_sha256="a" * 64,
        provider_key="bandianwa",
        provider_model="omni_flash",
    )
    second = _dual_write_media_execution_ledger(
        db_session,
        project=project,
        groups=[group],
        tasks=tasks,
        media_manifest_sha256="a" * 64,
        provider_key="bandianwa",
        provider_model="omni_flash",
    )

    assert first[0].id == second[0].id
    assert db_session.query(HermesContentExecution).count() == 1
    assert db_session.query(HermesContentVariantRun).count() == 1
    assert db_session.query(HermesContentSegmentRun).count() == 2
    assert {row.state for row in db_session.query(HermesContentSegmentRun)} == {
        "queued_local",
        "waiting_dependency",
    }


def _ledger_project(*, target_count: int = 1):
    return SimpleNamespace(
        id=168,
        project_key="project-168",
        workspace_id=12,
        user_id=34,
        config_json={"video_count": target_count, "video_model": "omni_flash"},
    )


def _submitted_segment(db_session, project):
    task = KieTask(
        workspace_id=project.workspace_id,
        key_id=1,
        created_by_user_id=project.user_id,
        model="omni_flash",
        task_id="local-ledger-downloaded",
        state="success",
        input_json={
            "content_factory_project_id": project.id,
            "content_factory_variant_index": 1,
            "content_factory_segment_index": 1,
        },
    )
    db_session.add(task)
    db_session.flush()
    group = {
        "video_index": 1,
        "aspect_ratio": "9:16",
        "duration": 10,
        "media_manifest_sha256": "b" * 64,
        "segments": [{"task_id": int(task.id), "segment_index": 1}],
    }
    _dual_write_media_execution_ledger(
        db_session,
        project=project,
        groups=[group],
        tasks=[task],
        media_manifest_sha256="b" * 64,
        provider_key="bandianwa",
        provider_model="omni_flash",
    )
    return task, group


def test_segment_sync_hashes_download_once_when_file_is_unchanged(
    db_session, tmp_path, monkeypatch
):
    project = _ledger_project()
    task, _group = _submitted_segment(db_session, project)
    local_video = tmp_path / "segment.mp4"
    local_video.write_bytes(b"stable-segment-content")
    db_session.add(
        KieFile(
            workspace_id=project.workspace_id,
            key_id=1,
            task_id=int(task.id),
            file_url="https://provider.invalid/segment.mp4",
            kind="result",
            meta_json={"local_path": str(local_video)},
        )
    )
    db_session.flush()
    real_hash = content_tasks._file_sha256
    calls = []

    def counted_hash(path, **kwargs):
        calls.append(str(path))
        return real_hash(path, **kwargs)

    monkeypatch.setattr(content_tasks, "_file_sha256", counted_hash)
    _sync_content_segment_execution_ledger(
        db_session, project=project, tasks=[task]
    )
    _sync_content_segment_execution_ledger(
        db_session, project=project, tasks=[task]
    )

    segment = db_session.query(HermesContentSegmentRun).one()
    assert segment.state == "downloaded"
    assert segment.output_sha256
    assert segment.meta_json["output_size_bytes"] == local_video.stat().st_size
    assert calls == [str(local_video)]


def test_deliverable_upsert_reuses_verified_hash_and_completes_execution(
    db_session, tmp_path, monkeypatch
):
    project = _ledger_project()
    _task, group = _submitted_segment(db_session, project)
    video_path = tmp_path / "video-001.mp4"
    guide_path = tmp_path / "video-001-guide.json"
    video_path.write_bytes(b"final-video-content")
    guide_path.write_text('{"title":"One"}', encoding="utf-8")
    video_asset = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name=video_path.name,
        file_path=str(video_path),
        mime_type="video/mp4",
        size_bytes=video_path.stat().st_size,
    )
    guide_asset = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="EDIT_PACKAGE",
        kind="edit_guide",
        original_name=guide_path.name,
        file_path=str(guide_path),
        mime_type="application/json",
        size_bytes=guide_path.stat().st_size,
    )
    db_session.add_all([video_asset, guide_asset])
    db_session.flush()
    real_hash = content_tasks._file_sha256
    calls = []

    def counted_hash(path, **kwargs):
        calls.append(str(path))
        return real_hash(path, **kwargs)

    monkeypatch.setattr(content_tasks, "_file_sha256", counted_hash)
    _upsert_content_deliverable_ledger(
        db_session,
        project=project,
        group=group,
        video_asset=video_asset,
        guidance_asset=guide_asset,
    )
    _upsert_content_deliverable_ledger(
        db_session,
        project=project,
        group=group,
        video_asset=video_asset,
        guidance_asset=guide_asset,
    )

    assert db_session.query(HermesContentDeliverable).count() == 2
    assert calls == [str(video_path), str(guide_path)]
    execution = db_session.query(HermesContentExecution).one()
    variant = db_session.query(HermesContentVariantRun).one()
    assert execution.status == "complete"
    assert variant.state == "composed"
    assert variant.output_sha256


def test_execution_creation_backfills_verified_preledger_local_pair(
    db_session, tmp_path, monkeypatch
):
    project = _ledger_project(target_count=2)
    video_path = tmp_path / "historical-001.mp4"
    guide_path = tmp_path / "historical-001-guide.json"
    video_path.write_bytes(b"historical-video" * 128)
    guide_path.write_text('{"title":"Historical"}', encoding="utf-8")
    video_asset = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name=video_path.name,
        file_path=str(video_path),
        mime_type="video/mp4",
        size_bytes=video_path.stat().st_size,
        meta_json={"content_factory_video_index": 1},
    )
    guide_asset = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="EDIT_PACKAGE",
        kind="edit_guidance",
        original_name=guide_path.name,
        file_path=str(guide_path),
        mime_type="application/json",
        size_bytes=guide_path.stat().st_size,
        meta_json={"content_factory_video_index": 1},
    )
    db_session.add_all([video_asset, guide_asset])
    db_session.flush()
    real_hash = content_tasks._file_sha256
    calls = []

    def counted_hash(path, **kwargs):
        calls.append(str(path))
        return real_hash(path, **kwargs)

    monkeypatch.setattr(content_tasks, "_file_sha256", counted_hash)
    execution = _ensure_content_execution_ledger(db_session, project)
    same_execution = _ensure_content_execution_ledger(db_session, project)

    assert execution.id == same_execution.id
    assert execution.status == "running"
    assert db_session.query(HermesContentVariantRun).count() == 1
    assert db_session.query(HermesContentDeliverable).count() == 2
    assert calls == [str(video_path), str(guide_path)]
    assert execution.meta_json["legacy_local_backfilled_ordinals"] == [1]


def test_execution_contract_ignores_runtime_controls_but_rejects_media_drift(
    db_session,
):
    project = _ledger_project()
    _submitted_segment(db_session, project)
    execution = db_session.query(HermesContentExecution).one()
    original_digest = execution.config_sha256

    project.config_json = {
        **project.config_json,
        "auto_run": False,
        "manual_paused_at": "2026-07-22T05:31:00",
        "max_api_video_variants_in_flight": 4,
        "variant_rollout_gate": {"enabled": True},
    }
    assert _ensure_content_execution_ledger(db_session, project).id == execution.id
    assert execution.config_sha256 == original_digest

    project.config_json = {
        **project.config_json,
        "video_resolution": "480p",
    }
    with pytest.raises(ValueError, match="CONTENT_EXECUTION_CONTRACT_DRIFT"):
        _ensure_content_execution_ledger(db_session, project)


def test_new_execution_key_supersedes_prior_running_contract(db_session):
    project = _ledger_project(target_count=2)
    _submitted_segment(db_session, project)
    prior = db_session.query(HermesContentExecution).one()

    project.config_json = {
        **project.config_json,
        "content_execution_key": "project-168:production-v2",
        "allowed_audio_modes": ["spoken"],
    }
    current = _ensure_content_execution_ledger(db_session, project)

    assert current.id != prior.id
    assert current.execution_key == "project-168:production-v2"
    assert current.status == "running"
    assert prior.status == "superseded"
    assert prior.meta_json["superseded_by_execution_id"] == current.id
    assert (
        prior.meta_json["superseded_by_execution_key"]
        == "project-168:production-v2"
    )
