from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.doubao_provider import client
from app.services.doubao_provider import tasks as doubao_tasks
from app.data.models.kie_api import KieFile, KieTask
from app.tasks.ai_video import video_tasks
from app.services.ai_video.prompt_budget import (
    _compact_text,
    compact_repair_instruction,
    compact_structured_video_prompt,
    is_structured_video_prompt,
    localize_structured_video_prompt_for_doubao,
    structured_video_prompt_semantic_invariants,
    validate_structured_video_prompt_fidelity,
)


def test_helper_classifies_nested_copyright_refusal_without_exposing_text() -> None:
    raw = json.dumps(
        {
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [
                        {"user_type": 1, "content": "request"},
                        {
                            "user_type": 2,
                            "payload": {
                                "text": "抱歉，由于版权相关限制，暂时无法创作对应的内容"
                            },
                        },
                    ]
                }
            }
        },
        ensure_ascii=False,
    )

    completed = subprocess.run(
        [
            "/opt/apps/doubao2api-lab/.venv/bin/python",
            "-c",
            (
                "import json,runpy,sys;"
                "ns=runpy.run_path(sys.argv[1]);"
                "print(json.dumps(ns['_poll_progress_diagnostic'](sys.argv[2])))"
            ),
            "/opt/apps/doubao2api-lab/scripts/context_generate.py",
            raw,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    progress = json.loads(completed.stdout)

    assert progress == {
        "state": "content_rejected",
        "message_count": 2,
        "bot_message_count": 1,
        "bot_content_count": 1,
        "video_model_count": 0,
        "content_rejected": True,
    }


def test_helper_classifies_real_nested_text_only_turn_for_account_rotation() -> None:
    raw = json.dumps(
        {
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [
                        {"user_type": 1, "content": "request"},
                        {
                            "user_type": 2,
                            "content": "",
                            "content_block": [
                                {
                                    "content": {
                                        "text_block": {
                                            "text": "assistant returned prose only"
                                        }
                                    }
                                }
                            ],
                        },
                    ]
                }
            }
        }
    )

    completed = subprocess.run(
        [
            "/opt/apps/doubao2api-lab/.venv/bin/python",
            "-c",
            (
                "import json,runpy,sys;"
                "ns=runpy.run_path(sys.argv[1]);"
                "print(json.dumps(ns['_poll_progress_diagnostic'](sys.argv[2])))"
            ),
            "/opt/apps/doubao2api-lab/scripts/context_generate.py",
            raw,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    progress = json.loads(completed.stdout)

    assert progress["state"] == "assistant_progress"
    assert progress["bot_content_count"] == 1
    assert progress["video_model_count"] == 0
    assert "assistant returned prose only" not in completed.stdout


def test_word_boundary_compaction_never_leaves_dangling_action_token() -> None:
    compacted = _compact_text(
        "the phone snaps dark at exactly the moment the character looks up",
        24,
    )

    assert compacted == "the phone snaps dark"


def test_helper_nonzero_without_result_has_specific_runtime_error(monkeypatch) -> None:
    monkeypatch.setattr(client, "validate_doubao_helper_runtime", lambda: None)
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args, returncode=1, stdout=b""),
    )

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        client.invoke_doubao_helper({"action": "keepalive"}, timeout_seconds=10)

    assert exc_info.value.code == "doubao_helper_runtime_failed"


def test_helper_maps_configuration_control_drift_to_composer_rotation() -> None:
    completed = subprocess.run(
        [
            "/opt/apps/doubao2api-lab/.venv/bin/python",
            "-c",
            (
                "import json,runpy,sys;"
                "ns=runpy.run_path(sys.argv[1]);"
                "exc=RuntimeError('Doubao video composer did not become ready "
                "[stage=configuration_controls]');"
                "print(json.dumps(ns['_failure_result'](exc)))"
            ),
            "/opt/apps/doubao2api-lab/scripts/context_generate.py",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)

    assert result["error_code"] == "doubao_composer_unavailable"
    assert result["error"] == "豆包视频生成器暂时不可用，系统将切换账号重试。"
    assert "configuration_controls" in result["diagnostic"]


def test_helper_success_envelope_is_returned(monkeypatch) -> None:
    monkeypatch.setattr(client, "validate_doubao_helper_runtime", lambda: None)
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=b'{"status":"healthy"}',
        ),
    )

    assert client.invoke_doubao_helper({"action": "keepalive"}, timeout_seconds=10) == {
        "status": "healthy"
    }


def test_helper_membership_rejection_keeps_stable_error_code(monkeypatch) -> None:
    monkeypatch.setattr(client, "validate_doubao_helper_runtime", lambda: None)
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=2,
            stdout=(
                b'{"status":"failed","error":"membership required",'
                b'"error_code":"doubao_membership_required"}'
            ),
        ),
    )

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        client.invoke_doubao_helper({"action": "poll"}, timeout_seconds=10)

    assert exc_info.value.code == "doubao_membership_required"


def test_helper_logs_bounded_internal_stage_diagnostic(monkeypatch, caplog) -> None:
    monkeypatch.setattr(client, "validate_doubao_helper_runtime", lambda: None)
    monkeypatch.setattr(
        client.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=2,
            stdout=(
                b'{"status":"failed","error":"temporarily unavailable",'
                b'"error_code":"doubao_composer_unavailable",'
                b'"diagnostic":"RuntimeError: request drift [stage=request_not_observed]"}'
            ),
        ),
    )

    with caplog.at_level("WARNING"), pytest.raises(client.DoubaoProviderError):
        client.invoke_doubao_helper({"action": "submit"}, timeout_seconds=10)

    assert "stage=request_not_observed" in caplog.text


def test_stable_doubao_quota_code_is_a_global_quota_failure() -> None:
    error = client.DoubaoProviderError(
        "capacity unavailable", code="doubao_quota_exhausted"
    )

    assert video_tasks._provider_error_is_quota_failure(error) is True


def test_live_composer_probe_holds_same_profile_before_paid_submit(monkeypatch) -> None:
    calls = []

    def invoke(payload, *, timeout_seconds):
        calls.append((payload, timeout_seconds))
        return {"status": "capable", "model": "seedance_v2.0_mini"}

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", invoke)

    doubao_tasks._ensure_live_video_composer(
        {"account_bridge_id": "br_test", "proxy_url": "http://proxy.test"},
        browser_cdp_url="http://127.0.0.1:9392",
    )

    assert calls == [({
        "account_bridge_id": "br_test",
        "proxy_url": "http://proxy.test",
        "action": "probe",
        "browser_cdp_url": "http://127.0.0.1:9392",
    }, max(
        30,
        int(doubao_tasks.settings.DOUBAO_COMPOSER_PROBE_TIMEOUT_SECONDS),
    ))]


def test_live_composer_probe_retries_once_while_cold_profile_warms(monkeypatch) -> None:
    calls = []

    def invoke(payload, *, timeout_seconds):
        calls.append((payload, timeout_seconds))
        if len(calls) == 1:
            raise client.DoubaoProviderError(
                "composer loading", code="doubao_composer_unavailable"
            )
        return {"status": "capable", "model": "seedance_v2.0_mini"}

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", invoke)
    monkeypatch.setattr(doubao_tasks.time, "sleep", lambda _seconds: None)

    doubao_tasks._ensure_live_video_composer(
        {"account_bridge_id": "br_test"},
        browser_cdp_url="http://127.0.0.1:9392",
    )

    assert len(calls) == 2


def test_live_composer_probe_does_not_retry_captcha(monkeypatch) -> None:
    calls = []

    def invoke(payload, *, timeout_seconds):
        calls.append((payload, timeout_seconds))
        raise client.DoubaoProviderError(
            "captcha required", code="doubao_captcha_required"
        )

    monkeypatch.setattr(doubao_tasks, "invoke_doubao_helper", invoke)
    monkeypatch.setattr(doubao_tasks.time, "sleep", lambda _seconds: None)

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        doubao_tasks._ensure_live_video_composer(
            {"account_bridge_id": "br_test"},
            browser_cdp_url="http://127.0.0.1:9392",
        )

    assert exc_info.value.code == "doubao_captcha_required"
    assert len(calls) == 1


def test_doubao_provider_compacts_long_structured_prompt_without_rewriting_dialogue() -> None:
    dialogue = (
        "I chose this simple routine step: blueberry flavor, two gummies per "
        "serving, with L-Theanine, GABA, magnesium glycinate."
    )
    source = "\n".join([
        "Segment 2: truthful reasons to consider",
        "Visual style (signed whole-video contract): " + "warm lifestyle " * 40,
        "Timeline (this segment only): " + "0-3s precise action and camera; " * 40,
        f"Dialogue: woman_1: '{dialogue}'",
        "Voice lock for this segment: adult US woman, female, clear alto. "
        "This speaker is explicitly female; do not change the speaker's gender "
        "in this or any adjacent segment. This is the visible protagonist's own "
        "voiceover, not an independent narrator, even when her lips are hidden. "
        "Keep the same speaker identity, gender, timbre, pitch, accent, and delivery.",
        "Continuity: " + "same woman, wardrobe, room, bottle, and gummies; " * 20,
        "Product presentation policy: use the uploaded product as sole package authority.",
        "Do not model-render captions, overlays, ingredient cards, sales UI, QR codes, collage, picture-in-picture, or watermark. Preserve the exact approved dialogue.",
        "Output: 9:16 720p; spoken language English (US) only.",
        "Segment scope: 2/3; show only this segment's actions and dialogue.",
        "One continuous full-frame scene. No collage, inset, playback UI, watermark, or language mixing.",
    ])

    compacted = compact_structured_video_prompt(
        source,
        max_characters=495,
    )
    task = KieTask(model="doubao-seedance-2-0-mini-260615", prompt=None)
    request = doubao_tasks._request(
        {"prompt": source, "seconds": 7, "aspect_ratio": "9:16"},
        task,
    )

    assert len(compacted) <= 495
    assert request["prompt"] == localize_structured_video_prompt_for_doubao(compacted)
    assert "参考图" in request["prompt"] or "本片段时间线" in request["prompt"]
    assert dialogue in request["prompt"]
    assert "same female visible protagonist" in request["prompt"]
    assert request["duration"] == 7
    assert request["ratio"] == "9:16"


def test_doubao_copy_delivery_retry_fits_budget_and_keeps_exact_dialogue() -> None:
    dialogue = (
        "I prefer melatonin-free. MYUPONA Sleep Easy Gummies: melatonin-free, "
        "two per serving. Take two. Phone down. Tap below."
    )
    source = "\n".join([
        "COPY DELIVERY REPAIR (highest audio priority): Begin speaking at 0.0 "
        "seconds and speak every word without omission. " + dialogue,
        "Timeline (this segment only): " + "warm product action; " * 30,
        f"Dialogue: female_narrator: '{dialogue}'",
        "Voice: same female speaker; preserve accent, timbre and delivery.",
        "Product presentation policy: uploaded package is sole authority.",
        "Output: 9:16 720p, English US, segment 2/2 only.",
    ])

    assert is_structured_video_prompt(source) is True
    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert len(compacted) <= 495
    assert dialogue in compacted
    assert "start at 0.0s" in compacted
    assert "no omissions" in compacted


def test_doubao_native_voice_retry_keeps_multimodal_repair_instruction() -> None:
    dialogue = (
        "Made with MSM, it fits right into an easy post-workout body-care "
        "routine."
    )
    source = "\n".join([
        (
            "Repair: Speak only the exact signed line; remove every added "
            "performance or superiority claim and pronounce MSM exactly."
        ),
        "Refs: @image1,@image2=story; @image3=package.",
        (
            "Beats: 0-3s: creator finishes a gentle massage and relaxes; "
            "product remains visible | 3-6s: absorbed texture and calm "
            "product-forward finish"
        ),
        f"Dialogue: '{dialogue}'",
        "Voice: same female off-screen narrator; US accent.",
        "Product: uploaded package is sole authority.",
        "9:16; no captions/UI/watermark; segment this segment.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert len(compacted) <= 495
    assert "Repair:" in compacted
    assert "remove every added" in compacted
    assert "pronounce MSM exactly" in compacted
    assert dialogue in compacted
    assert all(f"@image{index}" in compacted for index in range(1, 4))


def test_native_voice_final_review_compacts_exact_label_and_speech_repair() -> None:
    source = "\n".join([
        (
            'Repair: Regenerate all segments using one corrected authoritative '
            'product-packaging asset whose label reads exactly "MYUPONA '
            'Soothing Body Balm" and remains legible wherever shown. '
            'Regenerate or replace the spoken audio so every segment matches '
            'its signed dialogue_lines and expected_text exactly, especially '
            '"Post-Pilates reset," "MYUPONA Soothing Body Balm," and "MSM." '
            'Preserve the existing Pilates-studio concept, cast identity, '
            'green wardrobe, stylized 2D/2.5D medium, rapid progression, '
            'gentle intact-skin application, and non-medical claim boundaries.'
        ),
        "Refs: @image1=character+scene; @image2=package",
        (
            "Beats: 0-6s: exercise ball rolls fast; creator catches it and "
            "opens balm | 6-7s: show a small amount on intact shoulder skin"
        ),
        (
            "Dialogue: 'Post-Pilates reset? Meet MYUPONA Soothing Body "
            "Balm—tap the product card.'"
        ),
        "Voice: same adult US female creator, expressive.",
        "Product: uploaded package is sole authority.",
        "9:16; no captions/UI/watermark; segment 1 only.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert len(compacted) <= 495
    assert 'exact package label "MYUPONA Soothing Body Balm"' in compacted
    assert "speak Dialogue exactly" in compacted
    assert "Post-Pilates reset?" in compacted
    assert "Product: @image2 sole package authority." in compacted


def test_final_review_compacts_actionable_body_location_continuity() -> None:
    repair = (
        "No repair is required. Preserve the current segment continuity, "
        "including the same animated female creator and Pilates studio. "
        "Revise the opening of segment 3 to continue the same shoulder "
        "application and absorption state from segment 2. If a transition "
        "to leg application is intended, explicitly authorize and visually "
        "motivate that body-location change within the signed story."
    )

    compacted = compact_repair_instruction(repair, max_characters=145)

    assert len(compacted) <= 145
    assert "keep shoulder application continuous across segments" in compacted
    assert "no leg change" in compacted
    assert "No repair is" not in compacted


def test_doubao_small_budget_keeps_multimodal_bindings_and_every_motion_beat() -> None:
    source = "\n".join([
        "Segment 1: fast visual hook",
        (
            "Reference bindings: @image1=character+scene; "
            "@image2,@image3=character+scene+action; @image4=scene+action; "
            "images lock appearance/state, Motion controls animation."
        ),
        (
            "Timeline (this segment only): 0-3s cold bedroom opening; "
            "camera pushes toward phone | 3-6s portal escalation around her | "
            "6-9s phone goes face-down on warm tray"
        ),
        (
            "Motion and effects: 0-3s luminous portal pulls and shards multiply | "
            "3-6s shard storm slows around tired eyes | "
            "6-9s portal glow contracts into warm bedside light"
        ),
        (
            "Dialogue: female_narrator: 'One more video was 43 videos ago.' | "
            "female_narrator: 'There goes my morning walk.' | "
            "female_narrator: 'Phone down. I am starting my bedtime routine.'"
        ),
        "Voice: same female US narrator.",
        "Output: 9:16 720p, English US, segment 1/2 only.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert len(compacted) <= 495
    assert all(f"@image{index}" in compacted for index in range(1, 5))
    assert "portal pulls" in compacted
    assert "shard storm slows" in compacted
    assert "portal glow contracts" in compacted
    assert "One more video was 43 videos ago." in compacted
    assert "Phone down. I am starting my bedtime routine." in compacted


def test_doubao_local_voiceover_budget_prioritizes_multimodal_visual_execution() -> None:
    source = "\n".join([
        "Segment 1: animated portal hook",
        "Visual style (signed whole-video contract): adult 2D/2.5D animation, not a realistic human face.",
        (
            "Reference bindings: @image1=character+scene; "
            "@image2,@image3,@image4=ordered action states"
        ),
        (
            "Timeline (this segment only): 0-3s: phone portal pulls inward; "
            "FX: rapid scale distortion; Cam: fast push-in | "
            "3-6s: text-free shards multiply around tired eyes; "
            "FX: shard storm snaps toward her eyes; Cam: frontal close-up | "
            "6-9s: phone turns face-down on a warm tray; "
            "FX: blue portal glow contracts into warm bedside light; "
            "Cam: stable tabletop"
        ),
        "Audio: visible characters remain silent with no lip-sync; exact signed voiceover is added locally.",
        "Output: 9:16 720p.",
        "Segment scope: 1/2 only.",
    ])

    assert is_structured_video_prompt(source) is True
    compacted = compact_structured_video_prompt(source, max_characters=495)
    request = doubao_tasks._request(
        {"prompt": source, "seconds": 9, "aspect_ratio": "9:16"},
        KieTask(model="seedance_2_0_mini", prompt=None),
    )

    assert len(compacted) <= 495
    assert request["prompt"] == localize_structured_video_prompt_for_doubao(compacted)
    assert all(f"@image{index}" in compacted for index in range(1, 5))
    assert "phone portal" in compacted
    assert "shard storm" in compacted
    assert "blue portal glow" in compacted
    assert "Dialogue:" not in compacted
    assert "no speech/lip-sync" in compacted


def test_doubao_six_beat_hook_keeps_one_readable_action_per_timestamp() -> None:
    source = "\n".join([
        (
            "Reference bindings: @image1,@image2,@image3,@image4="
            "appearance+scene+action; @image5=package authority."
        ),
        (
            "Timeline (this segment only): "
            "0-1.72s: The female-presenting protagonist keeps scrolling "
            "despite a nearly empty red battery; FX: sharp battery pulse | "
            "1.72-2.5s: She freezes mid-scroll, lowers the phone, and looks "
            "toward the clock; FX: quick whip-pan | "
            "2.5-4.21s: She repeats rapid upward swipes and suddenly stops; "
            "FX: three jump cuts | "
            "4.21-5s: Her stopped hand hovers as the light trail collapses; "
            "FX: motion blur contracts | "
            "5-6.7s: She turns the phone face-down and sits up; FX: cable "
            "enters frame | "
            "6.7-9s: She unplugs the cable and reveals the product bottle; "
            "FX: match cut to nightstand"
        ),
        (
            "Product presentation policy: uploaded package is the sole "
            "authority; integrate it naturally in the scene."
        ),
        (
            "Audio: visible characters remain silent with no lip-sync; exact "
            "signed voiceover is added locally."
        ),
        "Output: 9:16; no generated text, UI, or watermark.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(
        source,
        compacted,
        required_reference_aliases=tuple(
            f"@image{index}" for index in range(1, 6)
        ),
        product_required=True,
    )

    assert len(compacted) <= 495
    assert "0-1.72s: scrolls on low battery" in compacted
    assert "1.72-2.5s: freezes; lowers phone; eyes clock" in compacted
    assert "2.5-4.21s: rapidly swipes; stops" in compacted
    assert "4.21-5s: hand hovers" in compacted
    assert "5-6.7s: phone face-down" in compacted
    assert "6.7-9s: product package visible" in compacted
    assert ": ; FX:" not in compacted
    assert "keeps scrolling despite |" not in compacted
    assert "looks back at |" not in compacted
    assert contract["validated"] is True


def test_long_compact_dialect_keeps_every_timed_beat_for_large_provider_lane() -> None:
    source = "\n".join([
        "Refs: @image1=character+scene; @image2=package",
        "Repair: " + ("preserve the approved visual repair evidence; " * 170),
        (
            "Beats: 0-4s: woman sets the balm jar on the nightstand and turns "
            "toward camera | 4-8s: she opens the jar, takes a small amount, "
            "and applies it to intact shoulder skin"
        ),
        "Direction: " + ("fast rhythmic cuts | tactile close-up | warm 2D animation | " * 130),
        "Dialogue: 'My nighttime reset stays simple.'",
        "Voice: same adult US female narrator; expressive and continuous.",
        "Product: uploaded package is sole authority.",
        "Audio: native expressive speech; no added words.",
        "9:16; this segment only; no text/UI/watermark.",
    ])

    assert len(source) > 12000
    actual = compact_structured_video_prompt(source, max_characters=12000)
    contract = validate_structured_video_prompt_fidelity(
        source,
        actual,
        required_reference_aliases=("@image1", "@image2"),
        product_required=True,
    )

    assert len(actual) <= 12000
    assert "0-4s:" in actual
    assert "4-8s:" in actual
    assert "sets the balm jar" in actual
    assert "opens the jar" in actual
    assert contract["validated"] is True


def test_product_visibility_invariant_does_not_rewrite_a_balm_jar_as_a_bottle() -> None:
    source = "\n".join([
        "Refs: @image1=character+scene; @image2=package",
        "Beats: 0-4s: she reveals the balm jar and holds it facing camera",
        "Dialogue: 'This is my simple wind-down step.'",
        "Product: uploaded package is sole authority.",
    ])

    invariants = structured_video_prompt_semantic_invariants(source)

    assert "0-4s product package visible" in invariants
    assert all("bottle" not in item for item in invariants)


def test_doubao_actual_payload_preserves_exact_timed_states_and_quantities() -> None:
    source = "\n".join([
        "Visual style (signed whole-video contract): adult stylized animation.",
        (
            "Reference bindings: @image1=character+scene+action; "
            "@image2,@image3=character+action+scene; @image4=package"
        ),
        (
            "Timeline (this segment only): "
            "0-3s: cool-blue bedroom, woman holds a glowing phone and looks "
            "tired; physical clock reads 1:43 and mechanical tally counter "
            "reads 43 | 3-6s: she makes one deliberate choice, places the "
            "phone face-down on the bedside; room changes from cool-blue "
            "phone light to warm light | 6-9s: she holds the uploaded bottle "
            "in a warm setting and presents exactly two gummies; end with "
            "the phone remaining face-down"
        ),
        (
            "Product presentation policy: uploaded package is the sole "
            "authority; integrate it naturally in the scene."
        ),
        "Audio: visible characters remain silent with no lip-sync.",
        "Output: 9:16; no generated text, UI, or watermark.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(
        source,
        compacted,
        required_reference_aliases=(
            "@image1",
            "@image2",
            "@image3",
            "@image4",
        ),
        product_required=True,
    )

    assert len(compacted) <= 495
    assert "clock reads 1:43" in compacted
    assert "tally reads 43" in compacted
    assert "exactly two gummies" in compacted
    assert compacted.count("phone face-down") >= 2
    assert "reads 1 |" not in compacted
    assert "gummies it" not in compacted
    assert "..." not in compacted
    assert contract["validated"] is True


def test_provider_payload_fidelity_rejects_silent_semantic_damage() -> None:
    source = (
        "Beats: 0-3s: physical clock reads 1:43 and tally reads 43 | "
        "3-6s: phone face-down | 6-9s: exactly two gummies"
    )
    broken = (
        "Refs: @image1,@image2.\n"
        "Beats: 0-3s: clock reads 1 | 3-6s: phone | 6-9s: gummies it"
    )

    with pytest.raises(ValueError, match="not semantically lossless"):
        validate_structured_video_prompt_fidelity(
            source,
            broken,
            required_reference_aliases=("@image1", "@image2"),
        )


def test_doubao_chinese_provider_beats_survive_small_prompt_compaction() -> None:
    source = "\n".join([
        "Refs: @image1=package",
        (
            "Beats: 0-2.5s: 手机清晰显示1:43和已刷43条；女人惊觉；产品不出现 | "
            "2.5-5.5s: 手机已扣在床头；双手离开；产品不出现 | "
            "5.5-9s: 硬切三态：产品瓶；恰好两颗软糖；最后手指明确向下"
        ),
        "Product: uploaded package is sole authority. In-scene; no white background/packshot.",
        "Audio: no speech/lip-sync; 9:16 stylized animation; no text/UI/watermark.",
        "Continuity: " + ("preserve the approved animated state; " * 10),
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(source, compacted)

    assert len(compacted) <= 495
    assert "0-2.5s:" in compacted
    assert "2.5-5.5s:" in compacted
    assert "5.5-9s:" in compacted
    assert "手机已扣在床头" in compacted
    assert "恰好两颗软糖" in compacted
    assert "最后手指明确向下" in compacted
    assert "产品不出现" in compacted
    assert "2.5-5.5s:  |" not in compacted
    assert contract["validated"] is True


def test_provider_payload_fidelity_rejects_missing_chinese_timed_beat() -> None:
    source = (
        "Beats: 0-2.5s: 女人看到异常时间 | "
        "2.5-5.5s: 手机扣到床头 | 5.5-9s: 产品自然进入场景"
    )
    broken = "Beats: 0-2.5s: 女人看到异常时间 | 5.5-9s: 产品自然进入场景"

    with pytest.raises(ValueError, match="missing timed actions: 2.5-5.5s"):
        validate_structured_video_prompt_fidelity(source, broken)


def test_seedance_native_voice_keeps_multimodal_beats_over_legacy_motion() -> None:
    source = "\n".join([
        "Visual style (signed whole-video contract): stylized 2.5D adult animation.",
        (
            "Timeline (this segment only): "
            "0-2.3s: 穿外套动作停住；归还工牌；手套落在长凳 | "
            "2.3-4.8s: 同事递出MYUPONA罐；主角接过；旁白同步 | "
            "4.8-7s: 开罐；取一小量；轻柔按摩前臂；提包离开"
        ),
        (
            "Motion and effects: obsolete rack-focus prose that must not "
            "replace the newer multimodal execution timeline."
        ),
        (
            "Dialogue: 'Long shift and ready for a body-care reset? This is "
            "MYUPONA Soothing Body Balm—tap the product card.'"
        ),
        "Voice lock for this segment: same female off-screen narrator, General American English.",
        "Product presentation policy: uploaded package is the sole authority.",
        "Output: 9:16 720p; no captions, UI, or watermark.",
    ])

    first_pass = compact_structured_video_prompt(source, max_characters=495)
    with_bindings = first_pass + "\nReference bindings: @image1=action; @image2=package"
    actual = compact_structured_video_prompt(
        with_bindings,
        max_characters=495,
    )
    contract = validate_structured_video_prompt_fidelity(
        source,
        actual,
        required_reference_aliases=("@image1", "@image2"),
        product_required=True,
    )

    assert len(actual) <= 495
    assert "0-2.3s:" in actual
    assert "2.3-4.8s:" in actual
    assert "4.8-7s:" in actual
    assert "同事递出MYUPONA罐" in actual
    assert "轻柔按摩前臂" in actual
    assert "obsolete rack-focus" not in actual
    assert "Product: uploaded package is sole authority." in actual
    assert "@image1" in actual and "@image2" in actual
    assert "..." not in actual
    assert contract["validated"] is True


def test_seedance_small_prompt_compaction_never_invents_truncation_ellipsis() -> None:
    source = "\n".join([
        "Segment 1: " + ("fast emotional visual hook with tactile motion " * 8),
        "Visual style (signed whole-video contract): "
        + ("warm adult animation with brisk handheld rhythm " * 8),
        "Reference bindings: @image1=character+scene; @image2=package",
        (
            "Timeline (this segment only): "
            "0-2s: 她在卷垫旁突然停住，镜头快速推近疲惫表情 | "
            "2-5s: 她放下手机并打开产品罐，动作清晰连贯 | "
            "5-7s: 她取少量涂抹前臂，表情从紧绷转为放松"
        ),
        "Dialogue: 'Long day? I keep this simple body-care reset close by.'",
        "Voice lock for this segment: same female off-screen narrator, "
        + ("General American English with warm expressive delivery " * 6),
        "Product presentation policy: uploaded package is the sole authority.",
        "Output: 9:16 720p; no captions, UI, or watermark.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(
        source,
        compacted,
        required_reference_aliases=("@image1", "@image2"),
        product_required=True,
    )

    assert len(compacted) <= 495
    assert "..." not in compacted
    assert all(label in compacted for label in ("0-2s:", "2-5s:", "5-7s:"))
    assert contract["validated"] is True


def test_seedance_reference_lane_carries_only_anchor_handles_not_story_prose() -> None:
    source = "\n".join([
        (
            "Reference bindings: @image1=character+scene, malformed action "
            "description must not direct the story...; @image2=package"
        ),
        (
            "Timeline (this segment only): 0-2s: 她快速转身看向镜头 | "
            "2-5s: 她放下手机并自然拿起产品 | "
            "5-7s: 镜头推近产品后回到放松表情"
        ),
        "Dialogue: 'This is the simple reset I keep close after class.'",
        "Voice: same female off-screen narrator; US accent.",
        "Product: uploaded package is sole authority.",
        "Output: 9:16; no captions, UI, or watermark.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert "@image1" in compacted and "@image2" in compacted
    assert "malformed action description" not in compacted
    assert "..." not in compacted
    assert all(label in compacted for label in ("0-2s:", "2-5s:", "5-7s:"))


def test_seedance_preserves_intentional_dialogue_ellipsis_verbatim() -> None:
    source = "\n".join([
        "Timeline (this segment only): 0-4s: phone turns face-down",
        "Dialogue: 'One more video... was forty videos ago.'",
        "Voice: same female visible protagonist; US accent.",
        "Output: 9:16; no captions, UI, or watermark.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(source, compacted)

    assert "One more video... was forty videos ago." in compacted
    assert contract["validated"] is True


def test_seedance_native_voice_recompacts_existing_beats_and_product_lanes() -> None:
    source = "\n".join([
        "Reference bindings: @image1=package",
        (
            "Beats: 0-1.8s: 卷垫旁停顿; 拿起已打开的罐子; 一根手指取少量 | "
            "1.8-4.8s: 硬切至完整外侧肩部; 点上同一小量; 轻柔画圈按摩 | "
            "4.8-7s: 硬切至平静面部; 手离开肩部; 伸向折叠毛巾"
        ),
        (
            "Dialogue: 'use a small amount where you want massage comfort. "
            "Massage gently until absorbed and enjoy the cooling-and-warming feel.'"
        ),
        "Voice: same female off-screen narrator; US accent.",
        "Product: uploaded package is sole authority. In-scene; no white background/packshot.",
        "9:16; no captions/UI/watermark; segment 2/3.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(
        source,
        compacted,
        required_reference_aliases=("@image1",),
        product_required=True,
    )

    assert len(compacted) <= 495
    assert all(label in compacted for label in ("0-1.8s:", "1.8-4.8s:", "4.8-7s:"))
    assert "Product: uploaded package is sole authority." in compacted
    assert "@image1" in compacted
    assert "..." not in compacted
    assert contract["validated"] is True


def test_doubao_local_voiceover_budget_keeps_real_hook_and_product_actions() -> None:
    source = "\n".join([
        (
            "Visual style (signed whole-video contract): Original adult "
            "stylized 2D/2.5D animation with blue-purple accents."
        ),
        (
            "Reference bindings: @image1=action+scene+character; "
            "@image2,@image3,@image4=scene+action+character"
        ),
        (
            "Timeline (this segment only): "
            "0-2.87s: Her thumb keeps scrolling while an oversized phone "
            "portal pulls luminous shards into the room; FX: rapid scale "
            "distortion and multiplying text-free shards; Cam: fast push-in | "
            "2.87-4.98s: Her alarmed tired eyes look toward the promised dawn "
            "walk; FX: the shard storm abruptly slows at realization; "
            "Cam: frontal close-up | "
            "4.98-9s: She places the phone face-down outside reach and opens "
            "the ceramic tray; FX: portal light contracts and the hand "
            "completes the action; Cam: stable bedside framing"
        ),
        (
            "Audio: visible characters remain silent with no lip-sync; exact "
            "signed voiceover is added locally."
        ),
        "Output: 9:16 720p.",
        "Segment scope: 1/2 only.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert len(compacted) <= 495
    assert all(f"@image{index}" in compacted for index in range(1, 5))
    assert "phone portal" in compacted
    assert "luminous shards" in compacted
    assert "shard storm abruptly slows" in compacted
    assert "phone-face-down" in compacted or "phone face-down" in compacted
    assert "portal light contracts" in compacted.lower()
    assert "stylized animation" in compacted
    assert "no speech/lip-sync" in compacted
    assert "..." not in compacted


def test_doubao_local_voiceover_product_reveal_keeps_package_role_and_count() -> None:
    source = "\n".join([
        (
            "Visual style (signed whole-video contract): Original adult "
            "stylized 2D/2.5D animation with warm amber light."
        ),
        (
            "Reference bindings: @image1=action+scene; "
            "@image2=scene+action+character; @image3=package"
        ),
        (
            "Timeline (this segment only): "
            "0-1.98s: Her hand pauses above unbranded gummies while the "
            "product remains out of frame; FX: slow rack focus and gentle "
            "amber reflection; Cam: tray close-up | "
            "1.98-5.87s: The MYUPONA blue bottle with purple label and front "
            "Melatonin-free marking enters; FX: restrained fade-in with no "
            "floating package; Cam: clean reveal | "
            "5.87-9s: She takes exactly two gummies while the MYUPONA bottle "
            "remains secondary; FX: each action lands and a final amber pulse "
            "settles; Cam: stable tabletop"
        ),
        (
            "Product presentation policy: use the uploaded MYUPONA package "
            "as the sole package authority."
        ),
        (
            "Audio: visible characters remain silent with no lip-sync; exact "
            "signed voiceover is added locally."
        ),
        "Output: 9:16 720p.",
        "Segment scope: 2/2 only.",
    ])

    compacted = compact_structured_video_prompt(source, max_characters=495)

    assert len(compacted) <= 495
    assert "@image3=package" in compacted
    assert "no product visible" in compacted
    assert "MYUPONA blue bottle" in compacted
    assert "Melatonin-free" in compacted
    assert "exactly two gummies" in compacted
    assert "Product: @image3 sole package authority." in compacted
    assert "MYUPO" not in compacted.replace("MYUPONA", "")
    assert "..." not in compacted


def test_doubao_local_voiceover_packet_is_idempotent_after_reference_injection() -> None:
    source = "\n".join([
        (
            "Visual style (signed whole-video contract): Original stylized "
            "adult animation."
        ),
        (
            "Timeline (this segment only): 0-4s: phone portal pulls luminous "
            "shards; FX: rapid scale distortion; Cam: fast push-in | "
            "4-9s: phone turns face-down; FX: portal glow contracts; "
            "Cam: stable bedside framing"
        ),
        (
            "Audio: visible characters remain silent with no lip-sync; exact "
            "signed voiceover is added locally."
        ),
        "Continuity: " + ("preserve the approved animated state; " * 12),
        "Output: 9:16 720p.",
    ])

    first_pass = compact_structured_video_prompt(
        source,
        max_characters=495,
    )
    second_pass = compact_structured_video_prompt(
        first_pass
        + "\nReference bindings: @image1=character+scene; @image2=action"
        + "\nSegment scope: 1/2 only."
        + "\nContinuity: " + ("do not preplay a later segment; " * 12),
        max_characters=495,
    )

    assert len(second_pass) <= 495
    assert "Audio:" in first_pass
    assert "@image1" in second_pass
    assert "@image2" in second_pass
    assert "phone portal" in second_pass
    assert "phone turns face-down" in second_pass
    assert "rapid scale distortion" in second_pass
    assert "portal glow contracts" in second_pass
    assert "no speech/lip-sync" in second_pass


def test_doubao_recompacts_ultra_lean_content_factory_packet_for_transport_prefix() -> None:
    source = "\n".join([
        (
            "Reference bindings: @image1=action+scene+character; "
            "@image2,@image3,@image4=scene+action+character"
        ),
        (
            "Motion and effects: Rapid scale distortion and multiplying "
            "text-free shards build pressure, snap toward her eyes, then "
            "abruptly slow at the transition."
        ),
        (
            "Dialogue: 'One more video... was 43 videos ago.' | "
            "'There goes my morning walk.' | "
            "'Phone down. I’m starting my bedtime routine with a gummy.'"
        ),
        "Voice: same female visible protagonist; US accent.",
        "9:16; no captions/UI/watermark; segment 1/2.",
    ])
    source = source + (" " * (500 - len(source)))

    assert len(source) == 500
    assert is_structured_video_prompt(source) is True

    request = doubao_tasks._request(
        {"prompt": source, "seconds": 10, "aspect_ratio": "9:16"},
        KieTask(model="seedance_2_0_mini", prompt=None),
    )

    assert len(request["prompt"]) <= 495
    assert all(f"@image{index}" in request["prompt"] for index in range(1, 5))
    assert "Rapid scale distortion" in request["prompt"]
    assert "One more video... was 43 videos ago." in request["prompt"]
    assert "Phone down. I’m starting my bedtime routine with a gummy." in request["prompt"]


def test_doubao_provider_preserves_freeform_prompt_up_to_verified_limit() -> None:
    source = "x" * 495
    assert is_structured_video_prompt(source) is False

    request = doubao_tasks._request(
        {"prompt": source, "seconds": 10, "aspect_ratio": "9:16"},
        KieTask(model="seedance_2_0_mini", prompt=None),
    )

    assert request["prompt"] == source
    assert request["duration"] == 10
    assert request["ratio"] == "9:16"


def test_free_lane_membership_card_is_normalized_to_account_capacity():
    free_error = client.DoubaoProviderError(
        "upgrade required",
        code="doubao_membership_required",
    )

    normalized = doubao_tasks._normalize_account_submit_error(
        free_error,
        requested_duration=7,
    )
    enhanced_only = doubao_tasks._normalize_account_submit_error(
        free_error,
        requested_duration=12,
    )

    assert normalized.code == "doubao_quota_exhausted"
    assert enhanced_only is free_error


def test_task_3161_success_prompt_stays_unchanged_before_browser_transport() -> None:
    source = "生成@image2介绍@image1的视频，中文。"
    request = doubao_tasks._request(
        {"prompt": source, "seconds": 10, "aspect_ratio": "9:16"},
        KieTask(model="seedance_2_0_mini", prompt=None),
    )
    assert request == {"prompt": source, "duration": 10, "ratio": "9:16"}


def test_doubao_provider_rejects_freeform_prompt_over_verified_limit_once() -> None:
    source = "x" * 496

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        doubao_tasks._request(
            {"prompt": source, "seconds": 10, "aspect_ratio": "9:16"},
            KieTask(model="seedance_2_0_mini", prompt=None),
        )

    assert exc_info.value.code == "doubao_prompt_too_long"
    assert video_tasks._provider_error_is_terminal_request_rejection(exc_info.value)


def test_unconfirmed_doubao_submit_resets_same_task_for_retry(monkeypatch) -> None:
    task = KieTask(
        id=3145,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        task_id="local-ai-video-original",
        state="queued_local",
        input_json={"prompt": "portrait scene", "seconds": 7, "aspect_ratio": "9:16"},
        result_json={"__local": {}},
    )
    account = SimpleNamespace(
        id=1230,
        bridge_id="br_test",
        cdp_url="http://127.0.0.1:9392",
        meta_json={},
    )
    commits: list[str] = []
    resets: list[str] = []

    class FakeDb:
        def add(self, _row):
            return None

        def flush(self):
            return None

        def commit(self):
            commits.append(str(task.state))

        def get(self, model, row_id):
            if model is KieTask and int(row_id) == 3145:
                return task
            return account

    def fake_reset(_db, *, task, retry_kind):
        resets.append(retry_kind)
        task.task_id = "local-ai-video-retry"
        task.state = "queued_local"
        task.result_json = {"__local": {}}
        return task

    monkeypatch.setattr(doubao_tasks, "_reference_paths", lambda _db, _task: [])
    monkeypatch.setattr(doubao_tasks, "claim_account", lambda *_args, **_kwargs: account)
    monkeypatch.setattr(
        doubao_tasks,
        "_wait_for_provider_browser_generation",
        lambda *_args, **_kwargs: account,
    )
    monkeypatch.setattr(doubao_tasks, "account_request_payload", lambda *_args: {})
    monkeypatch.setattr(
        doubao_tasks, "_ensure_live_video_composer", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(doubao_tasks, "release_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(doubao_tasks, "reset_video_task_for_retry", fake_reset)
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            client.DoubaoProviderError(
                "network unavailable",
                code="doubao_helper_runtime_failed",
            )
        ),
    )

    with pytest.raises(client.DoubaoProviderError):
        asyncio.run(doubao_tasks.submit_doubao_task(FakeDb(), task=task))

    assert resets == ["provider_submit_unconfirmed"]
    assert task.task_id == "local-ai-video-retry"
    assert task.state == "queued_local"
    assert task.result_json["__local"]["provider_submit_unconfirmed_code"] == (
        "doubao_helper_runtime_failed"
    )
    assert commits[-1] == "queued_local"


def test_redelivered_doubao_local_marker_reopens_submit_instead_of_polling(
    monkeypatch,
) -> None:
    task = KieTask(
        id=3310,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        task_id="doubao-local-3310",
        state="submitting",
        input_json={"service_provider": "doubao"},
        result_json={
            "__local": {
                "active_provider": "doubao",
                "doubao_account_bridge_id": "br_abandoned",
            },
        },
    )
    released: list[str] = []
    reset_kinds: list[str] = []

    class FakeDb:
        def add(self, _row):
            return None

        def commit(self):
            return None

    def fake_reset(_db, *, task, retry_kind):
        reset_kinds.append(retry_kind)
        task.task_id = "local-ai-video-recovered"
        task.state = "queued_local"
        task.result_json = {"__local": {"active_provider": "doubao"}}
        return task

    monkeypatch.setattr(
        video_tasks,
        "release_doubao_task_account",
        lambda _db, *, task, error_code: released.append(error_code),
    )
    monkeypatch.setattr(video_tasks, "reset_video_task_for_retry", fake_reset)

    assert video_tasks._is_unconfirmed_doubao_submit_marker(task) is True
    recovered = video_tasks._recover_unconfirmed_doubao_submit(FakeDb(), task)

    assert recovered.task_id == "local-ai-video-recovered"
    assert recovered.state == "queued_local"
    assert released == ["doubao_submit_unconfirmed"]
    assert reset_kinds == ["provider_submit_unconfirmed_recovery"]
    assert recovered.result_json["__local"][
        "doubao_submit_unconfirmed_recovered_at"
    ]


def test_periodic_stale_recovery_includes_unconfirmed_submit_markers() -> None:
    source = Path(video_tasks.__file__).read_text(encoding="utf-8")
    function = source[source.index("def recover_stale_ai_video_polling") :]

    assert '"submitting"' in function
    recovery = function.index("if _is_unconfirmed_doubao_submit_marker(task):")
    dispatch = function.index("submit_and_poll_ai_video_task.apply_async(")
    assert recovery < dispatch


def test_exhausted_account_rotation_resets_unconfirmed_submit(monkeypatch) -> None:
    task = KieTask(
        id=3178,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        task_id="local-ai-video-original",
        state="queued_local",
        input_json={"prompt": "portrait scene", "seconds": 4, "aspect_ratio": "9:16"},
        result_json={"__local": {}},
    )
    account = SimpleNamespace(
        id=1239,
        bridge_id="br_captcha",
        cdp_url="http://127.0.0.1:9392",
        meta_json={},
    )
    claims = 0
    resets: list[str] = []

    class FakeDb:
        def add(self, _row):
            return None

        def flush(self):
            return None

        def commit(self):
            return None

        def get(self, model, row_id):
            if model is KieTask and int(row_id) == 3178:
                return task
            return account

    def fake_claim(*_args, **_kwargs):
        nonlocal claims
        claims += 1
        if claims == 1:
            return account
        raise RuntimeError("no other verified account is ready")

    def fake_reset(_db, *, task, retry_kind):
        resets.append(retry_kind)
        task.task_id = "local-ai-video-retry"
        task.state = "queued_local"
        task.result_json = {"__local": {}}
        return task

    monkeypatch.setattr(doubao_tasks, "_reference_paths", lambda _db, _task: [])
    monkeypatch.setattr(doubao_tasks, "claim_account", fake_claim)
    monkeypatch.setattr(
        doubao_tasks,
        "_wait_for_provider_browser_generation",
        lambda *_args, **_kwargs: account,
    )
    monkeypatch.setattr(doubao_tasks, "account_request_payload", lambda *_args: {})
    monkeypatch.setattr(
        doubao_tasks, "_ensure_live_video_composer", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(doubao_tasks, "release_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(doubao_tasks, "reset_video_task_for_retry", fake_reset)
    monkeypatch.setattr(
        doubao_tasks,
        "invoke_doubao_helper",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            client.DoubaoProviderError(
                "captcha required",
                code="doubao_captcha_required",
            )
        ),
    )

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        asyncio.run(doubao_tasks.submit_doubao_task(FakeDb(), task=task))

    assert exc_info.value.code == "doubao_pool_unavailable"
    assert "CAPTCHA" not in str(exc_info.value)
    assert "切换" not in str(exc_info.value)
    assert claims == 2
    assert resets == ["provider_submit_unconfirmed"]
    assert task.task_id == "local-ai-video-retry"
    assert task.state == "queued_local"
    assert task.result_json["__local"]["provider_submit_unconfirmed_code"] == (
        "doubao_captcha_required"
    )
    assert task.result_json["__local"]["provider_submit_accounts_exhausted"] == 1


def test_late_poll_delivery_cannot_revive_terminal_doubao_rejection(monkeypatch) -> None:
    task = KieTask(
        id=3049,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        task_id="doubao:late-poll",
        state="queued",
        fail_code="doubao_membership_required",
        result_json={
            "__local": {
                "poll_owner_task_id": "late-worker",
                "doubao_account_bridge_id": "br_test",
            }
        },
    )
    released: list[str] = []
    monkeypatch.setattr(
        video_tasks,
        "release_doubao_task_account",
        lambda _db, *, task, error_code: released.append(error_code),
    )
    monkeypatch.setattr(
        video_tasks,
        "restore_archived_task_result_files",
        lambda *_args, **_kwargs: False,
    )
    db = SimpleNamespace(add=lambda _row: None, flush=lambda: None)

    video_tasks._seal_terminal_request_rejection(db, task)

    assert task.state == "failed"
    assert task.result_json["__local"].get("poll_owner_task_id") is None
    assert released == ["doubao_membership_required"]


def _doubao_reference_task(db_session, *, count: int) -> KieTask:
    task = KieTask(
        workspace_id=3,
        created_by_user_id=7,
        key_id=12,
        model="seedance_2_0_mini",
        task_id=f"local-doubao-{count}-refs",
        state="queued_local",
        input_json={"model": "seedance_2_0_mini"},
        result_json={},
    )
    db_session.add(task)
    db_session.flush()
    for index in range(count):
        db_session.add(KieFile(
            workspace_id=3,
            key_id=12,
            task_id=task.id,
            kind="reference_upload",
            file_url=f"/managed/reference-{index}.png",
        ))
    db_session.flush()
    return task


def test_doubao_provider_accepts_ten_reference_records(
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    task = _doubao_reference_task(db_session, count=10)
    rows = (
        db_session.query(KieFile)
        .filter(KieFile.task_id == task.id)
        .order_by(KieFile.id.asc())
        .all()
    )
    for index, row in enumerate(rows):
        path = tmp_path / f"reference-{index}.png"
        path.write_bytes(f"distinct-reference-{index}".encode())
        row.file_url = str(path)
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "get_local_path",
        lambda row: Path(str(row.file_url)),
    )

    assert len(doubao_tasks._reference_paths(db_session, task)) == 10


def test_doubao_provider_uses_task_reference_order_not_database_id_order(
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    task = _doubao_reference_task(db_session, count=3)
    rows = (
        db_session.query(KieFile)
        .filter(KieFile.task_id == task.id)
        .order_by(KieFile.id.asc())
        .all()
    )
    paths = []
    for index, row in enumerate(rows, start=1):
        path = tmp_path / f"reference-{index}.png"
        path.write_bytes(f"distinct-{index}".encode())
        row.file_url = str(path)
        paths.append(str(path))
    task.input_json = {
        "model": "seedance_2_0_mini",
        "reference_file_paths": [
            {"path": paths[2]},
            {"path": paths[0]},
            {"path": paths[1]},
        ],
    }
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "get_local_path",
        lambda row: Path(str(row.file_url)),
    )

    assert doubao_tasks._reference_paths(db_session, task) == [
        paths[2],
        paths[0],
        paths[1],
    ]


def test_doubao_provider_rejects_duplicate_reference_content(
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    task = _doubao_reference_task(db_session, count=2)
    rows = (
        db_session.query(KieFile)
        .filter(KieFile.task_id == task.id)
        .order_by(KieFile.id.asc())
        .all()
    )
    for index, row in enumerate(rows, start=1):
        path = tmp_path / f"duplicate-{index}.png"
        path.write_bytes(b"same-image")
        row.file_url = str(path)
    db_session.flush()
    monkeypatch.setattr(
        doubao_tasks,
        "get_local_path",
        lambda row: Path(str(row.file_url)),
    )

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        doubao_tasks._reference_paths(db_session, task)

    assert exc_info.value.code == "doubao_reference_duplicate"


def test_doubao_provider_rejects_eleven_reference_records(db_session) -> None:
    task = _doubao_reference_task(db_session, count=11)

    with pytest.raises(client.DoubaoProviderError, match="最多支持 10 张"):
        doubao_tasks._reference_paths(db_session, task)


def test_doubao_provider_rejects_square_result_for_portrait_request() -> None:
    task = KieTask(input_json={"aspect_ratio": "9:16"})

    with pytest.raises(client.DoubaoProviderError) as exc_info:
        doubao_tasks._validate_output_aspect(
            task,
            {"width": 960, "height": 960},
        )

    assert exc_info.value.code == "doubao_output_aspect_mismatch"
    assert "要求 9:16" in str(exc_info.value)


def test_doubao_provider_accepts_portrait_result_with_small_rounding_error() -> None:
    task = KieTask(input_json={"aspect_ratio": "9:16"})

    doubao_tasks._validate_output_aspect(
        task,
        {"width": 720, "height": 1278},
    )


def test_current_web_helper_repeats_ratio_in_visible_generation_instruction() -> None:
    source = Path("/opt/apps/doubao2api-lab/doubao2api/client.py").read_text(
        encoding="utf-8"
    )
    function = source[
        source.index("async def generate_video_current(") :
        source.index("async def fetch_generated_videos(")
    ]

    assert '"ratio": ratio' in function
    assert "画面比例 {ratio}" in function
    assert "禁止自适应比例或方形裁切" in function


def test_pending_doubao_poll_handoff_releases_owner_and_requeues(monkeypatch) -> None:
    task = KieTask(
        id=3044,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        task_id="doubao:remote-task",
        state="queued",
        result_json={
            "__local": {
                "active_provider": "doubao",
                "poll_owner_task_id": "old-worker",
                "poll_handoff_count": 2,
            }
        },
    )
    db = SimpleNamespace(add=lambda row: None, commit=lambda: None)
    queued: list[dict] = []
    monkeypatch.setattr(
        video_tasks.submit_and_poll_ai_video_task,
        "apply_async",
        lambda **kwargs: queued.append(kwargs),
    )

    payload = video_tasks._handoff_doubao_poll(
        db,
        task,
        workspace_id=3,
        local_task_id=3044,
        interval_seconds=15,
        timeout_seconds=600,
    )

    meta = task.result_json["__local"]
    assert payload["state"] == "queued"
    assert meta.get("poll_owner_task_id") is None
    assert meta["poll_handoff_count"] == 3
    assert len(queued) == 1
    assert queued[0]["countdown"] == 15
    assert queued[0]["queue"] == "gmv.tasks.ai_video.browser_poll"
    assert queued[0]["kwargs"]["local_task_id"] == 3044


def test_submit_poll_loop_hands_pending_doubao_to_fresh_delivery() -> None:
    source = Path(video_tasks.__file__).read_text(encoding="utf-8")
    function = source[
        source.index("def submit_and_poll_ai_video_task") :
        source.index("def recover_stale_ai_video_polling")
    ]

    handoff = function.index("return _handoff_doubao_poll(")
    sleeper = function.index("time.sleep(max(5, int(interval_seconds)))")
    assert handoff < sleeper


def test_doubao_remote_wait_deadline_survives_poll_handoffs(monkeypatch) -> None:
    monkeypatch.setattr(video_tasks.settings, "DOUBAO_POLL_TIMEOUT_SECONDS", 1800)
    accepted_at = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    task = KieTask(
        id=3047,
        workspace_id=3,
        key_id=12,
        model="seedance_2_0_mini",
        task_id="doubao:remote-task",
        state="queued",
        result_json={
            "__local": {
                "active_provider": "doubao",
                "doubao_remote_accepted_at": accepted_at.isoformat(),
                "poll_handoff_count": 99,
            }
        },
    )

    assert not video_tasks._doubao_remote_wait_expired(
        task,
        timeout_seconds=600,
        now=accepted_at + timedelta(seconds=1799),
    )
    assert video_tasks._doubao_remote_wait_expired(
        task,
        timeout_seconds=600,
        now=accepted_at + timedelta(seconds=1800),
    )


def test_provider_retry_respects_global_auto_retry_budget() -> None:
    task = KieTask(
        model="seedance_2_0_mini",
        state="timeout",
        fail_code="provider_poll_timeout",
        result_json={
            "__local": {
                "active_provider": "doubao",
                "auto_retry_count": video_tasks.MAX_AUTO_RETRIES,
                "provider_retry_counts": {"doubao": 1},
            }
        },
    )

    assert video_tasks._provider_retry_count(task) == 1
    assert video_tasks._should_retry_provider(task) is False

def test_doubao_risk_rate_limit_rotates_to_another_pool_account() -> None:
    assert "doubao_risk_rate_limited" in doubao_tasks._ROTATE_ACCOUNT_ERROR_CODES


def test_doubao_region_restriction_rotates_to_another_pool_account() -> None:
    assert "doubao_region_restricted" in doubao_tasks._ROTATE_ACCOUNT_ERROR_CODES
    assert "doubao_browser_unstable" in doubao_tasks._ROTATE_ACCOUNT_ERROR_CODES
    assert "doubao_composer_unavailable" in doubao_tasks._ROTATE_ACCOUNT_ERROR_CODES
    assert "doubao_submit_unconfirmed" in doubao_tasks._ROTATE_ACCOUNT_ERROR_CODES


def test_doubao_submission_contract_accepts_only_verified_ai_creation_video() -> None:
    actual = doubao_tasks._sanitized_submission_contract(
        {
            "submission_contract": {
                "surface": "ai_creation",
                "ability_type": 17,
                "model": "seedance_v2.0_mini",
                "ratio": "9:16",
                "duration": 6,
                "reference_count": 2,
                "prompt": "must not be persisted",
            }
        }
    )

    assert actual == {
        "surface": "ai_creation",
        "ability_type": 17,
        "model": "seedance_v2.0_mini",
        "ratio": "9:16",
        "duration": 6,
        "reference_count": 2,
    }


def test_doubao_submission_contract_rejects_ordinary_chat() -> None:
    assert doubao_tasks._sanitized_submission_contract(
        {
            "submission_contract": {
                "surface": "chat",
                "ability_type": 0,
                "model": "seedance_v2.0_mini",
                "ratio": "9:16",
                "duration": 6,
            }
        }
    ) is None


def test_seedance_retry_preserves_repair_direction_beats_and_copy_under_495() -> None:
    source = "\n".join([
        "Refs: @image1=action+scene+character; @image2=package",
        "Repair: keep shoulder application continuous across segments; no leg change",
        (
            "Direction: 风格化2D/2.5D；快节奏三段微切，每0.8至1.5秒出现清晰动作或构图变化；"
            "同一肩部由微距切至近景，禁止单一缓慢推进"
        ),
        (
            "Beats: 0-1.8s: 特写快切；打开白色罐，指尖取一小点 | "
            "1.8-4.8s: 肩部近景；将少量 balm 点在完整肩肤并画圈推匀 | "
            "4.8-7s: 切至同一肩肤吸收且无明显残留"
        ),
        (
            "Dialogue: 'use a small amount where you want massage comfort. "
            "Massage gently until absorbed and enjoy the cooling-and-warming feel.'"
        ),
        "Voice: same female off-screen narrator; US accent; 155 words per minute.",
        "Product: uploaded package is sole authority.",
        "9:16; this segment only; no text/UI/watermark.",
    ])

    actual = compact_structured_video_prompt(source, max_characters=495)
    localized = localize_structured_video_prompt_for_doubao(actual)
    contract = validate_structured_video_prompt_fidelity(
        source,
        actual,
        required_reference_aliases=("@image1", "@image2"),
        product_required=True,
    )

    assert len(actual) <= 495
    assert len(localized) <= 495
    assert "Repair:" in actual
    assert "shoulder application" in actual
    assert "no leg change" in actual
    assert "Direction:" in actual
    assert "2D/2.5D" in actual
    assert "0.8至1.5秒" in actual
    assert "微距切至近景" in actual
    assert all(label in actual for label in ("0-1.8s:", "1.8-4.8s:", "4.8-7s:"))
    assert (
        "'use a small amount where you want massage comfort. Massage gently "
        "until absorbed and enjoy the cooling-and-warming feel.'"
    ) in actual
    assert "155 wpm" in actual
    assert "节奏镜头风格：" in localized
    assert contract["validated"] is True


def test_seedance_identity_repair_compacts_to_executable_reference_lock() -> None:
    source = "\n".join([
        "Refs: @image1=character+scene",
        (
            "Repair: Regenerate only segment 5, preserving the exact recurring woman "
            "from segment 4: same face proportions, brown shoulder-length hair, age, "
            "lavender sleepwear wardrobe, 2.5D animated medium, deep-blue bedroom "
            "setting, bedside lighting and open-palm gesture."
        ),
        "Direction: 四个快切镜头；每1.5秒改变景别；禁止单一缓慢推进",
        (
            "Beats: 0-1.5s: 同一女性扣下手机 | 1.5-3s: 面部近景 | "
            "3-6s: 床头中景并张开手掌"
        ),
        "Dialogue: 'What tells your brain work is over? Share your cue below.'",
        "Voice: same female visible protagonist; US accent; 150 words per minute.",
        "9:16; this segment only; no text/UI/watermark.",
    ])

    actual = compact_structured_video_prompt(source, max_characters=495)

    assert len(actual) <= 495
    assert (
        "Repair: match @image1 exactly: face, hair, age, wardrobe, room, "
        "lighting, medium"
    ) in actual
    assert "Dialogue: 'What tells your brain work is over? Share your cue below.'" in actual
    assert "150 wpm" in actual


def test_seedance_dense_retry_shrinks_duplicate_prose_not_execution_contract() -> None:
    source = "\n".join([
        "Refs: @image1=action+scene+character; @image2=package",
        (
            "Repair: preserve the same shoulder application and absorption state "
            "from the prior segment; keep the same pose wardrobe studio product "
            "placement and do not change to leg application"
        ),
        (
            "Direction: 快节奏三拍细节切换；罐身微距硬切肩手紧特写，再硬切稳定产品近景；"
            "明亮风格化2D/3D普拉提画面；每一拍都有清晰构图变化，禁止单一缓慢推进"
        ),
        (
            "Beats: 0-1.8s: 同一女性在垫旁打开白色罐并取少量 | "
            "1.8-4.8s: 肩手紧特写，点在完整肩肤并画圈推匀 | "
            "4.8-7s: 切吸收细节，再切稳定产品近景"
        ),
        (
            "Dialogue: 'use a small amount where you want massage comfort. "
            "Massage gently until absorbed and enjoy the cooling-and-warming feel.'"
        ),
        "Voice: same female off-screen narrator; US accent; 155 words per minute.",
        "Product: uploaded package is sole authority.",
        "9:16; this segment only; no text/UI/watermark.",
    ])

    actual = compact_structured_video_prompt(source, max_characters=495)
    contract = validate_structured_video_prompt_fidelity(
        source,
        actual,
        required_reference_aliases=("@image1", "@image2"),
        product_required=True,
    )

    assert len(actual) <= 495
    assert all(prefix in actual for prefix in ("Repair:", "Direction:", "Beats:"))
    assert all(label in actual for label in ("0-1.8s:", "1.8-4.8s:", "4.8-7s:"))
    assert "Dialogue: 'use a small amount" in actual
    assert "155 wpm" in actual
    assert "Product: @image2 sole package authority." in actual
    assert "..." not in actual
    assert contract["validated"] is True


def test_manual_verification_helper_prepares_real_seedance_composer_only() -> None:
    source = Path("/opt/apps/doubao2api-lab/scripts/context_generate.py").read_text(
        encoding="utf-8"
    )
    function = source[
        source.index("async def _prepare_manual_video_challenge") :
        source.index("async def _configure_ai_video")
    ]

    assert "await _navigate_ai_creation(page)" in function
    assert "await _open_ai_video_composer(page)" in function
    assert "await _configure_ai_video(page, ratio=ratio, duration=duration)" in function
    assert '"status": "ready_to_submit"' in function
    assert "await _submit_video_composer" not in function
    assert "_manual_text_challenge" not in source
