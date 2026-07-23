from __future__ import annotations

import httpx
import pytest

from app.services import ttb_api
from app.services.ttb_api import (
    SharedTikTokRateLimiter,
    TTBApiClient,
    TTBHttpError,
    TTBRateLimitBudgetError,
    ttb_retry_countdown,
)


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _NoopBucket:
    async def acquire(self) -> None:
        return None


class _RecordingLimiter:
    def __init__(self) -> None:
        self.penalties: list[tuple[str, float | None]] = []

    async def acquire(self, _path: str) -> None:
        return None

    async def penalize(
        self,
        path: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.penalties.append((path, retry_after_seconds))


async def _client_with_response(response: httpx.Response) -> tuple[TTBApiClient, _RecordingLimiter]:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = TTBApiClient(access_token="token")
    headers = client._client.headers
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    limiter = _RecordingLimiter()
    client._bucket = _NoopBucket()
    client._shared_limiter = limiter
    return client, limiter


async def test_http_429_publishes_shared_retry_after_cooldown() -> None:
    client, limiter = await _client_with_response(
        httpx.Response(429, text="busy", headers={"Retry-After": "45"})
    )
    try:
        with pytest.raises(TTBHttpError):
            await client._request_json_once("GET", "/gmv_max/report/get/")
    finally:
        await client.aclose()

    assert limiter.penalties == [("/gmv_max/report/get/", 45.0)]


async def test_business_rate_limit_publishes_shared_cooldown() -> None:
    client, limiter = await _client_with_response(
        httpx.Response(
            200,
            json={"code": 40000, "message": "Request too frequent."},
        )
    )
    try:
        with pytest.raises(TTBHttpError):
            await client._request_json_once("GET", "/gmv_max/report/get/")
    finally:
        await client.aclose()

    assert limiter.penalties == [("/gmv_max/report/get/", None)]


async def test_shared_cooldown_stops_cross_process_retry_storm(monkeypatch) -> None:
    class _Redis:
        def pttl(self, _key: str) -> int:
            return 15_000

        def eval(self, *_args):  # pragma: no cover - cooldown blocks first
            raise AssertionError("quota counters must not advance during cooldown")

    monkeypatch.setattr(ttb_api, "get_redis_sync", lambda: _Redis())
    limiter = SharedTikTokRateLimiter(app_id="app", access_token="token")

    with pytest.raises(TTBRateLimitBudgetError) as exc_info:
        await limiter.acquire("/gmv_max/report/get/")

    assert exc_info.value.code == "UPSTREAM_RATE_LIMIT"
    assert exc_info.value.payload["retry_after_ms"] == 15_000


def test_retry_countdown_honors_quota_window_with_drain_margin() -> None:
    exc = TTBRateLimitBudgetError(
        "busy",
        payload={"retry_after_ms": 125_001},
    )

    assert ttb_retry_countdown(exc) == 131
