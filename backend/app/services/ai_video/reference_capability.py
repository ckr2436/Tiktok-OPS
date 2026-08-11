"""Short-lived capabilities for provider access to local reference images."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from app.core.config import settings


PURPOSE = "ai-video-reference-v1"


def _message(workspace_id: int, task_id: int, file_id: int, expires: int) -> bytes:
    return (
        f"{PURPOSE}:{int(workspace_id)}:{int(task_id)}:{int(file_id)}:{int(expires)}"
    ).encode("utf-8")


def sign_reference_capability(
    workspace_id: int,
    task_id: int,
    file_id: int,
    expires: int,
) -> str:
    return hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        _message(workspace_id, task_id, file_id, expires),
        hashlib.sha256,
    ).hexdigest()


def build_reference_capability_query(
    workspace_id: int,
    task_id: int,
    file_id: int,
    *,
    now: int | None = None,
) -> str:
    issued_at = int(time.time() if now is None else now)
    ttl = max(60, int(settings.AI_VIDEO_REFERENCE_URL_TTL_SECONDS))
    expires = issued_at + ttl
    signature = sign_reference_capability(workspace_id, task_id, file_id, expires)
    return urlencode({"expires": expires, "signature": signature})


def verify_reference_capability(
    workspace_id: int,
    task_id: int,
    file_id: int,
    *,
    expires: int,
    signature: str,
    now: int | None = None,
) -> bool:
    current = int(time.time() if now is None else now)
    ttl = max(60, int(settings.AI_VIDEO_REFERENCE_URL_TTL_SECONDS))
    if int(expires) < current or int(expires) > current + ttl + 60:
        return False
    expected = sign_reference_capability(workspace_id, task_id, file_id, int(expires))
    return hmac.compare_digest(expected, str(signature or ""))
