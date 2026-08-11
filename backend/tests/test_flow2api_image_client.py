from __future__ import annotations

import base64

import httpx
import pytest

from app.services.flow2api.client import Flow2ApiError, Flow2ApiImageClient


class _FakeAsyncClient:
    requests: list[dict] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, path, **kwargs):
        self.requests.append({"path": path, **kwargs})
        return httpx.Response(
            200,
            request=httpx.Request("POST", f"http://flow.test{path}"),
            json={
                "choices": [{
                    "message": {
                        "content": "![generated](https://flow-content.google/result.jpg)"
                    }
                }],
                "url": "https://flow-content.google/result.jpg",
            },
        )


@pytest.mark.asyncio
async def test_flow2api_nano_uses_ordered_multimodal_parts_and_body_idempotency(
    monkeypatch,
):
    _FakeAsyncClient.requests = []
    monkeypatch.setattr(
        "app.services.flow2api.client.httpx.AsyncClient",
        _FakeAsyncClient,
    )
    first = "data:image/png;base64," + base64.b64encode(b"first").decode()
    second = "data:image/png;base64," + base64.b64encode(b"second").decode()
    client = Flow2ApiImageClient(
        api_key="secret", base_url="http://flow.test/v1"
    )

    result = await client.create_image_task(
        prompt="preserve reference order",
        size="1024x1792",
        images=[first, second],
        model="nano_banana_pro",
        idempotency_key="project-stage-board-1",
    )

    assert result["status"] == "completed"
    assert result["provider_model"] == "gemini-3.0-pro-image"
    assert result["data"] == [{
        "url": "https://flow-content.google/result.jpg"
    }]
    request = _FakeAsyncClient.requests[0]
    assert request["path"] == "/chat/completions"
    payload = request["json"]
    assert payload["model"] == "gemini-3.0-pro-image"
    assert payload["generationConfig"]["imageConfig"] == {
        "aspectRatio": "9:16",
        "imageSize": "1K",
    }
    assert payload["gmv_idempotency_key"].startswith("gmv-cf-")
    assert "project-stage-board-1" not in payload["gmv_idempotency_key"]
    parts = payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "preserve reference order"}
    assert [part["image_url"]["url"] for part in parts[1:]] == [first, second]


@pytest.mark.asyncio
async def test_flow2api_requires_stable_idempotency_key_before_submission():
    client = Flow2ApiImageClient(
        api_key="secret", base_url="http://flow.test/v1"
    )
    with pytest.raises(Flow2ApiError) as caught:
        await client.create_image_task(
            prompt="draw one square",
            size="1024x1024",
            model="nano_banana_pro",
        )
    assert caught.value.code == "idempotency_key_required"
    assert caught.value.retryable is False


def test_flow2api_extracts_openai_string_image_url_part():
    assert Flow2ApiImageClient._extract_url({
        "choices": [{
            "message": {
                "content": [{
                    "type": "image_url",
                    "image_url": "https://flow-content.google/string.jpg",
                }],
            },
        }],
    }) == "https://flow-content.google/string.jpg"
