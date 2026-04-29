# app/celery_app.py
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Sequence
from urllib.parse import urlparse

from celery import Celery
from celery.schedules import crontab
from kombu import Queue, Exchange

from app.core.config import settings


def _use_ssl(url: str | None) -> bool:
    if not url:
        return False
    try:
        return urlparse(url).scheme.lower() == "amqps"
    except Exception:
        return False


def _dedupe_names(names: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        item = str(name or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


BROKER_URL = getattr(settings, "CELERY_BROKER_URL", None) or os.getenv("CELERY_BROKER_URL")
BACKEND_URL = (
    getattr(settings, "CELERY_RESULT_BACKEND", None)
    or os.getenv("CELERY_RESULT_BACKEND")
    or getattr(settings, "CELERY_BACKEND_URL", None)
    or os.getenv("CELERY_BACKEND_URL")
    or os.getenv("REDIS_URL")
)

celery_app = Celery("gmv")
celery_app.conf.broker_url = BROKER_URL
celery_app.conf.result_backend = BACKEND_URL

if _use_ssl(celery_app.conf.broker_url):
    celery_app.conf.broker_use_ssl = True
else:
    try:
        if "broker_use_ssl" in celery_app.conf:  # type: ignore[operator]
            del celery_app.conf["broker_use_ssl"]  # type: ignore[index]
    except Exception:
        pass


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

    required_queues = [
        default_q,
        "gmv.tasks.events",
        "gmv.tasks.maintenance",
        "gmv.tasks.kie_ai",
        "gmvmax",
        "gmvmax_sync",
    ]
    whisper_q = getattr(settings, "OPENAI_WHISPER_TASK_QUEUE", None)
    if whisper_q:
        required_queues.append(str(whisper_q))

    names = _dedupe_names([*names, *required_queues])
    exch = Exchange("gmv.celery", type="direct", durable=True)
    qs = [Queue(n, exchange=exch, routing_key=n, durable=True) for n in names]
    return default_q, qs


default_queue_name, queue_objs = _load_queues()
WHISPER_TASK_QUEUE = getattr(settings, "OPENAI_WHISPER_TASK_QUEUE", None) or default_queue_name

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=getattr(settings, "CELERY_TIMEZONE", "UTC"),
    enable_utc=True,
    task_acks_late=bool(getattr(settings, "CELERY_TASK_ACKS_LATE", True)),
    task_reject_on_worker_lost=bool(getattr(settings, "CELERY_TASK_REJECT_ON_WORKER_LOST", True)),
    worker_concurrency=int(getattr(settings, "CELERY_WORKER_CONCURRENCY", 4)),
    worker_prefetch_multiplier=int(getattr(settings, "CELERY_WORKER_PREFETCH", 1)),
    task_track_started=bool(getattr(settings, "CELERY_TASK_TRACK_STARTED", True)),
    task_time_limit=int(getattr(settings, "CELERY_TASK_HARD_TIME_LIMIT", 60 * 30)),
    task_soft_time_limit=int(getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 60 * 25)),
    result_expires=int(getattr(settings, "CELERY_RESULT_EXPIRES", 60 * 60 * 24 * 3)),
    worker_enable_remote_control=bool(getattr(settings, "CELERY_WORKER_ENABLE_REMOTE_CONTROL", False)),
    worker_send_task_events=bool(getattr(settings, "CELERY_WORKER_SEND_TASK_EVENTS", False)),
    task_send_sent_event=bool(getattr(settings, "CELERY_TASK_SEND_SENT_EVENT", False)),
    task_create_missing_queues=bool(getattr(settings, "CELERY_TASK_CREATE_MISSING_QUEUES", False)),
    task_default_queue=default_queue_name,
    task_default_exchange="gmv.celery",
    task_default_routing_key=default_queue_name,
    task_queues=queue_objs,
)

celery_app.conf.task_routes = {
    "openai_whisper.*": {"queue": WHISPER_TASK_QUEUE},
    "kie_ai.sora2.*": {"queue": "gmv.tasks.kie_ai"},
    "ttb.sync.*": {"queue": "gmv.tasks.events"},
    "gmvmax.*": {"queue": "gmvmax"},
}
celery_app.conf.task_routes.update(
    {
        "gmvmax.sync.run_scheduler": {"queue": "gmvmax_sync"},
        "gmvmax.sync.run_for_strategy": {"queue": "gmvmax_sync"},
    }
)

beat_schedule = dict(getattr(celery_app.conf, "beat_schedule", {}) or {})
beat_schedule.setdefault(
    "gmvmax_creative_heating_cycle",
    {
        "task": "gmvmax.creative_heating_cycle",
        "schedule": int(getattr(settings, "GMVMAX_HEATING_CYCLE_INTERVAL", 15 * 60)),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_sync_scheduler",
    {
        "task": "gmvmax.sync.run_scheduler",
        "schedule": int(
            getattr(
                settings,
                "GMVMAX_SCHEDULER_INTERVAL_SECONDS",
                os.getenv("GMVMAX_SCHEDULER_INTERVAL_SECONDS", "60"),
            )
        ),
        "options": {"queue": "gmvmax_sync"},
    },
)
beat_schedule.setdefault(
    "gmvmax_cleanup_overview_snapshots",
    {
        "task": "gmvmax.cleanup_overview_snapshots",
        "schedule": crontab(hour=3, minute=0),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_cleanup_campaign_tables",
    {
        "task": "gmvmax.cleanup_campaign_tables",
        "schedule": crontab(hour=4, minute=0),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "openai_whisper_cleanup_jobs",
    {
        "task": "openai_whisper.cleanup_jobs",
        "schedule": crontab(hour=3, minute=30),
        "options": {"queue": WHISPER_TASK_QUEUE},
    },
)
celery_app.conf.beat_schedule = beat_schedule

GMVMAX_SYNC_INTERVAL_OPTIONS = (10, 15, 20, 30)
GMVMAX_SYNC_TASK_NAME = "ttb.sync_gmvmax"


def _gmvmax_schedule_key(minutes: int) -> str:
    return f"gmvmax-sync-{int(minutes)}min"


_gmvmax_schedule_lock = threading.Lock()


def set_gmvmax_sync_interval(interval_minutes: int) -> int:
    normalized = interval_minutes if interval_minutes in GMVMAX_SYNC_INTERVAL_OPTIONS else GMVMAX_SYNC_INTERVAL_OPTIONS[0]
    with _gmvmax_schedule_lock:
        schedule = dict(getattr(celery_app.conf, "beat_schedule", {}) or {})
        for minutes in GMVMAX_SYNC_INTERVAL_OPTIONS:
            schedule.pop(_gmvmax_schedule_key(minutes), None)
        schedule[_gmvmax_schedule_key(normalized)] = {
            "task": GMVMAX_SYNC_TASK_NAME,
            "schedule": float(normalized) * 60.0,
            "options": {"queue": "gmvmax"},
        }
        celery_app.conf.beat_schedule = schedule
        celery_app.conf.gmvmax_sync_interval_minutes = normalized
    return normalized


def get_gmvmax_sync_interval() -> int:
    return int(getattr(celery_app.conf, "gmvmax_sync_interval_minutes", GMVMAX_SYNC_INTERVAL_OPTIONS[0]))


set_gmvmax_sync_interval(int(getattr(settings, "GMVMAX_SYNC_INTERVAL_MINUTES", GMVMAX_SYNC_INTERVAL_OPTIONS[0])))

import app.tasks.oauth_tasks  # noqa: F401
import app.tasks.ttb_sync_tasks  # noqa: F401
import app.tasks.kie_ai.sora.sora2_image_to_video_tasks  # noqa: F401
import app.tasks.ttb_gmvmax_tasks  # noqa: F401
import app.gmvmax.tasks_sync  # noqa: F401

try:
    import app.features.tenants.openai_whisper.tasks  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name == "yt_dlp":
        logging.getLogger(__name__).warning("skip registering Whisper tasks: missing optional dependency %s", exc.name)
    else:
        raise
