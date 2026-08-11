from __future__ import annotations

import base64

import httpx
import pytest

from app.services.sub2api.client import Sub2ApiApiError, Sub2ApiImageClient


class _FakeAsyncClient:
    requests: list[tuple[str, str, dict]] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, path, headers=None, **kwargs):
        self.requests.append((method, path, {"headers": headers, **kwargs}))
        url = path if str(path).startswith("http") else f"http://sub2api.test{path}"
        request = httpx.Request(method, url)
        return httpx.Response(
            202 if method == "POST" else 200,
            request=request,
            json={
                "task_id": "imgtask_1",
                "status": "processing" if method == "POST" else "completed",
            },
        )


@pytest.mark.asyncio
async def test_sub2api_text_image_submit_is_async_and_idempotent(monkeypatch):
    _FakeAsyncClient.requests = []
    monkeypatch.setattr(
        "app.services.sub2api.client.httpx.AsyncClient", _FakeAsyncClient
    )
    client = Sub2ApiImageClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )

    result = await client.create_image_task(
        prompt="draw one mug",
        size="1024x1024",
        model="gpt-image-2",
        idempotency_key="project-stage-reference-1",
    )

    assert result["task_id"] == "imgtask_1"
    method, path, request = _FakeAsyncClient.requests[0]
    assert (method, path) == ("POST", "/images/generations/async")
    assert request["json"]["model"] == "gpt-image-2"
    assert request["headers"]["Idempotency-Key"].startswith("gmv-cf-")
    assert len(request["headers"]["X-Idempotency-Fingerprint"]) == 64
    assert "secret" not in str(request["json"])


@pytest.mark.asyncio
async def test_sub2api_reference_submit_uses_async_edit_and_multipart(monkeypatch):
    _FakeAsyncClient.requests = []
    monkeypatch.setattr(
        "app.services.sub2api.client.httpx.AsyncClient", _FakeAsyncClient
    )
    client = Sub2ApiImageClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )
    image = b"fake-png-bytes"
    data_url = "data:image/png;base64," + base64.b64encode(image).decode()

    await client.create_image_task(
        prompt="keep the package identity",
        size="1024x1792",
        images=[data_url, data_url],
        idempotency_key="project-stage-reference-2",
    )

    method, path, request = _FakeAsyncClient.requests[0]
    assert (method, path) == ("POST", "/images/edits/async")
    assert request["data"]["input_fidelity"] == "high"
    assert request["data"]["size"] == "1024x1792"
    assert len(request["files"]) == 2
    assert all(field == "image" for field, _file in request["files"])
    assert all(file[1] == image for _field, file in request["files"])


@pytest.mark.asyncio
async def test_sub2api_poll_uses_durable_task_id(monkeypatch):
    _FakeAsyncClient.requests = []
    monkeypatch.setattr(
        "app.services.sub2api.client.httpx.AsyncClient", _FakeAsyncClient
    )
    client = Sub2ApiImageClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )

    result = await client.get_image_task(task_id="imgtask_1")

    assert result["status"] == "completed"
    assert _FakeAsyncClient.requests[0][1] == "/images/tasks/imgtask_1"


@pytest.mark.asyncio
async def test_sub2api_rejects_nano_banana_pro_without_calling_upstream():
    client = Sub2ApiImageClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )
    with pytest.raises(Sub2ApiApiError) as caught:
        await client.create_image_task(
            prompt="render one image",
            size="1024x1024",
            model="nano_banana_pro",
            idempotency_key="nano-must-not-use-sub2api",
        )
    error = caught.value
    assert error.code == "unsupported_image_model"
    assert error.retryable is False
