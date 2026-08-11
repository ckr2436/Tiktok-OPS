from __future__ import annotations

import importlib
from pathlib import Path

from app.data.models.kie_api import KieFile, KieTask


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_TASKS = BACKEND_ROOT / "app/tasks/ai_video/result_download_tasks.py"
LOCAL_STORAGE = BACKEND_ROOT / "app/services/ai_video/local_storage.py"
AI_VIDEO_TASKS = BACKEND_ROOT / "app/tasks/ai_video/video_tasks.py"
BANDIANWA_SERVICE_TASKS = BACKEND_ROOT / "app/services/bandianwa/tasks.py"
BANDIANWA_CLIENT = BACKEND_ROOT / "app/services/bandianwa/client.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_download_worker_module_imports_with_runtime_settings_available():
    importlib.import_module("app.celery_app")
    module = importlib.import_module("app.tasks.ai_video.result_download_tasks")

    assert int(module.settings.AI_VIDEO_RESULT_DOWNLOAD_TIMEOUT_SECONDS) > 0


def test_download_job_is_registered_before_celery_can_start_it():
    source = _source(DOWNLOAD_TASKS)

    registration = source.index("download_task_id=download_task_id")
    registration_commit = source.index("db.commit()", registration)
    enqueue = source.index("download_task_result_files.apply_async", registration_commit)

    assert registration < registration_commit < enqueue
    assert "task_id=download_task_id" in source[enqueue : enqueue + 1200]
    assert '"remote_task_id": remote_task_id' in source[enqueue : enqueue + 1200]
    assert "queue=AI_VIDEO_DOWNLOAD_TASK_QUEUE" in source[enqueue : enqueue + 1200]


def test_download_execution_isolated_from_recovery_and_generation():
    source = _source(DOWNLOAD_TASKS)
    decorator = source[
        source.index('@celery_app.task(', source.index('def _retry_or_fail'))
        : source.index('def download_task_result_files')
    ]

    assert 'name="ai_video.result.download_task_result_files"' in decorator
    assert "queue=AI_VIDEO_DOWNLOAD_TASK_QUEUE" in decorator
    recovery = source[
        source.index('name="ai_video.result.recover_stale_downloads"')
        : source.index("def recover_stale_result_downloads")
    ]
    assert "queue=AI_VIDEO_MAINTENANCE_TASK_QUEUE" in recovery
    assert 'queue="gmv.tasks.default"' not in source


def test_recovery_finalizes_a_task_when_every_result_is_already_local():
    source = _source(DOWNLOAD_TASKS)
    recovery = source[source.index("def recover_stale_result_downloads") :]

    all_local = recovery.index("if files and all(get_local_path(file) is not None for file in files)")
    mark_success = recovery.index("_mark_success(db, task)", all_local)
    missing_files = recovery.index("if not files:", mark_success)

    assert all_local < mark_success < missing_files
    assert '"finalized_task_ids": finalized_ids' in recovery


def test_recovery_does_not_requeue_a_fresh_download_merely_because_it_has_not_started():
    source = _source(DOWNLOAD_TASKS)
    recovery = source[source.index("def recover_stale_result_downloads") :]

    assert "or started_at is None" not in recovery
    assert "or enqueued_at is None" not in recovery
    assert "enqueued_at is not None and enqueued_at <= cutoff" in recovery
    assert "started_at is not None and started_at <= cutoff" in recovery


def test_download_worker_rejects_stale_queue_and_generation_tokens():
    source = _source(DOWNLOAD_TASKS)
    worker = source[source.index("def download_task_result_files") :]

    assert "ignored_superseded_download" in worker
    assert worker.count("ignored_superseded_generation") >= 2
    assert "expected_remote_task_id" in worker
    assert "ignored_active_download" in worker
    assert "download_execution_token" in worker


def test_completed_local_files_win_over_an_advisory_download_lease_change():
    source = _source(DOWNLOAD_TASKS)
    worker = source[source.index("def download_task_result_files") :]
    after_network = worker.index(
        "if expected_remote_task_id and str(current_task.task_id or \"\")"
    )
    all_local = worker.index(
        "if current_files and not missing_files:",
        after_network,
    )
    mark_success = worker.index("return _mark_success(db, current_task)", all_local)
    lease_check = worker.index(
        'current_meta.get("download_execution_token")',
        mark_success,
    )

    assert after_network < all_local < mark_success < lease_check


def test_download_contract_rejects_square_video_for_portrait_request(monkeypatch, tmp_path):
    module = importlib.import_module("app.tasks.ai_video.result_download_tasks")
    path = tmp_path / "result.mp4"
    path.write_bytes(b"video")
    task = KieTask(input_json={"aspect_ratio": "9:16"}, result_json={})
    file = KieFile(id=91, kind="result", mime_type="video/mp4")

    monkeypatch.setattr(module, "get_local_path", lambda _file: path)
    monkeypatch.setattr(module, "_probe_video_geometry", lambda _path: (960, 960))

    error = module._result_contract_error(task, [file])

    assert error is not None
    assert error[0] == "video_output_aspect_mismatch"
    assert "9:16" in error[1]
    assert task.result_json["__local"]["output_contract"]["passed"] is False


def test_download_contract_accepts_expected_portrait_geometry(monkeypatch, tmp_path):
    module = importlib.import_module("app.tasks.ai_video.result_download_tasks")
    path = tmp_path / "result.mp4"
    path.write_bytes(b"video")
    task = KieTask(input_json={"aspect_ratio": "9:16"}, result_json={})
    file = KieFile(id=92, kind="result", mime_type="video/mp4")

    monkeypatch.setattr(module, "get_local_path", lambda _file: path)
    monkeypatch.setattr(module, "_probe_video_geometry", lambda _path: (720, 1280))

    assert module._result_contract_error(task, [file]) is None
    assert task.result_json["__local"]["output_contract"]["passed"] is True


def test_result_file_is_reloaded_after_network_io_before_committing():
    source = _source(LOCAL_STORAGE)

    network_complete = source.index("tmp_path.replace(final_path)")
    reload_file = source.index("current_file = db.get(KieFile, file_id)", network_complete)
    success_commit = source.index("db.commit()", reload_file)

    assert network_complete < reload_file < success_commit
    assert "Result file was superseded while downloading" in source


def test_result_downloaders_use_distinct_partial_files_and_preserve_a_concurrent_success():
    source = _source(LOCAL_STORAGE)

    assert 'f".part.{uuid4().hex}"' in source
    exception_path = source[source.index("except Exception as exc") :]
    preserve_success = exception_path.index("if get_local_path(current_file) is not None:")
    mark_failed = exception_path.index('"local_download_status": "failed"')

    assert preserve_success < mark_failed


def test_provider_poll_is_serialized_and_stops_refreshing_in_download_state():
    source = _source(AI_VIDEO_TASKS)
    poll_loop = source[source.index("while True:", source.index("def submit_and_poll_ai_video_task")) :]

    lock = poll_loop.index("for_update=True")
    downloading = poll_loop.index('if pre_refresh_state == "downloading"')
    release_lock = poll_loop.index("db.commit()", downloading)
    enqueue = poll_loop.index("queue_task_result_download", release_lock)
    provider_refresh = poll_loop.index("_refresh_current_provider", enqueue)

    assert lock < downloading < release_lock < enqueue < provider_refresh


def test_provider_worker_proves_content_factory_authority_before_every_network_phase():
    source = _source(AI_VIDEO_TASKS)
    worker = source[
        source.index("def submit_and_poll_ai_video_task")
        : source.index('@celery_app.task(', source.index("def submit_and_poll_ai_video_task"))
    ]

    assert worker.count("_content_factory_execution_authority(db, task)") >= 3
    first_authority = worker.index("_content_factory_execution_authority(db, task)")
    claim = worker.index("_claim_poll_owner")
    submit = worker.index("_submit_current_provider")
    poll_loop = worker.index("while True:")
    refresh = worker.index("_refresh_current_provider", poll_loop)

    assert first_authority < claim < submit
    assert worker.index("_content_factory_execution_authority(db, task)", poll_loop) < refresh


def test_video_create_retries_carry_one_stable_provider_idempotency_key():
    service_source = _source(BANDIANWA_SERVICE_TASKS)
    client_source = _source(BANDIANWA_CLIENT)
    submit = service_source[service_source.index("async def submit_bandianwa_task") :]
    create = client_source[client_source.index("async def create_video_task") :]

    assert "idempotency_key=(" in submit
    assert 'f"gmv-video-{int(task.workspace_id)}-{int(task.id)}-{str(task.task_id)}"' in submit
    assert 'headers["Idempotency-Key"] = str(idempotency_key)' in create


def test_provider_failover_commits_new_owner_before_target_lane_publish():
    source = _source(AI_VIDEO_TASKS)
    switch = source[
        source.index("def _switch_to_next_provider_in_place")
        : source.index("def _switch_to_kyy_in_place")
    ]

    owner = switch.index("task.key_id = int(next_key.id)")
    publish_lease = switch.index("submit_enqueued_at=", owner)
    commit = switch.index("db.commit()", owner)
    publish = switch.index("submit_and_poll_ai_video_task.apply_async", commit)

    assert owner < publish_lease < commit < publish
    assert "queue=production_video_queue(task)" in switch[publish:]
    assert "_submit_current_provider(db, task)" not in switch


def test_provider_failover_never_reselects_a_quota_exhausted_key():
    source = _source(AI_VIDEO_TASKS)
    select_next = source[
        source.index("def _next_provider_key")
        : source.index("def _switch_to_next_provider_in_place")
    ]

    quota_exclusions = select_next.index("provider_quota_failed_key_ids")
    current_key = select_next.index("attempted.add(int(task.key_id))")
    route = select_next.index("resolve_video_model_key")
    exclude = select_next.index("exclude_key_ids=attempted", route)

    assert quota_exclusions < current_key < route < exclude


def test_initial_quota_failure_is_persisted_and_switched_without_celery_retry():
    source = _source(AI_VIDEO_TASKS)
    worker = source[
        source.index("def submit_and_poll_ai_video_task")
        : source.index('@celery_app.task(', source.index("def submit_and_poll_ai_video_task"))
    ]
    handler = worker[
        worker.index("except PROVIDER_TASK_ERRORS as exc:")
        : worker.index("while True:")
    ]

    quota = handler.index("_provider_error_is_quota_failure")
    persist = handler.index("_mark_provider_quota_failure", quota)
    switch = handler.index("_switch_to_next_provider_in_place", persist)
    non_quota = handler.index("else:", switch)
    retry = handler.index("self.retry", non_quota)

    assert quota < persist < switch < non_quota < retry


def test_polling_quota_failure_uses_the_same_provider_failover_path():
    source = _source(AI_VIDEO_TASKS)
    worker = source[
        source.index("def submit_and_poll_ai_video_task")
        : source.index('@celery_app.task(', source.index("def submit_and_poll_ai_video_task"))
    ]
    poll = worker[worker.index("while True:") :]
    refresh = poll.index("_refresh_current_provider")
    handler = poll[poll.index("except PROVIDER_TASK_ERRORS as exc:", refresh) :]

    quota = handler.index("_provider_error_is_quota_failure")
    persist = handler.index("_mark_provider_quota_failure", quota)
    switch = handler.index("_switch_to_next_provider_in_place", persist)
    retry = handler.index("self.retry", switch)

    assert quota < persist < switch < retry


def test_replacement_provider_quota_failure_is_not_flattened_to_generic_error():
    source = _source(AI_VIDEO_TASKS)
    handler = source[
        source.index("def _fail_provider_and_advance")
        : source.index("def _content_factory_dependency_pending")
    ]

    classify = handler.index("_provider_error_is_quota_failure")
    persist = handler.index("_mark_provider_quota_failure", classify)
    generic = handler.index("_mark_failed", persist)
    advance = handler.index("_should_advance_exhausted_provider", generic)

    assert classify < persist < generic < advance
