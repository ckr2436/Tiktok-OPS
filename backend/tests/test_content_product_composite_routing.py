import pytest

from app.services.ai_routing.router import AiGatewayError
from app.services.hermes_agent import content_factory_api


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
    monkeypatch.setattr(content_factory_api, "call_chat_with_failover", fail)

    with pytest.raises(content_factory_api.ContentFactoryApiError) as exc:
        content_factory_api._routed_multimodal_completion(
            object(),
            payload={"messages": [{"role": "user", "content": "inspect"}]},
            request_id="stable-id",
        )

    assert str(exc.value) == "CONTENT_VISUAL_REVIEW_ROUTING_FAILED: QUOTA"
    assert "balance" not in str(exc.value)
