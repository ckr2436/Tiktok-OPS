from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request

from app.core.config import settings
from app.core.errors import APIError
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAuthzSession
from app.data.models.oauth_tiktok_shop import OAuthTikTokShopAccount
from app.data.models.tiktok_shop_content_posting import TikTokShopContentPost
from app.core.deps import SessionUser
from app.services import oauth_tiktok_shop as oauth_service
from app.services.tiktok_shop_content_posting import (
    validate_product_link_title,
    validate_video_title,
)
from app.services.tiktok_shop_creator_api import CreatorAPIResult, TikTokShopCreatorAPIClient
from app.tasks import tiktok_shop_tasks


content_posting_router = importlib.import_module(
    "app.features.tenants.tiktok_shop.content_posting_router"
)
oauth_router = importlib.import_module("app.features.tenants.oauth_tiktok_shop.router")


def test_creator_authorization_url_uses_creator_flow(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    app = SimpleNamespace(id=7, client_id="app-key-1", service_id="service-1")
    monkeypatch.setattr(oauth_service, "_get_enabled_app", lambda *_args, **_kwargs: app)

    session, auth_url = oauth_service.create_authorization_session(
        db_session,
        workspace_id=3,
        provider_app_id=7,
        created_by_user_id=6,
        client_ip="127.0.0.1",
        user_agent="pytest",
        return_to="/tenants/3/tiktok-shop/content-posting",
        alias="Creator",
        authorization_type="creator",
    )

    assert session.authorization_type == "creator"
    assert auth_url.startswith("https://shop.tiktok.com/alliance/creator/auth?")
    assert "app_key=app-key-1" in auth_url
    assert f"state={session.state}" in auth_url
    assert "service_id=" not in auth_url


def test_creator_callback_requires_user_type_and_scope() -> None:
    session = OAuthTikTokShopAuthzSession(
        workspace_id=3,
        provider_app_id=1,
        state="state-1",
        status="pending",
        authorization_type="creator",
        expires_at=tiktok_shop_tasks.utcnow_naive(),
    )

    with pytest.raises(APIError) as wrong_type:
        oauth_service._validate_authorization_token(
            session,
            {"user_type": 0, "granted_scopes": ["creator.video.write"]},
        )
    assert wrong_type.value.code == "CREATOR_TOKEN_REQUIRED"

    with pytest.raises(APIError) as missing_scope:
        oauth_service._validate_authorization_token(
            session,
            {"user_type": 1, "granted_scopes": []},
        )
    assert missing_scope.value.code == "CREATOR_VIDEO_SCOPE_REQUIRED"

    oauth_service._validate_authorization_token(
        session,
        {"user_type": 1, "granted_scopes": ["creator.video.write"]},
    )


def test_creator_callback_errors_have_stable_frontend_reasons() -> None:
    assert (
        oauth_service.oauth_callback_error_reason("CREATOR_TOKEN_REQUIRED")
        == "CREATOR_TOKEN_REQUIRED"
    )
    assert (
        oauth_service.oauth_callback_error_reason("CREATOR_VIDEO_SCOPE_REQUIRED")
        == "CREATOR_VIDEO_SCOPE_REQUIRED"
    )


def test_content_title_validation_matches_official_constraints() -> None:
    assert validate_product_link_title("Sleep Ease Gummies") == "Sleep Ease Gummies"
    assert validate_product_link_title("睡眠软糖") == "睡眠软糖"
    with pytest.raises(APIError):
        validate_product_link_title("Sleep-Ease")
    with pytest.raises(APIError):
        validate_product_link_title("Sleep Gummies 😴")
    with pytest.raises(APIError):
        validate_product_link_title("x" * 31)

    assert validate_video_title("hello") == "hello"
    with pytest.raises(APIError):
        validate_video_title("😀" * 1101)


@pytest.mark.anyio
async def test_creator_account_cannot_enter_seller_shop_sync(db_session) -> None:
    account = OAuthTikTokShopAccount(
        id=91,
        workspace_id=3,
        provider_app_id=7,
        open_id="creator-sync-boundary",
        user_type=1,
        access_token_cipher=b"encrypted-access",
        refresh_token_cipher=b"encrypted-refresh",
        key_version=1,
        token_fingerprint=b"t" * 32,
        granted_scopes_json=["creator.video.write"],
        status="active",
        created_by_user_id=6,
    )
    db_session.add(account)
    db_session.commit()
    me = SessionUser(
        id=6,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode="000100001",
        is_platform_admin=False,
        workspace_id=3,
        role="owner",
        is_active=True,
    )
    request = Request({"type": "http", "method": "POST", "path": "/sync", "headers": []})

    with pytest.raises(APIError) as exc:
        await oauth_router.sync_shops(3, 91, request, me, db_session)

    assert exc.value.code == "CREATOR_ACCOUNT_NO_SHOP_SYNC"


@pytest.mark.anyio
async def test_creator_api_uses_creator_header_and_no_shop_cipher() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        captured["token"] = request.headers.get("x-tts-access-token")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"products": [], "next_page_token": ""},
                "message": "Success",
                "request_id": "request-1",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TikTokShopCreatorAPIClient(
        db=SimpleNamespace(),
        workspace_id=3,
        account=SimpleNamespace(id=9, granted_scopes_json=["creator.video.write"]),
        app_key="app-key",
        app_secret="app-secret",
        access_token="creator-token",
        http_client=http_client,
    )
    try:
        result = await client.shop_products(
            title_keyword="gummy",
            sort_field="PRODUCT_ID",
            sort_order="DESC",
            page_size=20,
            page_token=None,
        )
    finally:
        await http_client.aclose()

    assert result.request_id == "request-1"
    assert captured["path"] == "/affiliate_creator/202509/shop_products"
    assert captured["token"] == "creator-token"
    query = captured["query"]
    assert isinstance(query, dict)
    assert query["app_key"] == "app-key"
    assert query["page_size"] == "20"
    assert "sign" in query
    assert "shop_cipher" not in query
    assert "access_token" not in query


class _FakeCreatorClient:
    def __init__(self) -> None:
        self.precheck_results = ["PROCESSING", "SUCCESS"]
        self.post_results = ["PROCESSING", "SUCCESS"]
        self.publish_calls = 0

    @classmethod
    async def create(cls, *_args, **_kwargs):
        return _FAKE_CLIENT

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def upload_video_file(self, **_kwargs):
        return CreatorAPIResult(
            data={"video_file": {"id": "file-1", "md5": "ABC"}},
            request_id="upload-request",
        )

    async def create_precheck(self, body):
        assert body["video_info"]["file_id"] == "file-1"
        assert body["product_link_info"]["product_id"] == "product-1"
        return CreatorAPIResult(data={"precheck": {"task_id": "precheck-1"}}, request_id="precheck-create")

    async def precheck_status(self, _task_id):
        result = self.precheck_results.pop(0)
        return CreatorAPIResult(
            data={"precheck_task": {"id": "precheck-1", "result": result, "issues": []}},
            request_id=f"precheck-{result.lower()}",
        )

    async def publish_video(self, body):
        self.publish_calls += 1
        assert body["video_info"]["file_id"] == "file-1"
        return CreatorAPIResult(data={"video": {"id": "video-1"}}, request_id="publish-request")

    async def video_status(self, _video_id):
        result = self.post_results.pop(0)
        return CreatorAPIResult(
            data={"video": {"id": "video-1", "post_status": result, "post_time": 1785150000}},
            request_id=f"status-{result.lower()}",
        )


_FAKE_CLIENT = _FakeCreatorClient()


def _workflow_row(db_session, path: Path) -> TikTokShopContentPost:
    row = TikTokShopContentPost(
        workspace_id=3,
        account_id=9,
        created_by_user_id=6,
        idempotency_key="post-key-123",
        request_fingerprint=b"f" * 32,
        original_filename="video.mp4",
        local_file_path=str(path),
        media_type="video/mp4",
        file_size=path.stat().st_size,
        sha256_digest=b"s" * 32,
        product_id="product-1",
        product_link_title="Sleep Gummies",
        video_title="A useful caption",
        cover_timestamp_ms=1000,
        workflow_status="QUEUED",
        publish_requested=False,
        api_versions_json={"publish": "202603"},
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_async_workflow_requires_precheck_then_publishes(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global _FAKE_CLIENT
    _FAKE_CLIENT = _FakeCreatorClient()
    storage = tmp_path / "content-posting"
    video = storage / "workspace_3" / "account_9" / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video-bytes")
    monkeypatch.setattr(settings, "TT_SHOP_CONTENT_POSTING_STORAGE_ROOT", str(storage))
    monkeypatch.setattr(tiktok_shop_tasks, "TikTokShopCreatorAPIClient", _FakeCreatorClient)
    row = _workflow_row(db_session, video)

    result, delay = asyncio.run(tiktok_shop_tasks._advance_content_post(db_session, row))
    assert result["status"] == "PRECHECKING"
    assert delay is not None
    assert row.official_file_id == "file-1"
    assert row.precheck_task_id == "precheck-1"

    result, delay = asyncio.run(tiktok_shop_tasks._advance_content_post(db_session, row))
    assert result["status"] == "PRECHECKING"
    assert delay is not None

    result, delay = asyncio.run(tiktok_shop_tasks._advance_content_post(db_session, row))
    assert result["status"] == "READY_TO_PUBLISH"
    assert delay is None
    assert _FAKE_CLIENT.publish_calls == 0

    row.publish_requested = True
    db_session.add(row)
    db_session.commit()
    result, delay = asyncio.run(tiktok_shop_tasks._advance_content_post(db_session, row))
    assert result["status"] == "PROCESSING"
    assert row.video_id == "video-1"
    assert _FAKE_CLIENT.publish_calls == 1

    result, delay = asyncio.run(tiktok_shop_tasks._advance_content_post(db_session, row))
    assert result["status"] == "PROCESSING"
    assert delay is not None
    result, delay = asyncio.run(tiktok_shop_tasks._advance_content_post(db_session, row))
    assert result["status"] == "SUCCESS"
    assert delay is None
    assert row.post_status == "SUCCESS"
    assert row.completed_at is not None


class _UploadStub:
    filename = "video.mp4"
    content_type = "video/mp4"

    async def close(self) -> None:
        return None


@pytest.mark.anyio
async def test_create_route_is_idempotent_and_queues_once(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = OAuthTikTokShopAccount(
        id=9,
        workspace_id=3,
        provider_app_id=7,
        open_id="creator-open-id",
        user_type=1,
        access_token_cipher=b"encrypted-access",
        refresh_token_cipher=b"encrypted-refresh",
        key_version=1,
        token_fingerprint=b"t" * 32,
        granted_scopes_json=["creator.video.write"],
        status="active",
        created_by_user_id=6,
    )
    db_session.add(account)
    db_session.commit()
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    async def persist(*_args, **_kwargs):
        return path, "video.mp4", 5, b"s" * 32

    queued: list[int] = []
    monkeypatch.setattr(content_posting_router, "persist_uploaded_video", persist)
    monkeypatch.setattr(content_posting_router, "_queue_post", lambda post_id, **_kwargs: queued.append(post_id))
    monkeypatch.setattr(content_posting_router, "log_event", lambda *_args, **_kwargs: None)
    me = SessionUser(
        id=6,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode="000100001",
        is_platform_admin=False,
        workspace_id=3,
        role="owner",
        is_active=True,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    kwargs = {
        "workspace_id": 3,
        "request": request,
        "account_id": 9,
        "product_id": "product-1",
        "product_link_title": "Sleep Gummies",
        "video_title": "Video title",
        "cover_uri": None,
        "cover_timestamp_ms": 1000,
        "music_id": None,
        "idempotency_key": "posting-key-123",
        "me": me,
        "db": db_session,
    }
    first = await content_posting_router.create_content_post(video=_UploadStub(), **kwargs)
    second = await content_posting_router.create_content_post(video=_UploadStub(), **kwargs)

    assert first.reused is False
    assert second.reused is True
    assert first.item.id == second.item.id
    assert queued == [first.item.id]
    assert db_session.query(TikTokShopContentPost).count() == 1


def test_publish_route_never_bypasses_precheck(db_session) -> None:
    account = OAuthTikTokShopAccount(
        id=9,
        workspace_id=3,
        provider_app_id=7,
        open_id="creator-open-id",
        user_type=1,
        access_token_cipher=b"encrypted-access",
        refresh_token_cipher=b"encrypted-refresh",
        key_version=1,
        token_fingerprint=b"t" * 32,
        granted_scopes_json=["creator.video.write"],
        status="active",
        created_by_user_id=6,
    )
    row = TikTokShopContentPost(
        id=20,
        workspace_id=3,
        account_id=9,
        created_by_user_id=6,
        idempotency_key="posting-key-456",
        request_fingerprint=b"f" * 32,
        original_filename="video.mp4",
        local_file_path="/tmp/video.mp4",
        file_size=5,
        sha256_digest=b"s" * 32,
        product_id="product-1",
        product_link_title="Sleep Gummies",
        workflow_status="PRECHECKING",
        precheck_status="PROCESSING",
    )
    db_session.add_all([account, row])
    db_session.commit()
    me = SessionUser(
        id=6,
        email="owner@example.com",
        username="owner",
        display_name="Owner",
        usercode="000100001",
        is_platform_admin=False,
        workspace_id=3,
        role="owner",
        is_active=True,
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)}
    )
    with pytest.raises(APIError) as exc:
        content_posting_router.publish_content_post(
            workspace_id=3,
            post_id=20,
            request=request,
            me=me,
            db=db_session,
        )
    assert exc.value.code == "PRECHECK_REQUIRED"
