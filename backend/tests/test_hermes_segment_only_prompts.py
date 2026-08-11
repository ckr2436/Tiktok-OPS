from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from app.tasks.hermes_agent import content_factory_tasks
from app.tasks.hermes_agent.content_factory_tasks import (
    LOCAL_POSTPRODUCTION_POLICY_VERSION,
    VIDEO_PROMPT_POLICY_VERSION,
    _ass_overlay_text,
    _audit_composed_content_video,
    _authoritative_reference_count,
    _compact_packet_for_chatgpt,
    _compact_provider_segment_prompt,
    _exclude_blocked_reference_indices_from_video_plan,
    _normalize_segment_timeline,
    _omni_reference_prompt,
    _guidance_is_internal_variant_label,
    _guard_existing_media_manifest,
    _media_group_source_is_superseded,
    _recover_legacy_media_group_source_stage,
    _local_postprocess_segment,
    _segment_story_overlay,
    _segment_needs_product_anchor,
    _select_segment_refs,
    _write_local_overlay_ass,
)


def test_blocked_visual_anchor_is_removed_without_restoring_its_action_panel():
    plan = [{
        "segments": [{
            "segment_index": 1,
            "reference_indices": [1, 3],
        }],
    }]
    refs = [
        {"index": 1, "asset_id": 5749},
        {"index": 3, "asset_id": 5751},
    ]

    blocked = _exclude_blocked_reference_indices_from_video_plan(
        plan,
        refs,
        {5751},
    )

    assert blocked == {3}
    assert plan[0]["segments"][0]["reference_indices"] == [1]
    assert plan[0]["segments"][0]["reference_selection_authoritative"] is True


def test_blocked_provider_reference_does_not_transfer_action_role_to_scene():
    task = SimpleNamespace(
        fail_code="provider_reference_blocked",
        fail_msg="image reference 2 blocked by provider",
    )
    payload = {
        "prompt": "A timed prompt owns the complete action.",
        "content_factory_base_prompt": "A timed prompt owns the complete action.",
        "reference_file_paths": [
            {
                "asset_id": 10,
                "path": "/tmp/scene.png",
                "semantic_roles": ["scene_anchor"],
                "is_product_anchor": False,
            },
            {
                "asset_id": 11,
                "path": "/tmp/bad-hands.png",
                "semantic_roles": ["action_anchor"],
                "is_product_anchor": False,
            },
        ],
    }
    source_files = [
        SimpleNamespace(id=20, kind="reference_upload"),
        SimpleNamespace(id=21, kind="reference_upload"),
    ]

    repaired, _files, evidence = (
        content_factory_tasks._repair_blocked_omni_reference(
            task,
            payload,
            source_files,
        )
    )

    assert evidence["blocked_asset_id"] == 11
    assert repaired["reference_file_paths"][0]["semantic_roles"] == [
        "scene_anchor"
    ]


def test_media_manifest_is_idempotent_and_fails_closed_after_provider_submit():
    group = {
        "media_manifest_sha256": "signed-a",
        "segments": [{"task_id": 11}, {"task_id": 12}],
    }
    queued = [
        SimpleNamespace(id=11, state="queued_local"),
        SimpleNamespace(id=12, state="waiting_dependency"),
    ]
    assert _guard_existing_media_manifest(
        group,
        queued,
        media_manifest_sha256="signed-a",
        variant_index=7,
    ) == [11, 12]
    assert _guard_existing_media_manifest(
        group,
        queued,
        media_manifest_sha256="signed-b",
        variant_index=7,
    ) is None

    submitted = [
        SimpleNamespace(id=11, state="processing"),
        SimpleNamespace(id=12, state="waiting_dependency"),
    ]
    with pytest.raises(ValueError, match="CONTENT_MEDIA_MANIFEST_FROZEN"):
        _guard_existing_media_manifest(
            group,
            submitted,
            media_manifest_sha256="signed-b",
            variant_index=7,
        )


def test_media_manifest_rejects_missing_task_ledger_rows():
    with pytest.raises(
        ValueError,
        match="CONTENT_MEDIA_MANIFEST_TASK_LEDGER_INCOMPLETE",
    ):
        _guard_existing_media_manifest(
            {
                "media_manifest_sha256": "signed-a",
                "segments": [{"task_id": 11}, {"task_id": 12}],
            },
            [SimpleNamespace(id=11, state="queued_local")],
            media_manifest_sha256="signed-a",
            variant_index=7,
        )


def test_superseded_source_stage_unfreezes_only_its_bound_media_group():
    class FakeDB:
        def __init__(self, stage):
            self.stage = stage

        def get(self, _model, _identity):
            return self.stage

    project = SimpleNamespace(id=168)
    superseded = SimpleNamespace(
        project_id=168,
        stage="VIDEO_PROMPTS",
        status="superseded",
    )
    successful = SimpleNamespace(
        project_id=168,
        stage="VIDEO_PROMPTS",
        status="success",
    )

    assert _media_group_source_is_superseded(
        FakeDB(superseded), project, {"source_stage_id": 2306}
    ) is True
    assert _media_group_source_is_superseded(
        FakeDB(successful), project, {"source_stage_id": 2311}
    ) is False
    assert _media_group_source_is_superseded(
        FakeDB(superseded), project, {}
    ) is False


def test_legacy_media_group_recovers_source_stage_only_from_complete_scoped_ledger():
    project = SimpleNamespace(id=168, workspace_id=3, user_id=9)
    group = {
        "media_manifest_sha256": "manifest-a",
        "segments": [{"task_id": 11}, {"task_id": 12}],
    }
    tasks = [
        SimpleNamespace(
            id=task_id,
            workspace_id=3,
            created_by_user_id=9,
            input_json={
                "content_factory_project_id": 168,
                "content_factory_source_stage_id": 2306,
                "content_factory_media_manifest_sha256": "manifest-a",
            },
        )
        for task_id in (11, 12)
    ]

    recovered = _recover_legacy_media_group_source_stage(
        project,
        group,
        tasks,
    )

    assert recovered["source_stage_id"] == 2306
    assert recovered["source_stage_recovered_from_task_ledger"] is True
    assert "source_stage_id" not in group
    assert "source_stage_id" not in _recover_legacy_media_group_source_stage(
        project,
        group,
        tasks[:1],
    )
    wrong_tenant = [
        SimpleNamespace(**{**vars(tasks[0]), "workspace_id": 4}),
        tasks[1],
    ]
    assert "source_stage_id" not in _recover_legacy_media_group_source_stage(
        project,
        group,
        wrong_tenant,
    )


def _segment() -> dict:
    return {
        "prompt": (
            "Doorway reaction\n"
            "The woman steps back from the doorway, points at the bottle on the nightstand, and stops in a tense bridge pose.\n"
            "FULL VIDEO: she later explains every ingredient, gives the offer, and completes the entire story."
        ),
        "segment_goal": "Immediate confrontation beat",
        "pacing": "fast, punchy, no filler",
        "camera": "stable handheld medium shot, slight push-in",
        "characters": [{"name": "Maya", "wardrobe": "gray T-shirt", "action": "points once"}],
        "dialogue_lines": [{"speaker": "Maya", "line": "Why is that still here?"}],
        "continuity_note": "End with Maya's right hand pointing and the man frozen beside the doorway.",
        "negative_prompt": "No identity change, extra people, collage, or product deformation.",
    }


def test_provider_prompt_contains_only_current_segment_fields():
    prompt = _compact_provider_segment_prompt(
        _segment(),
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
        product_required=False,
    )

    assert "The woman steps back from the doorway" in prompt
    assert "Pacing:" in prompt
    assert "Camera:" in prompt
    assert "Dialogue:" in prompt
    assert "Continuity:" in prompt
    assert "she later explains every ingredient" not in prompt
    assert "entire story" not in prompt
    assert len(prompt) < 1500


def test_provider_prompt_never_adds_provider_ui_command_prefix():
    prompt = _compact_provider_segment_prompt(
        _segment(),
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
        product_required=False,
    )

    assert not prompt.lstrip().startswith(
        ("生成视频：", "生成视频:", "视频生成：", "Generate video:")
    )


def test_timeline_prompt_does_not_import_whole_video_hook_actions():
    prompt = _compact_provider_segment_prompt(
        {
            "compile_source": "signed_production_plan",
            "prompt": "Segment 2: quiet product resolution.",
            "timeline": [{
                "start_second": 0,
                "end_second": 10,
                "action": "Settle the bottle under the warm bedside lamp.",
                "camera": "Slow product push-in.",
            }],
            "pacing": "Whip-pan into flashing alarms from the opening hook.",
            "camera_direction": "Crash zoom on the opening alarm.",
        },
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
    )

    assert "Settle the bottle" in prompt
    assert "Slow product push-in" in prompt
    assert "Signed rhythm:" not in prompt
    assert "Whip-pan" not in prompt
    assert "Crash zoom" not in prompt


def test_retry_prompt_removes_legacy_whole_video_motion_lines():
    prompt = content_factory_tasks._scope_retry_prompt_to_segment_timeline(
        "\n".join(
            (
                "Segment 2: quiet product resolution.",
                "Timeline (this segment only): 0-10s: Hold on the product hero.",
                "Pacing: Whip-pan into flashing alarms from segment 1.",
                "Camera: Crash zoom on the opening alarm.",
                "Dialogue: 'Find MYUPONA on TikTok Shop.'",
            )
        ),
    )

    assert "Hold on the product hero" in prompt
    assert "Find MYUPONA" in prompt
    assert "Whip-pan" not in prompt
    assert "Crash zoom" not in prompt


def test_omni_prompt_binds_only_selected_segment_references():
    base = _compact_provider_segment_prompt(
        _segment(),
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
        product_required=False,
    )
    refs = [
        {
            "index": 1,
            "filename": "character.png",
            "description": "same woman, gray T-shirt",
            "semantic_roles": ["character_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 3,
            "filename": "doorway.png",
            "description": "warm bedroom doorway",
            "semantic_roles": ["scene_anchor", "action_anchor"],
            "is_product_anchor": False,
        },
    ]

    prompt = _omni_reference_prompt(base, refs, product_required=False)

    assert "@image1" in prompt and "@image2" in prompt
    assert "@image3" not in prompt
    assert "FULL VIDEO" not in prompt
    assert "uploaded order is authoritative" in prompt
    assert len(prompt) < 2100
    assert VIDEO_PROMPT_POLICY_VERSION == (
        "2026-08-04-prompt-authority-sparse-anchors-v45"
    )


def test_provider_dialogue_keeps_native_emotion_and_does_not_request_silence():
    segment = _segment()
    segment["dialogue_lines"] = [{
        "line_id": "l1",
        "speaker": "woman_1",
        "line": "I am ending the scroll tonight.",
        "delivery_mode": "spoken",
        "delivery_method": "provider_dialogue",
    }]
    segment["voice_lock"] = [{
        "identity": "woman_1",
        "gender": "female",
        "screen_relation": "character_voiceover",
        "timbre": "warm intimate alto",
        "pitch": "medium-low",
        "accent": "US",
        "delivery": "emotionally alive, relieved but decisive",
        "speech_rate": 175,
    }]

    prompt = _compact_provider_segment_prompt(
        segment,
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
        product_required=False,
        audio_mode="spoken",
    )

    assert "I am ending the scroll tonight." in prompt
    assert "emotionally alive" in prompt
    assert "visible characters remain silent" not in prompt
    assert "voiceover is added" not in prompt


def test_provider_dialogue_does_not_leak_truncated_speaker_metadata():
    segment = _segment()
    segment["dialogue_lines"] = [{
        "line_id": "l1",
        "speaker": "adult_american_english_narrator",
        "line": "Post-Pilates reset? Meet MYUPONA.",
        "delivery_mode": "spoken",
        "delivery_method": "provider_dialogue",
    }]
    segment["voice_lock"] = [{
        "identity": "adult American English female narrator",
        "gender": "female",
        "screen_relation": "off_screen_narrator",
        "accent": "general American",
    }]

    prompt = _compact_provider_segment_prompt(
        segment,
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
        product_required=False,
        audio_mode="spoken",
    )

    assert "Post-Pilates reset? Meet MYUPONA." in prompt
    assert "adult_american_english_narr" not in prompt
    assert "..." not in prompt


def test_local_overlay_preserves_complete_director_copy_and_wraps_it():
    text = _ass_overlay_text(
        "$7.99 per bottle. Find them in the yellow cart below.",
        line_width=24,
    )

    assert "$7.99 per bottle." in text
    assert "yellow cart below." in text.replace(r"\N", " ")
    assert r"\N" in text
    assert "..." not in text


def test_local_overlay_uses_director_placement_not_hardcoded_top(tmp_path):
    target = tmp_path / "copy.ass"
    assert _write_local_overlay_ass(
        [{
            "line": "This belongs with the decision, not across the face.",
            "start_seconds": 0,
            "end_seconds": 2,
            "overlay_presentation": {
                "placement": "lower_third",
                "emphasis": "strong",
                "background": "box",
                "max_lines": 2,
            },
        }],
        target,
        width=720,
        height=1280,
        duration=2,
    )
    contents = target.read_text(encoding="utf-8")
    assert ",LowerThirdBox," in contents
    assert ",TopSafeBox," not in next(
        line for line in contents.splitlines() if line.startswith("Dialogue:")
    )


def test_local_overlay_supports_bottom_right_watermark_cover_placement(tmp_path):
    target = tmp_path / "copy.ass"
    assert _write_local_overlay_ass(
        [{
            "line": "Find on TikTok Shop",
            "start_seconds": 0,
            "end_seconds": 2,
            "overlay_presentation": {
                "placement": "bottom_right",
                "emphasis": "standard",
                "background": "box",
                "max_lines": 1,
            },
        }],
        target,
        width=720,
        height=1280,
        duration=2,
    )
    contents = target.read_text(encoding="utf-8")
    assert ",BottomRightBox," in next(
        line for line in contents.splitlines() if line.startswith("Dialogue:")
    )


def test_local_overlay_band_draws_full_width_cover_behind_fresh_copy(tmp_path):
    target = tmp_path / "copy.ass"
    assert _write_local_overlay_ass(
        [{
            "line": "Find on TikTok Shop",
            "start_seconds": 0,
            "end_seconds": 2,
            "overlay_presentation": {
                "placement": "bottom_right",
                "emphasis": "standard",
                "background": "band",
                "max_lines": 1,
            },
        }],
        target,
        width=720,
        height=1280,
        duration=2,
    )
    contents = target.read_text(encoding="utf-8")
    dialogue_rows = [
        line for line in contents.splitlines() if line.startswith("Dialogue:")
    ]
    assert any(r"\p1" in line and "l 720" in line for line in dialogue_rows)
    assert any(",BottomRightShadow," in line for line in dialogue_rows)




def test_product_postprocess_never_pastes_uploaded_package(tmp_path):
    source = tmp_path / "scene.mp4"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204080:s=360x640:d=1:r=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
        timeout=60,
    )
    product = tmp_path / "product.png"
    image = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(image).rounded_rectangle(
        (55, 20, 145, 280),
        radius=15,
        fill=(210, 35, 45),
    )
    image.save(product, compress_level=0)
    task = SimpleNamespace(
        id=99,
        input_json={
            "reference_file_paths": [{
                "path": str(product),
                "is_product_anchor": True,
            }],
        },
    )
    target = tmp_path / "result.mp4"
    evidence = _local_postprocess_segment(
        source,
        target,
        segment={
            "product_anchor_required": True,
            "display_lines": [],
            "authoritative_product_composites": [{
                "placement": "lower_center",
                "width_fraction": 0.30,
                "entrance": "cut",
                "start_seconds": 0,
                "end_seconds": 1,
                "resolved_box": {
                    "x_fraction": 0.30,
                    "y_fraction": 0.40,
                },
            }],
        },
        task=task,
    )
    frame = tmp_path / "frame.png"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0.5", "-i", str(target), "-frames:v", "1", str(frame),
        ],
        check=True,
        timeout=60,
    )
    pixels = Image.open(frame).convert("RGB")
    red_pixels = sum(
        1
        for red, green, blue in pixels.crop((110, 380, 250, 620)).getdata()
        if red > 150 and green < 90 and blue < 100
    )
    top_left = pixels.getpixel((20, 20))
    assert red_pixels == 0
    assert top_left[2] > top_left[0]
    assert evidence["authoritative_product_insert"] is False
    assert evidence["product_render_mode"] == "provider_reference"
    assert evidence["product_reference_submitted"] is True
    assert evidence["product_reference_sha256"]








def test_final_video_gate_requires_complete_director_copy_contract(tmp_path):
    target = tmp_path / "final.mp4"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204080:s=360x640:d=1:r=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
        ],
        check=True,
        timeout=60,
    )
    segment = {
        "segment_index": 1,
        "duration": 1,
        "audio_mode": "silent",
        "product_anchor_required": False,
        "display_lines": [{
            "line_id": "line.1",
            "overlay_presentation": {
                "placement": "center",
                "emphasis": "strong",
                "background": "box",
                "max_lines": 2,
            },
        }],
    }
    report = _audit_composed_content_video(
        target,
        group={"duration": 1, "media_manifest_sha256": "a" * 64},
        segments=[segment],
        local_postproduction=[{
            "segment_index": 1,
            "display_line_ids": ["line.1"],
        }],
    )
    assert report["status"] == "PASS"

    broken = dict(segment)
    broken["display_lines"] = [{"line_id": "line.1"}]
    try:
        _audit_composed_content_video(
            target,
            group={"duration": 1, "media_manifest_sha256": "b" * 64},
            segments=[broken],
            local_postproduction=[{
                "segment_index": 1,
                "display_line_ids": ["line.1"],
            }],
        )
    except ValueError as exc:
        assert "Director-owned placement" in str(exc)
    else:
        raise AssertionError("final video gate accepted unspecified copy layout")


def test_final_video_gate_reads_landscape_ratio_from_frozen_group(tmp_path):
    target = tmp_path / "landscape.mp4"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204080:s=640x360:d=1:r=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
        ],
        check=True,
        timeout=60,
    )
    report = _audit_composed_content_video(
        target,
        group={
            "duration": 1,
            "aspect_ratio": "16:9",
            "media_manifest_sha256": "c" * 64,
        },
        segments=[{
            "segment_index": 1,
            "duration": 1,
            "audio_mode": "silent",
            "product_anchor_required": False,
            "display_lines": [],
        }],
        local_postproduction=[{
            "segment_index": 1,
            "display_line_ids": [],
        }],
    )
    assert report["status"] == "PASS"
    assert report["expected_aspect_ratio"] == "16:9"


def test_final_video_gate_preserves_intent_repair_coordinates(tmp_path):
    target = tmp_path / "intent-failure.mp4"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204080:s=360x640:d=1:r=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
        ],
        check=True,
        timeout=60,
    )
    try:
        _audit_composed_content_video(
            target,
            group={
                "duration": 1,
                "aspect_ratio": "9:16",
                "media_manifest_sha256": "d" * 64,
            },
            segments=[{
                "segment_index": 1,
                "duration": 1,
                "audio_mode": "silent",
                "product_anchor_required": False,
                "display_lines": [],
            }],
            local_postproduction=[{
                "segment_index": 1,
                "display_line_ids": [],
            }],
            intent_fidelity={
                "status": "fail",
                "blocking": True,
                "policy_version": "intent-test-v1",
                "video_index": 3,
                "blocking_requirement_ids": ["R-001"],
                "blocking_reasons": ["Opening mechanism is not readable."],
                "repair_scope": "segment_regeneration",
                "affected_segment_indices": [1],
                "affected_task_ids": [901],
                "repair_instruction": "Regenerate only the opening segment.",
                "requirement_evidence": {
                    "R-001": {"status": "fail"},
                },
                "result_sha256": "e" * 64,
                "contact_sheet_path": "/tmp/final-review-result.jpg",
                "benchmark_contact_sheet_path": "/tmp/benchmark-review.jpg",
                "reviewed_at": "2026-08-05T01:00:00",
            },
        )
    except ValueError as exc:
        message = str(exc)
        assert "CONTENT_FINAL_INTENT_QA_FAILED" in message
        assert '"blocking_requirement_ids": ["R-001"]' in message
        assert '"affected_task_ids": [901]' in message
        assert '"repair_scope": "segment_regeneration"' in message
        assert '"result_sha256": "eeee' in message
        assert '"contact_sheet_path": "/tmp/final-review-result.jpg"' in message
        assert '"benchmark_contact_sheet_path": "/tmp/benchmark-review.jpg"' in message
    else:
        raise AssertionError("final video gate accepted failed signed intent")










def test_signed_plan_provider_prompt_preserves_exact_conversion_copy():
    exact_line = "Take two gummies. Find it in the yellow cart below."
    segment = {
        "compile_source": "signed_production_plan",
        "visual_style": (
            "Sophisticated American adult 2D/2.5D editorial animation; "
            "no photorealistic humans."
        ),
        "project_visual_style_requirement": (
            "Use adult editorial animation. No photorealistic humans."
        ),
        "prompt": "Segment 4: the approved product enters the existing scene.",
        "timeline": [
            {
                "start_second": 0,
                "end_second": 10,
                "action": "She keeps the sealed bottle beside the lamp.",
            }
        ],
        "dialogue_lines": [{"speaker": "Maya", "line": exact_line}],
    }

    prompt = _compact_provider_segment_prompt(
        segment,
        resolution="720p",
        language_label="English (US)",
        requirement_contract=[],
        product_required=True,
    )

    assert exact_line in prompt
    assert "Sophisticated American adult 2D/2.5D editorial animation" in prompt
    assert "no photorealistic humans" in prompt
    assert "Project visual-medium constraint (authoritative)" in prompt
    assert "Use adult editorial animation" in prompt
    assert "This is my simple reset tonight." not in prompt


def test_editor_chapter_uses_actual_dialogue_instead_of_variant_label():
    segment = {
        "segment_goal": "Hook viewers with a funny mirror surprise.",
        "dialogue_lines": [{"speaker": "Skeleton", "line": "Why does my bedtime routine look like a garage sale?"}],
    }

    title = _segment_story_overlay(
        segment,
        index=1,
        count=3,
        concept="V10",
        point_title="A Melatonin-Free Night Routine",
        language="en-US",
    )

    assert title == "Why does my bedtime routine look like a garage"
    assert _guidance_is_internal_variant_label("V10") is True
    assert _guidance_is_internal_variant_label("Midnight Detective") is False


def test_segment_timeline_accepts_timecode_and_plural_seconds_fields():
    timeline = _normalize_segment_timeline(
        [
            {"timecode": "0.0-2.5s", "action": "She turns away from the phone."},
            {
                "start_seconds": 2.5,
                "end_seconds": 6,
                "visual": "She crosses the room and stops at the nightstand.",
            },
            {"time": "6–10 秒", "action": "She ends in a clear bridge pose."},
        ],
        segment_duration=10,
        segment_offset=0,
        video_index=1,
        segment_index=1,
    )

    assert [(row["start_second"], row["end_second"]) for row in timeline] == [
        (0.0, 2.5),
        (2.5, 6.0),
        (6.0, 10.0),
    ]
    assert timeline[1]["action"].startswith("She crosses")


def test_segment_timeline_rejects_nonempty_zero_duration_payload():
    import pytest

    with pytest.raises(ValueError, match="no valid positive-duration beats"):
        _normalize_segment_timeline(
            [{"timecode": "0-0", "action": "Invalid collapsed beat"}],
            segment_duration=10,
            segment_offset=0,
            video_index=2,
            segment_index=1,
        )


def test_non_product_nightstand_scene_does_not_request_product_anchor():
    prompt = (
        "She places her phone face-down on the nightstand and turns toward the doorway. "
        "Do not show the product yet; end on the unresolved bridge pose."
    )

    assert _segment_needs_product_anchor(prompt, []) is False


def test_explicit_product_action_or_selected_product_reference_requests_anchor():
    assert _segment_needs_product_anchor(
        "She opens the MYUPONA bottle and holds the label facing camera.",
        [],
    ) is True
    assert _segment_needs_product_anchor(
        "She crosses the bedroom.",
        [{"is_product_anchor": True}],
    ) is True


def test_time_scoped_no_product_does_not_hide_later_product_reveal():
    prompt = (
        "0-3s: no product is visible; phone glows in the dark | "
        "3-6s: she puts the phone face-down | "
        "6-9s: she holds the MYUPONA bottle and presents exactly two gummies"
    )

    assert _segment_needs_product_anchor(prompt, []) is True


def test_segment_reference_selection_rejects_other_segment_panels_even_when_model_selects_all():
    refs = [
        {
            "index": 1,
            "filename": "s1-character.png",
            "path": "/tmp/s1-character.png",
            "reference_segment": 1,
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 2,
            "filename": "s1-action.png",
            "path": "/tmp/s1-action.png",
            "reference_segment": 1,
            "semantic_roles": ["action_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 3,
            "filename": "s2-character.png",
            "path": "/tmp/s2-character.png",
            "reference_segment": 2,
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 4,
            "filename": "s2-action.png",
            "path": "/tmp/s2-action.png",
            "reference_segment": 2,
            "semantic_roles": ["action_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 5,
            "filename": "product.png",
            "path": "/tmp/product.png",
            "reference_segment": 0,
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        },
    ]

    first = _select_segment_refs(
        refs, [1, 2, 3, 4, 5], limit=7,
        prompt="Conflict hook. No product appears in this segment.",
        product_required=False, segment_index=1,
    )
    second = _select_segment_refs(
        refs, [1, 2, 3, 4, 5], limit=7,
        prompt="Product reveal and spoken CTA.",
        product_required=True, segment_index=2,
    )

    assert [ref["index"] for ref in first] == [1, 2]
    assert {ref["index"] for ref in second} == {3, 4, 5}
    assert all(ref.get("reference_segment") != 1 for ref in second if not ref.get("is_product_anchor"))


def test_segment_reference_selection_bounds_unknown_all_selected_board():
    refs = [
        {
            "index": index,
            "filename": f"panel-{index}.png",
            "path": f"/tmp/panel-{index}.png",
            "reference_segment": 0,
            "semantic_roles": (
                ["character_anchor", "scene_anchor"] if index == 1 else ["action_anchor"]
            ),
            "is_product_anchor": False,
        }
        for index in range(1, 8)
    ]

    selected = _select_segment_refs(
        refs, list(range(1, 8)), limit=7,
        prompt="One local action beat.", product_required=False, segment_index=1,
    )

    assert len(selected) <= 4
    assert any("character_anchor" in ref["semantic_roles"] for ref in selected)
    assert any("scene_anchor" in ref["semantic_roles"] for ref in selected)
    assert any("action_anchor" in ref["semantic_roles"] for ref in selected)


def test_reference_ranking_does_not_require_an_action_panel():
    refs = [
        {
            "index": 1,
            "path": "/tmp/identity.png",
            "reference_segment": 1,
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        },
        *[
            {
                "index": index,
                "path": f"/tmp/action-{index}.png",
                "reference_segment": 1,
                "semantic_roles": ["action_anchor"],
                "is_product_anchor": False,
            }
            for index in range(2, 8)
        ],
    ]

    selected = _select_segment_refs(
        refs,
        [1, 2, 3, 4, 5, 6, 7],
        limit=4,
        prompt="Text owns every timed action and camera cut.",
        product_required=False,
        segment_index=1,
    )

    assert selected[0]["index"] == 1
    assert len(selected) <= 4


def test_segment_reference_repair_never_borrows_another_segments_anchor():
    refs = [
        {
            "index": 1,
            "filename": "segment-one-action.png",
            "path": "/tmp/segment-one-action.png",
            "reference_segment": 1,
            "semantic_roles": ["action_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 2,
            "filename": "segment-two-character.png",
            "path": "/tmp/segment-two-character.png",
            "reference_segment": 2,
            "semantic_roles": ["character_anchor", "scene_anchor"],
            "is_product_anchor": False,
        },
    ]

    selected = _select_segment_refs(
        refs, [1, 2], limit=7,
        prompt="Segment one local action.", product_required=False, segment_index=1,
    )

    assert [ref["index"] for ref in selected] == [1]


def test_segment_reference_selection_returns_empty_instead_of_borrowing_later_product_frame():
    refs = [
        {
            "index": 1,
            "filename": "segment-four-product-scene.png",
            "path": "/tmp/segment-four-product-scene.png",
            "reference_segment": 4,
            "semantic_roles": ["action_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 2,
            "filename": "authoritative-product.png",
            "path": "/tmp/authoritative-product.png",
            "reference_segment": 0,
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        },
    ]

    selected = _select_segment_refs(
        refs,
        [],
        limit=7,
        prompt="Dinner conversation after the work shift. No product appears.",
        product_required=False,
        segment_index=1,
    )

    assert selected == []


def test_authoritative_multimodal_reference_selection_does_not_restore_action_or_scene_panels():
    refs = [
        {
            "index": 1,
            "filename": "scene.png",
            "path": "/tmp/scene.png",
            "reference_segment": 1,
            "semantic_roles": ["scene_anchor", "action_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 2,
            "filename": "malformed-hands.png",
            "path": "/tmp/malformed-hands.png",
            "reference_segment": 1,
            "semantic_roles": ["action_anchor"],
            "is_product_anchor": False,
        },
        {
            "index": 3,
            "filename": "package.png",
            "path": "/tmp/package.png",
            "reference_segment": 0,
            "semantic_roles": ["product_anchor"],
            "is_product_anchor": True,
        },
    ]

    selected = _select_segment_refs(
        refs,
        [],
        limit=10,
        prompt="Prompt text owns the opening, application, pacing and narration.",
        product_required=True,
        segment_index=1,
        authoritative_selection=True,
    )

    assert [ref["index"] for ref in selected] == [3]


def test_creative_reference_plan_count_is_authoritative_over_review_prose():
    creative = {
        "visual_job_ticket": {
            "final_reference_count": 5,
            "reference_plan": [{"index": index} for index in range(1, 6)],
        }
    }

    count = _authoritative_reference_count(
        creative,
        review_result={"reference_image_count": 6},
        fallback_texts=["A 3x2 example would also be possible."],
    )

    assert count == 5


def test_chatgpt_packets_keep_reference_plan_but_bound_large_creative_history():
    reference_plan = [
        {
            "index": index,
            "segment": 1 if index < 4 else 2,
            "roles": ["character_anchor", "action_anchor"],
            "description": (f"Panel {index} action " + "x" * 900),
        }
        for index in range(1, 8)
    ]
    packet = {
        "project_id": "cf_compact",
        "current_stage": "VISUAL_PREVIEW",
        "project_requirements": "fast conversion video " * 200,
        "previous_outputs": {
            "MEDIA_DESIGN": {
                "selected_concept": {"title": "Book Rescue", "logline": "y" * 4000},
                "visual_job_ticket": {
                    "board_rule": "one ordered board",
                    "reference_plan": reference_plan,
                    "unused_bulk": "z" * 12000,
                },
                "shot_plan": [{"visual": "q" * 1200} for _ in range(20)],
                "continuity_rules": ["same cast " + "r" * 800 for _ in range(20)],
            }
        },
    }

    compact = _compact_packet_for_chatgpt(packet, "VISUAL_PREVIEW")
    encoded = __import__("json").dumps(compact, ensure_ascii=False)

    assert len(compact["previous_outputs"]["MEDIA_DESIGN"]["visual_job_ticket"]["reference_plan"]) == 7
    assert "unused_bulk" not in encoded
    assert len(encoded) < 12000
