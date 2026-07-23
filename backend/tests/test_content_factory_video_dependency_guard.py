from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.tasks.hermes_agent.content_factory_tasks import (
    _enqueue_queued_local_video_tasks,
    _authoritative_editor_guidance_assets_by_index,
    _authoritative_completed_variant_indices,
    _configured_api_video_variant_parallelism,
    _compatible_chained_reference_route,
    _inflight_api_video_variant_indices,
    _queue_next_variant_after_video_submit,
    _recover_orphaned_bridged_video_assets,
    _segment_release_quality_gate,
    _schedule_video_wait,
    _successful_content_factory_task_missing_continuity,
)
from app.tasks.hermes_agent import content_factory_tasks as content_factory_tasks_module
from app.tasks.bandianwa.video_tasks import _content_factory_dependency_pending
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)


def _task(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(input_json=payload, model=payload.get("model"))


def test_chained_omni_segment_requires_continuity_frame_before_submit():
    task = _task({
        "model": "omni_flash",
        "content_factory_project_key": "cf_test",
        "content_factory_segment_index": 2,
        "content_factory_first_frame": False,
    })

    assert _content_factory_dependency_pending(task) is True


def test_first_or_released_omni_segment_can_submit():
    first = _task({
        "model": "omni_flash",
        "content_factory_project_key": "cf_test",
        "content_factory_segment_index": 1,
        "content_factory_first_frame": False,
    })
    released = _task({
        "model": "omni_flash",
        "content_factory_project_key": "cf_test",
        "content_factory_segment_index": 3,
        "content_factory_first_frame": True,
    })

    assert _content_factory_dependency_pending(first) is False
    assert _content_factory_dependency_pending(released) is False


def test_non_content_factory_task_is_not_affected():
    task = _task({
        "model": "omni_flash",
        "content_factory_segment_index": 2,
        "content_factory_first_frame": False,
    })

    assert _content_factory_dependency_pending(task) is False


def test_independent_successful_segment_does_not_require_previous_frame():
    task = SimpleNamespace(
        state="success",
        input_json={
            "content_factory_project_key": "cf_parallel",
            "content_factory_segment_index": 3,
            "content_factory_continuity_dependency": "independent",
            "content_factory_first_frame": False,
            "reference_file_paths": [],
        },
    )

    assert _successful_content_factory_task_missing_continuity(task) is False


def test_segment_release_gate_blocks_bad_media_before_dependency_release(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"downloaded-provider-result")
    task = SimpleNamespace(
        id=91,
        state="success",
        fail_code=None,
        fail_msg=None,
        result_json={},
        input_json={
            "seconds": 10,
            "aspect_ratio": "9:16",
            "content_factory_audio_mode": "spoken",
            "content_factory_product_anchor_required": False,
        },
    )

    class FakeDb:
        def add(self, _row):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(
        content_factory_tasks_module,
        "_probe_video_duration_seconds",
        lambda _path: 5.0,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_video_dimensions",
        lambda _path: (1280, 720),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_video_has_audio_stream",
        lambda _path: False,
    )

    report = _segment_release_quality_gate(
        FakeDb(),
        project=SimpleNamespace(id=1),
        task=task,
        source=source,
    )

    assert report["status"] == "FAIL"
    assert task.state == "failed"
    assert task.fail_code == "segment_release_quality_gate"
    assert any("duration mismatch" in item for item in report["failures"])
    assert any("aspect mismatch" in item for item in report["failures"])
    assert any("requires an audio stream" in item for item in report["failures"])


def test_chained_reference_route_drops_only_optional_refs_for_compatible_failover(monkeypatch):
    task = SimpleNamespace(
        model="omni_flash",
        key_id=3,
        input_json={
            "model": "omni_flash",
            "aspect_ratio": "9:16",
            "video_frame_mode": "reference",
            "seconds": "10",
            "resolution": "720p",
        },
        result_json={"__local": {"attempted_provider_key_ids": [3]}},
    )
    continuity = {"filename": "continuity.png", "is_continuity_frame": True}
    optional = {"filename": "scene.png", "is_product_anchor": False}
    calls = []

    def _resolve(_db, **kwargs):
        calls.append(kwargs)
        if kwargs["reference_count"] != 1:
            raise ValueError("route does not accept this reference count")
        return SimpleNamespace(id=7, provider_key="toapis")

    monkeypatch.setattr(content_factory_tasks_module, "resolve_video_model_key", _resolve)

    selected, key = _compatible_chained_reference_route(
        object(), task, [continuity, optional], product_required=False
    )

    assert selected == [continuity]
    assert key.id == 7
    assert [call["reference_count"] for call in calls] == [2, 1]
    assert calls[0]["exclude_key_ids"] == {3}


def test_chained_reference_route_never_drops_required_product_anchor(monkeypatch):
    task = SimpleNamespace(
        model="omni_flash",
        input_json={"model": "omni_flash", "seconds": 10},
        result_json={"__local": {"attempted_provider_key_ids": [3]}},
    )
    continuity = {"filename": "continuity.png", "is_continuity_frame": True}
    optional = {"filename": "scene.png"}
    product = {"filename": "product.png", "is_product_anchor": True}

    def _resolve(_db, **kwargs):
        if kwargs["reference_count"] != 3:
            raise ValueError("only the complete product packet is supported")
        return SimpleNamespace(id=7, provider_key="toapis")

    monkeypatch.setattr(content_factory_tasks_module, "resolve_video_model_key", _resolve)

    selected, key = _compatible_chained_reference_route(
        object(), task, [continuity, optional, product], product_required=True
    )

    assert selected == [continuity, optional, product]
    assert key.id == 7


def test_dependency_release_inherits_only_durable_provider_quota_exclusions():
    source = Path(content_factory_tasks_module.__file__).read_text(encoding="utf-8")
    release = source[
        source.index("def _release_ready_segment_dependencies")
        : source.index("def _fail_unreleasable_segment_dependencies")
    ]

    inherited = release.index('previous_meta.get("provider_quota_failed_key_ids")')
    persist = release.index("attempted_provider_key_ids=sorted(attempted_key_ids)", inherited)
    select = release.index("_compatible_chained_reference_route", persist)

    assert inherited < persist < select


def test_completed_variant_indices_are_rebuilt_only_from_local_video_assets():
    assert _authoritative_completed_variant_indices(
        [1, 2, 3, 5, 7, 99],
        target=7,
    ) == [1, 2, 3, 5, 7]


def test_removed_local_video_cannot_survive_as_stale_completed_metadata():
    stale_pipeline = {
        "completed_indices": [1, 2, 5],
        "submitted_indices": [1, 2, 3, 4, 5, 6],
    }

    completed = _authoritative_completed_variant_indices([2, 3], target=7)

    assert stale_pipeline["completed_indices"] == [1, 2, 5]
    assert completed == [2, 3]


def test_editor_guidance_authority_requires_matching_completed_video_and_local_file(
    tmp_path,
):
    valid_path = tmp_path / "v2-guide.md"
    valid_path.write_text("# guide", encoding="utf-8")
    missing_path = tmp_path / "v3-guide.md"
    rows = [
        SimpleNamespace(
            id=20,
            file_path=str(valid_path),
            meta_json={"content_factory_video_index": 2},
        ),
        SimpleNamespace(
            id=21,
            file_path=str(missing_path),
            meta_json={"content_factory_video_index": 3},
        ),
        SimpleNamespace(
            id=22,
            file_path=str(valid_path),
            meta_json={"content_factory_video_index": 9},
        ),
    ]

    class FakeQuery:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return rows

    class FakeDb:
        def query(self, *_args):
            return FakeQuery()

    project = SimpleNamespace(id=168)
    selected = _authoritative_editor_guidance_assets_by_index(
        FakeDb(),
        project,
        completed_video_indices={2, 3},
    )

    assert selected == {2: rows[0]}


def test_orphaned_bridge_video_is_restored_with_durable_plan_metadata(
    db_session,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "content"
    bridge_root = storage_root / "browser_inbox"
    monkeypatch.setattr(
        content_factory_tasks_module,
        "CONTENT_FACTORY_STORAGE_ROOT",
        storage_root,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "BROWSER_INBOX_ROOT",
        bridge_root,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_probe_video_duration_seconds",
        lambda _path: 40.041,
    )
    project = HermesContentFactoryProject(
        project_key="cf_recover_bridge_video",
        workspace_id=3,
        user_id=6,
        title="Recovery",
        product_name="MYUPONA",
        status="paused",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_count": 50,
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
            "video_resolution": "720p",
        },
        state_json={"active_variant_index": 25},
    )
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="superseded",
        input_json={"variant_index": 1},
        output_json={
            "result": {
                "videos": [{
                    "video_index": 1,
                    "title": "The Promise She Missed",
                    "segments": [{
                        "segment_index": 1,
                        "duration_seconds": 10,
                        "segment_goal": "Loss hook",
                        "timeline": [{"action": "A promise is missed."}],
                        "dialogue_lines": [{"line": "She stopped waiting."}],
                    }],
                }],
            },
        },
    )
    db_session.add(stage)
    db_session.commit()
    bridge_dir = (
        bridge_root
        / f"workspace_{project.workspace_id}"
        / project.project_key
    )
    bridge_dir.mkdir(parents=True)
    bridge_file = bridge_dir / "strong-pain-v01-1.mp4"
    bridge_file.write_bytes(b"x" * 2048)

    recovered = _recover_orphaned_bridged_video_assets(
        db_session,
        project,
    )
    db_session.commit()

    assert len(recovered) == 1
    asset = recovered[0]
    assert Path(asset.file_path).read_bytes() == bridge_file.read_bytes()
    assert asset.meta_json["content_factory_video_index"] == 1
    assert asset.meta_json["content_factory_variant_index"] == 1
    assert asset.meta_json["version_name"] == "The Promise She Missed"
    assert asset.meta_json["segment_plan"][0]["segment_goal"] == "Loss hook"
    assert asset.meta_json["recovered_from_bridge_copy"] is True
    assert project.state_json["ai_video_final_asset_ids"] == [asset.id]
    assert project.state_json["ai_video_ready_video_count"] == 1
    assert project.state_json["video_variant_pipeline"]["completed_indices"] == [1]

    assert _recover_orphaned_bridged_video_assets(
        db_session,
        project,
    ) == []


def test_successful_chained_segment_is_rejected_before_composition_without_continuity_reference():
    task = SimpleNamespace(
        state="success",
        model="omni_flash",
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_segment_index": 3,
            "content_factory_first_frame": False,
            "reference_file_paths": [{"path": "/tmp/action.png", "is_continuity_frame": False}],
        },
    )

    assert _successful_content_factory_task_missing_continuity(task) is True


def test_successful_chained_segment_with_declared_continuity_reference_can_compose():
    task = SimpleNamespace(
        state="success",
        model="omni_flash",
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_segment_index": 2,
            "content_factory_first_frame": True,
            "reference_file_paths": [
                {"path": "/tmp/previous-last-frame.png", "is_continuity_frame": True},
                {"path": "/tmp/action.png", "is_continuity_frame": False},
            ],
        },
    )

    assert _successful_content_factory_task_missing_continuity(task) is False


def test_local_content_factory_segment_is_reenqueued_without_waiting_for_stale_recovery(monkeypatch):
    submissions: list[dict] = []

    def _capture(**kwargs):
        submissions.append(kwargs)

    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_bandianwa_video_task,
        "apply_async",
        _capture,
    )
    project = SimpleNamespace(workspace_id=3)
    queued = SimpleNamespace(id=81, state="queued_local", result_json={})
    already_running = SimpleNamespace(id=82, state="in_progress", result_json={})

    assert _enqueue_queued_local_video_tasks(project, [queued, already_running]) == [81]
    assert submissions == [{
        "kwargs": {
            "workspace_id": 3,
            "local_task_id": 81,
            "interval_seconds": 15,
            "timeout_seconds": 600,
        },
        "queue": "gmv.tasks.ai_video",
    }]


def test_dependency_release_is_not_immediately_published_twice(monkeypatch):
    submissions: list[dict] = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_bandianwa_video_task,
        "apply_async",
        lambda **kwargs: submissions.append(kwargs),
    )
    project = SimpleNamespace(workspace_id=3)
    just_released = SimpleNamespace(id=81, state="queued_local", result_json={})
    abandoned = SimpleNamespace(id=82, state="queued_local", result_json={})

    queued = _enqueue_queued_local_video_tasks(
        project,
        [just_released, abandoned],
        exclude_task_ids={81},
    )

    assert queued == [82]
    assert [item["kwargs"]["local_task_id"] for item in submissions] == [82]


def test_local_content_factory_segment_with_fresh_submit_lease_is_not_reenqueued(monkeypatch):
    submissions: list[dict] = []

    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_bandianwa_video_task,
        "apply_async",
        lambda **kwargs: submissions.append(kwargs),
    )
    project = SimpleNamespace(workspace_id=3)
    claimed = SimpleNamespace(
        id=81,
        state="queued_local",
        result_json={
            "__local": {
                "poll_owner_task_id": "live-celery-task",
                "poll_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )

    assert _enqueue_queued_local_video_tasks(project, [claimed]) == []
    assert submissions == []


def test_local_content_factory_segment_with_expired_submit_lease_is_reenqueued(monkeypatch):
    submissions: list[dict] = []

    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_bandianwa_video_task,
        "apply_async",
        lambda **kwargs: submissions.append(kwargs),
    )
    project = SimpleNamespace(workspace_id=3)
    abandoned = SimpleNamespace(
        id=81,
        state="queued_local",
        result_json={
            "__local": {
                "poll_owner_task_id": "dead-celery-task",
                "poll_heartbeat_at": (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(),
            }
        },
    )

    assert _enqueue_queued_local_video_tasks(project, [abandoned]) == [81]
    assert submissions[0]["kwargs"]["local_task_id"] == 81


def test_local_content_factory_segment_with_fresh_publish_lease_is_not_reenqueued(monkeypatch):
    submissions: list[dict] = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_bandianwa_video_task,
        "apply_async",
        lambda **kwargs: submissions.append(kwargs),
    )
    project = SimpleNamespace(workspace_id=3)
    published = SimpleNamespace(
        id=81,
        state="queued_local",
        result_json={
            "__local": {
                "submit_enqueued_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )

    assert _enqueue_queued_local_video_tasks(project, [published]) == []
    assert submissions == []


def test_queued_local_publish_records_a_short_recovery_lease(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_bandianwa_video_task,
        "apply_async",
        lambda **_kwargs: None,
    )
    project = SimpleNamespace(workspace_id=3)
    task = SimpleNamespace(id=81, state="queued_local", result_json={})

    assert _enqueue_queued_local_video_tasks(project, [task]) == [81]
    assert task.result_json["__local"]["submit_enqueued_at"]


def test_successor_video_waiter_carries_its_predecessor_token(monkeypatch):
    submissions: list[dict] = []

    monkeypatch.setattr(
        content_factory_tasks_module.wait_for_content_factory_videos,
        "apply_async",
        lambda **kwargs: submissions.append(kwargs) or SimpleNamespace(id="next-waiter"),
    )

    class Db:
        def __init__(self):
            self.commits = 0

        def commit(self):
            self.commits += 1

    project = SimpleNamespace(id=168, state_json={"ai_video_wait_task_id": "prior-waiter"})
    db = Db()

    assert _schedule_video_wait(db, project, countdown=20, reason="test") == "next-waiter"
    assert submissions == [{
        "kwargs": {"project_id": 168, "predecessor_wait_id": "prior-waiter"},
        "countdown": 20,
        "queue": "gmv.tasks.hermes_agent",
    }]
    assert project.state_json["ai_video_wait_task_id"] == "next-waiter"
    assert db.commits == 1


def test_api_video_wait_hibernates_without_releasing_the_project_browser_lease(monkeypatch):
    """Submitting provider work must not make the Agent recycle Chrome.

    A bridge release queries HermesBrowserBridge rows.  The API-video handoff
    deliberately has no such query: the existing project lease remains until
    a terminal or explicit-pause path releases it.
    """
    hibernations: list[int] = []

    class FakeDB:
        def add(self, _value):
            pass

        def commit(self):
            pass

        def query(self, *_args, **_kwargs):
            raise AssertionError("API video handoff must not release the browser bridge")

    monkeypatch.setattr(
        content_factory_tasks_module,
        "hibernate_project_browser_slot_for_api_video",
        lambda _db, *, project: hibernations.append(int(project.id)) or True,
    )

    project = SimpleNamespace(
        id=71,
        config_json={"video_count": 2, "auto_run": True, "manual_paused": False},
        state_json={
            "video_variant_pipeline": {
                "target_count": 2,
                "active_index": 1,
                "submitted_indices": [],
                "completed_indices": [],
                "failed_indices": [],
            },
        },
        current_stage="VIDEO_PROMPTS",
        status="running",
        last_error=None,
    )

    assert _queue_next_variant_after_video_submit(
        FakeDB(), project, submitted_variant_index=1,
    ) is None
    assert project.current_stage == "WAITING_VIDEO_INPUT"
    assert project.status == "generating_video"
    assert project.state_json["video_variant_pipeline"]["awaiting_completed_variant_index"] == 1
    assert hibernations == [71]


def test_api_video_parallelism_is_explicitly_bounded_and_releases_failed_variant_slots():
    project = SimpleNamespace(
        config_json={"video_count": 50, "max_api_video_variants_in_flight": 99},
        state_json={
            "video_variant_pipeline": {
                "target_count": 50,
                "active_index": 3,
                "submitted_indices": [1, 2, 3],
                "completed_indices": [1],
                "failed_indices": [],
            },
            "ai_video_group_statuses": [
                {"video_index": 2, "status": "failed"},
            ],
            "ai_video_groups": [
                {"video_index": 2, "segments": [{"task_id": 202}]},
                {"video_index": 3, "segments": [{"task_id": 303}]},
            ],
        },
    )

    assert _configured_api_video_variant_parallelism(project) == 4
    assert _inflight_api_video_variant_indices(project) == {3}


def test_stale_submitted_variant_without_a_task_group_does_not_consume_parallel_slot():
    project = SimpleNamespace(
        config_json={"video_count": 50, "max_api_video_variants_in_flight": 2},
        state_json={
            "video_variant_pipeline": {
                "target_count": 50,
                "active_index": 38,
                "submitted_indices": [6, 9, 11, 37],
                "completed_indices": [37],
                "failed_indices": [],
            },
            "ai_video_groups": [
                {"video_index": 37, "segments": [{"task_id": 2610}]},
            ],
            "ai_video_group_statuses": [
                {"video_index": 37, "status": "composed"},
            ],
        },
    )

    assert _inflight_api_video_variant_indices(project) == set()


def test_api_parallel_submit_queues_one_next_browser_turn_without_expanding_the_cap(monkeypatch):
    hibernations: list[int] = []
    queued: list[str] = []
    next_stage = SimpleNamespace(id=912, stage="CREATIVE")

    class FakeDB:
        def add(self, _value):
            pass

        def commit(self):
            pass

        def query(self, *_args, **_kwargs):
            raise AssertionError("the scheduling decision is isolated from browser release")

    monkeypatch.setattr(
        content_factory_tasks_module,
        "hibernate_project_browser_slot_for_api_video",
        lambda _db, *, project: hibernations.append(int(project.id)) or True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_queue_next_unsubmitted_serial_variant_if_needed",
        lambda _db, _project, *, reason: queued.append(reason) or next_stage,
    )
    project = SimpleNamespace(
        id=72,
        config_json={
            "video_count": 50,
            "auto_run": True,
            "manual_paused": False,
            "max_api_video_variants_in_flight": 2,
        },
        state_json={
            "video_variant_pipeline": {
                "target_count": 50,
                "active_index": 1,
                "submitted_indices": [],
                "completed_indices": [],
                "failed_indices": [],
            },
        },
        current_stage="VIDEO_PROMPTS",
        status="running",
        last_error=None,
    )

    assert _queue_next_variant_after_video_submit(
        FakeDB(), project, submitted_variant_index=1,
    ) is next_stage
    pipeline = project.state_json["video_variant_pipeline"]
    assert pipeline["mode"] == "bounded_api_parallel_v1"
    assert pipeline["max_api_video_variants_in_flight"] == 2
    assert pipeline["submitted_indices"] == [1]
    assert hibernations == [72]
    assert len(queued) == 1


def test_api_parallel_submit_does_not_queue_a_third_variant_when_the_window_is_full(monkeypatch):
    queued: list[str] = []

    class FakeDB:
        def add(self, _value):
            pass

        def commit(self):
            pass

        def query(self, *_args, **_kwargs):
            raise AssertionError("the browser lease must not be released")

    monkeypatch.setattr(
        content_factory_tasks_module,
        "hibernate_project_browser_slot_for_api_video",
        lambda _db, *, project: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_queue_next_unsubmitted_serial_variant_if_needed",
        lambda _db, _project, *, reason: queued.append(reason),
    )
    project = SimpleNamespace(
        id=73,
        config_json={
            "video_count": 50,
            "auto_run": True,
            "manual_paused": False,
            "max_api_video_variants_in_flight": 2,
        },
        state_json={
            "video_variant_pipeline": {
                "target_count": 50,
                "active_index": 2,
                "submitted_indices": [1, 2],
                "completed_indices": [],
                "failed_indices": [],
            },
            "ai_video_groups": [
                {"video_index": 1, "segments": [{"task_id": 101}]},
                {"video_index": 2, "segments": [{"task_id": 202}]},
            ],
        },
        current_stage="VIDEO_PROMPTS",
        status="running",
        last_error=None,
    )

    assert _queue_next_variant_after_video_submit(
        FakeDB(), project, submitted_variant_index=2,
    ) is None
    assert queued == []


def test_content_factory_retry_uses_the_project_hermes_queue():
    assert content_factory_tasks_module.project_hermes_queue(SimpleNamespace()) == "gmv.tasks.hermes_agent"


def test_serial_variant_resume_never_republishes_a_globally_superseded_stage():
    source = (
        Path(content_factory_tasks_module.__file__)
        .read_text(encoding="utf-8")
    )
    resume = source[
        source.index("def _queue_serial_variant_resume_stage")
        : source.index("def _queue_next_unsubmitted_serial_variant_if_needed")
    ]

    assert "global_latest_for_target = _latest_stage" in resume
    assert "int(global_latest_for_target.id) == int(stage.id)" in resume
    assert "stage = _create_repair_stage" in resume


def test_stage_delivery_identity_is_committed_before_broker_publish():
    source = Path(content_factory_tasks_module.__file__).read_text(encoding="utf-8")
    publisher = source[
        source.index("def _publish_stage")
        : source.index("def _lock_stage_delivery_scope")
    ]

    register = publisher.index("stage.celery_task_id = celery_task_id")
    commit = publisher.index("session.commit()", register)
    publish = publisher.index("run_content_factory_stage.apply_async", commit)

    assert register < commit < publish
    assert "task_id=celery_task_id" in publisher[publish:]
    assert "soft_time_limit=soft_time_limit" in publisher[publish:]
    assert "time_limit=hard_time_limit" in publisher[publish:]
