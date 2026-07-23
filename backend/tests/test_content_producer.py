from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

from app.core.errors import APIError
from app.data.models.hermes_agent import (
    HermesAgentMessage,
    HermesContentFactoryProject,
    HermesContentProduct,
)
from app.services.hermes_agent import content_producer
from app.services.hermes_agent.content_producer import (
    ContentProducerProposal,
    authoritative_producer_script,
    copy_producer_attachments_to_project,
    confirmed_project_parameters,
    get_or_create_producer_conversation,
    producer_session,
    run_producer_turn,
    save_producer_attachment,
)


def _proposal(**overrides):
    values = {
        "title": "Three fast TikTok concepts",
        "content_objective": "Convert cold TikTok viewers with a clear problem-solution story",
        "target_audience": "US adults who recognize the described nighttime routine",
        "content_mode": "product",
        "platform": "tiktok",
        "video_count": 3,
        "video_duration_min_seconds": 40,
        "video_duration_max_seconds": 40,
        "video_model": "omni_flash",
        "video_resolution": "720p",
        "video_aspect_ratio": "9:16",
        "video_language": "en-US",
        "visual_style": "adult American animation with clear visual metaphors",
        "pacing": "fast first three seconds, then readable story beats",
        "audio_direction": "one consistent female narrator; female characters keep their own female dialogue voices",
        "conversion_direction": "earn the product reveal from the problem and close with the authorized offer",
        "creative_constraints": ["Do not render fake shopping UI"],
        "visual_reference_generation_mode": "board",
        "promotion_evidence_quote": "$7.99",
        "confidence": 0.92,
    }
    values.update(overrides)
    return values


@pytest.mark.anyio
async def test_producer_turn_saves_a_reviewable_proposal_without_browser(
    db_session,
    monkeypatch,
):
    product = HermesContentProduct(
        workspace_id=7,
        user_id=19,
        product_key="brand::gummies::us",
        brand_name="Brand",
        product_name="Gummies",
        market="US",
        product_brief="Company supplied product brief",
        facts_json={"claims": ["company fact only"]},
        meta_json={},
    )
    db_session.add(product)
    db_session.commit()

    output = {
        "status": "proposal_ready",
        "assistant_message": "我建议制作3条40秒的美国TikTok动画转化视频。",
        "missing_information": [],
        "proposal": _proposal(),
    }

    async def fake_create_response(self, **kwargs):
        assert kwargs["metadata"]["prompt_version"] == "content_producer_v4"
        assert kwargs["conversation"].startswith("gmv-cf-producer-")
        assert kwargs["session_key"].startswith("gmv-cf-producer-")
        assert kwargs["store"] is True
        assert "browser" not in kwargs
        return {"output_text": json.dumps(output)}, 321

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )

    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=7,
        user_id=19,
        message="给这个商品做3条TikTok视频，现在是$7.99。",
        session_key="safe-session",
        product_id=product.id,
    )

    assert decision.status == "proposal_ready"
    assert decision.proposal.video_count == 3
    assert decision.proposal.promotion_evidence_quote == "$7.99"
    assert conversation.meta_json["proposal_sha256"]
    assert conversation.meta_json["selected_product_id"] == product.id
    _, messages = producer_session(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="safe-session",
    )
    assert [row.role for row in messages] == ["user", "assistant"]


@pytest.mark.anyio
async def test_explicit_no_product_clears_an_earlier_product_selection(
    db_session,
    monkeypatch,
):
    product = HermesContentProduct(
        workspace_id=7,
        user_id=19,
        product_key="brand::clear-selection::us",
        brand_name="Brand",
        product_name="Earlier product",
        market="US",
        product_brief="Authoritative brief",
        facts_json={"claims": ["approved"]},
        meta_json={},
    )
    db_session.add(product)
    db_session.commit()
    outputs = [
        _proposal(),
        _proposal(content_mode="general", promotion_evidence_quote=None),
    ]

    async def fake_create_response(self, **kwargs):
        proposal = outputs.pop(0)
        return {
            "output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "方案已经整理好。",
                "missing_information": [],
                "proposal": proposal,
            })
        }, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    await run_producer_turn(
        db_session,
        workspace_id=7,
        user_id=19,
        message="先为这个商品做视频。",
        session_key="clear-product-session",
        product_id=product.id,
        product_selection_explicit=True,
    )
    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=7,
        user_id=19,
        message="改成不绑定商品的通用内容。",
        session_key="clear-product-session",
        product_id=None,
        product_selection_explicit=True,
    )

    assert decision.proposal.content_mode == "general"
    assert conversation.meta_json.get("selected_product_id") is None


@pytest.mark.anyio
async def test_producer_strips_promotion_not_quoted_by_user(
    db_session,
    monkeypatch,
):
    output = {
        "status": "proposal_ready",
        "assistant_message": "方案已经整理好。",
        "missing_information": [],
        "proposal": _proposal(
            content_mode="general",
            promotion_evidence_quote="$99 invented",
        ),
    }

    async def fake_create_response(self, **kwargs):
        return {"output_text": json.dumps(output)}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    _, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message="做一个不绑定商品的动画知识视频。",
        session_key="general-session",
        product_id=None,
    )

    assert decision.proposal.promotion_evidence_quote is None


@pytest.mark.anyio
async def test_producer_session_is_user_scoped(db_session, monkeypatch):
    output = {
        "status": "proposal_ready",
        "assistant_message": "方案已经整理好。",
        "missing_information": [],
        "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
    }

    async def fake_create_response(self, **kwargs):
        return {"output_text": json.dumps(output)}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message="做一条动画。",
        session_key="owned-session",
        product_id=None,
    )

    with pytest.raises(APIError) as exc:
        producer_session(
            db_session,
            workspace_id=8,
            user_id=21,
            session_key="owned-session",
        )
    assert exc.value.code == "CONTENT_PRODUCER_SESSION_NOT_FOUND"


@pytest.mark.anyio
async def test_producer_retry_reuses_one_durable_user_turn(db_session, monkeypatch):
    calls = 0

    async def fake_create_response(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise APIError("HERMES_UPSTREAM_QUOTA", "provider unavailable", 503)
        return {
            "output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "方案已经整理好。",
                "missing_information": [],
                "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
            })
        }, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    kwargs = {
        "workspace_id": 8,
        "user_id": 20,
        "message": "做一条动画。",
        "session_key": "idempotent-turn-session",
        "product_id": None,
        "client_turn_id": "turn_12345678",
    }
    with pytest.raises(APIError, match="provider unavailable"):
        await run_producer_turn(db_session, **kwargs)
    _conversation, messages = producer_session(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="idempotent-turn-session",
    )
    assert [(row.role, row.run_id) for row in messages] == [("user", "turn_12345678")]

    _conversation, decision = await run_producer_turn(db_session, **kwargs)
    assert decision.status == "proposal_ready"
    _conversation, repeated = await run_producer_turn(db_session, **kwargs)
    assert repeated == decision
    assert calls == 2
    _conversation, messages = producer_session(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="idempotent-turn-session",
    )
    assert [(row.role, row.run_id) for row in messages] == [
        ("user", "turn_12345678"),
        ("assistant", "turn_12345678"),
    ]


@pytest.mark.anyio
async def test_producer_preserves_full_source_script_without_per_message_slice(
    db_session,
    monkeypatch,
):
    script = "HOOK\n" + "A" * 4500
    captured = {}

    async def fake_create_response(self, **kwargs):
        captured.update(json.loads(kwargs["input_text"]))
        return {
            "output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "已按完整原文整理方案。",
                "missing_information": [],
                "changed_fields": [],
                "change_evidence": {},
                "authoritative_script_message_id": captured["authoritative_source_texts"][0]["message_id"],
                "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
            })
        }, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    conversation, _decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=script,
        session_key="full-script-session",
        product_id=None,
    )

    assert len(script) > 4000
    assert captured["latest_user_message"] == script.strip()
    assert "authoritative_source_texts" in captured["conversation"][-1]["content"]
    assert captured["authoritative_source_texts"][0]["content"] == script.strip()
    assert captured["context_manifest"]["individual_messages_truncated"] is False
    assert conversation.meta_json["source_text_assets"][0]["character_count"] == len(script.strip())
    assert conversation.meta_json["authoritative_script_message_id"] == captured["authoritative_source_texts"][0]["message_id"]


@pytest.mark.anyio
async def test_producer_followup_is_a_delta_and_preserves_unmentioned_duration(
    db_session,
    monkeypatch,
):
    followup = "人物改成美式动画，数量改为5条。"
    outputs = [
        {
            "status": "proposal_ready",
            "assistant_message": "先按3条40秒动画整理。",
            "missing_information": [],
            "changed_fields": [],
            "change_evidence": {},
            "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
        },
        {
            "status": "proposal_ready",
            "assistant_message": "已改为5条美式动画。",
            "missing_information": [],
            "changed_fields": ["video_count", "visual_style"],
            "change_evidence": {
                "video_count": followup,
                "visual_style": followup,
            },
            # The model also tries to replace duration and voice without user
            # evidence. Backend reconciliation must reject those changes.
            "proposal": _proposal(
                content_mode="general",
                video_count=5,
                video_duration_min_seconds=20,
                video_duration_max_seconds=20,
                visual_style="American adult animation",
                audio_direction="male narrator and male character dialogue",
                promotion_evidence_quote=None,
            ),
        },
    ]

    async def fake_create_response(self, **kwargs):
        return {"output_text": json.dumps(outputs.pop(0))}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message="按这个文案做美国 TikTok 动画。",
        session_key="delta-session",
        product_id=None,
    )
    _conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=followup,
        session_key="delta-session",
        product_id=None,
    )

    assert decision.proposal.video_count == 5
    assert decision.proposal.visual_style == "American adult animation"
    assert decision.proposal.video_duration_min_seconds == 40
    assert decision.proposal.video_duration_max_seconds == 40
    assert decision.proposal.audio_direction == _proposal()["audio_direction"]
    assert set(decision.changed_fields) == {"video_count", "visual_style"}


def test_producer_session_hides_adjacent_legacy_retry_duplicate_but_keeps_audit_rows(
    db_session,
):
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="legacy-duplicate-session",
    )
    for run_id in ("legacyfail01", "legacyretry02"):
        content_producer.repository.add_message(
            db_session,
            conversation=conversation,
            workspace_id=8,
            user_id=20,
            role="user",
            content_text="同一条失败后重试的消息",
            content_json={},
            run_id=run_id,
        )
    db_session.commit()

    _conversation, visible = producer_session(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="legacy-duplicate-session",
    )
    persisted = db_session.query(HermesAgentMessage).filter(
        HermesAgentMessage.conversation_id == int(conversation.id)
    ).all()

    assert len(persisted) == 2
    assert len(visible) == 1
    assert visible[0].run_id == "legacyretry02"


@pytest.mark.anyio
async def test_character_attachment_is_seen_by_producer_and_transferred_on_confirmation(
    db_session,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(content_producer, "PRODUCER_STORAGE_ROOT", tmp_path / "intake")
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="attachment-session",
    )
    db_session.commit()
    image_buffer = io.BytesIO()
    Image.new("RGB", (320, 480), color=(40, 80, 160)).save(image_buffer, format="PNG")
    image_buffer.seek(0)
    upload = UploadFile(
        filename="hero.png",
        file=image_buffer,
        headers=Headers({"content-type": "image/png"}),
    )
    attachment = await save_producer_attachment(
        db_session,
        conversation=conversation,
        user_id=19,
        upload=upload,
        kind="character_reference",
        character_key="hero",
        character_name="Hero",
    )
    db_session.commit()
    assert Path(attachment.file_path).is_file()
    assert Path(attachment.preview_path).is_file()

    output = {
        "status": "proposal_ready",
        "assistant_message": "我会把附件中的人物外形作为身份参考。",
        "missing_information": [],
        "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
    }

    async def fake_create_response(self, **kwargs):
        input_items = kwargs["input_items"]
        assert input_items[0]["role"] == "user"
        assert any(part.get("type") == "input_image" for part in input_items[0]["content"])
        assert "user_attachments" in kwargs["input_text"]
        return {"output_text": json.dumps(output)}, 20

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    await run_producer_turn(
        db_session,
        workspace_id=7,
        user_id=19,
        message="用我上传的人物做一条动画。",
        session_key="attachment-session",
        product_id=None,
        client_turn_id="attachment_turn_1",
    )

    project = HermesContentFactoryProject(
        project_key="cf_attachment_test",
        workspace_id=7,
        user_id=19,
        title="Attachment test",
        product_name="",
        market="US",
        status="draft",
        current_stage="DIRECTOR",
        config_json={},
        state_json={},
    )
    db_session.add(project)
    db_session.flush()
    rows = copy_producer_attachments_to_project(
        db_session,
        conversation=conversation,
        project=project,
        user_id=19,
        storage_root=tmp_path / "factory",
        browser_inbox=tmp_path / "bridge",
    )
    db_session.commit()
    assert len(rows) == 1
    assert rows[0].kind == "character_reference"
    assert rows[0].meta_json["producer_attachment_key"] == attachment.attachment_key
    assert Path(rows[0].file_path).is_file()
    db_session.refresh(attachment)
    assert attachment.project_asset_id == rows[0].id


@pytest.mark.anyio
async def test_reference_video_blocks_producer_until_audio_analysis_is_ready(
    db_session,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(content_producer, "PRODUCER_STORAGE_ROOT", tmp_path / "intake")
    monkeypatch.setattr(
        content_producer,
        "_probe_reference_video",
        lambda _path: {"duration_seconds": 12.0, "width": 720, "height": 1280},
    )

    def fake_preview(_source, target, *, duration_seconds):
        assert duration_seconds == 12.0
        Image.new("RGB", (320, 240), color=(10, 20, 30)).save(target, format="JPEG")

    monkeypatch.setattr(content_producer, "_render_video_preview", fake_preview)
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="reference-video-session",
    )
    db_session.commit()
    upload = UploadFile(
        filename="benchmark.mp4",
        file=io.BytesIO(b"test-video-payload"),
        headers=Headers({"content-type": "video/mp4"}),
    )
    attachment = await save_producer_attachment(
        db_session,
        conversation=conversation,
        user_id=19,
        upload=upload,
        kind="reference_video",
    )
    db_session.commit()

    assert attachment.analysis_status == "processing"
    assert attachment.analysis_json["transcript_status"] == "queued"
    with pytest.raises(APIError) as exc:
        await run_producer_turn(
            db_session,
            workspace_id=7,
            user_id=19,
            message="参考这个视频做三条。",
            session_key="reference-video-session",
            product_id=None,
            client_turn_id="video_wait_turn",
        )
    assert exc.value.code == "CONTENT_PRODUCER_ATTACHMENTS_PROCESSING"
    assert db_session.query(HermesAgentMessage).filter(
        HermesAgentMessage.conversation_id == conversation.id
    ).count() == 0


@pytest.mark.anyio
async def test_confirmed_parameters_require_exact_saved_digest(
    db_session,
    monkeypatch,
):
    output = {
        "status": "proposal_ready",
        "assistant_message": "方案已经整理好。",
        "missing_information": [],
        "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
    }

    async def fake_create_response(self, **kwargs):
        return {"output_text": json.dumps(output)}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    conversation, _ = await run_producer_turn(
        db_session,
        workspace_id=9,
        user_id=22,
        message="做一条40秒美式动画。",
        session_key="confirm-session",
        product_id=None,
    )
    meta = dict(conversation.meta_json)
    meta["proposal"]["video_count"] = 4
    conversation.meta_json = meta
    db_session.add(conversation)
    db_session.commit()

    with pytest.raises(APIError) as exc:
        confirmed_project_parameters(
            db_session,
            workspace_id=9,
            user_id=22,
            session_key="confirm-session",
        )
    assert exc.value.code == "CONTENT_PRODUCER_PROPOSAL_CHANGED"


def test_omni_proposal_requires_provider_legal_duration():
    with pytest.raises(ValueError):
        ContentProducerProposal.model_validate(
            _proposal(
                video_duration_min_seconds=41,
                video_duration_max_seconds=49,
            )
        )


def test_confirmed_offer_excludes_cancelled_offer_and_keeps_evidence_separate():
    latest = "把$7.99改成$14.99 shipped，取消新客立减$5。"
    safe = ContentProducerProposal.model_validate(
        _proposal(
            confirmed_offer="$14.99 shipped",
            promotion_evidence_quote=latest,
        )
    )
    content_producer._validate_promotion_authorization(
        safe,
        latest_user_message=latest,
        prior_proposal=None,
    )

    unsafe = ContentProducerProposal.model_validate(
        _proposal(
            confirmed_offer="取消$5，改为$14.99 shipped",
            promotion_evidence_quote=latest,
        )
    )
    with pytest.raises(ValueError, match="exclude canceled"):
        content_producer._validate_promotion_authorization(
            unsafe,
            latest_user_message=latest,
            prior_proposal=None,
        )


@pytest.mark.anyio
async def test_locked_script_rejects_impossible_duration_and_repairs(
    db_session,
    monkeypatch,
):
    calls = 0
    script = " ".join(["spoken"] * 169)

    async def fake_create_response(self, **kwargs):
        nonlocal calls
        calls += 1
        packet = json.loads(kwargs["input_text"])
        if calls == 1:
            source_id = packet["authoritative_source_texts"][0]["message_id"]
            duration = 15
        else:
            source_id = packet["original_packet"]["authoritative_source_texts"][0]["message_id"]
            assert "at least 47 seconds" in packet["validation_error"]
            duration = 50
        return {
            "output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "已按完整文案可读语速修正时长。",
                "missing_information": [],
                "changed_fields": [],
                "change_evidence": {},
                "authoritative_script_message_id": source_id,
                "proposal": _proposal(
                    content_mode="general",
                    video_duration_min_seconds=duration,
                    video_duration_max_seconds=duration,
                    video_model="seedance_2_0_mini",
                    promotion_evidence_quote=None,
                ),
            })
        }, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    _conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=script,
        session_key="script-fit-session",
        product_id=None,
    )

    assert calls == 2
    assert decision.proposal.video_duration_min_seconds == 50
    assert decision.proposal.video_duration_max_seconds == 50


@pytest.mark.anyio
async def test_explicit_script_edit_creates_versioned_authoritative_copy(
    db_session,
    monkeypatch,
):
    intro = "I started a simple nighttime routine before bed. " * 14
    original = intro + "Search MYUPONA on TikTok Shop—it’s $7.99 right now."
    revised = intro + "Search MYUPONA on TikTok Shop—it’s $14.99 shipped."
    followup = "修改文案为14.99包邮到家。"
    call = 0

    async def fake_create_response(self, **kwargs):
        nonlocal call
        call += 1
        packet = json.loads(kwargs["input_text"])
        if call == 1:
            source_id = packet["authoritative_source_texts"][0]["message_id"]
            return {"output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "已锁定原文。",
                "missing_information": [],
                "changed_fields": [],
                "change_evidence": {},
                "authoritative_script_message_id": source_id,
                "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
            })}, 10
        return {"output_text": json.dumps({
            "status": "proposal_ready",
            "assistant_message": "已只替换价格和包邮表达。",
            "missing_information": [],
            "changed_fields": [
                "conversion_direction",
                "confirmed_offer",
                "promotion_evidence_quote",
            ],
            "change_evidence": {
                "conversion_direction": followup,
                "confirmed_offer": followup,
                "promotion_evidence_quote": followup,
            },
            "authoritative_script_message_id": packet["current_authoritative_script_message_id"],
            "revised_authoritative_script": revised,
            "script_revision_evidence": followup,
            "proposal": _proposal(
                content_mode="general",
                conversion_direction="$14.99 shipped",
                confirmed_offer="$14.99 shipped",
                promotion_evidence_quote=followup,
            ),
        })}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    conversation, _ = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=original,
        session_key="script-revision-session",
        product_id=None,
    )
    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=followup,
        session_key="script-revision-session",
        product_id=None,
    )

    locked = authoritative_producer_script(db_session, conversation=conversation)
    assert locked is not None
    assert locked[1] == revised
    assert conversation.meta_json["authoritative_script_current_version"] == 2
    assert decision.revised_authoritative_script == revised
