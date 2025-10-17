# app/services/accounts.py
from __future__ import annotations
import re
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import select, func
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
    # 取该 company 下最大 usercode，+1
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
    # 若冲突，依次尝试 name, name-1, name-2 ...
    base = preferred
    i = 0
    while True:
        name = base if i == 0 else f"{base}-{i}"
        exists = db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.workspace_id == workspace_id, User.username == name, User.deleted_at.is_(None))
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
    # email 全局唯一
    if db.scalar(select(func.count()).select_from(User).where(User.email == email, User.deleted_at.is_(None))):
        raise ValueError("EMAIL_EXISTS")

    uname = ensure_unique_username(db, workspace_id, username or normalize_username_from_email(email))
    ucode = next_usercode(db, company_code)

    u = User(
        workspace_id=workspace_id,
        email=email,
        username=uname,
        display_name=display_name,
        password_hash=hash_password(password),
        is_active=True,
        is_platform_admin=is_platform_admin,
        role=role,
        usercode=ucode,
        created_by_user_id=created_by_user_id,
    )
    db.add(u)
    db.flush()
    return u

def alloc_company_code(db: Session) -> str:
    # 自动分配 0001..9999，避开 0000
    # 取现有最大 4 位数字字符串，+1
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

