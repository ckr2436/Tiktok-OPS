from pathlib import Path

from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from app.services.ai_video.retry_policy import (
    archive_successful_task_result_files,
    restore_archived_task_result_files,
)
from app.services.ai_video.task_state import reset_video_task_for_retry


def _local_path(file: KieFile) -> Path | None:
    raw = str(dict(file.meta_json or {}).get("local_path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def test_successful_result_survives_failed_regeneration(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "app.services.ai_video.retry_policy.get_local_path",
        _local_path,
    )
    monkeypatch.setattr(
        "app.services.ai_video.task_state.log_event",
        lambda *_args, **_kwargs: None,
    )
    key = KieApiKey(
        name="retry-generation-key",
        provider_key="sub2api",
        api_key_ciphertext="test",
        is_active=True,
        is_default=False,
    )
    db_session.add(key)
    db_session.flush()
    task = KieTask(
        workspace_id=1,
        key_id=int(key.id),
        model="omni_flash",
        task_id="sub2api-flow-old",
        state="success",
        input_json={"model": "omni_flash", "prompt": "old"},
        result_json={
            "__local": {
                "download_name_base": "old-deliverable",
                "active_provider": "sub2api",
                "active_provider_key_id": int(key.id),
            }
        },
    )
    db_session.add(task)
    db_session.flush()
    old_path = tmp_path / "old.mp4"
    old_path.write_bytes(b"old-success")
    old_file = KieFile(
        workspace_id=1,
        key_id=int(key.id),
        task_id=int(task.id),
        kind="result",
        file_url="https://example.invalid/old.mp4",
        mime_type="video/mp4",
        meta_json={"local_path": str(old_path)},
    )
    db_session.add(old_file)
    db_session.flush()

    assert archive_successful_task_result_files(db_session, task) == 1
    assert old_file.kind == "previous_result"
    reset_video_task_for_retry(db_session, task=task, retry_kind="manual")
    assert task.result_json["__local"]["download_name_base"].endswith("-g1")

    replacement_path = tmp_path / "replacement.mp4"
    replacement_path.write_bytes(b"failed-replacement")
    replacement = KieFile(
        workspace_id=1,
        key_id=int(key.id),
        task_id=int(task.id),
        kind="result",
        file_url="https://example.invalid/replacement.mp4",
        mime_type="video/mp4",
        meta_json={"local_path": str(replacement_path)},
    )
    db_session.add(replacement)
    db_session.flush()

    assert restore_archived_task_result_files(
        db_session,
        task,
        failure_code="upstream_error",
        failure_message="replacement failed",
    ) is True
    db_session.commit()

    assert task.state == "success"
    assert task.task_id == "sub2api-flow-old"
    assert task.fail_code is None
    assert old_path.read_bytes() == b"old-success"
    assert not replacement_path.exists()
    rows = (
        db_session.query(KieFile)
        .filter(KieFile.task_id == int(task.id))
        .order_by(KieFile.id.asc())
        .all()
    )
    assert [(row.id, row.kind) for row in rows] == [(old_file.id, "result")]
    assert task.result_json["__local"]["last_regeneration_failure"]["code"] == (
        "upstream_error"
    )
