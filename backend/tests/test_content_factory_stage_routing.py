from types import SimpleNamespace

from app.services.hermes_agent.stage_routing import (
    is_local_worker_stage,
    stage_execution_backend,
)


def test_final_assets_split_runs_locally_without_browser() -> None:
    assert is_local_worker_stage("FINAL_ASSETS") is True
    assert stage_execution_backend("FINAL_ASSETS") == "local"


def test_forced_final_assets_rebuild_still_uses_browser() -> None:
    stage_input = {"force_chatgpt_rebuild": True}
    assert is_local_worker_stage("FINAL_ASSETS", stage_input) is False
    assert stage_execution_backend("FINAL_ASSETS", stage_input=stage_input) == "browser"


def test_api_route_takes_precedence_over_local_policy() -> None:
    assert stage_execution_backend(
        "FINAL_ASSETS",
        api_route="provider:model",
    ) == "api"


def test_visual_preview_without_api_requires_browser() -> None:
    assert is_local_worker_stage("VISUAL_PREVIEW") is False
    assert stage_execution_backend("VISUAL_PREVIEW") == "browser"


def test_signed_segment_compile_can_never_activate_api_or_browser() -> None:
    assert is_local_worker_stage("VIDEO_PROMPTS") is True
    assert stage_execution_backend("VIDEO_PROMPTS") == "local"
    assert stage_execution_backend(
        "VIDEO_PROMPTS",
        api_route="toapis:text",
        stage_input={"api_force_browser_fallback": True},
    ) == "local"


def test_explicit_browser_fallback_wins_over_available_api_route() -> None:
    assert stage_execution_backend(
        "CREATIVE",
        api_route="toapis:text",
        stage_input={"api_fallback_to_browser": True},
    ) == "browser"
    assert stage_execution_backend(
        "VISUAL_PREVIEW",
        api_route="bandianwa:gpt-image-2",
        stage_input={"visual_api_force_browser_fallback": True},
    ) == "browser"
    assert stage_execution_backend(
        "CREATIVE",
        api_route="toapis:text",
        stage_input={"api_force_browser_fallback": True},
    ) == "browser"


def test_visual_progress_checkpoint_restamps_current_policy() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    class CommitRecorder:
        commits = 0

        def commit(self) -> None:
            self.commits += 1

    db = CommitRecorder()
    stage = SimpleNamespace(input_json=None)
    inherited_input = {
        "self_heal_policy_version": 49,
        "hermes_learning_policy_version": "old-policy",
    }

    content_factory_tasks._commit_visual_api_progress(
        db,
        stage_row=stage,
        stage_input=inherited_input,
        api_state={"status": "submitted"},
    )

    assert db.commits == 1
    assert stage.input_json["self_heal_policy_version"] == (
        content_factory_tasks.SELF_HEAL_POLICY_VERSION
    )
    assert stage.input_json["hermes_learning_policy_version"] == (
        content_factory_tasks.HERMES_LEARNING_POLICY_VERSION
    )
    assert stage.input_json["visual_api"] == {"status": "submitted"}


def test_provider_exception_retry_restamps_current_policy() -> None:
    from app.tasks.hermes_agent import content_factory_tasks

    refreshed = content_factory_tasks._restamp_stage_runtime_policy({
        "self_heal_policy_version": 49,
        "hermes_learning_policy_version": "old-policy",
        "automatic_retry_count": 3,
    })

    assert refreshed["self_heal_policy_version"] == (
        content_factory_tasks.SELF_HEAL_POLICY_VERSION
    )
    assert refreshed["hermes_learning_policy_version"] == (
        content_factory_tasks.HERMES_LEARNING_POLICY_VERSION
    )
    assert refreshed["automatic_retry_count"] == 3
