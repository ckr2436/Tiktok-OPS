from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import APIError
from app.data.models.workspaces import Workspace
from app.features.tenants.openai_whisper import repository, service, storage, url_security
from app.services.ai_video import local_storage
from app.services.ai_video.reference_capability import (
    sign_reference_capability,
    verify_reference_capability,
)


class DummyUpload:
    filename = "large.mp4"
    content_type = "video/mp4"

    def __init__(self, payload: bytes):
        self.stream = io.BytesIO(payload)

    async def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    async def seek(self, offset: int) -> None:
        self.stream.seek(offset)


def _workspace(db_session, workspace_id: int) -> None:
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"Workspace {workspace_id}",
            company_code=f"{workspace_id:04d}"[-4:],
        )
    )
    db_session.commit()


def _job(db_session, workspace_id: int, user_id: int, job_id: str) -> None:
    repository.create_job(
        db_session,
        {
            "job_id": job_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "filename": f"{job_id}.mp4",
            "video_path": f"/tmp/{job_id}.mp4",
        },
    )
    db_session.commit()


def test_whisper_repository_scopes_member_queries_and_deletes(db_session) -> None:
    _workspace(db_session, 7101)
    _job(db_session, 7101, 101, "owner-a")
    _job(db_session, 7101, 202, "owner-b")

    assert [row.job_id for row in repository.list_jobs(
        db_session, 7101, 20, user_id=101
    )] == ["owner-a"]
    assert repository.get_job(db_session, 7101, "owner-b", user_id=101) is None
    assert repository.delete_job(
        db_session, 7101, "owner-b", user_id=101
    ) is None
    assert repository.delete_jobs_by_ids(
        db_session, 7101, ["owner-a", "owner-b"], user_id=101
    ) == 1
    db_session.commit()
    assert repository.get_job(db_session, 7101, "owner-b") is not None


def test_whisper_upload_limit_removes_partial_file(tmp_path) -> None:
    target = tmp_path / "upload.mp4"
    with pytest.raises(APIError) as exc_info:
        asyncio.run(
            service._save_upload_file(
                target,
                DummyUpload(b"12345"),
                max_bytes=4,
                remaining_workspace_bytes=100,
            )
        )
    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert not target.exists()


def test_share_url_rejects_private_and_lookalike_hosts(monkeypatch) -> None:
    monkeypatch.setattr(
        url_security,
        "_resolved_public_addresses",
        lambda host, port: ("8.8.8.8",),
    )
    with pytest.raises(url_security.UnsafeShareURLError):
        url_security.validate_share_url("https://tiktok.com.example.test/video/1")
    with pytest.raises(url_security.UnsafeShareURLError):
        url_security.validate_share_url("http://www.tiktok.com/video/1")

    monkeypatch.undo()
    with pytest.raises(url_security.UnsafeShareURLError):
        url_security.validate_share_url("https://127.0.0.1/video/1", require_supported_host=False)


def test_extracted_xiaohongshu_cdn_urls_are_pinned_to_https(monkeypatch) -> None:
    monkeypatch.setattr(
        url_security,
        "_resolved_public_addresses",
        lambda host, port: ("8.8.8.8",),
    )
    info = {
        "url": "http://sns-video-v6.xhscdn.com/video/1.mp4?token=opaque",
        "formats": [{"url": "http://sns-bak-v1.xhscdn.com/video/1.mp4"}],
    }

    url_security.validate_extracted_media_urls(info)

    assert info["url"].startswith("https://sns-video-v6.xhscdn.com/")
    assert info["formats"][0]["url"].startswith("https://sns-bak-v1.xhscdn.com/")
    with pytest.raises(url_security.UnsafeShareURLError):
        url_security.validate_extracted_media_urls(
            {"url": "http://untrusted.example/video.mp4"}
        )


def test_reference_capability_is_bound_and_expires(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.ai_video.reference_capability.settings.AI_VIDEO_REFERENCE_URL_TTL_SECONDS",
        300,
    )
    expires = 1300
    signature = sign_reference_capability(3, 19, 41, expires)
    assert verify_reference_capability(
        3, 19, 41, expires=expires, signature=signature, now=1000
    )
    assert not verify_reference_capability(
        3, 19, 42, expires=expires, signature=signature, now=1000
    )
    assert not verify_reference_capability(
        3, 19, 41, expires=expires, signature=signature, now=1301
    )


def test_managed_file_boundary_rejects_prefix_collision(tmp_path, monkeypatch) -> None:
    root = tmp_path / "uploads"
    sibling = tmp_path / "uploads-evil"
    root.mkdir()
    sibling.mkdir()
    allowed = root / "safe.png"
    outside = sibling / "outside.png"
    allowed.write_bytes(b"safe")
    outside.write_bytes(b"outside")

    assert local_storage.resolve_managed_file(allowed, (root.resolve(),)) == allowed.resolve()
    assert local_storage.resolve_managed_file(outside, (root.resolve(),)) is None

    monkeypatch.setattr(local_storage, "managed_reference_roots", lambda: (root.resolve(),))
    poisoned = SimpleNamespace(
        kind="reference_upload",
        file_url=str(outside),
        meta_json={},
    )
    assert local_storage.get_local_path(poisoned) is None
