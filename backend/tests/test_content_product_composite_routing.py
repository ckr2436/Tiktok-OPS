import pytest
import json

from app.services.ai_routing.router import AiGatewayError
from app.services.hermes_agent import content_factory_api


def _passing_product_video_review() -> dict:
    return {
        "status": "pass",
        "product_present": True,
        "identity_verdict": "match",
        "scene_integration": "natural",
        "temporal_consistency": "stable",
        "pasted_or_white_background": False,
        "gross_deformation": False,
        "duplicate_unscripted_product": False,
        "confidence": 0.96,
        "observed_facts": ["package remains visible"],
        "blocking_reasons": [],
    }


def test_product_composite_inspection_uses_configured_multimodal_group(monkeypatch):
    captured = {}

    async def fake_call(_db, **kwargs):
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": '{"status":"selected"}'}}]
        }

    monkeypatch.setenv(
        "HERMES_PRODUCT_COMPOSITE_MODEL",
        "gmv-content-visual-inspector-v1",
    )
    monkeypatch.setenv(
        "HERMES_PRODUCT_COMPOSITE_WORKLOAD",
        "content_visual_inspector",
    )
    monkeypatch.setattr(content_factory_api, "call_chat_with_failover", fake_call)

    result = content_factory_api._routed_multimodal_completion(
        object(),
        payload={
            "model": "must-not-control-routing",
            "messages": [{"role": "user", "content": "inspect"}],
            "max_tokens": 50,
        },
        request_id="stable-id",
    )

    assert result["choices"]
    assert captured["logical_model_id"] == "gmv-content-visual-inspector-v1"
    assert captured["capability"] == "multimodal"
    assert captured["workload"] == "content_visual_inspector"
    assert captured["request_id"] == "stable-id"
    assert captured["payload_overrides"] == {"max_tokens": 50}


def test_product_composite_route_failure_does_not_leak_provider_body(monkeypatch):
    async def fail(_db, **_kwargs):
        raise AiGatewayError(
            "sensitive provider body and balance",
            error_class="QUOTA",
            status_code=403,
        )

    monkeypatch.setenv("HERMES_PRODUCT_COMPOSITE_MODEL", "visual-role")
    monkeypatch.setenv(
        "HERMES_PRODUCT_COMPOSITE_WORKLOAD",
        "content_visual_inspector",
    )
    monkeypatch.setattr(content_factory_api, "call_chat_with_failover", fail)

    with pytest.raises(content_factory_api.ContentFactoryApiError) as exc:
        content_factory_api._routed_multimodal_completion(
            object(),
            payload={"messages": [{"role": "user", "content": "inspect"}]},
            request_id="stable-id",
        )

    assert str(exc.value) == "CONTENT_VISUAL_REVIEW_ROUTING_FAILED: QUOTA"
    assert "balance" not in str(exc.value)


def test_product_video_review_accepts_responses_envelope(
    monkeypatch,
    tmp_path,
):
    sheet = tmp_path / "sheet.jpg"
    product = tmp_path / "product.png"
    sheet.write_bytes(b"s" * 2048)
    product.write_bytes(b"p" * 2048)
    verdict = _passing_product_video_review()
    calls = []
    monkeypatch.setattr(content_factory_api, "_data_url", lambda _path: "data:image/png;base64,eA==")
    monkeypatch.setattr(
        content_factory_api,
        "_routed_multimodal_completion",
        lambda *_args, **kwargs: calls.append(kwargs) or {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(verdict)}],
            }],
        },
    )

    result = content_factory_api.review_provider_rendered_product_video_api(
        object(),
        contact_sheet_path=str(sheet),
        product_reference_path=str(product),
        execution_id="exec-responses",
    )

    assert result["status"] == "pass"
    assert result["blocking"] is False
    assert len(calls) == 1
    system = calls[0]["payload"]["messages"][0]["content"]
    assert "PRIMARY PRODUCT IDENTITY" in system
    assert "SECONDARY LABEL DETAIL" in system
    assert "net-weight text" in system
    assert "garbled" in system
    assert "large competing brand" in system


def test_product_video_review_receives_signed_use_state_context(
    monkeypatch,
    tmp_path,
):
    sheet = tmp_path / "sheet.jpg"
    product = tmp_path / "product.png"
    sheet.write_bytes(b"s" * 2048)
    product.write_bytes(b"p" * 2048)
    verdict = _passing_product_video_review()
    calls = []
    monkeypatch.setattr(
        content_factory_api,
        "_data_url",
        lambda _path: "data:image/png;base64,eA==",
    )
    monkeypatch.setattr(
        content_factory_api,
        "_routed_multimodal_completion",
        lambda *_args, **kwargs: calls.append(kwargs) or {
            "choices": [{"message": {"content": json.dumps(verdict)}}]
        },
    )

    content_factory_api.review_provider_rendered_product_video_api(
        object(),
        contact_sheet_path=str(sheet),
        product_reference_path=str(product),
        execution_id="exec-open-state",
        segment_context={
            "segment_goal": "Open the same jar and apply a small amount.",
            "timeline": [{"action": "Remove the lid, then sample the balm."}],
        },
    )

    system = calls[0]["payload"]["messages"][0]["content"]
    user = calls[0]["payload"]["messages"][1]["content"][0]["text"]
    assert "closed packshot" in system
    assert "never waives primary identity" in system
    assert "Remove the lid" in user


def test_product_video_review_retries_empty_compatible_response(
    monkeypatch,
    tmp_path,
):
    sheet = tmp_path / "sheet.jpg"
    product = tmp_path / "product.png"
    sheet.write_bytes(b"s" * 2048)
    product.write_bytes(b"p" * 2048)
    verdict = _passing_product_video_review()
    request_ids = []

    def routed(*_args, **kwargs):
        request_ids.append(kwargs["request_id"])
        if len(request_ids) == 1:
            return {"choices": [{"message": {"reasoning_content": "not final"}}]}
        return {"choices": [{"message": {"content": json.dumps(verdict)}}]}

    monkeypatch.setattr(content_factory_api, "_data_url", lambda _path: "data:image/png;base64,eA==")
    monkeypatch.setattr(content_factory_api, "_routed_multimodal_completion", routed)

    result = content_factory_api.review_provider_rendered_product_video_api(
        object(),
        contact_sheet_path=str(sheet),
        product_reference_path=str(product),
        execution_id="exec-retry",
    )

    assert result["status"] == "pass"
    assert len(request_ids) == 2
    assert request_ids[0] != request_ids[1]
