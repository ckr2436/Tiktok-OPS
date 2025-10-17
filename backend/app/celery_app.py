# app/celery_app.py
from __future__ import annotations

import json
import os
from typing import Sequence

from celery import Celery
from kombu import Queue, Exchange

from app.core.config import settings

# 兼容两种环境变量命名
BROKER_URL = (
    getattr(settings, "CELERY_BROKER_URL", None)
    or os.getenv("CELERY_BROKER_URL")
)
BACKEND_URL = (
    getattr(settings, "CELERY_RESULT_BACKEND", None)
    or os.getenv("CELERY_RESULT_BACKEND")
    or getattr(settings, "CELERY_BACKEND_URL", None)  # 兼容旧名
    or os.getenv("CELERY_BACKEND_URL")
    or os.getenv("REDIS_URL")  # 最后兜底
)

# Celery 实例
celery_app = Celery("gmv", broker=BROKER_URL, backend=BACKEND_URL)

# 读取队列配置（来自 .env）
def _load_queues() -> tuple[str, Sequence[Queue]]:
    default_q = getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "gmv.tasks.default")
    raw_list = getattr(settings, "CELERY_TASK_QUEUES", None)
    names: list[str]
    if raw_list is None:
        env_list = os.getenv("CELERY_TASK_QUEUES")
        if env_list:
            try:
                names = list(json.loads(env_list))
            except Exception:
                names = [default_q]
        else:
            names = [default_q]
    else:
        # pydantic BaseSettings 可能已解析成 list[str]
        if isinstance(raw_list, (list, tuple)):
            names = list(raw_list)
        else:
            try:
                names = list(json.loads(str(raw_list)))
            except Exception:
                names = [default_q]

    # 统一 direct 交换器
    exch = Exchange("gmv.celery", type="direct", durable=True)
    qs = [Queue(n, exchange=exch, routing_key=n, durable=True) for n in names]
    return default_q, qs

default_queue_name, queue_objs = _load_queues()

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=getattr(settings, "CELERY_TIMEZONE", "UTC"),
    enable_utc=True,

    # 可靠性
    task_acks_late=bool(getattr(settings, "CELERY_TASK_ACKS_LATE", True)),
    task_reject_on_worker_lost=bool(getattr(settings, "CELERY_TASK_REJECT_ON_WORKER_LOST", True)),
    worker_concurrency=int(getattr(settings, "CELERY_WORKER_CONCURRENCY", 4)),

    # 队列
    task_default_queue=default_queue_name,
    task_default_exchange="gmv.celery",
    task_default_routing_key=default_queue_name,
    task_queues=queue_objs,
)

# ★ 导入任务，确保 worker 启动即注册
import app.tasks.oauth_tasks  # noqa: F401

