from __future__ import annotations

import asyncio
import subprocess
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.celery_app import VIDEO_ANALYSIS_TASK_QUEUE, task_modules_for_worker_queue
from app.core.errors import APIError
from app.data.models.hermes_agent import HermesContentProducerAttachment
from app.features.tenants.tiktok_shop import router as tiktok_shop_router
from app.data.models.tiktok_shop import TikTokShopVideoContentAnalysis
from app.services import tiktok_shop_video_analysis as service
from app.services import tiktok_shop_video_handoff as handoff
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


def _completed_analysis() -> TikTokShopVideoContentAnalysis:
    return TikTokShopVideoContentAnalysis(
        id=71,
        workspace_id=8,
        account_id=9,
        shop_row_id=10,
        video_id="7659362126863060255",
        cache_key="a" * 64,
        status="SUCCEEDED",
        model_alias="gpt-5.4-mini",
        provider_model="gmv-shop-video-analyst-v1",
        prompt_version="shop-video-analyst-v4-zh-compact",
        metric_start_date=date(2026, 7, 1),
        metric_end_date_exclusive=date(2026, 7, 8),
        transcript_status="READY",
        transcript_language="en",
        transcript_text="Wait, who are you? Sleep debt collection.",
        transcript_segments_json=[{
            "index": 0,
            "start": 0,
            "end": 2,
            "text": "Wait, who are you?",
        }],
        input_summary_json={
            "video": {
                "title": "When my brain will not turn off",
                "creator_username": "myupona6",
                "posted_at": "2026-07-20T14:35:00",
                "products": [{"product_id": "1732408", "name": "Sleep Gummies"}],
            },
            "shop_official_metrics": {
                "views": 630,
                "gmv": 39.88,
                "gpm": 63.30,
                "click_through_rate": 0.083,
                "sku_orders": 1,
                "items_sold": 1,
                "days_present": 7,
                "currency": "USD",
                "latest_report_date": "2026-07-07",
            },
            "gmv_max_paid_metrics": {
                "available": True,
                "product_impressions": 1000,
                "product_clicks": 40,
                "cost": 11.67,
                "gross_revenue": 51.88,
                "orders": 2,
                "roi": 4.45,
                "view_rate_2s": 0.44,
                "view_rate_6s": 0.21,
            },
        },
        analysis_json={
            "summary": "前两秒钩子有效，但商品露出偏晚。",
            "confidence": 0.9,
            "strengths": ["开头冲突明确"],
            "problems": [{"issue": "商品露出偏晚", "metric_evidence": "6秒留存下降"}],
            "actions": [{"priority": "P1", "action": "商品提前到第2秒"}],
            "next_experiment": {"variable": "商品露出时点", "success_metric": "6秒播放率"},
            "limitations": ["仅有日级指标"],
        },
        completed_at=datetime(2026, 7, 8, 1, 2, 3),
        created_at=datetime(2026, 7, 8, 1, 0, 0),
        updated_at=datetime(2026, 7, 8, 1, 2, 3),
    )


def test_analysis_cache_key_covers_metrics_media_model_and_prompt(monkeypatch):
    monkeypatch.setattr(service.settings, "HERMES_VIDEO_ANALYSIS_MAX_FRAMES", 8)
    first = service.analysis_cache_key(packet=_packet(), media_fingerprint="media-a")
    assert first == service.analysis_cache_key(packet=_packet(), media_fingerprint="media-a")
    assert first != service.analysis_cache_key(packet=_packet(views=101), media_fingerprint="media-a")
    assert first != service.analysis_cache_key(packet=_packet(), media_fingerprint="media-b")


def test_video_analyst_persists_the_logical_role_not_a_guessed_provider():
    assert service.PROVIDER_MODEL == "gmv-shop-video-analyst-v1"


def test_video_analysis_report_preserves_official_boundaries_and_evidence():
    report = handoff.render_video_analysis_report(_completed_analysis())
    assert "7659362126863060255" in report
    assert "播放量: 630" in report
    assert "视频 GMV: USD 39.88" in report
    assert "2 秒播放率: 44.00%" in report
    assert "Wait, who are you?" in report
    assert "前两秒钩子有效，但商品露出偏晚" in report
    assert "未匹配到表示未观测到，不按 0 处理" in report


@pytest.mark.anyio
async def test_content_factory_handoff_is_user_scoped_and_idempotent(db_session):
    row = _completed_analysis()
    first = await handoff.create_content_factory_video_handoff(
        db_session,
        row=row,
        user_id=19,
        media=None,
    )
    db_session.commit()
    second = await handoff.create_content_factory_video_handoff(
        db_session,
        row=row,
        user_id=19,
        media=None,
    )
    db_session.commit()

    attachments = db_session.query(HermesContentProducerAttachment).all()
    assert first["session_key"] == second["session_key"]
    assert first["content_factory_url"].endswith(
        f"producer_session={first['session_key']}&source=video-analysis"
    )
    assert first["report_attached"] is True
    assert first["video_attached"] is False
    assert first["media_unavailable"] is True
    assert second["reused"] is True
    assert len(attachments) == 1
    assert attachments[0].workspace_id == 8
    assert attachments[0].user_id == 19
    assert dict(attachments[0].meta_json or {})["source_analysis_id"] == 71
    assert Path(attachments[0].file_path).is_file()


@pytest.mark.anyio
async def test_content_factory_handoff_reuses_transcript_and_requests_multimodal(
    db_session,
    tmp_path,
):
    media_path = tmp_path / "benchmark.mp4"
    subprocess.run(
        [
            "/opt/apps/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x240:rate=4:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-shortest", "-threads", "1", "-pix_fmt", "yuv420p", "-y", str(media_path),
        ],
        check=True,
        timeout=30,
    )
    result = await handoff.create_content_factory_video_handoff(
        db_session,
        row=_completed_analysis(),
        user_id=19,
        media=(media_path, "video/mp4"),
    )
    db_session.commit()

    attachment = db_session.query(HermesContentProducerAttachment).filter_by(
        kind="reference_video"
    ).one()
    analysis = dict(attachment.analysis_json or {})
    assert attachment.analysis_status == "processing"
    assert analysis["analysis_reused"] is True
    assert analysis["transcript_status"] == "success"
    assert analysis["transcript"] == "Wait, who are you? Sleep debt collection."
    assert analysis["segments"][0]["text"] == "Wait, who are you?"
    assert analysis["multimodal_status"] == "queued"
    assert result["pending_multimodal_attachment_ids"] == [attachment.id]


def test_content_factory_handoff_dispatches_multimodal_task(monkeypatch):
    calls = []

    def send_task(name, **kwargs):
        calls.append((name, kwargs))
        return SimpleNamespace(id="analysis-task")

    monkeypatch.setattr(tiktok_shop_router.celery_app, "send_task", send_task)
    status = tiktok_shop_router._dispatch_content_factory_handoff_analyses(
        [13, "13", 0, None]
    )

    assert status == {"status": "queued", "attachment_count": 1}
    assert calls == [
        (
            "openai_whisper.analyze_content_producer_reference",
            {
                "kwargs": {"attachment_id": 13},
                "queue": str(tiktok_shop_router.WHISPER_TASK_QUEUE),
            },
        )
    ]


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
