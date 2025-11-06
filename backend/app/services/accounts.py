# app/services/accounts.py
from __future__ import annotations
import re
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.data.models.workspaces import Workspace
from app.data.models.users import User
from app.core.security import hash_password

USERNAME_RE = re.compile(r"[^a-z0-9._-]+")


def ensure_platform_workspace(db: Session) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.company_code == "0000"))
    if ws:
        return ws
    ws = Workspace(name="Platform", company_code="0000")
    db.add(ws)
    db.flush()
    return ws


def next_usercode(db: Session, company_code: str) -> str:
    like_prefix = f"{company_code}"
    last = db.scalar(
        select(User.usercode)
        .where(User.usercode.like(like_prefix + "%"))
        .order_by(User.usercode.desc())
        .limit(1)
    )
    seq = int(last[-5:]) + 1 if last else 1
    return f"{company_code}{seq:05d}"


def normalize_username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    local = USERNAME_RE.sub("", local)
    local = local.strip(".-_")
    return local or "user"


def ensure_unique_username(db: Session, workspace_id: int, preferred: str) -> str:
    base = preferred
    i = 0
    while True:
        name = base if i == 0 else f"{base}-{i}"
        exists = db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.workspace_id == workspace_id,
                User.username == name,
                User.deleted_at.is_(None),
            )
        )
        if not exists:
            return name
        i += 1


def create_user(
    db: Session,
    *,
    workspace_id: int,
    email: str,
    password: str,
    role: str,
    is_platform_admin: bool,
    display_name: Optional[str],
    username: Optional[str],
    created_by_user_id: Optional[int],
    company_code: str,
) -> User:
    """
    创建用户，兼容软删除“复活”逻辑。

    - 如果同邮箱用户存在且未软删除：抛 ValueError('EMAIL_EXISTS')
    - 如果同邮箱用户存在且已软删除：复活该条记录（更新字段、deleted_at 置空），不再插入新行
    - 否则正常插入新用户
    """
    # 统一归一化 email
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("EMAIL_REQUIRED")

    # 先算好 username & usercode
    preferred_uname = username or normalize_username_from_email(email)
    uname = ensure_unique_username(db, workspace_id, preferred_uname)
    ucode = next_usercode(db, company_code)

    pwd_hash = hash_password(password)

    # 不再带 deleted_at 过滤，先看有没有“任何状态”的同邮箱用户
    existing = db.scalar(select(User).where(User.email == email))

    if existing:
        # 1) 已存在且未软删：业务上不允许重复邮箱
        if existing.deleted_at is None:
            raise ValueError("EMAIL_EXISTS")

        # 2) 已存在且软删：复活这条记录
        existing.workspace_id = workspace_id
        existing.username = uname
        existing.display_name = display_name
        existing.password_hash = pwd_hash
        existing.is_active = True
        existing.is_platform_admin = is_platform_admin
        existing.role = role
        existing.created_by_user_id = created_by_user_id
        existing.deleted_at = None  # 关键：恢复

        # 对于已存在的 usercode，我们保留原值，不再重新分配
        # 如确实想重置，可改成 existing.usercode = ucode

        # 手动更新时间（虽然 updated_at 有 server_onupdate，但这里显式改也没问题）
        existing.updated_at = datetime.now(timezone.utc)

        db.add(existing)
        db.flush()
        return existing

    # 3) 正常新建用户（完全不存在同邮箱记录）
    u = User(
        workspace_id=workspace_id,
        email=email,
        username=uname,
        display_name=display_name,
        password_hash=pwd_hash,
        is_active=True,
        is_platform_admin=is_platform_admin,
        role=role,
        usercode=ucode,
        created_by_user_id=created_by_user_id,
    )
    db.add(u)
    try:
        db.flush()
    except IntegrityError as e:
        # 极端情况下（并发）兜底一次，防止直接 500
        raise ValueError("EMAIL_EXISTS") from e

    return u


def alloc_company_code(db: Session) -> str:
    last = db.scalar(
        select(Workspace.company_code)
        .where(Workspace.company_code != "0000")
        .order_by(Workspace.company_code.desc())
        .limit(1)
    )
    if last:
        n = int(last)
        if n >= 9999:
            raise RuntimeError("COMPANY_CODE_EXHAUSTED")
        return f"{n+1:04d}"
    return "0001"

