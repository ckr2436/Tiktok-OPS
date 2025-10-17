#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/dump_ttb_creds.py
解密 TikTok Business 的 app secret 与 access_token（基于你现有的 AES-GCM+AAG 方案）
- 依赖你的项目：app.services.crypto, app.data.models.oauth_ttb, app.data.db
- 不做任何别名/重命名；严格使用 client_id / client_secret_cipher 等原字段
- AAD 规则：f"{provider}|{client_id}|{redirect_uri}"

用法示例：
    # 仅指定 workspace，自动选取最新的 active 绑定 + 首个启用的 provider app
    python scripts/dump_ttb_creds.py --workspace-id 4

    # 指定 Provider App (例如你返回的 id=1)
    python scripts/dump_ttb_creds.py --workspace-id 4 --provider-app-id 1

    # 已知绑定ID（auth_id），直接指定，更精确
    python scripts/dump_ttb_creds.py --workspace-id 4 --auth-id 2
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from typing import Optional

# ---- 保证可以 import 到 app.* ----
# 建议在 /opt/gmv/backend 下执行，或自行修改此处 PYTHONPATH
ROOT_HINTS = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),  # scripts/.. -> backend/
    os.getcwd(),
]
for p in ROOT_HINTS:
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy import select, desc  # type: ignore
from sqlalchemy.orm import Session  # type: ignore

from app.data.db import get_db  # 按你的冻结规范
from app.services.crypto import decrypt_blob_to_text
from app.data.models.oauth_ttb import (
    OAuthProviderApp,
    OAuthAccountTTB,
)

def _open_db() -> Session:
    """按 FastAPI 依赖的写法拿到一个 Session，并负责收尾。"""
    db_gen = get_db()
    db = next(db_gen)
    # 将生成器对象挂在 session 上，便于 finally 关闭
    setattr(db, "_gen", db_gen)
    return db

def _close_db(db: Session) -> None:
    gen = getattr(db, "_gen", None)
    if gen:
        try:
            gen.close()
        except Exception:
            pass

def _pick_token_blob(acc: OAuthAccountTTB) -> Optional[bytes]:
    """
    兼容多个可能的密文字段名（与你代码一致的优先级）：
    access_token_cipher / token_cipher / access_token_encrypted / access_token_blob
    """
    for fname in ("access_token_cipher", "token_cipher", "access_token_encrypted", "access_token_blob"):
        if hasattr(acc, fname):
            v = getattr(acc, fname)
            if isinstance(v, (bytes, memoryview)) and v:
                return bytes(v)
    return None

def _decrypt_app_secret(app: OAuthProviderApp) -> str:
    """
    严格使用你的 AAD 规则解密 client_secret_cipher：
        aad = f"{provider}|{client_id}|{redirect_uri}"
    """
    aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
    return decrypt_blob_to_text(app.client_secret_cipher, aad_text=aad)

def _decrypt_access_token(db: Session, acc: OAuthAccountTTB, app: OAuthProviderApp) -> str:
    """
    统一逻辑：优先从密文字段解，失败再尝试明文字段（与你服务里的一致）。
    """
    # 1) 尝试密文解密
    blob = _pick_token_blob(acc)
    if blob:
        aad = f"{app.provider}|{app.client_id}|{app.redirect_uri}"
        try:
            return decrypt_blob_to_text(blob, aad_text=aad)
        except Exception as e:
            # 继续走明文兜底
            pass

    # 2) 兜底可能存在的明文字段
    for fname in ("access_token_plain", "access_token_decrypted", "access_token"):
        if hasattr(acc, fname):
            v = getattr(acc, fname)
            if isinstance(v, str) and v:
                return v

    raise RuntimeError(f"无法解析 access_token（auth_id={acc.id}）")

def _select_provider_app(db: Session, provider_app_id: Optional[int]) -> OAuthProviderApp:
    if provider_app_id:
        app = db.get(OAuthProviderApp, int(provider_app_id))
        if not app or app.provider != "tiktok_business" or not app.is_enabled:
            raise RuntimeError("指定的 provider_app_id 不存在或已禁用，或 provider 不是 tiktok_business")
        return app

    # 未指定则选择首个启用的 tiktok_business 应用（按 id 升序）
    q = (
        select(OAuthProviderApp)
        .where(OAuthProviderApp.provider == "tiktok_business", OAuthProviderApp.is_enabled.is_(True))
        .order_by(OAuthProviderApp.id.asc())
    )
    app = db.execute(q).scalars().first()
    if not app:
        raise RuntimeError("未找到启用的 tiktok_business Provider App")
    return app

def _select_active_account(db: Session, workspace_id: int, provider_app_id: int, auth_id: Optional[int]) -> OAuthAccountTTB:
    if auth_id:
        acc = db.get(OAuthAccountTTB, int(auth_id))
        if not acc or acc.workspace_id != int(workspace_id):
            raise RuntimeError("指定的 auth_id 不存在于该 workspace")
        if acc.provider_app_id != int(provider_app_id):
            raise RuntimeError("指定的 auth_id 与 provider_app_id 不匹配")
        if getattr(acc, "status", None) not in ("active", "ACTIVE", None):
            # 某些库可能是枚举/小写，宽松判断
            pass
        return acc

    # 未指定 auth_id：选该 workspace + provider_app 下最新创建/更新的 active 账户
    q = (
        select(OAuthAccountTTB)
        .where(
            OAuthAccountTTB.workspace_id == int(workspace_id),
            OAuthAccountTTB.provider_app_id == int(provider_app_id),
            # 宽松认为非 revoked 即可，有些库 status 字段可能不同步
        )
        .order_by(desc(OAuthAccountTTB.id))
    )
    acc = db.execute(q).scalars().first()
    if not acc:
        raise RuntimeError("该 workspace 下没有任何与该 Provider App 绑定的 OAuth 账户")
    return acc

def main():
    ap = argparse.ArgumentParser(description="解密 TikTok Business app secret 与 access_token（严格按你的 AES-GCM+AAG 实现）")
    ap.add_argument("--workspace-id", type=int, required=True, help="租户 workspace_id（例如 4）")
    ap.add_argument("--provider-app-id", type=int, help="Provider App ID（例如 1）")
    ap.add_argument("--auth-id", type=int, help="OAuth 绑定 ID（如已知则更精确）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（默认为纯文本）")
    args = ap.parse_args()

    db = _open_db()
    try:
        app = _select_provider_app(db, args.provider_app_id)
        acc = _select_active_account(db, args.workspace_id, int(app.id), args.auth_id)

        # 解密 client_secret
        secret = _decrypt_app_secret(app)

        # 解密 access_token
        token = _decrypt_access_token(db, acc, app)

        # 一些辅助信息
        sha = hashlib.sha256(token.encode("utf-8")).hexdigest()

        if args.json:
            out = {
                "workspace_id": int(args.workspace_id),
                "provider_app_id": int(app.id),
                "oauth_account_id": int(acc.id),
                "app_id": app.client_id,
                "secret": secret,
                "access_token": token,
                "access_token_sha256": sha,
                "redirect_uri": app.redirect_uri,
                "provider": app.provider,
                "account_status": getattr(acc, "status", None),
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"WORKSPACE_ID={args.workspace_id}")
            print(f"PROVIDER_APP_ID={int(app.id)}")
            print(f"OAUTH_ACCOUNT_ID={int(acc.id)}")
            print(f"APP_ID={app.client_id}")
            print(f"SECRET={secret}")
            print(f"ACCESS_TOKEN={token}")
            print(f"ACCESS_TOKEN_SHA256={sha}")
            print(f"REDIRECT_URI={app.redirect_uri}")
    finally:
        _close_db(db)

if __name__ == "__main__":
    main()

