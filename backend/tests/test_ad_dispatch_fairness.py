from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import app.celery_app  # noqa: F401 - load task registry before importing service internals
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.website_ads import (
    WebsiteAdsCreativeAsset,
    WebsiteAdsLandingPage,
    WebsiteAdsUploadFingerprint,
)
from app.data.models.workspaces import Workspace
from app.services import website_ads_asset_expansion
from app.services.website_ads_media_cache import LOCAL_CACHE_KEY
from app.services.website_ads_uploads import UPLOAD_JOB_KEY
from app.tasks import website_ads_tasks


class _ImmediateWebsiteAdsLock:
    def __init__(self, **_kwargs):
        self.acquired = False
        self.lost = False

    def acquire(self, **_kwargs):
        self.acquired = True
        return True

    def verify_ownership(self):
        return self.acquired and not self.lost

    def release(self):
        self.acquired = False
        return True


def _next_id(db_session, model) -> int:
    return int(
        db_session.scalar(select(func.coalesce(func.max(model.id), 0))) or 0
    ) + 1


def _account_scope(db_session) -> tuple[int, int]:
    workspace = Workspace(
        id=_next_id(db_session, Workspace),
        name="Fair dispatch",
        company_code="FAIR",
    )
    provider = OAuthProviderApp(
        id=_next_id(db_session, OAuthProviderApp),
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.test/callback",
    )
    db_session.add_all([workspace, provider])
    db_session.flush()
    account = OAuthAccountTTB(
        id=_next_id(db_session, OAuthAccountTTB),
        workspace_id=int(workspace.id),
        provider_app_id=int(provider.id),
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.commit()
    return int(workspace.id), int(account.id)


def _asset(
    *,
    workspace_id: int,
    auth_id: int,
    video_id: str,
    updated_at: datetime,
    raw_json: dict | None = None,
    landing_page_id: int | None = None,
    analysis_status: str = "NOT_ANALYZED",
) -> WebsiteAdsCreativeAsset:
    return WebsiteAdsCreativeAsset(
        workspace_id=workspace_id,
        auth_id=auth_id,
        advertiser_id="adv-fair",
        landing_page_id=landing_page_id,
        video_id=video_id,
        title=video_id,
        analysis_status=analysis_status,
        auto_launch_status="PENDING",
        is_active=True,
        raw_json=raw_json,
        last_synced_at=updated_at,
        created_at=updated_at,
        updated_at=updated_at,
    )


def test_analysis_dispatch_is_oldest_due_and_queue_failure_backs_off(
    db_session,
    monkeypatch,
):
    workspace_id, auth_id = _account_scope(db_session)
    oldest = _asset(
        workspace_id=workspace_id,
        auth_id=auth_id,
        video_id="analysis-oldest",
        updated_at=datetime(2024, 1, 1),
    )
    next_oldest = _asset(
        workspace_id=workspace_id,
        auth_id=auth_id,
        video_id="analysis-next",
        updated_at=datetime(2024, 1, 2),
    )
    newest = _asset(
        workspace_id=workspace_id,
        auth_id=auth_id,
        video_id="analysis-newest",
        updated_at=datetime(2024, 1, 3),
    )
    db_session.add_all([oldest, next_oldest, newest])
    db_session.commit()

    calls: list[int] = []

    def _publish(*, kwargs, queue):
        calls.append(int(kwargs["asset_id"]))
        if len(calls) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        website_ads_tasks.analyze_asset_task,
        "apply_async",
        _publish,
    )

    first = website_ads_tasks.dispatch_asset_analysis(db_session, limit=1)
    db_session.refresh(oldest)
    assert first["queued"] == 0
    assert calls == [int(oldest.id)]
    assert oldest.analysis_status == "NOT_ANALYZED"
    assert oldest.analysis_next_retry_at is not None
    assert oldest.analysis_next_retry_at > datetime.now(timezone.utc).replace(
        tzinfo=None
    )

    second = website_ads_tasks.dispatch_asset_analysis(db_session, limit=1)
    assert second["asset_ids"] == [int(next_oldest.id)]
    assert calls == [int(oldest.id), int(next_oldest.id)]


def test_media_cache_bounded_scan_persists_fair_inspection_cursor(
    db_session,
    monkeypatch,
):
    workspace_id, auth_id = _account_scope(db_session)
    timestamp = datetime(2024, 1, 1)
    cached = [
        _asset(
            workspace_id=workspace_id,
            auth_id=auth_id,
            video_id=f"cached-{index:03d}",
            updated_at=timestamp + timedelta(minutes=index),
            raw_json={
                LOCAL_CACHE_KEY: {
                    "state": "READY",
                    "video": {"path": f"/cached/{index}.mp4"},
                    "cover": {"path": f"/cached/{index}.jpg"},
                }
            },
        )
        for index in range(100)
    ]
    target = _asset(
        workspace_id=workspace_id,
        auth_id=auth_id,
        video_id="uncached-target",
        updated_at=timestamp - timedelta(days=30),
        raw_json={},
    )
    # The old newest-first bounded scan could never reach this insertion-order
    # tail while the first 100 rows remained locally ready.
    db_session.add_all([*cached, target])
    db_session.commit()

    monkeypatch.setattr(
        website_ads_tasks,
        "resolve_asset_media",
        lambda row, kind: object()
        if str(row.video_id).startswith("cached-")
        else None,
    )
    queued: list[int] = []
    monkeypatch.setattr(
        website_ads_tasks.cache_asset_media_task,
        "apply_async",
        lambda *, kwargs, queue: queued.append(int(kwargs["asset_id"])),
    )

    first = website_ads_tasks.dispatch_asset_media_cache(db_session, limit=1)
    assert first["queued"] == 0
    db_session.refresh(cached[0])
    assert (
        cached[0].raw_json[LOCAL_CACHE_KEY].get("dispatch_checked_at")
        is not None
    )

    second = website_ads_tasks.dispatch_asset_media_cache(db_session, limit=1)
    assert second["asset_ids"] == [int(target.id)]
    assert queued == [int(target.id)]


def test_pending_upload_claim_prevents_failed_oldest_from_blocking_queue(
    db_session,
    monkeypatch,
):
    workspace_id, auth_id = _account_scope(db_session)
    rows = []
    for index in range(2):
        rows.append(
            WebsiteAdsUploadFingerprint(
                workspace_id=workspace_id,
                auth_id=auth_id,
                advertiser_id="adv-fair",
                content_sha256=str(index + 1) * 64,
                file_size_bytes=10,
                file_name=f"upload-{index}.mp4",
                status="QUEUED",
                response_json={
                    UPLOAD_JOB_KEY: {
                        "provider": "tiktok-business",
                        "upload_name": f"upload-{index}.mp4",
                    }
                },
                created_at=datetime(2024, 1, index + 1),
                updated_at=datetime(2024, 1, index + 1),
            )
        )
    db_session.add_all(rows)
    db_session.commit()

    calls: list[int] = []

    def _publish(*, kwargs, queue):
        calls.append(int(kwargs["upload_id"]))
        if len(calls) == 1:
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        website_ads_tasks.upload_video_task,
        "apply_async",
        _publish,
    )

    first = website_ads_tasks.dispatch_pending_uploads(db_session, limit=1)
    db_session.refresh(rows[0])
    assert first["dispatch_failed"] == [int(rows[0].id)]
    assert rows[0].status == "RETRYING"

    second = website_ads_tasks.dispatch_pending_uploads(db_session, limit=1)
    db_session.refresh(rows[1])
    assert second["upload_ids"] == [int(rows[1].id)]
    assert rows[1].status == "UPLOADING"
    assert calls == [int(rows[0].id), int(rows[1].id)]


@pytest.mark.anyio
async def test_asset_expansion_window_includes_oldest_due_asset_beyond_newest_100(
    db_session,
    monkeypatch,
):
    workspace_id, auth_id = _account_scope(db_session)
    landing = WebsiteAdsLandingPage(
        workspace_id=workspace_id,
        external_id="landing-fair",
        identifier="landing-fair",
        title="Landing",
        landing_url="https://example.test/product",
        is_active=True,
    )
    db_session.add(landing)
    db_session.flush()

    base = datetime(2024, 1, 1)
    oldest = _asset(
        workspace_id=workspace_id,
        auth_id=auth_id,
        video_id="expansion-oldest",
        updated_at=base,
        landing_page_id=int(landing.id),
        analysis_status="READY",
    )
    newer = [
        _asset(
            workspace_id=workspace_id,
            auth_id=auth_id,
            video_id=f"expansion-newer-{index:03d}",
            updated_at=base + timedelta(minutes=index + 1),
            landing_page_id=int(landing.id),
            analysis_status="READY",
        )
        for index in range(100)
    ]
    db_session.add_all([oldest, *newer])
    db_session.commit()

    claimed: list[int] = []
    monkeypatch.setattr(
        website_ads_asset_expansion.settings,
        "WEBSITE_ADS_ASSET_EXPANSION_ENABLED",
        True,
    )
    monkeypatch.setattr(
        website_ads_asset_expansion,
        "_asset_rank",
        lambda db, asset: (0.0, 0, asset.created_at),
    )
    monkeypatch.setattr(
        website_ads_asset_expansion,
        "_claim_asset",
        lambda db, asset: claimed.append(int(asset.id)) or True,
    )

    async def _waiting(db, asset):
        return {"status": "WAITING_PRODUCT", "created": []}

    monkeypatch.setattr(
        website_ads_asset_expansion,
        "_expand_asset",
        _waiting,
    )

    result = await website_ads_asset_expansion.run_website_ads_asset_expansion_cycle(
        db_session,
        workspace_id=workspace_id,
        _lock_factory=_ImmediateWebsiteAdsLock,
    )

    assert result["scanned"] > 0
    assert int(oldest.id) in claimed
