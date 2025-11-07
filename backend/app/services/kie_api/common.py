# app/services/kie_api/common.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Iterable, Any

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey, KieFile
from app.services.kie_api.accounts import (
    get_effective_key,
    get_key_by_id,
    decrypt_api_key,
)
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError
from app.services.audit import log_event

logger = logging.getLogger(__name__)


async def get_remaining_credits_for_key(
    db: Session,
    *,
    key_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    actor_workspace_id: Optional[int] = None,
) -> int:
    """
    平台：查询某个 KIE API key 的余额。

    - 如果 key_id 为空，则使用 get_effective_key 选出一个启用中的 key
    - 正常返回整数积分
    - 调用失败 / 返回异常值会抛 KieApiError / ValueError，由上层路由转换为 HTTP 错误
    """
    # 1) 选 key
    if key_id is not None:
        key: KieApiKey | None = get_key_by_id(db, key_id=key_id)
        if key is None:
            raise ValueError("KIE API key not found")
        if not key.is_active:
            raise ValueError("KIE API key is not active")
    else:
        # 选“有效 key”（内部会优先默认 key）
        key = get_effective_key(db, key_id=None, require_active=True)

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    # 2) 调 /api/v1/chat/credit
    try:
        resp = await client.get_remaining_credits()
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Failed to call KIE /api/v1/chat/credit",
            extra={"key_id": int(key.id)},
        )
        raise KieApiError(f"Failed to fetch credits for key {key.id}: {exc}") from exc

    code = resp.get("code")
    if code != 200:
        msg = resp.get("msg") or resp.get("message") or "unknown error"
        logger.warning(
            "KIE credit api returned non-200 code",
            extra={"key_id": int(key.id), "code": code, "msg": msg, "raw": resp},
        )
        raise KieApiError(f"get_remaining_credits failed: code={code}, msg={msg}")

    # 3) 解析积分；兼容几种常见结构
    data = resp.get("data")
    if isinstance(data, dict):
        raw_credits: Any = (
            data.get("credits")
            or data.get("credit")
            or data.get("remaining")
        )
    else:
        # 有些实现可能直接把数字放在 data 里
        raw_credits = data

    try:
        credits = int(raw_credits)
    except Exception:  # noqa: BLE001
        logger.warning(
            "KIE credit api returned invalid credits value",
            extra={"key_id": int(key.id), "raw_credits": raw_credits, "raw": resp},
        )
        raise KieApiError("KIE credit api returned invalid credits value")

    if credits < 0:
        credits = 0

    # 4) 审计日志（平台侧）
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


async def select_best_kie_key_for_task(
    db: Session,
    *,
    min_credits_required: int | None = None,
) -> KieApiKey:
    """
    选择一个“最适合当前任务”的 KIE API key，用于实际发起 sora2 任务。

    策略：
    1. 先尝试默认 key：
       - 查询成功且余额 >= min_credits_required（如果有要求） → 用它
       - 查询失败 / 余额不足 → 记日志，继续 2
    2. 遍历所有 is_active=True 的 key（按 id 升序）：
       - 跳过第 1 步已经检查过的默认 key
       - 对每个 key 调 /chat/credit：
         - 成功且余额满足 → 立即返回
         - 失败则记录 warning 继续
    3. 如果所有 key 都查询失败或余额不足，则退回 get_effective_key 结果
       （不再查余额），由上层业务决定是否允许继续创建任务。
    """
    tried_key_ids: set[int] = set()

    # 1) 默认 key
    try:
        default_key = get_effective_key(db, key_id=None, require_active=True)
    except Exception:  # noqa: BLE001
        default_key = None

    if default_key is not None:
        tried_key_ids.add(int(default_key.id))
        try:
            credits = await get_remaining_credits_for_key(
                db,
                key_id=int(default_key.id),
                actor_user_id=None,
                actor_workspace_id=None,
            )
            logger.info(
                "Default KIE key credits fetched",
                extra={"key_id": int(default_key.id), "credits": credits},
            )
            if min_credits_required is None or credits >= min_credits_required:
                return default_key
        except KieApiError as exc:
            logger.warning(
                "Default KIE key not suitable for task",
                extra={"key_id": int(default_key.id), "error": str(exc)},
            )

    # 2) 其它启用 key
    active_keys: Iterable[KieApiKey] = (
        db.query(KieApiKey)
        .filter(KieApiKey.is_active.is_(True))
        .order_by(KieApiKey.id.asc())
        .all()
    )
    for k in active_keys:
        kid = int(k.id)
        if kid in tried_key_ids:
            continue

        try:
            credits = await get_remaining_credits_for_key(
                db,
                key_id=kid,
                actor_user_id=None,
                actor_workspace_id=None,
            )
            logger.info(
                "Active KIE key credits fetched",
                extra={"key_id": kid, "credits": credits},
            )
            if min_credits_required is None or credits >= min_credits_required:
                return k
        except KieApiError as exc:
            logger.warning(
                "Active KIE key not suitable for task",
                extra={"key_id": kid, "error": str(exc)},
            )

    # 3) 全部失败/余额不足，兜底
    logger.warning(
        "All KIE keys either failed credit check or had insufficient credits; "
        "falling back to get_effective_key() without credit check",
    )
    return get_effective_key(db, key_id=None, require_active=True)


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
    - 即使 key 已经在平台侧被标记为停用，也允许继续下载历史文件
    """
    key: KieApiKey | None = get_key_by_id(db, key_id=int(file.key_id))
    if key is None:
        raise ValueError("Associated KIE API key not found for file")

    if not key.is_active:
        # 这里不再阻止下载，只记一条 warning 方便排查
        logger.warning(
            "Using inactive KIE API key to refresh download url for historical file",
            extra={"file_id": int(file.id), "key_id": int(key.id)},
        )

    api_key = decrypt_api_key(key.api_key_ciphertext)
    client = Sora2ImageToVideoService(api_key=api_key)

    resp = await client.get_download_url(file_url=file.file_url)
    code = resp.get("code")
    if code != 200:
        msg = resp.get("msg") or resp.get("message") or "unknown error"
        raise KieApiError(f"get_download_url failed: code={code}, msg={msg}")

    raw_data = resp.get("data")
    download_url: Optional[str] = None
    if isinstance(raw_data, str):
        download_url = raw_data
    elif isinstance(raw_data, dict):
        # 兼容几种字段名
        download_url = (
            raw_data.get("url")
            or raw_data.get("downloadUrl")
            or raw_data.get("download_url")
        )

    if not download_url:
        raise KieApiError("get_download_url response missing usable url in data field")

    # 官方说明“20 分钟有效”，按当前时间 +20 分钟估算失效时间
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
    "select_best_kie_key_for_task",
    "refresh_download_url_for_file",
]

