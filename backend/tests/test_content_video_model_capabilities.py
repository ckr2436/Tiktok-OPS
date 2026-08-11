from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.hermes_agent.content_director import (
    production_segment_durations,
)
from app.services.hermes_agent.video_model_capabilities import (
    build_video_production_contract,
    get_video_model_capability,
    load_video_model_capability_manifest,
    resolve_project_variant_parallelism,
    resolve_video_model_policy,
)
from app.services.hermes_agent.video_duration_planner import (
    build_provider_duration_plan,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _segment_durations_for_project,
    _video_model_policy,
)
from app.services.hermes_agent import content_factory


def _project(model_id: str, *, duration: int) -> SimpleNamespace:
    return SimpleNamespace(
        product_name="",
        config_json={
            "video_model": model_id,
            "video_reference_limit": 10,
            "allow_reference_video": False,
            "product_required": False,
            "video_duration_min_seconds": duration,
            "video_duration_max_seconds": duration,
        },
        state_json={},
    )


def test_video_model_manifest_has_unique_declarative_capabilities():
    manifest = load_video_model_capability_manifest()

    assert {item.model_id for item in manifest.models} >= {
        "omni_flash",
        "seedance_2_0_mini",
    }
    assert (
        get_video_model_capability("seedance_2_0").model_id
        == "seedance_2_0_mini"
    )


def test_omni_contract_deterministically_requires_four_ten_second_segments():
    contract = build_video_production_contract(
        model_id="omni_flash",
        reference_image_limit=7,
        allow_reference_video=False,
    )

    assert production_segment_durations(contract, 40) == [
        10.0,
        10.0,
        10.0,
        10.0,
    ]
    with pytest.raises(ValueError, match="cannot compose total duration"):
        production_segment_durations(contract, 35)


def test_seedance_canonical_id_no_longer_falls_through_to_omni_policy():
    project = _project("seedance_2_0_mini", duration=40)

    policy = _video_model_policy(project)
    contract = build_video_production_contract(
        model_id="seedance_2_0_mini",
        reference_image_limit=10,
        allow_reference_video=False,
    )

    assert policy["id"] == "seedance_2_0_mini"
    assert policy["task_model"] == "seedance_2_0_mini"
    assert policy["provider_key"] == "volcengine"
    assert policy["segment_duration_min"] == 4
    assert policy["segment_duration_max"] == 15
    # The managed Doubao composer reserves five characters for its transport
    # command, leaving 495 for the Content Factory multimodal execution view.
    assert policy["provider_prompt_max_characters"] == 495
    assert policy["human_face_reference_mode"] == "stylized_animation_only"
    assert policy["allows_human_face_references"] is True
    assert policy["supports_native_spoken_audio"] is True
    assert policy["preferred_spoken_delivery"] == "provider_dialogue"
    # These constraints are part of the immutable Director contract as well
    # as the worker policy.  Otherwise a plan can be approved as photoreal
    # before the Seedance face-reference restriction is discovered.
    assert contract.provider_prompt_max_characters == 495
    assert contract.allows_human_face_references is True
    assert contract.human_face_reference_mode == "stylized_animation_only"
    assert contract.supports_native_spoken_audio is True
    assert contract.preferred_spoken_delivery == "provider_dialogue"
    assert any("stylized" in rule.lower() for rule in contract.provider_hard_rules)
    assert production_segment_durations(contract, 40) == [
        13.33,
        13.33,
        13.34,
    ]
    assert _segment_durations_for_project(project) == [14, 13, 13]


def test_policy_clamps_reference_limit_from_manifest_data():
    policy = resolve_video_model_policy(
        model_id="omni_flash",
        reference_image_limit=99,
        allow_reference_video=True,
        product_required=True,
    )
    assert policy["reference_limit"] == 7
    assert policy["reference_video_limit"] == 1
    assert any(
        "exact uploaded package image" in rule
        for rule in policy["hard_rules"]
    )
    assert policy["human_face_reference_mode"] == "allowed"
    assert policy["supports_native_spoken_audio"] is True
    assert policy["preferred_spoken_delivery"] == "provider_dialogue"


def test_benchmark_video_is_analysis_only_unless_video_to_video_is_explicit():
    project = _project("omni_flash", duration=40)
    project.config_json["allow_reference_video"] = True

    assert _video_model_policy(project)["reference_video_limit"] == 0

    project.config_json["video_generation_mode"] = "video_to_video"
    assert _video_model_policy(project)["reference_video_limit"] == 1


def _duration_route(
    key_id: int,
    provider_key: str,
    priority: int,
    durations: list[int],
) -> dict:
    return {
        "route_id": key_id + 100,
        "key_id": key_id,
        "provider_key": provider_key,
        "priority": priority,
        "allowed_segment_durations_seconds": durations,
        "segment_duration_minimum_seconds": min(durations),
        "segment_duration_maximum_seconds": max(durations),
        "reference_image_limit": 7,
    }


def test_duration_plan_preserves_fixed_ten_second_provider_fallback():
    plan = build_provider_duration_plan(
        model_id="omni_flash",
        minimum_seconds=45,
        maximum_seconds=65,
        preferred_seconds=55,
        routes=[
            _duration_route(1, "sub2api", 1, [4, 6, 8, 10]),
            _duration_route(2, "bandianwa", 10, [10]),
        ],
        reference_video_limit=0,
        routing_strategy="cross_provider_portable",
    )

    assert plan["planning_strategy"] == "cross_provider_portable"
    assert plan["normalized_seconds"] == 50
    assert plan["segment_durations_seconds"] == [10, 10, 10, 10, 10]
    assert [row["provider_key"] for row in plan["compatible_routes"]] == [
        "sub2api",
        "bandianwa",
    ]


def test_duration_plan_uses_sub2api_variable_segments_when_it_is_the_only_route():
    plan = build_provider_duration_plan(
        model_id="omni_flash",
        minimum_seconds=54,
        maximum_seconds=56,
        preferred_seconds=55,
        routes=[_duration_route(1, "sub2api", 1, [4, 6, 8, 10])],
        reference_video_limit=0,
        routing_strategy="creative_flexibility",
    )

    assert plan["planning_strategy"] == "creative_flexibility"
    assert plan["normalized_seconds"] == 54
    assert sum(plan["segment_durations_seconds"]) == 54
    assert set(plan["segment_durations_seconds"]) <= {4, 6, 8, 10}


def test_duration_plan_preserves_producer_confirmed_segment_topology():
    plan = build_provider_duration_plan(
        model_id="seedance_2_0_mini",
        minimum_seconds=20,
        maximum_seconds=20,
        preferred_seconds=20,
        preferred_segment_durations_seconds=[7, 7, 6],
        routes=[
            _duration_route(
                1,
                "doubao",
                1,
                [4, 5, 6, 7, 8, 9, 10],
            )
        ],
        reference_video_limit=0,
        routing_strategy="creative_flexibility",
    )

    assert plan["schema_version"] == "provider-model-duration-v4"
    assert plan["normalized_seconds"] == 20
    assert plan["preferred_segment_durations_seconds"] == [7, 7, 6]
    assert plan["segment_durations_seconds"] == [7, 7, 6]
    assert plan["production_contract"][
        "required_segment_durations_seconds"
    ] == [7.0, 7.0, 6.0]
    assert plan["normalization_reason"] == "preferred_segment_topology_supported"


def test_duration_plan_rejects_unsupported_producer_segment_topology():
    with pytest.raises(ValueError, match="not supported"):
        build_provider_duration_plan(
            model_id="omni_flash",
            minimum_seconds=20,
            maximum_seconds=20,
            preferred_seconds=20,
            preferred_segment_durations_seconds=[7, 7, 6],
            routes=[_duration_route(2, "bandianwa", 10, [10])],
            reference_video_limit=0,
            routing_strategy="creative_flexibility",
        )


def test_duration_plan_rejects_an_exact_unsupported_total():
    with pytest.raises(ValueError, match="cannot compose any total"):
        build_provider_duration_plan(
            model_id="omni_flash",
            minimum_seconds=55,
            maximum_seconds=55,
            preferred_seconds=55,
            routes=[
                _duration_route(1, "sub2api", 1, [4, 6, 8, 10]),
                _duration_route(2, "bandianwa", 10, [10]),
            ],
            reference_video_limit=0,
            routing_strategy="cross_provider_portable",
        )


def test_project_duration_revalidation_keeps_confirmed_segment_topology(
    monkeypatch,
):
    captured = {}

    def fake_plan(_db, **kwargs):
        captured.update(kwargs)
        return {
            "schema_version": "provider-model-duration-v4",
            "normalized_seconds": 20,
            "preferred_segment_durations_seconds": [7, 7, 6],
            "segment_durations_seconds": [7, 7, 6],
            "requested_range_seconds": {"min": 20, "max": 20},
            "planning_strategy": "creative_flexibility",
            "capability_sha256": "a" * 64,
            "production_contract": {
                "model_id": "seedance_2_0_mini",
                "segment_duration_minimum_seconds": 4,
                "segment_duration_maximum_seconds": 10,
                "allowed_segment_durations_seconds": [4, 5, 6, 7, 8, 9, 10],
                "required_segment_durations_seconds": [7, 7, 6],
                "reference_image_limit": 10,
                "reference_video_limit": 0,
            },
        }

    class FakeDb:
        def add(self, _row):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(
        "app.services.hermes_agent.video_duration_planner.plan_project_video_duration",
        fake_plan,
    )
    project = SimpleNamespace(
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_duration_min_seconds": 20,
            "video_duration_max_seconds": 20,
            "video_generation_mode": "image_to_video",
            "video_reference_limit": 10,
            "video_aspect_ratio": "9:16",
            "video_frame_mode": "reference",
            "video_resolution": "720p",
            "video_duration_strategy": "creative_flexibility",
            "preferred_segment_durations_seconds": [7, 7, 6],
        },
        state_json={},
    )

    plan = content_factory.ensure_project_video_duration_plan(
        FakeDb(),
        project,
        force=True,
    )

    assert captured["preferred_segment_durations_seconds"] == [7, 7, 6]
    assert plan["segment_durations_seconds"] == [7, 7, 6]
    assert project.config_json["preferred_segment_durations_seconds"] == [7, 7, 6]

def test_project_parallelism_comes_from_model_manifest_and_is_bounded():
    assert resolve_project_variant_parallelism(
        model_id="omni_flash",
        requested=None,
        target_count=50,
    ) == 2
    assert resolve_project_variant_parallelism(
        model_id="omni_flash",
        requested=99,
        target_count=50,
    ) == 4
    assert resolve_project_variant_parallelism(
        model_id="seedance_2_0_mini",
        requested=4,
        target_count=2,
    ) == 2
