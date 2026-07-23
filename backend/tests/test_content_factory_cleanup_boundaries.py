from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import UploadFile

from app.core.errors import APIError
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.data.models.kie_api import KieTask
from app.services.hermes_agent import content_factory
from app.features.tenants.hermes_agent import router as content_factory_router
from app.tasks.hermes_agent import content_factory_tasks


def _project(**overrides) -> HermesContentFactoryProject:
    values = {
        "project_key": "cf_cleanup_boundary_01",
        "workspace_id": 3,
        "user_id": 6,
        "title": "Cleanup boundary",
        "product_name": "",
        "market": "US",
        "status": "waiting_bridge",
        "current_stage": "DIRECTOR",
        "config_json": {"auto_run": True},
        "state_json": {},
    }
    values.update(overrides)
    return HermesContentFactoryProject(**values)


def test_project_delete_is_tombstoned_before_storage_is_removed(
    db_session,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "content"
    browser_inbox = storage_root / "browser_inbox"
    monkeypatch.setattr(content_factory, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(content_factory, "BROWSER_INBOX", browser_inbox)

    project = _project()
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="DIRECTOR",
        attempt=1,
        status="queued",
    )
    db_session.add(stage)
    db_session.flush()

    targets = (
        storage_root / "workspace_3" / project.project_key,
        browser_inbox / "workspace_3" / project.project_key,
        storage_root / "browser_outbox" / "workspace_3" / project.project_key,
    )
    for target in targets:
        target.mkdir(parents=True)
        (target / "payload.bin").write_bytes(b"content")

    content_factory.delete_project(db_session, project)

    assert project.status == "deleted"
    assert stage.status == "failed"
    assert all(target.is_dir() for target in targets)

    db_session.commit()
    assert content_factory.finalize_deleted_project(db_session, project) is True
    db_session.commit()

    assert db_session.get(HermesContentFactoryProject, project.id) is None
    assert all(not target.exists() for target in targets)


def test_deleted_project_is_hidden_from_member_query(db_session):
    visible = _project(project_key="cf_visible_boundary_01", status="draft")
    deleted = _project(project_key="cf_deleted_boundary_01", status="deleted")
    db_session.add_all([visible, deleted])
    db_session.commit()

    rows = content_factory.visible_project_query(db_session, 3, 6).all()

    assert [row.project_key for row in rows] == [visible.project_key]


def test_deleted_project_waits_for_submitted_provider_work(
    db_session,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "content"
    monkeypatch.setattr(content_factory, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(
        content_factory,
        "BROWSER_INBOX",
        storage_root / "browser_inbox",
    )
    project = _project(
        project_key="cf_provider_drain_01",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
    )
    db_session.add(project)
    db_session.flush()
    provider_task = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="submitted-provider-task",
        state="processing",
        input_json={"content_factory_project_id": project.id},
    )
    db_session.add(provider_task)
    db_session.flush()
    project.state_json = {"ai_video_task_ids": [provider_task.id]}
    db_session.commit()

    content_factory.delete_project(db_session, project)
    db_session.commit()
    assert content_factory.finalize_deleted_project(db_session, project) is False
    assert db_session.get(HermesContentFactoryProject, project.id).status == "deleted"

    provider_task.state = "success"
    db_session.add(provider_task)
    db_session.commit()
    assert content_factory.finalize_deleted_project(db_session, project) is True
    db_session.commit()
    assert db_session.get(HermesContentFactoryProject, project.id) is None


def test_completed_waiting_bridge_status_is_repaired_without_deletion(db_session):
    project = _project(status="waiting_bridge", current_stage="COMPLETE")
    db_session.add(project)
    db_session.commit()

    repaired = content_factory_tasks._normalize_completed_project_statuses(
        db_session,
    )
    db_session.commit()

    assert repaired == 1
    assert db_session.get(HermesContentFactoryProject, project.id).status == "complete"


def test_provider_task_ledger_is_scoped_by_workspace_and_user(db_session):
    project = _project(status="generating_video", current_stage="WAITING_VIDEO_INPUT")
    db_session.add(project)
    db_session.flush()
    owned = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="owned-task",
        state="queued_local",
        input_json={"content_factory_project_id": project.id},
    )
    foreign = KieTask(
        workspace_id=4,
        created_by_user_id=9,
        key_id=1,
        model="omni_flash",
        task_id="foreign-task",
        state="failed",
        input_json={"content_factory_project_id": 999},
    )
    db_session.add_all([owned, foreign])
    db_session.commit()

    rows = content_factory_tasks._scoped_content_video_tasks(
        db_session,
        project,
        [owned.id, foreign.id],
    )

    assert [row.id for row in rows] == [owned.id]


def test_superseded_provider_work_is_terminal_for_waiters_and_deletion(
    db_session,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "content"
    monkeypatch.setattr(content_factory, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(
        content_factory,
        "BROWSER_INBOX",
        storage_root / "browser_inbox",
    )
    project = _project(
        project_key="cf_superseded_drain_01",
        status="paused",
        current_stage="PRODUCTION_PLAN",
    )
    db_session.add(project)
    db_session.flush()
    task = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="superseded-provider-task",
        state="superseded",
        input_json={"content_factory_project_id": project.id},
    )
    db_session.add(task)
    db_session.flush()
    project.state_json = {"ai_video_task_ids": [task.id]}
    db_session.commit()

    assert content_factory_tasks._active_video_task_ids_for_waiter(
        db_session,
        project,
        [task.id],
    ) == []

    content_factory.delete_project(db_session, project)
    db_session.commit()
    assert content_factory.finalize_deleted_project(db_session, project) is True
    db_session.commit()
    assert db_session.get(HermesContentFactoryProject, project.id) is None


def test_upload_limit_counts_streamed_bytes_when_declared_size_is_missing(
    tmp_path,
):
    target = tmp_path / "bounded-upload.bin"
    upload = UploadFile(filename="payload.bin", file=io.BytesIO(b"123456"))
    upload.size = None

    with pytest.raises(APIError) as exc_info:
        asyncio.run(
            content_factory._write_upload_bounded(
                upload,
                target,
                max_bytes=5,
            )
        )

    assert exc_info.value.code == "CONTENT_ASSET_TOO_LARGE"
    assert not target.exists()


def test_asset_download_path_cannot_escape_content_repository(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "content"
    repository.mkdir()
    external = tmp_path / "secret.txt"
    external.write_text("not content", encoding="utf-8")
    monkeypatch.setattr(content_factory_router, "STORAGE_ROOT", repository)

    with pytest.raises(APIError) as exc_info:
        content_factory_router._content_asset_path(external)

    assert exc_info.value.code == "CONTENT_ASSET_STORAGE_SCOPE_INVALID"
