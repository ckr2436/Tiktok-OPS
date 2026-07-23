from __future__ import annotations

import base64
import httpx
import pytest

from app.services.bandianwa.tasks import build_bandianwa_payload
from app.services.google_gemini.tasks import _find_video_payload, _prompt_with_reference_tags
from app.services.toapis.client import ToApisApiError, ToApisVideoClient


def test_bandianwa_omni_uses_documented_string_seconds_and_size():
    payload = build_bandianwa_payload({
        "model": "omni_flash",
        "prompt": "A short product scene",
        "seconds": 8,
        "aspect_ratio": "9:16",
        "generate_audio": True,
    })
    assert payload["seconds"] == "8"
    assert payload["size"] == "1080x1920"
    assert payload["input_reference"] == "[]"


def test_google_rest_video_is_extracted_from_steps_inline_data():
    video = b"\x00\x00\x00\x18ftypisom-test-video"
    payload = {
        "steps": [{
            "type": "model_output",
            "content": [{
                "type": "video",
                "mime_type": "video/mp4",
                "data": base64.b64encode(video).decode("ascii"),
            }],
        }],
    }
    url, data = _find_video_payload(payload)
    assert url is None
    assert data == video


def test_google_reference_tags_follow_official_zero_based_roles():
    prompt = _prompt_with_reference_tags("A woman holds the product.", 2)
    assert "<IMAGE_REF_0>@Image1" in prompt
    assert "<IMAGE_REF_1>@Image2" in prompt

    first_frame = _prompt_with_reference_tags("She walks into frame.", 2, first_last=True)
    assert "<FIRST_FRAME>@Image1" in first_frame
    assert "<IMAGE_REF_0>@Image2" in first_frame


@pytest.mark.asyncio
async def test_toapis_image_submission_uses_stable_business_id_and_uploaded_refs(monkeypatch):
    client = ToApisVideoClient(api_key="test", base_url="https://toapis.example")
    captured = {}

    async def reference_url(value):
        return f"https://files.example/{value.rsplit('/', 1)[-1]}"

    async def request(method, path, **kwargs):
        if method == "GET":
            raise ToApisApiError("ToAPIs HTTP 404: not found")
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "img_task_1", "status": "queued"}

    monkeypatch.setattr(client, "_reference_image_url", reference_url)
    monkeypatch.setattr(client, "_request", request)

    response = await client.create_image_task(
        prompt="Adult editorial animation",
        size="1024x1792",
        images=["/tmp/anchor.png"],
        model="nano_banana_pro",
        idempotency_key="cf:168:v24:reference-3",
    )

    assert response["id"] == "img_task_1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/images/generations"
    assert captured["json"]["model"] == "gpt-image-2"
    assert captured["json"]["size"] == "9:16"
    assert captured["json"]["resolution"] == "1k"
    assert captured["json"]["reference_images"] == ["https://files.example/anchor.png"]
    assert captured["json"]["client_business_id"].startswith("cf_img_")


@pytest.mark.asyncio
async def test_toapis_image_status_uses_generation_endpoint(monkeypatch):
    client = ToApisVideoClient(api_key="test", base_url="https://toapis.example")
    captured = {}

    async def request(method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {"id": "img_task_1", "status": "completed"}

    monkeypatch.setattr(client, "_request", request)

    response = await client.get_image_task(task_id="img_task_1")

    assert response["status"] == "completed"
    assert captured["method"] == "GET"
    assert captured["path"] == "/v1/images/generations/img_task_1"


@pytest.mark.asyncio
async def test_toapis_request_wraps_transport_failure_for_bounded_provider_retry(monkeypatch):
    client = ToApisVideoClient(api_key="test", base_url="https://toapis.example")

    class FailingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FailingAsyncClient())

    with pytest.raises(ToApisApiError, match="transport error: RemoteProtocolError"):
        await client.get_image_task(task_id="img_task_1")
