from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.celery_app import VIDEO_ANALYSIS_TASK_QUEUE, task_modules_for_worker_queue
from app.core.errors import APIError
from app.services import tiktok_shop_video_analysis as service
from app.services.hermes_agent.client import HermesVideoAnalystClient
from app.tasks.tiktok_shop_video_analysis_tasks import _task_result
from app.tasks.tiktok_shop_video_transcript_tasks import classify_whisper_result


def _packet(views: int = 100) -> dict:
    return {
        "scope": {
            "workspace_id": 3,
            "shop_row_id": 4,
            "video_id": "video-1",
            "start_date": "2026-07-01",
            "end_date_exclusive": "2026-07-08",
        },
        "shop_official_metrics": {"views": views},
        "gmv_max_paid_metrics": {"available": True, "view_rate_2s": 0.45},
    }


def test_analysis_cache_key_covers_metrics_media_model_and_prompt(monkeypatch):
    monkeypatch.setattr(service.settings, "HERMES_VIDEO_ANALYSIS_MAX_FRAMES", 8)
    first = service.analysis_cache_key(packet=_packet(), media_fingerprint="media-a")
    assert first == service.analysis_cache_key(packet=_packet(), media_fingerprint="media-a")
    assert first != service.analysis_cache_key(packet=_packet(views=101), media_fingerprint="media-a")
    assert first != service.analysis_cache_key(packet=_packet(), media_fingerprint="media-b")


def test_video_analyst_provider_model_is_toapis():
    assert service.PROVIDER_MODEL == "toapis/gpt-5.4-mini"


def test_video_analysis_queue_imports_only_its_task_module():
    assert task_modules_for_worker_queue(VIDEO_ANALYSIS_TASK_QUEUE) == (
        "app.tasks.tiktok_shop_video_analysis_tasks",
    )


def test_queue_result_never_contains_model_output_or_business_metrics():
    row = SimpleNamespace(
        id=9,
        status="SUCCEEDED",
        analysis_json={"summary": "private"},
        input_summary_json={"gmv": 999},
    )
    assert _task_result(row) == {"analysis_id": 9, "status": "SUCCEEDED"}


def test_contact_sheet_is_bounded_and_caller_can_remove_all_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, "HERMES_VIDEO_ANALYSIS_MAX_FRAMES", 8)
    monkeypatch.setattr(service.settings, "HERMES_VIDEO_ANALYSIS_FFMPEG_TIMEOUT_SECONDS", 30)
    video_path = tmp_path / "sample.mp4"
    output_path = tmp_path / "sheet.jpg"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x240:rate=4:duration=2",
            "-threads", "1", "-pix_fmt", "yuv420p", "-y", str(video_path),
        ],
        check=True,
        timeout=30,
    )
    timestamps, size = service._render_contact_sheet(video_path, output_path)
    assert 1 <= len(timestamps) <= 8
    assert output_path.is_file()
    assert 0 < size <= service.MAX_IMAGE_BYTES
    assert service._data_url(output_path).startswith("data:image/jpeg;base64,")


def test_result_normalization_bounds_untrusted_model_output():
    result = service._normalize_result({
        "summary": "x" * 5000,
        "confidence": 99,
        "strengths": [f"s-{index}" for index in range(30)],
        "problems": [{"issue": "p", "extra": "y" * 1000}] * 20,
        "actions": [{"action": "a"}] * 20,
    })
    assert len(result["summary"]) == 1200
    assert result["confidence"] == 1.0
    assert len(result["strengths"]) == 8
    assert len(result["problems"]) == 8
    assert len(result["actions"]) == 8


def test_provider_percent_rates_are_weighted_normalized_and_ignore_zero_volume():
    rows = [
        SimpleNamespace(ad_video_view_rate_2s=45, product_impressions=121),
        SimpleNamespace(ad_video_view_rate_2s=43.85, product_impressions=133),
        SimpleNamespace(ad_video_view_rate_2s=0, product_impressions=0),
        SimpleNamespace(ad_video_view_rate_2s=100, product_impressions=0),
    ]
    result = service._weighted_provider_percent(
        rows,
        "ad_video_view_rate_2s",
        "product_impressions",
    )
    assert result == pytest.approx(((45 * 121) + (43.85 * 133)) / (121 + 133) / 100)


def test_missing_creative_impression_fields_remain_unavailable_not_zero():
    rows = [SimpleNamespace(impressions=None), SimpleNamespace(impressions=None)]
    assert service._sum_optional(rows, "impressions") is None


def test_whisper_music_markers_are_not_promoted_to_spoken_copy():
    status, reason, segments, text_value = classify_whisper_result({
        "segments": [
            {"index": 0, "start": 0, "end": 4, "text": "[Music]", "no_speech_prob": 0.1},
            {"index": 1, "start": 4, "end": 8, "text": "♪ ♫"},
        ]
    })
    assert (status, reason, segments, text_value) == (
        "NO_SPEECH", "NO_RELIABLE_SPEECH", [], "",
    )


def test_whisper_low_confidence_hallucination_is_not_spoken_copy():
    status, reason, segments, text_value = classify_whisper_result({
        "segments": [{
            "index": 0,
            "start": 0,
            "end": 5,
            "text": "Thanks for watching",
            "no_speech_prob": 0.96,
            "avg_logprob": -1.8,
        }]
    })
    assert status == "NO_SPEECH"
    assert reason == "LOW_SPEECH_CONFIDENCE"
    assert segments == []
    assert text_value == ""


def test_whisper_keeps_timestamped_confident_spoken_copy():
    status, reason, segments, text_value = classify_whisper_result({
        "segments": [{
            "index": 2,
            "start": 1.23456,
            "end": 4.56789,
            "text": "Try one gummy after lunch.",
            "no_speech_prob": 0.08,
            "avg_logprob": -0.22,
        }]
    })
    assert status == "READY"
    assert reason is None
    assert text_value == "Try one gummy after lunch."
    assert segments[0]["start"] == 1.235


def test_video_analyst_client_forces_stateless_requests(monkeypatch):
    client = HermesVideoAnalystClient()
    with pytest.raises(APIError) as exc_info:
        asyncio.run(client.create_response(
            input_text="bounded",
            instructions="json only",
            conversation="forbidden",
        ))
    assert exc_info.value.code == "HERMES_CONTENT_CONTEXT_FORBIDDEN"
