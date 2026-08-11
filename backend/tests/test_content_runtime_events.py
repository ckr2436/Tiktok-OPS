from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from app.data.db import Base
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentRuntimeEvent,
    HermesContentSegmentRun,
)
from app.data.models.kie_api import KieFile, KieTask
from app.services.hermes_agent import content_runtime
from app.tasks.hermes_agent import content_factory_tasks as content_tasks
from app.tasks.hermes_agent.content_factory_tasks import (
    _dual_write_media_execution_ledger,
)


def _project(db_session, *, project_key: str) -> HermesContentFactoryProject:
    row = HermesContentFactoryProject(
        project_key=project_key,
        workspace_id=912,
        user_id=934,
        title="Runtime event test",
        product_name="Test product",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True, "video_count": 1},
        state_json={"ai_video_task_ids": []},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _segment_task(db_session, project: HermesContentFactoryProject) -> KieTask:
    task = KieTask(
        workspace_id=int(project.workspace_id),
        key_id=1,
        created_by_user_id=project.user_id,
        model="omni_flash",
        task_id=f"runtime-{project.project_key}",
        state="queued_local",
        input_json={
            "content_factory_project_id": int(project.id),
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
        "segments": [{"task_id": int(task.id), "segment_index": 1}],
    }
    _dual_write_media_execution_ledger(
        db_session,
        project=project,
        groups=[group],
        tasks=[task],
        media_manifest_sha256="c" * 64,
        provider_key="sub2api",
        provider_model="omni_flash",
    )
    return task


def test_runtime_migration_is_idempotent(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0129_content_runtime_events_and_evaluations"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        migration.HermesContentEvaluation.__table__.drop(
            bind=connection,
            checkfirst=True,
        )
        migration.HermesContentRuntimeEvent.__table__.drop(
            bind=connection,
            checkfirst=True,
        )
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()
        tables = set(sa.inspect(connection).get_table_names())
        assert "hermes_content_evaluations" in tables
        assert "hermes_content_runtime_events" in tables


def test_provider_result_projects_atomically_without_mutating_transport(
    db_session,
    tmp_path,
    monkeypatch,
):
    project = _project(db_session, project_key="runtime-projection")
    task = _segment_task(db_session, project)
    video = tmp_path / "segment.mp4"
    video.write_bytes(b"provider-result")
    monkeypatch.setattr(
        content_runtime,
        "get_local_path",
        lambda _file: video,
    )
    from app.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda *_args, **_kwargs: None)
    db_session.add(
        KieFile(
            workspace_id=int(project.workspace_id),
            key_id=1,
            task_id=int(task.id),
            file_url="https://provider.invalid/result.mp4",
            kind="result",
            meta_json={"local_path": str(video)},
        )
    )
    task.state = "success"
    db_session.add(task)
    db_session.flush()
    db_session.commit()

    segment = db_session.query(HermesContentSegmentRun).one()
    assert task.state == "success"
    assert segment.state == "downloaded"
    assert segment.local_file_path == str(video)
    assert segment.output_sha256
    assert db_session.query(HermesContentRuntimeEvent).count() == 1

    db_session.flush()
    assert db_session.query(HermesContentRuntimeEvent).count() == 1


def test_legacy_semantic_failure_with_local_result_projects_as_downloaded(
    db_session,
    tmp_path,
    monkeypatch,
):
    project = _project(db_session, project_key="runtime-legacy-quality")
    task = _segment_task(db_session, project)
    video = tmp_path / "legacy.mp4"
    video.write_bytes(b"downloaded-before-old-quality-gate")
    monkeypatch.setattr(
        content_runtime,
        "get_local_path",
        lambda _file: video,
    )
    db_session.add(
        KieFile(
            workspace_id=int(project.workspace_id),
            key_id=1,
            task_id=int(task.id),
            file_url="https://provider.invalid/legacy.mp4",
            kind="result",
            meta_json={"local_path": str(video)},
        )
    )
    task.state = "failed"
    task.fail_code = "product_visual_qa"
    task.fail_msg = "legacy semantic mutation"
    db_session.add(task)
    db_session.flush()

    segment = db_session.query(HermesContentSegmentRun).one()
    assert task.state == "success"
    assert task.fail_code is None
    assert segment.state == "downloaded"
    assert segment.error_class is None
    assert task.result_json["__local"]["legacy_content_quality_incident"]["code"] == (
        "product_visual_qa"
    )


def test_runtime_event_processing_failure_is_retried(
    db_session,
    monkeypatch,
):
    project = _project(db_session, project_key="runtime-retry")
    event = HermesContentRuntimeEvent(
        idempotency_key="d" * 64,
        workspace_id=int(project.workspace_id),
        project_id=int(project.id),
        event_type="segment.downloaded",
        status="pending",
        payload_json={},
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()

    def fail_schedule(*_args, **_kwargs):
        raise RuntimeError("synthetic scheduling failure")

    monkeypatch.setattr(content_tasks, "_schedule_video_wait", fail_schedule)
    runtime_tasks = importlib.import_module(
        "app.tasks.hermes_agent.content_runtime_tasks"
    )
    result = runtime_tasks.process_content_runtime_events.run(limit=10)

    db_session.expire_all()
    refreshed = db_session.get(HermesContentRuntimeEvent, int(event.id))
    assert result["retried"] == [int(project.id)]
    assert refreshed.status == "retry"
    assert refreshed.attempts == 1
    assert "synthetic scheduling failure" in str(refreshed.last_error)


def test_runtime_event_never_reanimates_operator_required_pause(
    db_session,
    monkeypatch,
):
    project = _project(db_session, project_key="runtime-operator-boundary")
    project.status = "paused"
    project.state_json = {
        "pause_reason_code": "operator_required",
        "operator_required_at": "2026-08-06T00:00:00",
    }
    event = HermesContentRuntimeEvent(
        idempotency_key="e" * 64,
        workspace_id=int(project.workspace_id),
        project_id=int(project.id),
        event_type="segment.downloaded",
        status="pending",
        payload_json={},
        attempts=0,
    )
    db_session.add_all([project, event])
    db_session.commit()

    def forbidden_schedule(*_args, **_kwargs):
        raise AssertionError("operator-required pause must not schedule work")

    monkeypatch.setattr(content_tasks, "_schedule_video_wait", forbidden_schedule)
    runtime_tasks = importlib.import_module(
        "app.tasks.hermes_agent.content_runtime_tasks"
    )
    result = runtime_tasks.process_content_runtime_events.run(limit=10)

    db_session.expire_all()
    refreshed_event = db_session.get(HermesContentRuntimeEvent, int(event.id))
    refreshed_project = db_session.get(HermesContentFactoryProject, int(project.id))
    assert result["scheduled"] == []
    assert result["skipped"] == [int(project.id)]
    assert refreshed_event.status == "processed"
    assert refreshed_project.status == "paused"
    assert refreshed_project.state_json["pause_reason_code"] == "operator_required"


def test_legacy_backfill_event_never_reanimates_old_quality_pause(
    db_session,
    monkeypatch,
):
    project = _project(db_session, project_key="runtime-legacy-pause")
    project.status = "paused"
    project.state_json = {
        "pause_reason_code": "final_video_quality_gate",
        "ai_video_task_ids": [101, 102],
    }
    event = HermesContentRuntimeEvent(
        idempotency_key="f" * 64,
        workspace_id=int(project.workspace_id),
        project_id=int(project.id),
        event_type="segment.downloaded",
        status="pending",
        # Events written before content-runtime-event-v1 gained provenance
        # are one-time ledger backfill, never fresh provider completions.
        payload_json={},
        attempts=0,
    )
    db_session.add_all([project, event])
    db_session.commit()

    def forbidden_schedule(*_args, **_kwargs):
        raise AssertionError("legacy backfill must not wake old quality pause")

    monkeypatch.setattr(content_tasks, "_schedule_video_wait", forbidden_schedule)
    runtime_tasks = importlib.import_module(
        "app.tasks.hermes_agent.content_runtime_tasks"
    )
    result = runtime_tasks.process_content_runtime_events.run(limit=10)

    db_session.expire_all()
    refreshed = db_session.get(HermesContentRuntimeEvent, int(event.id))
    assert result["scheduled"] == []
    assert result["skipped"] == [int(project.id)]
    assert refreshed.status == "processed"


def test_reconciler_republishes_existing_durable_event(
    db_session,
    monkeypatch,
):
    project = _project(db_session, project_key="runtime-republish")
    event = HermesContentRuntimeEvent(
        idempotency_key="a" * 63 + "1",
        workspace_id=int(project.workspace_id),
        project_id=int(project.id),
        event_type="segment.downloaded",
        status="pending",
        payload_json={"event_origin": "provider_commit"},
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()

    runtime_tasks = importlib.import_module(
        "app.tasks.hermes_agent.content_runtime_tasks"
    )
    monkeypatch.setattr(
        runtime_tasks,
        "reconcile_content_execution_ledger",
        lambda _db, limit: {
            "tasks": 0,
            "segments": 0,
            "events": 0,
            "projects": [],
        },
    )
    submissions = []
    monkeypatch.setattr(
        runtime_tasks.process_content_runtime_events,
        "apply_async",
        lambda *args, **kwargs: submissions.append((args, kwargs)),
    )

    result = runtime_tasks.reconcile_content_runtime_ledger.run(limit=100)

    assert result["events"] == 0
    assert result["pending_events"] == 1
    assert len(submissions) == 1
