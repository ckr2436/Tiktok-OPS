from types import SimpleNamespace

from app.services.hermes_agent.content_autonomy import (
    convergence_state,
    derive_director_loop_policy,
    derive_quality_recovery_limit,
    is_external_operator_blocker,
    quality_snapshot,
    recovery_strategy,
)
from app.services.hermes_agent.content_director_runtime import (
    _best_available_copy_artifact,
)
from app.services.hermes_agent.content_production_plan_runtime import (
    _best_available_production_plan,
)


def test_director_budget_scales_with_project_complexity():
    simple = derive_director_loop_policy(target_count=1)
    complex_project = derive_director_loop_policy(
        target_count=12,
        has_reference_transfer=True,
        has_locked_script=True,
        product_required=True,
    )
    assert complex_project["maximum_revisions"] > simple["maximum_revisions"]
    assert (
        complex_project["maximum_series_revisions"]
        > simple["maximum_series_revisions"]
    )


def test_quality_epoch_is_bounded_but_not_fixed_globally():
    assert derive_quality_recovery_limit(
        {"video_count": 1}, pause_reason="content_director_quality_pause"
    ) == 3
    assert derive_quality_recovery_limit(
        {"video_count": 20}, pause_reason="final_video_quality_gate"
    ) > 3


def test_convergence_switches_from_targeted_to_alternate_candidate():
    first = quality_snapshot(scores={"hook": 80}, issue_codes=["flat_hook"])
    same = quality_snapshot(scores={"hook": 80}, issue_codes=["flat_hook"])
    state = convergence_state(first, same)
    assert state["no_progress"] is True
    assert recovery_strategy(attempt_count=3, no_progress_cycles=2) == (
        "alternate_candidate"
    )


def test_only_external_authority_is_an_operator_boundary():
    assert is_external_operator_blocker(fault_class="CAPTCHA_REQUIRED") is True
    assert is_external_operator_blocker(fault_class="NETWORK") is False
    assert is_external_operator_blocker(fault_class="OUTPUT_CONTRACT") is False


def test_best_available_copy_requires_preflight_and_critical_pass():
    artifact = SimpleNamespace(artifact_sha256="a" * 64)
    criterion = SimpleNamespace(
        criterion_id="truth",
        minimum_score=100,
        blocking=True,
        priority="critical",
    )
    review = SimpleNamespace(
        artifact_sha256=artifact.artifact_sha256,
        preflight=SimpleNamespace(approved=True),
        verdict=SimpleNamespace(scores={"truth": 100, "style": 84}),
    )
    assert _best_available_copy_artifact(
        SimpleNamespace(copy_review_criteria=[criterion]),
        [artifact],
        [review],
    ) is artifact
    review.verdict.scores["truth"] = 99
    assert _best_available_copy_artifact(
        SimpleNamespace(copy_review_criteria=[criterion]),
        [artifact],
        [review],
    ) is None


def test_best_available_plan_never_relaxes_critical_truth():
    plan = SimpleNamespace(plan_sha256="b" * 64)
    criterion = SimpleNamespace(
        criterion_id="truth",
        minimum_score=100,
        blocking=True,
        priority="critical",
    )
    review = SimpleNamespace(
        plan_sha256=plan.plan_sha256,
        verdict=SimpleNamespace(scores={"truth": 100, "style": 82}),
    )
    assert _best_available_production_plan(
        [criterion], [plan], [review]
    ) is plan
