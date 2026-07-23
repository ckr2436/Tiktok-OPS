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
from app.tasks.hermes_agent.content_factory_tasks import (
    _segment_durations_for_project,
    _video_model_policy,
)


def _project(model_id: str, *, duration: int) -> SimpleNamespace:
    return SimpleNamespace(
        product_name="",
        config_json={
            "video_model": model_id,
            "video_reference_limit": 9,
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
        reference_image_limit=9,
        allow_reference_video=False,
    )

    assert policy["id"] == "seedance_2_0_mini"
    assert policy["provider_key"] == "volcengine"
    assert policy["segment_duration_min"] == 1
    assert policy["segment_duration_max"] == 15
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
