from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.hermes_agent.content_rollout_gate import (
    parse_variant_rollout_gate,
    rollout_checkpoint_reached,
    rollout_variant_authorized,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _clear_terminal_video_work_ledger_at_checkpoint,
    _pause_at_variant_rollout_checkpoint,
)
from app.core.errors import APIError
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.services.hermes_agent.content_factory import (
    _refresh_stale_universal_director_profile,
    configure_variant_rollout_gate,
    resume_waiting_project_production,
)
from app.services.hermes_agent.content_director_profile import (
    compile_universal_director_series_brief,
)


def _config(*indices: int, pause_when_complete: bool = True) -> dict:
    return {
        "variant_rollout_gate": {
            "enabled": True,
            "schema_version": "1.0",
            "batch_id": "batch-canary-01",
            "authorized_variant_indices": list(indices),
            "pause_when_complete": pause_when_complete,
        }
    }


def test_disabled_gate_preserves_unbounded_project_behavior() -> None:
    assert rollout_variant_authorized(
        {},
        target_count=50,
        variant_index=43,
    ) is True
    assert rollout_checkpoint_reached(
        {},
        target_count=50,
        completed_variant_indices=range(1, 51),
    ) is None


def test_release_manifest_allows_only_explicit_variants() -> None:
    config = _config(41, 42)

    assert rollout_variant_authorized(
        config,
        target_count=50,
        variant_index=41,
    ) is True
    assert rollout_variant_authorized(
        config,
        target_count=50,
        variant_index=42,
    ) is True
    assert rollout_variant_authorized(
        config,
        target_count=50,
        variant_index=6,
    ) is False
    assert rollout_variant_authorized(
        config,
        target_count=50,
        variant_index=43,
    ) is False


def test_checkpoint_requires_every_released_variant_to_be_durable() -> None:
    config = _config(41, 42)

    assert rollout_checkpoint_reached(
        config,
        target_count=50,
        completed_variant_indices=[1, 2, 41],
    ) is None
    reached = rollout_checkpoint_reached(
        config,
        target_count=50,
        completed_variant_indices=[1, 2, 41, 42],
    )
    assert reached is not None
    assert reached.batch_id == "batch-canary-01"
    assert reached.authorized_variant_indices == (41, 42)


def test_gate_can_release_a_non_contiguous_repair_batch() -> None:
    config = _config(6, 9, 11)

    reached = rollout_checkpoint_reached(
        config,
        target_count=50,
        completed_variant_indices=[6, 9, 11, 40],
    )
    assert reached is not None
    assert reached.authorized_variant_indices == (6, 9, 11)


def test_checkpoint_resume_immediately_publishes_authorized_missing_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_stage = SimpleNamespace(id=501)
    project = SimpleNamespace(
        current_stage="WAITING_VIDEO_INPUT",
        status="ready",
        state_json={"ai_video_pending_task_ids": []},
    )
    db = MagicMock()
    observed: list[tuple[object, object, str]] = []

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._queue_missing_serial_video_variant_if_needed",
        lambda actual_db, actual_project, *, reason: (
            observed.append((actual_db, actual_project, reason)) or queued_stage
        ),
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._publish_stage",
        lambda stage, actual_project: (
            "celery-release-501"
            if stage is queued_stage and actual_project is project
            else ""
        ),
    )

    task_id = resume_waiting_project_production(db, project)

    assert task_id == "celery-release-501"
    assert observed and observed[0][0] is db and observed[0][1] is project
    assert "authorized incomplete video" in observed[0][2]
    db.flush.assert_called_once_with()


def test_resume_reconciles_terminal_paid_media_before_missing_variant_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = SimpleNamespace(
        id=176,
        current_stage="WAITING_VIDEO_INPUT",
        status="ready",
        state_json={
            "ai_video_pending_task_ids": [],
            "ai_video_resume_failed_task_ids": [],
            "ai_video_task_ids": [2917, 2918, 2922, 2923],
            "ai_video_groups": [
                {"video_index": 4, "segments": [{"task_id": 2917}]},
                {"video_index": 5, "segments": [{"task_id": 2922}]},
            ],
        },
    )
    db = MagicMock()
    queued = SimpleNamespace(id="wait-terminal-media-176")
    observed: list[dict] = []

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.wait_for_content_factory_videos.apply_async",
        lambda **kwargs: observed.append(kwargs) or queued,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._queue_missing_serial_video_variant_if_needed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing-variant cleanup must wait for paid-media reconciliation")
        ),
    )

    task_id = resume_waiting_project_production(db, project)

    assert task_id == "wait-terminal-media-176"
    assert observed == [{
        "kwargs": {"project_id": 176},
        "countdown": 1,
        "queue": "gmv.tasks.hermes_agent",
        "priority": 9,
    }]
    assert project.state_json["ai_video_wait_task_id"] == task_id
    assert "terminal paid media" in project.state_json["ai_video_wait_reason"]
    db.add.assert_called_once_with(project)
    db.flush.assert_called_once_with()


def test_stale_universal_profile_versions_contract_and_retires_pointer(
    db_session,
) -> None:
    brief = compile_universal_director_series_brief(
        series_id="cf_profile_upgrade.series",
        objective="Create emotionally sharp but truthful short videos.",
        platform="TikTok",
        locale="en-US",
        audience="US adults.",
        target_count=3,
        minimum_duration_seconds=40,
        maximum_duration_seconds=40,
        product_required=True,
        brand_name="MYUPONA",
        product_name="Sleep Ease Gummies",
        market="US",
        project_brief=None,
        confirmed_claims=["Melatonin-free"],
        confirmed_selling_points=["sugar free"],
        confirmed_promotions="$7.99",
        promotion_cta="Find it in the yellow cart.",
    )
    stale = brief.model_dump(mode="json")
    stale["truth_payload"]["profile_id"] = "universal-short-video-v2"
    project = HermesContentFactoryProject(
        project_key="cf_profile_upgrade",
        workspace_id=3,
        user_id=6,
        title="Profile upgrade",
        product_name="Sleep Ease Gummies",
        market="US",
        status="paused",
        current_stage="PRODUCTION_PLAN",
        config_json={
            "content_director_mode": "enforce",
            "director_series_brief_source": "universal_profile",
            "director_series_brief": stale,
            "content_objective": brief.objective,
            "target_audience": brief.audience,
            "video_count": 3,
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
            "product_required": True,
            "brand_name": "MYUPONA",
            "confirmed_claims": ["Melatonin-free"],
            "confirmed_selling_points": ["sugar free"],
            "confirmed_promotions": "$7.99",
            "promotion_cta": "Find it in the yellow cart.",
            "director_loop_policy": {
                "maximum_revisions": 4,
                "maximum_contract_repairs_per_revision": 1,
            },
        },
        state_json={
            "product_knowledge": {"source": "test-facts"},
            "approved_series_slate": {"slate_sha256": "old"},
        },
    )
    db_session.add(project)
    db_session.flush()

    assert _refresh_stale_universal_director_profile(db_session, project) is True
    upgraded = project.config_json["director_series_brief"]
    assert upgraded["series_version"] == 2
    assert (
        upgraded["truth_payload"]["profile_id"]
        == "universal-short-video-v8-autonomy"
    )
    assert (
        project.config_json["director_loop_policy"]
        ["maximum_contract_repairs_per_revision"]
        == 2
    )
    assert "approved_series_slate" not in project.state_json
    assert project.state_json["director_profile_upgrade"]["series_version"] == 2


def test_completed_release_manifest_creates_automatic_quality_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[int] = []
    monkeypatch.setattr(
        "app.services.hermes_agent.content_factory._release_bridge_for_project",
        lambda _db, *, project_id: released.append(int(project_id)),
    )
    project = SimpleNamespace(
        id=168,
        config_json={
            "video_count": 50,
            **_config(41, 42),
        },
        state_json={"browser_slot_mode": "dormant"},
        status="generating_video",
        last_error=None,
    )
    db = MagicMock()

    result = _pause_at_variant_rollout_checkpoint(
        db,
        project,
        completed_variant_indices=[1, 2, 41, 42],
    )

    assert result is not None
    assert result["batch_id"] == "batch-canary-01"
    assert result["status"] == "awaiting_operator_review"
    assert project.status == "paused"
    assert project.state_json["pause_reason_code"] == "variant_rollout_checkpoint"
    assert project.config_json.get("manual_paused") is not True
    assert released == [168]
    db.add.assert_called_once_with(project)


def _ledger_project(*, task_ids: list[int], groups: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        id=168,
        workspace_id=3,
        user_id=6,
        state_json={
            "ai_video_task_ids": task_ids,
            "ai_video_groups": groups,
            "ai_video_pending_task_ids": task_ids,
            "ai_video_failed_task_ids": [task_ids[-1]] if task_ids else [],
            "ai_video_wait_task_id": "waiter-123",
            "ai_video_group_statuses": [{"video_index": 42, "status": "composed"}],
        },
    )


def test_rollout_checkpoint_retires_only_fully_terminal_video_ledger() -> None:
    project = _ledger_project(
        task_ids=[2662, 2663],
        groups=[{
            "video_index": 42,
            "segments": [{"task_id": 2662}, {"task_id": 2663}],
        }],
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        (2662, "success"),
        (2663, "timeout"),
    ]

    state, cleared = _clear_terminal_video_work_ledger_at_checkpoint(
        db,
        project,
        dict(project.state_json),
    )

    assert cleared is True
    assert state["ai_video_task_ids"] == []
    assert state["ai_video_groups"] == []
    assert state["ai_video_pending_task_ids"] == []
    assert state["ai_video_failed_task_ids"] == []
    assert state["ai_video_wait_task_id"] is None
    assert state["ai_video_retired_task_ids"] == [2662, 2663]
    assert state["ai_video_group_statuses"] == [
        {"video_index": 42, "status": "composed"}
    ]


def test_rollout_checkpoint_preserves_ledger_while_submitted_work_drains() -> None:
    project = _ledger_project(
        task_ids=[2662, 2663],
        groups=[{
            "video_index": 42,
            "segments": [{"task_id": 2662}, {"task_id": 2663}],
        }],
    )
    original = dict(project.state_json)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        (2662, "success"),
        (2663, "generating"),
    ]

    state, cleared = _clear_terminal_video_work_ledger_at_checkpoint(
        db,
        project,
        dict(project.state_json),
    )

    assert cleared is False
    assert state == original


def test_existing_rollout_checkpoint_still_retires_terminal_active_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[int] = []
    monkeypatch.setattr(
        "app.services.hermes_agent.content_factory._release_bridge_for_project",
        lambda _db, *, project_id: released.append(int(project_id)),
    )
    project = SimpleNamespace(
        id=168,
        workspace_id=3,
        user_id=6,
        config_json={"video_count": 50, **_config(41, 42)},
        state_json={
            "variant_rollout_checkpoint": {
                "batch_id": "batch-canary-01",
                "status": "awaiting_operator_review",
            },
            "ai_video_task_ids": [2662],
            "ai_video_groups": [{
                "video_index": 42,
                "segments": [{"task_id": 2662}],
            }],
        },
        status="paused",
        last_error=None,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [
        (2662, "success"),
    ]

    result = _pause_at_variant_rollout_checkpoint(
        db,
        project,
        completed_variant_indices=[41, 42],
    )

    assert result == {
        "batch_id": "batch-canary-01",
        "status": "awaiting_operator_review",
    }
    assert project.state_json["ai_video_task_ids"] == []
    assert project.state_json["ai_video_groups"] == []
    assert project.state_json["ai_video_retired_task_ids"] == [2662]
    assert released == []
    db.add.assert_called_once_with(project)


def test_manual_pause_wins_when_rollout_checkpoint_races_operator_control() -> None:
    project = SimpleNamespace(
        id=168,
        config_json={
            "video_count": 50,
            "manual_paused": True,
            **_config(41, 42),
        },
        state_json={"pause_reason_code": "manual"},
        status="paused",
        last_error="operator pause",
    )
    db = MagicMock()

    result = _pause_at_variant_rollout_checkpoint(
        db,
        project,
        completed_variant_indices=[41, 42],
    )

    assert result == {
        "status": "manual_pause_wins_rollout_checkpoint",
        "batch_id": "batch-canary-01",
    }
    assert project.state_json == {"pause_reason_code": "manual"}
    assert project.last_error == "operator pause"
    db.add.assert_not_called()


def _paused_project() -> HermesContentFactoryProject:
    return HermesContentFactoryProject(
        project_key="cf_rollout_gate_test",
        workspace_id=3,
        user_id=6,
        title="Universal rollout gate",
        product_name="Test product",
        status="paused",
        current_stage="DIRECTOR",
        config_json={
            "video_count": 50,
            "manual_paused": True,
        },
        state_json={
            "pause_reason_code": "manual",
            "ai_video_task_ids": [],
        },
    )


def test_service_persists_release_manifest_without_resuming_project(
    db_session,
) -> None:
    project = _paused_project()
    db_session.add(project)
    db_session.commit()

    configure_variant_rollout_gate(
        db_session,
        project,
        authorized_variant_indices=[41, 42],
        batch_id="sample-01",
        pause_when_complete=True,
        released_by_user_id=6,
    )
    db_session.commit()
    db_session.refresh(project)

    gate = project.config_json["variant_rollout_gate"]
    assert gate == {
        "enabled": True,
        "schema_version": "1.0",
        "batch_id": "sample-01",
        "authorized_variant_indices": [41, 42],
        "pause_when_complete": True,
    }
    assert project.status == "paused"
    assert project.config_json["manual_paused"] is True
    assert project.state_json["variant_rollout_release"]["released_by_user_id"] == 6
    assert project.state_json["variant_rollout_gate_history"] == []


def test_service_archives_previous_release_and_checkpoint(db_session) -> None:
    project = _paused_project()
    project.config_json = {
        **dict(project.config_json or {}),
        "variant_rollout_gate": _config(41, 42)["variant_rollout_gate"],
    }
    project.state_json = {
        **dict(project.state_json or {}),
        "variant_rollout_checkpoint": {
            "batch_id": "batch-canary-01",
            "status": "awaiting_operator_review",
        },
    }
    db_session.add(project)
    db_session.commit()

    configure_variant_rollout_gate(
        db_session,
        project,
        authorized_variant_indices=[43, 44, 45, 46],
        batch_id="scale-02",
        released_by_user_id=6,
    )
    db_session.commit()
    db_session.refresh(project)

    history = project.state_json["variant_rollout_gate_history"]
    assert len(history) == 1
    assert history[0]["release_manifest"]["batch_id"] == "batch-canary-01"
    assert history[0]["checkpoint"]["status"] == "awaiting_operator_review"
    assert project.state_json.get("variant_rollout_checkpoint") is None
    assert project.config_json["variant_rollout_gate"]["batch_id"] == "scale-02"


def test_service_retires_paused_variant_excluded_by_narrowed_release(
    db_session,
) -> None:
    project = _paused_project()
    project.current_stage = "PRODUCTION_PLAN"
    project.config_json = {
        **dict(project.config_json or {}),
        "video_count": 6,
    }
    project.state_json = {
        **dict(project.state_json or {}),
        "video_variant_pipeline": {
            "target_count": 6,
            "active_index": 6,
            "submitted_indices": [1, 2, 3, 4, 5],
            "completed_indices": [1, 2, 3],
            "failed_indices": [],
        },
    }
    db_session.add(project)
    db_session.flush()
    paused_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="PRODUCTION_PLAN",
        attempt=1,
        status="paused",
        input_json={"variant_index": 6},
    )
    db_session.add(paused_stage)
    db_session.commit()

    configure_variant_rollout_gate(
        db_session,
        project,
        authorized_variant_indices=[4, 5],
        batch_id="stop-before-six",
        released_by_user_id=6,
    )
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(paused_stage)

    assert paused_stage.status == "failed"
    assert project.current_stage == "WAITING_VIDEO_INPUT"
    assert project.state_json["video_variant_pipeline"]["active_index"] == 4
    assert project.state_json["rollout_retired_unauthorized_variant"] == {
        "stage_ids": [paused_stage.id],
        "variant_indices": [6],
        "authorized_variant_indices": [4, 5],
        "at": project.state_json["variant_rollout_release"]["released_at"],
    }


def test_service_rejects_release_while_project_is_active(db_session) -> None:
    project = _paused_project()
    project.status = "running"
    project.config_json = {"video_count": 50, "manual_paused": False}
    db_session.add(project)
    db_session.commit()

    with pytest.raises(APIError) as raised:
        configure_variant_rollout_gate(
            db_session,
            project,
            authorized_variant_indices=[41, 42],
            batch_id="unsafe",
            released_by_user_id=6,
        )

    assert raised.value.code == "CONTENT_ROLLOUT_GATE_PROJECT_NOT_PAUSED"


def test_service_rejects_release_while_stage_is_active(db_session) -> None:
    project = _paused_project()
    db_session.add(project)
    db_session.flush()
    db_session.add(HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=project.workspace_id,
        user_id=project.user_id,
        stage="DIRECTOR",
        attempt=1,
        status="running",
    ))
    db_session.commit()

    with pytest.raises(APIError) as raised:
        configure_variant_rollout_gate(
            db_session,
            project,
            authorized_variant_indices=[41, 42],
            batch_id="unsafe-stage",
            released_by_user_id=6,
        )

    assert raised.value.code == "CONTENT_ROLLOUT_GATE_ACTIVE_STAGE"


@pytest.mark.parametrize(
    ("config", "error"),
    [
        (
            {
                "variant_rollout_gate": {
                    "enabled": True,
                    "batch_id": "missing-list",
                    "authorized_variant_indices": [],
                }
            },
            "CONTENT_ROLLOUT_GATE_AUTHORIZED_VARIANTS_REQUIRED",
        ),
        (
            {
                "variant_rollout_gate": {
                    "enabled": True,
                    "batch_id": "out-of-range",
                    "authorized_variant_indices": [51],
                }
            },
            "CONTENT_ROLLOUT_GATE_VARIANT_OUT_OF_RANGE",
        ),
        (
            {
                "variant_rollout_gate": {
                    "enabled": True,
                    "authorized_variant_indices": [1],
                }
            },
            "CONTENT_ROLLOUT_GATE_BATCH_ID_REQUIRED",
        ),
    ],
)
def test_enabled_gate_fails_closed_when_manifest_is_invalid(
    config: dict,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        parse_variant_rollout_gate(config, target_count=50)
