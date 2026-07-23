from __future__ import annotations

import asyncio
import inspect
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image

from app.services.bandianwa.client import BandianwaApiError, BandianwaImageClient, extract_image_outputs
from app.services.bandianwa import client as bandianwa_client_module
from app.services.bandianwa.tasks import reset_bandianwa_task_for_retry
from app.data.models.hermes_agent import HermesContentFactoryAsset, HermesContentFactoryProject
from app.services.hermes_agent import content_factory_api
from app.services.hermes_agent import content_factory as content_factory_service
from app.services.hermes_agent.content_factory_api import (
    ContentFactoryApiError,
    benchmark_imitation_mode,
    build_text_api_request,
    build_visual_api_prompt,
    build_visual_api_prompts,
    execute_text_stage_api,
    _image_prompt_without_product_triggers,
    _single_reference_repair_instruction,
    minimal_stage_context,
    visual_generation_reference_paths,
    visual_board_spec,
    visual_board_specs,
    visual_reference_mentions_product,
    visual_reference_requires_product,
    visual_reference_static_state,
)
from app.services.hermes_agent.direct_browser import (
    _chatgpt_generation_failed,
    _collect_inflight_visual_from_project_tabs,
    _execute_visual_boards_sequentially,
    _expected_visual_count,
    _generated_images_for_request,
    _insert_prompt_text_stably,
    _normalized_composer_text,
    _packet_for_prompt,
    _product_visual_lock,
    _prompt_text_chunks,
    _prompt_submission_marker,
    _prompt_text_submitted,
    _state_has_execution_request,
    _visual_board_execution_marker,
    _visual_browser_board_instruction,
    _visual_source_instruction,
)
from app.services.hermes_agent.content_factory import (
    _clear_creative_visual_recovery_state,
    _clear_project_pause_metadata,
    _resumable_visual_api_checkpoint,
    _retired_locked_slot_can_rebind,
    _select_visual_variant_api_route,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _asset_role,
    _acquire_project_execution_guard,
    _atomic_write_text_if_changed,
    _assert_adult_only_product_story,
    _assert_visual_provider_submission_current,
    _api_browser_fallback_input,
    _bandianwa_image_bytes,
    _browser_stage_output_path,
    _build_prompt,
    _creative_review_false_negative,
    _creative_review_has_hard_rejection,
    _creative_review_is_generated_package_only_rejection,
    _creative_review_is_explicit_clean_approval,
    _compact_provider_segment_prompt,
    _compact_packet_for_chatgpt,
    _creative_copy_contract,
    _creative_conversion_requirement_excerpt,
    _prepare_stage_packet_for_execution,
    _creative_spoken_copy_budget_contract,
    _creative_reference_text_dependencies,
    _creative_review_rejection_policy,
    _creative_review_asset_cleanup_allowed,
    _creative_review_reference_asset_status,
    _visual_board_reference_meta,
    _split_visual_board_native_files,
    _omni_reference_prompt,
    _creative_review_failed_reference_indices,
    _targeted_visual_repair_brief,
    _expand_failed_visual_reference_dependencies,
    _ensure_reference_plan,
    _ensure_reference_plan_segment_coverage,
    _assert_project_creative_copy_contract,
    _normalize_creative_video_blueprint,
    _normalized_dialogue_lines,
    _ordered_native_reference_assets,
    _sanitize_product_visual_action,
    _normalize_segment_timeline,
    _persist_completed_stage_capture,
    _per_video_editor_guidance_markdown,
    _provider_safe_base_prompt,
    _retry_failed_video_segments,
    _release_project_execution_guard,
    _retire_superseded_stage_row,
    _project_uses_product,
    _quarantine_generated_visual_capture,
    _recover_prior_stage_attempt_response,
    _split_panel_quality_ok,
    _semantic_api_exhaustion_decision,
    _semantic_api_retry_plan,
    _is_semantic_text_payload_failure,
    _is_text_api_output_validation_failure,
    _should_seed_generated_continuity_anchor,
    _repair_continuity_anchor_paths,
    _text_api_transport_budget_exhausted,
    _text_api_transport_retry_plan,
    _stage_retry_delay,
    _short_overlay_copy,
    _stage_replay_context_digest,
    _canonicalize_video_prompt_result,
    _video_segment_prompt_text,
    _video_wait_pause_mode,
    _visual_plan_needs_product_reference,
    _visual_grid_repair_budget,
    _visual_grid_repair_instruction,
    _visual_grid_repair_budget_exhausted,
    _visual_grid_failure_needs_browser_fallback,
    _visual_api_account_quota_exhausted,
    _prepare_visual_api_provider_failover,
    _prepare_visual_image_model_failover,
    _visual_api_provider_retry_delay,
    _visual_provider_task_reusable_after_error,
    _visual_api_final_provider_budget_exhausted,
    _visual_continuity_reference_instruction,
    _visual_grid_repair_instruction,
    _visual_expected_panel_count,
    _generate_individual_visual_references_via_api,
    _visual_api_model_for_packet,
    _SupersededVisualProviderSubmission,
    _visual_board_expected_counts,
    _is_recoverable_chatgpt_response_error,
    _is_current_stage_delivery,
    _is_visual_empty_response_error,
)
from app.tasks.hermes_agent import content_factory_tasks
from app.tasks.bandianwa.video_tasks import (
    _claim_poll_owner,
    _content_factory_task_authority,
    _content_project_drains_submitted_video,
    _poll_heartbeat_is_recent,
    _quarantine_non_authoritative_content_task,
)


def _packet() -> dict:
    # Keep the request fixture self-contained.  API request construction reads
    # the referenced files to build data URLs, so relying on another test to
    # leave /tmp files behind made this suite order-dependent.
    for path, color in (
        ("/tmp/product.png", (64, 32, 160)),
        ("/tmp/character.png", (220, 180, 120)),
    ):
        Image.new("RGB", (2, 2), color).save(path, format="PNG")
    return {
        "project_id": "cf_test",
        "execution_id": "cf_test:12:1:v06",
        "product": "MYUPONA SLEEP EASY GUMMIES",
        "market": "US",
        "brief": "Fast feed-native video. End with the confirmed offer.",
        "project_requirements": "Fast feed-native video. Say the offer and yellow-cart CTA naturally.",
        "user_instruction": "Continue variant six.",
        "current_stage": "VISUAL_PREVIEW",
        "browser_assets": [
            {"name": "product.png", "role": "product_visual"},
            {"name": "character.png", "role": "character_reference"},
        ],
        "browser_asset_paths": ["/tmp/product.png", "/tmp/character.png"],
        "browser_cdp_url": "http://127.0.0.1:9222",
        "browser_output_path": "/tmp/out",
        "project_assets": [{"name": f"unrelated-{index}.pdf"} for index in range(40)],
        "project_state": {"large_runtime_history": "x" * 20000},
        "previous_outputs": {
            "FACTS": {"product_passport": {"display_name": "Sleep Ease Gummies"}},
            "MEDIA_DESIGN": {
                "selected_concept": {"title": "Offer interruption"},
                "visual_job_ticket": {
                    "reference_image_count": 7,
                    "reference_plan": [
                        {
                            "index": index,
                            "segment": 1 if index <= 4 else 2,
                            "description": f"Ordered action panel {index}",
                            "roles": ["character_anchor", "action_anchor"],
                        }
                        for index in range(1, 8)
                    ],
                },
            },
            "FINAL_ASSETS": {
                "reference_images": [
                    {"index": index, "roles": ["action_anchor"]}
                    for index in range(1, 8)
                ]
            },
            "UNRELATED": {"history": "z" * 20000},
        },
        "video_variant_index": 6,
        "video_variant_total": 20,
        "video_segment_durations_seconds": [10, 10],
        "video_segment_count": 2,
        "video_model": "omni_flash",
        "video_reference_limit": 7,
        "video_resolution": "720p",
        "video_language_label": "English (US)",
        "marketing_authorization": {
            "allow_promotional_cta": True,
            "confirmed_promotions": "$7.99",
            "promotion_cta": "Click the yellow cart below to buy.",
        },
        "required_result_fields": ["preview_canvas", "asset_manifest", "self_check"],
        "required_next_stage": "CREATIVE_REVIEW",
    }


def test_visual_continuity_reference_locks_identity_without_cloning_composition() -> None:
    instruction = _visual_continuity_reference_instruction()

    assert "preserve each adult's identity" in instruction
    assert "nearest approved preceding continuity frame" in instruction
    assert "current segment's written location is authoritative" in instruction
    assert "do not copy the input room" in instruction
    assert "genuinely new shot" in instruction
    assert "DO NOT copy" in instruction
    for forbidden_clone in (
        "pose",
        "character placement",
        "camera angle",
        "framing",
        "departure action",
    ):
        assert forbidden_clone in instruction
    assert "current segment's action and spatial blocking are authoritative" in instruction


def test_targeted_visual_repair_uses_nearest_approved_preceding_scene(
    tmp_path,
) -> None:
    segment_one = tmp_path / "shop.png"
    segment_three = tmp_path / "apartment.png"
    segment_one.write_bytes(b"shop")
    segment_three.write_bytes(b"apartment")

    anchors = _repair_continuity_anchor_paths(
        {1: segment_one, 3: segment_three},
        {4},
    )

    assert anchors == {4: segment_three}


def test_superseded_stage_row_closes_its_running_lease() -> None:
    db = MagicMock()
    stage = SimpleNamespace(
        status="running",
        error_message=None,
        completed_at=None,
    )

    assert _retire_superseded_stage_row(
        db,
        stage,
        reason="Superseded by stage 99.",
    ) is True
    assert stage.status == "failed"
    assert stage.error_message == "Superseded by stage 99."
    assert stage.completed_at is not None
    db.add.assert_called_once_with(stage)
    db.commit.assert_called_once_with()


def test_visual_prompt_uses_exact_creative_count_and_is_compact():
    prompt, spec = build_visual_api_prompt(_packet())
    assert spec["count"] == 7
    assert spec["columns"] == 4
    assert spec["rows"] == 2
    assert spec["row_columns"] == [4, 3]
    assert spec["size"] == "1024x1024"
    assert "ENTIRE output canvas must be a square 1:1 board at 1024x1024" in prompt
    assert "separate vertical 9:16 panels" in prompt
    assert "same width and height as a first-row panel" in prompt
    assert "Never stretch the lower panels to fill the row" in prompt
    assert "Ordered action panel 7" in prompt
    assert "This entire board is product-free" in prompt
    assert "product.png" not in prompt
    assert "character.png" in prompt
    assert prompt.count("ONE STATIC STILL IMAGE") == 7
    assert "Each numbered image is one complete static composition" in prompt
    assert "before/after views, montage, diagram" in prompt
    assert "large_runtime_history" not in prompt
    assert "UNRELATED" not in prompt
    assert len(prompt) < 6000








def test_dialogue_normalizer_accepts_provider_copy_alias():
    assert _normalized_dialogue_lines([{
        "speaker_id": "narrator",
        "copy": "This is a complete sentence.",
    }]) == [{
        "speaker_id": "narrator",
        "speaker": "",
        "line": "This is a complete sentence.",
    }]






def test_creative_review_uses_individual_native_contract_for_short_product_video(monkeypatch):
    packet = _packet()
    packet.update({
        "product_required": True,
        "current_stage": "CREATIVE_REVIEW",
        "render_reference_images_individually": True,
        "video_segment_durations_seconds": [10, 10, 10, 10],
        "video_segment_count": 4,
    })
    monkeypatch.setattr(content_factory_tasks, "_stage_packet", lambda *_args: packet)
    monkeypatch.setattr(content_factory_tasks, "_compact_packet_for_chatgpt", lambda value, _stage: value)

    prompt = _build_prompt(None, SimpleNamespace(), SimpleNamespace(stage="CREATIVE_REVIEW"))

    assert "INDIVIDUAL NATIVE REFERENCE REVIEW CONTRACT" in prompt
    assert "NOT a storyboard board, collage, grid, split screen, or panel set" in prompt
    assert "do not require gutters, separators" in prompt
    assert "request native individual replacement frame(s)" in prompt
    assert "small phone text, a sender name, notification/app settings" in prompt
    assert "invisible setting be proven from pixels" in prompt
    assert "intermediate telescope POV" in prompt
    assert "controllable input anchors for a video model" in prompt
    assert "immediately animatable starting or adjacent pose" in prompt
    assert "Do not require the exact final hand position" in prompt
    assert "movable secondary prop changes position or is absent" in prompt




def test_creative_review_contract_accepts_separate_product_anchor_in_api_and_browser(monkeypatch):
    packet = _packet()
    packet.update({
        "product_required": True,
        "current_stage": "CREATIVE_REVIEW",
        "render_reference_images_individually": True,
        "required_result_fields": [
            "creative_review",
            "approved_for_split",
            "reference_image_count",
            "repair_brief",
        ],
        "required_next_stage": "FINAL_ASSETS",
    })
    packet["browser_assets"] = [
        *[
            {
                "name": f"reference-{index:02d}.png",
                "kind": "visual_preview",
                "role": "visual_preview",
                "mime_type": "image/png",
            }
            for index in range(1, 5)
        ],
        {
            "name": "product.png",
            "kind": "source",
            "role": "product_visual",
            "mime_type": "image/png",
        },
    ]
    packet["browser_asset_paths"] = [
        *[f"/tmp/reference-{index:02d}.png" for index in range(1, 5)],
        "/tmp/product.png",
    ]

    _system, api_prompt = build_text_api_request(packet, "CREATIVE_REVIEW")
    assert "Count files, not visual regions inside an image" in api_prompt
    assert "multiple adults, furniture, windows, shadows" in api_prompt
    assert "must place the product naturally in the scripted scene" in api_prompt
    assert "Minor micro-label illegibility" in api_prompt
    assert "pasted source rectangle" in api_prompt

    monkeypatch.setattr(content_factory_tasks, "_stage_packet", lambda *_args: packet)
    monkeypatch.setattr(content_factory_tasks, "_compact_packet_for_chatgpt", lambda value, _stage: value)
    browser_prompt = _build_prompt(None, SimpleNamespace(), SimpleNamespace(stage="CREATIVE_REVIEW"))
    assert "naturally rendered product matching the separately attached product_visual authority" in browser_prompt
    assert "generated product identity, natural interaction" in browser_prompt
    assert "request native individual replacement frame(s)" in browser_prompt

    context = minimal_stage_context(packet, "CREATIVE_REVIEW")
    assert [item["name"] for item in context["visual_assets"]] == [
        "reference-01.png",
        "reference-02.png",
        "reference-03.png",
        "reference-04.png",
        "product.png",
    ]


def test_creative_review_requires_pixel_grounded_checks_and_full_terminal_plan():
    packet = _packet()
    packet.update({
        "product_required": True,
        "current_stage": "CREATIVE_REVIEW",
        "render_reference_images_individually": True,
    })
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 2,
        "reference_plan": [
            {
                "index": 1,
                "segment": 1,
                "description": "Mara finds Jo asleep in the hallway chair.",
                "roles": ["character_anchor", "action_anchor"],
            },
            {
                "index": 2,
                "segment": 2,
                "description": (
                    "Mara's closed book rests on the narrow hallway console beside the sealed MYUPONA bottle. "
                    "Mara approaches the bedroom. Jo opens the door; they share a small, quiet smile."
                ),
                "roles": ["scene_anchor", "action_anchor"],
            },
        ],
    }

    _system, prompt = build_text_api_request(packet, "CREATIVE_REVIEW")
    context = minimal_stage_context(packet, "CREATIVE_REVIEW")

    assert len(context["reference_plan"]) == 2
    assert "share a small, quiet smile" in context["reference_plan"][1]["description"]
    assert context["reference_plan"][1]["single_frame_terminal_state"] == (
        "Mara's closed book rests on the narrow hallway console beside the sealed MYUPONA bottle. "
        "Mara stands at the bedroom. Jo stands beside the open door; they share a small, quiet smile"
    )
    assert context["reference_plan"][1]["requires_product_reference"] is True
    assert "reference_checks must contain exactly one ordered object" in prompt
    assert "Describe only pixels visibly present" in prompt
    assert "Uncertain is a rejection" in prompt
    assert "an earlier or intermediate action does not satisfy" in prompt
    assert "generic surrounding surface" in prompt
    assert "controllable input anchors for a video model" in prompt
    assert "immediately animatable starting or adjacent pose" in prompt
    assert "a paper to be fully hidden inside a pocket" in prompt
    assert "non-blocking degree/state differences must not be placed" in prompt


def test_review_terminal_state_ignores_intermediate_office_reset_actions():
    office_reset = (
        "Mara stands outside the bedroom, then walks to the home-office door. "
        "She closes her laptop, places her phone face-down in a drawer, switches off the office light, "
        "and returns to the hallway with a deliberate breath."
    )

    expected_terminal_state = (
        "She has closed her laptop, has placed her phone face-down in a drawer, "
        "the office light is visibly off; She stands in the hallway with a deliberate breath"
    )
    assert content_factory_api._single_frame_terminal_state_hint(office_reset) == expected_terminal_state

    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 3,
            "description": office_reset,
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }
    _system, prompt = build_text_api_request(packet, "CREATIVE_REVIEW")
    context = minimal_stage_context(packet, "CREATIVE_REVIEW")

    assert context["reference_plan"][0]["single_frame_terminal_state"] == expected_terminal_state
    assert "Earlier or intermediate actions are context" in prompt
    assert "their absence must never be reported as a mismatch" in prompt


def test_creative_review_does_not_make_model_invented_wardrobe_pixel_authority():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["previous_outputs"]["MEDIA_DESIGN"]["continuity_rules"] = {
        "characters": [{
            "name": "Mara",
            "hair": "dark curly shoulder-length hair",
            "wardrobe": "charcoal cardigan and cream blouse",
        }],
    }

    _system, prompt = build_text_api_request(packet, "CREATIVE_REVIEW")

    assert "hard character appearance authority comes only from an attached" in prompt.lower()
    assert "creative-model-invented hair texture, hair shade" in prompt.lower()
    assert "do not reject harmless invented appearance changes" in prompt.lower()
    assert "material identity drift between files" in prompt.lower()


def test_pixel_grounded_review_gate_ignores_only_model_invented_wardrobe_details():
    packet = _packet()
    packet["browser_assets"] = [{"name": "product.png", "role": "product_visual"}]
    packet["browser_asset_paths"] = ["/tmp/product.png"]
    packet["project_requirements"] = "Use one consistent fictional adult in an animated story."
    packet["user_instruction"] = "Continue this variant."
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "Mara reads the crossed-out shift notice.",
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Rejected. The sweater is green instead of navy.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "repair_brief": "Regenerate with the exact navy sweater.",
            "rewritten_script": "A vision reviewer must never become a copy author.",
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "mismatch",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": "One consistent fictional adult woman is visible.",
                "observed_terminal_state": "Mara reads the crossed-out shift notice.",
                "observed_gaze_expression": "She looks at the notice.",
                "observed_placement_surface": "Not required.",
                "observed_facts": ["The planned adult, hallway, and notice are visible."],
                "missing_or_wrong_facts": [
                    "The wardrobe is a green sweater rather than the creative-model-invented navy cardigan."
                ],
            }],
        },
        "issues": ["All frames use the wrong creative-model-invented wardrobe color."],
        "repair_brief": "Regenerate with the exact navy sweater.",
    }

    approved = content_factory_api._apply_creative_review_reference_gate(envelope, packet)

    assert approved["result"]["approved_for_split"] is True
    assert approved["result"]["reference_checks"][0]["character_scene_verdict"] == "match"
    assert approved["result"]["reference_checks"][0]["missing_or_wrong_facts"] == []
    assert approved["issues"] == []
    assert approved["evidence"]["pixel_grounded_reference_gate_passed"] is True
    assert approved["evidence"]["ignored_harmless_invented_appearance_differences"][0]["index"] == 1
    assert "rewritten_script" not in approved["result"]
    assert approved["evidence"]["visual_acceptance_discarded_non_visual_fields"] == [
        "rewritten_script"
    ]


def test_pixel_grounded_review_gate_removes_dependent_false_verdicts_from_invented_appearance():
    packet = _packet()
    packet["browser_assets"] = [{"name": "product.png", "role": "product_visual"}]
    packet["browser_asset_paths"] = ["/tmp/product.png"]
    packet["project_requirements"] = "Use one fictional adult in an animated story."
    packet["user_instruction"] = "Continue this variant."
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "Mara sits silently at the dinner table.",
            "roles": ["character_anchor", "scene_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Rejected only because Mara's invented styling changed.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "repair_brief": "Regenerate the exact creative-model-invented styling.",
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "mismatch",
                "terminal_action_verdict": "mismatch",
                "continuity_verdict": "mismatch",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": "One fictional adult woman is seated at the table.",
                "observed_terminal_state": "The woman sits silently at the dinner table.",
                "observed_gaze_expression": "Her gaze is lowered.",
                "observed_placement_surface": "Not required.",
                "observed_facts": ["The adult and planned dinner table are visible."],
                "missing_or_wrong_facts": [
                    "The visible adult has silver-streaked curly hair and a green shirt rather than the creative-model-invented dark bob and charcoal cardigan."
                ],
            }],
        },
        "issues": [],
        "repair_brief": "Regenerate the exact creative-model-invented styling.",
    }

    approved = content_factory_api._apply_creative_review_reference_gate(envelope, packet)
    check = approved["result"]["reference_checks"][0]

    assert approved["result"]["approved_for_split"] is True
    assert check["character_scene_verdict"] == "match"
    assert check["continuity_verdict"] == "match"
    assert check["terminal_action_verdict"] == "match"
    assert check["missing_or_wrong_facts"] == []


def test_pixel_grounded_review_gate_keeps_authoritative_character_wardrobe_mismatch():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "Mara reads the crossed-out shift notice.",
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Rejected. The uploaded character wardrobe does not match.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "mismatch",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": "One adult woman is visible.",
                "observed_terminal_state": "Mara reads the crossed-out shift notice.",
                "observed_gaze_expression": "She looks at the notice.",
                "observed_placement_surface": "Not required.",
                "observed_facts": ["The hallway and notice are visible."],
                "missing_or_wrong_facts": [
                    "The wardrobe does not match the uploaded character reference."
                ],
            }],
        },
        "issues": [],
    }

    rejected = content_factory_api._apply_creative_review_reference_gate(envelope, packet)

    assert rejected["result"]["approved_for_split"] is False
    assert rejected["evidence"]["pixel_grounded_reference_gate_passed"] is False


def test_pixel_grounded_review_gate_ignores_unobservable_exhale_only():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 3,
            "description": (
                "Mara sets her phone face-down, lowers the warm balcony light, "
                "and exhales beside the telescope."
            ),
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Rejected only because the exhale is not visible.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "uncertain",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": "One consistent fictional adult woman is visible.",
                "observed_terminal_state": (
                    "The phone is face-down, the chart is flat, and the warm light is lowered."
                ),
                "observed_gaze_expression": "She looks calmly toward the telescope.",
                "observed_placement_surface": "Not required.",
                "observed_facts": ["The required visible objects and lighting state are present."],
                "missing_or_wrong_facts": [
                    "The required exhale is not visibly confirmable in the still image."
                ],
            }],
        },
        "issues": ["The exhale is not visibly confirmable."],
    }

    approved = content_factory_api._apply_creative_review_reference_gate(envelope, packet)

    assert approved["result"]["approved_for_split"] is True
    assert approved["result"]["reference_checks"][0]["terminal_action_verdict"] == "match"
    assert approved["result"]["reference_checks"][0]["missing_or_wrong_facts"] == []
    assert approved["evidence"]["ignored_unobservable_still_facts"][0]["index"] == 1


def test_pixel_grounded_review_gate_keeps_real_action_missing_with_exhale():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 4,
            "description": "Mara points to the constellation and exhales.",
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Rejected because the pointing action and exhale are absent.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "mismatch",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": "One consistent fictional adult woman is visible.",
                "observed_terminal_state": "Mara stands with both hands lowered.",
                "observed_gaze_expression": "She looks toward the sky.",
                "observed_placement_surface": "Not required.",
                "observed_facts": ["The adult and balcony are visible."],
                "missing_or_wrong_facts": [
                    "Mara is not visibly pointing toward a constellation.",
                    "The required exhale is not visibly confirmable in the still image.",
                ],
            }],
        },
        "issues": [],
    }

    rejected = content_factory_api._apply_creative_review_reference_gate(envelope, packet)

    assert rejected["result"]["approved_for_split"] is False
    assert rejected["result"]["reference_checks"][0]["terminal_action_verdict"] == "mismatch"
    assert "ignored_unobservable_still_facts" not in rejected["evidence"]


def test_pixel_grounded_review_gate_retries_summary_only_without_redrawing_images():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 4,
            "description": (
                "The sealed MYUPONA bottle belongs on the narrow console. "
                "Mara and Jo meet at the doorway and share a small, quiet smile."
            ),
            "roles": ["scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Approved. The final doorway reconnection occurs.",
            "approved_for_split": True,
            "reference_image_count": 1,
        },
        "issues": [],
    }

    with pytest.raises(
        ContentFactoryApiError,
        match="CREATIVE_REVIEW reference_checks contract incomplete",
    ):
        content_factory_api._apply_creative_review_reference_gate(envelope, packet)


def test_pixel_grounded_review_gate_retries_string_evidence_without_redrawing_images():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "Ava rests her hand beside the closed handoff binder.",
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Every visible requirement matches.",
            "approved_for_split": True,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": (
                    "Ava has short natural curls, a rust overshirt, and a "
                    "charcoal work shirt."
                ),
                "observed_terminal_state": (
                    "Ava's hand rests beside the closed handoff binder."
                ),
                "observed_gaze_expression": "Ava looks toward the binder.",
                "observed_placement_surface": "Not required.",
                # This is the exact production failure: the reviewer returned
                # prose instead of the required evidence array. It must trigger
                # a cheap text re-review, never a paid image regeneration.
                "observed_facts": (
                    "The adult, work station, and closed binder are visible."
                ),
                "missing_or_wrong_facts": [],
            }],
        },
        "issues": [],
    }

    with pytest.raises(
        ContentFactoryApiError,
        match=r"reference 1 observed_facts must be a JSON list",
    ):
        content_factory_api._apply_creative_review_reference_gate(
            envelope,
            packet,
        )


def test_pixel_grounded_review_gate_requires_visible_smile_and_console():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 4,
            "description": (
                "The sealed MYUPONA bottle belongs on the narrow console. "
                "Mara and Jo meet at the doorway and share a small, quiet smile."
            ),
            "roles": ["scene_anchor", "action_anchor"],
        }],
    }
    base_check = {
        "index": 1,
        "character_scene_verdict": "match",
        "terminal_action_verdict": "match",
        "continuity_verdict": "match",
        "emotional_beat_verdict": "uncertain",
        "placement_surface_verdict": "uncertain",
        "observed_characters": "Mara is in the hall and Jo is inside the open door.",
        "observed_terminal_state": "Both adults stand apart near the doorway.",
        "observed_gaze_expression": "Mara looks down; Jo has a neutral expression.",
        "observed_placement_surface": "",
        "observed_facts": ["Two adults are visible near an open door."],
        "missing_or_wrong_facts": ["No mutual smile; the narrow console is not visible."],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "The doorway scene is present.",
            "approved_for_split": True,
            "reference_image_count": 1,
            "reference_checks": [base_check],
        },
        "issues": [],
    }

    rejected = content_factory_api._apply_creative_review_reference_gate(envelope, packet)
    assert rejected["result"]["approved_for_split"] is False
    assert any(
        "emotional beat" in failure
        for failure in rejected["evidence"]["pixel_grounded_reference_gate_failures"]
    )
    assert any(
        "placement surface" in failure
        for failure in rejected["evidence"]["pixel_grounded_reference_gate_failures"]
    )

    approved_check = {
        **base_check,
        "emotional_beat_verdict": "match",
        "placement_surface_verdict": "match",
        "observed_gaze_expression": "Mara and Jo look at each other with small, quiet smiles.",
        "observed_placement_surface": "The narrow hallway console is visible and clear beside the closed book.",
        "observed_facts": [
            "Both adults are at the bedroom doorway.",
            "Their gaze meets and both faces show a small smile.",
            "The narrow console has clear space beside the book.",
        ],
        "missing_or_wrong_facts": [],
    }
    approved = content_factory_api._apply_creative_review_reference_gate(
        {
            "status": "PASS",
            "result": {
                "creative_review": "Every visible contract matches.",
                "approved_for_split": True,
                "reference_image_count": 1,
                "reference_checks": [approved_check],
            },
            "issues": [],
        },
        packet,
    )
    assert approved["result"]["approved_for_split"] is True
    assert approved["evidence"]["pixel_grounded_reference_gate_passed"] is True


def test_pixel_grounded_review_gate_canonicalizes_required_surface_for_partial_repair():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 4,
            "description": (
                "The adult stands beside the entryway bench while the separate "
                "MYUPONA product anchor is reserved for the final video."
            ),
            "roles": ["scene_anchor", "action_anchor"],
            "requires_product_reference": True,
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "The scene is visible, but the product surface was not graded.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": ["One adult woman"],
                "observed_terminal_state": "The woman stands beside the entryway bench.",
                "observed_gaze_expression": "Calm expression.",
                "observed_placement_surface": "The entryway bench is visible.",
                "observed_facts": ["One adult woman and the entryway bench are visible."],
                "missing_or_wrong_facts": [],
            }],
        },
        "issues": [],
    }

    rejected = content_factory_api._apply_creative_review_reference_gate(
        envelope,
        packet,
    )

    assert rejected["result"]["approved_for_split"] is False
    assert (
        rejected["result"]["reference_checks"][0]["placement_surface_verdict"]
        == "mismatch"
    )
    failed, observed = _creative_review_failed_reference_indices(
        rejected["result"]
    )
    assert observed == {1}
    assert failed == {1}


def test_pixel_grounded_review_gate_rejects_legacy_source_reuse_row():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "source": "directed_production_plan",
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 4,
            "description": "Authoritative uploaded MYUPONA product source.",
            "roles": ["action_anchor"],
            "generation_mode": "reuse_source",
            "source_asset_refs": ["asset:2754"],
            "requires_product_reference": True,
        }],
    }
    with pytest.raises(
        ValueError,
        match="CONTENT_PRODUCTION_PLAN_LEGACY_SOURCE_REUSE_UNSUPPORTED",
    ):
        visual_board_specs(packet)


def test_pixel_grounded_review_gate_recomputes_inconsistent_top_level_approval():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "One adult closes a notebook at the kitchen table.",
            "roles": ["scene_anchor", "action_anchor"],
        }],
    }
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": "Every visible row passes.",
            "approved_for_split": False,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": ["One adult"],
                "observed_terminal_state": "The adult has closed the notebook.",
                "observed_gaze_expression": "Neutral expression.",
                "observed_placement_surface": "Not required.",
                "observed_facts": ["One adult and one closed notebook are visible."],
                "missing_or_wrong_facts": [],
            }],
        },
        "issues": [{
            "severity": "blocker",
            "code": "PIXEL_GROUNDED_REFERENCE_MISMATCH",
            "issue": "stale reviewer summary",
        }],
    }

    approved = content_factory_api._apply_creative_review_reference_gate(
        envelope,
        packet,
    )

    assert approved["result"]["approved_for_split"] is True
    assert approved["result"]["repair_brief"] is None
    assert approved["issues"] == []
    assert (
        approved["evidence"]["normalized_inconsistent_top_level_review_decision"]
        is True
    )


def test_creative_review_sends_images_at_high_detail(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setenv("HERMES_CREATIVE_REVIEW_MODEL", "visual-review-role")
    monkeypatch.setenv("HERMES_CREATIVE_REVIEW_WORKLOAD", "content_visual_inspector")
    monkeypatch.setattr(content_factory_api, "_data_url", lambda path: f"data:image/png;base64,{path}")

    check = {
        "index": 1,
        "character_scene_verdict": "match",
        "terminal_action_verdict": "match",
        "continuity_verdict": "match",
        "emotional_beat_verdict": "not_required",
        "placement_surface_verdict": "not_required",
        "observed_characters": "One adult is visible.",
        "observed_terminal_state": "The adult has entered the room.",
        "observed_gaze_expression": "",
        "observed_placement_surface": "",
        "observed_facts": ["One adult stands inside the room."],
        "missing_or_wrong_facts": [],
    }

    def respond(_db, **kwargs):
        calls.append(kwargs)
        envelope = {
            "schema_version": "1.0",
            "status": "PASS",
            "result": {
                "creative_review": "Visible contract matches.",
                "approved_for_split": True,
                "reference_image_count": 1,
                "reference_checks": [check],
            },
        }
        return {
            "model": "vision-model",
            "choices": [{"message": {"content": json.dumps(envelope)}}],
        }

    monkeypatch.setattr(content_factory_api, "_routed_multimodal_completion", respond)
    packet = _packet()
    packet.update({
        "current_stage": "CREATIVE_REVIEW",
        "render_reference_images_individually": True,
        "browser_assets": [{
            "name": "reference-01.png",
            "kind": "visual_preview",
            "role": "visual_preview",
            "mime_type": "image/png",
        }],
        "browser_asset_paths": ["/tmp/reference-01.png"],
    })
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "One adult enters the room.",
            "roles": ["character_anchor", "action_anchor"],
        }],
    }

    text, _meta = execute_text_stage_api(None, packet, "CREATIVE_REVIEW")

    content = calls[0]["payload"]["messages"][1]["content"]
    image_items = [item for item in content if item["type"] == "image_url"]
    assert [item["image_url"]["detail"] for item in image_items] == ["high"]
    assert json.loads(text)["result"]["approved_for_split"] is True
    assert calls[0]["logical_model"] == "visual-review-role"
    assert calls[0]["workload"] == "content_visual_inspector"






def test_creative_review_does_not_require_character_in_planned_phone_closeup():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "Phone close-up showing Place released on a desk.",
            "roles": ["scene", "action", "first_frame"],
        }],
    }
    envelope = {
        "status": "PASS",
        "issues": [],
        "result": {
            "creative_review": "The planned phone close-up is visible.",
            "approved_for_split": True,
            "reference_image_count": 1,
            "reference_checks": [{
                "index": 1,
                "character_scene_verdict": "mismatch",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "observed_characters": "No visible person; only desk objects.",
                "observed_terminal_state": "Phone shows Place released.",
                "observed_gaze_expression": "",
                "observed_placement_surface": "Wooden desk.",
                "observed_facts": ["phone", "Place released", "desk"],
                "missing_or_wrong_facts": [],
            }],
        },
    }

    approved = content_factory_api._apply_creative_review_reference_gate(
        envelope,
        packet,
    )

    assert approved["result"]["approved_for_split"] is True
    assert approved["result"]["reference_checks"][0][
        "character_scene_verdict"
    ] == "not_required"
    assert approved["issues"] == []


def test_individual_visual_api_prompt_does_not_call_one_frame_a_board():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 2,
        "reference_plan": [
            {"index": 1, "segment": 1, "description": "Adult enters the room.", "roles": ["action_anchor"]},
            {"index": 2, "segment": 2, "description": "Adult closes the laptop.", "roles": ["action_anchor"]},
        ],
    }

    prompts = build_visual_api_prompts(packet)

    assert len(prompts) == 2
    assert "This file represents scene 1 only." in prompts[0][0]
    assert "SCENE REQUIREMENT:" in prompts[0][0]
    assert not re.search(r"\b(?:board|panel|storyboard)\b", prompts[0][0], flags=re.IGNORECASE)


def test_adult_story_guard_ignores_negative_safety_echoes_but_rejects_real_minor_cast(monkeypatch):
    project = SimpleNamespace(config_json={
        "creative_cast_policy": {
            "allow_minor_story_characters": False,
        },
    })
    monkeypatch.setattr(content_factory_tasks, "_project_uses_product", lambda _project: True)
    adult_story = {
        "concepts": [{
            "title": "Rejected alternate concept with a daughter",
            "rationale": "This alternate was not selected.",
        }],
        "selected_concept": {
            "title": "Adult partners reset Friday trivia",
            "rationale": "Uses adult partners rather than a movie-night or child-bedtime scenario.",
        },
        "continuity_rules": {
            "characters": [{"name": "Maya", "age": "31", "role": "adult partner"}],
            "environment": "No children, minors, school references, or parenting cues.",
        },
        "shot_plan": [{"visual": "Maya closes her laptop in the adult living room."}],
        "evidence": ["The story contains no child or minor characters."],
    }
    _assert_adult_only_product_story(project, adult_story)

    adult_story["shot_plan"] = [{"visual": "Maya's daughter walks into the living room."}]
    with pytest.raises(
        ValueError,
        match="CREATIVE_CAST_POLICY_MINOR_STORY_CHARACTER_FORBIDDEN",
    ):
        _assert_adult_only_product_story(project, adult_story)


def test_project_cast_policy_can_allow_minor_story_characters(monkeypatch):
    project = SimpleNamespace(config_json={
        "creative_cast_policy": {
            "allow_minor_story_characters": True,
            "minimum_product_actor_age": 25,
        },
    })
    monkeypatch.setattr(
        content_factory_tasks,
        "_project_uses_product",
        lambda _project: True,
    )

    _assert_adult_only_product_story(
        project,
        {
            "selected_concept": {
                "title": "A mother notices her daughter stopped sharing",
            },
            "shot_plan": [
                {"visual": "Her daughter quietly puts a drawing away."},
            ],
        },
    )


def test_adult_story_guard_accepts_text_list_continuity_rules(monkeypatch):
    project = SimpleNamespace(config_json={
        "creative_cast_policy": {
            "allow_minor_story_characters": False,
        },
    })
    monkeypatch.setattr(content_factory_tasks, "_project_uses_product", lambda _project: True)
    adult_story = {
        "selected_concept": {
            "title": "Two adult partners reset the kitchen",
            "characters": [
                {"name": "Mara", "age": 42},
                {"name": "Ellis", "age": 44},
            ],
        },
        "continuity_rules": [
            "All characters are fictional adults age 25 or older.",
            "Mara remains the primary adult speaker.",
        ],
        "shot_plan": [],
        "complete_video_script": {"segments": []},
        "visual_job_ticket": {"reference_plan": []},
    }

    _assert_adult_only_product_story(project, adult_story)






def test_reference_plan_contract_merges_multiple_anchors_into_one_panel_per_segment():
    result = {
        "complete_video_script": {
            "segments": [
                {"segment_index": 1, "visual_action": "Adult enters the living room."},
                {"segment_index": 2, "visual_action": "Blanket and cushions collapse."},
                {"segment_index": 3, "visual_action": "Adult closes the laptop."},
                {"segment_index": 4, "visual_action": "Closed bottle rests naturally on the table."},
            ]
        },
        "visual_job_ticket": {
            "reference_plan": [
                {"index": 1, "segment": 1, "description": "Adult character anchor", "roles": ["character_anchor"]},
                {"index": 2, "segment": 1, "description": "Living room scene anchor", "roles": ["scene_anchor"]},
                {"index": 3, "segment": 2, "description": "Cushion collapse action", "roles": ["action_anchor"]},
                {"index": 4, "segment": 2, "description": "Adult partner reaction", "roles": ["character_anchor"]},
                {"index": 5, "segment": 3, "description": "Laptop closes", "roles": ["action_anchor"]},
                {"index": 6, "segment": 4, "description": "Closed MYUPONA bottle on table", "roles": ["action_anchor"], "requires_product_reference": True},
            ]
        },
    }

    normalized = _ensure_reference_plan_segment_coverage(result, product_allowed=True)
    plan = normalized["visual_job_ticket"]["reference_plan"]

    assert len(plan) == 4
    assert [item["segment"] for item in plan] == [1, 2, 3, 4]
    assert normalized["visual_job_ticket"]["reference_image_count"] == 4
    assert plan[-1]["requires_product_reference"] is True


def test_reference_plan_uses_canonical_segment_action_when_provider_rows_are_misaligned():
    result = {
        "complete_video_script": {
            "segments": [
                {"segment_index": 1, "visual_action": "Nora sees the fallen contract."},
                {"segment_index": 2, "visual_action": "A bride text arrives and the thank-you photo falls face-down."},
                {"segment_index": 3, "visual_action": "Nora closes her laptop and dims the lamp."},
                {"segment_index": 4, "visual_action": "The sealed MYUPONA bottle rests beside the folded throw."},
            ]
        },
        "visual_job_ticket": {
            "reference_plan": [
                {"index": 1, "segment": 1, "description": "Nora sees the fallen contract."},
                # Bad provider output: it accidentally mapped segment 3's
                # reset and product language to segment 2.
                {"index": 2, "segment": 2, "description": "Nora closes the laptop beside MYUPONA."},
                {"index": 3, "segment": 3, "description": "Nora closes the laptop and dims the lamp."},
                {"index": 4, "segment": 4, "description": "The sealed MYUPONA bottle rests beside the folded throw."},
            ]
        },
    }

    normalized = _ensure_reference_plan_segment_coverage(result, product_allowed=True)
    plan = normalized["visual_job_ticket"]["reference_plan"]

    assert "bride text arrives" in plan[1]["description"].lower()
    assert plan[1]["requires_product_reference"] is False
    assert plan[2]["requires_product_reference"] is False
    assert plan[3]["requires_product_reference"] is True


def test_visual_prompt_does_not_contradict_portrait_board_geometry():
    packet = _packet()
    packet["user_instruction"] = (
        "Use sophisticated American adult 2D editorial animation, not photoreal people."
    )
    creative = packet["previous_outputs"]["MEDIA_DESIGN"]
    creative["visual_job_ticket"] = {
        "reference_image_count": 4,
        "reference_plan": [
            {
                "index": index,
                "segment": index,
                "description": f"Portrait continuity panel {index}",
                "roles": ["character_anchor", "action_anchor"],
            }
            for index in range(1, 5)
        ],
    }

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["size"] == "1024x1792"
    assert "ENTIRE output canvas must be a portrait board at 1024x1792" in prompt
    assert "do not turn it into one single full-frame scene" in prompt
    assert "never make the whole canvas 9:16" not in prompt.lower()
    assert (
        "PROJECT VISUAL DIRECTION: Use sophisticated American adult 2D editorial animation"
        in prompt
    )
    assert "MANDATORY VISUAL MEDIUM: American adult 2D/2.5D editorial illustration" in prompt
    assert "this is not live action and not a photograph" in prompt
    assert "ONE STATIC ILLUSTRATED IMAGE" in prompt


def test_visual_prompt_honors_project_animation_direction_without_stage_instruction():
    packet = _packet()
    packet["user_instruction"] = ""
    packet["project_requirements"] = "VISUAL STYLE: sophisticated American adult 2D/2.5D editorial animation. No photorealistic humans."

    prompt, _spec = build_visual_api_prompt(packet)

    assert "MANDATORY VISUAL MEDIUM: American adult 2D/2.5D editorial illustration" in prompt


def test_native_reference_prompt_freezes_only_the_final_decisive_state():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 4,
            "description": (
                "Mara closes her book, then approaches the bedroom. "
                "Jo opens the door and they share a quiet smile."
            ),
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }],
    }

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["count"] == 1
    assert "SINGLE-FRAME KEYFRAME RULE" in prompt
    assert "depict ONLY the last decisive state" in prompt
    assert "Do not depict an earlier or intermediate action" in prompt
    assert "final character placement, interaction, prop state, and emotional beat are authoritative" in prompt


def test_closed_bottle_safety_clause_keeps_final_product_reference_enabled():
    result = {
        "complete_video_script": {
            "segments": [
                {"segment_index": 1, "visual_action": "Adult closes the laptop."},
                {"segment_index": 2, "visual_action": "Adult silences the phone."},
                {"segment_index": 3, "visual_action": "Adult folds the throw."},
                {
                    "segment_index": 4,
                    "visual_action": (
                        "The MYUPONA bottle rests on the coffee table. "
                        "Keep the bottle sealed and closed; no loose gummies or consumption."
                    ),
                },
            ]
        },
        "visual_job_ticket": {
            "reference_image_count": 4,
            "reference_plan": [
                {"index": index, "segment": index, "description": f"Stale row {index}"}
                for index in range(1, 5)
            ],
        },
    }

    plan = _ensure_reference_plan_segment_coverage(
        _ensure_reference_plan(result, product_allowed=True),
        product_allowed=True,
    )["visual_job_ticket"]["reference_plan"]

    assert plan[3]["requires_product_reference"] is True


def test_superseded_stage_delivery_is_never_considered_current():
    stage = SimpleNamespace(
        status="superseded",
        celery_task_id="task-current",
        input_json={"run_token": "run-current"},
    )

    assert _is_current_stage_delivery(
        stage,
        request_id="task-current",
        run_token="run-current",
    ) is False


def test_provider_submit_fence_rejects_stage_replaced_during_anchor_render():
    db = MagicMock()
    stale_stage = SimpleNamespace(status="running")
    project = SimpleNamespace(current_stage="VISUAL_PREVIEW")
    stage_query = MagicMock()
    stage_query.filter.return_value = stage_query
    stage_query.populate_existing.return_value = stage_query
    stage_query.one_or_none.return_value = stale_stage
    project_query = MagicMock()
    project_query.filter.return_value = project_query
    project_query.populate_existing.return_value = project_query
    project_query.one_or_none.return_value = project
    latest_query = MagicMock()
    latest_query.filter.return_value = latest_query
    latest_query.scalar.return_value = 1811
    db.query.side_effect = [stage_query, project_query, latest_query]

    with pytest.raises(_SupersededVisualProviderSubmission):
        _assert_visual_provider_submission_current(
            db,
            project_id=168,
            stage_id=1810,
            stage_name="VISUAL_PREVIEW",
        )


def test_provider_submit_fence_rejects_new_delivery_of_same_stage():
    db = MagicMock()
    replaced_delivery = SimpleNamespace(
        status="running",
        celery_task_id="task-new",
        input_json={"run_token": "run-new"},
    )
    project = SimpleNamespace(current_stage="VISUAL_PREVIEW")
    stage_query = MagicMock()
    stage_query.filter.return_value = stage_query
    stage_query.populate_existing.return_value = stage_query
    stage_query.one_or_none.return_value = replaced_delivery
    project_query = MagicMock()
    project_query.filter.return_value = project_query
    project_query.populate_existing.return_value = project_query
    project_query.one_or_none.return_value = project
    latest_query = MagicMock()
    latest_query.filter.return_value = latest_query
    latest_query.scalar.return_value = 1818
    db.query.side_effect = [stage_query, project_query, latest_query]

    with pytest.raises(_SupersededVisualProviderSubmission, match="expected_task=task-old"):
        _assert_visual_provider_submission_current(
            db,
            project_id=168,
            stage_id=1818,
            stage_name="VISUAL_PREVIEW",
            expected_request_id="task-old",
            expected_run_token="run-old",
        )


def test_provider_submit_fence_rejects_manual_pause_before_billable_post():
    db = MagicMock()
    active_stage = SimpleNamespace(
        status="running",
        celery_task_id="task-current",
        input_json={"run_token": "run-current"},
    )
    project = SimpleNamespace(
        status="paused",
        current_stage="VISUAL_PREVIEW",
        config_json={"manual_paused": True},
    )
    stage_query = MagicMock()
    stage_query.filter.return_value = stage_query
    stage_query.populate_existing.return_value = stage_query
    stage_query.one_or_none.return_value = active_stage
    project_query = MagicMock()
    project_query.filter.return_value = project_query
    project_query.populate_existing.return_value = project_query
    project_query.one_or_none.return_value = project
    latest_query = MagicMock()
    latest_query.filter.return_value = latest_query
    latest_query.scalar.return_value = 1818
    db.query.side_effect = [stage_query, project_query, latest_query]

    with pytest.raises(_SupersededVisualProviderSubmission):
        _assert_visual_provider_submission_current(
            db,
            project_id=168,
            stage_id=1818,
            stage_name="VISUAL_PREVIEW",
            expected_request_id="task-current",
            expected_run_token="run-current",
        )


def test_retry_release_ignores_manual_pause_without_publishing(monkeypatch):
    stage = SimpleNamespace(
        id=1928,
        stage="VISUAL_PREVIEW",
        status="retrying",
        input_json={"retry_release_task_id": "release-old"},
    )
    project = SimpleNamespace(
        id=168,
        project_key="cf_test",
        current_stage="VISUAL_PREVIEW",
        status="paused",
        config_json={"manual_paused": True},
    )
    db = MagicMock()
    monkeypatch.setattr(content_factory_tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        content_factory_tasks,
        "_lock_stage_delivery_scope",
        lambda *_args, **_kwargs: (stage, project, 168),
    )
    publish = MagicMock()
    monkeypatch.setattr(content_factory_tasks, "_publish_stage", publish)

    result = content_factory_tasks.release_content_factory_stage_retry.run(
        stage_id=1928,
        release_token="release-old",
    )

    assert result == {"status": "ignored_paused", "stage_id": 1928}
    assert stage.status == "retrying"
    publish.assert_not_called()
    db.commit.assert_not_called()
    db.close.assert_called_once()


def test_mysql_project_execution_guard_uses_connection_scoped_lock():
    db = MagicMock()
    engine = MagicMock()
    engine.dialect.name = "mysql"
    connection = MagicMock()
    connection.dialect.name = "mysql"
    connection.execute.return_value.scalar.return_value = 1
    engine.connect.return_value = connection
    db.get_bind.return_value = engine

    acquired, owner = _acquire_project_execution_guard(db, 168)

    assert acquired is True
    assert owner is connection
    assert "GET_LOCK" in str(connection.execute.call_args_list[0].args[0])

    _release_project_execution_guard(owner, 168)

    assert "RELEASE_LOCK" in str(connection.execute.call_args_list[-1].args[0])
    connection.commit.assert_called_once()
    connection.close.assert_called_once()


def test_execution_lock_collision_uses_database_cooldown_without_eta_task(monkeypatch):
    commits = []

    class FakeDb:
        def add(self, _value):
            return None

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(
        content_factory_tasks.run_content_factory_stage,
        "apply_async",
        lambda **_kwargs: pytest.fail("lock cooldown must not create a Celery ETA delivery"),
    )
    fixed_now = datetime(2026, 7, 20, 14, 0, 0)
    monkeypatch.setattr(content_factory_tasks, "_stage_now", lambda: fixed_now)
    project = SimpleNamespace(
        id=168,
        project_key="cf_test",
        current_stage="VISUAL_PREVIEW",
        status="queued",
        config_json={},
    )
    stage = SimpleNamespace(
        id=1887,
        stage="VISUAL_PREVIEW",
        status="queued",
        celery_task_id="current-task",
        input_json={
            "run_token": "current-token",
            "queue": "gmv.tasks.hermes_agent",
            "queue_priority": 9,
        },
        error_message=None,
    )

    result = content_factory_tasks._reschedule_current_stage_after_execution_lock_collision(
        FakeDb(),
        project,
        stage,
        request_id="current-task",
        delivery_run_token="current-token",
    )

    assert result == {
        "status": "project_execution_lock_cooldown_scheduled",
        "stage_id": 1887,
        "retry_in_seconds": 2,
        "collision_count": 1,
        "release_strategy": "periodic_self_heal",
    }
    assert stage.celery_task_id is None
    assert stage.status == "retrying"
    assert stage.input_json["project_execution_lock_retry_count"] == 1
    assert stage.input_json["retry_after"] == "2026-07-20T14:00:02"
    assert stage.input_json["retry_release_strategy"] == "database_due_at_periodic_self_heal"
    assert "retry_release_task_id" not in stage.input_json
    assert commits == [True]


def test_execution_lock_collision_does_not_reschedule_stale_delivery(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks.run_content_factory_stage,
        "apply_async",
        lambda **_kwargs: pytest.fail("stale delivery must not be republished"),
    )
    project = SimpleNamespace(
        id=168,
        project_key="cf_test",
        current_stage="VISUAL_PREVIEW",
        status="queued",
        config_json={},
    )
    stage = SimpleNamespace(
        id=1887,
        stage="VISUAL_PREVIEW",
        status="queued",
        celery_task_id="replacement-task",
        input_json={"run_token": "current-token"},
    )

    result = content_factory_tasks._reschedule_current_stage_after_execution_lock_collision(
        SimpleNamespace(),
        project,
        stage,
        request_id="stale-task",
        delivery_run_token="stale-token",
    )

    assert result is None


def test_failed_individual_reference_gets_api_retry_budget():
    delay = _visual_api_provider_retry_delay(
        "VISUAL_PREVIEW",
        {"visual_api": {"provider_retry_generation": 1, "boards": {"1": {"status": "failed"}}}},
        "Bandianwa image reference failures after polling every submitted reference: reference 1: task_failed",
    )

    assert delay == 20


def test_visual_transport_retry_keeps_paid_task_id_and_uses_separate_budget():
    stage_input = {
        "visual_api": {
            "provider_retry_generation": 0,
            "provider_transport_retry_count": 1,
            "boards": {
                "1": {
                    "status": "poll_retrying",
                    "task_id": "task-paid-image",
                    "provider_transport_retry_count": 1,
                }
            },
        }
    }

    assert _visual_provider_task_reusable_after_error(
        "task-paid-image",
        "Bandianwa image transport error during GET /v1/images/task-paid-image: ConnectTimeout",
    ) is True
    assert _visual_provider_task_reusable_after_error(
        "task-paid-image",
        "Bandianwa image failed: task_failed unsafe composition",
    ) is False
    assert _visual_api_provider_retry_delay(
        "VISUAL_PREVIEW",
        stage_input,
        "Bandianwa image transport error during GET /v1/images/task-paid-image: ConnectTimeout",
    ) == 20


def test_visual_api_insufficient_balance_activates_fallback_without_waiting():
    message = (
        'Bandianwa image HTTP 403: {"error":{"code":"insufficient_user_quota",'
        '"message":"预扣费额度失败, 用户剩余额度: ¥0.03, 需要预扣费额度: ¥0.13"}}'
    )

    assert _visual_api_account_quota_exhausted("VISUAL_PREVIEW", message) is True
    assert _visual_api_account_quota_exhausted("CREATIVE", message) is False
    assert _visual_api_account_quota_exhausted(
        "VISUAL_PREVIEW",
        "Bandianwa image failed: task_failed adjust prompt",
    ) is False


def test_visual_api_provider_failover_preserves_completed_boards_only():
    assert "provider_key" in inspect.signature(
        _generate_individual_visual_references_via_api
    ).parameters

    switched = _prepare_visual_api_provider_failover(
        {
            "execution_backend": "api",
            "api_route": "bandianwa:gpt-image-2",
            "visual_api_force_browser_fallback": True,
            "visual_api": {
                "status": "failed",
                "provider_retry_generation": 4,
                "boards": {
                    "1": {
                        "status": "completed",
                        "task_id": "done-1",
                        "output_path": "/tmp/reference-1.png",
                    },
                    "3": {
                        "status": "failed",
                        "task_id": "failed-3",
                        "provider_retry_generation": 4,
                    },
                },
            },
        },
        from_provider="bandianwa",
        to_provider="toapis",
        error="insufficient balance",
    )

    assert switched["execution_backend"] == "api"
    assert switched["api_route"] == "toapis:gpt-image-2"
    assert switched["visual_api_skip_bandianwa"] is True
    assert "visual_api_force_browser_fallback" not in switched
    assert switched["visual_api"]["boards"]["1"]["status"] == "completed"
    assert switched["visual_api"]["boards"]["1"]["task_id"] == "done-1"
    assert switched["visual_api"]["boards"]["3"]["status"] == "pending_provider_failover"
    assert switched["visual_api"]["boards"]["3"]["task_id"] is None
    assert switched["visual_api"]["boards"]["3"]["provider_retry_generation"] == 0
    assert switched["visual_api"]["bandianwa_account_quota_exhausted"] is True


def test_partial_failed_individual_board_exhausts_provider_budget(monkeypatch):
    monkeypatch.setenv("HERMES_VISUAL_API_PROVIDER_MAX_RETRIES", "3")
    base = {
        "visual_api": {
            "status": "partial_failed",
            "task_id": None,
            "boards": {"1": {"status": "failed", "task_id": None}},
        },
    }

    within_budget = json.loads(json.dumps(base))
    within_budget["visual_api"]["provider_retry_generation"] = 3
    exhausted = json.loads(json.dumps(base))
    exhausted["visual_api"]["provider_retry_generation"] = 4

    assert content_factory_tasks._visual_api_provider_budget_exhausted(
        "VISUAL_PREVIEW",
        within_budget,
    ) is False
    assert content_factory_tasks._visual_api_provider_budget_exhausted(
        "VISUAL_PREVIEW",
        exhausted,
    ) is True


def test_retry_budget_failover_marks_provider_exhausted_without_fake_quota_failure():
    switched = _prepare_visual_api_provider_failover(
        {
            "api_route": "bandianwa:gpt-image-2",
            "visual_api": {
                "status": "partial_failed",
                "provider_retry_generation": 4,
                "boards": {"1": {"status": "failed", "task_id": None}},
            },
        },
        from_provider="bandianwa",
        to_provider="toapis",
        error="bounded image task retry budget exhausted",
        account_quota_exhausted=False,
    )

    failure = switched["visual_api"]["provider_failures"]["bandianwa"]
    assert switched["api_route"] == "toapis:gpt-image-2"
    assert switched["visual_api_skip_bandianwa"] is True
    assert switched["visual_api"]["bandianwa_account_quota_exhausted"] is False
    assert failure["account_quota_exhausted"] is False
    assert failure["retry_budget_exhausted"] is True


def test_retry_release_does_not_acquire_browser_when_toapis_can_take_over(monkeypatch):
    stage_input = {
        "api_route": "bandianwa:gpt-image-2",
        "visual_api": {
            "status": "partial_failed",
            "provider_retry_generation": 4,
            "boards": {"1": {"status": "failed", "task_id": None}},
        },
    }
    project = SimpleNamespace(id=168)
    stage = SimpleNamespace(stage="VISUAL_PREVIEW")

    monkeypatch.setattr(
        content_factory_tasks,
        "_content_factory_api_route",
        lambda *_args, **_kwargs: "bandianwa:gpt-image-2",
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "stage_execution_backend",
        lambda *_args, **_kwargs: "api",
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "has_active_key",
        lambda *_args, **kwargs: kwargs.get("provider_key") == "toapis",
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_locked_browser_routing",
        lambda *_args, **_kwargs: pytest.fail("browser routing must stay dormant"),
    )

    assert content_factory_tasks._retry_release_routing(
        SimpleNamespace(),
        project,
        stage,
        stage_input,
    ) == (None, None, "gmv.tasks.hermes_agent")


def test_creative_review_rejects_photoreal_board_when_animation_is_required():
    packet = _packet()
    packet["user_instruction"] = "Use American adult 2D editorial animation, not photoreal people."

    _system, user = build_text_api_request(packet, "CREATIVE_REVIEW")

    assert "Reject any photorealistic, live-action, photographic, or real-person-looking board" in user
    assert "visibly illustrated characters, environment, and lighting" in user


def test_visual_api_model_uses_pro_image_for_requested_adult_animation():
    animated = _packet()
    animated["user_instruction"] = "Use American adult 2D/2.5D editorial animation, never photoreal people."

    assert _visual_api_model_for_packet(animated) == "nano_banana_pro"
    assert _visual_api_model_for_packet(_packet()) == "nano_banana_2"


def test_visual_api_model_obeys_project_owned_preference_chain():
    packet = _packet()
    packet["visual_image_model_chain"] = [
        "gpt-image-2.0",
        "nano_banana_pro",
    ]

    assert _visual_api_model_for_packet(packet) == "gpt-image-2"
    packet["visual_image_model_index"] = 1
    assert _visual_api_model_for_packet(packet) == "nano_banana_pro"


def test_visual_image_model_failover_resets_board_identity():
    switched = _prepare_visual_image_model_failover(
        {
            "api_route": "bandianwa:gpt-image-2",
            "visual_api": {
                "provider": "bandianwa",
                "model": "gpt-image-2",
                "status": "partial_failed",
                "provider_retry_generation": 4,
                "boards": {"1": {"status": "completed"}},
            },
        },
        from_model="gpt-image-2",
        to_model="nano_banana_pro",
        next_index=1,
        error="primary model exhausted",
    )

    assert switched["visual_image_model_index"] == 1
    assert switched["visual_api"]["model"] == "nano_banana_pro"
    assert switched["visual_api"]["boards"] == {}
    assert switched["visual_api"]["provider_retry_generation"] == 0


def test_visual_compaction_preserves_stage_and_project_animation_contract():
    packet = _packet()
    packet["user_instruction"] = "Render independent adult 2D/2.5D editorial animation frames; never photoreal."
    packet["project_requirements"] = "copy constraints\n\nVISUAL STYLE:\nUse sophisticated American adult 2D/2.5D editorial animation. No photorealistic humans.\n\nPRODUCT TRUTH:\nOnly approved facts."

    compact = _compact_packet_for_chatgpt(packet, "VISUAL_PREVIEW")
    prompt, _spec = build_visual_api_prompt(compact)

    assert compact["user_instruction"] == packet["user_instruction"]
    assert "American adult 2D/2.5D editorial animation" in compact["visual_style_requirement"]
    assert "MANDATORY VISUAL MEDIUM: American adult 2D/2.5D editorial illustration" in prompt
    assert "PROJECT VISUAL STYLE CONTRACT" in prompt


def test_segment_timeline_accepts_mm_ss_provider_timecodes():
    timeline = _normalize_segment_timeline(
        [
            {"timecode": "0:00-0:03", "action": "Reveal the empty chair."},
            {"timecode": "0:03-0:07", "action": "Follow her hand across the books."},
            {"timecode": "0:07-0:10", "action": "End on the turning page."},
        ],
        segment_duration=10,
        segment_offset=0,
        video_index=2,
        segment_index=1,
    )

    assert [(beat["start_second"], beat["end_second"]) for beat in timeline] == [
        (0.0, 3.0), (3.0, 7.0), (7.0, 10.0),
    ]


def test_video_prompt_provider_alias_is_canonicalized_before_persistence():
    result = {
        "videos": [{
            "version_name": "v01",
            "segments": [
                {"segment_index": 1, "short_prompt": "Only this segment."},
                {"segment_index": 2, "prompt": "Canonical wins.", "short_prompt": "Alias."},
            ],
        }],
    }

    normalized = _canonicalize_video_prompt_result(result)
    segments = normalized["videos"][0]["segments"]

    assert segments[0]["prompt"] == "Only this segment."
    assert segments[1]["prompt"] == "Canonical wins."
    assert all("short_prompt" not in segment for segment in segments)
    assert "short_prompt" in result["videos"][0]["segments"][0]


def test_video_wait_drains_submitted_work_after_automatic_quality_pause():
    project = SimpleNamespace(
        status="paused",
        config_json={"manual_paused": False},
        state_json={"ai_video_task_ids": [101, 102]},
    )

    assert _video_wait_pause_mode(project) == "drain_submitted_video"


def test_video_wait_respects_manual_pause_even_with_submitted_work():
    project = SimpleNamespace(
        status="paused",
        config_json={"manual_paused": True},
        state_json={"ai_video_task_ids": [101, 102]},
    )

    assert _video_wait_pause_mode(project) == "manual"


def test_video_wait_repairs_stale_manual_flag_when_newer_quality_pause_owns_state():
    project = SimpleNamespace(
        status="paused",
        config_json={"manual_paused": True},
        state_json={
            "paused_at": "2026-07-17T20:24:04",
            "ai_video_task_ids": [101, 102],
            "creative_visual_replan_exhausted": {"at": "2026-07-18T06:46:48"},
        },
    )

    assert _video_wait_pause_mode(project) == "drain_submitted_video"


def test_video_wait_keeps_quality_drain_owner_after_retry_temporarily_sets_generating():
    project = SimpleNamespace(
        status="generating_video",
        config_json={"manual_paused": True},
        state_json={
            "paused_at": "2026-07-17T20:24:04",
            "ai_video_task_ids": [101, 102],
            "creative_visual_replan_exhausted": {"at": "2026-07-18T06:46:48"},
        },
    )

    assert _video_wait_pause_mode(project) == "drain_submitted_video"


def test_video_wait_keeps_newer_operator_pause_ahead_of_old_quality_marker():
    project = SimpleNamespace(
        status="paused",
        config_json={"manual_paused": True},
        state_json={
            "paused_at": "2026-07-18T07:00:00",
            "manual_paused_at": "2026-07-18T07:00:00",
            "pause_reason_code": "manual",
            "ai_video_task_ids": [101, 102],
            "creative_visual_replan_exhausted": {"at": "2026-07-18T06:46:48"},
        },
    )

    assert _video_wait_pause_mode(project) == "manual"


def test_resume_metadata_cleanup_removes_pause_ownership_only():
    state = {
        "paused_at": "2026-07-18T07:00:00",
        "manual_paused_at": "2026-07-18T07:00:00",
        "automatic_quality_paused_at": "2026-07-18T06:46:48",
        "pause_reason_code": "manual",
        "pause_note": "operator pause",
        "ai_video_task_ids": [101, 102],
    }

    cleaned = _clear_project_pause_metadata(state)

    assert cleaned == {"ai_video_task_ids": [101, 102]}
    assert state["pause_reason_code"] == "manual"


def test_video_recovery_does_not_orphan_automatic_quality_pause_drain():
    project = SimpleNamespace(
        status="paused",
        config_json={"manual_paused": True},
        state_json={
            "paused_at": "2026-07-17T20:24:04",
            "ai_video_task_ids": [2408],
            "creative_visual_replan_exhausted": {"at": "2026-07-18T06:46:48"},
        },
    )

    assert _content_project_drains_submitted_video(project, task_id=2408) is True
    assert _content_project_drains_submitted_video(project, task_id=2409) is False


def test_video_poll_owner_rejects_duplicate_live_worker():
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        result_json={
            "__local": {
                "poll_owner_task_id": "worker-a",
                "poll_heartbeat_at": now.isoformat(),
            }
        },
        input_json={"service_provider": "bandianwa"},
        updated_at=None,
    )

    assert _poll_heartbeat_is_recent(task, max_age_seconds=60, now=now) is True
    assert _claim_poll_owner(task, owner_task_id="worker-b", max_age_seconds=60) is False
    assert task.result_json["__local"]["poll_owner_task_id"] == "worker-a"


def test_superseded_content_factory_task_is_rejected_before_provider_io():
    task = SimpleNamespace(
        id=2550,
        workspace_id=3,
        fail_code="cf_variant_superseded",
        fail_msg=None,
        state="queued",
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_variant_index": 24,
            "content_factory_segment_index": 1,
        },
        result_json={"__local": {"superseded_by_task_ids": [2554, 2555, 2556, 2557]}},
        updated_at=None,
    )
    project = SimpleNamespace(state_json={
        "ai_video_groups": [{
            "video_index": 24,
            "segments": [{"segment_index": 1, "task_id": 2554}],
        }],
    })

    authorized, reason = _content_factory_task_authority(project, task)

    assert authorized is False
    assert reason == "superseded_marker"


def test_replacement_group_is_the_only_authoritative_segment_owner():
    project = SimpleNamespace(state_json={
        "ai_video_groups": [{
            "video_index": 24,
            "segments": [
                {"segment_index": 1, "task_id": 2554},
                {"segment_index": 2, "task_id": 2555},
            ],
        }],
    })
    current = SimpleNamespace(
        id=2554,
        fail_code=None,
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_variant_index": 24,
            "content_factory_segment_index": 1,
        },
        result_json={"__local": {}},
    )
    stale = SimpleNamespace(
        id=2542,
        fail_code=None,
        input_json=dict(current.input_json),
        result_json={"__local": {}},
    )

    assert _content_factory_task_authority(project, current) == (
        True,
        "current_variant_segment",
    )
    assert _content_factory_task_authority(project, stale) == (
        False,
        "segment_owned_by_replacement",
    )


def test_content_factory_task_requires_group_stage_and_manifest_identity():
    project = SimpleNamespace(state_json={
        "ai_video_groups": [{
            "video_index": 6,
            "source_stage_id": 2311,
            "media_manifest_sha256": "manifest-new",
            "segments": [{"segment_index": 1, "task_id": 2701}],
        }],
    })
    task = SimpleNamespace(
        id=2701,
        fail_code=None,
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_variant_index": 6,
            "content_factory_segment_index": 1,
            "content_factory_source_stage_id": 2306,
            "content_factory_media_manifest_sha256": "manifest-old",
        },
        result_json={"__local": {}},
    )

    assert _content_factory_task_authority(project, task) == (
        False,
        "source_stage_identity_mismatch",
    )


def test_non_authoritative_delivery_is_quarantined_without_erasing_remote_identity():
    task = SimpleNamespace(
        task_id="task_remote_already_submitted",
        state="queued",
        fail_code="cf_variant_superseded",
        fail_msg=None,
        input_json={"service_provider": "bandianwa"},
        result_json={"__local": {"poll_owner_task_id": "stale-delivery"}},
        updated_at=None,
    )

    _quarantine_non_authoritative_content_task(task, reason="superseded_marker")

    assert task.task_id == "task_remote_already_submitted"
    assert task.state == "failed"
    assert task.fail_code == "cf_variant_superseded"
    assert task.result_json["__local"]["authority_rejected_reason"] == "superseded_marker"
    assert task.result_json["__local"].get("poll_owner_task_id") is None


def test_bandianwa_retry_releases_previous_poll_owner_lease(monkeypatch):
    class _Session:
        def add(self, _value):
            pass

        def flush(self):
            pass

    monkeypatch.setattr("app.services.bandianwa.tasks.log_event", lambda *_args, **_kwargs: None)
    task = SimpleNamespace(
        id=81,
        workspace_id=3,
        key_id=11,
        model="omni_flash",
        task_id="remote-old-task",
        state="failed",
        prompt="keep this prompt",
        input_json={"model": "omni_flash", "prompt": "keep this prompt"},
        result_json={
            "__local": {
                "poll_owner_task_id": "stale-worker",
                "poll_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "poll_heartbeat_provider": "bandianwa",
            }
        },
        fail_code="PUBLIC_ERROR_MINOR",
        fail_msg="temporary provider failure",
    )

    retried = reset_bandianwa_task_for_retry(_Session(), task=task, retry_kind="manual")

    assert retried.state == "queued_local"
    assert retried.task_id.startswith("local-bandianwa-")
    meta = retried.result_json["__local"]
    assert "poll_owner_task_id" not in meta
    assert "poll_heartbeat_at" not in meta
    assert "poll_heartbeat_provider" not in meta


def test_dependency_failed_segment_is_never_retried_independently():
    failed = SimpleNamespace(
        id=2450,
        fail_code="dependency_failed",
        fail_msg="Previous segment failed; this chained segment cannot be generated safely.",
        result_json={"__local": {"dependency_failed_task_id": 2449}},
    )
    project = SimpleNamespace(config_json={"content_factory_video_retry_limit": 5}, state_json={})

    assert _retry_failed_video_segments(SimpleNamespace(), project, [failed]) == []


def test_five_panel_visual_board_keeps_short_row_cells_portrait_and_same_size():
    packet = _packet()
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 5
    ticket["reference_plan"] = list(ticket["reference_plan"][:5])

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["row_columns"] == [3, 2]
    assert "same width and height as a first-row panel" in prompt
    assert "pure-white outer margins" in prompt
    assert "Never stretch either lower panel to half-canvas width" in prompt


def test_visual_prompt_compiles_motion_and_multi_closeups_into_one_static_cell():
    packet = _packet()
    creative = packet["previous_outputs"]["MEDIA_DESIGN"]
    plan = creative["visual_job_ticket"]["reference_plan"]
    plan[0]["description"] = (
        "Close-up of worried homeowner looking at washing machine. "
        "Cut inside drum to a tiny resident. | Hook recreation | Fast push-in transition."
    )
    plan[2]["description"] = (
        "Show close-ups of detergent residue, fabric softener traces, lint, and buildup layers. "
        "| Problem education | Extreme macro inspection shots."
    )

    prompt, _spec = build_visual_api_prompt(packet)

    assert "Cut inside drum" not in prompt
    assert "Fast push-in transition" not in prompt
    assert "Show close-ups of" not in prompt
    assert "One extreme macro still shows one combined physical mass containing" in prompt


def test_native_reference_prompt_compiles_video_motion_to_static_terminal_states():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["visual_style_requirement"] = (
        "American adult 2D editorial animation with restrained cinematic stylization. "
        "Use 6-10 shots. A child hides a drawing."
    )
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 4
    ticket["reference_plan"] = [
        {
            "index": 1,
            "segment": 1,
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
            "description": (
                "Loss hook: Fast backward tracking retreats down the cool blue-gray hallway as "
                "Rhea follows the unfinished score toward the wrong door; the metronome rolls "
                "into frame and the camera catches Rhea's immediate recognition"
            ),
        },
        {
            "index": 2,
            "segment": 2,
            "roles": ["action_anchor"],
            "description": (
                "The backward track accelerates door to door with the metronome rolling ahead. "
                "Rhea catches it beside a blank page, kneels, and holds the silence of the hallway"
            ),
        },
        {
            "index": 3,
            "segment": 3,
            "roles": ["action_anchor"],
            "description": (
                "The camera stabilizes. Rhea closes the score, places the metronome on the console, "
                "dims the hallway controls, and turns toward the quiet threshold"
            ),
        },
        {
            "index": 4,
            "segment": 4,
            "roles": ["action_anchor", "scene_anchor"],
            "description": (
                "In warm amber light, Rhea places the closed score beside the metronome and sets "
                "the sealed MYUPONA bottle on the console. She steps to the calm threshold and "
                "rests one hand on the doorframe."
            ),
            "requires_product_reference": True,
        },
    ]

    prompts = [prompt for prompt, _spec in build_visual_api_prompts(packet)]
    combined = "\n".join(prompts)

    assert not re.search(
        r"\b(?:camera|tracking|track|rolls|rolling|cinematic|dolly|pan|zoom|child)\b",
        combined,
        flags=re.IGNORECASE,
    )
    assert "looks directly at the metronome with immediate recognition" in prompts[0]
    assert "has visibly dimmed the hallway controls" in prompts[2]
    assert "warm amber light" in prompts[3]
    assert "closed score beside the metronome" in prompts[3]
    assert "render it naturally with the scene lighting, perspective, contact, and occlusion" in prompts[3]
    assert "Do not paste the uploaded image or its white background" in prompts[3]
    assert content_factory_api._IMAGE_PROMPT_PRODUCT_TRIGGER_RE.search(prompts[3])


def test_review_failure_indices_preserve_only_clean_rows():
    failed, observed = _creative_review_failed_reference_indices({
        "reference_checks": [
            {
                "index": 1,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "mismatch",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "uncertain",
                "placement_surface_verdict": "not_required",
                "missing_or_wrong_facts": ["motion cannot be proved"],
            },
            {
                "index": 2,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "match",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "not_required",
                "missing_or_wrong_facts": [],
            },
            {
                "index": 3,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "uncertain",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "uncertain",
                "placement_surface_verdict": "not_required",
                "missing_or_wrong_facts": [],
            },
            {
                "index": 4,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "mismatch",
                "continuity_verdict": "mismatch",
                "emotional_beat_verdict": "uncertain",
                "placement_surface_verdict": "mismatch",
                "missing_or_wrong_facts": ["surface missing"],
            },
        ]
    })

    assert observed == {1, 2, 3, 4}
    assert failed == {1, 3, 4}


def test_targeted_visual_repair_uses_concrete_pixel_failures_first():
    brief = _targeted_visual_repair_brief({
        "reference_checks": [
            {
                "index": 3,
                "character_scene_verdict": "mismatch",
                "terminal_action_verdict": "mismatch",
                "continuity_verdict": "mismatch",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "match",
                "missing_or_wrong_facts": [
                    "Required adult Rosa is not visibly present.",
                ],
                "observed_terminal_state": (
                    "A face-down phone and apron are visible, but no person is shown."
                ),
            },
            {
                "index": 4,
                "character_scene_verdict": "match",
                "terminal_action_verdict": "mismatch",
                "continuity_verdict": "match",
                "emotional_beat_verdict": "not_required",
                "placement_surface_verdict": "mismatch",
                "missing_or_wrong_facts": [
                    "An unrelated cardboard box blocks the product placement area.",
                ],
                "observed_terminal_state": "Rosa holds a cardboard box on the table.",
            },
        ],
    })

    assert brief.startswith("Regenerate only the failed native target-aspect references.")
    assert "Reference 3: Required adult Rosa is not visibly present." in brief
    assert "Reference 4: An unrelated cardboard box" in brief
    assert "Do not repeat the rejected state" in brief


def test_creative_reference_text_dependency_blocks_impossible_image_contract():
    result = {
        "visual_job_ticket": {
            "reference_plan": [
                {
                    "index": 1,
                    "single_frame_terminal_state": (
                        "Maren sees a notebook showing the handwritten word KETTLE."
                    ),
                },
                {
                    "index": 2,
                    "single_frame_terminal_state": (
                        "Maren looks at a cold mug beside a cream kettle."
                    ),
                },
                {
                    "index": 3,
                    "description": "A phone screen displays the sender name Laura.",
                },
            ],
        },
    }

    assert _creative_reference_text_dependencies(result) == [1, 3]


def test_single_reference_repair_keeps_only_current_frame_correction():
    repair = (
        "Regenerate only the failed native 9:16 references. "
        "Reference 2: The adult friend must look toward the empty cushion. "
        "Do not repeat the rejected state: the friend looks at the phone. "
        "Reference 4: Remove the unrelated blue box and leave the side table clear. "
        "Preserve all passed references and the established cast."
    )

    second = _single_reference_repair_instruction(
        repair,
        reference_index=2,
    )
    fourth = _single_reference_repair_instruction(
        repair,
        reference_index=4,
    )

    assert "adult friend must look toward the empty cushion" in second
    assert "blue box" not in second
    assert "Remove the unrelated blue box" in fourth
    assert "empty cushion" not in fourth
    assert "do not create a board, grid, collage, or split screen" in fourth


def test_single_reference_repair_keeps_server_product_surface_instruction():
    repair = (
        "Regenerate only reference 5. The prior product-free scene had no clear "
        "continuous support surface large enough for the exact package. Create "
        "an unobstructed natural tabletop with generous clearance from people, "
        "hands, props, and every foreground furniture edge."
    )

    fifth = _single_reference_repair_instruction(
        repair,
        reference_index=5,
    )
    fourth = _single_reference_repair_instruction(
        repair,
        reference_index=4,
    )

    assert "continuous support surface" in fifth
    assert "unobstructed natural tabletop" in fifth
    assert fourth == ""


def test_individual_visual_repair_submits_only_failed_references(
    monkeypatch,
    tmp_path,
):
    approved = tmp_path / "approved-reference-02.png"
    anchor = tmp_path / "continuity-anchor.png"
    for path, color in ((approved, "green"), (anchor, "blue")):
        Image.new("RGB", (32, 56), color).save(path, format="PNG")
    image_bytes = io.BytesIO()
    Image.new("RGB", (32, 56), "purple").save(image_bytes, format="PNG")
    rendered = image_bytes.getvalue()

    class Client:
        def __init__(self):
            self.submitted: list[int] = []

        async def create_image_task(self, *, prompt, size, model, images, idempotency_key):
            match = re.search(r"reference-(\d+)", idempotency_key)
            index = int(match.group(1)) if match else 0
            self.submitted.append(index)
            return {"task_id": f"task-{index}", "status": "queued"}

        async def get_image_task(self, *, task_id):
            return {"task_id": task_id, "status": "completed"}

    async def completed_bytes(_client, _response, *, task_id):
        return rendered

    monkeypatch.setattr(
        content_factory_tasks,
        "_assert_visual_provider_submission_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_bandianwa_image_bytes",
        completed_bytes,
    )
    client = Client()
    prompt_specs = [
        (
            f"static scene {index}",
            {
                "board_index": index,
                "board_count": 4,
                "count": 1,
                "size": "1024x1792",
                "global_start_index": index,
                "global_end_index": index,
                "plan": [{"index": index}],
            },
        )
        for index in range(1, 5)
    ]
    api_state = {
        "boards": {
            "2": {
                "status": "completed",
                "output_path": str(approved),
                "reference_count": 1,
                "task_id": "task-approved-2",
            }
        }
    }
    commits: list[bool] = []
    db = SimpleNamespace(commit=lambda: commits.append(True))
    stage_row = SimpleNamespace(id=1891, stage="VISUAL_PREVIEW", input_json={})
    project = SimpleNamespace(id=168)

    paths, meta = _generate_individual_visual_references_via_api(
        db,
        project=project,
        stage_row=stage_row,
        packet={"browser_assets": [{"role": "character_reference"}]},
            prompt_specs=prompt_specs,
            client=client,
            provider_key="bandianwa",
            visual_model="nano_banana_pro",
        output_dir=tmp_path,
        execution_id="partial-repair",
        stage_input={},
        api_state=api_state,
        provider_retry_generation=0,
        expected_request_id="",
        expected_run_token="",
        continuity_anchor_path=anchor,
    )

    assert client.submitted == [1, 3, 4]
    assert len(paths) == 4
    assert [row["board_index"] for row in meta["boards"]] == [1, 2, 3, 4]
    assert api_state["board_count"] == 4
    assert any(row.get("recovered_existing_file") for row in meta["boards"] if row["board_index"] == 2)
    assert commits
    persisted_boards = stage_row.input_json["visual_api"]["boards"]
    assert [persisted_boards[str(index)]["status"] for index in range(1, 5)] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]


def test_visual_provider_result_is_not_persisted_after_manual_pause(
    monkeypatch,
    tmp_path,
):
    rendered_buffer = io.BytesIO()
    Image.new("RGB", (32, 56), "purple").save(rendered_buffer, format="PNG")
    rendered = rendered_buffer.getvalue()

    class Client:
        async def create_image_task(self, **_kwargs):
            return {"task_id": "task-late", "status": "queued"}

        async def get_image_task(self, *, task_id):
            return {"task_id": task_id, "status": "completed"}

    async def completed_bytes(_client, _response, *, task_id):
        return rendered

    checks = 0

    def assert_current(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise _SupersededVisualProviderSubmission(
                "manual pause arrived while provider task was polling"
            )

    monkeypatch.setattr(
        content_factory_tasks,
        "_assert_visual_provider_submission_current",
        assert_current,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_bandianwa_image_bytes",
        completed_bytes,
    )
    db = SimpleNamespace(commit=lambda: None)
    stage_row = SimpleNamespace(id=2078, stage="VISUAL_PREVIEW", input_json={})
    project = SimpleNamespace(id=168)

    with pytest.raises(
        _SupersededVisualProviderSubmission,
        match="manual pause arrived",
    ):
        _generate_individual_visual_references_via_api(
            db,
            project=project,
            stage_row=stage_row,
            packet={
                "browser_assets": [
                    {"role": "character_reference", "local_path": ""}
                ]
            },
            prompt_specs=[(
                "one adult in a quiet room",
                {
                    "board_index": 1,
                    "board_count": 1,
                    "count": 1,
                    "size": "1024x1792",
                    "global_start_index": 1,
                    "global_end_index": 1,
                    "plan": [{"index": 1}],
                },
            )],
            client=Client(),
            provider_key="toapis",
            visual_model="gpt-image-2",
            output_dir=tmp_path,
            execution_id="late-result-pause",
            stage_input={},
            api_state={},
            provider_retry_generation=0,
            expected_request_id="task-current",
            expected_run_token="run-current",
        )

    assert checks == 3
    assert not list(tmp_path.glob("visual-preview-api-reference-*.png"))


def test_individual_visual_submit_transport_failure_isolated_to_one_reference(
    monkeypatch,
    tmp_path,
):
    anchor = tmp_path / "continuity-anchor.png"
    Image.new("RGB", (32, 56), "blue").save(anchor, format="PNG")
    image_bytes = io.BytesIO()
    Image.new("RGB", (32, 56), "purple").save(image_bytes, format="PNG")
    rendered = image_bytes.getvalue()

    class Client:
        def __init__(self):
            self.submitted: list[int] = []

        async def create_image_task(self, *, prompt, size, model, images, idempotency_key):
            match = re.search(r"reference-(\d+)", idempotency_key)
            index = int(match.group(1)) if match else 0
            self.submitted.append(index)
            if index == 3:
                raise content_factory_tasks.ToApisApiError(
                    "Server disconnected without sending a response."
                )
            return {"task_id": f"task-{index}", "status": "queued"}

        async def get_image_task(self, *, task_id):
            return {"task_id": task_id, "status": "completed"}

    async def completed_bytes(_client, _response, *, task_id):
        return rendered

    monkeypatch.setattr(
        content_factory_tasks,
        "_assert_visual_provider_submission_current",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        content_factory_tasks,
        "_bandianwa_image_bytes",
        completed_bytes,
    )
    prompt_specs = [
        (
            f"static scene {index}",
            {
                "board_index": index,
                "board_count": 4,
                "count": 1,
                "size": "1024x1792",
                "global_start_index": index,
                "global_end_index": index,
                "plan": [{"index": index}],
            },
        )
        for index in (2, 3, 4)
    ]
    db = SimpleNamespace(commit=lambda: None)
    stage_row = SimpleNamespace(id=1928, stage="VISUAL_PREVIEW", input_json={})
    project = SimpleNamespace(id=168)
    client = Client()

    with pytest.raises(BandianwaApiError, match="toapis image reference failures"):
        _generate_individual_visual_references_via_api(
            db,
            project=project,
            stage_row=stage_row,
            packet={"browser_assets": [{"role": "character_reference"}]},
            prompt_specs=prompt_specs,
            client=client,
            provider_key="toapis",
            visual_model="gpt-image-2",
            output_dir=tmp_path,
            execution_id="transport-isolation",
            stage_input={},
            api_state={},
            provider_retry_generation=0,
            expected_request_id="",
            expected_run_token="",
            continuity_anchor_path=anchor,
        )

    assert client.submitted == [2, 3, 4]
    boards = stage_row.input_json["visual_api"]["boards"]
    assert boards["2"]["status"] == "completed"
    assert boards["3"]["status"] == "failed"
    assert boards["3"]["provider_retry_generation"] == 1
    assert boards["4"]["status"] == "completed"


def test_browser_fallback_does_not_inherit_previous_provider_failure_budget():
    stage_input = {
        "api_route": "toapis:gpt-image-2",
        "visual_api": {
            "provider": "toapis",
            "status": "partial_failed",
            "provider_retry_generation": 1,
            "boards": {
                "1": {"status": "completed"},
                "3": {"status": "failed", "provider_retry_generation": 1},
            },
        },
    }

    assert _visual_api_final_provider_budget_exhausted(
        "VISUAL_PREVIEW",
        stage_input,
        api_route="toapis:gpt-image-2",
    ) is False
    assert _visual_api_final_provider_budget_exhausted(
        "VISUAL_PREVIEW",
        stage_input,
        api_route="bandianwa:gpt-image-2",
    ) is False


def test_partial_visual_repair_inherits_toapis_instead_of_resetting_bandianwa():
    selected = _select_visual_variant_api_route(
        [
            {
                "api_route": "toapis:gpt-image-2",
                "visual_api": {
                    "provider": "toapis",
                    "provider_failures": {
                        "bandianwa": {"retry_budget_exhausted": True},
                    },
                },
            }
        ],
        default_route="bandianwa:gpt-image-2",
        toapis_available=True,
    )

    assert selected == "toapis:gpt-image-2"


def test_failed_generated_character_anchor_invalidates_all_dependent_references():
    observed = {1, 2, 3, 4}

    assert _expand_failed_visual_reference_dependencies(
        {1, 3},
        observed,
        has_uploaded_character_anchor=False,
    ) == observed
    assert _expand_failed_visual_reference_dependencies(
        {1, 3},
        observed,
        has_uploaded_character_anchor=True,
    ) == {1, 3}
    assert _expand_failed_visual_reference_dependencies(
        {1, 3},
        observed,
        has_uploaded_character_anchor=False,
        reference_one_identity_failed=False,
    ) == {1, 3}
    assert _expand_failed_visual_reference_dependencies(
        {3},
        observed,
        has_uploaded_character_anchor=False,
    ) == {3}


def test_long_visual_plan_is_partitioned_into_ordered_readable_boards():
    packet = _packet()
    plan = [
        {
            "index": index,
            "segment": 1 + ((index - 1) // 4),
            "description": f"Chronological beat {index}",
            "roles": ["character_anchor", "action_anchor"],
        }
        for index in range(1, 14)
    ]
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = len(plan)
    ticket["reference_plan"] = plan

    specs = visual_board_specs(packet)
    prompts = build_visual_api_prompts(packet)

    assert [item["count"] for item in specs] == [7, 6]
    assert [(item["global_start_index"], item["global_end_index"]) for item in specs] == [(1, 7), (8, 13)]
    assert all(item["board_count"] == 2 for item in specs)
    assert len(prompts) == 2
    assert "Chronological beat 7" in prompts[0][0]
    assert "Chronological beat 8" not in prompts[0][0]
    assert "Chronological beat 8" in prompts[1][0]


def test_short_product_video_renders_one_native_portrait_reference_per_segment():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 4
    ticket["reference_plan"] = [
        {"index": 1, "segment": 1, "description": "Adult closes laptop", "roles": ["action_anchor"]},
        {"index": 2, "segment": 2, "description": "Adult sees a missed text", "roles": ["action_anchor"]},
        {"index": 3, "segment": 3, "description": "Adult lowers lamp", "roles": ["action_anchor"]},
        {"index": 4, "segment": 4, "description": "Closed MYUPONA bottle rests on side table", "roles": ["action_anchor"], "requires_product_reference": True},
    ]

    specs = visual_board_specs(packet)
    prompts = build_visual_api_prompts(packet)

    assert len(specs) == 4
    assert all(spec["count"] == 1 and spec["size"] == "1024x1792" for spec in specs)
    assert "collage, grid, split screen" in prompts[0][0]
    assert all(
        "Generate one 9:16 reference image for scene" in prompt
        for prompt, _spec in prompts
    )
    assert all(
        not content_factory_api._IMAGE_PROMPT_PRODUCT_TRIGGER_RE.search(prompt)
        for prompt, _spec in prompts[:3]
    )
    assert content_factory_api._IMAGE_PROMPT_PRODUCT_TRIGGER_RE.search(prompts[3][0])


def test_individual_references_follow_project_landscape_aspect_ratio():
    packet = _packet()
    packet["video_aspect_ratio"] = "16:9"
    packet["render_reference_images_individually"] = True
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 1
    ticket["reference_plan"] = [{
        "index": 1,
        "segment": 1,
        "description": "An adult closes a laptop at a wide office desk.",
        "roles": ["scene_anchor", "action_anchor"],
        "generation_mode": "generate",
    }]

    specs = visual_board_specs(packet)
    prompts = build_visual_api_prompts(packet)

    assert specs[0]["size"] == "1792x1024"
    assert specs[0]["aspect_ratio"] == "16:9"
    assert "one native 16:9 image" in prompts[0][0]


def test_signed_source_assets_guide_generation_but_every_row_is_rendered():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket.update({
        "source": "directed_production_plan",
        "reference_image_count": 3,
        "final_reference_count": 3,
        "reference_plan": [
            {
                "index": 1,
                "reference_id": "uploaded-character",
                "segment": 1,
                "segments": [1],
                "description": "Exact uploaded adult character anchor.",
                "roles": ["character_anchor"],
                "source_asset_refs": ["asset:41"],
                "generation_mode": "generate",
            },
            {
                "index": 2,
                "reference_id": "generated-action",
                "segment": 2,
                "segments": [2],
                "description": "Adult closes a laptop in one room.",
                "roles": ["action_anchor"],
                "source_asset_refs": [],
                "generation_mode": "generate",
            },
            {
                "index": 3,
                "reference_id": "uploaded-product",
                "segment": 3,
                "segments": [3],
                "description": "Exact uploaded package authority.",
                "roles": ["action_anchor"],
                "source_asset_refs": ["asset:42"],
                "generation_mode": "generate",
                "requires_product_reference": True,
            },
        ],
    })

    specs = visual_board_specs(packet)
    prompts = build_visual_api_prompts(packet)

    assert len(specs) == 3
    assert [spec["global_start_index"] for spec in specs] == [1, 2, 3]
    assert all(
        row["generation_mode"] == "generate"
        for spec in specs
        for row in spec["plan"]
    )
    assert len(prompts) == 3


def test_signed_plan_keeps_distinct_reference_scenes_that_share_one_segment():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    creative = packet["previous_outputs"]["MEDIA_DESIGN"]
    creative["shot_plan"] = [{
        "segment": 3,
        "visual_state": (
            "Begin at the entry table, then cut to the warm bedside table."
        ),
    }]
    creative["visual_job_ticket"] = {
        "source": "directed_production_plan",
        "reference_image_count": 2,
        "final_reference_count": 2,
        "reference_plan": [
            {
                "index": 1,
                "reference_id": "seal-insert",
                "segment": 3,
                "segments": [3],
                "description": (
                    "Close top-down entry-table insert. One adult hand presses "
                    "a plain envelope flap closed."
                ),
                "roles": ["scene_anchor", "action_anchor"],
                "source_asset_refs": [],
                "generation_mode": "generate",
            },
            {
                "index": 2,
                "reference_id": "bedside-anchor",
                "segment": 3,
                "segments": [3, 4],
                "description": (
                    "Warm bedside-table scene with a sealed plain envelope and "
                    "a small glass of water."
                ),
                "roles": ["scene_anchor", "action_anchor"],
                "source_asset_refs": [],
                "generation_mode": "generate",
            },
        ],
    }

    prompts = [prompt for prompt, _spec in build_visual_api_prompts(packet)]

    assert "entry-table insert" in prompts[0]
    assert "bedside-table scene" not in prompts[0]
    assert "bedside-table scene" in prompts[1]
    assert "entry-table insert" not in prompts[1]
    assert "then cut" not in prompts[0].lower()
    assert "then cut" not in prompts[1].lower()


def test_final_assets_orders_generated_native_references(tmp_path):
    generated_one = tmp_path / "visual-preview-api-reference-01.png"
    generated_two = tmp_path / "visual-preview-api-reference-02.png"
    source_three = tmp_path / "reference-03-source-42.png"
    for path in (generated_one, generated_two, source_three):
        Image.new("RGB", (32, 48), "navy").save(path)

    assets = [
        SimpleNamespace(
            id=11,
            file_path=str(generated_one),
            meta_json={
                "variant_index": 41,
                "reference_index": 1,
                "capture_origin": "assistant_generated",
                "outbox_path": str(generated_one),
            },
        ),
        SimpleNamespace(
            id=12,
            file_path=str(generated_two),
            meta_json={
                "variant_index": 41,
                "reference_index": 2,
                "capture_origin": "assistant_generated",
                "outbox_path": str(generated_two),
            },
        ),
        SimpleNamespace(
            id=13,
            file_path=str(source_three),
            meta_json={
                "variant_index": 41,
                "reference_index": 3,
                "generation_mode": "generate",
                "capture_origin": "assistant_generated",
                "outbox_path": str(source_three),
            },
        ),
    ]
    plan = [
        {"index": 1, "generation_mode": "generate", "source_asset_refs": []},
        {"index": 2, "generation_mode": "generate", "source_asset_refs": []},
        {
            "index": 3,
            "generation_mode": "generate",
            "source_asset_refs": [],
        },
    ]

    ordered = _ordered_native_reference_assets(
        assets,
        plan,
        evidence_files={
            generated_one.name,
            generated_two.name,
            source_three.name,
        },
        active_variant=41,
    )

    assert [asset.id for asset in ordered] == [11, 12, 13]


def test_signed_plan_reference_count_is_not_overridden_by_legacy_four_image_copy():
    signed_design = {
        "visual_job_ticket": {
            "source": "directed_production_plan",
            "reference_image_count": 3,
            "final_reference_count": 3,
            "reference_plan": [
                {"index": 1, "generation_mode": "generate"},
                {"index": 2, "generation_mode": "generate"},
                {
                    "index": 3,
                    "generation_mode": "generate",
                    "source_asset_refs": ["asset:42"],
                },
            ],
        }
    }

    assert content_factory_tasks._authoritative_reference_count(
        signed_design,
        review_result={"reference_image_count": 3},
        fallback_texts=["Legacy template says 2x2 and four panels."],
    ) == 3


def test_product_reference_still_requires_generated_scene_call():
    packet = _packet()
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket.update({
        "source": "directed_production_plan",
        "reference_image_count": 1,
        "final_reference_count": 1,
        "reference_plan": [{
            "index": 1,
            "reference_id": "uploaded-product",
            "segment": 1,
            "description": "Exact uploaded product package.",
            "roles": ["action_anchor"],
            "source_asset_refs": ["asset:42"],
            "generation_mode": "generate",
            "requires_product_reference": True,
        }],
    })

    specs = visual_board_specs(packet)
    prompts = build_visual_api_prompts(packet)
    assert len(specs) == 1
    assert len(prompts) == 1


def test_individual_visual_prompt_includes_only_its_targeted_repair():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["visual_repair_instruction"] = (
        "Regenerate only the failed native 9:16 references. "
        "Reference 2: The adult friend must look toward the empty cushion. "
        "Do not repeat the rejected state: the friend looks at the phone. "
        "Reference 4: Remove the unrelated blue box and leave the side table clear. "
        "Preserve all passed references and the established cast."
    )
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 4
    ticket["reference_plan"] = [
        {
            "index": index,
            "segment": index,
            "description": f"Adult story beat {index} in the same living room.",
            "roles": ["action_anchor"],
        }
        for index in range(1, 5)
    ]

    prompts = [prompt for prompt, _spec in build_visual_api_prompts(packet)]

    assert "MANDATORY REPAIR OVERRIDE" not in prompts[0]
    assert "adult friend must look toward the empty cushion" in prompts[1]
    assert "unrelated blue box" not in prompts[1]
    assert "MANDATORY REPAIR OVERRIDE" not in prompts[2]
    assert "Remove the unrelated blue box" in prompts[3]
    assert "empty cushion" not in prompts[3]


def test_long_video_reference_plan_covers_every_segment_before_board_partition():
    result = {
        "complete_video_script": {
            "segments": [
                {
                    "segment_index": index,
                    "story_function": f"Story beat {index}",
                    "visual_action": f"Character performs action {index}",
                }
                for index in range(1, 10)
            ],
        },
        "visual_job_ticket": {
            "reference_image_count": 7,
            "reference_plan": [
                {
                    "index": index,
                    "segment": 1,
                    "description": f"Opening detail {index}",
                    "roles": ["character_anchor", "action_anchor"],
                }
                for index in range(1, 8)
            ],
        },
    }

    normalized = _ensure_reference_plan_segment_coverage(
        result,
        product_allowed=False,
    )
    ticket = normalized["visual_job_ticket"]
    covered = {int(item["segment"]) for item in ticket["reference_plan"]}
    segment_order = [int(item["segment"]) for item in ticket["reference_plan"]]

    assert covered == set(range(1, 10))
    assert segment_order == sorted(segment_order)
    assert ticket["reference_image_count"] == len(ticket["reference_plan"])
    assert ticket["final_reference_count"] == len(ticket["reference_plan"])
    assert ticket["reference_image_count"] > 7
    assert ticket["board_count"] == 3
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"] = normalized
    specs = visual_board_specs(packet)
    assert [item["count"] for item in specs] == [5, 5, 5]
    assert all(item["count"] <= 7 for item in specs)


def test_benchmark_intent_classification_distinguishes_exact_and_adaptive_modes():
    assert benchmark_imitation_mode("1:1 精准复刻对标视频", has_benchmark=True) == "exact"
    assert benchmark_imitation_mode("参考这个视频的节奏做差异化仿写", has_benchmark=True) == "adaptive"
    assert benchmark_imitation_mode("1:1 精准复刻", has_benchmark=False) == "none"


def test_creative_review_repair_policy_stops_outer_loop_after_bounded_replans():
    state: dict = {}

    first = _creative_review_rejection_policy(
        state,
        variant_index=1,
        repair_limit=2,
        replan_limit=2,
    )
    assert first["next_stage"] == "VISUAL_PREVIEW"
    assert first["exhausted"] is False

    second = _creative_review_rejection_policy(
        state,
        variant_index=1,
        repair_limit=2,
        replan_limit=2,
    )
    assert second["next_stage"] == "PRODUCTION_PLAN"
    assert second["replan_count"] == 1
    assert second["exhausted"] is False

    _creative_review_rejection_policy(
        state,
        variant_index=1,
        repair_limit=2,
        replan_limit=2,
    )
    fourth = _creative_review_rejection_policy(
        state,
        variant_index=1,
        repair_limit=2,
        replan_limit=2,
    )
    assert fourth["next_stage"] == "PRODUCTION_PLAN"
    assert fourth["replan_count"] == 2
    assert fourth["exhausted"] is True


def test_split_quality_accepts_provider_portrait_board_cells():
    accepted, reason = _split_panel_quality_ok(
        {"source_width": 232, "source_height": 824},
        index=1,
    )
    assert accepted is True
    assert reason is None

    accepted, reason = _split_panel_quality_ok(
        {"source_width": 199, "source_height": 824},
        index=1,
    )
    assert accepted is False
    assert "too small" in str(reason)


def test_split_quality_accepts_landscape_cells_for_landscape_project():
    accepted, reason = _split_panel_quality_ok(
        {"source_width": 824, "source_height": 463},
        index=1,
        aspect_ratio="16:9",
    )
    assert accepted is True
    assert reason is None


def test_visual_prompt_resolves_string_selected_concept_without_crashing():
    packet = _packet()
    creative = packet["previous_outputs"]["MEDIA_DESIGN"]
    creative["selected_concept"] = "v13_screen_glow_intervention"
    creative["concepts"] = [
        {
            "concept_id": "v13_screen_glow_intervention",
            "title": "Your Screen Called a Meeting",
            "hook": "A tired creator catches her reflection in the monitor glow.",
        }
    ]

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["count"] == 7
    assert "Ordered action panel 1" in prompt
    assert "Your Screen Called a Meeting" not in prompt
    assert "monitor glow" not in prompt
    assert "ORDERED PANEL SHOTS" in prompt


def test_visual_prompt_ignores_non_object_ticket_instead_of_throwing():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = "invalid"

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["count"] == 1
    assert "Generate one 9:16 reference image for scene 1" in prompt
    assert not re.search(r"\b(?:board|panel|storyboard)\b", prompt, flags=re.IGNORECASE)


def test_video_segment_prompt_alias_is_normalized_without_losing_text():
    assert _video_segment_prompt_text({"prompt": "Canonical local segment."}) == (
        "Canonical local segment."
    )
    assert _video_segment_prompt_text({"short_prompt": "Provider local segment."}) == (
        "Provider local segment."
    )




def test_creative_blueprint_locks_complete_dialogue_and_voice_across_segments():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 30,
            "video_duration_max_seconds": 30,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 30,
            "target_edit_duration_seconds": 25,
            "story_outline": {
                "opening": "A roommate discovers a ridiculous midnight hobby.",
                "development": "The pair race through a comic investigation.",
                "resolution": "They solve the mystery and laugh together.",
            },
            "segments": [
                {
                    "segment_index": index,
                    "duration_seconds": 10,
                    "story_function": goal,
                    "visual_action": action,
                    "dialogue_lines": [{"speaker_id": "HOST", "line": line}],
                    "end_bridge": bridge,
                }
                for index, goal, action, line, bridge in (
                    (1, "hook", "She opens the door in shock.", "What are you building in here?", "She points inside."),
                    (2, "escalation", "They inspect the blanket maze.", "This is the strangest fort I have seen.", "They lift one pillow."),
                    (3, "payoff", "The fort collapses harmlessly.", "Mystery solved, and nobody mention the pillows.", "They laugh together."),
                )
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "HOST",
            "speakers": [{
                "speaker_id": "HOST",
                "name": "Host",
                "gender": "female",
                "timbre": "warm and bright",
                "pitch": "medium",
                "accent": "US English",
                "delivery": "fast but intelligible",
                "speech_rate_wpm": 180,
            }],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    assert _project_uses_product(project) is False
    assert [item["duration_seconds"] for item in normalized["complete_video_script"]["segments"]] == [10, 10, 10]
    assert all(item["dialogue_lines"][0]["speaker_id"] == "HOST" for item in normalized["complete_video_script"]["segments"])
    assert {
        int(item["segment"])
        for item in normalized["visual_job_ticket"]["reference_plan"]
    } == {1, 2, 3}

    result["complete_video_script"]["segments"][0]["spoken_copy"] = (
        result["complete_video_script"]["segments"][0].pop("dialogue_lines")[0]["line"]
    )
    rebuilt = _normalize_creative_video_blueprint(project, result)
    assert rebuilt["complete_video_script"]["segments"][0]["dialogue_lines"][0]["line"] == (
        "What are you building in here?"
    )

    first_segment = result["complete_video_script"]["segments"][0]
    first_segment.pop("spoken_copy")
    first_segment["speaker_assignments"] = [{
        "speaker_id": "HOST",
        "spoken_copy": "What are you building in here?",
    }]
    rebuilt = _normalize_creative_video_blueprint(project, result)
    assert rebuilt["complete_video_script"]["segments"][0]["dialogue_lines"][0]["line"] == (
        "What are you building in here?"
    )


def test_creative_blueprint_normalizes_segment_array_and_incomplete_voice_bible():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create a complete short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 20,
            "video_duration_max_seconds": 20,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": [
            {
                "segment_index": 1,
                "duration_seconds": 10,
                "story_function": "hook and setup",
                "visual_action": "The host finds a mysterious box.",
                "dialogue_lines": [{"speaker_id": "HOST", "line": "Who left this here?"}],
                "end_bridge": "The host reaches for the lid.",
            },
            {
                "segment_index": 2,
                "duration_seconds": 10,
                "story_function": "reveal and resolution",
                "visual_action": "The harmless surprise is revealed.",
                "dialogue_lines": [{"speaker_id": "HOST", "line": "Okay, that is actually brilliant."}],
                "end_bridge": "The host smiles at camera.",
            },
        ],
        "voice_bible": {"speakers": [{"speaker_id": "HOST", "name": "Host"}]},
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    script = normalized["complete_video_script"]
    voice = normalized["voice_bible"]["speakers"][0]
    assert script["duration_seconds"] == 20
    assert script["story_outline"] == {
        "opening": "hook and setup",
        "development": "hook and setup",
        "resolution": "reveal and resolution",
    }
    assert voice["speaker_id"] == "HOST"
    assert voice["speech_rate"] == 165
    assert voice["accent"] == "en-US"


def test_complete_browser_response_is_durable_before_business_validation():
    class FakeDb:
        commits = 0

        def commit(self):
            self.commits += 1

    stage = SimpleNamespace(response_text=None, chat_url=None, output_json={})
    response = json.dumps({
        "schema_version": "1.0",
        "project_id": "cf_test",
        "stage": "CREATIVE",
        "status": "PASS",
        "result": {"complete_video_script": []},
        "evidence": {},
        "issues": [],
        "repair_brief": None,
        "next_stage": "VISUAL_PREVIEW",
    })

    stored = _persist_completed_stage_capture(
        FakeDb(),
        stage,
        project_key="cf_test",
        stage="CREATIVE",
        response_text=response,
        chat_url="https://chatgpt.com/c/test",
    )

    assert stored == response
    assert stage.response_text == response
    assert stage.chat_url == "https://chatgpt.com/c/test"
    assert stage.output_json["durable_response_capture"]["validated_envelope"] is True
    assert len(stage.output_json["durable_response_capture"]["sha256"]) == 64


def test_fresh_semantic_generation_replaces_a_longer_rejected_capture():
    class FakeDb:
        commits = 0

        def commit(self):
            self.commits += 1

    old_response = json.dumps({
        "schema_version": "1.0",
        "project_id": "cf_test",
        "stage": "CREATIVE",
        "status": "PASS",
        "result": {
            "selected_concept": {
                "title": "Rejected long child premise",
                "description": "son " * 500,
            },
        },
        "next_stage": "VISUAL_PREVIEW",
    })
    new_response = json.dumps({
        "schema_version": "1.0",
        "project_id": "cf_test",
        "stage": "CREATIVE",
        "status": "PASS",
        "result": {
            "selected_concept": {
                "title": "Two adult coworkers",
            },
        },
        "next_stage": "VISUAL_PREVIEW",
    })
    stage = SimpleNamespace(
        response_text=old_response,
        chat_url=None,
        output_json={},
    )

    stored = _persist_completed_stage_capture(
        FakeDb(),
        stage,
        project_key="cf_test",
        stage="CREATIVE",
        response_text=new_response,
        chat_url=None,
        replace_with_incoming_complete=True,
    )

    assert stored == new_response
    assert stage.response_text == new_response
    assert stage.output_json["durable_response_capture"][
        "replaced_prior_complete_capture"
    ] is True


def test_later_attempt_reuses_complete_prior_response_without_crossing_restart_or_variant():
    response = json.dumps({
        "schema_version": "1.0",
        "project_id": "cf_test",
        "stage": "CREATIVE",
        "status": "PASS",
        "result": {"complete_video_script": []},
        "evidence": {},
        "issues": [],
        "repair_brief": None,
        "next_stage": "VISUAL_PREVIEW",
    })
    replay_digest = "same-business-input"
    prior = SimpleNamespace(
        id=41,
        input_json={
            "variant_index": 2,
            "replay_context_digest": replay_digest,
            "restart_generation": 4,
        },
        response_text=response,
        chat_url="https://chatgpt.com/c/prior",
        created_at=content_factory_tasks.datetime(2026, 7, 17, 12, 0, 0),
    )

    class FakeQuery:
        def filter(self, *args):
            return self

        def order_by(self, *args):
            return self

        def limit(self, value):
            return self

        def all(self):
            return [prior]

    class FakeDb:
        def query(self, model):
            return FakeQuery()

    project = SimpleNamespace(
        project_key="cf_test",
        state_json={"last_restart": {"at": "2026-07-17T11:59:00"}},
    )
    current = SimpleNamespace(
        id=42,
        project_id=7,
        stage="CREATIVE",
        input_json={"variant_index": 2, "restart_generation": 4},
    )

    recovered = _recover_prior_stage_attempt_response(
        FakeDb(),
        project,
        current,
        replay_context_digest=replay_digest,
    )

    assert recovered == (response, "https://chatgpt.com/c/prior", 41)

    current.input_json = {"variant_index": 3, "restart_generation": 4}
    assert _recover_prior_stage_attempt_response(
        FakeDb(),
        project,
        current,
        replay_context_digest=replay_digest,
    ) is None

    current.input_json = {"variant_index": 2, "restart_generation": 4}
    project.state_json = {"last_restart": {"at": "2026-07-17T12:01:00"}}
    assert _recover_prior_stage_attempt_response(
        FakeDb(),
        project,
        current,
        replay_context_digest=replay_digest,
    ) is None

    project.state_json = {"last_restart": {"at": "2026-07-17T11:59:00"}}
    assert _recover_prior_stage_attempt_response(
        FakeDb(),
        project,
        current,
        replay_context_digest="new-visual-board",
    ) is None

    current.input_json = {"variant_index": 2, "restart_generation": 5}
    assert _recover_prior_stage_attempt_response(
        FakeDb(),
        project,
        current,
        replay_context_digest=replay_digest,
    ) is None

    current.input_json = {"variant_index": 2, "restart_generation": 4}
    failed_envelope = json.loads(response)
    failed_envelope["status"] = "FAIL"
    failed_envelope["issues"] = [{"code": "OLD_PROMPT_REJECTED"}]
    prior.response_text = json.dumps(failed_envelope)
    assert _recover_prior_stage_attempt_response(
        FakeDb(),
        project,
        current,
        replay_context_digest=replay_digest,
    ) is None


def test_stage_replay_context_digest_changes_when_selected_visual_changes():
    packet = _packet()
    packet["browser_assets"] = [{"id": 101, "path": "/tmp/visual-a.png"}]
    packet["previous_outputs"] = {"VISUAL_PREVIEW": {"asset_ids": [101]}}
    first = _stage_replay_context_digest(packet, "CREATIVE_REVIEW")

    packet["browser_assets"] = [{"id": 102, "path": "/tmp/visual-b.png"}]
    packet["previous_outputs"] = {"VISUAL_PREVIEW": {"asset_ids": [102]}}
    second = _stage_replay_context_digest(packet, "CREATIVE_REVIEW")

    assert first != second


def test_current_review_contract_failure_is_a_fresh_semantic_retry_even_after_replay():
    error = ValueError(
        "CREATIVE_REVIEW reference_checks contract incomplete"
    )

    assert _is_text_api_output_validation_failure(
        api_route="toapis:text",
        stage="CREATIVE_REVIEW",
        replayed_response=False,
        error=error,
    ) is True
    assert _is_text_api_output_validation_failure(
        api_route="toapis:text",
        stage="CREATIVE_REVIEW",
        replayed_response=True,
        error=error,
    ) is False
    assert _is_semantic_text_payload_failure(
        api_route="toapis:text",
        stage="CREATIVE_REVIEW",
        error=error,
    ) is True


def test_edit_package_contract_failure_is_validation_retry_not_semantic_replan():
    error = ValueError(
        "EDIT_PACKAGE required result field missing"
    )

    assert _is_text_api_output_validation_failure(
        api_route="toapis:text",
        stage="EDIT_PACKAGE",
        replayed_response=False,
        error=error,
    ) is True
    assert _is_text_api_output_validation_failure(
        api_route="toapis:text",
        stage="EDIT_PACKAGE",
        replayed_response=True,
        error=error,
    ) is False
    assert _is_semantic_text_payload_failure(
        api_route="toapis:text",
        stage="EDIT_PACKAGE",
        error=error,
    ) is False


def test_fresh_creative_plan_clears_bounded_visual_repair_state():
    cleaned = _clear_creative_visual_recovery_state({
        "creative_replan_counts": {"1": 2},
        "creative_review_visual_repair_counts": {"1": 3},
        "creative_visual_replan_exhausted": {"variant_index": 1},
        "api_browser_cycle_exhausted": {"variant_index": 1},
        "last_creative_review": {"approved_for_split": False},
        "last_visual_preview_asset_recovered": {"asset_id": 99},
        "completed_variants": [2],
    })

    assert "creative_replan_counts" not in cleaned
    assert "creative_review_visual_repair_counts" not in cleaned
    assert "creative_visual_replan_exhausted" not in cleaned
    assert "api_browser_cycle_exhausted" not in cleaned
    assert cleaned["completed_variants"] == [2]


@pytest.mark.parametrize("status", [502, 503, 504, 520, 522, 524])
def test_text_api_cloudflare_failures_use_bounded_transport_retry(status):
    error = RuntimeError(
        f"Server error '{status} <none>' for url "
        "'https://toapis.com/v1/chat/completions'; a timeout occurred"
    )

    assert _stage_retry_delay(error, 1) == 15
    assert _stage_retry_delay(error, 4) == 300
    assert _stage_retry_delay(error, 5) is None


def test_hermes_provider_failures_use_code_specific_durable_backoff():
    transient = RuntimeError(
        "Hermes content model provider is temporarily unavailable."
    )
    transient.code = "HERMES_UPSTREAM_EXECUTION_FAILED"
    quota = RuntimeError(
        "Hermes content model provider is temporarily unavailable."
    )
    quota.code = "HERMES_UPSTREAM_QUOTA"
    auth = RuntimeError(
        "Hermes content model provider is temporarily unavailable."
    )
    auth.code = "HERMES_UPSTREAM_AUTH"

    assert _stage_retry_delay(transient, 1) == 15
    assert _stage_retry_delay(transient, 4) == 300
    assert _stage_retry_delay(transient, 5) is None
    assert _stage_retry_delay(quota, 1) == 21600
    assert _stage_retry_delay(quota, 2) == 86400
    assert _stage_retry_delay(quota, 3) is None
    assert _stage_retry_delay(auth, 1) is None


def test_exact_benchmark_blueprint_rebuilds_every_subtitle_cue_once_in_order():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="1:1 精准复刻对标视频",
        state_json={
            "benchmark_video_analysis": {
                "status": "success",
                "segments": [
                    {"cue_index": 1, "start": "00:00:00,000", "end": "00:00:03,000", "text": "First cue."},
                    {"cue_index": 2, "start": "00:00:03,000", "end": "00:00:08,000", "text": "Second cue."},
                    {"cue_index": 3, "start": "00:00:08,000", "end": "00:00:14,000", "text": "Third cue."},
                ],
            }
        },
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 20,
            "video_duration_max_seconds": 20,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 20,
            "target_edit_duration_seconds": 16,
            "story_outline": {
                "opening": "The first benchmark beat opens the story.",
                "development": "The middle benchmark beat changes direction.",
                "resolution": "The final benchmark beat resolves it.",
            },
            "segments": [
                {
                    "segment_index": 1,
                    "duration_seconds": 10,
                    "story_function": "hook and setup",
                    "visual_action": "The host enters and reacts.",
                    "dialogue_lines": [{"speaker_id": "HOST", "line": "First cue and second cue happen here."}],
                    "benchmark_cue_indices": [1, 2],
                    "end_bridge": "The host turns toward the next beat.",
                },
                {
                    "segment_index": 2,
                    "duration_seconds": 10,
                    "story_function": "resolution",
                    "visual_action": "The host completes the action.",
                    "dialogue_lines": [{"speaker_id": "HOST", "line": "Third cue closes the sequence."}],
                    "benchmark_cue_indices": [3],
                    "end_bridge": "The scene ends cleanly.",
                },
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "HOST",
            "speakers": [{
                "speaker_id": "HOST",
                "name": "Host",
                "gender": "female",
                "timbre": "clear and warm",
                "pitch": "medium",
                "accent": "US English",
                "delivery": "quick and natural",
                "speech_rate_wpm": 180,
            }],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)
    assert normalized["benchmark_imitation_mode"] == "exact"
    assert [
        cue
        for segment in normalized["complete_video_script"]["segments"]
        for cue in segment["benchmark_cue_indices"]
    ] == [1, 2, 3]

    result["complete_video_script"]["segments"][1]["benchmark_cue_indices"] = [2, 3]
    rebuilt = _normalize_creative_video_blueprint(project, result)
    rebuilt_segments = rebuilt["complete_video_script"]["segments"]
    assert [
        cue
        for segment in rebuilt_segments
        for cue in segment["benchmark_cue_indices"]
    ] == [1, 2, 3]
    assert [
        line["line"]
        for segment in rebuilt_segments
        for line in segment["dialogue_lines"]
    ] == ["First cue.", "Second cue.", "Third cue."]


def test_creative_blueprint_normalizes_shorthand_script_voice_and_shot_plan():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create an original short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 20,
            "video_duration_max_seconds": 20,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 20,
            "segments": [
                {"time": "0-10s", "script": "The host finds the strange clue."},
                {"time": "10-20s", "script": "The host solves it and closes the story."},
            ],
        },
        "voice_bible": {
            "voice_type": "Energetic US TikTok storyteller",
            "tone": "curious and playful",
            "speed": "fast but intelligible",
        },
        "shot_plan": [
            {
                "purpose": "opening hook",
                "visual": "The host enters and discovers a clue.",
            },
            {
                "purpose": "resolution",
                "visual": "The host reveals the answer and exits.",
            },
        ],
        "visual_job_ticket": {"reference_image_count": 4},
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    script = normalized["complete_video_script"]
    assert script["story_outline"] == {
        "opening": "The host finds the strange clue.",
        "development": "The host finds the strange clue.",
        "resolution": "The host solves it and closes the story.",
    }
    assert [item["story_function"] for item in script["segments"]] == [
        "opening hook",
        "resolution",
    ]
    assert [item["visual_action"] for item in script["segments"]] == [
        "The host enters and discovers a clue.",
        "The host reveals the answer and exits.",
    ]
    assert [
        item["dialogue_lines"][0]["line"] for item in script["segments"]
    ] == [
        "The host finds the strange clue.",
        "The host solves it and closes the story.",
    ]
    assert normalized["voice_bible"]["primary_speaker_id"] == "primary_narrator"


def test_creative_blueprint_visual_state_overrides_nonempty_abstract_segment_action():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create an original short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 20,
            "video_duration_max_seconds": 20,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 20,
            "opening": "A relationship ritual has already disappeared.",
            "development": "The specific admission exposes the loss.",
            "resolution": "He sees what the pattern is costing.",
            "segments": [
                {
                    "segment": 1,
                    "duration_seconds": 10,
                    "story_function": "Loss hook.",
                    "visual_action": "Reveal the relationship ritual already being lost.",
                    "script": "He used to wait for me. Now he leaves alone.",
                },
                {
                    "segment": 2,
                    "duration_seconds": 10,
                    "story_function": "Knife-twist.",
                    "visual_action": "Turn the encounter into a painful admission.",
                    "script": "He stopped asking because I was never really there.",
                },
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "narrator",
            "speakers": [{
                "speaker_id": "narrator",
                "name": "Narrator",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": 1,
                "visual_state": "Daniel stands in the office doorway while Miles carries keys toward the front door.",
                "camera": "Fast backward tracking.",
            },
            {
                "segment": 2,
                "visual_state": "Miles rests one hand on the closed bedroom doorknob while facing Daniel.",
                "camera": "Slow push-in.",
            },
        ],
        "visual_job_ticket": {
            "reference_image_count": 2,
            "reference_plan": [
                {"index": 1, "segment": 1, "description": "Reveal the relationship ritual."},
                {"index": 2, "segment": 2, "description": "Deliver the painful admission."},
            ],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)
    segments = normalized["complete_video_script"]["segments"]
    references = normalized["visual_job_ticket"]["reference_plan"]

    assert segments[0]["visual_action"].startswith("Daniel stands in the office doorway")
    assert segments[1]["visual_action"].startswith("Miles rests one hand")
    assert "office doorway" in references[0]["description"]
    assert "closed bedroom doorknob" in references[1]["description"]
    assert all("tracking" not in item["description"].lower() for item in references)


def test_creative_blueprint_normalizes_ordered_segment_assignments_from_api():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 40,
            "opening": "A father discovers the family moment he missed.",
            "development": "His daughter's quiet response exposes what is being lost.",
            "resolution": "He chooses a clean end-of-day transition.",
            "ordered_segment_assignments": [
                {
                    "segment": index,
                    "start_seconds": (index - 1) * 10,
                    "end_seconds": index * 10,
                    "speaker_id": "NARRATOR",
                    "dialogue": line,
                }
                for index, line in enumerate((
                    "At midnight, he found the birthday card she had stopped waiting to show him.",
                    "At breakfast, she put away another drawing and said he always looked tired lately.",
                    "That hurt. He was becoming the dad who missed what mattered, so he changed his evening transition.",
                    "He lowered the lights, put down the phone, and gave the day a clear ending.",
                ), 1)
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [{
                "speaker_id": "NARRATOR",
                "name": "Narrator",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": index,
                "story_function": function,
                "visuals": visual,
            }
            for index, function, visual in (
                (1, "loss hook", "He discovers a facedown handmade card."),
                (2, "knife-twist moment", "His daughter closes a drawer over her drawing."),
                (3, "identity loss and bridge", "He recognizes what the pattern is costing."),
                (4, "resolution", "He creates a clean end-of-day transition."),
            )
        ],
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    script = normalized["complete_video_script"]
    assert script["duration_seconds"] == 40
    assert script["story_outline"] == {
        "opening": "A father discovers the family moment he missed.",
        "development": "His daughter's quiet response exposes what is being lost.",
        "resolution": "He chooses a clean end-of-day transition.",
    }
    assert [item["duration_seconds"] for item in script["segments"]] == [10, 10, 10, 10]
    assert [item["story_function"] for item in script["segments"]] == [
        "loss hook",
        "knife-twist moment",
        "identity loss and bridge",
        "resolution",
    ]
    assert script["segments"][1]["visual_action"] == (
        "His daughter closes a drawer over her drawing."
    )
    assert all(
        item["dialogue_lines"][0]["speaker_id"] == "NARRATOR"
        for item in script["segments"]
    )


def test_creative_blueprint_uses_visual_beats_instead_of_story_taxonomy_labels():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    purposes = (
        "Loss hook",
        "Knife-twist moment",
        "Recognition and routine bridge",
        "Resolution",
    )
    visual_beats = (
        [
            "Dana sits alone on the sofa with a blanket looped around her ankle.",
            "A half-finished welcome sign and two unused tickets rest on the table.",
        ],
        [
            "Dana's phone visibly shows HOST SLOT REASSIGNED.",
            "She lowers the phone beside the unused tickets.",
        ],
        [
            "Dana places the phone face-down and folds the welcome sign.",
            "The amber lamp is visibly dimmed.",
        ],
        [
            "Dana settles under the blanket.",
            "The television remains paused on a calm title screen.",
        ],
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 40,
            "opening": "Dana's movie-night ritual is disappearing.",
            "development": "A reassigned host notice makes the loss concrete.",
            "resolution": "Dana creates a deliberate end-of-day transition.",
            "segment_assignments": [
                {
                    "segment": index,
                    "start_seconds": (index - 1) * 10,
                    "end_seconds": index * 10,
                    "spoken_copy": line,
                }
                for index, line in enumerate((
                    "Dana once made Friday movie nights feel special.",
                    "Then one message reassigned the role she loved.",
                    "The loss was the dependable host she had been.",
                    "She gave the day a clear and calmer ending.",
                ), 1)
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [{
                "speaker_id": "NARRATOR",
                "name": "Narrator",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": index,
                "purpose": purpose,
                "visual_beats": beats,
            }
            for index, (purpose, beats) in enumerate(
                zip(purposes, visual_beats),
                1,
            )
        ],
        "visual_job_ticket": {
            "reference_image_count": 4,
            "reference_plan": [
                {
                    "index": 1,
                    "segment": 1,
                    "description": (
                        "Dana sits alone on the sofa with the blanket looped around her ankle; "
                        "the unfinished welcome sign and two unused tickets rest on the table."
                    ),
                },
                {
                    "index": 2,
                    "segment": 2,
                    "description": (
                        "Dana sits on the same sofa holding a phone that visibly reads "
                        "HOST SLOT REASSIGNED beside the unused tickets."
                    ),
                },
                {
                    "index": 3,
                    "segment": 3,
                    "description": (
                        "Dana sits beside the visibly dimmed amber lamp; her phone is face-down "
                        "and the welcome sign is folded beside the tickets."
                    ),
                },
                {
                    "index": 4,
                    "segment": 4,
                    "description": (
                        "Dana sits under the blanket while the television remains paused "
                        "on a calm title screen."
                    ),
                }
            ],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)
    segments = normalized["complete_video_script"]["segments"]
    references = normalized["visual_job_ticket"]["reference_plan"]

    assert "blanket looped around her ankle" in segments[0]["visual_action"]
    assert "HOST SLOT REASSIGNED" in segments[1]["visual_action"]
    assert "phone face-down" in segments[2]["visual_action"]
    assert "television remains paused" in segments[3]["visual_action"]
    assert all(
        segment["visual_action"] != segment["story_function"]
        for segment in segments
    )
    assert "unused tickets" in references[0]["description"]
    assert "Rapid insert" not in references[0]["description"]
    assert "HOST SLOT REASSIGNED" in references[1]["description"]
    assert "phone is face-down" in references[2]["description"]
    assert "television remains paused" in references[3]["description"]
    assert all(
        reference["description"] not in purposes
        for reference in references
    )


def test_creative_blueprint_accepts_prose_structure_with_sibling_story_blocks():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 40,
            "structure": "Four ordered ten-second segments.",
            "opening": {
                "dialogue": "Rhea discovers the unfinished page.",
                "visual_direction": "The page is trapped beneath the wrong door.",
            },
            "development": {
                "dialogue": [
                    {"segment": 2, "dialogue": "The loss becomes personal."},
                    {"segment": 3, "dialogue": "She creates a transition."},
                ],
                "visual_direction": "She stops the metronome and closes the score.",
            },
            "resolution": {
                "dialogue": "She gives the day a clear ending.",
                "visual_direction": "The hallway becomes calm.",
            },
            "ordered_segment_assignments": [
                {
                    "segment": index,
                    "speaker_id": "NARRATOR",
                    "spoken_copy": line,
                }
                for index, line in enumerate((
                    "At midnight, Rhea found the unfinished page beneath the wrong door.",
                    "The blank ending showed how often she had abandoned her own work.",
                    "She stopped the metronome and gave the day a deliberate transition.",
                    "She closed the score and let the quiet hallway mark a real ending.",
                ), 1)
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [{
                "speaker_id": "NARRATOR",
                "name": "Narrator",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": index,
                "purpose": purpose,
                "visual": visual,
            }
            for index, purpose, visual in (
                (1, "loss hook", "Rhea sees the unfinished page under the wrong door."),
                (2, "knife twist", "Rhea catches the rolling metronome beside a blank page."),
                (3, "recognition", "Rhea closes the score and dims the hallway."),
                (4, "resolution", "Rhea pauses alone in the quiet hallway."),
            )
        ],
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    outline = normalized["complete_video_script"]["story_outline"]
    assert outline["opening"].startswith("Rhea discovers the unfinished page.")
    assert "The loss becomes personal." in outline["development"]
    assert outline["resolution"].startswith("She gives the day a clear ending.")
    assert len(normalized["complete_video_script"]["segments"]) == 4


def test_envelope_shape_moves_known_creative_fields_from_root_into_result():
    envelope = {
        "schema_version": "1.0",
        "stage": "CREATIVE",
        "result": {
            "concepts": [{"concept_id": "one"}],
            "selected_concept": {"concept_id": "one"},
            "continuity_rules": {"character_lock": "Rhea is a 38-year-old adult."},
        },
        "visual_job_ticket": {"reference_image_count": 4},
        "shot_plan": [{"segment": 1, "visual": "Rhea sees the notice."}],
        "cta_options": ["$7.99 in the yellow cart."],
        "complete_video_script": {"duration_seconds": 40, "segments": []},
        "voice_bible": {"primary_speaker_id": "rhea"},
        "next_stage": "VISUAL_PREVIEW",
    }

    normalized = content_factory_tasks._normalize_envelope_shape(envelope)

    assert normalized["result"]["visual_job_ticket"] == envelope["visual_job_ticket"]
    assert normalized["result"]["shot_plan"] == envelope["shot_plan"]
    assert normalized["result"]["complete_video_script"] == envelope["complete_video_script"]
    assert normalized["result"]["voice_bible"] == envelope["voice_bible"]
    assert normalized["result"]["continuity_rules"] == envelope["result"]["continuity_rules"]
    assert normalized["next_stage"] == "VISUAL_PREVIEW"


def test_creative_blueprint_preserves_visual_story_and_product_reference_from_api():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        product_brief="Create one complete 40-second product short-form story.",
        state_json={},
        config_json={
            "content_mode": "product",
            "product_required": True,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    visual_stories = (
        "Daniel alone beside a compact telescope looks down at a clipped guide schedule marked reassigned.",
        "Daniel's profile remains visible while the telescope view lands on a neighboring lit window instead of Orion.",
        "Daniel stands at the narrow balcony desk after closing his laptop, phone face-down beside an open star chart.",
        "Daniel points toward a constellation while the sealed MYUPONA Sleep Ease Gummies bottle sits on the side table.",
    )
    purposes = (
        "LOSS HOOK",
        "KNIFE-TWIST MOMENT",
        "RECOGNITION AND BRIDGE",
        "RESOLUTION AND CTA",
    )
    dialogue = (
        "Daniel watched guests line up for a guide who was not him.",
        "At his telescope, he found a neighbor's lit window, not Orion.",
        "Exhaustion was costing the dependable part of him.",
        "MYUPONA Sleep Ease Gummies are seven ninety-nine in the yellow cart.",
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 34,
            "opening": "Daniel discovers that his valued role has already been reassigned.",
            "development": "The wrong telescope view makes the identity loss concrete.",
            "resolution": "He creates a clean evening transition and a product routine step.",
            "ordered_segment_assignments": [
                {
                    "segment": index,
                    "duration_seconds": 10,
                    "section": purposes[index - 1],
                    "speaker_id": "daniel",
                    "dialogue": dialogue[index - 1],
                }
                for index in range(1, 5)
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "daniel",
            "speakers": [{
                "speaker_id": "daniel",
                "name": "Daniel",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": index,
                "purpose": purposes[index - 1],
                "visual_story": visual_stories[index - 1],
            }
            for index in range(1, 5)
        ],
        "visual_job_ticket": {
            "reference_image_count": 4,
            "reference_plan": [
                {
                    "index": index,
                    "segment": index,
                    "description": visual_stories[index - 1],
                    "roles": ["character_anchor", "scene_anchor", "action_anchor"],
                }
                for index in range(1, 5)
            ],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    segments = normalized["complete_video_script"]["segments"]
    references = normalized["visual_job_ticket"]["reference_plan"]
    assert [item["story_function"] for item in segments] == list(purposes)
    assert [
        item["visual_action"].split(". Keep")[0].rstrip(".")
        for item in segments
    ] == [item.rstrip(".") for item in visual_stories]
    assert all(item["description"] not in purposes for item in references)
    assert "guide schedule marked reassigned" in references[0]["description"]
    assert "neighboring lit window" in references[1]["description"]
    assert "laptop" in references[2]["description"]
    assert "MYUPONA Sleep Ease Gummies bottle" in references[3]["description"]
    assert references[3]["requires_product_reference"] is True


def test_strong_pain_conversion_bridge_rejects_early_product_and_catalog_ending():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        product_brief=(
            "MANDATORY 40-SECOND COPY STRUCTURE:\n"
            "Segments 1-2 show the valued loss. Segment 3 is product-free.\n"
            "QUALITY REJECTION RULES:\n"
            "Reject an abrupt announcer advertisement.\n"
            "The product must not feel pasted on; it enters naturally as one step inside the chosen wind-down.\n"
            "FINAL API EXECUTION CLARIFICATIONS (AUTHORITATIVE):\n"
            "Close with a concrete prevention-of-regret tieback."
        ),
        state_json={},
        config_json={
            "content_mode": "product",
            "product_required": True,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
            "confirmed_promotions": "Current price: $7.99.",
            "promotion_cta": "Find MYUPONA in the yellow cart.",
            "creative_copy_contract": {
                "product_role_terms": ["step", "part"],
                "require_post_cta_agency_ending": True,
                "post_cta_agency_terms": [
                    "decision",
                    "choose",
                    "change",
                    "start",
                    "wait",
                ],
                "required_post_cta_agency_term_count": 1,
                "minimum_post_cta_word_count": 6,
            },
        },
    )
    segments = [
        {
            "segment_index": 1,
            "visual_action": "A longtime customer hands Lena's kiln order to another maker.",
            "dialogue_lines": [{
                "speaker_id": "narrator",
                "line": "Lena watched her longtime customer hand the kiln order to another maker.",
            }],
        },
        {
            "segment_index": 2,
            "visual_action": "The returned sample sits unopened on her commission shelf.",
            "dialogue_lines": [{
                "speaker_id": "narrator",
                "line": "The returned sample sat unopened where her commissions once waited.",
            }],
        },
        {
            "segment_index": 3,
            "visual_action": "Lena closes the studio ledger and turns her phone face down.",
            "dialogue_lines": [{
                "speaker_id": "narrator",
                "line": (
                    "That silence exposed the pattern: unfinished nights were "
                    "rewriting her dependable name, so she chose a wind-down step."
                ),
            }],
        },
        {
            "segment_index": 4,
            "visual_action": "The sealed MYUPONA bottle rests beside the closed ledger.",
            "dialogue_lines": [{
                "speaker_id": "narrator",
                "line": (
                    "For that wind-down step, she chose MYUPONA—$7.99 in the yellow "
                    "cart. That sample was already gone; her decision could not wait."
                ),
            }],
        },
    ]

    _assert_project_creative_copy_contract(project, segments)

    product_as_prevention = json.loads(json.dumps(segments))
    product_as_prevention[3]["dialogue_lines"][0]["line"] = (
        "MYUPONA fits clean endings; $7.99—find it in the yellow cart "
        "below before clients leave."
    )
    with pytest.raises(
        ValueError,
        match="CREATIVE_FINAL_AGENCY_TIEBACK_MISSING",
    ):
        _assert_project_creative_copy_contract(project, product_as_prevention)

    early_product = json.loads(json.dumps(segments))
    early_product[2]["dialogue_lines"][0]["line"] = (
        "She added MYUPONA to mark the end of every day."
    )
    with pytest.raises(
        ValueError,
        match="CREATIVE_CONVERSION_BRIDGE_PRODUCT_TOO_EARLY",
    ):
        _assert_project_creative_copy_contract(project, early_product)

    catalog_ending = json.loads(json.dumps(segments))
    catalog_ending[3]["dialogue_lines"][0]["line"] = (
        "For that wind-down step, she chose MYUPONA—$7.99 in the yellow "
        "cart. Her decision to change could not wait."
    )
    with pytest.raises(
        ValueError,
        match="CREATIVE_FINAL_REGRET_TIEBACK_MISSING",
    ):
        _assert_project_creative_copy_contract(project, catalog_ending)

    noncausal_ending = json.loads(json.dumps(segments))
    noncausal_ending[3]["dialogue_lines"][0]["line"] = (
        "Keep making kiln orders: MYUPONA is $7.99 in the yellow cart. "
        "Her decision to change could not wait."
    )
    with pytest.raises(
        ValueError,
        match="CREATIVE_CONVERSION_BRIDGE_CAUSAL_LINK_MISSING",
    ):
        _assert_project_creative_copy_contract(project, noncausal_ending)


def test_creative_copy_contract_is_compiled_from_project_not_campaign_constants():
    project = SimpleNamespace(
        product_id=9,
        product_name="NOVA Evening Tea",
        product_brief=(
            "Segment 1 shows the loss.\n"
            "Segment 2 must remain product-free.\n"
            "Segment 3, PRODUCT AND CONVERSION: introduce the product.\n"
            "Tie the ending back to the opening loss."
        ),
        state_json={},
        config_json={
            "content_mode": "product",
            "product_required": True,
            "brand_name": "NOVA",
            "video_model": "omni_flash",
            "video_duration_min_seconds": 30,
            "video_duration_max_seconds": 30,
            "video_language": "en-US",
            "confirmed_promotions": "Current price: $12.50.",
            "promotion_cta": "Find NOVA in the green storefront link.",
        },
    )

    contract = _creative_copy_contract(project)
    encoded = json.dumps(contract, ensure_ascii=False)

    assert contract["segment_count"] == 3
    assert contract["product_free_through_segment"] == 2
    assert contract["product_reveal_segment"] == 3
    assert contract["required_product_identity_terms"] == ["nova"]
    assert contract["required_price_tokens"] == ["$12.50"]
    assert {"green", "storefront", "link"} >= set(contract["required_cta_terms"])
    assert contract["require_opening_tieback"] is True
    assert "MYUPONA" not in encoded
    assert "7.99" not in encoded
    assert "yellow" not in encoded.lower()


def test_creative_blueprint_accepts_singular_speaker_assignment_from_api():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 40,
            "opening": "The ritual is already disappearing.",
            "development": "A small response exposes the deeper loss.",
            "resolution": "The adult chooses a clean evening transition.",
            "segment_assignments": [
                {
                    "segment": index,
                    "start_seconds": (index - 1) * 10,
                    "end_seconds": index * 10,
                    "structure_role": role,
                    "speaker_assignment": [{
                        "speaker_id": "NARRATOR",
                        "text": line,
                    }],
                }
                for index, role, line in (
                    (1, "LOSS HOOK", "The empty chair showed that their Friday ritual was already disappearing."),
                    (2, "KNIFE-TWIST", "She stopped waiting and quietly moved to the other end of the sofa."),
                    (3, "RECOGNITION", "That small distance showed him what another exhausted evening was costing."),
                    (4, "RESOLUTION", "He closed the laptop, lowered the light, and made room for the ritual again."),
                )
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [{
                "speaker_id": "NARRATOR",
                "name": "Narrator",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": index,
                "story_function": role,
                "visual": visual,
            }
            for index, role, visual in (
                (1, "loss hook", "An adult notices the empty chair."),
                (2, "knife-twist", "His partner moves down the sofa."),
                (3, "recognition", "He sees the distance between them."),
                (4, "resolution", "He resets the living room ritual."),
            )
        ],
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    assert [
        item["dialogue_lines"][0]["line"]
        for item in normalized["complete_video_script"]["segments"]
    ] == [
        "The empty chair showed that their Friday ritual was already disappearing.",
        "She stopped waiting and quietly moved to the other end of the sofa.",
        "That small distance showed him what another exhausted evening was costing.",
        "He closed the laptop, lowered the light, and made room for the ritual again.",
    ]


def test_creative_blueprint_normalizes_speaker_assignments_to_one_required_narrator():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief=(
            "Create a 40-second short-form story. Use exactly one adult US-English narrator as the only spoken voice."
        ),
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    lines = (
        "Elena saw the empty chair beside the book she used to share.",
        "Her son said, “I will save your place, Mom.” Then he carried his book away.",
        "She realized nobody expected her at story time anymore, so she changed her evening transition.",
        "She lowered the light, put away her phone, and opened one chosen page.",
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 34,
            "opening": lines[0],
            "development": lines[1],
            "resolution": lines[3],
            "ordered_segment_assignments": [
                {
                    "segment": index,
                    "duration_seconds": 10,
                    "structure_role": f"beat {index}",
                    "visual_action": f"Concrete action for beat {index}.",
                    "speaker_assignments": (
                        [{"speaker_id": "NARRATOR", "dialogue": line}]
                        if index != 2 else [
                            {"speaker_id": "NARRATOR", "dialogue": "Her son said,"},
                            {"speaker_id": "CHILD", "dialogue": "I will save your place, Mom."},
                            {"speaker_id": "NARRATOR", "dialogue": "Then he carried his book away."},
                        ]
                    ),
                }
                for index, line in enumerate(lines, 1)
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [
                {"speaker_id": "NARRATOR", "name": "Narrator", "speech_rate_wpm": 180},
                {"speaker_id": "CHILD", "name": "Child", "speech_rate_wpm": 140},
            ],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    assert [voice["speaker_id"] for voice in normalized["voice_bible"]["speakers"]] == ["NARRATOR"]
    assert all(
        segment["dialogue_lines"][0]["speaker_id"] == "NARRATOR"
        for segment in normalized["complete_video_script"]["segments"]
    )
    assert "I will save your place, Mom." in normalized["complete_video_script"]["segments"][1]["dialogue_lines"][0]["line"]


def test_creative_blueprint_normalizes_named_segments_and_speaker_dialogue():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 34,
            "opening": "A daughter sees that her dependable father has changed.",
            "development": "One quiet admission reveals the loss inside the family.",
            "resolution": "She helps the household give the day a clean ending.",
            "segment_assignments": [{
                "segment": "segment_1",
                "timecode": "0:00-0:10",
                "purpose": "LOSS HOOK",
                "speaker_dialogue": [{
                    "speaker_id": "MARA",
                    "text": "At one in the morning, Mara found the family ritual her father had abandoned.",
                }],
            }, {
                "segment": "segment_2",
                "timecode": "0:10-0:20",
                "purpose": "KNIFE-TWIST",
                "speaker_dialogue": [{
                    "speaker_id": "MARA",
                    "text": "She watched him hide it and admit,",
                }, {
                    "speaker_id": "DAD",
                    "text": "I no longer feel like myself,",
                }, {
                    "speaker_id": "MARA",
                    "text": "before he quietly walked away.",
                }],
            }, {
                "segment": "segment_3",
                "timecode": "0:20-0:30",
                "purpose": "RECOGNITION",
                "speaker_dialogue": [{
                    "speaker_id": "MARA",
                    "text": "She realized the home had lost the steadiness everyone once trusted.",
                }],
            }, {
                "segment": "segment_4",
                "timecode": "0:30-0:40",
                "purpose": "RESOLUTION",
                "speaker_dialogue": [{
                    "speaker_id": "MARA",
                    "text": "Together, they gave the long day a clean and deliberate ending.",
                }],
            }],
        },
        "voice_bible": {
            "primary_speaker_id": "MARA",
            "speakers": [{
                "speaker_id": "MARA",
                "name": "Mara",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {
                "segment": f"segment_{index}",
                "story_function": purpose,
                "shot": shot,
            }
            for index, purpose, shot in (
                (1, "LOSS HOOK", "Mara discovers the abandoned family object."),
                (2, "KNIFE-TWIST", "Her father quietly hides it from view."),
                (3, "RECOGNITION", "Mara sees how the household has changed."),
                (4, "RESOLUTION", "They create a visible end-of-day transition."),
            )
        ],
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    segments = normalized["complete_video_script"]["segments"]
    assert [item["segment_index"] for item in segments] == [1, 2, 3, 4]
    assert [item["duration_seconds"] for item in segments] == [10, 10, 10, 10]
    assert segments[1]["dialogue_lines"] == [{
        "speaker_id": "MARA",
        "speaker": "Mara",
        "line": (
            "She watched him hide it and admit, I no longer feel like myself, "
            "before he quietly walked away."
        ),
    }]
    assert segments[3]["visual_action"] == (
        "They create a visible end-of-day transition."
    )


def test_creative_blueprint_normalizes_spoken_dialogue_and_assignment_aliases():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    lines = (
        "At nine forty-seven, she found the drawing her son had stopped sharing.",
        "He put it away and said she always seemed somewhere else lately.",
        "She recognized that his small world was already closing without her.",
        "She gave the day a clean ending before another ordinary moment disappeared.",
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 34,
            "opening": "A mother sees the relationship moment already being lost.",
            "development": "A child's quiet action makes that loss undeniable.",
            "resolution": "She chooses a clean end-of-day transition.",
            "segment_assignments": [
                {
                    "segment": index,
                    "start_seconds": (index - 1) * 10,
                    "end_seconds": index * 10,
                    "assignment": assignment,
                    "spoken_dialogue": lines[index - 1],
                }
                for index, assignment in enumerate((
                    "LOSS HOOK",
                    "KNIFE-TWIST MOMENT",
                    "RECOGNITION AND BRIDGE",
                    "RESOLUTION",
                ), 1)
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "MOTHER",
            "speakers": [{
                "speaker_id": "MOTHER",
                "name": "Mother",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {"segment": index, "visuals": f"Concrete visual action {index}."}
            for index in range(1, 5)
        ],
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    segments = normalized["complete_video_script"]["segments"]
    assert [item["story_function"] for item in segments] == [
        "LOSS HOOK",
        "KNIFE-TWIST MOMENT",
        "RECOGNITION AND BRIDGE",
        "RESOLUTION",
    ]
    assert [
        item["dialogue_lines"][0]["line"] for item in segments
    ] == list(lines)
    assert all(
        item["dialogue_lines"][0]["speaker_id"] == "MOTHER"
        for item in segments
    )


def test_creative_blueprint_normalizes_nested_structure_story_outline():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "total_duration_seconds": 40,
            "target_edit_duration_seconds": 34,
            "structure": {
                "opening": "A father discovers a relationship moment already being lost.",
                "development": "One quiet sentence reveals how his child now sees him.",
                "resolution": "He chooses to close each day differently.",
            },
            "segments": [
                {
                    "segment": index,
                    "assignment": assignment,
                    "dialogue": dialogue,
                }
                for index, assignment, dialogue in (
                    (1, "LOSS HOOK", "At one fourteen, he found his daughter hiding an ordinary moment from him."),
                    (2, "KNIFE-TWIST", "She said she did not want him angry again, then quietly walked away."),
                    (3, "RECOGNITION", "He was becoming the father she tiptoed around, so he changed the transition."),
                    (4, "RESOLUTION", "He gave the day a clean ending while the relationship could still be repaired."),
                )
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "FATHER",
            "speakers": [{
                "speaker_id": "FATHER",
                "name": "Father",
                "speech_rate_wpm": 180,
            }],
        },
        "shot_plan": [
            {"segment": index, "visual": f"Concrete story visual {index}."}
            for index in range(1, 5)
        ],
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    assert normalized["complete_video_script"]["story_outline"] == {
        "opening": "A father discovers a relationship moment already being lost.",
        "development": "One quiet sentence reveals how his child now sees him.",
        "resolution": "He chooses to close each day differently.",
    }
    assert [
        item["story_function"]
        for item in normalized["complete_video_script"]["segments"]
    ] == ["LOSS HOOK", "KNIFE-TWIST", "RECOGNITION", "RESOLUTION"]


def test_creative_blueprint_enforces_prompt_hard_limit_before_fast_delivery_tolerance():
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="Create one complete 40-second short-form story.",
        state_json={},
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 40,
            "target_edit_duration_seconds": 40,
            "story_outline": {
                "opening": "A relationship loss is discovered.",
                "development": "One action makes the loss concrete.",
                "resolution": "The adult chooses a clean transition.",
            },
            "segments": [
                {
                    "segment_index": index,
                    "duration_seconds": 10,
                    "story_function": function,
                    "visual_action": f"Concrete visual action {index}.",
                    "dialogue_lines": [{
                        "speaker_id": "NARRATOR",
                        "line": line,
                    }],
                }
                for index, function, line in (
                    (1, "LOSS HOOK", "She found the drawing he no longer waited to show her."),
                    (2, "KNIFE-TWIST", "He put it away and said she always seemed somewhere else lately."),
                    (3, "RECOGNITION", "She recognized his small world was already closing without her."),
                    (
                        4,
                        "CTA",
                        "This final direct response beat contains thirty one spoken words "
                        "and still fits a fast TikTok delivery when the bounded short form "
                        "pace allowance is applied without accepting an unreasonably long script.",
                    ),
                )
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [{
                "speaker_id": "NARRATOR",
                "name": "Narrator",
                "speech_rate_wpm": 165,
            }],
        },
    }

    original_recognition = (
        result["complete_video_script"]["segments"][2]["dialogue_lines"][0]["line"]
    )
    result["complete_video_script"]["segments"][2]["dialogue_lines"][0]["line"] = (
        "I'm not just missing movies; I'm losing our shared life. I'm starting "
        "a clean end-of-day wind-down, with one repeatable routine step."
    )
    with pytest.raises(
        ValueError,
        match=(
            r"CREATIVE segment #3 spoken copy has 21 units but the hard "
            r"maximum is 18 English words"
        ),
    ):
        _normalize_creative_video_blueprint(project, result)
    result["complete_video_script"]["segments"][2]["dialogue_lines"][0]["line"] = (
        original_recognition
    )

    with pytest.raises(
        ValueError,
        match=(
            r"CREATIVE segment #4 spoken copy has 32 units but the hard "
            r"maximum is 18 English words"
        ),
    ):
        _normalize_creative_video_blueprint(project, result)

    result["complete_video_script"]["segments"][3]["dialogue_lines"][0]["line"] = (
        "Try MYUPONA tonight: melatonin-free, sugar free, blueberry flavored, "
        "only $7.99 in the yellow cart."
    )
    normalized = _normalize_creative_video_blueprint(project, result)
    assert (
        normalized["complete_video_script"]["segments"][3]["dialogue_lines"][0]["line"]
        == result["complete_video_script"]["segments"][3]["dialogue_lines"][0]["line"]
    )


def test_creative_reference_plan_restores_product_anchor_from_segment_four_script():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        product_brief="Create a product short-form story.",
        state_json={},
        config_json={
            "content_mode": "product",
            "product_required": True,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 40,
            "target_edit_duration_seconds": 34,
                "story_outline": {
                    "opening": "An adult notices a hard-won relationship moment being lost.",
                    "development": "A close friend puts away a drawing she no longer expects to share.",
                    "resolution": "The adult creates a clean ending and a product routine step.",
            },
            "segments": [
                {
                    "segment_index": index,
                    "duration_seconds": 10,
                    "story_function": f"beat {index}",
                    "visual_action": action,
                    "dialogue_lines": [{"speaker_id": "NARRATOR", "line": line}],
                }
                for index, action, line in (
                    (1, "She sees the family moment already changing.", "She saw the moment changing."),
                    (2, "The drawing disappears beneath the mail.", "The drawing went away."),
                    (3, "She turns her phone face down and dims the light.", "She chose a clean ending."),
                    (
                        4,
                        "At the same countertop, she places the approved MYUPONA Sleep Ease Gummies bottle beside her phone as one routine step.",
                        "MYUPONA becomes one routine step.",
                    ),
                )
            ],
        },
        "voice_bible": {
            "primary_speaker_id": "NARRATOR",
            "speakers": [{"speaker_id": "NARRATOR", "name": "Narrator", "speech_rate_wpm": 180}],
        },
        "visual_job_ticket": {
            "reference_image_count": 4,
            "reference_plan": [
                {
                    "index": index,
                    "segment": index,
                    "description": (
                        "Character, scene, or action continuity for this scripted beat. "
                        "Do not create a standalone product, packshot, white-background package image, label study, logo study, or product-only panel; "
                        "the user's uploaded product image remains the separate product anchor."
                        if index == 4 else f"Story reference {index}"
                    ),
                    "roles": ["character_anchor", "action_anchor"],
                }
                for index in range(1, 5)
            ],
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)

    final_panel = normalized["visual_job_ticket"]["reference_plan"][3]
    assert final_panel["segment"] == 4
    assert final_panel["requires_product_reference"] is True
    assert "MYUPONA Sleep Ease Gummies bottle" in final_panel["description"]
    assert "uploaded product reference" in final_panel["description"]

    # The restored conversion panel must actually attach the authoritative
    # uploaded package to the image request, rather than merely describing it
    # in text. The package is an extra source input, not a fifth storyboard
    # panel.
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = normalized["visual_job_ticket"]
    assert visual_generation_reference_paths(packet) == [
        "/tmp/product.png",
        "/tmp/character.png",
    ]


def test_exact_benchmark_shorthand_expands_source_duration_and_canonicalizes_legacy_indices():
    cues = [
        {
            "index": 1,
            "start_seconds": float(index * 3.6),
            "end_seconds": float(index * 3.6 + 2.8),
            "text": f"Exact source cue {index + 1}.",
        }
        for index in range(23)
    ]
    project = SimpleNamespace(
        product_id=None,
        product_name="",
        product_brief="1:1 精准复刻这个对标视频",
        state_json={
            "benchmark_video_analysis": {
                "status": "success",
                "duration_seconds": 86.4,
                "segments": cues,
            }
        },
        config_json={
            "content_mode": "entertainment",
            "product_required": False,
            "video_model": "omni_flash",
            "video_duration_min_seconds": 80,
            "video_duration_max_seconds": 90,
            "video_language": "en-US",
        },
    )
    result = {
        "complete_video_script": {
            "duration_seconds": 80,
            "segments": [
                {"time": f"{index * 10}-{(index + 1) * 10}s", "script": f"Model summary {index}."}
                for index in range(8)
            ],
        },
        "voice_bible": {
            "voice_type": "Energetic narrator",
            "tone": "curious",
            "speed": "fast",
        },
        "shot_plan": [
            {"purpose": f"beat {index + 1}", "visual": f"Visual beat {index + 1}."}
            for index in range(8)
        ],
        "visual_job_ticket": {
            "duration_seconds": 80,
            "segment_count": 8,
            "segment_duration_seconds": 10,
        },
    }

    normalized = _normalize_creative_video_blueprint(project, result)
    script = normalized["complete_video_script"]

    assert script["duration_seconds"] == 90
    assert script["target_edit_duration_seconds"] == 86
    assert len(script["segments"]) == 9
    assert [
        cue
        for segment in script["segments"]
        for cue in segment["benchmark_cue_indices"]
    ] == list(range(1, 24))
    assert [
        line["line"]
        for segment in script["segments"]
        for line in segment["dialogue_lines"]
    ] == [f"Exact source cue {index}." for index in range(1, 24)]
    assert normalized["visual_job_ticket"]["segment_count"] == 9


def test_provider_prompt_contains_only_local_dialogue_and_compact_voice_lock():
    prompt = _compact_provider_segment_prompt(
        {
            "prompt": "Segment 2: the roommate lifts one pillow.",
            "segment_goal": "escalation",
            "timeline": [{"start_second": 0, "end_second": 10, "action": "Inspect the blanket maze."}],
            "dialogue_lines": [{"speaker_id": "HOST", "speaker": "Host", "line": "This is the strangest fort I have seen."}],
            "voice_lock": [{
                "speaker_id": "HOST", "name": "Host", "gender": "female",
                "screen_relation": "on_screen_character",
                "timbre": "warm and bright", "pitch": "medium", "accent": "US English",
                "delivery": "fast but intelligible", "speech_rate": 180,
                "speech_rate_unit": "words_per_minute",
            }],
        },
        resolution="720p",
        language_label="English (US)",
        fast_pacing=True,
    )

    assert "This is the strangest fort I have seen." in prompt
    assert "Voice lock for this segment" in prompt
    assert "explicitly female" in prompt
    assert "do not change the speaker's gender" in prompt
    assert "lip-sync only that same character" in prompt
    assert "opening/development/resolution" not in prompt
    assert "complete_video_script" not in prompt


def test_provider_prompt_preserves_complete_long_final_dialogue():
    line = (
        "I close the day with MYUPONA Sleep Ease Gummies, a melatonin-free, "
        "sugar-free blueberry routine step. They're $7.99 in the yellow cart below."
    )
    prompt = _compact_provider_segment_prompt(
        {
            "prompt": "Stable closing frame.",
            "timeline": [{"start_second": 0, "end_second": 10, "action": "Set the sealed bottle on the console."}],
            "dialogue_lines": [{"speaker": "Priya", "line": line}],
        },
        resolution="720p",
        language_label="English (US)",
        fast_pacing=False,
        promotion="MYUPONA Sleep Ease Gummies are $7.99. Find them in the yellow cart below.",
        product_required=True,
        product_presentation_policy={
            "authority_mode": "uploaded_source_only",
            "presentation_instructions": [
                "Keep the configured package closed on the console.",
            ],
            "forbidden_interaction_categories": [
                "open_package",
                "consume_product",
            ],
        },
    )

    assert line in prompt
    assert "..." not in prompt.split("Dialogue:", 1)[1].splitlines()[0]
    assert "sole package authority" in prompt
    assert "Keep the configured package closed on the console." in prompt
    assert "forbidden interaction categories: open_package, consume_product" in prompt


def test_omni_prompt_preserves_signed_tiktok_shop_dialogue_exactly():
    source = (
        "Dialogue: narrator: 'Search MYUPONA on TikTok Shop. It’s currently $7.99.'\n"
        "Preserve the exact approved dialogue."
    )

    prompt = _omni_reference_prompt(
        source,
        [],
        product_required=False,
    )

    assert "Search MYUPONA on TikTok Shop. It’s currently $7.99." in prompt
    assert "short-form Shop" not in prompt


def test_signed_provider_prompt_sanitizes_visual_opening_but_not_dialogue():
    spoken = (
        "Two blueberry gummies, melatonin-free, made with magnesium "
        "glycinate, and zero grams of total sugar."
    )
    prompt = _compact_provider_segment_prompt(
        {
            "compile_source": "signed_production_plan",
            "prompt": "Segment 4: product reveal",
            "timeline": [{
                "start_second": 0,
                "end_second": 10,
                "action": (
                    "She opens the MYUPONA bottle, shows exactly two gummies "
                    "in her palm, closes the bottle, and sets it by the charger."
                ),
            }],
            "dialogue_lines": [{"speaker": "narrator", "line": spoken}],
        },
        resolution="720p",
        language_label="English (US)",
        fast_pacing=True,
        product_required=True,
        product_name="MYUPONA Sleep Ease Gummies",
        product_presentation_policy={
            "authority_mode": "uploaded_source_only",
            "presentation_instructions": [
                "Keep the authoritative package sealed and closed.",
            ],
            "forbidden_interaction_categories": ["open_package"],
        },
    )

    timeline = prompt.split("Timeline (this segment only):", 1)[1].splitlines()[0]
    assert "opens the" not in timeline.lower()
    assert "sealed and closed" in timeline.lower()
    assert spoken in prompt


def test_internal_price_policy_never_becomes_spoken_cta_or_conversion_dialogue():
    project = HermesContentFactoryProject(
        id=168,
        project_key="cf_test",
        workspace_id=3,
        user_id=6,
        product_id=1,
        title="Test",
        product_name="MYUPONA Sleep Ease Gummies",
        product_brief="A melatonin-free, sugar-free blueberry routine step with GABA.",
        market="US",
        status="running",
        current_stage="VIDEO_PROMPTS",
        config_json={
            "video_language": "en-US",
            "allow_promotional_cta": True,
            "user_confirmed_marketing": True,
            "confirmed_promotions": (
                "Current product price: $7.99 per bottle. This is a confirmed current price, "
                "not permission to claim a discount. Do not claim free shipping, delivered "
                "price, coupon, savings, limited time, scarcity, or countdown."
            ),
            "promotion_cta": (
                "MYUPONA Sleep Ease Gummies are $7.99. "
                "Find them in the yellow cart below."
            ),
            "confirmed_selling_points": (
                "Melatonin-free; Sugar free; Blueberry flavor"
            ),
            "product_presentation_policy": {
                "authority_mode": "uploaded_source_only",
                "presentation_instructions": [
                    "Keep the exact source package closed; do not show loose contents.",
                ],
                "forbidden_interaction_categories": [
                    "open_package",
                    "expose_loose_contents",
                    "consume_product",
                ],
            },
        },
    )

    promotion = content_factory_tasks._required_video_promotion(project, {})
    assert promotion == (
        "MYUPONA Sleep Ease Gummies are $7.99. "
        "Find them in the yellow cart below."
    )
    assert "not permission" not in promotion.lower()
    assert "do not claim" not in promotion.lower()

    prompt = content_factory_tasks._ensure_prompt_conversion_block(
        "Dialogue: Priya: 'The bottle stays sealed.'",
        project,
        language="en-US",
        promotion=promotion,
        final_segment=True,
    )
    assert "must say" not in prompt.lower()
    assert "add no dialogue" in prompt.lower()
    assert "keep the exact source package closed" in prompt.lower()
    assert "do not show loose contents" in prompt.lower()
    assert "gummies on a palm/table" not in prompt.lower()


def test_existing_price_and_yellow_cart_dialogue_prevents_duplicate_cta():
    creative_prompt = (
        "Dialogue: Priya: 'I close the day with MYUPONA Sleep Ease Gummies, "
        "a melatonin-free, sugar-free blueberry routine step. "
        "They're $7.99 in the yellow cart below.'"
    )
    normalized = content_factory_tasks._ensure_prompt_required_promotion(
        creative_prompt,
        "MYUPONA Sleep Ease Gummies are $7.99. Find them in the yellow cart below.",
        "en-US",
    )

    assert normalized == creative_prompt
    assert "Required spoken CTA:" not in normalized
    assert normalized.count("$7.99") == 1


def test_variant_superseded_machine_code_fits_database_contract():
    assert content_factory_tasks.CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE == "cf_variant_superseded"
    assert len(content_factory_tasks.CONTENT_FACTORY_VARIANT_SUPERSEDED_CODE) <= 32


def test_text_api_timeout_never_fails_over_or_submits_twice(monkeypatch):
    calls: list[dict] = []

    monkeypatch.setattr(
        content_factory_api,
        "get_effective_key",
        lambda *_args, **_kwargs: SimpleNamespace(api_key_ciphertext="cipher"),
    )
    monkeypatch.setattr(content_factory_api, "decrypt_api_key", lambda _value: "token")

    def timed_out(*_args, **kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("response timed out after provider acceptance")

    monkeypatch.setattr(content_factory_api.httpx, "post", timed_out)
    packet = _packet()
    packet["current_stage"] = "FACTS"

    with pytest.raises(ContentFactoryApiError, match="without safe model failover"):
        execute_text_stage_api(None, packet, "FACTS")

    assert len(calls) == 1
    assert calls[0]["headers"]["Idempotency-Key"] == (
        "content-factory:cf_test:12:1:v06:FACTS"
    )


def test_text_api_fails_over_once_only_after_explicit_model_rejection(monkeypatch):
    calls: list[dict] = []

    monkeypatch.setattr(
        content_factory_api,
        "get_effective_key",
        lambda *_args, **_kwargs: SimpleNamespace(api_key_ciphertext="cipher"),
    )
    monkeypatch.setattr(content_factory_api, "decrypt_api_key", lambda _value: "token")

    def respond(*_args, **kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", "https://toapis.example/v1/chat/completions")
        if len(calls) == 1:
            return httpx.Response(
                404,
                json={"error": {"message": "model not found"}},
                request=request,
            )
        envelope = {
            "schema_version": "1.0",
            "status": "PASS",
            "result": {"concepts": [], "selected_concept": {}, "visual_job_ticket": {}},
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(envelope)}}]},
            request=request,
        )

    monkeypatch.setattr(content_factory_api.httpx, "post", respond)
    packet = _packet()
    packet["current_stage"] = "FACTS"

    text, meta = execute_text_stage_api(None, packet, "FACTS")

    assert json.loads(text)["stage"] == "FACTS"
    assert meta["model_failover_used"] is True
    assert len(calls) == 2
    assert calls[0]["headers"]["Idempotency-Key"] != calls[1]["headers"]["Idempotency-Key"]
    assert all(len(call["headers"]["Idempotency-Key"]) <= 64 for call in calls)


def test_text_api_semantic_regeneration_uses_a_new_idempotency_identity(monkeypatch):
    calls: list[dict] = []

    monkeypatch.setattr(
        content_factory_api,
        "get_effective_key",
        lambda *_args, **_kwargs: SimpleNamespace(api_key_ciphertext="cipher"),
    )
    monkeypatch.setattr(content_factory_api, "decrypt_api_key", lambda _value: "token")

    def respond(*_args, **kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", "https://toapis.example/v1/chat/completions")
        envelope = {
            "schema_version": "1.0",
            "status": "PASS",
            "result": {"concepts": [], "selected_concept": {}, "visual_job_ticket": {}},
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(envelope)}}]},
            request=request,
        )

    monkeypatch.setattr(content_factory_api.httpx, "post", respond)
    packet = _packet()
    packet["current_stage"] = "FACTS"
    packet["api_regeneration_generation"] = 2

    execute_text_stage_api(None, packet, "FACTS")

    first_key = calls[0]["headers"]["Idempotency-Key"]
    first_prompt = calls[0]["json"]["messages"][1]["content"][0]["text"]
    packet["api_regeneration_generation"] = 3
    execute_text_stage_api(None, packet, "FACTS")
    second_key = calls[1]["headers"]["Idempotency-Key"]

    assert len(first_key) <= 64
    assert len(second_key) <= 64
    assert first_key != second_key
    assert "SEMANTIC_REGENERATION_GENERATION: 2" in first_prompt


def test_text_api_semantic_model_route_prefers_alternate_model(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        content_factory_api,
        "get_effective_key",
        lambda *_args, **_kwargs: SimpleNamespace(api_key_ciphertext="cipher"),
    )
    monkeypatch.setattr(content_factory_api, "decrypt_api_key", lambda _value: "token")

    def respond(*_args, **kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", "https://toapis.example/v1/chat/completions")
        envelope = {
            "schema_version": "1.0",
            "status": "PASS",
            "result": {"concepts": [], "selected_concept": {}, "visual_job_ticket": {}},
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(envelope)}}]},
            request=request,
        )

    monkeypatch.setattr(content_factory_api.httpx, "post", respond)
    packet = _packet()
    packet.update({
        "current_stage": "FACTS",
        "api_regeneration_generation": 1,
        "text_api_prefer_alternate_model": True,
    })

    _text, meta = execute_text_stage_api(None, packet, "FACTS")

    assert calls[0]["json"]["model"] == "gpt-5.6-luna"
    assert meta["model"] == "gpt-5.6-luna"
    assert meta["semantic_model_route"] == "alternate_preferred"


def test_semantic_api_retry_plan_is_bounded_and_switches_adult_story_model(monkeypatch):
    monkeypatch.setattr(
        content_factory_tasks,
        "MAX_SEMANTIC_API_REGENERATION_CYCLES",
        2,
    )
    error = (
        "CREATIVE_CAST_POLICY_MINOR_STORY_CHARACTER_FORBIDDEN: the saved "
        "project cast policy does not allow a minor story character"
    )
    first = _semantic_api_retry_plan({}, error_message=error)

    assert first == {
        "semantic_api_retry_count": 1,
        "semantic_api_retry_limit": 2,
        "text_api_prefer_alternate_model": True,
    }
    single_adult = _semantic_api_retry_plan(
        {},
        error_message=(
            "CREATIVE single-adult constraint violated: exactly one depicted "
            "adult was requested"
        ),
    )
    assert single_adult["text_api_prefer_alternate_model"] is True
    exhausted = _semantic_api_retry_plan(
        {"semantic_api_retry_count": 2, "text_api_prefer_alternate_model": True},
        error_message=error,
    )
    assert exhausted is None


def test_semantic_api_exhaustion_quality_pauses_without_browser_fallback():
    exhausted = _semantic_api_exhaustion_decision(
        {},
        stage="PRODUCTION_PLAN",
        variant_index=27,
        error_message="production plan contract incomplete",
    )

    assert exhausted["action"] == "automatic_quality_pause"
    assert exhausted["state"]["last_semantic_api_exhaustion"]["stage"] == "PRODUCTION_PLAN"
    assert "browser" not in exhausted["action"]


def test_text_api_transport_retries_alternate_models_across_deliveries():
    first = _text_api_transport_retry_plan({})
    second = _text_api_transport_retry_plan(first)
    third = _text_api_transport_retry_plan(second)

    assert first == {
        "text_api_prefer_alternate_model": True,
        "text_api_transport_model_rotation_count": 1,
    }
    assert second == {
        "text_api_prefer_alternate_model": False,
        "text_api_transport_model_rotation_count": 2,
    }
    assert third == {
        "text_api_prefer_alternate_model": True,
        "text_api_transport_model_rotation_count": 3,
    }


def test_text_api_transport_budget_is_local_to_the_current_stage():
    first = _text_api_transport_retry_plan({})
    second = _text_api_transport_retry_plan(first)
    third = _text_api_transport_retry_plan(second)

    assert _text_api_transport_budget_exhausted(first, maximum=3) is False
    assert _text_api_transport_budget_exhausted(second, maximum=3) is False
    assert _text_api_transport_budget_exhausted(third, maximum=3) is True
    assert _text_api_transport_budget_exhausted(
        {"api_stage_failure_history_count": 99},
        maximum=3,
    ) is False


def test_partial_visual_repair_keeps_segment_one_identity_and_room_anchor():
    assert _should_seed_generated_continuity_anchor(
        {},
        has_uploaded_character_anchor=False,
        reference_count=4,
    ) is True
    assert _should_seed_generated_continuity_anchor(
        {"visual_repair_failed_indices": [3, 4]},
        has_uploaded_character_anchor=False,
        reference_count=4,
    ) is True
    assert _should_seed_generated_continuity_anchor(
        {},
        has_uploaded_character_anchor=True,
        reference_count=4,
    ) is False


def test_product_prompt_sanitizer_preserves_the_non_product_scene_action():
    segment_three = _image_prompt_without_product_triggers(
        "Mara stands at the workshop counter with the task lamp lowered, "
        "phone face-down, and the sealed MYUPONA bottle lightly held beside "
        "the unfinished brass plaque.",
        resolution_scene=True,
        product_terms=["MYUPONA"],
    )
    segment_four = _image_prompt_without_product_triggers(
        "Mara calmly wipes the workshop counter while the sealed MYUPONA "
        "bottle rests beside the closed phone and a neatly aligned brass ruler.",
        resolution_scene=True,
        product_terms=["MYUPONA"],
    )
    already_sanitized = _image_prompt_without_product_triggers(
        "Mara lowers the lamp beside a clear empty placement area and "
        "the sealed MYUPONA bottle resting on the workshop counter.",
        resolution_scene=True,
        product_terms=["MYUPONA"],
    )
    color_locked = _image_prompt_without_product_triggers(
        "Mara folds the cloth and places the sealed blue MYUPONA bottle "
        "beside a closed notebook on the wooden workbench.",
        resolution_scene=True,
        product_terms=["MYUPONA"],
    )

    for prompt in (segment_three, segment_four):
        assert "MYUPONA" not in prompt
        assert "bottle" not in prompt.lower()
        assert "placement area" in prompt.lower()
        assert "Mara" in prompt
        assert "workshop counter" in prompt
        assert "placement area lightly held" not in prompt.lower()
        assert "placement area rests" not in prompt.lower()
    assert "task lamp lowered" in segment_three
    assert "phone face-down" in segment_three
    assert "unfinished brass plaque" in segment_three
    assert "calmly wipes" in segment_four
    assert "closed phone" in segment_four
    assert "aligned brass ruler" in segment_four
    assert already_sanitized.lower().count("clear empty placement area") == 1
    assert "placement area remains" in already_sanitized.lower()
    assert "sealed blue" not in color_locked.lower()
    assert "myupona" not in color_locked.lower()
    assert "bottle" not in color_locked.lower()
    assert "clear empty placement area" in color_locked.lower()


def test_browser_fallback_packet_drops_unrelated_runtime_fields():
    packet = _packet()
    compact = _packet_for_prompt(packet, "VISUAL_PREVIEW")
    assert "browser_cdp_url" not in compact
    assert "project_assets" not in compact
    assert "project_state" not in compact
    assert "UNRELATED" not in compact["previous_outputs"]
    assert compact["browser_assets"][0]["role"] == "product_visual"


def test_bandianwa_image_output_parser_handles_immediate_and_nested_shapes():
    assert extract_image_outputs({"data": [{"b64_json": "YWJj"}]}) == [
        {"b64_json": "YWJj"}
    ]
    assert extract_image_outputs(
        {"result": {"outputs": [{"url": "https://cdn.example/image.png"}]}}
    ) == [{"url": "https://cdn.example/image.png"}]


def test_bandianwa_completed_response_prefers_embedded_image_over_protected_content_url():
    class FakeClient:
        async def download(self, _url):
            raise AssertionError("protected content URL must not win over b64_json")

    content = asyncio.run(
        _bandianwa_image_bytes(
            FakeClient(),
            {
                "status": "completed",
                "content_url": "https://api.hellobabygo.com/v1/images/task-1/content",
                "data": [{"b64_json": "YWJj"}],
            },
            task_id="task-1",
        )
    )
    assert content == b"abc"


def test_visual_board_size_tracks_grid_geometry():
    packet = _packet()
    assert visual_board_spec(packet)["size"] == "1024x1024"


def test_browser_visual_fallback_uses_same_multi_board_plan_as_api():
    packet = _packet()
    plan = []
    for index in range(1, 16):
        plan.append(
            {
                "index": index,
                "segment": index,
                "description": f"Ordered global reference panel {index}",
                "roles": ["character_anchor", "action_anchor"],
            }
        )
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 15
    ticket["reference_plan"] = plan

    instruction, board_count = _visual_browser_board_instruction(packet, "VISUAL_PREVIEW")

    assert board_count == 3
    assert _expected_visual_count(packet, "VISUAL_PREVIEW") == 3
    assert "exactly 3 separate storyboard-board images" in instruction
    assert "BOARD IMAGE 1 OF 3" in instruction
    assert "BOARD IMAGE 2 OF 3" in instruction
    assert "BOARD IMAGE 3 OF 3" in instruction
    assert "global reference panels 1 through 5" in instruction
    assert "global reference panels 11 through 15" in instruction
    assert "single-board" not in instruction


def test_three_panel_board_uses_one_landscape_row():
    packet = _packet()
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 3
    ticket["reference_plan"] = ticket["reference_plan"][:3]

    spec = visual_board_spec(packet)

    assert spec["row_columns"] == [3]
    assert spec["columns"] == 3
    assert spec["rows"] == 1
    assert spec["size"] == "1792x1024"
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_image_count"] = 6
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_plan"] = packet[
        "previous_outputs"
    ]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_plan"][:6]
    assert visual_board_spec(packet)["size"] == "1024x1024"


def test_visual_generation_uploads_character_images_only():
    assert visual_generation_reference_paths(_packet()) == ["/tmp/character.png"]


def test_visual_generation_uses_uploaded_product_as_scene_identity_authority():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_plan"][0]["description"] = (
        "Woman holds the MYUPONA bottle beside the bed."
    )

    prompt, _spec = build_visual_api_prompt(packet)

    assert visual_generation_reference_paths(packet) == [
        "/tmp/product.png",
        "/tmp/character.png",
    ]
    assert "product.png [product_visual]" in prompt
    assert "uploaded product reference as the authoritative identity" in prompt
    assert "Do not paste the uploaded image or its white background" in prompt
    assert "render it naturally with the scene lighting" in prompt


def test_product_only_scripted_action_still_uses_uploaded_product_authority():
    action = "A ZEKMUI cleaning tablet appears in a bright transition and dissolves in the washer."
    normalized = (
        action
        + " Character, scene, or action continuity for this scripted beat. "
        "Do not create a standalone product, packshot, white-background package image, label study, logo study, or product-only panel; "
        "the user's uploaded product image remains the separate product anchor."
    )

    assert visual_reference_mentions_product(action) is True
    assert visual_reference_requires_product(action) is True
    assert visual_reference_mentions_product(normalized) is True
    assert visual_reference_requires_product(normalized) is True
    assert _visual_plan_needs_product_reference([{"description": normalized}]) is True


def test_stale_product_free_suffix_cannot_override_scripted_product_action():
    stale = (
        "A ZEKMUI cleaning tablet appears in a bright transition. "
        "This keyframe is product-free: do not show any product, package, bottle, label, logo, or branding."
    )

    normalized = content_factory_api.visual_reference_description(stale, index=5)

    assert "ZEKMUI cleaning tablet appears" in normalized
    assert "copy it only from the uploaded product reference" in normalized
    assert "product-free" not in normalized
    assert visual_reference_requires_product(normalized) is True


def test_bare_tablet_dissolve_beat_uses_uploaded_product_authority():
    action = "Tablet dissolves visibly inside the washer drum."

    assert visual_reference_mentions_product(action) is True
    assert visual_reference_requires_product(action) is True


def test_explicit_semantic_product_exclusion_remains_product_free():
    action = "The homeowner reacts beside the washer; no product or branding appears."
    normalized = content_factory_api.visual_reference_description(action, index=3)

    assert "no product or branding" in normalized
    assert "This keyframe is product-free" in normalized
    assert visual_reference_requires_product(normalized) is False


def test_generic_product_lock_uses_current_project_brand_not_myupona():
    lock = _product_visual_lock({
        "product_required": True,
        "product": {"name": "Washing Machine Cleaning Tablets", "brand": "ZEKMUI"},
    })

    assert "ZEKMUI Washing Machine Cleaning Tablets" in lock
    assert "MYUPONA" not in lock


def test_product_free_visual_lock_does_not_inject_a_brand():
    lock = _product_visual_lock({"product_required": False, "product": None})

    assert "product-free project" in lock
    assert "MYUPONA" not in lock


def test_reference_plan_restores_aligned_product_beat_from_detailed_shot_plan():
    result = {
        "shot_plan": [{
            "segment": 1,
            "visual": "ZEKMUI package hero beside the open washer after the cleaning cycle.",
            "purpose": "End on the real uploaded product and completed washer action.",
        }],
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [{
                "index": 1,
                "segment": 1,
                "description": (
                    "Hero ending. This keyframe is product-free: do not show any product, package, bottle, "
                    "label, logo, or branding."
                ),
                "roles": ["scene_anchor", "action_anchor"],
            }],
        },
    }

    normalized = _ensure_reference_plan(result)
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert "ZEKMUI package hero" in item["description"]
    assert "copy it only from the uploaded product reference" in item["description"]
    assert "product-free" not in item["description"]
    assert item["requires_product_reference"] is True


def test_standalone_generated_packshot_is_separate_from_product_authority_upload():
    packshot = "Standalone white-background MYUPONA product hero packshot."

    assert visual_reference_mentions_product(packshot) is True
    assert visual_reference_requires_product(packshot) is False
    assert _visual_plan_needs_product_reference([{"description": packshot}]) is True


def test_product_free_script_does_not_request_product_authority():
    product_free = "A skeleton sprints down the hallway; no product or branding."

    assert visual_reference_mentions_product(product_free) is False
    assert visual_reference_requires_product(product_free) is False
    assert _visual_plan_needs_product_reference([{"description": product_free}]) is False


def test_unrelated_water_bottles_do_not_request_product_authority():
    action = (
        "Priya stands beside an untouched gallery welcome table while unopened "
        "sparkling-water bottles remain beside a handwritten place card."
    )

    normalized = content_factory_api.visual_reference_description(action, index=1)

    assert visual_reference_mentions_product(action) is False
    assert visual_reference_requires_product(action) is False
    assert "uploaded product reference" not in normalized


def test_before_product_introduction_remains_product_free_without_policy_leak():
    action = (
        "Recognition before product introduction: Priya closes her laptop and "
        "places the gallery keys in a ceramic dish."
    )

    normalized = content_factory_api.visual_reference_description(action, index=3)

    assert visual_reference_mentions_product(action) is False
    assert visual_reference_requires_product(action) is False
    assert "uploaded product reference" not in normalized


def test_single_adult_repair_rejects_a_second_named_adult():
    blueprint = {
        "continuity_rules": {
            "character_lock": (
                "Priya is a 39-year-old adult curator. "
                "Grant is a 42-year-old adult artist."
            ),
        },
        "shot_plan": [{
            "segment": 1,
            "visual_story": "Priya and Grant face each other in the gallery corridor.",
        }],
        "complete_video_script": {"segments": []},
        "visual_job_ticket": {"reference_plan": []},
    }

    with pytest.raises(ValueError, match="single-adult constraint violated"):
        content_factory_tasks._assert_single_adult_visual_story(
            blueprint,
            {
                "self_heal_reason": (
                    "Use exactly one fictional adult protagonist and no second person."
                ),
            },
        )


def test_single_adult_repair_accepts_one_adult_with_object_based_loss():
    blueprint = {
        "continuity_rules": {
            "character_lock": "Priya is a 39-year-old adult curator.",
        },
        "shot_plan": [{
            "segment": 1,
            "visual_story": (
                "Priya stands alone beside the reassignment notice and empty desk."
            ),
        }],
        "complete_video_script": {"segments": []},
        "visual_job_ticket": {"reference_plan": []},
    }

    content_factory_tasks._assert_single_adult_visual_story(
        blueprint,
        {
            "self_heal_reason": (
                "Use exactly one fictional adult protagonist and no second person."
            ),
        },
    )


def test_browser_visual_source_instruction_handles_legitimate_zero_file_stage():
    assert "attached approved input references" in _visual_source_instruction(["product.png"])
    zero_file = _visual_source_instruction([])
    assert "No input reference image is attached" in zero_file
    assert "do not ask for uploads" in zero_file


def test_retired_managed_slot_can_rebind_only_on_same_online_device(monkeypatch):
    project = SimpleNamespace(id=166)
    bridge = SimpleNamespace(
        status="retired",
        active_project_id=None,
        meta_json={
            "agent_managed": True,
            "retired_reason": "agent_confirmed_slot_stopped",
        },
    )
    monkeypatch.setattr(content_factory_service, "_bridge_device_bound", lambda _bridge: True)
    monkeypatch.setattr(
        content_factory_service,
        "_bridge_base_device_online",
        lambda _db, _bridge: True,
    )

    assert _retired_locked_slot_can_rebind(None, project=project, bridge=bridge) is True
    bridge.status = "offline"
    assert _retired_locked_slot_can_rebind(None, project=project, bridge=bridge) is False
    bridge.status = "retired"
    bridge.meta_json["retired_reason"] = "stale_heartbeat"
    assert _retired_locked_slot_can_rebind(None, project=project, bridge=bridge) is False


def test_explicit_product_library_role_survives_generic_camera_filename():
    asset = SimpleNamespace(
        kind="source",
        stage="FACTS",
        original_name="ChatGPT_Image_2026_07_16.png",
        file_path="/tmp/ChatGPT_Image_2026_07_16.png",
        mime_type="image/png",
        meta_json={"source": "product_library", "asset_role": "product_visual"},
    )

    assert _asset_role(asset) == "product_visual"


@pytest.mark.parametrize(
    "message",
    [
        "图像生成失败",
        "图片生成失败",
        "There was an error generating this image",
    ],
)
def test_visual_generation_failure_uses_bounded_visual_recovery(message):
    assert _chatgpt_generation_failed(message) is True
    assert _is_visual_empty_response_error("VISUAL_PREVIEW", message) is True
    assert _is_visual_empty_response_error("CREATIVE", message) is False


def test_visual_retry_matches_execution_only_in_user_turn():
    execution_id = "cf_test:12:1:v06"
    assert _state_has_execution_request(
        {
            "userMessageTexts": [f"project request execution_id={execution_id}"],
            "messageTexts": [],
        },
        execution_id,
    ) is True
    assert _state_has_execution_request(
        {
            "userMessageTexts": ["a different project request"],
            "messageTexts": [f"diagnostic mentioning {execution_id}"],
        },
        execution_id,
    ) is False


def test_inflight_visual_response_is_collector_retry_not_empty_generation():
    message = "CHATGPT_RESPONSE_STILL_RUNNING: browser response exceeded this collector window"
    assert _is_recoverable_chatgpt_response_error(message) is True
    assert _is_visual_empty_response_error("VISUAL_PREVIEW", message) is False


def test_inflight_visual_collector_waits_on_matching_turn_without_resubmitting(monkeypatch, tmp_path):
    from app.services.hermes_agent import direct_browser

    packet = _packet()
    packet["browser_output_path"] = str(tmp_path)
    expected_boards = _expected_visual_count(packet, "VISUAL_PREVIEW")
    generated_boards = [f"generated-board-{index}" for index in range(1, expected_boards + 1)]
    calls = {"wait": 0, "persist": 0}
    monkeypatch.setattr(
        direct_browser,
        "_list_tabs",
        lambda: [{"tabId": "tab-current", "active": True}],
    )
    monkeypatch.setattr(direct_browser, "_activate_tab", lambda tab_id: True)
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda **kwargs: {
            "userMessageTexts": [f"execution_id={packet['execution_id']}"],
            "messageTexts": [],
            "generatedImages": [],
            "busy": True,
            "url": "https://chatgpt.com/c/current",
        },
    )
    monkeypatch.setattr(direct_browser, "_recover_visible_visuals", lambda *args, **kwargs: None)

    def wait_for_existing(*args, **kwargs):
        calls["wait"] += 1
        return {
            "generatedImages": generated_boards,
            "messageTexts": [],
            "text": "",
            "url": "https://chatgpt.com/c/current",
            "busy": False,
        }

    def persist_existing(output_dir, stage, images, expected):
        calls["persist"] += 1
        assert images == generated_boards
        assert expected == expected_boards
        return [str(tmp_path / f"visual_preview-{index}.png") for index in range(1, expected + 1)]

    monkeypatch.setattr(direct_browser, "_wait_for_answer", wait_for_existing)
    monkeypatch.setattr(direct_browser, "_persist_visuals", persist_existing)

    result = _collect_inflight_visual_from_project_tabs(
        packet,
        tmp_path,
        "VISUAL_PREVIEW",
    )

    assert calls == {"wait": 1, "persist": 1}
    assert result is not None
    assert result[1] == "https://chatgpt.com/c/current"


def test_generated_visual_is_bound_to_the_exact_request_turn():
    state = {
        "conversationRecords": [
            {"role": "user", "text": "old request", "generatedImages": []},
            {"role": "assistant", "text": "", "generatedImages": ["old-image"]},
            {"role": "user", "text": "request marker::visual-board:02/02", "generatedImages": []},
            {"role": "assistant", "text": "", "generatedImages": ["board-2-image"]},
        ]
    }

    assert _generated_images_for_request(state, "marker::visual-board:02/02") == ["board-2-image"]


def test_visual_board_marker_is_stable_and_ordered():
    packet = {"execution_id": "cf_test:visual:1"}

    assert _visual_board_execution_marker(packet, 1, 2) == "cf_test:visual:1::visual-board:01/02"
    assert _visual_board_execution_marker(packet, 2, 2) == "cf_test:visual:1::visual-board:02/02"

    regenerated = {
        "execution_id": "cf_test:visual:1",
        "force_fresh_response": True,
        "visual_fresh_regeneration_count": 2,
        "automatic_retry_count": 4,
    }
    assert _visual_board_execution_marker(regenerated, 1, 2) == (
        "cf_test:visual:1::visual-generation:04::visual-board:01/02"
    )


def test_board_submission_requires_exact_uncorrupted_marker_even_while_busy():
    prompt = (
        "payload\nBROWSER REQUEST MARKER (idempotency only; do not reproduce): "
        "cf_exact:1553:41:v01::visual-board:01/02"
    )
    marker = _prompt_submission_marker(prompt)
    assert marker == "cf_exact:1553:41:v01::visual-board:01/02"
    assert _prompt_text_submitted(
        {
            "busy": True,
            "count": 2,
            "userMessageTexts": ["cf_exct:1553:41:v01::visual-board:01/02"],
        },
        prompt,
        previous_count=1,
    ) is False
    assert _prompt_text_submitted(
        {
            "busy": True,
            "count": 2,
            "userMessageTexts": [marker],
        },
        prompt,
        previous_count=1,
    ) is True


def test_prompt_text_chunks_round_trip_without_mutating_payload():
    prompt = ("alpha beta gamma\n" * 180) + "BROWSER REQUEST MARKER: cf_exact:42"
    chunks = _prompt_text_chunks(prompt, max_chars=420)

    assert len(chunks) > 1
    assert all(1 <= len(chunk) <= 420 for chunk in chunks)
    assert "".join(chunks) == prompt


def test_composer_integrity_tolerates_only_prosemirror_layout_whitespace():
    expected = "Role prompt\n\nPROJECT cf_e64eaf7b1b894012b893: board 1/2"
    rendered = "Role prompt\n\n\n\n\nPROJECT cf_e64eaf7b1b894012b893: board 1/2"
    rendered_with_editor_artifacts = "Role\u2060 prompt\ufeff\n\n\ufffcPROJECT cf_e64eaf7b1b894012b893: board 1/2"
    corrupted = "Role prompt\n\n\nPROJECT cf_e6af7b1b894012b893: board 1/2"

    assert _normalized_composer_text(rendered) == _normalized_composer_text(expected)
    assert _normalized_composer_text(rendered_with_editor_artifacts) == _normalized_composer_text(expected)
    assert _normalized_composer_text(corrupted) != _normalized_composer_text(expected)


def test_composer_integrity_difference_is_bounded_and_escaped():
    from app.services.hermes_agent.direct_browser import _composer_text_difference

    difference = _composer_text_difference("alpha marker-123 omega", "alpha marker-12X omega")

    assert "mismatch_at=" in difference
    assert "U+0033" in difference
    assert "U+0058" in difference
    assert len(difference) < 500


def test_stable_prompt_insertion_verifies_every_accumulated_chunk(monkeypatch):
    from app.services.hermes_agent import direct_browser

    prompt = ("ordered prompt line\n" * 120) + "cf_exact:1554:42:v01::visual-board:01/02"
    state = {"value": ""}
    inserted: list[str] = []

    def fake_run(*args, **kwargs):
        if args[:2] == ("keyboard", "inserttext"):
            inserted.append(str(args[2]))
            state["value"] += str(args[2])
        return ""

    monkeypatch.setattr(direct_browser, "_run", fake_run)
    monkeypatch.setattr(direct_browser, "_composer_text_value", lambda: state["value"])
    monkeypatch.setattr(direct_browser.time, "sleep", lambda *_args, **_kwargs: None)

    _insert_prompt_text_stably(prompt)

    assert len(inserted) > 1
    assert "".join(inserted) == prompt
    assert state["value"] == prompt


def test_sequential_visual_boards_skip_persisted_board_and_collect_only_missing(monkeypatch, tmp_path):
    from app.services.hermes_agent import direct_browser

    first = tmp_path / "visual_preview-board-01.png"
    first.write_bytes(b"board-one")
    second = tmp_path / "visual_preview-board-02.png"
    collected: list[int] = []

    def collect(*args, **kwargs):
        collected.append(int(kwargs["board_index"]))
        second.write_bytes(b"board-two")
        return "", "https://chatgpt.com/c/board-two", str(second)

    monkeypatch.setattr(direct_browser, "_collect_visual_board_from_project_tabs", collect)
    monkeypatch.setattr(
        direct_browser,
        "_run",
        lambda *args, **kwargs: pytest.fail("a completed or in-flight board must not be submitted again"),
    )
    boards = [
        ("board one", {"board_index": 1, "board_count": 2}),
        ("board two", {"board_index": 2, "board_count": 2}),
    ]

    result = _execute_visual_boards_sequentially(
        {"execution_id": "cf_test:visual:1"},
        tmp_path,
        "VISUAL_PREVIEW",
        [],
        "prompt __HERMES_SEQUENTIAL_BOARD_INSTRUCTION__",
        boards,
    )

    assert collected == [2]
    assert result[2] == [str(first), str(second)]


def test_sequential_visual_force_fresh_stops_exact_stalled_request_before_resubmit(monkeypatch, tmp_path):
    from app.services.hermes_agent import direct_browser

    marker = "cf_test:1557:45:v01::visual-generation:01::visual-board:01/01"
    observed: dict[str, object] = {"sent": 0}

    def collect(*args, **kwargs):
        observed["wait_if_busy"] = kwargs.get("wait_if_busy")
        return None

    def interrupt(value):
        observed["interrupted"] = value
        return True

    def persist(output_dir, stage, state, request_marker, board_index, board_count):
        target = output_dir / "visual_preview-1.png"
        target.write_bytes(b"fresh-board")
        return str(target)

    monkeypatch.setattr(direct_browser, "_collect_visual_board_from_project_tabs", collect)
    monkeypatch.setattr(direct_browser, "_interrupt_stalled_visual_request", interrupt)
    monkeypatch.setattr(direct_browser, "_activate_project_visual_tab", lambda *_args: True)
    monkeypatch.setattr(direct_browser, "_ensure_normal_chat_for_visual_stage", lambda: None)
    monkeypatch.setattr(direct_browser, "_enter_chat_composer", lambda *_args: None)
    monkeypatch.setattr(direct_browser, "_dismiss_nonblocking_chatgpt_overlays", lambda: None)
    monkeypatch.setattr(direct_browser, "_clear_composer_strict", lambda: None)
    monkeypatch.setattr(direct_browser, "_fill_prompt_text", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(direct_browser, "_wake_composer_for_send", lambda *_args: None)
    monkeypatch.setattr(direct_browser, "_wait_until_sendable", lambda **_kwargs: None)
    monkeypatch.setattr(direct_browser, "_pace_before_send", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(direct_browser, "_wait_selector", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(direct_browser, "_record_chatgpt_success", lambda: None)
    monkeypatch.setattr(direct_browser, "_page_state", lambda: {"count": 1, "generatedImages": []})
    monkeypatch.setattr(
        direct_browser,
        "_send_prompt",
        lambda *_args: observed.__setitem__("sent", int(observed["sent"]) + 1),
    )
    monkeypatch.setattr(
        direct_browser,
        "_wait_for_answer",
        lambda *_args, **_kwargs: {"url": "https://chatgpt.com/c/fresh", "newImages": ["fresh"]},
    )
    monkeypatch.setattr(direct_browser, "_persist_visual_board_from_state", persist)

    result = _execute_visual_boards_sequentially(
        {"execution_id": "cf_test:1557:45:v01", "force_fresh_response": True},
        tmp_path,
        "VISUAL_PREVIEW",
        [],
        "prompt __HERMES_SEQUENTIAL_BOARD_INSTRUCTION__",
        [("board one", {"board_index": 1, "board_count": 1})],
    )

    assert observed["wait_if_busy"] is False
    assert observed["interrupted"] == marker
    assert observed["sent"] == 1
    assert result[2] == [str(tmp_path / "visual_preview-1.png")]


def test_sequential_visual_force_fresh_rescues_turn_that_finishes_during_interrupt(monkeypatch, tmp_path):
    from app.services.hermes_agent import direct_browser

    marker = "cf_test:1557:45:v01::visual-generation:01::visual-board:01/01"
    target = tmp_path / "visual_preview-1.png"
    calls = {"collect": 0, "sent": 0}

    def collect(*args, **kwargs):
        calls["collect"] += 1
        assert kwargs["wait_if_busy"] is False
        if calls["collect"] == 1:
            return None
        target.write_bytes(b"late-completed-board")
        return "", "https://chatgpt.com/c/late", str(target)

    monkeypatch.setattr(direct_browser, "_collect_visual_board_from_project_tabs", collect)
    monkeypatch.setattr(direct_browser, "_interrupt_stalled_visual_request", lambda value: value == marker)
    monkeypatch.setattr(
        direct_browser,
        "_send_prompt",
        lambda *_args: calls.__setitem__("sent", calls["sent"] + 1),
    )

    result = _execute_visual_boards_sequentially(
        {"execution_id": "cf_test:1557:45:v01", "force_fresh_response": True},
        tmp_path,
        "VISUAL_PREVIEW",
        [],
        "prompt __HERMES_SEQUENTIAL_BOARD_INSTRUCTION__",
        [("board one", {"board_index": 1, "board_count": 1})],
    )

    assert calls == {"collect": 2, "sent": 0}
    assert result[2] == [str(target)]


def test_generated_images_prefers_newest_completed_matching_turn():
    from app.services.hermes_agent.direct_browser import _generated_images_for_request

    marker = "cf_exact:1557:45:v01::visual-board:01/02"
    state = {
        "conversationRecords": [
            {"role": "user", "text": marker},
            {"role": "assistant", "generatedImages": ["https://image/first.png"]},
            {"role": "user", "text": marker},
            {"role": "assistant", "generatedImages": []},
        ]
    }

    assert _generated_images_for_request(state, marker) == ["https://image/first.png"]


def test_visual_request_turn_status_distinguishes_absent_busy_terminal_and_completed():
    from app.services.hermes_agent.direct_browser import _visual_request_turn_status

    marker = "cf_exact:1557:45:v01::visual-board:01/02"
    assert _visual_request_turn_status({"conversationRecords": []}, marker) == "absent"
    assert _visual_request_turn_status({
        "busy": True,
        "conversationRecords": [{"role": "user", "text": marker}],
    }, marker) == "busy"
    assert _visual_request_turn_status({
        "busy": False,
        "conversationRecords": [
            {"role": "user", "text": marker},
            {"role": "assistant", "text": "", "generatedImages": []},
        ],
    }, marker) == "terminal_no_media"
    assert _visual_request_turn_status({
        "busy": False,
        "conversationRecords": [
            {"role": "user", "text": marker},
            {"role": "assistant", "text": "", "generatedImages": ["board.png"]},
        ],
    }, marker) == "completed"


def test_canonical_conversation_records_collapses_nested_react_branches():
    from app.services.hermes_agent.direct_browser import _canonical_conversation_records

    generation_marker = (
        "BROWSER REQUEST MARKER: "
        "cf_test:1561:49:v01::visual-generation:01::visual-board:01/02"
    )
    records = _canonical_conversation_records([
        {"role": "user", "text": generation_marker, "generatedImages": []},
        {
            "role": "user",
            "text": "BROWSER REQUEST MARKER: cf_test:1561:49:v01::visual-board:01/02",
            "generatedImages": [],
        },
        {"role": "assistant", "text": "", "generatedImages": []},
        {"role": "assistant", "text": "Image ready", "generatedImages": ["board.png"]},
    ])

    assert len(records) == 2
    assert records[0]["role"] == "user"
    assert "visual-generation:01" in records[0]["text"]
    assert records[1]["role"] == "assistant"
    assert records[1]["text"] == "Image ready"
    assert records[1]["generatedImages"] == ["board.png"]


def test_terminal_visual_marker_refuses_duplicate_same_generation(monkeypatch, tmp_path):
    from app.services.hermes_agent import direct_browser

    marker = "cf_exact:1557:45:v01::visual-board:01/02"
    monkeypatch.setattr(direct_browser, "_list_tabs", lambda: [{"tabId": "target", "active": True}])
    monkeypatch.setattr(direct_browser, "_activate_tab", lambda _tab_id: True)
    monkeypatch.setattr(direct_browser.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(direct_browser, "_raise_if_chatgpt_login_required", lambda _state: None)
    monkeypatch.setattr(direct_browser, "_raise_if_rate_limited", lambda _state: None)
    monkeypatch.setattr(
        direct_browser,
        "_page_state",
        lambda **_kwargs: {
            "busy": False,
            "url": "https://chatgpt.com/c/terminal",
            "userMessageTexts": [marker],
            "conversationRecords": [
                {"role": "user", "text": marker},
                {"role": "assistant", "text": "", "generatedImages": []},
            ],
        },
    )

    with pytest.raises(direct_browser.ChatGPTStageError, match="TERMINAL_NO_MEDIA"):
        direct_browser._collect_visual_board_from_project_tabs(
            {"execution_id": "cf_exact:1557:45:v01"},
            tmp_path,
            "VISUAL_PREVIEW",
            marker=marker,
            board_index=1,
            board_count=2,
        )


def test_stalled_visual_interruption_is_scoped_to_exact_marker(monkeypatch):
    from app.services.hermes_agent import direct_browser

    marker = "cf_exact:1557:45:v01::visual-board:01/02"
    active = {"tab": "", "target_reads": 0}
    evaluated: list[str] = []

    monkeypatch.setattr(
        direct_browser,
        "_list_tabs",
        lambda: [
            {"tabId": "other", "active": True},
            {"tabId": "target", "active": False},
        ],
    )

    def activate(tab_id):
        active["tab"] = tab_id
        return True

    def state(*_args, **_kwargs):
        if active["tab"] == "other":
            return {"busy": True, "userMessageTexts": ["cf_unrelated"]}
        active["target_reads"] += 1
        return {
            "busy": active["target_reads"] == 1,
            "userMessageTexts": [marker],
        }

    monkeypatch.setattr(direct_browser, "_activate_tab", activate)
    monkeypatch.setattr(direct_browser, "_page_state", state)
    monkeypatch.setattr(direct_browser.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        direct_browser,
        "_eval",
        lambda expression, **_kwargs: evaluated.append(expression) or True,
    )

    assert direct_browser._interrupt_stalled_visual_request(marker) is True
    assert len(evaluated) == 1


def test_browser_visual_output_is_isolated_by_variant_and_execution():
    project = SimpleNamespace(workspace_id=3, project_key="cf_isolated")
    stage = SimpleNamespace(id=1552, attempt=40, stage="VISUAL_PREVIEW")

    path = _browser_stage_output_path(project, stage, 2)

    assert str(path).endswith("workspace_3/cf_isolated/VISUAL_PREVIEW/variant_02/execution_1552_40")


def test_multi_board_grid_repair_does_not_demand_full_sequence_in_each_board():
    instruction = _visual_grid_repair_instruction(
        None,
        SimpleNamespace(),
        expected_panel_count=9,
        repair_attempt=1,
    )

    assert "ordered set of clean board images" in instruction
    assert "one local board at a time" in instruction
    assert "Do not squeeze the full sequence into every board" in instruction
    assert "exactly one clean board containing 9" not in instruction


def test_explicit_product_free_keyframe_does_not_upload_product():
    packet = _packet()
    item = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_plan"][0]
    item["description"] = (
        "Full-body skeleton sprints through a hospital hallway; no product or branding."
    )

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["plan"][0]["requires_product_reference"] is False
    assert visual_generation_reference_paths(packet) == ["/tmp/character.png"]
    assert "This entire board is product-free" in prompt
    assert "input 1: character.png [character_reference]" in prompt
    assert "input 2:" not in prompt
    assert "product.png [product_visual]" not in prompt
    assert "skeleton sprints through a hospital hallway" in prompt


def test_visual_panel_count_is_pinned_to_the_current_stage_snapshot():
    assert _visual_expected_panel_count(
        {"visual_api": {"expected_panels": 4}, "expected_reference_count": 5}
    ) == 4
    assert _visual_expected_panel_count({"visual_api": {"expected_panels": "6"}}) == 6
    assert _visual_expected_panel_count({"visual_api": {"expected_panels": "bad"}}) is None


def test_creative_review_preflight_requires_every_generated_reference(tmp_path):
    first = tmp_path / "reference-01.png"
    first.write_bytes(b"png")
    product = tmp_path / "product.png"
    product.write_bytes(b"product")
    creative = {
        "visual_job_ticket": {
            "reference_image_count": 4,
            "reference_plan": [
                {"index": index, "segment": index, "description": f"beat {index}"}
                for index in range(1, 5)
            ],
        },
    }
    preview = SimpleNamespace(
        id=11,
        file_path=str(first),
        original_name=first.name,
        meta_json={"reference_index": 1, "global_start_index": 1},
    )
    product_anchor = SimpleNamespace(
        id=12,
        file_path=str(product),
        original_name=product.name,
        meta_json={"asset_role": "product_visual"},
    )

    status = _creative_review_reference_asset_status(creative, [preview])
    product_only_status = _creative_review_reference_asset_status(
        creative,
        [product_anchor],
    )

    assert status["complete"] is False
    assert status["covered_indices"] == [1]
    assert status["missing_indices"] == [2, 3, 4]
    assert product_only_status["complete"] is False


def test_visual_board_reference_meta_preserves_full_board_range(tmp_path):
    board = tmp_path / "visual-preview-api-board-01.png"
    board.write_bytes(b"png")
    evidence = {
        "api": {
            "boards": [{
                "board_index": 1,
                "output_path": str(board),
                "expected_panels": 4,
                "global_start_index": 1,
                "global_end_index": 4,
            }],
        },
    }

    meta = _visual_board_reference_meta(evidence)
    assert meta[str(board)] == {
        "reference_index": 1,
        "global_start_index": 1,
        "global_end_index": 4,
        "expected_panels": 4,
    }

    preview = SimpleNamespace(
        id=99,
        file_path=str(board),
        original_name=board.name,
        meta_json=meta[board.name],
    )
    creative = {
        "visual_job_ticket": {
            "reference_image_count": 4,
            "reference_plan": [{"index": index} for index in range(1, 5)],
        },
    }
    status = _creative_review_reference_asset_status(creative, [preview])
    assert status["complete"] is True
    assert status["covered_indices"] == [1, 2, 3, 4]
    assert status["missing_indices"] == []


def test_paid_visual_board_is_split_locally_into_native_reference_files(tmp_path):
    board = Image.new("RGB", (1100, 1940), "white")
    colors = [(28, 42, 70), (140, 80, 30), (55, 105, 80), (90, 45, 110)]
    cells = [(0, 0, 540, 960), (560, 0, 540, 960), (0, 980, 540, 960), (560, 980, 540, 960)]
    for color, (x, y, width, height) in zip(colors, cells):
        board.paste(Image.new("RGB", (width, height), color), (x, y))
    source = tmp_path / "paid-board.png"
    board.save(source)

    rows = _split_visual_board_native_files(
        source,
        start_index=1,
        panel_count=4,
        output_dir=tmp_path / "native",
        aspect_ratio="9:16",
    )

    assert [row["index"] for row in rows] == [1, 2, 3, 4]
    assert len({row["path"] for row in rows}) == 4
    for row in rows:
        target = Path(row["path"])
        assert target.is_file()
        with Image.open(target) as image:
            assert image.size == (1080, 1920)


def test_quality_pause_preserves_latest_visual_evidence():
    assert _creative_review_asset_cleanup_allowed(
        creative_replan_exhausted=False,
    ) is True
    assert _creative_review_asset_cleanup_allowed(
        creative_replan_exhausted=True,
    ) is False




















def test_paused_visual_checkpoint_keeps_completed_and_unambiguous_inflight_boards(tmp_path):
    completed = tmp_path / "reference-01.png"
    completed.write_bytes(b"paid-result")
    missing = tmp_path / "reference-02.png"
    stage = SimpleNamespace(
        id=1973,
        stage="VISUAL_PREVIEW",
        instruction="Preserve this exact visual direction.",
        input_json={
            "variant_index": 25,
            "api_route": "toapis:gpt-image-2",
            "visual_api": {
                "provider": "toapis",
                "status": "completed",
                "boards": {
                    "1": {
                        "status": "completed",
                        "task_id": "paid-1",
                        "output_path": str(completed),
                    },
                    "2": {
                        "status": "completed",
                        "task_id": "missing-file",
                        "output_path": str(missing),
                    },
                    "3": {
                        "status": "submitted",
                        "task_id": "in-flight",
                    },
                    "4": {
                        "status": "submitted",
                        "task_id": "recoverable-in-flight",
                        "prompt_digest": "stable-prompt-digest",
                    },
                },
            },
        },
    )

    checkpoint = _resumable_visual_api_checkpoint(stage)

    assert checkpoint["source_stage_id"] == 1973
    assert checkpoint["source_instruction"] == (
        "Preserve this exact visual direction."
    )
    assert checkpoint["variant_index"] == 25
    assert checkpoint["api_route"] == "toapis:gpt-image-2"
    assert list(checkpoint["visual_api"]["boards"]) == ["1", "4"]
    assert checkpoint["visual_api"]["boards"]["1"]["task_id"] == "paid-1"
    assert checkpoint["visual_api"]["boards"]["4"]["task_id"] == (
        "recoverable-in-flight"
    )
    assert checkpoint["visual_api"]["status"] == "partial_resumable"


def test_visual_checkpoint_keeps_paid_scene_after_product_placement_false_negative(
    tmp_path,
):
    raw_scene = tmp_path / "paid-product-free-scene.png"
    raw_scene.write_bytes(b"paid-provider-result")
    stage = SimpleNamespace(
        id=2380,
        stage="VISUAL_PREVIEW",
        instruction="",
        input_json={
            "variant_index": 9,
            "visual_api": {
                "provider": "bandianwa",
                "status": "quality_replan_required",
                "boards": {
                    "5": {
                        "status": "failed",
                        "failure_class": "product_scene_unplaceable",
                        "output_path": None,
                        "generated_scene_source_path": str(raw_scene),
                    },
                },
            },
        },
    )

    checkpoint = _resumable_visual_api_checkpoint(stage)

    board = checkpoint["visual_api"]["boards"]["5"]
    assert board["generated_scene_source_path"] == str(raw_scene)
    assert checkpoint["source_stage_id"] == 2380


def test_browser_multiboard_validation_uses_each_board_local_panel_count():
    first = "/tmp/execution_1556_44/visual_preview-board-01.png"
    second = "/tmp/execution_1556_44/visual_preview-board-02.png"
    counts = _visual_board_expected_counts(
        {
            "visual_boards": [
                {"output_path": first, "expected_panels": 5},
                {"output_path": second, "expected_panels": 4},
            ]
        }
    )

    assert counts[first] == 5
    assert counts["visual_preview-board-01.png"] == 5
    assert counts[second] == 4
    assert counts["visual_preview-board-02.png"] == 4


def test_explicit_approved_review_is_not_mistaken_for_visual_rejection():
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": False,
            "creative_review": (
                "Approved. The board contains exactly 4 visible ordered panels matching the "
                "reference plan. No disqualifying wrong product or broken continuity detected."
            ),
        },
        "issues": [],
        "repair_brief": "Creative review found a blocking visual mismatch.",
    }

    assert _creative_review_false_negative(envelope) is True


def test_explicit_approved_review_with_blocking_issue_still_requires_repair():
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": False,
            "creative_review": "Approved with one blocking package defect.",
        },
        "issues": [{"severity": "high", "issue": "Package is deformed"}],
    }

    assert _creative_review_false_negative(envelope) is False


def test_rejected_photoreal_review_is_a_hard_rejection_even_with_positive_checks():
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": False,
            "creative_review": (
                "Rejected. The board has correct ordered panels and no nested frames, "
                "but it is photorealistic/live-action-looking rather than the required illustration."
            ),
            "repair_brief": "Regenerate with visibly illustrated 2D characters and lighting.",
        },
        "issues": [],
    }

    assert _creative_review_has_hard_rejection(envelope) is True
    assert _creative_review_false_negative(envelope) is False


def test_generated_package_label_mismatch_uses_the_separate_product_anchor():
    packet = _packet()
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": False,
            "creative_review": (
                "The illustrated board is in chronological order with no continuity problem, but panel 4 has "
                "an incorrect purple package with mismatched branding instead of the approved blue MYUPONA product."
            ),
            "repair_brief": "Replace the incorrect purple package with the exact product reference.",
        },
        "issues": [],
    }

    assert _creative_review_is_generated_package_only_rejection(envelope, packet) is True


def test_package_anchor_acknowledgement_does_not_hide_duplicate_character_rejection():
    packet = _packet()
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": (
                "Rejected. Frame 3 contains a broken duplicate-character continuity error. "
                "The separate uploaded product_visual is present and correctly serves as package authority."
            ),
            "approved_for_split": True,
            "reference_image_count": 4,
            "repair_brief": None,
        },
        "issues": [
            "Broken character continuity in frame 3.",
            "Wrong scene/action and missing final doorway exchange in frame 4.",
            "Frame 4 does not provide the required placement surface for the separate product input.",
        ],
        "repair_brief": None,
    }

    assert _creative_review_is_generated_package_only_rejection(envelope, packet) is False
    assert _creative_review_has_hard_rejection(envelope, envelope["result"]) is True
    assert _creative_review_is_explicit_clean_approval(envelope, envelope["result"]) is False


def test_package_anchor_acknowledgement_does_not_hide_missing_resolution_rejection():
    packet = _packet()
    envelope = {
        "status": "PASS",
        "result": {
            "creative_review": (
                "Four generated reference files are present in chronological order. "
                "Reference 4 is not an acceptable match for the planned resolution: it shows Mara handing "
                "books to Jo while Jo remains seated, rather than the required calm doorway exchange. "
                "The generated frames contain no product, which is correct, and the separately uploaded "
                "original product_visual is present and is not counted as a storyboard row."
            ),
            "approved_for_split": False,
            "reference_image_count": 4,
        },
        "issues": [],
        "repair_brief": None,
    }

    assert _creative_review_is_generated_package_only_rejection(envelope, packet) is False
    assert _creative_review_has_hard_rejection(envelope, envelope["result"]) is True
    assert _creative_review_is_explicit_clean_approval(envelope, envelope["result"]) is False


def test_product_visual_prompt_uses_provider_reference_not_local_composite():
    packet = _packet()
    packet["user_instruction"] = "Use American adult 2D editorial animation, not photoreal people."
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 1,
        "reference_plan": [{
            "index": 1,
            "segment": 1,
            "description": "The adult places the MYUPONA bottle on a bedside table as a routine prop.",
            "roles": ["character_anchor", "action_anchor"],
            "requires_product_reference": True,
            "authoritative_product_composite": {
                "placement": "lower_right",
                "width_fraction": 0.32,
            },
        }],
    }

    visual_prompt, _spec = build_visual_api_prompt(packet)
    _system, review_prompt = build_text_api_request(packet, "CREATIVE_REVIEW")
    ordered_shot = visual_prompt.split("SCENE REQUIREMENT:", 1)[1].split("INPUT REFERENCES:", 1)[0]

    assert "bedside table" in ordered_shot
    assert "uploaded product reference as the authoritative identity" in visual_prompt
    assert "render it naturally with the scene lighting" in visual_prompt
    assert "Do not paste the uploaded image or its white background" in visual_prompt
    assert "HARD COMPOSITE SAFE ZONE" not in visual_prompt
    assert "product.png [product_visual]" in visual_prompt
    assert "character.png" in visual_prompt
    assert "product_presentation_policy" in review_prompt
    assert "separate uploaded product_visual authority" in review_prompt
    assert "must place the product naturally in the scripted scene" in review_prompt
    assert "never accept a pasted source rectangle" in review_prompt
    assert "COMPOSITE OCCLUSION TEST" not in review_prompt


def test_all_individual_visual_prompts_remove_shared_product_trigger_terms():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["user_instruction"] = (
        "American adult animation. The exact uploaded MYUPONA package is the sole product authority."
    )
    packet["visual_style_requirement"] = (
        "Keep one room throughout. Do not redraw the product bottle or logo."
    )
    packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"] = {
        "reference_image_count": 4,
        "reference_plan": [
            {
                "index": index,
                "segment": index,
                "description": (
                    "The adult completes the evening reset beside a clear lamp-side table."
                    if index == 4
                    else f"The same adult performs story beat {index} in the living room."
                ),
                "roles": ["character_anchor", "action_anchor"],
                "requires_product_reference": index == 4,
            }
            for index in range(1, 5)
        ],
    }

    prompts = build_visual_api_prompts(packet)

    assert len(prompts) == 4
    assert all(
        not content_factory_api._IMAGE_PROMPT_PRODUCT_TRIGGER_RE.search(prompt)
        for prompt, _spec in prompts
    )


def test_product_visual_action_strips_opening_and_consumption_instructions():
    action = (
        "She places the MYUPONA bottle on the table, opens the bottle, takes out exactly two purple gummies, "
        "and swallows them before returning to her book."
    )

    normalized = _sanitize_product_visual_action(
        action,
        {
            "forbidden_interaction_categories": [
                "open_package",
                "expose_loose_contents",
                "consume_product",
            ],
            "presentation_instructions": [
                "Keep the authoritative package sealed and closed.",
            ],
        },
    )

    assert "opens" not in normalized.lower()
    assert "takes out" not in normalized.lower()
    assert "swallows" not in normalized.lower()
    assert "places the myupona bottle on the table" in normalized.lower()


def test_closed_package_rewrite_never_leaves_an_impossible_pour_action():
    action = (
        "She opens the MYUPONA bottle, pours two blueberry-colored gummies "
        "into her palm, and holds them beside the bottle."
    )

    normalized = _sanitize_product_visual_action(
        action,
        {
            "forbidden_interaction_categories": ["open_package"],
            "presentation_instructions": [
                "Keep the authoritative package sealed and closed.",
            ],
        },
    )

    lowered = normalized.lower()
    assert "sealed and closed" in lowered
    assert "pours" not in lowered
    assert "previously prepared loose serving" in lowered
    assert "closed package" in lowered


def test_provider_safe_retry_prompt_preserves_signed_product_action_idempotently():
    source = "Eli opens the approved coffee pouch and pours the beans into a grinder."

    first = _provider_safe_base_prompt(source)
    second = _provider_safe_base_prompt(first)

    assert "opens the approved coffee pouch" in first.lower()
    assert "pours the beans" in first.lower()
    assert second == first


def test_explicit_approved_review_wins_over_negated_format_wording():
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": False,
            "creative_review": (
                "Approved. This is not a nested collage and no wrong product is present."
            ),
        },
        "issues": [],
    }

    assert _creative_review_false_negative(envelope) is True


def test_structured_clean_approval_wins_over_negated_blocking_keywords():
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": True,
            "creative_review": (
                "Approved. No unrelated imagery is visible and there is no wrong product."
            ),
        },
        "issues": [],
    }

    assert _creative_review_is_explicit_clean_approval(envelope) is True


def test_structured_approval_with_real_blocking_issue_is_not_clean():
    envelope = {
        "status": "PASS",
        "result": {
            "approved_for_split": True,
            "creative_review": "Approved except for a blocking package defect.",
        },
        "issues": [{"severity": "high", "issue": "The package is deformed."}],
    }

    assert _creative_review_is_explicit_clean_approval(envelope) is False


def test_visual_prompt_prefers_detailed_ticket_and_repair_over_raw_project_brief():
    packet = _packet()
    packet["project_requirements"] = "CONFLICTING RAW BRIEF: use a bedroom and show ingestion."
    packet["visual_repair_instruction"] = (
        "Use only the same moonlit balcony. Keep the bottle sealed."
    )
    ticket = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    ticket["reference_image_count"] = 2
    ticket["reference_plan"] = [
        {"index": 1, "segment": 1, "description": "Reference panel 1", "roles": ["scene_anchor"]},
        {"index": 2, "segment": 1, "description": "Reference panel 2", "roles": ["product_anchor"]},
    ]
    ticket["reference_panels"] = [
        {
            "name": "Balcony master",
            "purpose": "Lock the creator and telescope geography.",
            "composition": "Vertical medium-wide shot on a moonlit balcony.",
        },
        {
            "name": "Sealed product hero",
            "purpose": "Lock the exact MYUPONA package.",
            "composition": "Sealed bottle on the balcony table, label facing camera.",
        },
    ]
    prompt, spec = build_visual_api_prompt(packet)
    assert spec["count"] == 2
    assert "Balcony master" in prompt
    assert "Sealed product hero" not in prompt
    assert "Do not create a standalone product" in prompt
    assert "MANDATORY REPAIR OVERRIDE" in prompt
    assert "same moonlit balcony" in prompt
    assert "CONFLICTING RAW BRIEF" not in prompt


def test_creative_normalization_removes_generated_product_anchor():
    result = {
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [
                {
                    "index": 1,
                    "segment": 1,
                    "description": "Exact MYUPONA bottle and label product hero",
                    "roles": ["product_anchor"],
                }
            ],
        }
    }

    normalized = _ensure_reference_plan(result)
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert "product_anchor" not in item["roles"]
    assert item["roles"]
    assert "standalone product" in item["description"]
    assert "uploaded product image" in item["description"]


def test_creative_normalization_preserves_scripted_product_interaction_without_making_product_anchor():
    result = {
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [
                {
                    "index": 1,
                    "segment": 1,
                    "description": "Woman holds the MYUPONA bottle beside the bed.",
                    "roles": ["character_anchor", "action_anchor", "product_anchor"],
                }
            ],
        }
    }

    normalized = _ensure_reference_plan(result)
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert "product_anchor" not in item["roles"]
    assert "Woman holds the MYUPONA bottle" in item["description"]
    assert "copy it only from the uploaded product reference" in item["description"]


def test_creative_normalization_preserves_product_free_contract():
    result = {
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [
                {
                    "index": 1,
                    "segment": 1,
                    "description": "Skeleton points at a thought cloud; no product or branding.",
                    "roles": ["character_anchor", "action_anchor"],
                    "requires_product_reference": True,
                }
            ],
        }
    }

    normalized = _ensure_reference_plan(result)
    ticket = normalized["visual_job_ticket"]
    item = ticket["reference_plan"][0]

    assert item["requires_product_reference"] is False
    assert "Skeleton points at a thought cloud" in item["description"]
    assert "whole board is product-free" in ticket["board_rule"]


@pytest.mark.parametrize(
    "description",
    [
        "Skeleton runs down the hall; no product-like items, packaging, labels, logos, or branded objects.",
        "Fast chase with no readable text, products, labels, or branding.",
        "Scene completely free of products, bottles, labels, logos, or branded objects.",
    ],
)
def test_product_free_list_phrases_do_not_request_product_upload(description):
    result = {
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [{
                "index": 1,
                "description": description,
                "roles": ["action_anchor"],
                "requires_product_reference": True,
            }],
        }
    }

    normalized = _ensure_reference_plan(result)
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert item["requires_product_reference"] is False
    assert "This keyframe is product-free" in item["description"]
    assert "If the product appears" not in item["description"]


def test_stage_packet_product_upload_decision_ignores_stale_boolean():
    plan = [{
        "description": "Skeleton points at a thought cloud; no product or branding.",
        "requires_product_reference": True,
    }]

    assert _visual_plan_needs_product_reference(plan) is False


def test_stage_packet_product_upload_decision_keeps_scripted_interaction():
    plan = [{
        "description": "The woman holds the exact MYUPONA bottle beside the bed.",
        "requires_product_reference": False,
    }]

    assert _visual_plan_needs_product_reference(plan) is True


def test_stage_packet_product_upload_decision_keeps_signed_product_authority():
    plan = [{
        "description": "Reserve clean lower-center space in the room.",
        "requires_product_reference": True,
        "source_asset_refs": ["asset:2754"],
        "authoritative_product_composite": {
            "placement": "lower_center",
            "width_fraction": 0.34,
            "entrance": "fade",
        },
    }]

    assert _visual_plan_needs_product_reference(plan) is True


def test_closed_product_handling_is_not_mistaken_for_product_exclusion():
    action = (
        "The sealed MYUPONA Sleep Easy Gummies bottle sits upright on the walnut console. "
        "Rowan straightens the closed bottle without opening it; no loose gummies or consumption."
    )

    assert visual_reference_mentions_product(action) is True
    assert visual_reference_requires_product(action) is True


def test_short_segment_plan_keeps_static_terminal_state_and_product_authority():
    result = {
        "complete_video_script": {
            "segments": [
                {
                    "segment_index": 1,
                    "story_function": "ROUTINE STEP AND CTA",
                    "visual_action": (
                        "The sealed MYUPONA Sleep Easy Gummies bottle sits upright on the walnut console. "
                        "Rowan straightens the closed bottle without opening it; no loose gummies or consumption."
                    ),
                }
            ],
        },
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [{
                "index": 1,
                "segment": 1,
                "description": "Generic resolution reference.",
                "roles": ["action_anchor"],
            }],
        },
    }

    normalized = _ensure_reference_plan_segment_coverage(
        _ensure_reference_plan(result),
    )
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert item["requires_product_reference"] is True
    assert item["single_frame_terminal_state"]
    assert "MYUPONA" in item["single_frame_terminal_state"]


def test_static_reference_compiler_collapses_out_and_back_action():
    state = visual_reference_static_state(
        "Maya exits the kitchen doorway carrying a covered dessert dish toward the hallway console, "
        "then turns back and returns it untouched to the kitchen counter. "
        "Rowan stands beside the office door, watching too late.",
        index=1,
    )

    assert "carrying" not in state.lower()
    assert "then turns back" not in state.lower()
    assert "dessert dish rests there" in state.lower()
    assert "Rowan stands beside the office door" in state


def test_static_reference_compiler_preserves_place_nouns_and_rewrites_action():
    state = visual_reference_static_state(
        "Mara tries to shift toward the remote but is held in place for a beat. "
        "Four empty place cards sit beside a dim lamp and a miniature film set. "
        "Mara places her phone face-down, closes the movie app, and dims the lamp.",
        index=1,
    )

    assert "held in place" in state
    assert "empty place cards" in state
    assert "miniature film set" in state
    assert "has placed her phone face-down" in state
    assert "has closed the movie app" in state
    assert "has visibly dimmed the lamp" in state
    assert "in has placed" not in state
    assert "has placed cards" not in state


def test_static_reference_compiler_collapses_hallway_relationship_sequence():
    actions = [
        (
            "Maya enters the apartment hallway carrying a closed laptop against her chest. "
            "Renee unexpectedly steps out from the kitchen doorway, sees Maya, then quietly passes "
            "her toward the bedroom door without their familiar greeting."
        ),
        (
            "Maya turns and follows Renee briskly down the hallway. Renee stops at the threshold "
            "but does not turn fully around as she speaks."
        ),
        (
            "Maya stands alone after Renee closes the bedroom door. She looks toward her still-open "
            "home office, walks there, closes her laptop, silences her phone, turns off the office "
            "light, then returns to the hallway and dims an amber sconce."
        ),
    ]

    states = [
        visual_reference_static_state(action, index=index)
        for index, action in enumerate(actions, 1)
    ]

    assert "Maya stands in the apartment hallway holding a closed laptop" in states[0]
    assert "Renee stands near the bedroom door" in states[0]
    assert "familiar greeting" not in states[0]
    assert "visible physical distance" in states[0]
    assert "Maya stands several feet behind Renee" in states[1]
    assert "her laptop is closed" in states[2]
    assert "her phone screen is dark" in states[2]
    assert "office light is off" in states[2]
    assert "amber sconce emitting only a faint low glow" in states[2]
    assert "surrounding room is mostly dark" in states[2]
    assert not any(
        token in state.lower()
        for state in states
        for token in ("then ", " follows ", "walks there", "returns to")
    )


def test_visual_prompt_includes_authoritative_cast_and_location_locks():
    packet = _packet()
    creative = packet["previous_outputs"]["MEDIA_DESIGN"]
    creative["continuity_rules"] = {
        "camera_continuity": ["Fast backward tracking through the hallway."],
        "character_continuity": [
            "Maya is a 38-year-old woman with a short dark bob, charcoal knit cardigan, and black trousers.",
            "Renee is a 40-year-old woman with warm brown skin, long dark hair in a low bun, and a muted rust lounge set.",
            "Maya is the on-camera narrator in every segment.",
        ],
        "location_continuity": [
            "Use the same US apartment hallway with warm amber sconces, pale walls, dark wood doors, and a narrow walnut console.",
        ],
    }

    prompts = [prompt for prompt, _spec in build_visual_api_prompts(packet)]

    assert prompts
    assert all("AUTHORITATIVE CAST APPEARANCE" in prompt for prompt in prompts)
    assert all("short dark bob" in prompt and "warm brown skin" in prompt for prompt in prompts)
    assert all("AUTHORITATIVE LOCATION APPEARANCE" in prompt for prompt in prompts)
    assert all("narrow walnut console" in prompt for prompt in prompts)
    assert all("Fast backward tracking" not in prompt for prompt in prompts)
    assert all("on-camera narrator" not in prompt for prompt in prompts)


def test_visual_prompt_accepts_creative_lock_schema_aliases():
    packet = _packet()
    packet["previous_outputs"]["MEDIA_DESIGN"]["continuity_rules"] = {
        "character_lock": [
            {
                "character_id": "dara",
                "age": 38,
                "appearance": (
                    "Adult woman with a dark curly bob, charcoal cardigan over a rust sleep top, "
                    "black lounge pants, and house slippers."
                ),
            },
            {
                "character_id": "lena",
                "age": 41,
                "appearance": (
                    "Adult woman with straight dark-blonde hair in a low bun, navy raincoat over "
                    "office clothes, and a leather key lanyard."
                ),
            },
        ],
        "location_lock": (
            "One ordinary condo hallway at night with wall sconces, two apartment doors, "
            "and one narrow shared console table."
        ),
    }

    prompts = [prompt for prompt, _spec in build_visual_api_prompts(packet)]

    assert all("character_id: dara" in prompt for prompt in prompts)
    assert all("dark curly bob" in prompt and "dark-blonde hair in a low bun" in prompt for prompt in prompts)
    assert all("ordinary condo hallway at night" in prompt for prompt in prompts)


def test_static_reference_compiler_collapses_doorway_key_handoff():
    state = visual_reference_static_state(
        "Dara follows Lena through the hall, catching her at Lena's apartment doorway. "
        "Lena keeps the keys at her side and delivers the painful answer. "
        "Dara stands alone at the shared console; the hallway sconce is lowered.",
        index=2,
    )

    assert "At Lena's apartment doorway, Dara stands near Lena" in state
    assert "Lena holds the keys at her side with a guarded, resolved expression" in state
    assert "follows" not in state.lower()
    assert "catching" not in state.lower()
    assert "delivers" not in state.lower()
    assert "exactly one hallway sconce emits a faint low glow" in state
    assert "every other hallway light is visibly off" in state


def test_static_reference_compiler_makes_hidden_phone_and_nod_visible():
    opening_state = visual_reference_static_state(
        "Dara opens her apartment door into the nighttime hallway and freezes when "
        "she sees Lena at the opposite end holding the shared lobby keys.",
        index=1,
    )
    phone_state = visual_reference_static_state(
        "Recognition and routine bridge: Dara stands alone at the shared console. "
        "Her phone is put away in its closed drawer, the hallway sconce is lowered, "
        "and the lobby-key hook is empty beside her.",
        index=3,
    )
    nod_state = visual_reference_static_state(
        "Resolution: At Lena's doorway, Dara and Lena share a calm nod.",
        index=4,
    )

    assert "stands beside her open apartment door" in opening_state
    assert "visibly startled expression" in opening_state
    assert "opens" not in opening_state
    assert "freezes" not in opening_state
    assert "smartphone lies screen-down flat against the wood" in phone_state
    assert "black protective back and three circular rear lenses face the viewer" in phone_state
    assert "entire glass screen is pressed against the tabletop and cannot be seen" in phone_state
    assert "empty lobby-key hook is clearly visible in the foreground" in phone_state
    assert "no keys hanging from it" in phone_state
    assert "Recognition and routine bridge" not in phone_state
    assert "put away" not in phone_state
    assert "face each other with clear eye contact" in nod_state
    assert "calm softened expressions" in nod_state
    assert "heads slightly inclined" not in nod_state
    assert "share a calm nod" not in nod_state


def test_visual_grid_repair_budget_is_capped_to_three_total_generations(monkeypatch):
    monkeypatch.setenv("HERMES_VISUAL_GRID_CURRENT_REPAIR_MAX_ATTEMPTS", "99")
    assert _visual_grid_repair_budget({}) == (0, 2)
    assert _visual_grid_repair_budget({"visual_grid_repair_count": 2}) == (2, 2)


def test_exhausted_api_grid_budget_hands_off_once_to_browser(monkeypatch):
    monkeypatch.setenv("HERMES_VISUAL_GRID_CURRENT_REPAIR_MAX_ATTEMPTS", "2")
    stage_input = {
        "execution_backend": "api",
        "visual_grid_repair_count": 2,
    }

    assert _visual_grid_repair_budget_exhausted(stage_input) is True
    assert _visual_grid_failure_needs_browser_fallback(stage_input) is True

    stage_input["visual_grid_browser_fallback_attempted"] = True
    assert _visual_grid_failure_needs_browser_fallback(stage_input) is False


def test_exhausted_browser_grid_budget_never_reopens_api_cycle(monkeypatch):
    monkeypatch.setenv("HERMES_VISUAL_GRID_CURRENT_REPAIR_MAX_ATTEMPTS", "2")
    stage_input = {
        "execution_backend": "browser",
        "visual_grid_repair_count": 2,
        "visual_api_force_browser_fallback": True,
    }

    assert _visual_grid_repair_budget_exhausted(stage_input) is True
    assert _visual_grid_failure_needs_browser_fallback(stage_input) is False


def test_text_api_browser_handoff_is_durable_and_provider_agnostic():
    stage_input = {
        "execution_backend": "api",
        "api_route": "toapis:text",
    }

    fallback = _api_browser_fallback_input(stage_input, stage="CREATIVE")

    assert fallback["execution_backend"] == "browser"
    assert fallback["api_route"] is None
    assert fallback["api_primary_route"] == "toapis:text"
    assert fallback["api_fallback_to_browser"] is True
    assert fallback["api_force_browser_fallback"] is True
    assert fallback["api_browser_fallback_attempted"] is True
    assert fallback["force_fresh_response"] is True
    assert "visual_api_force_browser_fallback" not in fallback


def test_visual_api_browser_handoff_keeps_legacy_visual_markers():
    fallback = _api_browser_fallback_input(
        {"execution_backend": "api", "api_route": "bandianwa:gpt-image-2"},
        stage="VISUAL_PREVIEW",
    )

    assert fallback["api_fallback_to_browser"] is True
    assert fallback["visual_api_force_browser_fallback"] is True
    assert fallback["visual_grid_browser_fallback_attempted"] is True


def test_three_panel_grid_repair_uses_new_landscape_geometry():
    instruction = _visual_grid_repair_instruction(
        None,
        SimpleNamespace(),
        expected_panel_count=3,
        repair_attempt=1,
    )

    assert "one row of exactly three equal vertical 9:16 panels" in instruction
    assert "repair 1 of 2" in instruction
    assert "one camera exposure at one instant" in instruction
    assert "never render internal gutters" in instruction
    assert "two portrait panels on top" not in instruction


def test_rejected_visual_is_quarantined_outside_project_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(content_factory_tasks, "CONTENT_FACTORY_STORAGE_ROOT", tmp_path)
    source = tmp_path / "provider-output.png"
    source.write_bytes(b"provider evidence")
    project = SimpleNamespace(workspace_id=3, project_key="cf_test")

    target = _quarantine_generated_visual_capture(
        project,
        stage="VISUAL_PREVIEW",
        source=source,
        reason="grid invalid",
    )

    assert target is not None
    assert target.is_file()
    assert not source.exists()
    assert "generated/diagnostics/visual_capture" in target.as_posix()
    assert target.with_suffix(target.suffix + ".json").is_file()


def test_upstream_product_flag_cannot_reenable_a_standalone_packshot():
    packet = _packet()
    item = packet["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_plan"][0]
    item.update({
        "description": "Standalone white-background MYUPONA product hero packshot.",
        "requires_product_reference": True,
    })

    prompt, spec = build_visual_api_prompt(packet)

    assert spec["plan"][0]["requires_product_reference"] is False
    assert visual_generation_reference_paths(packet) == ["/tmp/character.png"]
    assert "product.png [product_visual]" not in prompt
    assert "Do not create a standalone product" in prompt


def test_creative_normalization_keeps_structured_shots_and_detailed_reference_panels():
    result = {
        "shot_plan": [
            {
                "segment": 1,
                "visual": "Creator looks through a telescope on the balcony.",
                "camera": "Vertical medium-wide shot.",
                "reference_panel": "R1",
            }
        ],
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [
                {"index": 1, "segment": 1, "description": "Reference panel 1"}
            ],
            "reference_panels": [
                {
                    "panel_id": "R1",
                    "name": "Moonlit balcony master",
                    "purpose": "Lock creator, telescope and balcony geography.",
                    "composition": "One complete vertical scene.",
                }
            ],
        },
    }
    normalized = _ensure_reference_plan(result)
    description = normalized["visual_job_ticket"]["reference_plan"][0]["description"]
    assert "Moonlit balcony master" in description
    assert "Reference panel 1" not in description


def test_creative_normalization_prefers_concrete_visual_state_over_abstract_reference_purpose():
    result = {
        "shot_plan": [{
            "segment": 1,
            "purpose": "Reveal the adult relationship ritual already being lost.",
            "visual_state": (
                "Daniel stands in the open office doorway while Miles, holding his keys, "
                "walks alone toward the apartment front door."
            ),
            "camera": "Fast backward tracking.",
            "spoken_copy": "Now he takes the walk alone.",
        }],
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [{
                "index": 1,
                "segment": 1,
                "description": "Reveal the adult relationship ritual already being lost.",
                "roles": ["character_anchor", "scene_anchor"],
            }],
        },
    }

    normalized = _ensure_reference_plan(result)
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert "Daniel stands in the open office doorway" in item["description"]
    assert "Miles, holding his keys" in item["description"]
    assert "Reveal the adult relationship ritual" not in item["description"]
    assert "Fast backward tracking" not in item["description"]
    assert "Now he takes the walk alone" not in item["description"]


def test_creative_normalization_preserves_concrete_product_still_over_motion_timeline():
    still_state = (
        "Dana sits alone under the charcoal blanket; the exact sealed, closed "
        "MYUPONA Sleep Ease Gummies bottle stands upright on the coffee table "
        "beside a remote, face-down phone, folded welcome sign, and unused tickets."
    )
    result = {
        "complete_video_script": {
            "segments": [{
                "segment_index": 4,
                "visual_action": (
                    "Dana steadies the sealed MYUPONA bottle, leaves it upright, "
                    "then settles under the blanket while the television changes "
                    "to a calm title screen."
                ),
            }],
        },
        "visual_job_ticket": {
            "reference_image_count": 1,
            "reference_plan": [{
                "index": 1,
                "segment": 4,
                "description": still_state,
                "roles": ["character_anchor", "scene_anchor", "action_anchor"],
            }],
        },
    }

    normalized = _ensure_reference_plan(result, product_allowed=True)
    item = normalized["visual_job_ticket"]["reference_plan"][0]

    assert "sits alone under the charcoal blanket" in item["description"]
    assert "stands upright on the coffee table" in item["description"]
    assert "then settles" not in item["description"]
    assert item["requires_product_reference"] is True


def test_visual_packet_compaction_preserves_complete_concrete_shot_state():
    visual_state = (
        "Daniel and Miles share quiet eye contact at the partially open bedroom doorway; "
        "the exact sealed MYUPONA Sleep Ease Gummies bottle sits on the hallway console "
        "beside the closed laptop and face-down phone."
    )
    previous = {
        "MEDIA_DESIGN": {
            "shot_plan": [{
                "segment": 4,
                "purpose": "Resolution and direct-response offer.",
                "visual_state": visual_state,
                "camera": "Stable doorway two-shot.",
                "spoken_copy": "They are $7.99.",
            }],
            "visual_job_ticket": {
                "reference_image_count": 1,
                "reference_plan": [{
                    "index": 1,
                    "segment": 4,
                    "description": "Resolution and direct-response offer.",
                }],
            },
        },
    }

    compact = content_factory_tasks._compact_visual_previous_outputs(previous)
    creative = compact["MEDIA_DESIGN"]

    assert creative["shot_plan"][0]["visual_state"] == visual_state
    assert "visual_state" in creative["shot_plan"][0]
    assert "spoken_copy" not in creative["shot_plan"][0]
    assert "quiet eye contact" in creative["visual_job_ticket"]["reference_plan"][0]["description"]


def test_signed_visual_contract_strips_local_composite_and_keeps_provider_authority():
    signed_design = {
        "shot_plan": [
            {
                "segment": index,
                "visual_state": f"Adult story state {index} in the same apartment.",
            }
            for index in range(1, 5)
        ],
        "visual_job_ticket": {
            "source": "directed_production_plan",
            "plan_id": "signed-plan-v41",
            "plan_sha256": "a" * 64,
            "reference_image_count": 4,
            "final_reference_count": 4,
            "reference_plan": [
                {
                    "index": index,
                    "reference_id": f"scene-{index}",
                    "segment": index,
                    "segments": [index],
                    "description": f"Adult story state {index} in the same apartment.",
                    "roles": ["character_anchor", "scene_anchor", "action_anchor"],
                    "source_asset_refs": [],
                    "generation_mode": "generate",
                    "requires_product_reference": False,
                }
                for index in range(1, 4)
            ] + [{
                "index": 4,
                "reference_id": "product-anchor",
                "segment": 4,
                "segments": [4],
                "description": "Exact uploaded MYUPONA package authority.",
                "roles": ["action_anchor"],
                "source_asset_refs": ["asset:2754"],
                "generation_mode": "generate",
                "requires_product_reference": True,
                "authoritative_product_composite": {
                    "placement": "lower_center",
                    "width_fraction": 0.28,
                    "entrance": "fade",
                },
            }],
        },
    }
    packet = _packet()
    packet["render_reference_images_individually"] = True
    packet["previous_outputs"] = {"MEDIA_DESIGN": signed_design}

    compact = _compact_packet_for_chatgpt(packet, "VISUAL_PREVIEW")
    ticket = compact["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]
    plan = ticket["reference_plan"]

    assert ticket["source"] == "directed_production_plan"
    assert len(plan) == 4
    assert plan[3]["generation_mode"] == "generate"
    assert plan[3]["source_asset_refs"] == ["asset:2754"]
    assert "authoritative_product_composite" not in plan[3]
    assert plan[3]["requires_product_reference"] is True
    specs = visual_board_specs(compact)
    assert len(specs) == 4
    assert [spec["global_start_index"] for spec in specs] == [1, 2, 3, 4]
    assert all(
        row["generation_mode"] == "generate"
        for spec in specs
        for row in spec["plan"]
    )
    assert "authoritative_product_composite" not in specs[3]["plan"][0]
    assert specs[3]["plan"][0]["requires_product_reference"] is True
    review_packet = _compact_packet_for_chatgpt(
        packet,
        "CREATIVE_REVIEW",
    )
    review_context = minimal_stage_context(
        review_packet,
        "CREATIVE_REVIEW",
    )
    review_plan = review_context["reference_plan"]
    assert len(review_plan) == 4
    assert review_plan[3]["reference_id"] == "product-anchor"
    assert review_plan[3]["generation_mode"] == "generate"
    assert review_plan[3]["source_asset_refs"] == ["asset:2754"]


def test_signed_visual_contract_rejects_reference_count_drift():
    signed_design = {
        "visual_job_ticket": {
            "source": "directed_production_plan",
            "reference_image_count": 4,
            "final_reference_count": 4,
            "reference_plan": [{
                "index": 1,
                "reference_id": "only-row",
                "segment": 1,
                "description": "One signed row.",
                "roles": ["action_anchor"],
                "source_asset_refs": [],
                "generation_mode": "generate",
                "requires_product_reference": False,
            }],
        },
    }

    with pytest.raises(ValueError, match="PRODUCTION_PLAN_REFERENCE_COUNT_MISMATCH"):
        content_factory_tasks._reference_contract_for_media_execution(
            signed_design,
        )


def test_visual_prompt_compiler_recovers_segment_local_visual_state_from_persisted_creative():
    packet = _packet()
    packet["render_reference_images_individually"] = True
    creative = packet["previous_outputs"]["MEDIA_DESIGN"]
    creative["shot_plan"] = [
        {
            "segment": 1,
            "visual_state": "Daniel stands in the office doorway while Miles carries keys toward the front door.",
            "camera": "Camera detail must not enter the still prompt.",
            "spoken_copy": "Dialogue detail must not enter the still prompt.",
        },
        {
            "segment": 2,
            "visual_state": "Miles rests one hand on the closed bedroom doorknob while facing Daniel.",
        },
        {
            "segment": 3,
            "visual_state": "Daniel stands beside a closed laptop and face-down phone under one faint wall lamp.",
        },
        {
            "segment": 4,
            "visual_state": (
                "Daniel and Miles share quiet eye contact at the partially open bedroom doorway; "
                "the exact sealed MYUPONA bottle sits on the hallway console."
            ),
        },
    ]
    creative["visual_job_ticket"]["reference_image_count"] = 4
    creative["visual_job_ticket"]["final_reference_count"] = 4
    creative["visual_job_ticket"]["reference_plan"] = [
        {
            "index": index,
            "segment": index,
            "description": label,
            "roles": ["character_anchor", "scene_anchor", "action_anchor"],
        }
        for index, label in enumerate(
            (
                "Reveal the relationship ritual already being lost.",
                "Knife-twist the painful admission.",
                "Choose a repeatable clean transition.",
                "Resolution and direct-response offer with the product.",
            ),
            1,
        )
    ]

    prompts = [prompt for prompt, _spec in build_visual_api_prompts(packet)]

    assert len(prompts) == 4
    assert "office doorway while Miles carries keys" in prompts[0]
    assert "closed bedroom doorknob while facing Daniel" in prompts[1]
    assert "face-down phone under one faint wall lamp" in prompts[2]
    assert "quiet eye contact at the partially open bedroom doorway" in prompts[3]
    assert "closed bedroom doorknob" not in prompts[0]
    assert "office doorway while Miles carries keys" not in prompts[1]
    assert all("Camera detail must not enter" not in prompt for prompt in prompts)
    assert all("Dialogue detail must not enter" not in prompt for prompt in prompts)


def test_bandianwa_image_transport_timeout_has_a_retryable_error_message(monkeypatch):
    class TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("", request=httpx.Request("POST", "https://example.test/v1/images"))

    monkeypatch.setattr(
        bandianwa_client_module.httpx,
        "AsyncClient",
        lambda **_kwargs: TimeoutClient(),
    )

    client = BandianwaImageClient(api_key="test-key", base_url="https://example.test", timeout=1)
    with pytest.raises(BandianwaApiError, match="Bandianwa image transport error.*ReadTimeout"):
        asyncio.run(client.create_image_task(prompt="test", size="1024x1024"))


def test_bandianwa_image_request_omits_optional_empty_images_field(monkeypatch):
    captured = {}

    async def fake_request(_method, _path, **kwargs):
        captured.update(kwargs.get("json") or {})
        return {"task_id": "image-task"}

    client = BandianwaImageClient(api_key="test-key", base_url="https://example.test")
    monkeypatch.setattr(client, "_json_request", fake_request)

    asyncio.run(client.create_image_task(prompt="illustrated adult living room", size="1024x1792", images=[]))

    assert "images" not in captured


def test_short_overlay_preserves_decimal_price_and_complete_story_quote():
    assert _short_overlay_copy(
        "Current product price: $7.99. Find it in the yellow cart.",
        fallback="Shop now",
        language="en-US",
    ) == "Current product price: $7.99"
    assert _short_overlay_copy(
        'When he found it, Ellis said, "I stopped saving you the first bite." Mara heard the distance.',
        fallback="Story turn",
        language="en-US",
    ) == "I stopped saving you the first bite"
    assert _short_overlay_copy(
        "When the book tower crashed during Nora’s reading circle, the room went quiet.",
        fallback="Story hook",
        language="en-US",
    ) == "When the book tower crashed during Nora’s reading circle"
    assert _short_overlay_copy(
        "It wasn’t fatigue—it was becoming the distracted bookseller people avoided.",
        fallback="Recognition",
        language="en-US",
    ) == "It wasn’t fatigue"
    assert _short_overlay_copy(
        "By closing, Mara's regular asked her colleague to finish the alterations.",
        fallback="Loss hook",
        language="en-US",
    ) == "Mara's regular asked her colleague to finish the alterations"
    assert _short_overlay_copy(
        "When a basket of clean shop linens toppled, Mara froze. "
        "Her colleague said, “I’ll take the fitting—you look spent.”",
        fallback="Knife twist",
        language="en-US",
    ) == "I’ll take the fitting—you look spent"
    assert _short_overlay_copy(
        "That landed harder than fatigue: her reputation was becoming an apology.",
        fallback="Recognition",
        language="en-US",
    ) == "That landed harder than fatigue"


def test_per_video_editor_guide_keeps_showrunner_declared_price():
    project = HermesContentFactoryProject(
        id=168,
        workspace_id=3,
        user_id=1,
        project_key="cf_test_editor_guide",
        title="Strong-pain animation",
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        product_brief="Current product price is $7.99. Use the yellow-cart CTA.",
        config_json={
            "video_language": "en-US",
            "video_resolution": "720p",
            "video_duration_min_seconds": 40,
            "video_duration_max_seconds": 40,
            "confirmed_promotions": "$7.99",
            "promotion_cta": "Current product price: $7.99. Find it in the yellow cart.",
            "allow_promotional_cta": True,
            "user_confirmed_marketing": True,
        },
        state_json={"creative_video_duration_seconds": 40},
    )
    asset = HermesContentFactoryAsset(
        id=3026,
        project_id=168,
        workspace_id=3,
        user_id=1,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name="variant-21.mp4",
        file_path="/tmp/variant-21.mp4",
        mime_type="video/mp4",
        size_bytes=4096,
        meta_json={
            "content_factory_video_index": 21,
            "version_name": "V21",
            "story_arc": "Mara realizes trust is thinning",
            "duration": 40,
            "segment_plan": [
                {
                    "segment_index": 1,
                    "duration": 10,
                    "segment_goal": "Mara hides in the kitchen",
                    "dialogue_lines": [{"line": "By midnight, Mara hid in the kitchen."}],
                    "compile_source": "signed_production_plan",
                },
                {
                    "segment_index": 2,
                    "duration": 10,
                    "segment_goal": "Ellis names the distance",
                    "dialogue_lines": [{
                        "line": 'When he found it, Ellis said, "I stopped saving you the first bite."'
                    }],
                    "compile_source": "signed_production_plan",
                },
                {
                    "segment_index": 3,
                    "duration": 10,
                    "segment_goal": "Trust is thinning",
                    "dialogue_lines": [{"line": "This was not another tired morning; it was trust thinning."}],
                    "compile_source": "signed_production_plan",
                },
                {
                    "segment_index": 4,
                    "duration": 10,
                    "segment_goal": "The night routine",
                    "dialogue_lines": [{"line": "MYUPONA fits the wind-down routine. Find it in the yellow cart."}],
                    "display_lines": [{"line": "Current product price: $7.99."}],
                    "compile_source": "signed_production_plan",
                },
            ],
            "source_task_ids": [2508, 2509, 2510, 2511],
        },
    )

    content, metadata = _per_video_editor_guidance_markdown(project, asset)

    assert '10-20s: "I stopped saving you the first bite"' in content
    assert '30-40s: "Current product price: $7.99"' in content
    assert "Current product price: $7\"" not in content
    assert len(metadata["hashtags"]) <= 5


def test_signed_noncommercial_editor_guide_does_not_invent_configured_cta():
    project = HermesContentFactoryProject(
        id=169,
        workspace_id=3,
        user_id=1,
        project_key="cf_test_editor_no_invented_cta",
        title="Quiet visual study",
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_language": "en-US",
            "video_duration_min_seconds": 20,
            "video_duration_max_seconds": 20,
            "confirmed_promotions": "$7.99",
            "promotion_cta": "Find it in the yellow cart.",
            "allow_promotional_cta": True,
            "user_confirmed_marketing": True,
        },
    )
    asset = HermesContentFactoryAsset(
        id=3027,
        project_id=169,
        workspace_id=3,
        user_id=1,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name="quiet-study.mp4",
        file_path="/tmp/quiet-study.mp4",
        mime_type="video/mp4",
        size_bytes=4096,
        meta_json={
            "content_factory_video_index": 1,
            "version_name": "Quiet Window",
            "story_arc": "A silent change in evening light",
            "duration": 20,
            "segment_plan": [
                {
                    "segment_index": 1,
                    "duration": 10,
                    "segment_goal": "Daylight leaves the room",
                    "compile_source": "signed_production_plan",
                },
                {
                    "segment_index": 2,
                    "duration": 10,
                    "segment_goal": "The lamp settles into stillness",
                    "compile_source": "signed_production_plan",
                },
            ],
        },
    )

    content, metadata = _per_video_editor_guidance_markdown(project, asset)

    assert "$7.99" not in content
    assert "yellow cart" not in content.lower()
    assert metadata["promotion"] == ""


def test_editor_overlay_keeps_am_abbreviation_as_one_hook():
    assert _short_overlay_copy(
        "3:07 a.m. again?",
        fallback="Night Hook",
        language="en-US",
    ) == "3:07 a.m. again"


def test_editor_publish_title_preserves_lowercase_am_abbreviation():
    project = HermesContentFactoryProject(
        id=171,
        workspace_id=3,
        user_id=1,
        project_key="cf_test_editor_am_title",
        title="Night hook",
        product_name="MYUPONA",
        config_json={"video_language": "en-US"},
    )
    asset = HermesContentFactoryAsset(
        id=3029,
        project_id=171,
        workspace_id=3,
        user_id=1,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name="hook.mp4",
        file_path="/tmp/hook.mp4",
        mime_type="video/mp4",
        meta_json={
            "content_factory_video_index": 1,
            "version_name": "v01",
            "duration": 10,
            "segment_plan": [{
                "segment_index": 1,
                "duration": 10,
                "compile_source": "signed_production_plan",
                "dialogue_lines": [{"line": "3:07 a.m. again?"}],
            }],
        },
    )

    content, metadata = _per_video_editor_guidance_markdown(project, asset)

    assert metadata["publish_title"] == "3:07 a.m. Again"
    assert "A.m." not in content


def test_signed_display_only_editor_guide_uses_viewer_copy_as_public_title():
    project = HermesContentFactoryProject(
        id=170,
        workspace_id=3,
        user_id=1,
        project_key="cf_test_editor_display_title",
        title="Display story",
        product_name="MYUPONA",
        config_json={"video_language": "en-US"},
    )
    asset = HermesContentFactoryAsset(
        id=3028,
        project_id=170,
        workspace_id=3,
        user_id=1,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name="v42.mp4",
        file_path="/tmp/v42.mp4",
        mime_type="video/mp4",
        size_bytes=4096,
        meta_json={
            "content_factory_video_index": 42,
            "version_name": "v42",
            "story_arc": "Make a work-to-home role change visible through removing items.",
            "duration": 40,
            "segment_plan": [{
                "segment_index": 1,
                "duration": 10,
                "segment_goal": "Establish post-shift time",
                "display_lines": [{
                    "line": "Shift ended an hour ago. Your badge is still on the counter."
                }],
                "compile_source": "signed_production_plan",
            }],
        },
    )

    content, metadata = _per_video_editor_guidance_markdown(project, asset)

    assert content.startswith("Title: Shift Ended An Hour Ago\n")
    assert metadata["publish_title"] == "Shift Ended An Hour Ago"


def test_editor_guide_publish_is_atomic_and_skips_unchanged_content(tmp_path):
    target = tmp_path / "guide.md"

    assert _atomic_write_text_if_changed(target, "first\n") is True
    first_stat = target.stat()
    assert _atomic_write_text_if_changed(target, "first\n") is False
    assert target.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert _atomic_write_text_if_changed(target, "second\n") is True
    assert target.read_text(encoding="utf-8") == "second\n"
    assert list(tmp_path.glob(".*.tmp")) == []
