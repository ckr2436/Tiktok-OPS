from __future__ import annotations

import hashlib
from types import SimpleNamespace

from sqlalchemy.orm import sessionmaker

from app.data.models.hermes_agent import (
    HermesAgentConversation,
    HermesAgentMessage,
    HermesContentProducerAttachment,
)
from app.features.tenants.openai_whisper import tasks
from app.services.hermes_agent import content_factory_api
from app.services.hermes_agent import content_producer
from app.tasks.hermes_agent import content_factory_tasks


def test_ready_multimodal_benchmark_uses_analysis_without_reattaching_contact_sheet(
    tmp_path,
):
    preview = tmp_path / "benchmark-contact-sheet.jpg"
    preview.write_bytes(b"already-reviewed-contact-sheet")
    attachment = SimpleNamespace(
        analysis_status="ready",
        preview_path=str(preview),
        kind="reference_video",
        analysis_json={
            "multimodal_status": "success",
            "visual_semantic_analysis": {
                "opening_hook": {"summary": "Abrupt contradiction"},
            },
        },
    )

    assert content_producer._producer_input_items("{}", [attachment]) is None


def test_reference_transcript_completion_survives_intermediate_commit(
    db_session,
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "benchmark.mp4"
    source.write_bytes(b"reference-video")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    conversation = HermesAgentConversation(
        conversation_key="ws-7-user-19-content_producer-transcript-regression",
        workspace_id=7,
        user_id=19,
        task_type="content_producer",
        title="Transcript regression",
        meta_json={"session_key": "transcript-regression"},
    )
    db_session.add(conversation)
    db_session.flush()
    attachment = HermesContentProducerAttachment(
        attachment_key="pa_transcript_regression",
        conversation_id=conversation.id,
        workspace_id=7,
        user_id=19,
        kind="reference_video",
        original_name="benchmark.mp4",
        file_path=str(source),
        preview_path=None,
        mime_type="video/mp4",
        size_bytes=source.stat().st_size,
        sha256=digest,
        analysis_status="processing",
        analysis_json={
            "duration_seconds": 12.0,
            "transcript_status": "queued",
        },
        meta_json={"source": "producer_intake"},
    )
    db_session.add(attachment)
    db_session.commit()

    worker_sessions = sessionmaker(
        bind=db_session.get_bind(),
        expire_on_commit=True,
    )
    monkeypatch.setattr(tasks, "SessionLocal", worker_sessions)
    monkeypatch.setattr(tasks, "_producer_attachment_source", lambda _path: source)
    monkeypatch.setattr(tasks, "_has_audio_stream", lambda _path: True)
    monkeypatch.setattr(
        tasks.transcriber,
        "transcribe",
        lambda *_args, **_kwargs: {
            "detected_language": "en",
            "segments": [
                {"index": 1, "start": 0.0, "end": 1.5, "text": "Hook now."}
            ],
        },
    )
    sheet = tmp_path / "sheet.jpg"
    sheet.write_bytes(b"fake-contact-sheet")
    monkeypatch.setattr(
        content_factory_tasks,
        "_render_benchmark_contact_sheets",
        lambda *_args, **_kwargs: [{
            "path": str(sheet),
            "board_index": 1,
            "frame_start": 1,
            "frame_end": 6,
            "start_second": 0.0,
            "end_second": 12.0,
            "frame_count": 6,
            "interval_seconds": 2.0,
        }],
    )
    monkeypatch.setattr(
        content_factory_api,
        "analyze_benchmark_storyboard_api",
        lambda *_args, **_kwargs: {
            "analysis_status": "success",
            "opening_hook": {"summary": "Abrupt visual reveal"},
            "story_progression": [{"beat": "problem"}],
            "product_entry": {"summary": "Earned reveal"},
            "must_transfer": ["hook timing"],
            "must_not_copy": ["source pixels", "identity", "wording"],
            "storyboard_guidance": ["Create an original visual equivalent"],
        },
    )

    result = tasks.analyze_content_producer_reference.run(
        attachment_id=attachment.id,
    )

    db_session.expire_all()
    refreshed = db_session.get(HermesContentProducerAttachment, attachment.id)
    assert result == {"status": "ready", "attachment_id": attachment.id}
    assert refreshed.analysis_status == "ready"
    assert refreshed.analysis_json["transcript_status"] == "success"
    assert refreshed.analysis_json["transcript"] == "0.0-1.5s Hook now."
    assert refreshed.analysis_json["segments"][0]["text"] == "Hook now."
    assert refreshed.analysis_json["multimodal_status"] == "success"
    assert refreshed.analysis_json["visual_semantic_analysis"]["opening_hook"]["summary"] == "Abrupt visual reveal"


def test_ready_benchmark_durably_continues_one_idempotent_producer_turn(
    db_session,
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "ready-benchmark.mp4"
    source.write_bytes(b"ready-benchmark")
    conversation = HermesAgentConversation(
        conversation_key="ws-7-user-19-content_producer-ready-benchmark",
        workspace_id=7,
        user_id=19,
        task_type="content_producer",
        title="Ready benchmark",
        meta_json={"session_key": "ready-benchmark"},
    )
    db_session.add(conversation)
    db_session.flush()
    attachment = HermesContentProducerAttachment(
        attachment_key="pa_ready_benchmark",
        conversation_id=conversation.id,
        workspace_id=7,
        user_id=19,
        kind="reference_video",
        original_name="ready.mp4",
        file_path=str(source),
        preview_path=None,
        mime_type="video/mp4",
        size_bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        analysis_status="ready",
        analysis_json={
            "transcript_status": "success",
            "multimodal_status": "success",
            "producer_turn_status": "queued",
        },
        meta_json={
            "active_for_current_requirement": True,
            "analysis_request_context": "拆解这个爆款，再跟我讨论如何原创复刻。",
            "producer_turn_client_id": "benchmark_1234567890123456789012",
            "producer_turn_product_id": None,
        },
    )
    db_session.add(attachment)
    db_session.commit()

    worker_sessions = sessionmaker(bind=db_session.get_bind(), expire_on_commit=True)
    monkeypatch.setattr(content_factory_tasks, "SessionLocal", worker_sessions)
    calls = []

    async def fake_run_producer_turn(db, **kwargs):
        calls.append(kwargs)
        return db.get(HermesAgentConversation, conversation.id), SimpleNamespace(status="needs_input")

    monkeypatch.setattr(content_producer, "run_producer_turn", fake_run_producer_turn)

    result = content_factory_tasks.continue_producer_benchmark_turn.run(
        attachment_id=attachment.id,
    )

    db_session.expire_all()
    refreshed = db_session.get(HermesContentProducerAttachment, attachment.id)
    assert result["status"] == "success"
    assert len(calls) == 1
    assert calls[0]["client_turn_id"] == "benchmark_1234567890123456789012"
    assert calls[0]["message"] == "拆解这个爆款，再跟我讨论如何原创复刻。"
    assert refreshed.analysis_json["producer_turn_status"] == "success"


def test_ready_imported_analysis_proactively_notifies_user_once(
    db_session,
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "imported-ready.mp4"
    source.write_bytes(b"imported-ready")
    conversation = HermesAgentConversation(
        conversation_key="ws-7-user-19-content_producer-imported-ready",
        workspace_id=7,
        user_id=19,
        task_type="content_producer",
        title="Imported ready",
        meta_json={
            "session_key": "imported-ready",
            "source_type": "tiktok_shop_video_analysis",
            "source_analysis_id": 81,
        },
    )
    db_session.add(conversation)
    db_session.flush()
    attachment = HermesContentProducerAttachment(
        attachment_key="pa_imported_ready",
        conversation_id=conversation.id,
        workspace_id=7,
        user_id=19,
        kind="reference_video",
        original_name="imported-ready.mp4",
        file_path=str(source),
        preview_path=None,
        mime_type="video/mp4",
        size_bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        analysis_status="ready",
        analysis_json={
            "transcript_status": "success",
            "multimodal_status": "success",
            "visual_semantic_analysis": {"opening_hook": {"summary": "Hook"}},
        },
        meta_json={
            "active_for_current_requirement": True,
            "source_kind": "reference_video",
            "source_analysis_id": 81,
        },
    )
    db_session.add(attachment)
    db_session.commit()

    worker_sessions = sessionmaker(
        bind=db_session.get_bind(),
        expire_on_commit=True,
    )
    monkeypatch.setattr(tasks, "SessionLocal", worker_sessions)

    first = tasks.recover_content_producer_reference_analyses.run()
    second = tasks.recover_content_producer_reference_analyses.run()

    db_session.expire_all()
    messages = db_session.query(HermesAgentMessage).filter(
        HermesAgentMessage.conversation_id == conversation.id,
        HermesAgentMessage.run_id == f"benchmark_ready_{attachment.id}",
    ).all()
    refreshed = db_session.get(HermesContentProducerAttachment, attachment.id)
    assert first["notified_ready"] == 1
    assert second["notified_ready"] == 0
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert "完整多模态分析已经完成" in messages[0].content_text
    assert refreshed.analysis_json["producer_notification_status"] == "success"


def test_explicit_pending_benchmark_turn_does_not_get_generic_notification(
    db_session,
    tmp_path,
):
    source = tmp_path / "pending-turn.mp4"
    source.write_bytes(b"pending-turn")
    conversation = HermesAgentConversation(
        conversation_key="ws-7-user-19-content_producer-pending-turn",
        workspace_id=7,
        user_id=19,
        task_type="content_producer",
        title="Pending turn",
        meta_json={
            "session_key": "pending-turn",
            "source_type": "tiktok_shop_video_analysis",
        },
    )
    db_session.add(conversation)
    db_session.flush()
    attachment = HermesContentProducerAttachment(
        attachment_key="pa_pending_turn",
        conversation_id=conversation.id,
        workspace_id=7,
        user_id=19,
        kind="reference_video",
        original_name="pending-turn.mp4",
        file_path=str(source),
        preview_path=None,
        mime_type="video/mp4",
        size_bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        analysis_status="ready",
        analysis_json={"multimodal_status": "success"},
        meta_json={
            "active_for_current_requirement": True,
            "analysis_request_context": "分析这个视频并继续回复。",
        },
    )
    db_session.add(attachment)
    db_session.flush()

    analysis = dict(attachment.analysis_json or {})
    assert tasks._ensure_producer_analysis_ready_notification(
        db_session, attachment, analysis
    ) is False
    assert db_session.query(HermesAgentMessage).filter(
        HermesAgentMessage.conversation_id == conversation.id,
    ).count() == 0


def test_durable_successful_transcript_skips_second_whisper_pass(
    db_session,
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "reused-benchmark.mp4"
    source.write_bytes(b"reused-benchmark")
    conversation = HermesAgentConversation(
        conversation_key="ws-7-user-19-content_producer-reused-transcript",
        workspace_id=7,
        user_id=19,
        task_type="content_producer",
        title="Reused transcript",
        meta_json={"session_key": "reused-transcript"},
    )
    db_session.add(conversation)
    db_session.flush()
    attachment = HermesContentProducerAttachment(
        attachment_key="pa_reused_transcript",
        conversation_id=conversation.id,
        workspace_id=7,
        user_id=19,
        kind="reference_video",
        original_name="reused.mp4",
        file_path=str(source),
        preview_path=None,
        mime_type="video/mp4",
        size_bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        analysis_status="processing",
        analysis_json={
            "duration_seconds": 12.0,
            "transcript_status": "success",
            "transcript_language": "en",
            "transcript_text": "Existing transcript.",
            "transcript_segments": [
                {"index": 1, "start": 0, "end": 1, "text": "Existing transcript."}
            ],
            "multimodal_status": "queued",
            "multimodal_error": "stale-error-must-not-survive",
        },
        meta_json={"source": "tiktok_shop_video_analysis"},
    )
    db_session.add(attachment)
    db_session.commit()

    worker_sessions = sessionmaker(bind=db_session.get_bind(), expire_on_commit=True)
    monkeypatch.setattr(tasks, "SessionLocal", worker_sessions)
    monkeypatch.setattr(tasks, "_producer_attachment_source", lambda _path: source)
    monkeypatch.setattr(
        tasks.transcriber,
        "transcribe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reused transcript must not run Whisper")
        ),
    )
    sheet = tmp_path / "reused-sheet.jpg"
    sheet.write_bytes(b"fake-contact-sheet")
    monkeypatch.setattr(
        content_factory_tasks,
        "_render_benchmark_contact_sheets",
        lambda *_args, **_kwargs: [{
            "path": str(sheet),
            "board_index": 1,
            "frame_start": 1,
            "frame_end": 6,
            "start_second": 0.0,
            "end_second": 12.0,
            "frame_count": 6,
            "interval_seconds": 2.0,
        }],
    )
    monkeypatch.setattr(
        content_factory_api,
        "analyze_benchmark_storyboard_api",
        lambda *_args, **_kwargs: {"analysis_status": "success"},
    )

    result = tasks.analyze_content_producer_reference.run(
        attachment_id=attachment.id,
    )

    db_session.expire_all()
    refreshed = db_session.get(HermesContentProducerAttachment, attachment.id)
    assert result == {"status": "ready", "attachment_id": attachment.id}
    assert refreshed.analysis_json["transcript"] == "Existing transcript."
    assert refreshed.analysis_json["multimodal_status"] == "success"
    assert "multimodal_error" not in refreshed.analysis_json


def test_recovery_dispatches_orphaned_multimodal_analysis(
    db_session,
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "orphaned.mp4"
    source.write_bytes(b"orphaned")
    conversation = HermesAgentConversation(
        conversation_key="ws-7-user-19-content_producer-orphaned",
        workspace_id=7,
        user_id=19,
        task_type="content_producer",
        title="Orphaned analysis",
        meta_json={"session_key": "orphaned"},
    )
    db_session.add(conversation)
    db_session.flush()
    attachment = HermesContentProducerAttachment(
        attachment_key="pa_orphaned_analysis",
        conversation_id=conversation.id,
        workspace_id=7,
        user_id=19,
        kind="reference_video",
        original_name="orphaned.mp4",
        file_path=str(source),
        mime_type="video/mp4",
        size_bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        analysis_status="processing",
        analysis_json={
            "transcript_status": "success",
            "multimodal_status": "queued",
        },
        meta_json={"active_for_current_requirement": True},
    )
    db_session.add(attachment)
    db_session.commit()

    worker_sessions = sessionmaker(bind=db_session.get_bind(), expire_on_commit=True)
    monkeypatch.setattr(tasks, "SessionLocal", worker_sessions)
    calls = []
    monkeypatch.setattr(
        tasks.celery_app,
        "send_task",
        lambda name, **kwargs: calls.append((name, kwargs)),
    )

    result = tasks.recover_content_producer_reference_analyses.run(
        stale_seconds=0,
        limit=10,
    )

    assert result["dispatched"] == 1
    assert result["attachment_ids"] == [attachment.id]
    assert calls[0][0] == "openai_whisper.analyze_content_producer_reference"
    assert calls[0][1]["kwargs"] == {"attachment_id": attachment.id}
