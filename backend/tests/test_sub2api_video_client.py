from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.sub2api.client import Sub2ApiApiError
from app.services.sub2api.video_client import Sub2ApiVideoClient
from app.services.sub2api.video_tasks import _generation_idempotency_key, _model_name
from app.services.ai_video.accounts import (
    OMNI_FLASH_MODEL,
    SUB2API_PROVIDER_KEY,
    provider_reference_limit,
)


class _FakeAsyncClient:
    request: dict | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, path, headers=None, json=None):
        type(self).request = {"path": path, "headers": headers, "json": json}
        request = httpx.Request("POST", f"http://sub2api.test{path}")
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-flow-1",
                "choices": [{
                    "message": {
                        "content": "Video: http://127.0.0.1:19082/tmp/result.mp4"
                    }
                }],
            },
        )


class _UnavailableAsyncClient(_FakeAsyncClient):
    async def post(self, path, headers=None, json=None):
        request = httpx.Request("POST", f"http://sub2api.test{path}")
        return httpx.Response(
            503,
            request=request,
            headers={"Retry-After": "60"},
            json={"error": {"message": "Service temporarily unavailable"}},
        )


class _InvalidRequestAsyncClient(_FakeAsyncClient):
    async def post(self, path, headers=None, json=None):
        request = httpx.Request("POST", f"http://sub2api.test{path}")
        return httpx.Response(
            502,
            request=request,
            json={
                "error": {
                    "message": (
                        "Flow API request failed: HTTP Error 400: "
                        "Request contains an invalid argument."
                    ),
                    "code": "upstream_error",
                }
            },
        )


class _IdempotencyConflictAsyncClient(_FakeAsyncClient):
    async def post(self, path, headers=None, json=None):
        request = httpx.Request("POST", f"http://sub2api.test{path}")
        return httpx.Response(
            409,
            request=request,
            json={
                "error": {
                    "message": "Idempotency key was reused with a different request",
                    "code": "idempotency_conflict",
                }
            },
        )


class _DelayedAsyncClient(_FakeAsyncClient):
    async def post(self, path, headers=None, json=None):
        await asyncio.sleep(0.04)
        return await super().post(path, headers=headers, json=json)


def test_flow_model_compilation_is_deterministic():
    assert _model_name({"duration": 8, "aspect_ratio": "9:16"}, 0) == (
        "gemini_omni_t2v_portrait_8s"
    )
    assert _model_name(
        {"seconds": 10, "aspect_ratio": "16:9", "resolution": "1080p"},
        3,
    ) == "gemini_omni_r2v_10s_1080p"


def test_flow_reference_limit_comes_from_provider_capability():
    assert provider_reference_limit(SUB2API_PROVIDER_KEY, OMNI_FLASH_MODEL) == 7


def test_flow_idempotency_identity_is_stable_per_generation():
    task = type("Task", (), {})()
    task.workspace_id = 3
    task.id = 3587
    task.result_json = {"__local": {}}
    assert _generation_idempotency_key(task) == "workspace:3:task:3587"

    task.result_json = {"__local": {"generation_epoch": 2, "manual_retry_count": 2}}
    assert _generation_idempotency_key(task) == (
        "workspace:3:task:3587:generation:2"
    )


def test_flow_idempotency_identity_repairs_legacy_manual_retry_metadata():
    task = type("Task", (), {})()
    task.workspace_id = 3
    task.id = 3587
    task.result_json = {"__local": {"manual_retry_count": 1}}

    assert _generation_idempotency_key(task) == (
        "workspace:3:task:3587:generation:1"
    )


def test_video_url_parser_accepts_extensionless_google_flow_result():
    url = (
        "https://flow-content.google/video/2ed61f93-f54a-44f9-9e45-ab9e6f9d68e7"
        "?Expires=123&KeyName=test&Signature=redacted"
    )
    payload = {
        "choices": [{
            "message": {
                "content": f"```html\n<video src='{url}' controls></video>\n```"
            }
        }],
        "url": url,
    }

    assert Sub2ApiVideoClient._video_urls(payload) == [url]


def test_video_url_parser_rejects_untrusted_extensionless_video_path():
    payload = {
        "choices": [{
            "message": {
                "content": "https://example.invalid/video/not-a-provider-result"
            }
        }]
    }

    assert Sub2ApiVideoClient._video_urls(payload) == []


@pytest.mark.asyncio
async def test_video_generation_uses_background_safe_identity(monkeypatch):
    monkeypatch.setattr(
        "app.services.sub2api.video_client.httpx.AsyncClient", _FakeAsyncClient
    )
    client = Sub2ApiVideoClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )

    body, urls = await client.generate(
        model="gemini_omni_t2v_portrait_4s",
        prompt="Fast opening shot",
        references=[],
        idempotency_key="workspace:1:task:2",
    )

    assert body["id"] == "chatcmpl-flow-1"
    assert urls == ["http://127.0.0.1:19082/tmp/result.mp4"]
    request = _FakeAsyncClient.request
    assert request is not None
    assert request["path"] == "/chat/completions"
    assert request["json"]["stream"] is False
    assert request["json"]["gmv_idempotency_key"]
    assert request["headers"]["Idempotency-Key"].startswith("gmv-video-")
    assert "secret" not in str(request["json"])


@pytest.mark.asyncio
async def test_video_generation_preserves_all_seven_reference_images(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "app.services.sub2api.video_client.httpx.AsyncClient", _FakeAsyncClient
    )
    references = []
    for index in range(7):
        path = tmp_path / f"reference-{index}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
        references.append((str(path), "image/png"))

    client = Sub2ApiVideoClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )
    await client.generate(
        model="gemini_omni_r2v_portrait_8s",
        prompt="Use every reference according to its assigned visual role",
        references=references,
        idempotency_key="workspace:1:task:seven-images",
    )

    request = _FakeAsyncClient.request
    assert request is not None
    content = request["json"]["messages"][0]["content"]
    assert len(content) == 8
    assert [part["type"] for part in content] == ["text"] + ["image_url"] * 7


@pytest.mark.asyncio
async def test_video_generation_preserves_retry_after(monkeypatch):
    monkeypatch.setattr(
        "app.services.sub2api.video_client.httpx.AsyncClient",
        _UnavailableAsyncClient,
    )
    client = Sub2ApiVideoClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )

    with pytest.raises(Sub2ApiApiError) as caught:
        await client.generate(
            model="gemini_omni_t2v_portrait_4s",
            prompt="Fast opening shot",
            references=[],
            idempotency_key="workspace:1:task:503",
        )

    assert caught.value.status_code == 503
    assert caught.value.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_video_generation_recovers_nested_terminal_request_status(monkeypatch):
    monkeypatch.setattr(
        "app.services.sub2api.video_client.httpx.AsyncClient",
        _InvalidRequestAsyncClient,
    )
    client = Sub2ApiVideoClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )

    with pytest.raises(Sub2ApiApiError) as caught:
        await client.generate(
            model="gemini_omni_t2v_portrait_4s",
            prompt="Rejected request",
            references=[],
            idempotency_key="workspace:1:task:400",
        )

    assert caught.value.status_code == 502
    assert caught.value.upstream_status_code == 400
    assert caught.value.code == "flow_request_rejected"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_video_generation_treats_idempotency_mismatch_as_terminal(monkeypatch):
    monkeypatch.setattr(
        "app.services.sub2api.video_client.httpx.AsyncClient",
        _IdempotencyConflictAsyncClient,
    )
    client = Sub2ApiVideoClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )

    with pytest.raises(Sub2ApiApiError) as caught:
        await client.generate(
            model="gemini_omni_t2v_portrait_4s",
            prompt="Changed generation",
            references=[],
            idempotency_key="workspace:3:task:3587:generation:1",
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "flow_idempotency_conflict"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_video_generation_keeps_long_request_lease_alive(monkeypatch):
    monkeypatch.setattr(
        "app.services.sub2api.video_client.httpx.AsyncClient",
        _DelayedAsyncClient,
    )
    heartbeats: list[int] = []

    async def heartbeat() -> None:
        heartbeats.append(len(heartbeats) + 1)

    client = Sub2ApiVideoClient(
        api_key="secret", base_url="http://sub2api.test/v1"
    )
    _, urls = await client.generate(
        model="gemini_omni_t2v_portrait_4s",
        prompt="Fast opening shot",
        references=[],
        idempotency_key="workspace:1:task:heartbeat",
        heartbeat=heartbeat,
        heartbeat_interval_seconds=0.01,
    )

    assert urls == ["http://127.0.0.1:19082/tmp/result.mp4"]
    assert len(heartbeats) >= 2
