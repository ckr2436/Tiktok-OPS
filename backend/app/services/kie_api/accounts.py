# app/services/kie_api/accounts.py
from __future__ import annotations

from typing import Optional, Iterable, List, Mapping, Any

from sqlalchemy.orm import Session

from app.data.models.kie_api import KieApiKey
from app.services.audit import log_event
from app.services.kie_api.sora2 import Sora2ImageToVideoService, KieApiError


# === 加解密占位实现 ===
def encrypt_api_key(plaintext: str) -> str:
    """
    现在先直接返回明文，将来接入 KMS / Fernet 时在这里统一加密。
    """
    return plaintext.strip()


def decrypt_api_key(ciphertext: str) -> str:
    """
    现在直接当明文用，将来密文解密改这里即可。
    """
    return (ciphertext or "").strip()


# === CRUD & 查询工具（平台级，不绑定 workspace）===

def list_keys(db: Session) -> List[KieApiKey]:
    return (
        db.query(KieApiKey)
        .order_by(KieApiKey.id.asc())
        .all()
    )


def get_key_by_id(db: Session, *, key_id: int) -> Optional[KieApiKey]:
    return db.query(KieApiKey).filter(KieApiKey.id == key_id).one_or_none()


def get_default_key(db: Session, *, require_active: bool = True) -> Optional[KieApiKey]:
    q = db.query(KieApiKey).filter(KieApiKey.is_default.is_(True))
    if require_active:
        q = q.filter(KieApiKey.is_active.is_(True))
    return q.one_or_none()


def get_effective_key(
    db: Session,
    *,
    key_id: int | None = None,
    require_active: bool = True,
) -> KieApiKey:
    """
    统一挑选要用的 key：

    - 如果传了 key_id，则直接按 id 查，并校验 active（若 require_active=True）
    - 否则：优先用 is_default 为 True 且 active 的 key
    - 再否则：取第一个 active 的 key
    """
    if key_id is not None:
        k = get_key_by_id(db, key_id=key_id)
        if k is None:
            raise ValueError("KIE API key not found")
        if require_active and not k.is_active:
            raise ValueError("KIE API key is not active")
        return k

    k = get_default_key(db, require_active=require_active)
    if k is not None:
        return k

    q = db.query(KieApiKey)
    if require_active:
        q = q.filter(KieApiKey.is_active.is_(True))
    k = q.order_by(KieApiKey.id.asc()).first()
    if k is None:
        raise ValueError("No KIE API key configured")
    return k


# === 余额查询 & 自动选择 Key（供平台/租户共享）===

def _parse_credit_value(resp: Mapping[str, Any]) -> Optional[int]:
    """
    从 /api/v1/chat/credit 的响应中解析当前剩余 credits。
    兼容几种字段名：credits / credit / balance / remain / remaining。
    """
    if not isinstance(resp, Mapping):
        return None

    data = resp.get("data") or resp
    if not isinstance(data, Mapping):
        return None

    for key in ("credits", "credit", "balance", "remain", "remaining"):
        if key not in data:
            continue
        val = data.get(key)
        try:
            return int(val)
        except (TypeError, ValueError):
            continue

    return None


async def fetch_key_credits(key: KieApiKey) -> Optional[int]:
    """
    调用 KIE /api/v1/chat/credit 查询某个 key 的当前 credits。
    查询失败或返回格式不对时，返回 None（不抛异常）。
    """
    api_key = decrypt_api_key(key.api_key_ciphertext)
    if not api_key:
        return None

    client = Sora2ImageToVideoService(api_key=api_key)
    try:
        resp = await client.get_remaining_credits()
    except Exception:
        return None

    return _parse_credit_value(resp)


async def select_best_kie_key_for_task(
    db: Session,
    *,
    min_credits: int = 1,
) -> KieApiKey:
    """
    选择一个“启用、余额满足 min_credits”的 key：
    - 优先尝试默认 key（is_default=1 且 is_active=1）
    - 若默认 key 余额不足，则在其它启用 key 中找 credits >= min_credits 的
    - 若都查不到有效余额：
        - 若有默认 key，则返回默认 key
        - 否则返回任意一个 active key
        - 若连 active key 都没有，则抛 ValueError
    """
    tried: set[int] = set()

    # 1) 默认 key 优先
    default_key = get_default_key(db, require_active=True)
    if default_key is not None:
        tried.add(int(default_key.id))
        credits = await fetch_key_credits(default_key)
        if credits is not None and credits >= min_credits:
            return default_key

    # 2) 其它启用的 key
    others: List[KieApiKey] = (
        db.query(KieApiKey)
        .filter(KieApiKey.is_active.is_(True))
        .order_by(KieApiKey.id.asc())
        .all()
    )

    best_key: Optional[KieApiKey] = None
    best_credits: int = -1

    for k in others:
        if int(k.id) in tried:
            continue
        credits = await fetch_key_credits(k)
        if credits is None:
            continue
        if credits >= min_credits and credits > best_credits:
            best_key = k
            best_credits = credits

    if best_key is not None:
        return best_key

    # 3) 都没有有效余额：尽量返回一个“默认 key 或任意 active key”，
    #    让调用方拿到更明确的上游错误，而不是本地直接挂死。
    if default_key is not None:
        return default_key

    any_active = (
        db.query(KieApiKey)
        .filter(KieApiKey.is_active.is_(True))
        .order_by(KieApiKey.id.asc())
        .first()
    )
    if any_active is not None:
        return any_active

    raise ValueError("No active KIE API key configured")


def create_kie_key(
    db: Session,
    *,
    name: str,
    api_key_plaintext: str,
    is_default: bool = False,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieApiKey:
    """
    新建一个 KIE API key 记录（平台级）。

    注意：这里只做 flush，不 commit。
    """
    name = name.strip()
    if not name:
        raise ValueError("name is required")

    api_key_plaintext = api_key_plaintext.strip()
    if not api_key_plaintext:
        raise ValueError("api_key is required")

    ciphertext = encrypt_api_key(api_key_plaintext)

    if is_default:
        # 取消其它默认 key
        existing_defaults: Iterable[KieApiKey] = (
            db.query(KieApiKey)
            .filter(KieApiKey.is_default.is_(True))
            .all()
        )
        for k in existing_defaults:
            k.is_default = False
            db.add(k)

    key = KieApiKey(
        name=name,
        api_key_ciphertext=ciphertext,
        is_active=True,
        is_default=is_default,
    )
    db.add(key)
    db.flush()  # 拿到 id

    log_event(
        db,
        action="kie.key.create",
        resource_type="kie_api_key",
        resource_id=key.id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=None,  # 平台级配置，不属于某个租户
        details={
            "name": name,
            "is_default": is_default,
        },
    )

    return key


def update_kie_key(
    db: Session,
    *,
    key: KieApiKey,
    name: Optional[str] = None,
    api_key_plaintext: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_default: Optional[bool] = None,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieApiKey:
    """
    更新 key 信息（名称 / key / 启用状态 / 默认标记）。
    """
    changed: dict[str, object] = {}

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name cannot be empty")
        if key.name != name:
            changed["name"] = {"old": key.name, "new": name}
            key.name = name

    if api_key_plaintext is not None:
        api_key_plaintext = api_key_plaintext.strip()
        if not api_key_plaintext:
            raise ValueError("api_key cannot be empty")
        new_cipher = encrypt_api_key(api_key_plaintext)
        if key.api_key_ciphertext != new_cipher:
            changed["api_key"] = "***changed***"
            key.api_key_ciphertext = new_cipher

    if is_active is not None and key.is_active != is_active:
        changed["is_active"] = {"old": key.is_active, "new": is_active}
        key.is_active = is_active

    if is_default is not None and key.is_default != is_default:
        changed["is_default"] = {"old": key.is_default, "new": is_default}
        key.is_default = is_default

        if is_default:
            # 取消其它默认 key
            others = (
                db.query(KieApiKey)
                .filter(
                    KieApiKey.id != key.id,
                    KieApiKey.is_default.is_(True),
                )
                .all()
            )
            for other in others:
                other.is_default = False
                db.add(other)

    if changed:
        db.add(key)
        db.flush()

        log_event(
            db,
            action="kie.key.update",
            resource_type="kie_api_key",
            resource_id=key.id,
            actor_user_id=actor_user_id,
            actor_workspace_id=actor_workspace_id,
            actor_ip=actor_ip,
            user_agent=user_agent,
            workspace_id=None,
            details=changed,
        )

    return key


def deactivate_kie_key(
    db: Session,
    *,
    key: KieApiKey,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieApiKey:
    """
    停用某个 key。
    """
    if not key.is_active and not key.is_default:
        return key

    key.is_active = False
    key.is_default = False
    db.add(key)
    db.flush()

    log_event(
        db,
        action="kie.key.deactivate",
        resource_type="kie_api_key",
        resource_id=key.id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=None,
        details={"id": key.id},
    )

    return key


__all__ = [
    "encrypt_api_key",
    "decrypt_api_key",
    "list_keys",
    "get_key_by_id",
    "get_default_key",
    "get_effective_key",
    "create_kie_key",
    "update_kie_key",
    "deactivate_kie_key",
    "fetch_key_credits",
    "select_best_kie_key_for_task",
]

