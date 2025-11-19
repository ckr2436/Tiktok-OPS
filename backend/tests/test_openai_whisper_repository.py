from __future__ import annotations

import uuid

from app.data.models.workspaces import Workspace
from app.features.tenants.openai_whisper import repository


def _create_workspace(db_session, code: str = "0001") -> int:
    ws = Workspace(name=f"Workspace {code}", company_code=code)
    ws.id = int(code)
    db_session.add(ws)
    db_session.commit()
    return int(ws.id)


def test_create_and_list_jobs(db_session):
    workspace_id = _create_workspace(db_session, "1001")
    job_id = uuid.uuid4().hex
    payload = {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "user_id": None,
        "filename": "demo.mp4",
        "size": 1234,
        "content_type": "video/mp4",
        "video_path": "/tmp/demo.mp4",
        "translate": False,
        "show_bilingual": False,
        "status": "pending",
    }
    repository.create_job(db_session, payload)
    db_session.commit()

    jobs = repository.list_jobs(db_session, workspace_id, limit=5)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_id == job_id
    assert job.filename == "demo.mp4"
    assert job.file_size == 1234
    assert job.status == "pending"


def test_status_transitions(db_session):
    workspace_id = _create_workspace(db_session, "1002")
    job_id = uuid.uuid4().hex
    repository.create_job(
        db_session,
        {
            "job_id": job_id,
            "workspace_id": workspace_id,
            "user_id": None,
            "filename": "file.mp4",
            "size": 10,
            "video_path": "/tmp/file.mp4",
            "translate": True,
            "show_bilingual": True,
            "source_language": "en",
            "target_language": "zh",
        },
    )
    db_session.commit()

    repository.mark_processing(db_session, workspace_id, job_id)
    db_session.commit()
    job = repository.get_job(db_session, workspace_id, job_id)
    assert job.status == "processing"
    assert job.started_at is not None

    repository.mark_completed(
        db_session,
        workspace_id,
        job_id,
        detected_language="en",
        translation_language="zh",
        segments_count=5,
        translation_segments_count=5,
    )
    db_session.commit()
    job = repository.get_job(db_session, workspace_id, job_id)
    assert job.status == "success"
    assert job.detected_language == "en"
    assert job.translation_language == "zh"
    assert job.segments_count == 5
    assert job.translation_segments_count == 5

    repository.mark_failed(db_session, workspace_id, job_id, "err")
    db_session.commit()
    job = repository.get_job(db_session, workspace_id, job_id)
    assert job.status == "failed"
    assert job.error == "err"


def test_list_jobs_ordering(db_session):
    workspace_id = _create_workspace(db_session, "1003")
    first_job = uuid.uuid4().hex
    second_job = uuid.uuid4().hex
    repository.create_job(
        db_session,
        {
            "job_id": first_job,
            "workspace_id": workspace_id,
            "user_id": None,
            "filename": "first.mp4",
            "size": 10,
            "video_path": "/tmp/first",
            "translate": False,
            "show_bilingual": False,
        },
    )
    repository.create_job(
        db_session,
        {
            "job_id": second_job,
            "workspace_id": workspace_id,
            "user_id": None,
            "filename": "second.mp4",
            "size": 11,
            "video_path": "/tmp/second",
            "translate": True,
            "show_bilingual": False,
        },
    )
    db_session.commit()

    jobs = repository.list_jobs(db_session, workspace_id, limit=1)
    assert len(jobs) == 1
    assert jobs[0].job_id == second_job
