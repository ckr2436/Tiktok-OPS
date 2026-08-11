from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile

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
    compile_confirmed_creative_copy_contract,
    copy_producer_attachments_to_project,
    confirmed_project_parameters,
    get_or_create_producer_conversation,
    producer_session,
    run_producer_turn,
    save_producer_attachment,
    stage_producer_reference_link,
)


def _proposal(**overrides):
    values = {
        "title": "Three fast TikTok concepts",
        "content_objective": "Convert cold TikTok viewers with a clear problem-solution story",
        "target_audience": "US adults who recognize the described nighttime routine",
        "content_mode": "product",
        "product_use_mode": "required",
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
        "spoken_density": "dense",
        "spoken_density_reason": "Fast continuous TikTok narration was requested.",
        "audio_direction": "one consistent female narrator; female characters keep their own female dialogue voices",
        "conversion_direction": "earn the product reveal from the problem and close with the authorized offer",
        "creative_constraints": ["Do not render fake shopping UI"],
        "visual_reference_generation_mode": "board",
        "promotion_evidence_quote": "$7.99",
        "confidence": 0.92,
    }
    values.update(overrides)
    if "product_use_mode" not in overrides:
        values["product_use_mode"] = (
            "required" if values["content_mode"] == "product" else "none"
        )
    return values


def test_producer_defaults_image_references_to_one_splittable_board():
    values = _proposal()
    values.pop("visual_reference_generation_mode")

    proposal = ContentProducerProposal.model_validate(values)

    assert proposal.visual_reference_generation_mode == "board"


def test_product_selection_preflight_does_not_draft_a_discarded_proposal():
    instructions = " ".join(content_producer._instructions().split())

    assert "intent_spec=null and proposal=null" in instructions
    assert "Do not spend tokens drafting a plan" in instructions


def test_producer_keeps_selected_product_as_context_without_conversion_gate():
    proposal = ContentProducerProposal.model_validate(
        _proposal(
            product_use_mode="context_only",
            conversion_direction=None,
            promotion_evidence_quote=None,
        )
    )

    assert proposal.content_mode == "product"
    assert proposal.product_use_mode == "context_only"


def test_general_producer_proposal_defaults_to_no_product_use():
    values = _proposal(
        content_mode="general",
        conversion_direction=None,
        promotion_evidence_quote=None,
    )
    values.pop("product_use_mode")

    proposal = ContentProducerProposal.model_validate(values)

    assert proposal.product_use_mode == "none"


def test_general_producer_proposal_rejects_required_product_use():
    with pytest.raises(ValueError, match="general content requires"):
        ContentProducerProposal.model_validate(
            _proposal(
                content_mode="general",
                product_use_mode="required",
                conversion_direction=None,
                promotion_evidence_quote=None,
            )
        )


@pytest.mark.anyio
async def test_selected_product_semantic_review_removes_impossible_conversion_gate(
    db_session,
    monkeypatch,
):
    product = HermesContentProduct(
        workspace_id=8,
        user_id=20,
        product_key="myupona::sleep-ease::us",
        brand_name="MYUPONA",
        product_name="Sleep Ease Gummies",
        market="US",
        product_brief="Authoritative product context.",
        facts_json={"claims": []},
        meta_json={},
    )
    db_session.add(product)
    db_session.commit()
    user_message = "生成10条相关爆款开头钩子，不出现产品、品牌或购买引导。"
    candidate_proposal = _proposal(
        video_count=10,
        video_duration_min_seconds=4,
        video_duration_max_seconds=4,
        product_use_mode="required",
        promotion_evidence_quote=None,
    )
    reviewed_proposal = {
        **candidate_proposal,
        "product_use_mode": "context_only",
        "conversion_direction": None,
    }
    intent_spec = {
        "delivery_mode": "independent_videos",
        "source_material_mode": "requirements",
        "user_goal": "Create ten product-category hook clips without product presence.",
        "intent_manifest": _intent_manifest(
            evidence_quote=user_message,
        ),
        "deliverables": [
            {
                "ordinal": ordinal,
                "label": f"Hook {ordinal}",
                "objective": "Create one standalone opening hook.",
                "relationship": "independent",
                "script_text": None,
                "source_message_id": None,
                "target_duration_seconds": 4,
                "must_preserve": [
                    "Do not show or name the product, brand, package, offer, or CTA."
                ],
                "differentiation": [f"Distinct hook mechanism {ordinal}"],
            }
            for ordinal in range(1, 11)
        ],
    }

    async def fake_create_response(self, **kwargs):
        if kwargs["metadata"].get("review_type") == "semantic_consistency":
            return {
                "output_text": json.dumps(
                    {
                        "verdict": "revised",
                        "issues": [
                            "Product appearance conflicts with the explicit product-free hook boundary."
                        ],
                        "reviewed_intent_spec": intent_spec,
                        "reviewed_proposal": reviewed_proposal,
                        "assistant_question": None,
                    }
                )
            }, 12
        return {
            "output_text": json.dumps(
                {
                    "status": "proposal_ready",
                    "assistant_message": "已整理为10条不出现商品的独立钩子。",
                    "missing_information": [],
                    "changed_fields": [],
                    "change_evidence": {},
                    "intent_spec": intent_spec,
                    "proposal": candidate_proposal,
                }
            )
        }, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )

    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=user_message,
        session_key="product-context-only-hooks",
        product_id=int(product.id),
        product_selection_explicit=True,
    )

    assert decision.proposal.product_use_mode == "context_only"
    assert decision.proposal.conversion_direction is None
    assert conversation.meta_json["semantic_review"]["verdict"] == "revised"


def test_producer_normalizes_complete_timing_coordinates_to_time_window():
    payload = {
        "intent_spec": {
            "intent_manifest": {
                "requirements": [
                    {
                        "requirement_id": "R-001",
                        "scope": "project",
                        "start_seconds": 0,
                        "end_seconds": 2,
                    },
                    {
                        "requirement_id": "R-002",
                        "scope": "deliverable",
                        "start_seconds": None,
                        "end_seconds": None,
                    },
                ]
            }
        }
    }

    normalized = content_producer._normalize_decision_payload(payload)

    requirements = normalized["intent_spec"]["intent_manifest"]["requirements"]
    assert requirements[0]["scope"] == "time_window"
    assert requirements[0]["start_seconds"] == 0
    assert requirements[0]["end_seconds"] == 2
    assert requirements[1]["scope"] == "deliverable"
    assert payload["intent_spec"]["intent_manifest"]["requirements"][0]["scope"] == "project"


def test_producer_packet_carries_the_current_intent_handoff(db_session):
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="intent-handoff-packet",
    )
    intent_spec = {
        "delivery_mode": "visual_variants",
        "user_goal": "Preserve the accepted hook while making three variants.",
        "intent_manifest": _intent_manifest("Create videos."),
    }
    conversation.meta_json = {
        **dict(conversation.meta_json or {}),
        "intent_spec": intent_spec,
    }
    db_session.add(conversation)
    db_session.commit()

    packet = content_producer._packet(
        db_session,
        conversation=conversation,
        selected_product=None,
    )

    assert packet["current_working_intent_spec"] == intent_spec


def _intent_manifest(evidence_quote="Create videos.", **overrides):
    values = {
        "schema_version": "2.0",
        "objective": "Execute the user's complete video request.",
        "requirements": [{
            "requirement_id": "R-001",
            "kind": "objective",
            "priority": "critical",
            "scope": "project",
            "deliverable_ordinals": [],
            "intent": "Deliver the requested complete videos.",
            "evidence_quote": evidence_quote,
            "interpretation": "Each requested output must be complete and independently usable.",
            "observable_checks": ["Every requested output exists as a complete video."],
        }],
        "transformation_contract": None,
        "manifest_sha256": None,
    }
    values.update(overrides)
    return values


def _adaptive_reference_contract():
    return {
        "source_role": "reference_copy",
        "fidelity": "adaptive",
        "execution_strategy": "full_regeneration",
        "transfer_mode": "semantic_structure",
        "source_media_reuse": "forbidden",
        "protected_requirements": ["Preserve the conversion logic."],
        "authorized_changes": [{
            "instruction": "Rewrite the wording and create differentiated videos.",
            "dimensions": ["spoken wording", "story premise", "setting"],
            "start_seconds": None,
            "end_seconds": None,
            "evidence_quote": "可以适当修改，但转化逻辑不能变",
        }],
        "creative_freedom": ["Invent original hooks and scenes."],
        "excluded_source_artifacts": ["source-specific wording"],
        "success_checks": ["Each output preserves the conversion logic."],
        "rationale": "The source is a semantic reference, not locked copy.",
    }


def test_confirmed_reference_copy_uses_ai_reviewed_adaptive_authority():
    intent = {
        "delivery_mode": "independent_videos",
        "source_material_mode": "reference_copy",
        "user_goal": "Create three differentiated videos from this reference copy.",
        "intent_manifest": _intent_manifest(
            "可以适当修改，但转化逻辑不能变",
            transformation_contract=_adaptive_reference_contract(),
        ),
        "deliverables": [
            {
                "ordinal": ordinal,
                "label": f"Video {ordinal}",
                "objective": f"Create differentiated video {ordinal}",
                "relationship": "independent",
                "target_duration_seconds": 24,
                "differentiation": [f"Distinct hook {ordinal}"],
            }
            for ordinal in range(1, 4)
        ],
    }

    contract = compile_confirmed_creative_copy_contract(
        intent_spec=intent,
        authoritative_script=(181, "Reference copy with a proven conversion arc."),
    )

    assert contract["copy_authority"] == "producer_draft_editable"
    assert contract["source_copy_role"] == "semantic_reference"
    assert "required_verbatim_voiceover" not in contract
    assert "required_verbatim_voiceovers" not in contract
    assert [
        row["deliverable_ordinal"]
        for row in contract["director_seed_voiceovers"]
    ] == [1, 2, 3]


def test_confirmed_single_script_defaults_to_verbatim_authority():
    intent = {
        "delivery_mode": "visual_variants",
        "source_material_mode": "single_script",
        "user_goal": "Keep this script and create three visual variants.",
        "intent_manifest": _intent_manifest("Keep this script."),
        "deliverables": [
            {
                "ordinal": ordinal,
                "label": f"Visual variant {ordinal}",
                "objective": "Use the same spoken copy with a different visual treatment.",
                "relationship": "visual_variant",
            }
            for ordinal in range(1, 4)
        ],
    }

    contract = compile_confirmed_creative_copy_contract(
        intent_spec=intent,
        authoritative_script=(22, "This exact script remains unchanged."),
        authoritative_script_version=3,
    )

    assert contract["copy_authority"] == "user_verbatim"
    assert contract["required_verbatim_voiceover"] == (
        "This exact script remains unchanged."
    )
    assert contract["script_reuse_mode"] == "same_copy_visual_variants"
    assert contract["source_version"] == 3


def test_confirmed_single_script_can_become_editable_when_ai_review_authorizes_it():
    intent = {
        "delivery_mode": "single",
        "source_material_mode": "single_script",
        "user_goal": "Rewrite this script while preserving its conversion logic.",
        "intent_manifest": _intent_manifest(
            "Rewrite this script.",
            transformation_contract=_adaptive_reference_contract(),
        ),
        "deliverables": [],
    }

    contract = compile_confirmed_creative_copy_contract(
        intent_spec=intent,
        authoritative_script=(23, "The original editable script."),
    )

    assert contract["copy_authority"] == "producer_draft_editable"
    assert contract["source_copy_role"] == "editable_source_script"
    assert contract["director_seed_voiceover"]["text"] == (
        "The original editable script."
    )


def test_requirements_mode_does_not_relock_stale_source_provenance():
    intent = {
        "delivery_mode": "single",
        "source_material_mode": "requirements",
        "user_goal": "Use these notes as requirements and write a new script.",
        "intent_manifest": _intent_manifest("Write a new script."),
        "deliverables": [],
    }

    assert compile_confirmed_creative_copy_contract(
        intent_spec=intent,
        authoritative_script=(24, "Old source text retained only for audit."),
    ) == {}


@pytest.mark.anyio
async def test_source_project_runs_semantic_reconciliation_before_confirmation(
    db_session,
    monkeypatch,
):
    user_message = (
        "先保持仓库剧情、演员和时长不变。"
        "现在请制作5条优化后具有差异化的原创视频。"
    )
    proposal = _proposal(
        video_count=5,
        video_duration_min_seconds=20,
        video_duration_max_seconds=20,
        preferred_segment_durations_seconds=[7, 7, 6],
        content_mode="general",
        confirmed_offer=None,
        promotion_evidence_quote=None,
    )
    deliverables = [
        {
            "ordinal": ordinal,
            "label": f"Video {ordinal}",
            "objective": f"Create differentiated original video {ordinal}",
            "relationship": "independent",
            "script_text": None,
            "source_message_id": None,
            "target_duration_seconds": 20,
            "must_preserve": [],
            "differentiation": [f"Distinct setting {ordinal}"],
        }
        for ordinal in range(1, 6)
    ]
    conflicting_contract = {
        "source_role": "reference_video",
        "fidelity": "adaptive",
        "execution_strategy": "full_regeneration",
        "transfer_mode": "semantic_structure",
        "source_media_reuse": "forbidden",
        "protected_requirements": ["保持仓库剧情、演员和时长不变"],
        "authorized_changes": [
            {
                "instruction": "制作5条优化后具有差异化的原创视频",
                "dimensions": ["story premise", "setting", "cast roles"],
                "start_seconds": None,
                "end_seconds": None,
                "evidence_quote": "制作5条优化后具有差异化的原创视频",
            }
        ],
        "creative_freedom": ["Create distinct original settings and casts"],
        "excluded_source_artifacts": ["source actors and pixels"],
        "success_checks": ["Five original videos exist"],
        "rationale": "The later request broadened the experiment.",
    }
    corrected_contract = {
        **conflicting_contract,
        "protected_requirements": [],
    }
    base_decision = {
        "status": "proposal_ready",
        "assistant_message": "已整理为5条差异化原创视频。",
        "missing_information": [],
        "proposal": proposal,
        "changed_fields": [],
        "change_evidence": {},
        "authoritative_script_message_id": None,
        "revised_authoritative_script": None,
        "script_revision_evidence": None,
        "intent_spec": {
            "delivery_mode": "independent_videos",
            "source_material_mode": "requirements",
            "user_goal": "Create five differentiated original videos.",
            "intent_manifest": _intent_manifest(
                evidence_quote="制作5条优化后具有差异化的原创视频",
                transformation_contract=conflicting_contract,
            ),
            "deliverables": deliverables,
        },
        "pending_decision_id": None,
    }
    corrected_decision = json.loads(json.dumps(base_decision))
    corrected_decision["intent_spec"]["intent_manifest"][
        "transformation_contract"
    ] = corrected_contract
    calls = []

    async def fake_create_response(self, **kwargs):
        calls.append(kwargs)
        if kwargs["metadata"].get("review_type") == "semantic_consistency":
            return {
                "output_text": json.dumps(
                    {
                        "verdict": "revised",
                        "issues": [
                            "An earlier global warehouse lock conflicts with the later five-video scope."
                        ],
                        "reviewed_intent_spec": corrected_decision[
                            "intent_spec"
                        ],
                        "assistant_question": None,
                    }
                )
            }, 40
        return {"output_text": json.dumps(base_decision)}, 30

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )

    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=7,
        user_id=19,
        message=user_message,
        session_key="semantic-reconciliation",
        product_id=None,
    )

    contract = decision.intent_spec.intent_manifest.transformation_contract
    assert contract is not None
    assert contract.protected_requirements == []
    assert len(calls) == 2
    assert calls[1]["metadata"]["review_type"] == "semantic_consistency"
    assert conversation.meta_json["semantic_review"]["verdict"] == "revised"
    assert conversation.meta_json["last_latency_ms"] == 70


@pytest.mark.anyio
async def test_semantic_review_repairs_a_structurally_invalid_reviewed_handoff():
    proposal = ContentProducerProposal.model_validate(
        _proposal(
            content_mode="general",
            promotion_evidence_quote=None,
        )
    )
    candidate_intent = content_producer.ContentProducerIntentSpec.model_validate({
        "delivery_mode": "single",
        "source_material_mode": "requirements",
        "user_goal": "Create one original video.",
        "intent_manifest": _intent_manifest("Create one original video."),
        "deliverables": [],
    })
    decision = content_producer.ContentProducerDecision(
        status="proposal_ready",
        assistant_message="Ready.",
        missing_information=[],
        proposal=proposal,
        intent_spec=candidate_intent,
    )
    invalid_intent = candidate_intent.model_copy(
        update={"user_goal": "Invalid reviewed handoff."}
    )
    repaired_intent = candidate_intent.model_copy(
        update={"user_goal": "Repaired reviewed handoff."}
    )
    responses = [invalid_intent, repaired_intent]
    calls = []

    class FakeClient:
        async def create_response(self, **kwargs):
            calls.append(kwargs)
            reviewed = responses[len(calls) - 1]
            return {
                "output_text": json.dumps({
                    "verdict": "revised",
                    "issues": ["Reconcile the reviewed handoff."],
                    "reviewed_intent_spec": reviewed.model_dump(mode="json"),
                    "reviewed_proposal": proposal.model_dump(mode="json"),
                    "assistant_question": None,
                })
            }, 15

    def validate_reviewed(candidate):
        if candidate.intent_spec.user_goal.startswith("Invalid"):
            raise ValueError("reviewed handoff is structurally inconsistent")

    review, latency_ms = await content_producer._semantic_review_decision(
        client=FakeClient(),
        packet={
            "conversation": [{
                "role": "user",
                "content": "Create one original video.",
            }],
            "authoritative_attachment_texts": [],
            "selected_product": None,
        },
        decision=decision,
        idempotency_base="semantic-reviewed-handoff",
        conversation_scope="semantic-reviewed-handoff",
        validate_reviewed_decision=validate_reviewed,
    )

    assert review.reviewed_intent_spec.user_goal == (
        "Repaired reviewed handoff."
    )
    assert latency_ms == 30
    assert len(calls) == 2
    repair_packet = json.loads(calls[1]["input_text"])
    assert "structurally inconsistent" in repair_packet["validation_error"]


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
        assert kwargs["metadata"]["prompt_version"] == content_producer.PRODUCER_PROMPT_VERSION
        assert "conversation" not in kwargs
        assert "session_key" not in kwargs
        assert "store" not in kwargs
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
async def test_producer_resolves_a_clear_natural_language_product_choice_and_regrounds(
    db_session,
    monkeypatch,
):
    gummies = HermesContentProduct(
        workspace_id=7,
        user_id=19,
        product_key="myupona::sleep-gummies::us",
        brand_name="MYUPONA",
        product_name="MYUPONA SLEEP EASY GUMMIES",
        market="US",
        product_brief="Sleep gummies",
        facts_json={"flavor": "blueberry"},
        meta_json={},
    )
    balm = HermesContentProduct(
        workspace_id=7,
        user_id=19,
        product_key="myupona::body-balm::us",
        brand_name="MYUPONA",
        product_name="MYUPONA Soothing Body Balm",
        market="US",
        product_brief="Body balm",
        facts_json={"format": "balm"},
        meta_json={},
    )
    db_session.add_all([gummies, balm])
    db_session.commit()
    calls = []

    async def fake_create_response(self, **kwargs):
        calls.append(dict(kwargs))
        packet = json.loads(kwargs["input_text"])
        if len(calls) == 1:
            assert packet["selected_product"] is None
            assert len(packet["available_products"]) == 2
            return {
                "output_text": json.dumps({
                    "status": "needs_input",
                    "assistant_message": "已识别为睡眠软糖。",
                    "missing_information": ["正在载入商品事实。"],
                    "product_selection": {
                        "action": "select",
                        "product_id": gummies.id,
                        "evidence_quote": "肯定是睡眠软糖啊",
                    },
                    "proposal": None,
                }, ensure_ascii=False)
            }, 10
        assert packet["selected_product"]["id"] == gummies.id
        assert packet["selected_product"]["stable_facts"]["flavor"] == "blueberry"
        return {
            "output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "已按睡眠软糖整理好3条短视频方案。",
                "missing_information": [],
                "product_selection": {"action": "keep"},
                "proposal": _proposal(),
            }, ensure_ascii=False)
        }, 12

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )

    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=7,
        user_id=19,
        message="你觉得呢？这个还用问吗？肯定是睡眠软糖啊",
        session_key="natural-product-selection",
        product_id=None,
        client_turn_id="natural_product_turn_01",
    )

    assert len(calls) == 2
    assert decision.status == "proposal_ready"
    assert conversation.meta_json["selected_product_id"] == gummies.id


def test_reference_link_reuses_same_url_and_only_latest_benchmark_is_active(
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        content_producer,
        "validate_share_url",
        lambda value: str(value).strip(),
    )
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="benchmark-links",
    )
    first = stage_producer_reference_link(
        db_session,
        conversation=conversation,
        user_id=19,
        source_url="https://www.tiktok.com/@brand/video/1",
        context_message="拆解第一个爆款。",
    )
    db_session.flush()
    same = stage_producer_reference_link(
        db_session,
        conversation=conversation,
        user_id=19,
        source_url="https://www.tiktok.com/@brand/video/1",
        context_message="重新讨论这个爆款。",
    )
    assert same.id == first.id
    assert same.meta_json["active_for_current_requirement"] is True

    second = stage_producer_reference_link(
        db_session,
        conversation=conversation,
        user_id=19,
        source_url="https://youtu.be/second-video",
        context_message="改用第二个爆款。",
    )
    db_session.flush()
    db_session.refresh(first)
    assert first.meta_json["active_for_current_requirement"] is False
    assert second.meta_json["active_for_current_requirement"] is True


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
async def test_producer_uses_one_fresh_upstream_chain_per_validation_attempt(
    db_session,
    monkeypatch,
):
    calls = []

    async def fake_create_response(self, **kwargs):
        calls.append(dict(kwargs))
        return {
            "output_text": json.dumps({
                "status": "proposal_ready",
                "assistant_message": "方案已经整理好。",
                "missing_information": [],
                "proposal": _proposal(
                    content_mode="general",
                    promotion_evidence_quote=None,
                ),
            })
        }, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message="请直接生成一条动画视频。",
        session_key="isolated-upstream-turn",
        product_id=None,
        client_turn_id="isolated_turn_01",
    )

    assert len(calls) == 1
    # SQL transcript + effective packet are the durable memory.  The upstream
    # call must remain stateless so a large packet is not appended to a hidden
    # provider conversation and compacted through extra model calls.
    assert "conversation" not in calls[0]
    assert "session_key" not in calls[0]
    assert "previous_response_id" not in calls[0]
    assert calls[0]["idempotency_key"].endswith(":attempt:1")


def test_producer_prompt_does_not_block_on_avoidable_unknown_product_shape():
    instructions = content_producer._instructions()
    assert "do not invent an unverified decorative shape" in instructions
    assert "close-up of the unknown detail" in instructions
    assert "explicit request to generate, make, create, start or execute" in instructions
    assert "Never translate a truthful or provider-safe request" in instructions
    assert "constrain facts and forbidden source" in instructions
    assert "not dramatic strength" in instructions
    assert "something to experience" in instructions
    assert "Conflict\"\n  is semantic" in instructions
    assert "without imposing one reusable plot" in instructions


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


@pytest.mark.anyio
async def test_created_project_session_accepts_a_followup_without_overwriting_history(
    db_session,
    monkeypatch,
):
    original = _proposal(
        content_mode="general",
        promotion_evidence_quote=None,
    )
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="continued-project-session",
    )
    conversation.meta_json = {
        "session_key": "continued-project-session",
        "status": "created",
        "created_project_id": 91,
        "created_project_key": "cf_original_delivery",
        "proposal": original,
        "proposal_prompt_version": content_producer.PRODUCER_PROMPT_VERSION,
    }
    db_session.add(conversation)
    db_session.commit()
    followup = "在原要求基础上再增加2条，其他要求不变。"

    async def fake_create_response(self, **kwargs):
        packet = json.loads(kwargs["input_text"])
        assert packet["current_working_proposal"]["video_count"] == 3
        assert packet["project_history"]["followup_parent_project_key"] == "cf_original_delivery"
        assert packet["project_history"]["completed_or_started_projects"][0]["project_id"] == 91
        return {"output_text": json.dumps({
            "status": "proposal_ready",
            "assistant_message": "已继承原要求，整理为2条追加视频。原项目不会被覆盖。",
            "missing_information": [],
            "changed_fields": ["video_count"],
            "change_evidence": {"video_count": followup},
            "proposal": _proposal(
                content_mode="general",
                video_count=2,
                promotion_evidence_quote=None,
            ),
        })}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )

    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=followup,
        session_key="continued-project-session",
        product_id=None,
        client_turn_id="continued_project_turn",
    )

    meta = dict(conversation.meta_json or {})
    assert decision.proposal.video_count == 2
    assert meta["status"] == "proposal_ready"
    assert meta["followup_parent_project_id"] == 91
    assert meta["created_projects"][0]["project_key"] == "cf_original_delivery"
    assert "created_project_id" not in meta


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

    followup_project = HermesContentFactoryProject(
        project_key="cf_attachment_followup",
        workspace_id=7,
        user_id=19,
        title="Attachment follow-up",
        product_name="",
        market="US",
        status="draft",
        current_stage="DIRECTOR",
        config_json={},
        state_json={},
    )
    db_session.add(followup_project)
    db_session.flush()
    followup_rows = copy_producer_attachments_to_project(
        db_session,
        conversation=conversation,
        project=followup_project,
        user_id=19,
        storage_root=tmp_path / "factory",
        browser_inbox=tmp_path / "bridge",
    )
    db_session.commit()
    assert len(followup_rows) == 1
    assert followup_rows[0].project_id == followup_project.id
    assert Path(followup_rows[0].file_path).is_file()
    db_session.refresh(attachment)
    assert attachment.project_asset_id == rows[0].id


@pytest.mark.anyio
async def test_docx_handoff_is_extracted_for_producer_and_transferred_as_project_brief(
    db_session,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(content_producer, "PRODUCER_STORAGE_ROOT", tmp_path / "intake")
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="document-handoff-session",
    )
    db_session.commit()
    document = io.BytesIO()
    with zipfile.ZipFile(document, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body>
                <w:p><w:r><w:t>Six-video brand story handoff</w:t></w:r></w:p>
                <w:p><w:r><w:t>Keep all six scripts verbatim and preserve one character.</w:t></w:r></w:p>
              </w:body>
            </w:document>""",
        )
    document.seek(0)
    upload = UploadFile(
        filename="handoff.docx",
        file=document,
        headers=Headers({
            "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }),
    )
    attachment = await save_producer_attachment(
        db_session,
        conversation=conversation,
        user_id=19,
        upload=upload,
        kind="supporting_material",
    )
    db_session.commit()

    assert attachment.kind == "brief_document"
    assert attachment.preview_path is None
    assert attachment.analysis_json["document_text"] == (
        "Six-video brand story handoff\nKeep all six scripts verbatim and preserve one character."
    )
    assert "document_text" not in content_producer.producer_attachment_out(attachment)["analysis"]

    output = {
        "status": "proposal_ready",
        "assistant_message": "我已读取交接文档，并会保持六条脚本与统一人物。",
        "missing_information": [],
        "proposal": _proposal(content_mode="general", promotion_evidence_quote=None),
    }

    async def fake_create_response(self, **kwargs):
        packet = json.loads(kwargs["input_text"])
        assert kwargs["input_items"] is None
        assert packet["authoritative_attachment_texts"][0]["original_name"] == "handoff.docx"
        assert "Keep all six scripts verbatim" in packet["authoritative_attachment_texts"][0]["content"]
        assert "document_text" not in packet["user_attachments"][0]["technical_summary"]
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
        message="按交接文档创建这个系列。",
        session_key="document-handoff-session",
        product_id=None,
        client_turn_id="document_handoff_turn",
    )

    project = HermesContentFactoryProject(
        project_key="cf_document_handoff",
        workspace_id=7,
        user_id=19,
        title="Document handoff",
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
    assert rows[0].stage == "SOURCE"
    assert rows[0].kind == "source"
    assert rows[0].meta_json["asset_role"] == "project_brief"
    assert rows[0].meta_json["producer_attachment_kind"] == "brief_document"


@pytest.mark.anyio
async def test_xlsx_matrix_is_extracted_with_sheet_row_and_cell_structure(
    db_session,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(content_producer, "PRODUCER_STORAGE_ROOT", tmp_path / "intake")
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="xlsx-matrix-session",
    )
    db_session.commit()
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="50 Video Matrix" sheetId="1" r:id="rId1"/>
              <sheet name="Locked Rules" sheetId="2" r:id="rId2"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/>
              <Relationship Id="rId2" Target="worksheets/sheet2.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <si><t>Video ID</t></si><si><t>Hook</t></si>
              <si><t>3:07 a.m. again?</t></si><si><t>Keep each script complete.</t></si>
            </sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
                <row r="2"><c r="A2"><v>1</v></c><c r="B2" t="s"><v>2</v></c>
                  <c r="C2" t="inlineStr"><is><t>Fast TikTok conversion script</t></is></c></row>
              </sheetData>
            </worksheet>""",
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row r="1"><c r="A1" t="s"><v>3</v></c></row></sheetData>
            </worksheet>""",
        )
    workbook.seek(0)
    attachment = await save_producer_attachment(
        db_session,
        conversation=conversation,
        user_id=19,
        upload=UploadFile(
            filename="MYUPONA_50_video_matrix.xlsx",
            file=workbook,
            headers=Headers({
                "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }),
        ),
        kind="supporting_material",
    )
    db_session.commit()

    extracted = attachment.analysis_json["document_text"]
    assert attachment.kind == "brief_document"
    assert attachment.analysis_json["document_format"] == "xlsx"
    assert "[Sheet: 50 Video Matrix]" in extracted
    assert "Row 2: A=1 | B=3:07 a.m. again? | C=Fast TikTok conversion script" in extracted
    assert "[Sheet: Locked Rules]" in extracted
    assert "A=Keep each script complete." in extracted


@pytest.mark.anyio
async def test_pptx_slides_and_speaker_notes_are_extracted(
    db_session,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(content_producer, "PRODUCER_STORAGE_ROOT", tmp_path / "intake")
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=7,
        user_id=19,
        session_key="pptx-brief-session",
    )
    db_session.commit()
    presentation = io.BytesIO()
    with zipfile.ZipFile(presentation, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <p:cSld><a:p><a:r><a:t>Opening visual hook</a:t></a:r></a:p>
              <a:p><a:r><a:t>Show the product naturally</a:t></a:r></a:p></p:cSld>
            </p:sld>""",
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            """<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:p><a:r><a:t>Voiceover must stay energetic.</a:t></a:r></a:p>
            </p:notes>""",
        )
    presentation.seek(0)
    attachment = await save_producer_attachment(
        db_session,
        conversation=conversation,
        user_id=19,
        upload=UploadFile(
            filename="creative_brief.pptx",
            file=presentation,
            headers=Headers({
                "content-type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }),
        ),
        kind="supporting_material",
    )
    db_session.commit()

    extracted = attachment.analysis_json["document_text"]
    assert "[Slide 1]" in extracted
    assert "Opening visual hook" in extracted
    assert "Show the product naturally" in extracted
    assert "[Speaker notes 1]" in extracted
    assert "Voiceover must stay energetic." in extracted


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
async def test_confirmed_parameters_survive_prompt_revision(
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
        session_key="prompt-revision-confirm-session",
        product_id=None,
    )
    meta = dict(conversation.meta_json)
    meta["proposal_prompt_version"] = "earlier_prompt_revision"
    conversation.meta_json = meta
    db_session.add(conversation)
    db_session.commit()

    confirmed_conversation, proposal, product, _ = confirmed_project_parameters(
        db_session,
        workspace_id=9,
        user_id=22,
        session_key="prompt-revision-confirm-session",
    )

    assert confirmed_conversation.id == conversation.id
    assert proposal.video_count == 3
    assert product is None


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


def test_producer_accepts_user_duration_before_live_provider_planning():
    proposal = ContentProducerProposal.model_validate(
        _proposal(
            video_duration_min_seconds=41,
            video_duration_max_seconds=49,
        )
    )

    assert proposal.video_duration_min_seconds == 41
    assert proposal.video_duration_max_seconds == 49


def test_producer_proposal_persists_explicit_text_to_video_contract():
    proposal = ContentProducerProposal.model_validate(
        _proposal(video_generation_mode="text_to_video")
    )

    assert proposal.video_generation_mode == "text_to_video"
    assert '"video_generation_mode": "text_to_video"' in json.dumps(
        proposal.model_dump(mode="json")
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


@pytest.mark.anyio
async def test_multi_script_package_is_validated_per_deliverable_not_as_one_video(
    db_session,
    monkeypatch,
):
    first = " ".join(["first"] * 120)
    second = " ".join(["second"] * 120)
    package = f"VIDEO 1\n{first}\n\nVIDEO 2\n{second}"

    async def fake_create_response(self, **kwargs):
        packet = json.loads(kwargs["input_text"])
        if kwargs["metadata"].get("review_type") == "semantic_consistency":
            return {"output_text": json.dumps({
                "verdict": "pass",
                "issues": [],
                "reviewed_intent_spec": packet["candidate_intent_spec"],
                "reviewed_proposal": packet["candidate_proposal"],
                "assistant_question": None,
            })}, 10
        source_id = packet["authoritative_source_texts"][0]["message_id"]
        return {"output_text": json.dumps({
            "status": "proposal_ready",
            "assistant_message": "已识别为两条独立视频和两份独立文案。",
            "missing_information": [],
            "changed_fields": [],
            "change_evidence": {},
            "authoritative_script_message_id": source_id,
            "intent_spec": {
                "delivery_mode": "independent_videos",
                "source_material_mode": "multi_script_package",
                "user_goal": "Produce two independent videos from two supplied scripts.",
                "intent_manifest": _intent_manifest("VIDEO 1"),
                "deliverables": [
                    {
                        "ordinal": 1,
                        "label": "Video 1",
                        "objective": "Produce the first script",
                        "relationship": "independent",
                        "script_text": first,
                        "source_message_id": source_id,
                        "target_duration_seconds": 40,
                    },
                    {
                        "ordinal": 2,
                        "label": "Video 2",
                        "objective": "Produce the second script",
                        "relationship": "independent",
                        "script_text": second,
                        "source_message_id": source_id,
                        "target_duration_seconds": 40,
                    },
                ],
            },
            "proposal": _proposal(
                content_mode="general",
                video_count=2,
                video_duration_min_seconds=40,
                video_duration_max_seconds=40,
                promotion_evidence_quote=None,
            ),
        })}, 10

    monkeypatch.setattr(
        content_producer.HermesContentProducerClient,
        "create_response",
        fake_create_response,
    )
    conversation, decision = await run_producer_turn(
        db_session,
        workspace_id=8,
        user_id=20,
        message=package,
        session_key="multi-script-package",
        product_id=None,
    )

    assert decision.status == "proposal_ready"
    assert decision.intent_spec.delivery_mode == "independent_videos"
    assert len(decision.intent_spec.deliverables) == 2
    assert conversation.meta_json["pending_decision_id"].startswith("producer-")
    assert conversation.meta_json["intent_spec"]["deliverables"][1]["script_text"] == second


def test_current_authoritative_script_version_wins_over_echoed_source_id(
    db_session,
):
    conversation = get_or_create_producer_conversation(
        db_session,
        workspace_id=8,
        user_id=20,
        session_key="latest-script-version",
    )
    source = content_producer.repository.add_message(
        db_session,
        conversation=conversation,
        workspace_id=8,
        user_id=20,
        role="user",
        content_text="Original script that must no longer be used.",
        content_json={},
    )
    revised = "Current revised script is authoritative."
    conversation.meta_json = {
        "authoritative_script_message_id": source.id,
        "authoritative_script_current_version": 2,
        "authoritative_script_versions": [{
            "version": 2,
            "source_message_id": source.id,
            "sha256": content_producer.hashlib.sha256(revised.encode()).hexdigest(),
            "text": revised,
            "revised": True,
        }],
    }
    db_session.add(conversation)
    db_session.commit()
    decision = content_producer.ContentProducerDecision(
        status="proposal_ready",
        assistant_message="Ready.",
        missing_information=[],
        authoritative_script_message_id=source.id,
        proposal=ContentProducerProposal.model_validate(
            _proposal(content_mode="general", promotion_evidence_quote=None)
        ),
    )

    assert content_producer._decision_script_text(
        db_session,
        conversation=conversation,
        decision=decision,
    ) == revised


def test_independent_intent_requires_one_deliverable_per_video():
    decision = content_producer.ContentProducerDecision(
        status="proposal_ready",
        assistant_message="Ready.",
        missing_information=[],
        proposal=ContentProducerProposal.model_validate(
            _proposal(content_mode="general", video_count=3, promotion_evidence_quote=None)
        ),
        intent_spec={
            "delivery_mode": "independent_videos",
            "source_material_mode": "requirements",
            "user_goal": "Create three different videos.",
            "intent_manifest": _intent_manifest(),
            "deliverables": [{
                "ordinal": 1,
                "label": "Only one",
                "objective": "This is incomplete",
                "relationship": "independent",
            }],
        },
    )

    with pytest.raises(ValueError, match="exactly one ordered deliverable"):
        content_producer._validate_intent_spec(decision)


def test_dense_locked_script_cannot_pad_thirty_words_across_fifty_five_seconds():
    script = " ".join(["spoken"] * 30)
    decision = content_producer.ContentProducerDecision(
        status="proposal_ready",
        assistant_message="Ready.",
        missing_information=[],
        proposal=ContentProducerProposal.model_validate(
            _proposal(
                content_mode="general",
                video_count=1,
                video_duration_min_seconds=55,
                video_duration_max_seconds=55,
                promotion_evidence_quote=None,
            )
        ),
        intent_spec={
            "delivery_mode": "single",
            "source_material_mode": "single_script",
            "user_goal": "Create one fast conversion video.",
            "intent_manifest": _intent_manifest(),
            "deliverables": [{
                "ordinal": 1,
                "label": "Video",
                "objective": "Fast conversion delivery",
                "relationship": "standalone",
                "script_text": script,
                "target_duration_seconds": 55,
            }],
        },
    )

    with pytest.raises(ValueError, match="spoken_density=dense"):
        content_producer._validate_intent_spec(decision)


def test_pacing_and_spoken_density_are_semantic_review_responsibilities():
    instructions = content_producer._semantic_review_instructions()

    assert "not by matching isolated words" in instructions
    assert "Fast visual cutting" in instructions
    assert not hasattr(content_producer, "_validate_pacing_density")
