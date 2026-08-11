import json
from datetime import datetime
from types import SimpleNamespace

from PIL import Image

from app.services.hermes_agent import content_factory_api
from app.services.hermes_agent import content_factory
from app.tasks.hermes_agent import content_factory_tasks


def _project(*, state_json=None):
    return SimpleNamespace(
        product_brief="Preserve the first 3 seconds visual hook.",
        config_json={
            "content_objective": "前3秒必须有强视觉钩子，不得变成平淡看手机。",
            "producer_intent_spec": {
                "intent_manifest": {
                    "manifest_sha256": "signed-hook-authority",
                    "requirements": [{
                        "requirement_id": "R-HOOK",
                        "kind": "reference_transfer",
                        "intent": "Preserve the opening's fast attention mechanism.",
                        "observable_checks": [
                            "The first three seconds contain a clear escalating hook."
                        ],
                        "must_not_reuse": ["source actors", "source UI"],
                    }],
                    "transformation_contract": {
                        "fidelity": "semantic_structure",
                        "transfer_mode": "adaptive",
                        "source_media_reuse": "forbidden",
                    }
                }
            },
        },
        state_json=state_json or {},
    )


def _visual_analysis():
    return {
        "status": "success",
        "policy_version": "test-policy",
        "opening_hook": {
            "ordered_states": [
                {"state_index": 1, "visible_state": "visual abnormality"},
                {"state_index": 2, "visible_state": "scale escalation"},
                {"state_index": 3, "visible_state": "overwhelmed reaction"},
            ],
            "attention_mechanisms": ["abnormality", "rapid scale change"],
            "contrast_and_escalation": "quiet to visually overwhelming",
            "minimum_distinct_visual_states": 3,
            # Models can recommend two composed anchors while separately
            # identifying three different visible states.  Production must
            # preserve the stricter pixel-evidence count, not flatten a state.
            "recommended_opening_reference_count": 2,
        },
        "story_progression": [
            {"order": 1, "narrative_job": "hook"},
            {"order": 2, "narrative_job": "bridge"},
        ],
        "must_transfer": ["three-state opening escalation"],
        "must_not_copy": ["source actors", "source UI"],
    }


def test_source_storyboard_analysis_calls_multimodal_and_returns_ordered_hook(
    monkeypatch,
    tmp_path,
):
    paths = []
    for index in range(2):
        path = tmp_path / f"sheet-{index + 1}.png"
        Image.new("RGB", (640, 960), (20 + index, 30, 40)).save(path)
        paths.append(str(path))
    captured = {}

    def fake_completion(_db, *, payload, request_id, **_kwargs):
        captured["payload"] = payload
        captured["request_id"] = request_id
        return {
            "choices": [{
                "message": {"content": json.dumps(_visual_analysis())},
            }]
        }

    monkeypatch.setattr(
        content_factory_api,
        "_routed_multimodal_completion",
        fake_completion,
    )
    result = content_factory_api.analyze_benchmark_storyboard_api(
        object(),
        contact_sheet_paths=paths,
        transcript="0.0-1.0s One more video.",
        project_requirement="Preserve the first 3 seconds visual hook.",
        transformation_contract={"transfer_mode": "adaptive"},
        execution_id="project:benchmark:1",
    )

    assert result["status"] == "success"
    assert len(result["opening_hook"]["ordered_states"]) == 3
    image_parts = [
        item
        for item in captured["payload"]["messages"][1]["content"]
        if item.get("type") == "image_url"
    ]
    assert len(image_parts) == 2
    assert captured["request_id"].startswith("cf-benchmark-vision:")


def test_signed_hook_contract_does_not_apply_a_fixed_reference_count():
    project = _project(state_json={
        "benchmark_video_analysis": {
            "status": "success",
            "duration_seconds": 18,
            "visual_semantic_analysis": _visual_analysis(),
        }
    })
    contract = content_factory_tasks._benchmark_opening_hook_contract(project)

    # The benchmark analysis is evidence for the independent multimodal
    # Critic, not a server-owned minimum image count.  A signed adaptive
    # transfer may express several ordered hook states in one composed board
    # or via motion inside the generated clip.
    assert contract["multimodal_opening_evidence"][
        "minimum_distinct_visual_states"
    ] == 3
    assert "required_reference_count" not in contract
    assert "no server-owned reference count is mandatory" in contract[
        "authority_rule"
    ]


def test_signed_hook_contract_preserves_exclusions_over_raw_suggestions():
    project = _project(state_json={
        "benchmark_video_analysis": {
            "status": "success",
            "duration_seconds": 18,
            "visual_semantic_analysis": _visual_analysis(),
        }
    })
    contract = content_factory_tasks._benchmark_opening_hook_contract(project)

    assert contract["signed_reference_transfer_requirements"][0][
        "must_not_reuse"
    ] == ["source actors", "source UI"]
    assert "must_not_reuse" in contract["authority_rule"]


def test_creative_visual_exhaustion_is_eligible_for_director_self_heal():
    project = SimpleNamespace(
        status="paused",
        config_json={"auto_run": True, "manual_paused": False},
        state_json={
            "pause_reason_code": "creative_visual_replan_exhausted",
            "automatic_quality_paused_at": "2026-07-29T10:00:00",
            "creative_visual_replan_exhausted": {
                "stage_id": 88,
                "variant_index": 1,
                "at": "2026-07-29T10:00:00",
            },
        },
    )

    decision = content_factory_tasks._automatic_quality_recovery_decision(
        project,
        now=datetime(2026, 7, 29, 10, 2, 0),
        generation="new-policy",
    )

    assert decision["eligible"] is True
    assert decision["due"] is True
    assert decision["variant_index"] == 1


def test_targeted_repair_keeps_shared_hook_panels_on_one_board():
    packet = {
        "video_aspect_ratio": "9:16",
        "visual_repair_failed_indices": [1, 2, 3, 5],
        "previous_outputs": {
            "MEDIA_DESIGN": {
                "visual_job_ticket": {
                    "reference_plan": [
                        {
                            "index": 1,
                            "reference_id": "ref.hook_wide",
                            "description": (
                                "Panel 1 of one shared three-panel opening storyboard "
                                "board with ref.hook_scale and ref.hook_reaction."
                            ),
                        },
                        {
                            "index": 2,
                            "reference_id": "ref.hook_scale",
                            "description": (
                                "Panel 2 of the same shared storyboard board as "
                                "ref.hook_wide and ref.hook_reaction."
                            ),
                        },
                        {
                            "index": 3,
                            "reference_id": "ref.hook_reaction",
                            "description": (
                                "Panel 3 of the same shared storyboard board as "
                                "ref.hook_wide and ref.hook_scale."
                            ),
                        },
                        {"index": 4, "reference_id": "ref.bridge"},
                        {"index": 5, "reference_id": "ref.product"},
                    ]
                }
            }
        },
    }

    specs = content_factory_api.visual_board_specs(packet)

    assert [spec["count"] for spec in specs] == [3, 1]
    assert [spec["global_start_index"] for spec in specs] == [1, 5]
    assert [spec["global_end_index"] for spec in specs] == [3, 5]


def test_targeted_repair_never_regenerates_passed_panel_from_shared_board():
    packet = {
        "video_aspect_ratio": "9:16",
        "visual_repair_failed_indices": [2, 3],
        "previous_outputs": {
            "MEDIA_DESIGN": {
                "visual_job_ticket": {
                    "reference_plan": [
                        {
                            "index": 1,
                            "reference_id": "ref.hook_wide",
                            "description": (
                                "Panel 1 of one shared storyboard board with "
                                "ref.hook_scale and ref.hook_reaction."
                            ),
                        },
                        {
                            "index": 2,
                            "reference_id": "ref.hook_scale",
                            "description": (
                                "Panel 2 of the same shared storyboard board as "
                                "ref.hook_wide and ref.hook_reaction."
                            ),
                        },
                        {
                            "index": 3,
                            "reference_id": "ref.hook_reaction",
                            "description": (
                                "Panel 3 of the same shared storyboard board as "
                                "ref.hook_wide and ref.hook_scale."
                            ),
                        },
                    ]
                }
            }
        },
    }

    specs = content_factory_api.visual_board_specs(packet)

    assert len(specs) == 1
    assert [row["index"] for row in specs[0]["plan"]] == [2, 3]


def test_full_failed_board_is_redrawn_as_one_board():
    packet = {
        "video_aspect_ratio": "9:16",
        "visual_reference_generation_mode": "board",
        "render_reference_images_individually": False,
        "visual_repair_failed_indices": [1, 2, 3, 4, 5],
        "previous_outputs": {
            "MEDIA_DESIGN": {
                "visual_job_ticket": {
                    "reference_plan": [
                        {"index": index, "reference_id": f"ref.{index}"}
                        for index in range(1, 6)
                    ]
                }
            }
        },
    }

    specs = content_factory_api.visual_board_specs(packet)

    assert len(specs) == 1
    assert specs[0]["count"] == 5
    assert specs[0]["global_start_index"] == 1
    assert specs[0]["global_end_index"] == 5


def test_full_board_repair_prompt_keeps_late_panel_evidence_and_time_cue():
    repair = (
        "Reference 1: add an unmistakable late-night bedside clock cue. "
        "Reference 2: make the phone glow oversized with dense abstract content. "
        + ("Preserve the same woman and bedroom. " * 20)
        + "Reference 5: show exactly two gummies and the phone face-down."
    )
    packet = {
        "video_aspect_ratio": "9:16",
        "product_required": True,
        "visual_repair_failed_indices": [1, 2, 3, 4, 5],
        "visual_repair_instruction": repair,
        "previous_outputs": {
            "MEDIA_DESIGN": {
                "visual_job_ticket": {
                    "reference_plan": [
                        {
                            "index": 1,
                            "description": "Wide bedroom with an abnormal late-night clock time cue.",
                        },
                        {"index": 2, "description": "Oversized abstract phone glow."},
                        {"index": 3, "description": "Tight startled reaction."},
                        {"index": 4, "description": "Warm product-free routine bridge."},
                        {
                            "index": 5,
                            "description": (
                                "MYUPONA package; exactly two gummies in her open palm; "
                                "phone already face-down beside the package."
                            ),
                            "requires_product_reference": True,
                        },
                    ]
                }
            }
        },
    }

    prompt, spec = content_factory_api.build_visual_api_prompt(packet)

    assert spec["count"] == 5
    assert "Reference 5: show exactly two gummies and the phone face-down." in prompt
    assert "diegetic late-night time cue is allowed" in prompt
    assert "EXACT LOOSE-GUMMY COUNT" not in prompt
    assert "phone already face-down beside the package" in prompt
    assert "Add no third gummy" not in prompt
    assert "No editorial text" not in prompt  # multi-panel wording is used
    assert "No panel labels, editorial text" in prompt


def test_new_director_generation_never_reuses_prior_production_plan():
    assert content_factory._production_plan_repair_matches_current_director_generation(
        latest_director_stage_id=2898,
        latest_successful_plan_stage_id=2889,
    ) is False
    assert content_factory._production_plan_repair_matches_current_director_generation(
        latest_director_stage_id=2885,
        latest_successful_plan_stage_id=2889,
    ) is True
