# app/celery_app.py
from __future__ import annotations

import json
import logging
import os
from typing import Sequence
from urllib.parse import urlparse

from celery import Celery
from kombu import Queue, Exchange

from app.core.config import settings


def _use_ssl(url: str | None) -> bool:
    if not url:
        return False
    try:
        return urlparse(url).scheme.lower() == "amqps"
    except Exception:
        return False


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
celery_app = Celery("gmv")
celery_app.conf.broker_url = BROKER_URL
celery_app.conf.result_backend = BACKEND_URL

# 安全处理 SSL 选项（避免 pop 触发 KeyError）
if _use_ssl(celery_app.conf.broker_url):
    celery_app.conf.broker_use_ssl = True
else:
    try:
        if "broker_use_ssl" in celery_app.conf:  # type: ignore[operator]
            del celery_app.conf["broker_use_ssl"]  # type: ignore[index]
    except Exception:
        pass


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
        if isinstance(raw_list, (list, tuple)):
            names = list(raw_list)
        else:
            try:
                names = list(json.loads(str(raw_list)))
            except Exception:
                names = [default_q]

    # 统一 direct 交换器；并强制把 gmvmax 队列加上（防止忘配 .env）
    if "gmvmax" not in names:
        names.append("gmvmax")

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

    # 可靠性（生产默认；均可用环境变量覆盖）
    task_acks_late=bool(getattr(settings, "CELERY_TASK_ACKS_LATE", True)),
    task_reject_on_worker_lost=bool(getattr(settings, "CELERY_TASK_REJECT_ON_WORKER_LOST", True)),
    worker_concurrency=int(getattr(settings, "CELERY_WORKER_CONCURRENCY", 4)),
    worker_prefetch_multiplier=int(getattr(settings, "CELERY_WORKER_PREFETCH", 1)),
    task_track_started=bool(getattr(settings, "CELERY_TASK_TRACK_STARTED", True)),
    task_time_limit=int(getattr(settings, "CELERY_TASK_HARD_TIME_LIMIT", 60 * 30)),   # 30 min
    task_soft_time_limit=int(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 60 * 25)),  # 25 min
    result_expires=int(getattr(settings, "CELERY_RESULT_EXPIRES", 60 * 60 * 24 * 3)),  # 3 days

    # 队列
    task_default_queue=default_queue_name,
    task_default_exchange="gmv.celery",
    task_default_routing_key=default_queue_name,
    task_queues=queue_objs,
)

# 路由双保险：所有 gmvmax.* 任务默认进 gmvmax 队列
celery_app.conf.task_routes = {
    "gmvmax.*": {"queue": "gmvmax"},
}

# 默认注册 creative heating 巡检任务（可通过环境变量覆盖）
beat_schedule = dict(getattr(celery_app.conf, "beat_schedule", {}) or {})
beat_schedule.setdefault(
    "gmvmax_creative_heating_cycle",
    {
        "task": "gmvmax.creative_heating_cycle",
        "schedule": int(getattr(settings, "GMVMAX_HEATING_CYCLE_INTERVAL", 15 * 60)),
    },
)
celery_app.conf.beat_schedule = beat_schedule

# ★ 导入任务，确保 worker 启动即注册
import app.tasks.oauth_tasks  # noqa: F401
import app.tasks.ttb_sync_tasks  # noqa: F401
import app.tasks.kie_ai.sora.sora2_image_to_video_tasks  # noqa: F401
import app.tasks.ttb_gmvmax_tasks  # noqa: F401  # ← 新增：注册 gmvmax 任务

# Whisper 字幕任务依赖第三方库（yt_dlp），在某些环境下可能未安装。
# 为了避免整个应用的模块导入失败，这里容错处理缺失依赖，
# 仅跳过相关任务注册并打印警告日志。
try:
    import app.features.tenants.openai_whisper.tasks  # noqa: F401  # 注册 Whisper 字幕任务
except ModuleNotFoundError as exc:
    if exc.name == "yt_dlp":
        logging.getLogger(__name__).warning(
            "skip registering Whisper tasks: missing optional dependency %s", exc.name
        )
    else:
        raise

