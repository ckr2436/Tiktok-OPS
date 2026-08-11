from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.ai_video.local_storage import get_task_local_meta

from app.tasks.hermes_agent.content_factory_tasks import (
    _enqueue_queued_local_video_tasks,
    _authoritative_editor_guidance_assets_by_index,
    _authoritative_completed_variant_indices,
    _apply_segment_execution_retry_prompt,
    _compact_content_factory_retry_prompt,
    _apply_ai_segment_execution_replan,
    _apply_ai_segment_reference_strategy,
    _prepare_segment_execution_retry_prompt,
    _rebuild_retry_prompt_from_segment_execution_contract,
    _configured_api_video_variant_parallelism,
    _content_factory_retryable_video_failure,
    _content_factory_transient_retry_wait_seconds,
    _content_factory_local_transport_rebuild_failure,
    _compatible_chained_reference_route,
    _inflight_api_video_variant_indices,
    _terminal_failed_video_variant_indices,
    _retire_terminal_failed_video_groups_from_active_ledger,
    _lock_project_for_video_state_merge,
    _normalize_omni_retry_references,
    _normalize_segment_release_failure_code,
    _omni_reference_prompt,
    _project_forbids_local_display_overlays,
    _queue_next_variant_after_video_submit,
    _recover_orphaned_bridged_video_assets,
    _reconcile_segment_execution_qa_after_policy_change,
    _release_ready_segment_dependencies,
    _retry_failed_video_segments,
    _restore_inherited_final_intent_repair,
    _retry_product_visual_evidence,
    _segment_release_quality_gate,
    _schedule_video_wait,
    _segment_execution_contact_sheet,
    _segment_requirement_contract_for_review,
    _scope_final_intent_repair_tasks,
    _supersede_untracked_local_content_video_tasks,
    _should_retry_exhausted_content_video_after_cooldown,
    _set_serial_variant_progress_message,
    _successful_content_factory_task_missing_continuity,
    _validate_and_stamp_provider_prompt,
)


def test_retry_recovers_product_requirement_from_full_ai_timeline():
    payload = {
        "content_factory_product_required": False,
        "content_factory_product_anchor_required": False,
        "prompt": "Beats: 0-3s: clock reads 1 | 6-9s: Hold bottle",
        "content_factory_execution_feasibility_replan": {
            "timeline": [
                {
                    "start_second": 0,
                    "end_second": 3,
                    "action": "No product is visible.",
                },
                {
                    "start_second": 6,
                    "end_second": 9,
                    "action": (
                        "Hold the MYUPONA bottle in the warm setting and "
                        "show exactly two gummies beside it."
                    ),
                },
            ],
        },
    }

    assert "bottle" in str(_retry_product_visual_evidence(payload)).lower()


def test_retry_does_not_infer_product_visual_from_dialogue_or_repair():
    payload = {
        "content_factory_product_required": False,
        "content_factory_product_anchor_required": False,
        "content_factory_base_prompt": "\n".join([
            "Refs: @image1=character+scene; @image2=package",
            'Repair: exact package label "melatonin-free".',
            "Direction: 快速直切；全程无产品的2.5D睡前动画。",
            "Beats: 0-3s: 同一无产品卧室；她比较三张空白卡片。",
            (
                "Dialogue: 'Tired, staring at bedtime gummy labels? "
                "Start with three simple checks.'"
            ),
            "Product: @image2 sole package authority.",
        ]),
    }

    assert _retry_product_visual_evidence(payload) is None


def test_initial_segment_product_evidence_reads_multimodal_timeline():
    evidence = content_factory_tasks_module._segment_product_visual_evidence({
        "segment_goal": "Complete an ordinary after-work routine.",
        "timeline": [
            {"action": "She puts away her gloves at the end of the shift."},
            {
                "action": (
                    "Together they present MYUPONA, then naturally hold the "
                    "approved Soothing Body Balm jar."
                ),
            },
        ],
    })

    assert evidence is not None
    assert "jar" in evidence.lower()


def test_seedance_retry_restores_authoritative_product_reference(
    monkeypatch,
    tmp_path,
):
    product_path = tmp_path / "authoritative-product.png"
    product_path.write_bytes(b"product")
    product_ref = {
        "index": 99,
        "path": str(product_path),
        "filename": "authoritative-product.png",
        "content_type": "image/png",
        "size_bytes": product_path.stat().st_size,
        "asset_id": 901,
        "semantic_roles": ["product_anchor"],
        "is_product_anchor": True,
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "object_session",
        lambda _project: object(),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_reference_images_for_project",
        lambda *_args, **_kwargs: [object()],
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_copy_video_refs",
        lambda *_args, **_kwargs: [product_ref],
    )
    project = SimpleNamespace(
        id=184,
        workspace_id=1,
        product_id=7,
        product_name="MYUPONA",
        config_json={
            "product_required": True,
            "video_generation_mode": "image_to_video",
        },
        state_json={},
    )
    scene_ref = {
        "path": str(tmp_path / "scene.png"),
        "filename": "scene.png",
        "asset_id": 101,
        "semantic_roles": ["character_anchor", "scene_anchor"],
        "is_product_anchor": False,
    }
    source_file = SimpleNamespace(
        workspace_id=1,
        file_url=scene_ref["path"],
        kind="reference_upload",
        mime_type="image/png",
        size_bytes=10,
        meta_json={"asset_id": 101},
    )
    payload = {
        "model": "seedance_2_0_mini",
        "content_factory_video_generation_mode": "image_to_video",
        "content_factory_product_required": False,
        "content_factory_product_anchor_required": False,
        "content_factory_video_index": 1,
        "reference_file_paths": [scene_ref],
        "content_factory_execution_feasibility_replan": {
            "timeline": [{
                "start_second": 6,
                "end_second": 9,
                "action": "Reveal the bottle and exactly two gummies.",
            }],
        },
    }

    repaired, repaired_files = _normalize_omni_retry_references(
        payload,
        [source_file],
        set(),
        retry_attempt=1,
        project=project,
    )

    assert repaired["content_factory_product_required"] is True
    assert repaired["content_factory_product_anchor_required"] is True
    assert [
        row["is_product_anchor"]
        for row in repaired["content_factory_reference_manifest"]
    ] == [False, True]
    assert repaired["content_factory_reference_manifest"][1]["alias"] == "@image2"
    assert len(repaired_files) == 2
    assert repaired_files[1].file_url == str(product_path)


def test_seedance_policy_retry_restores_generated_character_anchor_before_product(
    monkeypatch,
    tmp_path,
):
    character_path = tmp_path / "generated-character.png"
    product_path = tmp_path / "authoritative-product.png"
    character_path.write_bytes(b"character")
    product_path.write_bytes(b"product")
    character_ref = {
        "index": 1,
        "path": str(character_path),
        "filename": "generated-character.png",
        "content_type": "image/png",
        "size_bytes": character_path.stat().st_size,
        "asset_id": 801,
        "semantic_roles": ["character_anchor", "scene_anchor"],
        "is_product_anchor": False,
        "reference_segment": 0,
    }
    product_ref = {
        "index": 2,
        "path": str(product_path),
        "filename": "authoritative-product.png",
        "content_type": "image/png",
        "size_bytes": product_path.stat().st_size,
        "asset_id": 901,
        "semantic_roles": ["product_anchor"],
        "is_product_anchor": True,
        "reference_segment": 0,
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "object_session",
        lambda _project: object(),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_reference_images_for_project",
        lambda *_args, **_kwargs: [object(), object()],
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_copy_video_refs",
        lambda *_args, **_kwargs: [character_ref, product_ref],
    )
    project = SimpleNamespace(
        id=185,
        workspace_id=3,
        product_id=8,
        product_name="MYUPONA",
        config_json={
            "product_required": True,
            "video_generation_mode": "image_to_video",
        },
        state_json={},
    )
    existing_product_file = SimpleNamespace(
        workspace_id=3,
        file_url=str(product_path),
        kind="reference_upload",
        mime_type="image/png",
        size_bytes=product_path.stat().st_size,
        meta_json={"asset_id": 901, "is_product_anchor": True},
    )
    payload = {
        "model": "seedance_2_0_mini",
        "content_factory_video_generation_mode": "image_to_video",
        "content_factory_product_required": True,
        "content_factory_product_anchor_required": True,
        "content_factory_video_index": 4,
        "reference_file_paths": [product_ref],
    }

    repaired, repaired_files = _normalize_omni_retry_references(
        payload,
        [existing_product_file],
        set(),
        retry_attempt=1,
        project=project,
    )

    assert [
        row["asset_id"]
        for row in repaired["content_factory_reference_manifest"]
    ] == [801, 901]
    assert repaired[
        "content_factory_retry_restored_character_anchor_asset_id"
    ] == 801
    assert [row.file_url for row in repaired_files] == [
        str(character_path),
        str(product_path),
    ]
    assert repaired_files[0].meta_json[
        "restored_for_cross_segment_character_identity"
    ] is True


def test_reference_plan_marks_real_generated_continuity_seed_as_shared_identity(
    monkeypatch,
):
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_successful_media_design_for_variant",
        lambda *_args, **_kwargs: {"result": "unused"},
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_reference_contract_for_media_execution",
        lambda *_args, **_kwargs: {
            "visual_job_ticket": {
                "reference_plan": [
                    {
                        "index": 1,
                        "segment": 1,
                        "description": "Opening Pilates action and studio layout.",
                        "roles": ["action_anchor", "scene_anchor"],
                    },
                    {
                        "index": 2,
                        "segment": 2,
                        "description": "Application action close-up.",
                        "roles": ["action_anchor"],
                    },
                ]
            }
        },
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_active_variant_index",
        lambda _project: 2,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_project_uses_product",
        lambda _project: True,
    )

    rows = content_factory_tasks_module._reference_plan_for_project(
        object(),
        SimpleNamespace(id=185),
        2,
    )

    assert rows[0]["roles"] == [
        "action_anchor",
        "scene_anchor",
        "character_anchor",
    ]
    assert rows[1]["roles"] == ["action_anchor"]


def test_seedance_retry_recovers_legacy_generated_continuity_seed(
    monkeypatch,
    tmp_path,
):
    continuity_path = tmp_path / "legacy-first-generated-reference.png"
    product_path = tmp_path / "authoritative-product.png"
    continuity_path.write_bytes(b"continuity")
    product_path.write_bytes(b"product")
    legacy_continuity_ref = {
        "index": 1,
        "path": str(continuity_path),
        "filename": continuity_path.name,
        "content_type": "image/png",
        "size_bytes": continuity_path.stat().st_size,
        "asset_id": 5811,
        "semantic_roles": ["action_anchor", "scene_anchor"],
        "is_product_anchor": False,
        "reference_segment": 1,
    }
    product_ref = {
        "index": 5,
        "path": str(product_path),
        "filename": product_path.name,
        "content_type": "image/png",
        "size_bytes": product_path.stat().st_size,
        "asset_id": 5564,
        "semantic_roles": ["product_anchor"],
        "is_product_anchor": True,
        "reference_segment": 0,
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "object_session",
        lambda _project: object(),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_reference_images_for_project",
        lambda *_args, **_kwargs: [object(), object()],
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_copy_video_refs",
        lambda *_args, **_kwargs: [legacy_continuity_ref, product_ref],
    )
    project = SimpleNamespace(
        id=185,
        workspace_id=3,
        product_id=8,
        product_name="MYUPONA",
        config_json={
            "product_required": True,
            "video_generation_mode": "image_to_video",
        },
        state_json={},
    )
    payload = {
        "model": "seedance_2_0_mini",
        "content_factory_video_generation_mode": "image_to_video",
        "content_factory_product_required": True,
        "content_factory_product_anchor_required": True,
        "content_factory_video_index": 2,
        "reference_file_paths": [product_ref],
    }
    product_file = SimpleNamespace(
        workspace_id=3,
        file_url=str(product_path),
        kind="reference_upload",
        mime_type="image/png",
        size_bytes=product_path.stat().st_size,
        meta_json={"asset_id": 5564, "is_product_anchor": True},
    )

    repaired, repaired_files = _normalize_omni_retry_references(
        payload,
        [product_file],
        set(),
        retry_attempt=1,
        project=project,
    )

    restored = repaired["content_factory_reference_manifest"][0]
    assert restored["asset_id"] == 5811
    assert restored["semantic_roles"] == [
        "action_anchor",
        "scene_anchor",
        "character_anchor",
    ]
    assert repaired[
        "content_factory_retry_restored_character_anchor_asset_id"
    ] == 5811
    assert [item.file_url for item in repaired_files] == [
        str(continuity_path),
        str(product_path),
    ]


def test_seedance_retry_promotes_existing_continuity_seed_without_duplication(
    monkeypatch,
    tmp_path,
):
    continuity_path = tmp_path / "existing-first-reference.png"
    product_path = tmp_path / "product.png"
    continuity_path.write_bytes(b"continuity")
    product_path.write_bytes(b"product")
    continuity_ref = {
        "index": 1,
        "path": str(continuity_path),
        "filename": continuity_path.name,
        "content_type": "image/png",
        "size_bytes": continuity_path.stat().st_size,
        "asset_id": 5811,
        "semantic_roles": ["action_anchor", "scene_anchor"],
        "is_product_anchor": False,
        "reference_segment": 1,
    }
    product_ref = {
        "index": 5,
        "path": str(product_path),
        "filename": product_path.name,
        "content_type": "image/png",
        "size_bytes": product_path.stat().st_size,
        "asset_id": 5564,
        "semantic_roles": ["product_anchor"],
        "is_product_anchor": True,
        "reference_segment": 0,
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "object_session",
        lambda _project: object(),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_reference_images_for_project",
        lambda *_args, **_kwargs: [object(), object()],
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_copy_video_refs",
        lambda *_args, **_kwargs: [continuity_ref, product_ref],
    )
    project = SimpleNamespace(
        id=185,
        workspace_id=3,
        product_id=8,
        product_name="MYUPONA",
        config_json={
            "product_required": True,
            "video_generation_mode": "image_to_video",
        },
        state_json={},
    )
    payload = {
        "model": "seedance_2_0_mini",
        "content_factory_video_generation_mode": "image_to_video",
        "content_factory_product_required": True,
        "content_factory_product_anchor_required": True,
        "content_factory_video_index": 2,
        "reference_file_paths": [continuity_ref, product_ref],
    }
    files = [
        SimpleNamespace(
            workspace_id=3,
            file_url=str(ref["path"]),
            kind="reference_upload",
            mime_type="image/png",
            size_bytes=int(ref["size_bytes"]),
            meta_json={"asset_id": int(ref["asset_id"])},
        )
        for ref in (continuity_ref, continuity_ref, product_ref)
    ]

    repaired, repaired_files = _normalize_omni_retry_references(
        payload,
        files,
        set(),
        retry_attempt=1,
        project=project,
    )

    assert [
        item["asset_id"]
        for item in repaired["content_factory_reference_manifest"]
    ] == [5811, 5564]
    assert "character_anchor" in repaired[
        "content_factory_reference_manifest"
    ][0]["semantic_roles"]
    assert [item.file_url for item in repaired_files] == [
        str(continuity_path),
        str(product_path),
    ]


def test_segment_execution_contact_sheet_contains_only_provider_pixels(
    tmp_path,
):
    source = tmp_path / "solid-green.mp4"
    target = tmp_path / "sheet.jpg"
    result = content_factory_tasks_module.subprocess.run(
        [
            content_factory_tasks_module.FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x22aa44:s=360x640:d=3:r=12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    _segment_execution_contact_sheet(source, target)

    sheet = content_factory_tasks_module.Image.open(target).convert("RGB")
    # The old inspector-generated timestamp badge made this raw-green corner
    # nearly black. JPEG tolerance still leaves a wide safety margin here.
    red, green, blue = sheet.getpixel((10, 10))
    assert green > 100
    assert green > red * 2
    assert green > blue * 2


def test_segment_requirement_review_defers_whole_video_positive_actions():
    project = SimpleNamespace(
        state_json={
            "ai_video_groups": [{
                "segments": [
                    {"task_id": 3248, "duration": 9},
                    {"task_id": 3249, "duration": 9},
                ],
            }],
        },
    )
    task = SimpleNamespace(id=3248)
    requirements = [
        {
            "requirement_id": "R-HOOK",
            "scope": "time_window",
            "start_seconds": 0,
            "end_seconds": 6,
        },
        {
            "requirement_id": "R-PROJECT",
            "scope": "project",
            "observable_checks": ["later product and CTA are present"],
        },
        {
            "requirement_id": "R-LATER",
            "scope": "time_window",
            "start_seconds": 12,
            "end_seconds": 18,
        },
    ]

    scoped, start_seconds, end_seconds = (
        _segment_requirement_contract_for_review(
            project,
            task,
            {"duration_seconds": 9},
            requirements,
        )
    )

    assert (start_seconds, end_seconds) == (0.0, 9.0)
    assert [item["requirement_id"] for item in scoped] == [
        "R-HOOK",
        "R-PROJECT",
    ]
    assert scoped[0]["segment_gate_mode"] == "positive_evidence"
    assert scoped[1]["segment_gate_mode"] == "constraint_only"


def test_segment_requirement_review_marks_cross_segment_window_advisory():
    project = SimpleNamespace(
        state_json={
            "ai_video_groups": [{
                "segments": [
                    {"task_id": 3289, "duration": 9},
                    {"task_id": 3290, "duration": 9},
                ],
            }],
        },
    )
    task = SimpleNamespace(id=3289)

    scoped, start_seconds, end_seconds = (
        _segment_requirement_contract_for_review(
            project,
            task,
            {"duration_seconds": 9},
            [{
                "requirement_id": "R-CONVERSION",
                "scope": "time_window",
                "start_seconds": 6,
                "end_seconds": 19,
                "observable_checks": [
                    "The whole-video product action resolves after the hook."
                ],
            }],
        )
    )

    assert (start_seconds, end_seconds) == (0.0, 9.0)
    assert len(scoped) == 1
    assert scoped[0]["segment_gate_mode"] == "partial_positive_evidence"
    assert scoped[0]["segment_observable_start_seconds"] == 6.0
    assert scoped[0]["segment_observable_end_seconds"] == 9.0
    assert scoped[0]["positive_evidence_deferred_before_segment"] is False
    assert scoped[0]["positive_evidence_deferred_after_segment"] is True


def test_ai_execution_replan_replaces_qa_contract_without_rewriting_copy():
    original = {
        "content_factory_base_prompt": (
            "SIGNED VISUAL EXECUTION REPAIR (authoritative): old repair\n"
            "Dialogue: host: 'Keep this exact line.'\n"
            "Beats: 0-10s: old dense choreography"
        ),
        "content_factory_dialogue_lines": [{
            "line_id": "l1",
            "speaker_id": "host",
            "line": "Keep this exact line.",
        }],
        "content_factory_segment_execution_contract": {
            "segment_index": 1,
            "duration_seconds": 10,
            "timeline": [],
        },
    }
    replanned = {
        "policy_version": "test-v1",
        "segment_index": 1,
        "duration_seconds": 10,
        "segment_goal": "same function",
        "timeline": [{
            "start_second": 0,
            "end_second": 10,
            "action": "one observable state change",
            "camera": "one stable move",
            "provider_action_en": "Show one complete visible state change.",
            "provider_action_zh": "展示一个完整、可见的状态变化。",
            "dialogue_key": "l1",
        }],
        "pacing": "fast then steady",
        "camera_direction": "one move",
        "provider_instruction": "execute the visible state change",
        "rationale": "provider feasibility",
    }

    repaired = _apply_ai_segment_execution_replan(original, replanned)

    assert "Keep this exact line." in repaired["prompt"]
    assert "Repair:" not in repaired["prompt"]
    assert "Obey Beats" not in repaired["prompt"]
    assert "old repair" not in repaired["prompt"]
    assert "old dense choreography" not in repaired["prompt"]
    assert "Beats: 0-10s: Show one complete visible state change." in repaired["prompt"]
    assert "Dialogue: 'Keep this exact line.'" in repaired["prompt"]
    assert "Dialogue: host:" not in repaired["prompt"]
    assert repaired["content_factory_segment_execution_contract"]["timeline"] == replanned["timeline"]
    assert "rationale" not in repaired["content_factory_segment_execution_contract"]
    assert repaired["content_factory_execution_replan_attempt"] == 1
    assert repaired["content_factory_execution_feasibility_replan"][
        "provider_instruction"
    ] == "execute the visible state change"
    assert repaired["content_factory_segment_execution_repair"][
        "source"
    ] == "ai_feasibility_replan"


def test_ai_execution_replan_drops_stale_provider_direction_and_requirement_prose():
    repaired = _apply_ai_segment_execution_replan(
        {
            "prompt": (
                "Direction: 7 slow shots; hold 3s; old wide -> old push-in.\n"
                "Signed intent requirements for this segment: R-001: preserve the hook.\n"
                "Voice lock for this segment: same male US narrator.\n"
                "Output: 9:16 720p; spoken language English (US) only."
            ),
            "content_factory_dialogue_lines": [{
                "line_id": "hook",
                "speaker_id": "narrator",
                "line": "The day ended, but my brain is still screaming at 10.",
            }],
            "content_factory_requirement_contract": [{
                "requirement_id": "R-001",
                "intent": "Preserve the hook.",
            }],
            "content_factory_segment_execution_contract": {
                "segment_index": 1,
                "visual_style": "Cold kinetic home-interior realism.",
            },
        },
        {
            "policy_version": "replan-v2",
            "segment_index": 1,
            "duration_seconds": 4,
            "timeline": [{
                "start_second": 0,
                "end_second": 4,
                "action": "Snap upright, react, then freeze.",
                "provider_action_en": "Snap upright; react; freeze the final pose.",
                "dialogue_key": "hook",
            }],
            "provider_direction_en": (
                "3 shots; hold 0.5-1.2s; off-center wide -> tight reaction "
                "-> frozen medium; cold home realism."
            ),
        },
    )

    prompt = repaired["prompt"]
    assert prompt.count("Direction:") == 1
    assert "7 slow shots" not in prompt
    assert "Signed intent requirements for this segment" not in prompt
    assert "3 shots; hold 0.5-1.2s" in prompt
    assert "same male US narrator" in prompt
    assert "Output: 9:16 720p" in prompt


def test_ai_execution_replan_preserves_final_qa_repair_in_doubao_payload():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Soothing Body Balm",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
        },
    )
    original = {
        "service_provider": "doubao",
        "model": "seedance_2_0_mini",
        "prompt": (
            "Refs: @image1=story; @image2=package\n"
            "Beats: 0-6s: old application action\n"
            "Dialogue: 'Made with MSM, it fits right into an easy "
            "post-workout body-care routine.'\n"
            "Voice: same female off-screen narrator; US accent.\n"
            "Product: uploaded package is sole authority.\n"
            "9:16; no captions/UI/watermark; segment this segment."
        ),
        "content_factory_product_anchor_required": True,
        "content_factory_reference_manifest": [
            {
                "alias": "@image1",
                "semantic_roles": ["character_anchor", "scene_anchor"],
            },
            {
                "alias": "@image2",
                "semantic_roles": ["product_anchor"],
                "is_product_anchor": True,
            },
        ],
        "content_factory_dialogue_lines": [{
            "speaker_id": "narrator",
            "line": (
                "Made with MSM, it fits right into an easy post-workout "
                "body-care routine."
            ),
        }],
        "content_factory_segment_execution_repair": {
            "policy_version": (
                content_factory_tasks_module.FINAL_INTENT_REVIEW_POLICY_VERSION
            ),
            "repair_instruction": (
                "Show only a visibly small amount on intact skin and do not "
                "add any spoken claim."
            ),
        },
    }
    replanned = {
        "policy_version": "feasibility-v1",
        "timeline": [{
            "start_second": 0,
            "end_second": 6,
            "provider_action_zh": (
                "创作者轻柔按摩完整外部皮肤；产品罐正面清晰可见。"
            ),
        }],
        "provider_instruction": "Use one feasible massage action.",
    }

    repaired = _apply_ai_segment_execution_replan(original, replanned)
    compacted = _compact_content_factory_retry_prompt(project, repaired)

    active_repair = repaired["content_factory_segment_execution_repair"]
    assert active_repair["source"] == "ai_feasibility_replan"
    assert "visibly small amount" in active_repair[
        "upstream_repair_instruction"
    ]
    assert "Repair:" in compacted["prompt"]
    assert "visibly small amount" in compacted["prompt"]
    assert "do not add any spoken claim" in compacted["prompt"]
    assert "Made with MSM" in compacted["prompt"]


def test_legacy_ai_replan_restores_final_qa_repair_from_same_segment_parent():
    project = SimpleNamespace(
        id=185,
        project_key="cf_test",
        workspace_id=3,
    )
    evidence_parent = SimpleNamespace(
        id=3444,
        workspace_id=3,
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_video_index": 2,
            "content_factory_segment_index": 3,
        },
        result_json={
            "__local": {
                "content_factory_project_key": "cf_test",
                "final_intent_qa_failure": {
                    "policy_version": (
                        content_factory_tasks_module
                        .FINAL_INTENT_REVIEW_POLICY_VERSION
                    ),
                    "repair_instruction": (
                        "Show only a visibly small amount of balm and do not "
                        "add any spoken claim."
                    ),
                },
            },
        },
    )
    transport_parent = SimpleNamespace(
        id=3446,
        workspace_id=3,
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_video_index": 2,
            "content_factory_segment_index": 3,
        },
        result_json={
            "__local": {
                "content_factory_project_key": "cf_test",
                "content_factory_retry_parent_task_id": 3444,
            },
        },
    )
    failed = SimpleNamespace(
        id=3447,
        workspace_id=3,
        input_json={
            "content_factory_project_key": "cf_test",
            "content_factory_video_index": 2,
            "content_factory_segment_index": 3,
        },
    )
    parents = {3444: evidence_parent, 3446: transport_parent}
    db = SimpleNamespace(get=lambda _model, task_id: parents.get(task_id))
    retry_input = {
        "content_factory_segment_execution_repair": {
            "source": "ai_feasibility_replan",
            "policy_version": "feasibility-v1",
            "repair_instruction": "Use one feasible massage action.",
        },
    }

    restored = _restore_inherited_final_intent_repair(
        db,
        project=project,
        failed_task=failed,
        failed_meta={"content_factory_retry_parent_task_id": 3446},
        retry_input=retry_input,
    )

    active = restored["content_factory_segment_execution_repair"]
    assert active["upstream_repair_parent_task_id"] == 3444
    assert "visibly small amount" in active["upstream_repair_instruction"]
    assert "do not add any spoken claim" in active[
        "upstream_repair_instruction"
    ]


def test_composed_final_intent_repair_is_attached_to_named_segment_task():
    task = SimpleNamespace(
        input_json={"content_factory_segment_index": 1},
        result_json={"__local": {}},
    )
    report = {
        "policy_version": (
            content_factory_tasks_module.FINAL_INTENT_REVIEW_POLICY_VERSION
        ),
        "repair_scope": "segment_regeneration",
        "affected_segment_indices": [1, 3],
        "blocking_requirement_ids": ["R-002", "R-003"],
        "blocking_reasons": ["The first two seconds lack a body-care cue."],
        "repair_instruction": (
            "In segment 1 show visible post-workout fatigue within the first "
            "two seconds and preserve the Pilates-ball conflict."
        ),
    }

    content_factory_tasks_module._attach_final_intent_repair_evidence(
        task,
        report,
    )

    packet = task.result_json["__local"]["final_intent_qa_failure"]
    assert packet["segment_index"] == 1
    assert packet["affected_segment_indices"] == [1, 3]
    assert packet["failed_requirement_ids"] == ["R-002", "R-003"]
    assert "first two seconds" in packet["repair_instruction"]


def test_ai_execution_replan_rebuilds_multi_speaker_dialogue_from_copy_ledger():
    original = {
        "prompt": (
            "Dialogue: long_internal_narrator_id...: 'Old compact copy.'\n"
            "Beats: 0-8s: old action"
        ),
        "content_factory_dialogue_lines": [
            {
                "speaker_id": "host",
                "line": "Did you see that?",
            },
            {
                "speaker_id": "friend",
                "line": "I did.",
            },
        ],
    }
    replanned = {
        "policy_version": "test-v2",
        "timeline": [{
            "start_second": 0,
            "end_second": 8,
            "provider_action_en": "Show the complete exchange.",
        }],
    }

    repaired = _apply_ai_segment_execution_replan(original, replanned)

    assert "long_internal_narrator_id..." not in repaired["prompt"]
    assert "Dialogue: host: 'Did you see that?' | friend: 'I did.'" in repaired[
        "prompt"
    ]


def test_ai_replan_uses_complete_chinese_provider_beats_for_doubao():
    original = {
        "service_provider": "doubao",
        "prompt": (
            "Refs: @image1=package\n"
            "Beats: 0-9s: old incomplete action\n"
            "Product: uploaded package is sole authority. No white packshot.\n"
            "Audio: no speech/lip-sync; 9:16; no text/UI/watermark."
        ),
    }
    replanned = {
        "policy_version": "test-provider-beats-v2",
        "timeline": [
            {
                "start_second": 0,
                "end_second": 3,
                "action": "Full audit action with a readable 1:43 time and 43-video count.",
                "camera": "Locked medium shot.",
                "provider_action_en": "Phone shows 1:43 and 43 videos; woman looks alarmed; no product.",
                "provider_action_zh": "手机清晰显示1:43和已刷43条；女人惊觉；产品不出现。",
            },
            {
                "start_second": 3,
                "end_second": 6,
                "action": "Full audit reset action.",
                "camera": "Locked side shot.",
                "provider_action_en": "Phone already face-down; hands withdraw; no product.",
                "provider_action_zh": "手机已扣在床头；双手离开；产品不出现。",
            },
            {
                "start_second": 6,
                "end_second": 9,
                "action": "Full audit product sequence with exactly two gummies and a final downward point.",
                "camera": "Three locked hard-cut states.",
                "provider_action_en": "Hard cuts: bottle; exactly two gummies; final downward point.",
                "provider_action_zh": "硬切三态：产品瓶；恰好两颗软糖；最后手指明确向下。",
            },
        ],
        "provider_instruction": "Full audit-only instruction.",
    }

    repaired = _apply_ai_segment_execution_replan(original, replanned)

    assert "手机清晰显示1:43和已刷43条" in repaired["prompt"]
    assert "恰好两颗软糖" in repaired["prompt"]
    assert "old incomplete action" not in repaired["prompt"]
    assert "Full audit action" not in repaired["prompt"]


def test_doubao_replan_compiles_complete_chinese_actions_without_duplicate_time():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    original = {
        "service_provider": "doubao",
        "model": "seedance_2_0_mini",
        "prompt": (
            "Refs: @image1=package\n"
            "Beats: 0-9s: old action\n"
            "Product: uploaded package is sole authority.\n"
            "Audio: no speech/lip-sync; 9:16; no text/UI/watermark."
        ),
        "content_factory_product_anchor_required": True,
        "content_factory_reference_manifest": [{
            "alias": "@image1",
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        }],
    }
    replanned = {
        "policy_version": "test-complete-cjk-v1",
        "timeline": [
            {
                "start_second": 0,
                "end_second": 2.5,
                "action": "Full audit action one.",
                "provider_action_en": "Woman sees 1:43 and 43 videos; no product.",
                "provider_action_zh": (
                    "0–2.5秒固定中景；女人坐着；手机停在胸前；"
                    "屏幕显示1:43和43条；不出现产品。"
                ),
            },
            {
                "start_second": 2.5,
                "end_second": 5.5,
                "action": "Full audit action two.",
                "provider_action_en": "Phone face-down; no product.",
                "provider_action_zh": (
                    "2.5–5.5秒硬切；手机屏幕朝下；女人转身；"
                    "双手离开手机；不出现产品。"
                ),
            },
            {
                "start_second": 5.5,
                "end_second": 9,
                "action": "Full audit action three.",
                "provider_action_en": "Bottle, two gummies, downward point.",
                "provider_action_zh": (
                    "5.5–9秒依次硬切；蓝色MYUPONA瓶正面朝镜头；"
                    "同一瓶旁恰好两颗紫色软糖；最后手指明确向下。"
                ),
            },
        ],
        "provider_instruction": "Use the three complete states.",
    }

    repaired = _apply_ai_segment_execution_replan(original, replanned)
    compiled = _compact_content_factory_retry_prompt(project, repaired)["prompt"]

    assert len(compiled) <= 495
    assert "0-2.5s: 固定中景" in compiled
    assert "0-2.5s: 0–2.5秒" not in compiled
    assert "不出现产品" in compiled
    assert "双手离开手机" in compiled
    assert "恰好两颗紫色软糖" in compiled
    assert "最后手指明确向下" in compiled


def test_doubao_replan_keeps_dynamic_hook_ahead_of_duplicated_scene_context():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Soothing Body Balm",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    original = {
        "service_provider": "doubao",
        "model": "seedance_2_0_mini",
        "prompt": (
            "Refs: @image1=package\n"
            "Beats: 0-7s: old action\n"
            "Dialogue: 'Desk day finally over? MYUPONA Soothing Body Balm "
            "is part of my wind-down routine—tap the product card.'\n"
            "Voice: same female visible protagonist; US accent.\n"
            "Product: uploaded package is sole authority.\n"
            "9:16; no captions/UI/watermark; segment this segment."
        ),
        "content_factory_product_anchor_required": True,
        "content_factory_dialogue_lines": [{
            "speaker_id": "adult_pov_voice",
            "line": (
                "Desk day finally over? MYUPONA Soothing Body Balm is part "
                "of my wind-down routine—tap the product card."
            ),
        }],
        "content_factory_reference_manifest": [{
            "alias": "@image1",
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        }],
    }
    context = (
        "竖屏第一视角家庭办公到夜间护理；成年人的手、书桌和白色"
        "MYUPONA罐盒，环境安静干净"
    )
    replanned = {
        "policy_version": "test-action-first-v1",
        "provider_visual_context_zh": context,
        "timeline": [
            {
                "start_second": 0,
                "end_second": 2.2,
                "provider_action_zh": (
                    context
                    + "；书桌突然上升；左手托住滑动键盘，右手立刻按停控制器。"
                ),
            },
            {
                "start_second": 2.2,
                "end_second": 4.6,
                "provider_action_zh": "硬切产品罐正面；手指轻点罐旁。",
            },
            {
                "start_second": 4.6,
                "end_second": 7,
                "provider_action_zh": "少量膏体点在颈肩并轻柔按摩；产品同框收尾。",
            },
        ],
    }

    repaired = _apply_ai_segment_execution_replan(original, replanned)
    compiled = _compact_content_factory_retry_prompt(project, repaired)["prompt"]

    assert len(compiled) <= 495
    first_beat = compiled.split(" | ", 1)[0]
    assert "书桌突然上升" in first_beat
    assert first_beat.index("书桌突然上升") < first_beat.find("场景：") \
        if "场景：" in first_beat else True


def test_segment_execution_retry_rebuilds_from_full_ai_replan_metadata():
    full_replan = {
        "policy_version": "test-replan-v1",
        "timeline": [
            {
                "start_second": 0,
                "end_second": 2,
                "action": (
                    "Product-free opening with exactly two gummies; "
                    "turn the phone face-down."
                ),
            },
            {
                "start_second": 2,
                "end_second": 5.5,
                "action": (
                    "Hold the MYUPONA package beside exactly two gummies."
                ),
            },
            {
                "start_second": 5.5,
                "end_second": 9,
                "action": (
                    "Phone already face-down, hands empty, one fingertip tap."
                ),
            },
        ],
    }
    prior = {
        "prompt": (
            "Repair: reviewer sentence that crowded the transport\n"
            "Beats: 0-2s: Product-free; two | 2-5.5s: product reveal | "
            "5.5-9s: holds prior\n"
            "Audio: no speech/lip-sync; 9:16 stylized animation."
        ),
        "content_factory_base_prompt": "Beats: old truncated transport",
        "content_factory_execution_feasibility_replan": full_replan,
    }

    rebuilt = _prepare_segment_execution_retry_prompt(
        prior,
        {
            "blocking": True,
            "repair_instruction": "The final tap was not visible.",
        },
    )

    assert "turn the phone face-down" in rebuilt["prompt"]
    assert "hands empty, one fingertip tap" in rebuilt["prompt"]
    assert "holds prior" not in rebuilt["prompt"]
    assert rebuilt["content_factory_latest_segment_execution_review"][
        "blocking"
    ] is True


def test_policy_migration_rebuilds_empty_rapid_beats_from_full_contract():
    prior = {
        "prompt": (
            "Refs: @image1,@image2=character+scene; @image3=package\n"
            "Must: 5-6.7s phone face-down\n"
            "Beats: 0-1.72s: ; FX: sharp | 1.72-2.5s: She freezes | "
            "2.5-4.21s: She looks | 4.21-5s: Her stopped | "
            "5-6.7s: She | 6.7-9s: She unplugs\n"
            "Product: uploaded package is sole authority. In-scene.\n"
            "Audio: no speech/lip-sync; 9:16; no text/UI/watermark."
        ),
        "content_factory_base_prompt": "legacy lossy transport",
        "content_factory_prompt_policy_version": "legacy-v31",
        "content_factory_voice_lock": [
            {
                "gender": "female",
                "screen_relation": "off_screen_narrator",
                "speech_rate": 175,
            }
        ],
        "content_factory_segment_execution_repair": {
            "policy_version": "legacy-segment-review-v6",
            "repair_instruction": (
                "Require a later-segment action that is not owned by this clip."
            ),
        },
        "content_factory_segment_execution_contract": {
            "timeline": [
                {
                    "start_second": 0,
                    "end_second": 1.72,
                    "subject_action": (
                        "She keeps scrolling despite a nearly empty red "
                        "battery shape."
                    ),
                    "motion_and_transition": "A sharp battery pulse pushes in.",
                },
                {
                    "start_second": 1.72,
                    "end_second": 2.5,
                    "subject_action": (
                        "She freezes mid-scroll and darts her eyes to the clock."
                    ),
                    "motion_and_transition": "Whip-pan to the clock glow.",
                },
                {
                    "start_second": 2.5,
                    "end_second": 4.21,
                    "subject_action": (
                        "She repeats upward swipes, then stops her thumb."
                    ),
                    "motion_and_transition": "Three jump cuts halt abruptly.",
                },
                {
                    "start_second": 4.21,
                    "end_second": 5,
                    "subject_action": "Her stopped hand hovers over the phone.",
                },
                {
                    "start_second": 5,
                    "end_second": 6.7,
                    "subject_action": (
                        "She turns the phone face-down and sits up."
                    ),
                },
                {
                    "start_second": 6.7,
                    "end_second": 9,
                    "subject_action": (
                        "She unplugs the cable and reveals the MYUPONA bottle."
                    ),
                },
            ]
        },
    }

    rebuilt = _rebuild_retry_prompt_from_segment_execution_contract(prior)

    assert "0-1.72s: She keeps scrolling" in rebuilt["prompt"]
    assert "5-6.7s: She turns the phone face-down" in rebuilt["prompt"]
    assert "6.7-9s: She unplugs the cable" in rebuilt["prompt"]
    assert "0-1.72s: ;" not in rebuilt["prompt"]
    assert "later-segment action" not in rebuilt["prompt"]
    assert "Voice: same female off-screen narrator, US accent; 175 words per minute." in rebuilt["prompt"]
    assert rebuilt["content_factory_prompt_migration"]["timeline_rows"] == 6
    assert "content_factory_provider_prompt_contract" not in rebuilt


def test_multimodal_reference_strategy_drops_conflicting_generated_packshot():
    refs = [
        {
            "path": "/tmp/product-scene.png",
            "asset_id": 11,
            "filename": "product-scene.png",
            "semantic_roles": ["action_anchor", "scene_anchor"],
            "is_product_anchor": False,
        },
        {
            "path": "/tmp/animated-character.png",
            "asset_id": 12,
            "filename": "animated-character.png",
            "semantic_roles": ["character_anchor", "action_anchor"],
            "is_product_anchor": False,
        },
        {
            "path": "/tmp/package.png",
            "asset_id": 13,
            "filename": "package.png",
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        },
    ]
    files = [
        SimpleNamespace(id=101, kind="reference_upload"),
        SimpleNamespace(id=102, kind="reference_upload"),
        SimpleNamespace(id=103, kind="reference_upload"),
    ]

    repaired, repaired_files = _apply_ai_segment_reference_strategy(
        {
            "reference_file_paths": refs,
            "content_factory_reference_manifest": [],
        },
        files,
        {
            "policy_version": "multimodal-test-v1",
            "keep_reference_aliases": ["@image2", "@image3"],
            "reference_rationale": (
                "The static product scene conflicts with the opening action."
            ),
        },
    )

    assert [row["asset_id"] for row in repaired["reference_file_paths"]] == [
        12,
        13,
    ]
    assert [row["alias"] for row in repaired[
        "content_factory_reference_manifest"
    ]] == ["@image1", "@image2"]
    assert [row.id for row in repaired_files] == [102, 103]
    assert repaired["content_factory_execution_reference_replan"][
        "kept_original_aliases"
    ] == ["@image2", "@image3"]


def test_multimodal_reference_strategy_can_omit_product_for_product_free_segment():
    refs = [
        {
            "path": "/tmp/animated-character.png",
            "asset_id": 12,
            "filename": "animated-character.png",
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        },
        {
            "path": "/tmp/package.png",
            "asset_id": 13,
            "filename": "package.png",
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        },
    ]
    files = [
        SimpleNamespace(id=102, kind="reference_upload"),
        SimpleNamespace(id=103, kind="reference_upload"),
    ]

    repaired, repaired_files = _apply_ai_segment_reference_strategy(
        {
            "reference_file_paths": refs,
            "content_factory_reference_manifest": [],
            "content_factory_product_required": True,
            "content_factory_product_anchor_required": True,
            "content_factory_product_requirement_source": {
                "kind": "stale_retry_evidence",
            },
        },
        files,
        {
            "policy_version": "multimodal-test-v1",
            "keep_reference_aliases": ["@image1"],
            "reference_rationale": "This opening is explicitly product-free.",
        },
    )

    assert [row["asset_id"] for row in repaired["reference_file_paths"]] == [12]
    assert [row.id for row in repaired_files] == [102]
    assert repaired["content_factory_product_presence_decided_by_model"] is True
    assert repaired["content_factory_product_required"] is False
    assert repaired["content_factory_product_anchor_required"] is False
    assert "content_factory_product_requirement_source" not in repaired


def test_product_free_multimodal_retry_drops_stale_product_prompt_lane():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    payload = {
        "model": "seedance_2_0_mini",
        "prompt": "\n".join([
            "Refs: @image1=character+scene; @image2=package",
            "Beats: 0-6s: compare three blank cards; no product visible.",
            "Product: uploaded package is sole authority.",
            "Audio: exact dialogue; 9:16; no text/UI/watermark.",
        ]),
        "content_factory_reference_manifest": [{
            "alias": "@image1",
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        }],
        "content_factory_product_presence_decided_by_model": True,
        "content_factory_product_required": False,
        "content_factory_product_anchor_required": False,
        "content_factory_segment_execution_repair": {
            "source": "ai_feasibility_replan",
            "repair_instruction": "Keep the card comparison product-free.",
            "upstream_repair_instruction": 'Show exact package label "melatonin-free".',
        },
    }

    compacted = _compact_content_factory_retry_prompt(project, payload)["prompt"]

    assert "Product:" not in compacted
    assert "package label" not in compacted
    assert "@image2" not in compacted
    assert "@image1" in compacted


def test_ai_execution_replan_replaces_provider_beats_before_seedance_compaction():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    original = {
        "model": "seedance_2_0_mini",
        "prompt": "\n".join(
            [
                "Refs: @image1=action+scene; @image2=character+scene; @image3=package",
                "Beats: 0-9s: old rack focus and hand choreography",
                "Product: uploaded package is sole authority.",
                "Audio: no speech/lip-sync; 9:16 stylized animation; no text/UI/watermark; 2/2.",
            ]
        ),
        "content_factory_reference_manifest": [],
    }
    replanned = {
        "policy_version": "test-v1",
        "segment_index": 2,
        "duration_seconds": 9,
        "segment_goal": "same conversion function",
        "provider_instruction": (
            "Generate stable shots and join final tabletop states with one hard cut."
        ),
        "timeline": [
            {
                "start_second": 0,
                "end_second": 2,
                "action": "Unbranded gummies and a distant dark phone; no product.",
            },
            {
                "start_second": 2,
                "end_second": 5.5,
                "action": "MYUPONA bottle centered beside exactly two gummies.",
            },
            {
                "start_second": 5.5,
                "end_second": 9,
                "action": (
                    "Hard cut from two gummies present and phone screen-up to "
                    "dish empty and phone face-down; fading amber pulse below."
                ),
            },
        ],
    }

    replanned_payload = _apply_ai_segment_execution_replan(original, replanned)
    compacted = _compact_content_factory_retry_prompt(
        project,
        replanned_payload,
    )["prompt"]

    assert len(compacted) <= 495
    assert "old rack focus" not in compacted
    assert "@image1" in compacted and "@image3" in compacted
    assert "two gummies" in compacted
    assert "dish empty" in compacted
    assert "phone-face-down" in compacted or "phone face-down" in compacted
    assert "amber pulse" in compacted


def test_inspiration_only_no_watermark_cover_contract_forbids_overlays():
    project = SimpleNamespace(
        config_json={
            "producer_intent_spec": {
                "intent_manifest": {
                    "transformation_contract": {
                        "transfer_mode": "inspiration_only",
                        "excluded_source_artifacts": [
                            "顶部字幕条和底部搜索栏等遮水印元素"
                        ],
                    },
                    "requirements": [{
                        "intent": "不得出现字幕条、搜索栏、平台UI或遮水印覆盖层。"
                    }],
                },
            }
        }
    )

    assert _project_forbids_local_display_overlays(project) is True


def test_inspiration_only_can_keep_explicit_native_display_copy():
    project = SimpleNamespace(
        config_json={
            "producer_intent_spec": {
                "transformation_contract": {
                    "transfer_mode": "inspiration_only",
                },
                "shared_requirements": [
                    "Create fresh native on-screen copy for the new concept."
                ],
            }
        }
    )

    assert _project_forbids_local_display_overlays(project) is False


def test_excluding_source_watermark_bands_does_not_forbid_fresh_local_overlays():
    project = SimpleNamespace(
        config_json={
            "producer_intent_spec": {
                "transformation_contract": {
                    "transfer_mode": "inspiration_only",
                    "excluded_source_artifacts": [
                        "Do not copy the source top caption band or bottom watermark cover."
                    ],
                },
                "shared_requirements": [
                    "Retain the cover function using freshly authored local overlays."
                ],
            }
        }
    )

    assert _project_forbids_local_display_overlays(project) is False


def test_explicit_postproduction_revision_overrides_stale_overlay_ban():
    project = SimpleNamespace(
        config_json={
            "producer_intent_spec": {
                "transformation_contract": {
                    "transfer_mode": "inspiration_only",
                },
                "shared_requirements": [
                    "不得出现字幕条、搜索栏、平台UI或遮水印覆盖层。"
                ],
            }
        },
        state_json={
            "functional_overlay_revision": {
                "status": "approved_local_postproduction_revision",
                "source": "explicit_user_clarification",
                "reuse_source_pixels": False,
                "reuse_source_wording": False,
            }
        },
    )

    assert _project_forbids_local_display_overlays(project) is False


def test_segment_execution_retry_preserves_signed_story_and_adds_pixel_repair():
    payload = {
        "prompt": "Original signed segment prompt.",
        "content_factory_base_prompt": "Original signed segment prompt.",
        "content_factory_requirement_contract": [{
            "requirement_id": "R-001",
            "intent": "The opening must stop the target viewer.",
            "observable_checks": ["A concrete visual disruption is visible in the first three seconds."],
        }],
        "content_factory_forbid_overlay_bands": True,
    }

    repaired = _apply_segment_execution_retry_prompt(
        payload,
        {
            "blocking": True,
            "policy_version": "test-v1",
            "failed_requirement_ids": ["R-001"],
            "blocking_reasons": ["static talking head"],
            "repair_instruction": "Execute the signed whip pan and object action.",
        },
    )

    assert "Original signed segment prompt." in repaired["prompt"]
    assert "Repair:" in repaired["prompt"]
    assert "requirements R-001" in repaired["prompt"]
    assert "no caption bands/search bars/platform UI" in repaired["prompt"]
    assert repaired["content_factory_segment_execution_repair"][
        "policy_version"
    ] == "test-v1"


def test_seedance_multimodal_retry_keeps_refs_actions_and_provider_budget():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    payload = {
        "model": "seedance_2_0_mini",
        "prompt": (
            "Refs: @image1=action+scene; @image2=scene+action+character; "
            "@image3=package\n"
            "Beats: 0-2s: hand pauses above unbranded gummies; product remains "
            "out of frame | 2-6s: MYUPONA bottle enters tray | 6-9s: takes "
            "exactly two gummies; final amber pulse\n"
            "Product: uploaded package is sole authority.\n"
            "Audio: no speech/lip-sync; 9:16 stylized animation; no "
            "text/UI/watermark; 2/2."
        ),
        "content_factory_base_prompt": "Older prompt without image aliases.",
    }
    repaired = _apply_segment_execution_retry_prompt(
        payload,
        {
            "blocking": True,
            "repair_instruction": (
                "Keep the final beat on the bedside tray: take exactly two "
                "gummies, set the phone face-down, then gesture downward to a "
                "descending amber pulse while the bottle stays secondary."
            ),
        },
    )
    compacted = _compact_content_factory_retry_prompt(project, repaired)

    assert len(compacted["prompt"]) <= 495
    assert "@image1" in compacted["prompt"]
    assert "@image3" in compacted["prompt"]
    assert (
        "phone-face-down" in compacted["prompt"]
        or "phone face-down" in compacted["prompt"]
    )
    assert "gesture downward" in compacted["prompt"]
    assert "SIGNED VISUAL EXECUTION REPAIR" not in compacted["prompt"]
    assert "two gummies" in compacted["prompt"]
    assert "amber pulse" in compacted["prompt"]
    assert (
        "no speech/lip-sync" in compacted["prompt"]
        or "Audio: silent" in compacted["prompt"]
    )
    assert compacted["content_factory_retry_prompt_budget"][
        "actual_characters"
    ] == len(compacted["prompt"])


def test_seedance_legacy_retry_recovers_manifest_aliases_and_qa_repair():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    payload = {
        "model": "seedance_2_0_mini",
        "prompt": (
            "SIGNED VISUAL EXECUTION REPAIR (authoritative): old verbose "
            "worker prefix that must not replace the compact repair.\n"
            "Beats: 0-2s: product out of frame | 2-6s: bottle enters tray | "
            "6-9s: take exactly two gummies\n"
            "Product: uploaded package is sole authority.\n"
            "Audio: no speech/lip-sync; 9:16 stylized animation; no "
            "text/UI/watermark; 2/2."
        ),
        "content_factory_segment_execution_repair": {
            "repair_instruction": (
                "Keep the tray: take two gummies, set phone face-down, then "
                "gesture downward to a descending amber pulse."
            ),
        },
        "content_factory_reference_manifest": [
            {
                "alias": "@image1",
                "semantic_roles": ["action_anchor", "scene_anchor"],
                "is_product_anchor": False,
            },
            {
                "alias": "@image2",
                "semantic_roles": ["scene_anchor", "character_anchor"],
                "is_product_anchor": False,
            },
            {
                "alias": "@image3",
                "semantic_roles": ["product_anchor"],
                "is_product_anchor": True,
            },
        ],
    }

    compacted = _compact_content_factory_retry_prompt(project, payload)

    assert len(compacted["prompt"]) <= 495
    assert "@image1=action+scene" in compacted["prompt"]
    assert "@image3=package" in compacted["prompt"]
    assert (
        "phone-face-down" in compacted["prompt"]
        or "phone face-down" in compacted["prompt"]
    )


def test_seedance_face_reference_repair_keeps_renumbered_package_alias():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Soothing Body Balm",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    payload = {
        "model": "seedance_2_0_mini",
        "prompt": (
            "Refs: @image1=package\n"
            "Character continuity: text-only fictional animated adult; not "
            "a face identity reference.\n"
            "Beats: 0-2s: folds towel | 2-4s: product upright | "
            "4-6s: nods once\n"
            "Dialogue: 'Made with MSM, it fits an easy body-care routine.'\n"
            "Voice: same female off-screen narrator; US accent.\n"
            "Product: uploaded package is sole authority.\n"
            "9:16; no captions/UI/watermark; segment 3/3."
        ),
        "content_factory_product_anchor_required": True,
        "content_factory_product_required": True,
        "content_factory_reference_manifest": [{
            "alias": "@image1",
            "asset_id": 5564,
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        }],
    }

    compacted = _compact_content_factory_retry_prompt(project, payload)

    assert len(compacted["prompt"]) <= 495
    assert "@image1" in compacted["prompt"]
    assert "@image2" not in compacted["prompt"]
    assert "Character continuity:" in compacted["prompt"]
    assert "0-2s" in compacted["prompt"]
    assert "4-6s" in compacted["prompt"]
    assert compacted["content_factory_provider_prompt_contract"][
        "reference_aliases"
    ] == ["@image1"]


def test_seedance_long_ai_replan_uses_extreme_budget_without_losing_actions():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    payload = {
        "model": "seedance_2_0_mini",
        "prompt": (
            "Repair: take exactly two gummies; set phone face-down; gesture "
            "downward to a descending amber pulse; keep bottle secondary; "
            "use distinct chronological visual states with a stable tray and "
            "clear hand-object contact throughout the entire final beat.\n"
            "Beats: 0-2s: hand over unbranded gummies, phone in distance | "
            "2-6s: centered MYUPONA bottle beside exactly two gummies | "
            "6-9s: take two, phone down, downward hand gesture\n"
            "Product: uploaded package is sole authority.\n"
            "Audio: no speech/lip-sync; 9:16 stylized animation; no "
            "text/UI/watermark; 2/2."
        ),
        "content_factory_reference_manifest": [
            {
                "alias": "@image1",
                "semantic_roles": ["action_anchor", "scene_anchor"],
            },
            {
                "alias": "@image2",
                "semantic_roles": [
                    "scene_anchor", "action_anchor", "character_anchor"
                ],
            },
            {
                "alias": "@image3",
                "semantic_roles": ["product_anchor"],
                "is_product_anchor": True,
            },
        ],
    }

    compacted = _compact_content_factory_retry_prompt(project, payload)

    assert len(compacted["prompt"]) <= 495
    assert "@image1" in compacted["prompt"]
    assert "@image3" in compacted["prompt"]
    assert "two gummies" in compacted["prompt"]
    assert (
        "phone-face-down" in compacted["prompt"]
        or "phone face-down" in compacted["prompt"]
    )
    assert "gesture downward" in compacted["prompt"]
    assert "Product: @image3 sole package authority." in compacted["prompt"]
    assert "no white packshot" in compacted["prompt"]


def test_seedance_extreme_budget_balances_all_repair_clauses():
    project = SimpleNamespace(
        product_id=1,
        product_name="MYUPONA Sleep Ease Gummies",
        config_json={
            "video_model": "seedance_2_0_mini",
            "video_reference_limit": 10,
            "video_generation_mode": "image_to_video",
        },
    )
    payload = {
        "model": "seedance_2_0_mini",
        "prompt": "\n".join(
            [
                "Refs: @image1=action+scene; @image2=character+scene; @image3=package",
                (
                    "Repair: Re-render in signed order; begin with a hand above "
                    "unbranded gummies and the phone distant; reveal the centered "
                    "MYUPONA bottle beside exactly two gummies; then take exactly "
                    "two gummies; then set the phone face-down; finally make a "
                    "distinct downward hand gesture with a fading amber pulse."
                ),
                (
                    "Beats: 0-3s hold the opening composition and rack focus | "
                    "3-6s reveal the bottle and two gummies | 6-9s execute actions"
                ),
                "Product: uploaded package is sole authority.",
                (
                    "Audio: no speech/lip-sync; 9:16 stylized animation; no "
                    "text/UI/watermark; 2/2."
                ),
            ]
        ),
        "content_factory_reference_manifest": [],
    }

    compacted = _compact_content_factory_retry_prompt(project, payload)["prompt"]

    assert len(compacted) <= 495
    assert "@image1" in compacted and "@image3" in compacted
    assert "two gummies" in compacted
    assert "phone-face-down" in compacted or "phone face-down" in compacted
    assert "downward" in compacted and "amber pulse" in compacted
    assert (
        "sole authority" in compacted
        or "sole package authority" in compacted
    )
from app.tasks.hermes_agent import content_factory_tasks as content_factory_tasks_module
from app.tasks.ai_video.video_tasks import (
    _content_factory_dependency_pending,
    _content_factory_execution_authority,
)
from app.data.models.hermes_agent import (
    HermesContentExecution,
    HermesContentFactoryProject,
    HermesContentFactoryStage,
    HermesContentSegmentRun,
    HermesContentVariantRun,
)
from app.data.models.kie_api import KieApiKey, KieFile, KieTask
from sqlalchemy.orm import sessionmaker


def _task(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(input_json=payload, model=payload.get("model"))


def test_retry_state_merge_preserves_concurrently_added_sibling_group():
    latest = {
        "ai_video_task_ids": [101, 202],
        "ai_video_failed_task_ids": [101, 202],
        "ai_video_groups": [
            {
                "video_index": 1,
                "segments": [{"segment_index": 1, "task_id": 101}],
            },
            {
                "video_index": 2,
                "segments": [{"segment_index": 1, "task_id": 202}],
            },
        ],
    }
    stale_retry = {
        "ai_video_task_ids": [303],
        "ai_video_failed_task_ids": [],
        "ai_video_groups": [
            {
                "video_index": 1,
                "segments": [{
                    "segment_index": 1,
                    "task_id": 303,
                    "retry_source_task_id": 101,
                }],
            }
        ],
        "ai_video_segment_retry_counts": {"1:1": 1},
    }

    merged = content_factory_tasks_module._merge_video_retry_state(
        latest,
        stale_retry,
        retry_groups=stale_retry["ai_video_groups"],
        affected_video_indices={1},
        replaced_task_ids={101},
        restored_dependency_ids=set(),
    )

    groups = {
        int(item["video_index"]): item for item in merged["ai_video_groups"]
    }
    assert groups[1]["segments"][0]["task_id"] == 303
    assert groups[2]["segments"][0]["task_id"] == 202
    assert merged["ai_video_task_ids"] == [202, 303]
    assert merged["ai_video_failed_task_ids"] == [202]


def test_missing_media_group_is_recovered_from_immutable_execution_ledger(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_restore_media_group_signed_contract",
        lambda _db, _project, group: group,
    )
    project = SimpleNamespace(
        id=168,
        project_key="project-168",
        workspace_id=12,
        user_id=34,
        title="Project 168",
        product_name="",
        config_json={"video_count": 2, "video_model": "omni_flash"},
    )
    stage = HermesContentFactoryStage(
        project_id=168,
        workspace_id=12,
        user_id=34,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="success",
        output_json={},
    )
    db_session.add(stage)
    db_session.flush()
    manifest = "a" * 64
    task = KieTask(
        workspace_id=12,
        key_id=1,
        created_by_user_id=34,
        model="omni_flash",
        task_id="provider-ledger-recovery",
        state="failed",
        input_json={
            "prompt": "One more scroll and suddenly it is tomorrow.",
            "seconds": "4",
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "content_factory_project_id": 168,
            "content_factory_video_index": 2,
            "content_factory_variant_index": 2,
            "content_factory_segment_index": 1,
            "content_factory_final_name_base": "project-168-v02-2",
            "content_factory_source_stage_id": int(stage.id),
            "content_factory_media_manifest_sha256": manifest,
            "content_factory_audio_mode": "spoken",
            "content_factory_video_language": "en-US",
            "content_factory_dialogue_lines": [
                "One more scroll and suddenly it is tomorrow."
            ],
            "content_factory_segment_execution_contract": {
                "segment_index": 1,
                "duration_seconds": 4,
                "segment_goal": "Immediate scrolling contradiction.",
                "timeline": [{
                    "start_second": 0,
                    "end_second": 4,
                    "action": "The clock races forward while she scrolls.",
                }],
                "requirement_ids": ["R-004"],
            },
        },
    )
    db_session.add(task)
    db_session.flush()
    execution = HermesContentExecution(
        project_id=168,
        workspace_id=12,
        user_id=34,
        execution_key="execution-168",
        status="running",
        target_count=2,
        config_sha256="b" * 64,
    )
    db_session.add(execution)
    db_session.flush()
    variant = HermesContentVariantRun(
        execution_id=int(execution.id),
        project_id=168,
        workspace_id=12,
        user_id=34,
        variant_index=2,
        deliverable_ordinal=2,
        attempt=1,
        state="segments_downloaded",
        media_manifest_sha256=manifest,
        input_sha256="c" * 64,
        meta_json={
            "duration": 4,
            "aspect_ratio": "9:16",
            "source_stage_id": int(stage.id),
        },
    )
    db_session.add(variant)
    db_session.flush()
    db_session.add(HermesContentSegmentRun(
        variant_run_id=int(variant.id),
        project_id=168,
        workspace_id=12,
        user_id=34,
        segment_index=1,
        attempt=1,
        state="downloaded",
        provider_key="sub2api",
        provider_model="omni_flash",
        provider_task_row_id=int(task.id),
        input_sha256="d" * 64,
    ))
    db_session.flush()

    group = content_factory_tasks_module._recover_media_group_from_execution_ledger(
        db_session,
        project=project,
        task=task,
    )

    assert group is not None
    assert group["video_index"] == 2
    assert group["source_stage_id"] == int(stage.id)
    assert group["media_manifest_sha256"] == manifest
    assert group["segments"][0]["task_id"] == int(task.id)
    assert group["segments"][0]["duration"] == 4
    assert group["segments"][0]["recovered_from_execution_ledger"] is True

    recovered_by_variant = (
        content_factory_tasks_module
        ._recover_variant_media_group_from_execution_ledger(
            db_session,
            project=project,
            variant_index=2,
        )
    )
    assert recovered_by_variant is not None
    assert recovered_by_variant["video_index"] == 2
    assert recovered_by_variant["segments"][0]["task_id"] == int(task.id)


def test_provider_authority_falls_back_to_newest_immutable_segment_ledger(
    db_session,
    monkeypatch,
):
    project = HermesContentFactoryProject(
        project_key="cf-ledger-authority",
        workspace_id=12,
        user_id=34,
        title="Ledger authority",
        product_name="",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={},
        state_json={"ai_video_groups": []},
    )
    db_session.add(project)
    db_session.flush()
    stage = HermesContentFactoryStage(
        project_id=int(project.id),
        workspace_id=12,
        user_id=34,
        stage="VIDEO_PROMPTS",
        attempt=1,
        status="success",
        output_json={},
    )
    db_session.add(stage)
    db_session.flush()
    manifest = "e" * 64
    task = KieTask(
        workspace_id=12,
        key_id=1,
        created_by_user_id=34,
        model="omni_flash",
        task_id="local-ai-video-ledger-authority",
        state="queued_local",
        input_json={
            "content_factory_project_key": project.project_key,
            "content_factory_project_id": int(project.id),
            "content_factory_variant_index": 3,
            "content_factory_video_index": 3,
            "content_factory_segment_index": 2,
            "content_factory_source_stage_id": int(stage.id),
            "content_factory_media_manifest_sha256": manifest,
        },
        result_json={"__local": {}},
    )
    db_session.add(task)
    db_session.flush()
    execution = HermesContentExecution(
        project_id=int(project.id),
        workspace_id=12,
        user_id=34,
        execution_key="ledger-authority-execution",
        status="running",
        target_count=3,
        config_sha256="f" * 64,
    )
    db_session.add(execution)
    db_session.flush()
    variant = HermesContentVariantRun(
        execution_id=int(execution.id),
        project_id=int(project.id),
        workspace_id=12,
        user_id=34,
        variant_index=3,
        deliverable_ordinal=3,
        attempt=1,
        state="segments_submitted",
        media_manifest_sha256=manifest,
        input_sha256=manifest,
        meta_json={"source_stage_id": int(stage.id)},
    )
    db_session.add(variant)
    db_session.flush()
    db_session.add(HermesContentSegmentRun(
        variant_run_id=int(variant.id),
        project_id=int(project.id),
        workspace_id=12,
        user_id=34,
        segment_index=2,
        attempt=1,
        state="queued_local",
        provider_key="sub2api",
        provider_model="omni_flash",
        provider_task_row_id=int(task.id),
        input_sha256="1" * 64,
    ))
    db_session.commit()

    assert _content_factory_execution_authority(db_session, task) == (
        True,
        "current_variant_segment_ledger",
    )

    monkeypatch.setattr(
        content_factory_tasks_module,
        "_restore_media_group_signed_contract",
        lambda _db, _project, group: group,
    )
    task.state = "failed"
    task.fail_code = "cf_task_not_authoritative"
    task.fail_msg = "Content-factory task ignored before provider I/O."
    task.result_json = {
        "__local": {"authority_rejected_reason": "variant_group_missing"}
    }
    db_session.add(task)
    db_session.commit()
    assert content_factory_tasks_module._recover_premature_authority_rejections(
        db_session,
        project=project,
        tasks=[task],
    ) == [int(task.id)]
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(task)
    assert task.state == "queued_local"
    assert task.fail_code is None
    assert project.state_json["ai_video_groups"][0]["segments"][0][
        "task_id"
    ] == int(task.id)

    newer = HermesContentVariantRun(
        execution_id=int(execution.id),
        project_id=int(project.id),
        workspace_id=12,
        user_id=34,
        variant_index=3,
        deliverable_ordinal=3,
        attempt=2,
        state="segments_submitted",
        media_manifest_sha256="2" * 64,
        input_sha256="2" * 64,
        meta_json={"source_stage_id": int(stage.id)},
    )
    db_session.add(newer)
    project.state_json = {"ai_video_groups": []}
    db_session.add(project)
    db_session.commit()

    assert _content_factory_execution_authority(db_session, task) == (
        False,
        "variant_group_missing",
    )


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


def test_dependency_release_replaces_refs_row_and_restamps_exact_prompt():
    base_prompt = (
        "Refs: @image1=character+scene; @image2=package\n"
        "Beats: 0-2s: she sets down her running bag.\n"
        "Product: uploaded package is sole authority. In-scene; no white background/packshot.\n"
        "Dialogue: 'Step one: apply a small amount.'\n\n"
        "REFERENCE ANCHORS (uploaded order is authoritative):\n"
        "@image1: character_anchor; stale portrait\n"
        "Character identity authority: @image1.\n"
        "One continuous full-frame scene."
    )
    references = [
        {
            "filename": "previous-last-frame.png",
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_continuity_frame": True,
        },
        {
            "filename": "product.png",
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        },
    ]
    prompt = _omni_reference_prompt(
        base_prompt,
        references,
        product_required=True,
        first_frame=True,
    )
    params = {
        "prompt": prompt,
        "content_factory_product_required": True,
        "content_factory_product_anchor_required": True,
        "content_factory_reference_manifest": [
            {"alias": "@image1", "is_product_anchor": False},
            {"alias": "@image2", "is_product_anchor": True},
        ],
    }

    _validate_and_stamp_provider_prompt(
        params,
        source_prompt=base_prompt,
        actual_prompt=prompt,
    )

    assert "Refs: =" not in prompt
    assert prompt.count("REFERENCE ANCHORS") == 1
    assert "stale portrait" not in prompt
    assert "@image1" in prompt and "@image2" in prompt
    assert params["content_factory_provider_prompt_contract"]["validated"] is True
    assert (
        params["content_factory_provider_prompt_contract"]["actual_sha256"]
        == __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest()
    )


def test_final_intent_repair_scope_uses_segment_indices_not_evidence_task_ids():
    segment_two = SimpleNamespace(
        id=3670,
        input_json={
            "content_factory_video_index": 7,
            "content_factory_segment_index": 2,
        },
    )
    segment_three = SimpleNamespace(
        id=3671,
        input_json={
            "content_factory_video_index": 7,
            "content_factory_segment_index": 3,
        },
    )

    scoped = _scope_final_intent_repair_tasks(
        [segment_two, segment_three],
        {
            "repair_scope": "segment_regeneration",
            "video_index": 7,
            "evidence_segment_indices": [2, 3],
            "regenerate_segment_indices": [3],
            "affected_segment_indices": [2, 3],
            "affected_task_ids": [3670, 3671],
        },
    )

    assert [task.id for task in scoped] == [3671]


def test_final_intent_repair_scope_does_not_broaden_stale_explicit_segment():
    segment_two = SimpleNamespace(
        id=3670,
        input_json={
            "content_factory_video_index": 7,
            "content_factory_segment_index": 2,
        },
    )

    scoped = _scope_final_intent_repair_tasks(
        [segment_two],
        {
            "repair_scope": "segment_regeneration",
            "video_index": 7,
            "affected_segment_indices": [3],
            "affected_task_ids": [3670],
        },
    )

    assert scoped == []


def test_untracked_local_content_task_is_superseded_without_touching_live_group(
    db_session,
):
    now = datetime(2026, 8, 9, 12, 0, 0)
    project = HermesContentFactoryProject(
        project_key="cf_orphan_local_task",
        workspace_id=3,
        user_id=6,
        title="Interrupted retry commit",
        product_name="Example",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True},
        state_json={},
    )
    db_session.add(project)
    db_session.flush()
    live = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="local-live-ledger-task",
        state="waiting_dependency",
        input_json={
            "content_factory_project_id": int(project.id),
            "content_factory_video_index": 1,
            "content_factory_segment_index": 2,
        },
        result_json={},
    )
    orphan = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="local-interrupted-retry-task",
        state="waiting_dependency",
        input_json={
            "content_factory_project_id": int(project.id),
            "content_factory_video_index": 1,
            "content_factory_segment_index": 3,
        },
        result_json={},
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    db_session.add_all([live, orphan])
    db_session.flush()
    project.state_json = {
        "ai_video_task_ids": [int(live.id)],
        "ai_video_groups": [{
            "video_index": 1,
            "segments": [{"segment_index": 2, "task_id": int(live.id)}],
        }],
    }
    db_session.add(project)
    db_session.flush()

    superseded = _supersede_untracked_local_content_video_tasks(
        db_session,
        project,
        now=now,
    )

    assert superseded == [int(orphan.id)]
    assert live.state == "waiting_dependency"
    assert orphan.state == "failed"
    assert orphan.fail_code == "cf_variant_superseded"
    assert (
        get_task_local_meta(orphan).get("superseded_reason")
        == "untracked_local_task_after_interrupted_commit"
    )


def test_untracked_local_content_task_is_protected_during_waiter_commit_window(
    db_session,
):
    now = datetime(2026, 8, 9, 12, 0, 0)
    project = HermesContentFactoryProject(
        project_key="cf_inflight_retry_commit",
        workspace_id=3,
        user_id=6,
        title="In-flight retry commit",
        product_name="Example",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"auto_run": True},
        state_json={
            "ai_video_wait_heartbeat_at": (
                now - timedelta(minutes=6)
            ).isoformat(),
        },
    )
    db_session.add(project)
    db_session.flush()
    in_flight = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="local-inflight-retry-task",
        state="queued_local",
        input_json={
            "content_factory_project_id": int(project.id),
            "content_factory_video_index": 1,
            "content_factory_segment_index": 2,
        },
        result_json={},
        created_at=now - timedelta(minutes=4),
        updated_at=now - timedelta(minutes=4),
    )
    db_session.add(in_flight)
    db_session.flush()

    superseded = _supersede_untracked_local_content_video_tasks(
        db_session,
        project,
        now=now,
    )

    assert superseded == []
    assert in_flight.state == "queued_local"
    assert in_flight.fail_code is None


def test_text_to_video_recovery_releases_all_segments_without_references(
    db_session,
    monkeypatch,
):
    project = HermesContentFactoryProject(
        project_key="cf_t2v_dependency_recovery",
        workspace_id=3,
        user_id=6,
        title="T2V recovery",
        product_name="",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={
            "video_generation_mode": "text_to_video",
            "video_model": "omni_flash",
            "video_reference_limit": 7,
            "product_required": False,
        },
        state_json={},
    )
    db_session.add(project)
    db_session.flush()
    task = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="local-t2v-recovery",
        state="waiting_dependency",
        input_json={
            "model": "omni_flash",
            "content_factory_project_id": project.id,
            "content_factory_project_key": project.project_key,
            "content_factory_video_generation_mode": "text_to_video",
            "content_factory_segment_index": 2,
            "content_factory_dependency_task_id": 999,
            "content_factory_continuity_dependency": "previous_segment",
            "content_factory_first_frame": False,
            "reference_file_paths": [{"path": "/tmp/stale.png"}],
            "reference_video_file_paths": [{"path": "/tmp/stale.mp4"}],
        },
        result_json={},
    )
    db_session.add(task)
    db_session.flush()
    db_session.add_all([
        KieFile(
            workspace_id=3,
            key_id=1,
            task_id=task.id,
            kind="reference_upload",
            file_url="/tmp/stale.png",
        ),
        KieFile(
            workspace_id=3,
            key_id=1,
            task_id=task.id,
            kind="reference_video_upload",
            file_url="/tmp/stale.mp4",
        ),
    ])
    project.state_json = {
        "ai_video_groups": [{
            "video_index": 1,
            "segments": [{
                "task_id": task.id,
                "segment_index": 2,
                "dependency_task_id": 999,
                "dependency_status": "waiting_previous_segment",
                "continuity_dependency": "previous_segment",
                "reference_indices": [1],
                "reference_names": ["stale.png"],
                "reference_bindings": [{"filename": "stale.png"}],
                "reference_video_names": ["stale.mp4"],
            }],
        }],
    }
    db_session.flush()
    published = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    released = _release_ready_segment_dependencies(
        db_session,
        project,
        [task],
    )

    db_session.refresh(task)
    params = dict(task.input_json or {})
    segment = dict(project.state_json["ai_video_groups"][0]["segments"][0])
    assert released == [task.id]
    assert task.state == "queued_local"
    assert params["content_factory_dependency_task_id"] is None
    assert params["content_factory_continuity_dependency"] == "independent"
    assert params["reference_file_paths"] == []
    assert params["reference_video_file_paths"] == []
    assert segment["dependency_status"] == "ready"
    assert segment["reference_bindings"] == []
    assert db_session.query(KieFile).filter(KieFile.task_id == task.id).count() == 0
    assert published[0]["kwargs"]["local_task_id"] == task.id


def test_non_content_factory_task_is_not_affected():
    task = _task({
        "model": "omni_flash",
        "content_factory_segment_index": 2,
        "content_factory_first_frame": False,
    })

    assert _content_factory_dependency_pending(task) is False


def test_terminal_dependency_chain_collapses_to_fixed_point(
    db_session,
):
    project = HermesContentFactoryProject(
        project_key="cf_dependency_fixed_point",
        workspace_id=3,
        user_id=6,
        title="Dependency fixed point",
        product_name="",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"content_factory_video_retry_limit": 2},
        state_json={},
    )
    db_session.add(project)
    db_session.flush()
    root = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="dependency-root-failed",
        state="failed",
        fail_code="dependency_failed",
        fail_msg="terminal predecessor",
        input_json={
            "content_factory_project_id": int(project.id),
            "content_factory_project_key": project.project_key,
            "content_factory_video_index": 1,
            "content_factory_segment_index": 1,
        },
        result_json={},
    )
    db_session.add(root)
    db_session.flush()
    child = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="dependency-child-waiting",
        state="waiting_dependency",
        input_json={
            "content_factory_project_id": int(project.id),
            "content_factory_project_key": project.project_key,
            "content_factory_video_index": 1,
            "content_factory_segment_index": 2,
            "content_factory_dependency_task_id": int(root.id),
        },
        result_json={},
    )
    db_session.add(child)
    db_session.flush()
    grandchild = KieTask(
        workspace_id=3,
        created_by_user_id=6,
        key_id=1,
        model="omni_flash",
        task_id="dependency-grandchild-waiting",
        state="waiting_dependency",
        input_json={
            "content_factory_project_id": int(project.id),
            "content_factory_project_key": project.project_key,
            "content_factory_video_index": 1,
            "content_factory_segment_index": 3,
            "content_factory_dependency_task_id": int(child.id),
        },
        result_json={},
    )
    db_session.add(grandchild)
    db_session.flush()
    project.state_json = {
        "ai_video_task_ids": [root.id, child.id, grandchild.id],
        "ai_video_pending_task_ids": [child.id, grandchild.id],
    }
    db_session.flush()

    failed_ids = (
        content_factory_tasks_module
        ._fail_unreleasable_segment_dependencies(
            db_session,
            project,
            [grandchild, child, root],
        )
    )

    db_session.flush()
    assert set(failed_ids) == {int(child.id), int(grandchild.id)}
    assert child.state == grandchild.state == "failed"
    assert child.fail_code == grandchild.fail_code == "dependency_failed"
    assert project.state_json["ai_video_pending_task_ids"] == []


def test_provider_cycle_quota_tail_does_not_mask_prior_retryable_route():
    cycle_failure = SimpleNamespace(
        fail_code="bandianwa_worker_error",
        fail_msg='ToAPIs HTTP 403: {"code":"quota_not_enough"}',
        result_json={
            "__local": {
                "active_provider": "toapis",
                "attempted_provider_key_ids": [3],
            },
        },
    )
    lone_quota_failure = SimpleNamespace(
        fail_code="provider_quota_exhausted",
        fail_msg="quota not enough",
        result_json={"__local": {}},
    )

    assert _content_factory_retryable_video_failure(cycle_failure) is True
    assert _content_factory_retryable_video_failure(lone_quota_failure) is False


def test_quality_diagnostic_insufficient_language_is_not_quota_failure():
    task = SimpleNamespace(
        fail_code=content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE,
        fail_msg=(
            "CONTENT_FINAL_INTENT_QA_FAILED: Effectiveness transfer is "
            "insufficient because the opening hook is too weak."
        ),
        result_json={"__local": {}},
    )

    assert _content_factory_retryable_video_failure(task) is True


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


def test_seedance_reference_anchored_segment_does_not_invent_previous_frame_requirement():
    task = SimpleNamespace(
        state="success",
        model="seedance_2_0_mini",
        input_json={
            "content_factory_project_key": "cf_seedance_parallel",
            "content_factory_segment_index": 2,
            # Preserve the historical packet shape that exposed the incident:
            # narrative continuity existed but no provider frame dependency did.
            "content_factory_continuity_dependency": "previous_segment",
            "content_factory_dependency_task_id": None,
            "content_factory_requires_previous_segment_frame": False,
            "content_factory_first_frame": False,
            "reference_file_paths": [
                {"path": "/tmp/scene-anchor.png", "is_continuity_frame": False},
                {"path": "/tmp/product-anchor.png", "is_continuity_frame": False},
            ],
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
    assert task.state == "success"
    assert task.fail_code is None
    incident = get_task_local_meta(task)["content_quality_incident"]
    assert incident["code"] == "segment_release_quality_gate"
    assert any("duration mismatch" in item for item in report["failures"])
    assert any("aspect mismatch" in item for item in report["failures"])
    assert any("requires an audio stream" in item for item in report["failures"])


def test_segment_release_gate_preserves_product_visual_failure_code(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"downloaded-provider-result")
    task = SimpleNamespace(
        id=92,
        state="success",
        fail_code=None,
        fail_msg=None,
        result_json={},
        input_json={
            "seconds": 10,
            "aspect_ratio": "9:16",
            "content_factory_audio_mode": "spoken",
            "content_factory_product_anchor_required": True,
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
        lambda _path: 10.0,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_video_dimensions",
        lambda _path: (720, 1280),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_video_has_audio_stream",
        lambda _path: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_review_provider_product_segment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError(
                "CONTENT_FINAL_QUALITY_GATE_FAILED: "
                "CONTENT_PRODUCT_VIDEO_VISUAL_QA_FAILED_TASK_92: "
                "package identity changed"
            )
        ),
    )

    report = _segment_release_quality_gate(
        FakeDb(),
        project=SimpleNamespace(id=1),
        task=task,
        source=source,
    )

    assert report["status"] == "FAIL"
    assert task.state == "success"
    assert task.fail_code is None
    incident = get_task_local_meta(task)["content_quality_incident"]
    assert incident["code"] == "product_visual_qa"
    assert "package identity changed" in incident["message"]


def test_segment_release_gate_does_not_regenerate_for_reviewer_outage(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "segment.mp4"
    source.write_bytes(b"downloaded-provider-result")
    task = SimpleNamespace(
        id=93,
        state="success",
        fail_code=None,
        fail_msg=None,
        result_json={},
        input_json={
            "seconds": 10,
            "aspect_ratio": "9:16",
            "content_factory_audio_mode": "spoken",
            "content_factory_product_anchor_required": True,
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
        lambda _path: 10.0,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_video_dimensions",
        lambda _path: (720, 1280),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_video_has_audio_stream",
        lambda _path: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_review_provider_segment_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("multimodal reviewer HTTP 503")
        ),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_review_provider_product_segment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("multimodal reviewer timed out")
        ),
    )

    report = _segment_release_quality_gate(
        FakeDb(),
        project=SimpleNamespace(id=1),
        task=task,
        source=source,
    )

    assert report["status"] == "PASS"
    assert report["blocking"] is False
    assert report["failures"] == []
    assert len(report["review_diagnostics"]) == 2
    assert task.state == "success"
    assert get_task_local_meta(task).get("content_quality_incident") is None


def test_old_generic_release_failure_is_normalized_for_immediate_product_retry():
    task = SimpleNamespace(
        fail_code="segment_release_quality_gate",
        fail_msg=(
            "Segment failed the pre-dependency quality gate: "
            "CONTENT_FINAL_QUALITY_GATE_FAILED: "
            "CONTENT_PRODUCT_VIDEO_VISUAL_QA_FAILED_TASK_92: "
            "package identity changed"
        ),
        result_json={},
    )

    assert _normalize_segment_release_failure_code(task) == "product_visual_qa"
    assert task.fail_code == "segment_release_quality_gate"


def test_product_visual_review_becomes_multimodal_replan_evidence():
    review = content_factory_tasks_module._product_visual_replan_review(
        {
            "provider_product_video_review": {
                "policy_version": "visual-review-v9",
                "observed_facts": [
                    "The woman is visible, but no product package appears."
                ],
                "blocking_reasons": [
                    "Product missing from the intended product segment."
                ],
            }
        },
        diagnostic="CONTENT_PRODUCT_VIDEO_VISUAL_QA_FAILED_TASK_92",
    )

    assert review["blocking"] is True
    assert review["policy_version"] == "visual-review-v9"
    assert review["observed_execution"] == [
        "The woman is visible, but no product package appears."
    ]
    assert "Product missing" in review["repair_instruction"]
    assert "Preserve the signed story" in review["repair_instruction"]


def test_retry_restores_signed_style_and_edit_grammar_from_exact_source_stage():
    source_stage = SimpleNamespace(
        project_id=185,
        stage="VIDEO_PROMPTS",
        status="success",
        output_json={
            "result": {
                "videos": [{
                    "visual_style": "Stylized 2D adult editorial animation.",
                    "visual_grammar": "Rapid hard cuts with no slow push-in.",
                    "segments": [
                        {"segment_index": 1, "prompt": "Opening."},
                        {
                            "segment_index": 2,
                            "prompt": "Application.",
                            "camera_direction": "Macro, tight, then locked medium.",
                        },
                    ],
                }],
            },
        },
    )

    class FakeDb:
        def get(self, _model, identity):
            return source_stage if int(identity) == 3159 else None

    project = SimpleNamespace(id=185)
    restored = content_factory_tasks_module._restore_authoritative_segment_direction_from_source_stage(
        FakeDb(),
        project=project,
        retry_input={
            "content_factory_source_stage_id": 3159,
            "content_factory_video_index": 2,
            "content_factory_segment_index": 2,
            "content_factory_segment_execution_contract": {
                "segment_index": 2,
                "pacing": "Existing AI-authored beat timing.",
            },
        },
    )

    contract = restored["content_factory_segment_execution_contract"]
    assert contract["visual_style"] == "Stylized 2D adult editorial animation."
    assert contract["visual_grammar"] == "Rapid hard cuts with no slow push-in."
    assert contract["pacing"] == "Existing AI-authored beat timing."
    assert contract["camera_direction"] == "Macro, tight, then locked medium."
    assert restored["content_factory_source_direction_restored"] is True


def test_execution_replan_preserves_signed_visual_medium_and_edit_grammar():
    repaired = _apply_ai_segment_execution_replan(
        {
            "prompt": "Old provider prompt.",
            "content_factory_segment_execution_contract": {
                "segment_index": 2,
                "visual_style": "Stylized 2D adult editorial animation.",
                "visual_grammar": "Rapid hard cuts; never a slow push-in.",
            },
        },
        {
            "policy_version": "replan-v1",
            "segment_index": 2,
            "duration_seconds": 6,
            "timeline": [{
                "start_second": 0,
                "end_second": 6,
                "action": "She completes the application.",
                "provider_action_en": "She completes the application.",
            }],
            "pacing": "Fast three-beat action.",
            "camera_direction": "Macro to locked medium.",
            "provider_direction_en": (
                "Three rapid cuts; macro to locked medium; stylized 2D animation."
            ),
        },
    )

    contract = repaired["content_factory_segment_execution_contract"]
    assert contract["visual_style"] == "Stylized 2D adult editorial animation."
    assert contract["visual_grammar"] == "Rapid hard cuts; never a slow push-in."
    assert "Direction: Three rapid cuts" in repaired["prompt"]


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


def test_retry_loop_never_resubmits_a_just_restored_chained_segment():
    source = Path(content_factory_tasks_module.__file__).read_text(encoding="utf-8")
    retry = source[
        source.index("def _retry_failed_video_segments")
        : source.index("def _queue_next_variant_after_video_submit")
    ]

    restored_guard = retry.index(
        "if int(failed.id) in restored_dependency_ids:"
    )
    retry_reset = retry.index("retry_task = create_local_video_task")
    restore_chain = retry.index(
        "restored_dependency_ids.update(",
        retry_reset,
    )

    assert restored_guard < retry_reset < restore_chain


def test_content_factory_retry_commits_publish_lease_before_queue_handoff():
    source = Path(content_factory_tasks_module.__file__).read_text(encoding="utf-8")
    retry = source[
        source.index("def _retry_failed_video_segments")
        : source.index("def _queue_next_variant_after_video_submit")
    ]

    retry_reset = retry.index("retry_task = create_local_video_task")
    publish_lease = retry.index("submit_enqueued_at=", retry_reset)
    commit = retry.index("db.commit()", publish_lease)
    publish = retry.index("submit_and_poll_ai_video_task.apply_async", commit)

    assert retry_reset < publish_lease < commit < publish


def test_content_factory_quality_retry_creates_new_task_and_repoints_group(
    db_session,
    monkeypatch,
):
    key = KieApiKey(
        name="content-retry-route",
        provider_key="sub2api",
        api_key_ciphertext="test-only",
        is_active=True,
        is_default=False,
        scopes_json=["video:generate", "video:seedance_2_0_mini"],
        model_priorities_json={"seedance_2_0_mini": 1},
    )
    db_session.add(key)
    db_session.flush()
    failed = KieTask(
        workspace_id=3,
        key_id=int(key.id),
        created_by_user_id=None,
        model="seedance_2_0_mini",
        task_id="provider-completed-but-quality-rejected",
        state="failed",
        fail_code="bandianwa_worker_error",
        fail_msg=(
            "Sub2API Flow HTTP 409: Idempotency key was reused with a "
            "different request"
        ),
        input_json={
            "model": "seedance_2_0_mini",
            "prompt": "original prompt",
            "seconds": 10,
            "aspect_ratio": "9:16",
            "content_factory_project_id": 179,
            "content_factory_project_key": "cf-retry-179",
            "content_factory_video_index": 1,
                "content_factory_segment_index": 2,
                "content_factory_final_name_base": "video-001",
                "content_factory_product_visual_retry": True,
                "content_factory_prompt_policy_version": (
                    content_factory_tasks_module.VIDEO_PROMPT_POLICY_VERSION
                ),
            },
        result_json={"__local": {
            "content_factory_retry": 7,
            "content_factory_retry_mode": "same_task_id",
            "doubao_failed_account_bridge_ids": ["br_text_only"],
        }},
    )
    db_session.add(failed)
    db_session.flush()
    old_task_id = int(failed.id)
    downstream = KieTask(
        workspace_id=3,
        key_id=int(key.id),
        created_by_user_id=None,
        model="seedance_2_0_mini",
        task_id="local-downstream-waiting",
        state="waiting_dependency",
        input_json={
            "model": "seedance_2_0_mini",
            "seconds": 10,
            "content_factory_project_id": 179,
            "content_factory_project_key": "cf-retry-179",
            "content_factory_video_index": 1,
            "content_factory_segment_index": 3,
            "content_factory_dependency_task_id": old_task_id,
        },
        result_json={"__local": {"dependency_task_id": old_task_id}},
    )
    db_session.add(downstream)
    db_session.flush()
    project = SimpleNamespace(
        id=179,
        workspace_id=3,
        user_id=None,
        project_key="cf-retry-179",
        config_json={"content_factory_video_retry_limit": 2},
        state_json={
            "ai_video_task_ids": [old_task_id, int(downstream.id)],
            "ai_video_segment_retry_counts": {"1:2": 7},
            "ai_video_groups": [{
                "video_index": 1,
                "segments": [
                    {
                        "task_id": old_task_id,
                        "segment_index": 2,
                        "dependency_task_id": None,
                    },
                    {
                        "task_id": int(downstream.id),
                        "segment_index": 3,
                        "dependency_task_id": old_task_id,
                    },
                ],
            }],
        },
        status="failed",
        last_error="quality failed",
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "resolve_video_model_key",
        lambda *_args, **_kwargs: key,
    )
    published = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    retry_ids = _retry_failed_video_segments(
        db_session,
        project,
        [failed],
    )

    assert len(retry_ids) == 1
    assert retry_ids[0] != old_task_id
    assert project.state_json["ai_video_task_ids"] == [
        int(downstream.id),
        retry_ids[0],
    ]
    active_segment = project.state_json["ai_video_groups"][0]["segments"][0]
    assert active_segment["task_id"] == retry_ids[0]
    assert active_segment["retry_source_task_id"] == old_task_id
    downstream_segment = project.state_json["ai_video_groups"][0]["segments"][1]
    assert downstream_segment["dependency_task_id"] == retry_ids[0]
    assert downstream.input_json["content_factory_dependency_task_id"] == (
        retry_ids[0]
    )
    retry_task = db_session.get(KieTask, retry_ids[0])
    assert retry_task is not None
    assert retry_task.state == "queued_local"
    assert retry_task.task_id.startswith("local-ai-video-")
    assert retry_task.result_json["__local"]["content_factory_retry_mode"] == (
        "new_task_attempt"
    )
    assert not retry_task.result_json["__local"].get(
        "doubao_failed_account_bridge_ids"
    )
    assert failed.state == "failed"
    assert failed.result_json["__local"]["content_factory_retry_child_task_id"] == (
        retry_ids[0]
    )
    assert project.state_json["ai_video_retry_history"][-1]["retry_task_id"] == (
        retry_ids[0]
    )
    assert project.state_json["ai_video_retry_history"][-1][
        "legacy_same_task_identity_migration"
    ] is True
    assert project.state_json["ai_video_segment_retry_counts"]["1:2"] == 1
    assert published[0]["kwargs"]["local_task_id"] == retry_ids[0]


def test_content_factory_retry_preserves_durable_quota_exclusions():
    source = Path(content_factory_tasks_module.__file__).read_text(encoding="utf-8")
    retry = source[
        source.index("def _retry_failed_video_segments")
        : source.index("def _queue_next_variant_after_video_submit")
    ]

    collect = retry.index("quota_failed_key_ids = {")
    route = retry.index("cycle_key = resolve_video_model_key", collect)
    exclude = retry.index("exclude_key_ids=quota_failed_key_ids", route)
    persist = retry.index(
        "provider_quota_failed_key_ids=sorted(quota_failed_key_ids)",
        exclude,
    )

    assert collect < route < exclude < persist


def test_missing_complete_video_repair_preserves_approved_director_boundary():
    source = Path(content_factory_tasks_module.__file__).read_text(encoding="utf-8")
    repair = source[
        source.index("def _queue_missing_serial_video_variant_if_needed")
        : source.index("def _configured_video_duration_range")
    ]

    assert 'repair_stage_name = "VISUAL_PREVIEW"' in repair
    assert '_create_repair_stage(\n        db,\n        project,\n        repair_stage_name,' in repair
    assert 'project.current_stage = repair_stage_name' in repair
    assert 'stage:DIRECTOR' not in repair


def test_missing_variant_gate_adopts_paid_execution_ledger_before_resubmit(
    db_session,
    monkeypatch,
):
    project = HermesContentFactoryProject(
        project_key="cf_paid_ledger_adoption",
        workspace_id=3,
        user_id=6,
        title="Paid ledger adoption",
        product_name="",
        market="US",
        status="generating_video",
        current_stage="WAITING_VIDEO_INPUT",
        config_json={"video_count": 2, "auto_run": True},
        state_json={
            "video_variant_pipeline": {
                "mode": "bounded_api_parallel_v1",
                "target_count": 2,
                "active_index": 2,
                "submitted_indices": [1, 2],
                "completed_indices": [1],
                "failed_indices": [2],
            },
            "ai_video_groups": [],
            "ai_video_task_ids": [],
        },
    )
    db_session.add(project)
    db_session.flush()
    recovered_group = {
        "video_index": 2,
        "segments": [{"segment_index": 1, "task_id": 902}],
        "recovered_from_execution_ledger": True,
    }
    waits: list[dict] = []
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_sequential_variants_enabled",
        lambda _project: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_target_video_count",
        lambda _project: 2,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_completed_video_assets_by_index",
        lambda _db, _project: {1: object()},
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_variant_rollout_authorized",
        lambda _project, _index: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_recover_variant_media_group_from_execution_ledger",
        lambda _db, *, project, variant_index: recovered_group,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_ledger_group_still_owns_missing_variant",
        lambda _db, *, project, group: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_schedule_video_wait",
        lambda _db, _project, **kwargs: waits.append(kwargs) or "wait-902",
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_create_repair_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paid provider work must not be resubmitted")
        ),
    )

    stage = content_factory_tasks_module._queue_missing_serial_video_variant_if_needed(
        db_session,
        project,
        reason="completion gate",
    )

    assert stage is None
    assert project.state_json["ai_video_task_ids"] == [902]
    assert project.state_json["ai_video_groups"] == [recovered_group]
    assert project.state_json["video_variant_pipeline"]["failed_indices"] == []
    assert waits and waits[0]["countdown"] == 5


def test_terminal_failed_execution_ledger_group_does_not_block_missing_variant(
    monkeypatch,
):
    project = SimpleNamespace(id=186, workspace_id=3, user_id=6)
    failed_task = SimpleNamespace(id=3564, state="failed")
    group = {
        "video_index": 4,
        "source_stage_id": 3190,
        "segments": [{"segment_index": 1, "task_id": 3564}],
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_media_group_source_is_superseded",
        lambda _db, _project, _group: False,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_scoped_content_video_tasks",
        lambda _db, _project, task_ids: (
            [failed_task] if task_ids == [3564] else []
        ),
    )

    assert not content_factory_tasks_module._ledger_group_still_owns_missing_variant(
        object(),
        project=project,
        group=group,
    )


def test_live_execution_ledger_group_still_blocks_duplicate_submission(
    monkeypatch,
):
    project = SimpleNamespace(id=186, workspace_id=3, user_id=6)
    pending_task = SimpleNamespace(id=3581, state="generating")
    group = {
        "video_index": 4,
        "source_stage_id": 3220,
        "segments": [{"segment_index": 1, "task_id": 3581}],
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_media_group_source_is_superseded",
        lambda _db, _project, _group: False,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_scoped_content_video_tasks",
        lambda _db, _project, task_ids: (
            [pending_task] if task_ids == [3581] else []
        ),
    )

    assert content_factory_tasks_module._ledger_group_still_owns_missing_variant(
        object(),
        project=project,
        group=group,
    )


def test_superseded_execution_ledger_group_cannot_reclaim_missing_variant(
    monkeypatch,
):
    project = SimpleNamespace(id=186, workspace_id=3, user_id=6)
    group = {
        "video_index": 9,
        "source_stage_id": 3207,
        "segments": [{"segment_index": 1, "task_id": 3574}],
    }
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_media_group_source_is_superseded",
        lambda _db, _project, _group: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_scoped_content_video_tasks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("superseded work must not be recovered")
        ),
    )

    assert not content_factory_tasks_module._ledger_group_still_owns_missing_variant(
        object(),
        project=project,
        group=group,
    )


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
            "video_model": "omni_flash",
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
            "content_factory_dependency_task_id": 1002,
            "content_factory_requires_previous_segment_frame": True,
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
            "content_factory_dependency_task_id": 1001,
            "content_factory_requires_previous_segment_frame": True,
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
        content_factory_tasks_module.submit_and_poll_ai_video_task,
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
        "queue": "gmv.tasks.ai_video.api",
    }]


def test_dependency_release_is_not_immediately_published_twice(monkeypatch):
    submissions: list[dict] = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
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
        content_factory_tasks_module.submit_and_poll_ai_video_task,
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
        content_factory_tasks_module.submit_and_poll_ai_video_task,
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
                "poll_heartbeat_at": (
                    datetime.now(timezone.utc)
                    - timedelta(
                        seconds=int(
                            content_factory_tasks_module.settings.SUB2API_HTTP_TIMEOUT_SECONDS
                        )
                        + 31
                    )
                ).isoformat(),
            }
        },
    )

    assert _enqueue_queued_local_video_tasks(project, [abandoned]) == [81]
    assert submissions[0]["kwargs"]["local_task_id"] == 81


def test_local_content_factory_segment_keeps_owner_during_sub2api_submit_timeout(
    monkeypatch,
):
    submissions: list[dict] = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: submissions.append(kwargs),
    )
    project = SimpleNamespace(workspace_id=3)
    in_flight = SimpleNamespace(
        id=81,
        state="queued_local",
        result_json={
            "__local": {
                "poll_owner_task_id": "sub2api-submit-owner",
                "poll_heartbeat_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=120)
                ).isoformat(),
            }
        },
    )

    assert _enqueue_queued_local_video_tasks(project, [in_flight]) == []
    assert submissions == []


def test_local_content_factory_segment_with_fresh_publish_lease_is_not_reenqueued(monkeypatch):
    submissions: list[dict] = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
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
        content_factory_tasks_module.submit_and_poll_ai_video_task,
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

    old_lane_message = "视频供应商暂时不可用；稍后重试。"
    project = SimpleNamespace(
        id=168,
        state_json={
            "ai_video_wait_task_id": "prior-waiter",
            "ai_video_lane_status_message": old_lane_message,
            "ai_video_lane_status_updated_at": "2026-08-05T01:00:00",
            "ai_video_provider_cooldown_until": "2026-08-05T01:03:00",
        },
        last_error=old_lane_message,
    )
    db = Db()

    assert _schedule_video_wait(db, project, countdown=20, reason="test") == "next-waiter"
    assert submissions == [{
        "kwargs": {"project_id": 168, "predecessor_wait_id": "prior-waiter"},
        "countdown": 20,
        "queue": "gmv.tasks.hermes_agent",
    }]
    assert project.state_json["ai_video_wait_task_id"] == "next-waiter"
    assert "ai_video_lane_status_message" not in project.state_json
    assert "ai_video_provider_cooldown_until" not in project.state_json
    assert project.last_error is None
    assert db.commits == 1


def test_content_video_waiter_uses_mysql_project_advisory_lock():
    calls: list[tuple[str, dict]] = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    class Connection:
        def __init__(self):
            self.closed = False

        def execute(self, statement, params):
            calls.append((str(statement), params))
            return Result(1)

        def close(self):
            self.closed = True

    connection = Connection()

    class Bind:
        dialect = SimpleNamespace(name="mysql")

        def connect(self):
            return connection

    class Db:
        def get_bind(self):
            return Bind()

    db = Db()
    acquired, lock_connection = content_factory_tasks_module._acquire_content_video_wait_lock(
        db,
        project_id=183,
    )
    assert acquired is True
    assert lock_connection is connection
    content_factory_tasks_module._release_content_video_wait_lock(
        lock_connection,
        project_id=183,
    )

    assert calls == [
        (
            "SELECT GET_LOCK(:lock_name, 0)",
            {"lock_name": "gmv:content-video-wait:183"},
        ),
        (
            "SELECT RELEASE_LOCK(:lock_name)",
            {"lock_name": "gmv:content-video-wait:183"},
        ),
    ]
    assert connection.closed is True


def test_content_video_waiter_skips_mysql_lock_when_another_writer_owns_it():
    class Result:
        def scalar(self):
            return 0

    class Connection:
        def __init__(self):
            self.closed = False

        def execute(self, _statement, _params):
            return Result()

        def close(self):
            self.closed = True

    connection = Connection()

    class Bind:
        dialect = SimpleNamespace(name="mysql")

        def connect(self):
            return connection

    class Db:
        def get_bind(self):
            return Bind()

    acquired, lock_connection = content_factory_tasks_module._acquire_content_video_wait_lock(
        Db(),
        project_id=183,
    )
    assert acquired is False
    assert lock_connection is None
    assert connection.closed is True


def test_content_video_waiter_skips_mysql_lock_for_sqlite_without_connection():
    class Db:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    acquired, lock_connection = content_factory_tasks_module._acquire_content_video_wait_lock(
        Db(),
        project_id=183,
    )
    assert acquired is True
    assert lock_connection is None


def test_content_video_waiter_lock_owner_probe_distinguishes_live_mysql_owner():
    class Result:
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    calls: list[tuple[str, dict]] = []

    class Db:
        def __init__(self, owner_id):
            self.owner_id = owner_id

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        def execute(self, statement, params):
            calls.append((str(statement), params))
            return Result(self.owner_id)

    assert content_factory_tasks_module._content_video_wait_lock_is_owned(
        Db(103820),
        project_id=183,
    ) is True
    assert content_factory_tasks_module._content_video_wait_lock_is_owned(
        Db(None),
        project_id=183,
    ) is False
    assert calls == [
        (
            "SELECT IS_USED_LOCK(:lock_name)",
            {"lock_name": "gmv:content-video-wait:183"},
        ),
        (
            "SELECT IS_USED_LOCK(:lock_name)",
            {"lock_name": "gmv:content-video-wait:183"},
        ),
    ]


def test_video_wait_self_heal_does_not_publish_while_live_waiter_owns_lock(
    monkeypatch,
):
    lock_probes: list[int] = []

    def fake_lock_owner(_db, *, project_id):
        lock_probes.append(int(project_id))
        return True

    monkeypatch.setattr(
        content_factory_tasks_module,
        "_content_video_wait_lock_is_owned",
        fake_lock_owner,
    )

    assert content_factory_tasks_module._video_wait_recovery_publish_allowed(
        object(),
        project_id=187,
        heartbeat_stale=True,
        all_terminal=False,
        wait_task_missing=False,
    ) is False
    assert lock_probes == [187]


def test_video_wait_self_heal_publishes_only_when_recovery_is_needed_and_unowned(
    monkeypatch,
):
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_content_video_wait_lock_is_owned",
        lambda _db, *, project_id: False,
    )

    assert content_factory_tasks_module._video_wait_recovery_publish_allowed(
        object(),
        project_id=187,
        heartbeat_stale=False,
        all_terminal=True,
        wait_task_missing=True,
    ) is True
    assert content_factory_tasks_module._video_wait_recovery_publish_allowed(
        object(),
        project_id=187,
        heartbeat_stale=False,
        all_terminal=False,
        wait_task_missing=True,
    ) is False


def test_completed_project_rejects_delayed_video_waiter_before_local_work():
    assert content_factory_tasks_module._content_video_wait_project_is_complete(
        SimpleNamespace(status="complete", current_stage="COMPLETE")
    ) is True
    assert content_factory_tasks_module._content_video_wait_project_is_complete(
        SimpleNamespace(status="generating_video", current_stage="WAITING_VIDEO_INPUT")
    ) is False


def test_serial_video_progress_does_not_hide_browser_login_instruction():
    project = SimpleNamespace(
        status="waiting_bridge",
        last_error="Sign in to ChatGPT; Hermes will resume automatically.",
    )

    _set_serial_variant_progress_message(
        project,
        completed_count=3,
        target_count=6,
        guide_count=3,
    )

    assert project.last_error == (
        "Sign in to ChatGPT; Hermes will resume automatically."
    )


def test_serial_video_progress_updates_nonblocked_project():
    project = SimpleNamespace(status="generating_video", last_error=None)

    _set_serial_variant_progress_message(
        project,
        completed_count=3,
        target_count=6,
        guide_count=3,
    )

    assert "3/6" in project.last_error
    assert "3 个对应剪辑发布指导" in project.last_error


def test_video_state_merge_refresh_preserves_new_control_pointer(db_session):
    project = HermesContentFactoryProject(
        project_key="cf-video-state-merge",
        workspace_id=1,
        user_id=None,
        title="Concurrent state merge",
        product_name="",
        status="generating_video",
        current_stage="PRODUCTION_PLAN",
        config_json={},
        state_json={"ai_video_task_ids": [91]},
    )
    db_session.add(project)
    db_session.commit()
    stale_project = db_session.get(HermesContentFactoryProject, project.id)
    assert "approved_production_plan" not in dict(
        stale_project.state_json or {}
    )

    Writer = sessionmaker(bind=db_session.bind, expire_on_commit=False)
    with Writer() as writer:
        current = writer.get(HermesContentFactoryProject, project.id)
        pointer = {
            "variant_index": 4,
            "plan_sha256": "a" * 64,
            "production_plan_stage_id": 42,
            "audit_row_id": 7,
        }
        current.state_json = {
            **dict(current.state_json or {}),
            "approved_production_plan": pointer,
            "approved_production_plans_by_variant": {"4": pointer},
            "pause_reason_code": "manual",
            "manual_paused_at": "2026-07-24T13:17:00",
        }
        current.config_json = {"manual_paused": True}
        current.status = "paused"
        writer.commit()

    refreshed = _lock_project_for_video_state_merge(
        db_session,
        stale_project,
    )
    merged = dict(refreshed.state_json or {})
    merged["ai_video_pending_task_ids"] = [91]
    refreshed.state_json = merged
    db_session.commit()
    db_session.refresh(refreshed)

    assert refreshed.state_json["approved_production_plan"][
        "plan_sha256"
    ] == "a" * 64
    assert refreshed.state_json["ai_video_pending_task_ids"] == [91]
    assert refreshed.state_json["pause_reason_code"] == "manual"
    assert refreshed.config_json["manual_paused"] is True
    assert refreshed.status == "paused"


def test_control_stage_commit_preserves_concurrent_video_repair_state(db_session):
    project = HermesContentFactoryProject(
        project_key="cf-control-video-state-merge",
        workspace_id=1,
        user_id=None,
        title="Concurrent control and video state merge",
        product_name="",
        status="running",
        current_stage="FINAL_ASSETS",
        config_json={},
        state_json={
            "creative_marker": "before",
            "ai_video_task_ids": [3811, 3815],
            "ai_video_groups": [{
                "video_index": 2,
                "segments": [
                    {"segment_index": 1, "task_id": 3815},
                    {"segment_index": 2, "task_id": 3811},
                ],
            }],
        },
    )
    db_session.add(project)
    db_session.commit()
    stale_project = db_session.get(HermesContentFactoryProject, project.id)
    stale_state = dict(stale_project.state_json or {})

    Writer = sessionmaker(bind=db_session.bind, expire_on_commit=False)
    with Writer() as writer:
        current = writer.get(HermesContentFactoryProject, project.id)
        current.state_json = {
            **dict(current.state_json or {}),
            "ai_video_task_ids": [3816, 3817],
            "ai_video_groups": [{
                "video_index": 2,
                "segments": [
                    {
                        "segment_index": 1,
                        "task_id": 3816,
                        "retry_source_task_id": 3815,
                    },
                    {
                        "segment_index": 2,
                        "task_id": 3817,
                        "retry_source_task_id": 3811,
                    },
                ],
            }],
            "ai_video_last_retry_at": "2026-08-11T02:56:47",
        }
        writer.commit()

    stale_project.state_json = {
        **stale_state,
        "creative_marker": "final-assets-success",
    }
    content_factory_tasks_module._merge_latest_video_runtime_before_control_commit(
        db_session,
        stale_project,
    )
    db_session.commit()
    db_session.refresh(stale_project)

    merged = dict(stale_project.state_json or {})
    assert merged["creative_marker"] == "final-assets-success"
    assert merged["ai_video_task_ids"] == [3816, 3817]
    assert [
        row["task_id"]
        for row in merged["ai_video_groups"][0]["segments"]
    ] == [3816, 3817]
    assert merged["ai_video_last_retry_at"] == "2026-08-11T02:56:47"


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


def test_terminal_failed_variant_blocks_new_ordinal_admission_until_replenished():
    project = SimpleNamespace(
        config_json={
            "video_count": 6,
            "max_api_video_variants_in_flight": 2,
        },
        state_json={
            "video_variant_pipeline": {
                "target_count": 6,
                "active_index": 5,
                "submitted_indices": [1, 2, 3, 4, 5],
                "completed_indices": [1, 2, 3],
                "failed_indices": [],
            },
            "ai_video_groups": [
                {"video_index": 4, "segments": [{"task_id": 401}]},
                {"video_index": 5, "segments": [{"task_id": 501}]},
            ],
            "ai_video_group_statuses": [
                {"video_index": 4, "status": "failed"},
                {"video_index": 5, "status": "pending"},
            ],
        },
    )

    assert _terminal_failed_video_variant_indices(project) == {4}

    class _NoQueryDB:
        def query(self, *_args, **_kwargs):
            raise AssertionError("failed-hole guard must stop before DB scheduling")

    assert content_factory_tasks_module._queue_next_unsubmitted_serial_variant_if_needed(
        _NoQueryDB(),
        project,
        reason="test failed-hole priority",
    ) is None


def test_terminal_failed_group_is_retired_without_overwriting_new_control_stage():
    project = SimpleNamespace(
        current_stage="PRODUCTION_PLAN",
        status="running",
        last_error=None,
        state_json={
            "ai_video_task_ids": [101, 102, 201],
            "ai_video_pending_task_ids": [201],
            "ai_video_failed_task_ids": [101],
            "ai_video_wait_task_id": "old-waiter",
            "ai_video_terminal_failure": "old variant failed",
            "ai_video_groups": [
                {
                    "video_index": 1,
                    "segments": [{"task_id": 101}, {"task_id": 102}],
                },
                {
                    "video_index": 2,
                    "segments": [{"task_id": 201}],
                },
            ],
            "ai_video_group_statuses": [
                {
                    "video_index": 1,
                    "status": "failed",
                    "task_ids": [101, 102],
                    "failed_task_ids": [101],
                },
                {
                    "video_index": 2,
                    "status": "pending",
                    "task_ids": [201],
                },
            ],
            "video_variant_pipeline": {
                "target_count": 3,
                "active_index": 2,
                "submitted_indices": [1],
                "completed_indices": [],
                "failed_indices": [],
            },
        },
    )

    retired = _retire_terminal_failed_video_groups_from_active_ledger(
        project,
        failed_task_ids=[101],
    )

    assert retired == {
        "video_indices": [1],
        "task_ids": [101, 102],
        "remaining_task_ids": [201],
    }
    assert project.current_stage == "PRODUCTION_PLAN"
    assert project.status == "running"
    assert project.last_error is None
    assert project.state_json["ai_video_task_ids"] == [201]
    assert [
        item["video_index"]
        for item in project.state_json["ai_video_groups"]
    ] == [2]
    assert project.state_json["ai_video_group_statuses"][0]["status"] == "failed"
    assert project.state_json["video_variant_pipeline"]["submitted_indices"] == [1]
    assert "ai_video_terminal_failure" not in project.state_json


def test_transient_provider_outage_waits_for_next_round_without_rewriting_creative():
    now = datetime(2026, 7, 26, 5, 0, 0)
    failed_at = now - timedelta(seconds=60)
    task = SimpleNamespace(
        fail_code="bandianwa_worker_error",
        fail_msg="Sub2API Flow HTTP 503: Service temporarily unavailable",
        input_json={
            "content_factory_video_index": 6,
            "content_factory_segment_index": 4,
        },
        result_json={},
        state="failed",
        updated_at=failed_at,
        external_complete_time=None,
        created_at=failed_at,
    )
    project = SimpleNamespace(
        config_json={
            "content_factory_video_provider_cycle_cooldown_seconds": 180,
            "content_factory_video_exhausted_recovery_limit": 5,
        },
        state_json={
            "ai_video_exhausted_cooldown_retry_generations": {
                "6:4": {"round": 1, "round_limit": 5},
            },
        },
    )

    assert _content_factory_transient_retry_wait_seconds(
        project,
        [task],
        now=now,
    ) == 120


def test_elapsed_provider_cooldown_does_not_create_a_permanent_thirty_second_loop():
    now = datetime(2026, 7, 26, 5, 5, 0)
    failed_at = now - timedelta(seconds=181)
    task = SimpleNamespace(
        fail_code="bandianwa_worker_error",
        fail_msg="Sub2API Flow HTTP 503: Service temporarily unavailable",
        input_json={
            "content_factory_video_index": 6,
            "content_factory_segment_index": 4,
        },
        result_json={},
        state="failed",
        updated_at=failed_at,
        external_complete_time=None,
        created_at=failed_at,
    )
    project = SimpleNamespace(
        config_json={
            "content_factory_video_provider_cycle_cooldown_seconds": 180,
            "content_factory_video_exhausted_recovery_limit": 5,
        },
        state_json={
            "ai_video_exhausted_cooldown_retry_generations": {
                "6:4": {"round": 1, "round_limit": 5},
            },
        },
    )

    assert _content_factory_transient_retry_wait_seconds(
        project,
        [task],
        now=now,
    ) is None


def test_latest_local_prompt_compile_failure_supersedes_historical_provider_503():
    now = datetime(2026, 8, 8, 10, 42, 22)
    task = SimpleNamespace(
        fail_code="bandianwa_worker_error",
        fail_msg="Sub2API Flow HTTP 503: Service temporarily unavailable",
        input_json={
            "content_factory_video_index": 4,
            "content_factory_segment_index": 1,
        },
        result_json={
            "__local": {
                "content_factory_retry_prompt_compile_error": (
                    "structured provider prompt is not semantically lossless"
                ),
            },
        },
        state="failed",
        updated_at=now - timedelta(minutes=5),
        external_complete_time=None,
        created_at=now - timedelta(minutes=10),
    )
    project = SimpleNamespace(
        config_json={
            "content_factory_video_provider_cycle_cooldown_seconds": 180,
        },
        state_json={},
    )

    assert _content_factory_local_transport_rebuild_failure(task) is True
    assert _content_factory_transient_retry_wait_seconds(
        project,
        [task],
        now=now,
    ) is None


def test_local_provider_prompt_contract_error_never_enters_provider_cooldown():
    now = datetime(2026, 8, 5, 14, 15, 48)
    task = SimpleNamespace(
        fail_code="content_provider_prompt_contract_invalid",
        fail_msg="provider prompt changed after semantic validation",
        input_json={
            "content_factory_video_index": 1,
            "content_factory_segment_index": 1,
        },
        result_json={},
        state="failed",
        updated_at=now,
        external_complete_time=None,
        created_at=now,
    )
    project = SimpleNamespace(
        config_json={
            "content_factory_video_provider_cycle_cooldown_seconds": 180,
        },
        state_json={},
    )

    assert _content_factory_local_transport_rebuild_failure(task) is True
    assert _content_factory_transient_retry_wait_seconds(
        project,
        [task],
        now=now,
    ) is None


def test_flow_request_rejection_reauthors_without_account_cooldown():
    now = datetime(2026, 8, 5, 23, 40, 0)
    task = SimpleNamespace(
        fail_code="flow_request_rejected",
        fail_msg=(
            "Sub2API Flow HTTP 400: Request contains an invalid argument."
        ),
        input_json={
            "content_factory_video_index": 3,
            "content_factory_segment_index": 1,
        },
        result_json={},
        state="failed",
        updated_at=now,
        external_complete_time=None,
        created_at=now,
    )
    project = SimpleNamespace(
        config_json={
            "content_factory_video_provider_cycle_cooldown_seconds": 180,
        },
        state_json={},
    )

    assert _content_factory_retryable_video_failure(task) is True
    assert _content_factory_local_transport_rebuild_failure(task) is True
    assert _content_factory_transient_retry_wait_seconds(
        project,
        [task],
        now=now,
    ) is None


def test_video_wait_reconciles_downloaded_siblings_before_provider_recovery():
    source = inspect.getsource(
        content_factory_tasks_module.wait_for_content_factory_videos
    )

    compose_offset = source.index(
        "final_assets = _compose_segmented_videos(db, project, tasks)"
    )
    retry_offset = source.index("_retry_failed_video_segments(")
    cooldown_offset = source.index(
        "_content_factory_transient_retry_wait_seconds("
    )

    assert compose_offset < retry_offset
    assert compose_offset < cooldown_offset
    assert "completed_during_recovery" in source
    assert "queued_parallel_variant_stage_id" in source


def test_recoverable_failed_group_stays_owned_by_hermes_during_retry():
    source = inspect.getsource(
        content_factory_tasks_module._compose_segmented_videos
    )

    assert "recoverable_failures" in source
    assert 'status = "pending" if recoverable_failures else "failed"' in source
    assert '"recoverable_failed_task_ids"' in source
    decision_offset = source.index(
        'status = "pending" if recoverable_failures else "failed"'
    )
    assert decision_offset < source.index(
        "_delete_editor_guidance_for_video_index",
        decision_offset,
    )


def test_exhausted_provider_recovery_rounds_are_bounded_but_not_one_shot():
    now = datetime(2026, 7, 26, 5, 0, 0)
    failed_at = now - timedelta(seconds=300)
    task = SimpleNamespace(
        fail_code="bandianwa_worker_error",
        fail_msg="Sub2API Flow HTTP 502: Upstream service temporarily unavailable",
        input_json={},
        result_json={},
        updated_at=failed_at,
        external_complete_time=None,
        created_at=failed_at,
    )

    assert _should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="6:4",
        effective_count=3,
        retry_limit=2,
        recovery_generations={"6:4": {"round": 1}},
        recovery_limit=5,
        now=now,
        cooldown_seconds=180,
    ) is True


    retry_source = inspect.getsource(
        content_factory_tasks_module._retry_failed_video_segments
    )
    record_generation = retry_source[
        retry_source.index("if exhausted_cooldown_retry:")
        : retry_source.index("recovery_generations[retry_key]", retry_source.index("if exhausted_cooldown_retry:"))
    ]
    assert 'prior_generation.get("policy_version")' in record_generation
    assert "prior_policy in {0, SELF_HEAL_POLICY_VERSION}" in record_generation
    assert _should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="6:4",
        effective_count=3,
        retry_limit=2,
        recovery_generations={"6:4": {"round": 5}},
        recovery_limit=5,
        now=now,
        cooldown_seconds=180,
    ) is False

    # A source-level recovery-policy fix starts a fresh bounded generation.
    # Old rounds were spent under different routing semantics and must not
    # permanently suppress the corrected self-heal path.
    assert _should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="6:4",
        effective_count=3,
        retry_limit=2,
        recovery_generations={
            "6:4": {
                "round": 5,
                "policy_version": (
                    content_factory_tasks_module.SELF_HEAL_POLICY_VERSION - 1
                ),
            }
        },
        recovery_limit=5,
        now=now,
        cooldown_seconds=180,
    ) is True


def test_doubao_text_only_response_has_separate_bounded_account_rotation_budget():
    source = inspect.getsource(
        content_factory_tasks_module._retry_failed_video_segments
    )

    assert "content_factory_video_account_rotation_retry_limit" in source
    assert "ai_video_account_rotation_retry_counts" in source
    assert "account_rotation_count < account_rotation_retry_limit" in source
    assert "and not account_rotation_retry_allowed" in source


def test_doubao_text_only_response_rotates_after_provider_round_budget_is_exhausted(
    db_session,
    monkeypatch,
):
    key = KieApiKey(
        name="doubao-account-rotation-route",
        provider_key="doubao",
        api_key_ciphertext="test-only",
        is_active=True,
        is_default=False,
        scopes_json=["video:generate", "video:seedance_2_0_mini"],
        model_priorities_json={"seedance_2_0_mini": 1},
    )
    db_session.add(key)
    db_session.flush()
    failed = KieTask(
        workspace_id=3,
        key_id=int(key.id),
        created_by_user_id=None,
        model="seedance_2_0_mini",
        task_id="doubao-text-only-exhausted-provider-rounds",
        state="failed",
        fail_code="doubao_text_only_response",
        fail_msg="conversation responded without creating a video task",
        input_json={
            "model": "seedance_2_0_mini",
            "prompt": "Create the approved current segment.",
            "seconds": 6,
            "aspect_ratio": "9:16",
            "content_factory_project_id": 185,
            "content_factory_project_key": "cf-rotation-185",
            "content_factory_video_index": 2,
            "content_factory_segment_index": 1,
            "content_factory_final_name_base": "video-002",
            "content_factory_prompt_policy_version": (
                content_factory_tasks_module.VIDEO_PROMPT_POLICY_VERSION
            ),
        },
        result_json={"__local": {}},
    )
    db_session.add(failed)
    db_session.flush()
    failed_id = int(failed.id)
    project = SimpleNamespace(
        id=185,
        workspace_id=3,
        user_id=None,
        project_key="cf-rotation-185",
        config_json={
            "content_factory_video_retry_limit": 2,
            "content_factory_video_exhausted_recovery_limit": 5,
        },
        state_json={
            "ai_video_task_ids": [failed_id],
            "ai_video_segment_retry_counts": {"2:1": 7},
            "ai_video_exhausted_cooldown_retry_generations": {
                "2:1": {
                    "round": 5,
                    "policy_version": (
                        content_factory_tasks_module.SELF_HEAL_POLICY_VERSION
                    ),
                },
            },
            "ai_video_groups": [{
                "video_index": 2,
                "provider_retry_budget_per_segment": 2,
                "segments": [{
                    "task_id": failed_id,
                    "segment_index": 1,
                    "dependency_task_id": None,
                }],
            }],
        },
        status="failed",
        last_error="text-only response",
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "resolve_video_model_key",
        lambda *_args, **_kwargs: key,
    )
    published = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    retry_ids = _retry_failed_video_segments(
        db_session,
        project,
        [failed],
        allow_exhausted_cooldown_retry=True,
    )

    assert len(retry_ids) == 1
    assert retry_ids[0] != failed_id
    assert project.state_json["ai_video_account_rotation_retry_counts"] == {
        "2:1": 1,
    }
    assert project.state_json["ai_video_retry_history"][-1][
        "account_rotation_retry"
    ] is True
    assert published[0]["kwargs"]["local_task_id"] == retry_ids[0]


def test_multimodal_quality_repair_has_budget_after_transport_retries_are_exhausted(
    db_session,
    monkeypatch,
):
    key = KieApiKey(
        name="quality-repair-route",
        provider_key="doubao",
        api_key_ciphertext="test-only",
        is_active=True,
        is_default=False,
        scopes_json=["video:generate", "video:seedance_2_0_mini"],
        model_priorities_json={"seedance_2_0_mini": 1},
    )
    db_session.add(key)
    db_session.flush()
    failed = KieTask(
        workspace_id=3,
        key_id=int(key.id),
        created_by_user_id=None,
        model="seedance_2_0_mini",
        task_id="quality-failed-after-many-provider-attempts",
        state="failed",
        fail_code=content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE,
        fail_msg="final multimodal review found a cast and medium switch",
        input_json={
            "model": "seedance_2_0_mini",
            "prompt": "Create the approved current segment.",
            "seconds": 6,
            "aspect_ratio": "9:16",
            "content_factory_project_id": 185,
            "content_factory_project_key": "cf-quality-185",
            "content_factory_video_index": 2,
            "content_factory_segment_index": 2,
            "content_factory_final_name_base": "video-002",
            "content_factory_prompt_policy_version": (
                content_factory_tasks_module.VIDEO_PROMPT_POLICY_VERSION
            ),
        },
        result_json={
            "__local": {
                "segment_execution_review": {
                    "policy_version": (
                        content_factory_tasks_module
                        .SEGMENT_EXECUTION_VIDEO_REVIEW_POLICY_VERSION
                    ),
                    "blocking": True,
                    "blocking_reasons": ["cast and visual medium changed"],
                }
            }
        },
    )
    db_session.add(failed)
    db_session.flush()
    failed_id = int(failed.id)
    project = SimpleNamespace(
        id=185,
        workspace_id=3,
        user_id=None,
        project_key="cf-quality-185",
        config_json={
            "video_model": "seedance_2_0_mini",
            "content_factory_video_retry_limit": 2,
            "content_factory_video_quality_retry_limit": 2,
        },
        state_json={
            "ai_video_task_ids": [failed_id],
            "ai_video_segment_retry_counts": {"2:2": 8},
            "ai_video_groups": [{
                "video_index": 2,
                "provider_retry_budget_per_segment": 2,
                "segments": [{
                    "task_id": failed_id,
                    "segment_index": 2,
                    "dependency_task_id": None,
                }],
            }],
        },
        status="failed",
        last_error="quality failed",
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "resolve_video_model_key",
        lambda *_args, **_kwargs: key,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_normalize_omni_retry_references",
        lambda retry_input, retry_files, *_args, **_kwargs: (
            retry_input,
            retry_files,
        ),
    )
    replans = []
    monkeypatch.setattr(
        content_factory_tasks_module,
        "replan_failed_segment_execution_api",
        lambda *_args, **kwargs: replans.append(kwargs) or {
            "policy_version": "test-replan-v1",
            "segment_index": 2,
            "duration_seconds": 6,
            "segment_goal": "Preserve the same animated creator.",
            "timeline": [{
                "start_second": 0,
                "end_second": 6,
                "action": "The same animated creator applies the balm.",
                "provider_action_en": "Same animated creator applies balm.",
                "provider_action_zh": "同一动画女性涂抹膏体。",
            }],
            "pacing": "Fast three-beat rhythm.",
            "camera_direction": "Macro to locked medium.",
            "provider_direction_en": (
                "Three rapid cuts; macro to locked medium; stylized 2D animation."
            ),
            "provider_direction_zh": "三次快切；微距转固定中景；2D动画风格。",
            "keep_reference_aliases": [],
        },
    )
    published = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    retry_ids = _retry_failed_video_segments(db_session, project, [failed])

    assert len(replans) == 1
    assert len(retry_ids) == 1
    assert project.state_json["ai_video_segment_retry_counts"]["2:2"] == 8
    quality_counts = project.state_json[
        "ai_video_segment_quality_retry_counts"
    ]
    assert list(quality_counts.values()) == [1]
    assert project.state_json["ai_video_retry_history"][-1][
        "budget_kind"
    ] == "quality"
    retry_task = db_session.get(KieTask, retry_ids[0])
    assert retry_task.result_json["__local"]["content_factory_quality_retry"] == 1
    assert published[0]["kwargs"]["local_task_id"] == retry_ids[0]


def test_final_composite_repair_has_one_incident_budget_after_segment_qa_exhausted():
    review = {
        "policy_version": (
            content_factory_tasks_module.FINAL_INTENT_REVIEW_POLICY_VERSION
        ),
        "repair_scope": "segment_regeneration",
        "segment_index": 2,
        "affected_segment_indices": [2],
        "repair_instruction": (
            "Regenerate segment 2 with the exact recurring creator identity."
        ),
        "blocking_reasons": ["The creator changes across the segment boundary."],
    }
    existing_counts = {
        "2:2:segment_execution_qa:2026-08-05-segment-evidence-continuity-v4": 2,
    }

    enabled, budget_key, retry_limit, current_count = (
        content_factory_tasks_module._final_intent_segment_repair_budget(
            retry_key="2:2",
            normalized_failure_code=(
                content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE
            ),
            failed_meta={"final_intent_qa_failure": review},
            quality_retry_counts=existing_counts,
            config={},
        )
    )

    assert enabled is True
    assert ":final_intent:segment_execution_qa:" in budget_key
    assert retry_limit == 1
    assert current_count == 0

    # Rewording the same final-review defect cannot mint another paid budget.
    review["repair_instruction"] = (
        "Keep the same face, hair, wardrobe and recurring creator in segment 2."
    )
    review["blocking_reasons"] = ["Cross-clip identity continuity failed."]
    enabled_again, same_key, same_limit, spent_count = (
        content_factory_tasks_module._final_intent_segment_repair_budget(
            retry_key="2:2",
            normalized_failure_code=(
                content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE
            ),
            failed_meta={"final_intent_qa_failure": review},
            quality_retry_counts={**existing_counts, budget_key: 1},
            config={},
        )
    )
    assert enabled_again is True
    assert same_key == budget_key
    assert same_limit == 1
    assert spent_count == 1


def test_quality_retry_adopts_live_child_created_by_concurrent_recovery(
    db_session,
    monkeypatch,
):
    key = KieApiKey(
        name="concurrent-quality-route",
        provider_key="doubao",
        api_key_ciphertext="test-only",
        is_active=True,
        is_default=False,
        scopes_json=["video:generate", "video:seedance_2_0_mini"],
        model_priorities_json={"seedance_2_0_mini": 1},
    )
    db_session.add(key)
    db_session.flush()
    failed = KieTask(
        workspace_id=3,
        key_id=int(key.id),
        created_by_user_id=None,
        model="seedance_2_0_mini",
        task_id="failed-parent-for-concurrent-recovery",
        state="failed",
        fail_code=content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE,
        fail_msg="Effectiveness transfer is insufficient.",
        input_json={
            "model": "seedance_2_0_mini",
            "content_factory_project_id": 185,
            "content_factory_video_index": 2,
            "content_factory_segment_index": 1,
            "content_factory_prompt_policy_version": "legacy-policy",
        },
        result_json={"__local": {}},
    )
    db_session.add(failed)
    db_session.flush()
    live_child = KieTask(
        workspace_id=3,
        key_id=int(key.id),
        created_by_user_id=None,
        model="seedance_2_0_mini",
        task_id="live-child-for-concurrent-recovery",
        state="submitting",
        input_json={
            "model": "seedance_2_0_mini",
            "content_factory_project_id": 185,
            "content_factory_video_index": 2,
            "content_factory_segment_index": 1,
            "content_factory_prompt_policy_version": (
                content_factory_tasks_module.VIDEO_PROMPT_POLICY_VERSION
            ),
        },
        result_json={
            "__local": {
                "content_factory_retry_parent_task_id": int(failed.id),
            }
        },
    )
    db_session.add(live_child)
    db_session.flush()
    project = SimpleNamespace(
        id=185,
        workspace_id=3,
        user_id=None,
        project_key="cf-quality-185",
        config_json={"content_factory_video_quality_retry_limit": 2},
        state_json={
            "ai_video_task_ids": [int(failed.id)],
            "ai_video_groups": [{
                "video_index": 2,
                "segments": [{
                    "task_id": int(failed.id),
                    "segment_index": 1,
                    "dependency_task_id": None,
                }],
            }],
        },
        status="paused",
        last_error="quality failed",
    )
    published = []
    monkeypatch.setattr(
        content_factory_tasks_module.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    retry_ids = _retry_failed_video_segments(db_session, project, [failed])

    assert retry_ids == [int(live_child.id)]
    assert project.state_json["ai_video_task_ids"] == [int(live_child.id)]
    assert project.state_json["ai_video_groups"][0]["segments"][0][
        "retry_adopted_existing_child"
    ] is True
    assert published == []


def test_live_control_stage_protects_project_pointer_from_any_terminal_video_group():
    source = inspect.getsource(
        content_factory_tasks_module.wait_for_content_factory_videos
    )
    terminal = source[source.index("if failed and (not is_segmented") :]

    assert (
        "if is_segmented and _project_has_live_control_stage(db, project):"
        in terminal
    )
    protected = terminal[:terminal.index("project.status = \"failed\"")]
    assert "_retire_terminal_failed_video_groups_from_active_ledger" in protected


def test_new_multimodal_reference_replan_gets_one_fresh_bounded_generation():
    now = datetime(2026, 7, 30, 1, 0, 0)
    failed_at = now - timedelta(seconds=300)
    task = SimpleNamespace(
        fail_code=content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE,
        fail_msg="CONTENT_SEGMENT_EXECUTION_QA_FAILED_TASK_3263",
        input_json={},
        result_json={},
        updated_at=failed_at,
        external_complete_time=None,
        created_at=failed_at,
    )
    legacy_generation = {
        "1:2": {
            "round": 5,
            "round_limit": 5,
            "policy_version": content_factory_tasks_module.SELF_HEAL_POLICY_VERSION,
        }
    }

    assert _should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="1:2",
        effective_count=2,
        retry_limit=2,
        recovery_generations=legacy_generation,
        recovery_limit=5,
        now=now,
        cooldown_seconds=180,
    ) is True

    current_generation = {
        "1:2": {
            **legacy_generation["1:2"],
            "segment_execution_reference_policy_version": (
                content_factory_tasks_module
                .SEGMENT_EXECUTION_REFERENCE_REPLAN_POLICY_VERSION
            ),
        }
    }
    assert _should_retry_exhausted_content_video_after_cooldown(
        task,
        retry_key="1:2",
        effective_count=2,
        retry_limit=2,
        recovery_generations=current_generation,
        recovery_limit=5,
        now=now,
        cooldown_seconds=180,
    ) is False


def test_stale_segment_qa_is_reconciled_from_local_video_without_regeneration(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "already-downloaded.mp4"
    source.write_bytes(b"local-provider-result")
    task = SimpleNamespace(
        id=3264,
        state="failed",
        fail_code=content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE,
        fail_msg="old semantic mismatch",
        result_json={
            "__local": {
                "segment_execution_review": {
                    "policy_version": "old-rigid-plan-gate",
                    "blocking": True,
                },
                "segment_execution_qa_failed_at": "2026-07-30T01:00:00",
            }
        },
    )
    project = SimpleNamespace(id=183)
    writes = []
    db = SimpleNamespace(
        add=lambda value: writes.append(value),
        flush=lambda: None,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_result_video_for_task",
        lambda *_args, **_kwargs: (source, SimpleNamespace(id=12169)),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_review_provider_segment_execution",
        lambda *_args, **_kwargs: {
            "policy_version": (
                content_factory_tasks_module
                .SEGMENT_EXECUTION_VIDEO_REVIEW_POLICY_VERSION
            ),
            "status": "pass",
            "blocking": False,
        },
    )

    assert _reconcile_segment_execution_qa_after_policy_change(
        db,
        project=project,
        task=task,
    ) is True
    assert task.state == "success"
    assert task.fail_code is None
    assert task.fail_msg is None
    assert task.result_json["__local"][
        "segment_execution_qa_policy_reconciled_from"
    ] == "old-rigid-plan-gate"
    assert "segment_execution_qa_failed_at" not in task.result_json["__local"]
    assert writes == [task]


def test_final_intent_failure_is_never_reconciled_by_segment_only_review(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "final-intent-failed.mp4"
    source.write_bytes(b"local-provider-result")
    task = SimpleNamespace(
        id=3439,
        state="failed",
        fail_code=content_factory_tasks_module.SEGMENT_EXECUTION_QA_FAIL_CODE,
        fail_msg="CONTENT_FINAL_INTENT_QA_FAILED exact spoken copy is wrong",
        result_json={
            "__local": {
                "segment_execution_review": {
                    "policy_version": "composition-policy-v3",
                    "blocking": True,
                },
                "final_intent_qa_failure": {
                    "policy_version": "composition-policy-v3",
                    "blocking": True,
                },
            }
        },
    )
    reviewed = []
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_result_video_for_task",
        lambda *_args, **_kwargs: (source, SimpleNamespace(id=5815)),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_review_provider_segment_execution",
        lambda *_args, **_kwargs: reviewed.append(True),
    )

    assert _reconcile_segment_execution_qa_after_policy_change(
        SimpleNamespace(add=lambda _value: None, flush=lambda: None),
        project=SimpleNamespace(id=185),
        task=task,
    ) is False
    assert task.state == "failed"
    assert reviewed == []


def test_final_intent_repair_instruction_survives_retry_prompt_compaction():
    project = SimpleNamespace(
        config_json={"video_model": "seedance_2_0_mini"},
    )
    repair = {
        "policy_version": (
            content_factory_tasks_module
            .SEGMENT_EXECUTION_VIDEO_REVIEW_POLICY_VERSION
        ),
        "final_intent_policy_version": "composition-policy-v3",
        "repair_instruction": (
            "Accurately pronounce MYUPONA and MSM in the exact signed dialogue."
        ),
    }
    params = {
        "model": "seedance_2_0_mini",
        "prompt": (
            "Beats: 0-6s: creator massages shoulder and shows product\n"
            "Dialogue: 'Made with MSM.'\n"
            "9:16; no captions/UI/watermark."
        ),
        "content_factory_base_prompt": "legacy",
        "content_factory_segment_execution_repair": repair,
        "content_factory_reference_manifest": [],
        "content_factory_product_anchor_required": False,
    }

    compacted = content_factory_tasks_module._compact_content_factory_retry_prompt(
        project,
        params,
    )

    assert "Repair:" in compacted["prompt"]
    assert "pronounce MYUPONA and MSM" in compacted["prompt"]
    assert compacted["content_factory_base_prompt"] == compacted["prompt"]


def test_legacy_final_intent_policy_repair_survives_transport_retry_compaction():
    project = SimpleNamespace(
        config_json={"video_model": "seedance_2_0_mini"},
    )
    params = {
        "model": "seedance_2_0_mini",
        "prompt": "Beats: 0-7s: creator catches ball\nDialogue: 'Meet MYUPONA.'",
        "content_factory_base_prompt": "legacy",
        "content_factory_segment_execution_repair": {
            "policy_version": (
                content_factory_tasks_module.FINAL_INTENT_REVIEW_POLICY_VERSION
            ),
            "repair_instruction": "Pronounce MYUPONA exactly and clearly.",
        },
        "content_factory_reference_manifest": [],
        "content_factory_product_anchor_required": False,
    }

    compacted = content_factory_tasks_module._compact_content_factory_retry_prompt(
        project,
        params,
    )

    assert "Repair: Pronounce MYUPONA exactly and clearly." in compacted["prompt"]


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


def test_serial_variant_resume_publishes_prepared_queued_stage_without_lease(
    monkeypatch,
):
    """A prepared future variant is queued but has not been dispatched yet."""

    prepared = SimpleNamespace(
        id=3340,
        stage="CREATIVE_REVIEW",
        status="queued",
        input_json={"variant_index": 2},
        celery_task_id=None,
    )
    queued: list[str] = []

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    class _DB:
        def query(self, *_args, **_kwargs):
            return _Query()

        def add(self, _value):
            pass

        def flush(self):
            pass

    project = SimpleNamespace(
        id=188,
        current_stage="WAITING_VIDEO_INPUT",
        status="generating_video",
        last_error=None,
        config_json={
            "video_count": 3,
            "auto_run": True,
            "manual_paused": False,
        },
        state_json={
            "video_variant_pipeline": {
                "target_count": 3,
                "active_index": 1,
                "submitted_indices": [1],
                "completed_indices": [1],
                "failed_indices": [],
            },
        },
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_variant_rollout_authorized",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_resume_stage_for_serial_variant",
        lambda *_args, **_kwargs: "CREATIVE_REVIEW",
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_latest_variant_stage",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_latest_stage",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_stage_owns_publish_lease",
        lambda _stage: False,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_queue_existing_stage",
        lambda _db, _project, stage, *, reason: queued.append(reason),
    )

    result = content_factory_tasks_module._queue_serial_variant_resume_stage(
        _DB(),
        project,
        variant_index=2,
        reason="release prepared variant after completed video",
    )

    assert result is prepared
    assert queued == ["release prepared variant after completed video"]
    assert project.current_stage == "CREATIVE_REVIEW"
    assert project.status == "queued"
    assert prepared.input_json["variant_index"] == 2


def test_serial_variant_replenishment_returns_unavailable_browser_to_recovery_supervisor(
    monkeypatch,
):
    from app.core.errors import APIError

    recovered: list[dict] = []
    stage = SimpleNamespace(
        id=3042,
        stage="VISUAL_PREVIEW",
        status="failed",
        input_json={
            "variant_index": 3,
            "execution_backend": "browser",
            "api_fallback_to_browser": True,
        },
    )
    project = SimpleNamespace(
        id=185,
        config_json={"video_count": 5, "auto_run": True},
        state_json={
            "video_variant_pipeline": {
                "target_count": 5,
                "active_index": 3,
            }
        },
        current_stage="VISUAL_PREVIEW",
        status="generating_video",
        last_error=None,
    )

    class _Query:
        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    class _Db:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        content_factory_tasks_module,
        "_sequential_variants_enabled",
        lambda _project: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_variant_rollout_authorized",
        lambda _project, _index: True,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_set_variant_pipeline",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_resume_stage_for_serial_variant",
        lambda *_args, **_kwargs: "VISUAL_PREVIEW",
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_latest_variant_stage",
        lambda *_args, **_kwargs: stage,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_latest_stage",
        lambda *_args, **_kwargs: stage,
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_queue_existing_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            APIError(
                "CONTENT_BROWSER_BRIDGE_REQUIRED",
                "请先在当前电脑创建并连接浏览器桥，再运行内容工厂项目。",
                409,
            )
        ),
    )
    monkeypatch.setattr(
        content_factory_tasks_module,
        "_recover_browser_fault_with_supervisor",
        lambda _db, _project, _stage, **kwargs: recovered.append(kwargs) or {
            "status": "recovery_supervisor_api_retry_scheduled"
        },
    )

    queued = content_factory_tasks_module._queue_serial_variant_resume_stage(
        _Db(),
        project,
        variant_index=3,
        reason="parallel video waiter replenishment",
    )

    assert queued is None
    assert recovered == [{
        "reason": (
            "CONTENT_BROWSER_BRIDGE_REQUIRED: "
            "请先在当前电脑创建并连接浏览器桥，再运行内容工厂项目。"
        ),
        "browser_retry_exhausted": True,
    }]


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
