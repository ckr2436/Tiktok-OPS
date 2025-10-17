# app/tasks/ttb_sync_tasks.py
from __future__ import annotations

import asyncio
import contextlib
from typing import Dict

from celery import Task
from celery.utils.log import get_task_logger
from celery.result import AsyncResult
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.data.db import get_db
from app.core.config import settings
from app.core.errors import APIError

from app.services.oauth_ttb import (
    get_access_token_for_auth_id,      # ✅ 保持不变
    get_app_credentials_for_auth_id,   # ✅ 使用你文件里存在的函数名
)
from app.services.ttb_api import TTBApiClient
from app.services.ttb_sync import TTBSyncService
from app.services.db_locks import mysql_advisory_lock, binding_action_lock_key

logger = get_task_logger(__name__)


def _db_session() -> Session:
    gen = get_db()
    db = next(gen)
    setattr(db, "__GEN__", gen)
    return db


def _db_close(db: Session):
    gen = getattr(db, "__GEN__", None)
    with contextlib.suppress(Exception):
        if gen:
            next(gen, None)
    with contextlib.suppress(Exception):
        db.close()


def _push_recent_job(workspace_id: int, auth_id: int, task_id: str, max_len: int = 200):
    backend = getattr(celery_app, "backend", None)
    client = getattr(backend, "client", None)
    if not client:
        return
    try:
        key = f"jobs:ttb:{workspace_id}:{auth_id}"
        client.lpush(key, task_id)
        client.ltrim(key, 0, max_len - 1)
    except Exception:
        pass


class BindingTask(Task):
    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = 60
    retry_jitter = True

    def set_progress(self, meta: Dict):
        try:
            self.update_state(state="PROGRESS", meta=meta)
        except Exception:
            pass


@celery_app.task(name="tenant.ttb.sync.bc", base=BindingTask, queue="gmv.tasks.events")
def task_sync_bc(workspace_id: int, auth_id: int, params: Dict):
    _push_recent_job(workspace_id, auth_id, task_sync_bc.request.id)
    db = _db_session()
    try:
        lock_key = binding_action_lock_key(workspace_id, auth_id, "bc")
        with mysql_advisory_lock(db, lock_key, wait_seconds=1) as got:
            if not got:
                return {"error": "another bc job running for this binding"}
            token = get_access_token_for_auth_id(db, int(auth_id))
            client = TTBApiClient(access_token=token, qps=float(getattr(settings, "TTB_QPS", 10.0)))
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            task_sync_bc.update_state(state="STARTED", meta={"progress": {"phase": "bc"}})
            data = asyncio.run(svc.sync_bc(limit=int(params.get("limit") or 200)))
            db.commit()
            return {"result": data}
    except APIError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        _db_close(db)


@celery_app.task(name="tenant.ttb.sync.advertisers", base=BindingTask, queue="gmv.tasks.events")
def task_sync_advertisers(workspace_id: int, auth_id: int, params: Dict):
    _push_recent_job(workspace_id, auth_id, task_sync_advertisers.request.id)
    db = _db_session()
    try:
        lock_key = binding_action_lock_key(workspace_id, auth_id, "advertisers")
        with mysql_advisory_lock(db, lock_key, wait_seconds=1) as got:
            if not got:
                return {"error": "another advertisers job running for this binding"}
            # ✅ 对齐你的 oauth_ttb.py：分别取 token 与 app 的 client_id/secret
            token = get_access_token_for_auth_id(db, int(auth_id))
            app_id, secret, _redirect_uri = get_app_credentials_for_auth_id(db, int(auth_id))

            client = TTBApiClient(access_token=token, qps=float(getattr(settings, "TTB_QPS", 10.0)))
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            task_sync_advertisers.update_state(state="STARTED", meta={"progress": {"phase": "advertisers"}})
            data = asyncio.run(
                svc.sync_advertisers(limit=int(params.get("limit") or 200), app_id=app_id, secret=secret)
            )
            db.commit()
            return {"result": data}
    except APIError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        _db_close(db)


@celery_app.task(name="tenant.ttb.sync.shops", base=BindingTask, queue="gmv.tasks.events")
def task_sync_shops(workspace_id: int, auth_id: int, params: Dict):
    _push_recent_job(workspace_id, auth_id, task_sync_shops.request.id)
    db = _db_session()
    try:
        lock_key = binding_action_lock_key(workspace_id, auth_id, "shops")
        with mysql_advisory_lock(db, lock_key, wait_seconds=1) as got:
            if not got:
                return {"error": "another shops job running for this binding"}
            token = get_access_token_for_auth_id(db, int(auth_id))
            client = TTBApiClient(access_token=token, qps=float(getattr(settings, "TTB_QPS", 10.0)))
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            task_sync_shops.update_state(state="STARTED", meta={"progress": {"phase": "shops"}})
            data = asyncio.run(svc.sync_shops(limit=int(params.get("limit") or 200)))
            db.commit()
            return {"result": data}
    except APIError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        _db_close(db)


@celery_app.task(name="tenant.ttb.sync.products", base=BindingTask, queue="gmv.tasks.events")
def task_sync_products(workspace_id: int, auth_id: int, params: Dict):
    _push_recent_job(workspace_id, auth_id, task_sync_products.request.id)
    db = _db_session()
    try:
        lock_key = binding_action_lock_key(workspace_id, auth_id, "products")
        with mysql_advisory_lock(db, lock_key, wait_seconds=1) as got:
            if not got:
                return {"error": "another products job running for this binding"}
            token = get_access_token_for_auth_id(db, int(auth_id))
            client = TTBApiClient(access_token=token, qps=float(getattr(settings, "TTB_QPS", 10.0)))
            svc = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)
            task_sync_products.update_state(state="STARTED", meta={"progress": {"phase": "products"}})
            data = asyncio.run(
                svc.sync_products(limit=int(params.get("limit") or 200), shop_id=params.get("shop_id"))
            )
            db.commit()
            return {"result": data}
    except APIError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        _db_close(db)


@celery_app.task(name="tenant.ttb.sync.bootstrap_orchestrator", base=BindingTask, queue="gmv.tasks.default")
def task_bootstrap(workspace_id: int, auth_id: int, idempotency_key: str):
    _push_recent_job(workspace_id, auth_id, task_bootstrap.request.id)
    plan = [
        ("tenant.ttb.sync.bc", {}),
        ("tenant.ttb.sync.advertisers", {}),
        ("tenant.ttb.sync.shops", {}),
        ("tenant.ttb.sync.products", {}),
    ]
    enqueued = []
    for task_name, kwargs in plan:
        sig = celery_app.signature(
            task_name, kwargs={"workspace_id": workspace_id, "auth_id": auth_id, "params": kwargs}
        )
        res = sig.apply_async(queue="gmv.tasks.events")
        enqueued.append({"task": task_name, "job_id": res.id})
    return {"result": {"enqueued": enqueued}}

