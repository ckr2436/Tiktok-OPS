# app/data/db.py
from __future__ import annotations

import logging
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session as ORMSession, sessionmaker

from app.core.config import settings

logger = logging.getLogger("gmv.db")


# ---- ORM Base -------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---- Engine（生产参数从 settings 注入；带合理默认）--------------------------
engine: Engine = create_engine(
    settings.DATABASE_URL,
    future=True,
    pool_pre_ping=getattr(settings, "DB_POOL_PRE_PING", True),
    pool_recycle=getattr(settings, "DB_POOL_RECYCLE", 1800),
    pool_size=getattr(settings, "DB_POOL_SIZE", 5),
    max_overflow=getattr(settings, "DB_MAX_OVERFLOW", 10),
    echo=getattr(settings, "DB_ECHO", False),
)

# ---- Session 工厂 ---------------------------------------------------------
SessionLocal = sessionmaker(
    bind=engine,
    class_=ORMSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


# ---- 写入标记工具 ----------------------------------------------------------
def _reset_mutation_flag(sess: ORMSession) -> None:
    sess.info.pop("has_writes", None)


def _mark_mutated(sess: ORMSession) -> None:
    sess.info["has_writes"] = True


# 每个事务一开始就重置，避免上一事务的标记串味
@event.listens_for(ORMSession, "after_begin")
def _on_tx_begin(sess: ORMSession, tx, connection) -> None:
    _reset_mutation_flag(sess)


# ORM flush 产生 new/dirty/deleted 即认为有写
@event.listens_for(ORMSession, "after_flush")
def _on_after_flush(sess: ORMSession, ctx) -> None:
    if sess.new or sess.dirty or sess.deleted:
        _mark_mutated(sess)


# 任何非 SELECT 的 ORM 语句（insert/update/delete/merge 等）都视为写
@event.listens_for(ORMSession, "do_orm_execute")
def _on_do_orm_execute(exec_state) -> None:
    try:
        if not exec_state.is_select:
            _mark_mutated(exec_state.session)
    except Exception:
        # 防御性：不让标记影响主流程
        pass


def _has_writes(sess: ORMSession) -> bool:
    if sess.info.get("has_writes"):
        return True
    # 兜底：未触发事件时，以 identity map 判断
    return bool(sess.new or sess.dirty or sess.deleted)


# ---- FastAPI 依赖：请求级 Session -----------------------------------------
def get_db() -> Generator[ORMSession, None, None]:
    """
    生产模式统一会话管理：
    - 正常：检测到写入 → COMMIT；否则（纯读）若事务仍活跃 → ROLLBACK（释放锁/降噪）
    - 异常：ROLLBACK 并继续向上抛
    - 最终：CLOSE
    """
    db: ORMSession = SessionLocal()
    _reset_mutation_flag(db)  # 避免继承连接 info

    try:
        yield db

        tx = db.get_transaction()
        if tx is not None and tx.is_active:
            if _has_writes(db):
                try:
                    db.commit()
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "DB COMMIT ok (new=%d dirty=%d deleted=%d)",
                            len(db.new), len(db.dirty), len(db.deleted),
                        )
                except Exception:
                    try:
                        db.rollback()
                    finally:
                        logger.exception("DB COMMIT failed -> ROLLBACK")
                        raise
            else:
                # 纯读请求：显示回滚以结束事务
                db.rollback()
    except Exception:
        # 任意异常 → 兜底回滚
        try:
            tx = db.get_transaction()
            if tx is not None and tx.is_active:
                db.rollback()
        finally:
            raise
    finally:
        db.close()

