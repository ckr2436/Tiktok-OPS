# app/services/kie_api/common.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieFile
from app.services.kie_api.accounts import (
    get_effective_key,
    get_key_by_id,
    decrypt_api_key,
)
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError
from app.services.audit import log_event


async def get_remaining_credits_for_key(
    db: Session,
    *,
    key_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    actor_workspace_id: Optional[int] = None,
) -> int:
    """
    平台：查询某个 KIE API key 的余额。
    - 如果 key_id 为空，则使用“默认 key”（get_effective_key）
    - 返回整数积分；如果接口异常会抛出 KieApiError / ValueError
    """
    if key_id is not None:
        key: KieApiKey | None = get_key_by_id(db, key_id=key_id)
        if key is None:
            raise ValueError("KIE API key not found")
        if not key.is_active:
            raise ValueError("KIE API key is not active")
    else:
        # 选“默认 key”（内部会校验 active）
        key = get_effective_key(db, key_id=None, require_active=True)

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    # 直接调用 /api/v1/chat/credit
    resp = await client.get_remaining_credits()
    code = resp.get("code")
    if code != 200:
        raise KieApiError(f"get_remaining_credits failed: code={code}, msg={resp.get('msg')}")

    credits_raw = resp.get("data", 0)
    try:
        credits = int(credits_raw)
    except Exception:  # noqa: BLE001
        credits = 0

    # 审计日志（平台侧）
    log_event(
        db,
        action="kie.key.check_credit",
        resource_type="kie_key",
        resource_id=int(key.id),
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        workspace_id=None,  # 平台级 key，不绑定具体 workspace
        details={
            "credits": credits,
            "raw": resp,
        },
    )

    return credits


async def refresh_download_url_for_file(
    db: Session,
    *,
    file: KieFile,
    actor_user_id: Optional[int] = None,
    actor_workspace_id: Optional[int] = None,
) -> str:
    """
    租户：把某个 KIE 文件记录的 file_url 换成最新的 20 分钟有效 download_url，
    并回写到 KieFile.download_url / expires_at。

    - 必须使用当时调用任务/上传时的 key（file.key_id）
    - 如果 key 已经被停用，也可以根据业务需求选择报错；这里直接要求 key 仍然 active
    """
    key: KieApiKey | None = get_key_by_id(db, key_id=int(file.key_id))
    if key is None:
        raise ValueError("Associated KIE API key not found for file")
    if not key.is_active:
        raise ValueError("Associated KIE API key is not active")

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    resp = await client.get_download_url(file_url=file.file_url)
    code = resp.get("code")
    if code != 200:
        raise KieApiError(f"get_download_url failed: code={code}, msg={resp.get('msg')}")

    download_url = resp.get("data")
    if not download_url:
        raise KieApiError("get_download_url response missing data field")

    # 由于官方说明是“20 分钟有效”，我们按当前时间 +20 分钟估算失效时间
    now = datetime.now(timezone.utc)
    file.download_url = download_url
    file.expires_at = now + timedelta(minutes=20)

    db.add(file)
    db.flush()

    log_event(
        db,
        action="kie.file.refresh_download_url",
        resource_type="kie_file",
        resource_id=int(file.id),
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id or int(file.workspace_id),
        workspace_id=int(file.workspace_id),
        details={
            "file_id": int(file.id),
            "file_url": file.file_url,
            "download_url": download_url,
            "key_id": int(file.key_id),
        },
    )

    return download_url


__all__ = [
    "get_remaining_credits_for_key",
    "refresh_download_url_for_file",
]

