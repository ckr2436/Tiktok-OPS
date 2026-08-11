# app/celery_app.py
from __future__ import annotations

import json
import importlib
import logging
import os
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
TTB_SYNC_QUEUE = "gmv.tasks.events"
TIKTOK_SHOP_TASK_QUEUE = "tiktok_shop"
VIDEO_ANALYSIS_TASK_QUEUE = getattr(
    settings,
    "HERMES_VIDEO_ANALYSIS_TASK_QUEUE",
    "gmv.tasks.video_analysis",
)

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
        TTB_SYNC_QUEUE,
        "gmv.tasks.maintenance",
        getattr(settings, "AI_VIDEO_API_TASK_QUEUE", "gmv.tasks.ai_video.api"),
        getattr(settings, "AI_VIDEO_BROWSER_TASK_QUEUE", "gmv.tasks.ai_video.browser"),
        getattr(settings, "AI_VIDEO_BROWSER_POLL_TASK_QUEUE", "gmv.tasks.ai_video.browser_poll"),
        getattr(settings, "AI_VIDEO_DOWNLOAD_TASK_QUEUE", "gmv.tasks.ai_video.download"),
        getattr(settings, "AI_VIDEO_MAINTENANCE_TASK_QUEUE", "gmv.tasks.ai_video.maintenance"),
        getattr(settings, "HERMES_AGENT_TASK_QUEUE", "gmv.tasks.hermes_agent"),
        getattr(settings, "HERMES_MAINTENANCE_TASK_QUEUE", "gmv.tasks.hermes_maintenance"),
        "gmvmax",
        "gmvmax_control",
        "gmvmax_sync",
        getattr(settings, "WEBSITE_ADS_TASK_QUEUE", "website_ads"),
        getattr(settings, "WEBSITE_ADS_MEDIA_TASK_QUEUE", "website_ads_media"),
        TIKTOK_SHOP_TASK_QUEUE,
        VIDEO_ANALYSIS_TASK_QUEUE,
    ]
    try:
        hermes_slots = max(1, min(8, int(os.getenv("HERMES_BROWSER_SLOTS", "1"))))
    except ValueError:
        hermes_slots = 1
    hermes_base_q = str(getattr(settings, "HERMES_AGENT_TASK_QUEUE", "gmv.tasks.hermes_agent"))
    legacy_slot_queues = str(os.getenv("HERMES_ENABLE_LEGACY_SLOT_QUEUES", "")).lower() in {"1", "true", "yes"}
    if legacy_slot_queues and hermes_slots > 1:
        required_queues.extend(f"{hermes_base_q}.slot{slot}" for slot in range(hermes_slots))
    whisper_q = getattr(settings, "OPENAI_WHISPER_TASK_QUEUE", None)
    if whisper_q:
        required_queues.append(str(whisper_q))

    names = _dedupe_names([*names, *required_queues])
    exch = Exchange("gmv.celery", type="direct", durable=True)
    qs = [Queue(n, exchange=exch, routing_key=n, durable=True) for n in names]
    return default_q, qs


default_queue_name, queue_objs = _load_queues()
WHISPER_TASK_QUEUE = getattr(settings, "OPENAI_WHISPER_TASK_QUEUE", None) or default_queue_name
HERMES_AGENT_TASK_QUEUE = getattr(settings, "HERMES_AGENT_TASK_QUEUE", "gmv.tasks.hermes_agent")
HERMES_MAINTENANCE_TASK_QUEUE = getattr(
    settings, "HERMES_MAINTENANCE_TASK_QUEUE", "gmv.tasks.hermes_maintenance"
)
AI_VIDEO_API_TASK_QUEUE = getattr(
    settings, "AI_VIDEO_API_TASK_QUEUE", "gmv.tasks.ai_video.api"
)
AI_VIDEO_BROWSER_TASK_QUEUE = getattr(
    settings, "AI_VIDEO_BROWSER_TASK_QUEUE", "gmv.tasks.ai_video.browser"
)
AI_VIDEO_BROWSER_POLL_TASK_QUEUE = getattr(
    settings,
    "AI_VIDEO_BROWSER_POLL_TASK_QUEUE",
    "gmv.tasks.ai_video.browser_poll",
)
AI_VIDEO_DOWNLOAD_TASK_QUEUE = getattr(
    settings, "AI_VIDEO_DOWNLOAD_TASK_QUEUE", "gmv.tasks.ai_video.download"
)
AI_VIDEO_MAINTENANCE_TASK_QUEUE = getattr(
    settings, "AI_VIDEO_MAINTENANCE_TASK_QUEUE", "gmv.tasks.ai_video.maintenance"
)
WEBSITE_ADS_MEDIA_TASK_QUEUE = getattr(settings, "WEBSITE_ADS_MEDIA_TASK_QUEUE", "website_ads_media")
WEBSITE_ADS_TASK_QUEUE = getattr(settings, "WEBSITE_ADS_TASK_QUEUE", "website_ads")

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
    "openai_whisper.website_ads_asset_media_cache": {"queue": WEBSITE_ADS_MEDIA_TASK_QUEUE},
    "gmvmax.creative_asset_media_cache": {"queue": WEBSITE_ADS_MEDIA_TASK_QUEUE},
    "website_ads.upload_video": {"queue": WEBSITE_ADS_MEDIA_TASK_QUEUE},
    "openai_whisper.*": {"queue": WHISPER_TASK_QUEUE},
    "ai_video.result.download_task_result_files": {"queue": AI_VIDEO_DOWNLOAD_TASK_QUEUE},
    "ai_video.result.recover_stale_downloads": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    "ai_video.video.recover_stale_polling": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    "ai_video.video.*": {"queue": AI_VIDEO_API_TASK_QUEUE},
    "globalaiopc.video.*": {"queue": AI_VIDEO_API_TASK_QUEUE},
    "jimeng_lab.*": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    "doubao_lab.*": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    "doubao_provider.*": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    "hermes_content_factory.*": {"queue": HERMES_AGENT_TASK_QUEUE},
    "hermes_agent.reconcile_yt_dlp_cookie_keepalives": {
        "queue": HERMES_MAINTENANCE_TASK_QUEUE
    },
    "hermes_agent.reconcile_flow_auto_reauth": {
        "queue": HERMES_MAINTENANCE_TASK_QUEUE
    },
    "hermes_agent.*": {"queue": HERMES_AGENT_TASK_QUEUE},
    "ttb.sync.*": {"queue": TTB_SYNC_QUEUE},
    "tiktok_shop.*": {"queue": TIKTOK_SHOP_TASK_QUEUE},
    "tiktok_shop_video_analysis.*": {"queue": VIDEO_ANALYSIS_TASK_QUEUE},
    # Operator pauses must never sit behind scheduled reporting or Guard work.
    # Website Ads control-plane work has its own worker and queue.
    "gmvmax.execute_campaign_pause_intent": {"queue": "gmvmax_control"},
    "gmvmax.recover_campaign_pause_intents": {"queue": "gmvmax_control"},
    "gmvmax.*": {"queue": "gmvmax"},
    "website_ads.*": {"queue": WEBSITE_ADS_TASK_QUEUE},
}
celery_app.conf.task_routes.update(
    {
        "gmvmax.sync.run_scheduler": {"queue": "gmvmax_sync"},
        "gmvmax.sync.run_for_strategy": {"queue": "gmvmax_sync"},
    }
)

beat_schedule = dict(getattr(celery_app.conf, "beat_schedule", {}) or {})
beat_schedule.setdefault(
    "yt_dlp_cookie_keepalive_reconciliation",
    {
        "task": "hermes_agent.reconcile_yt_dlp_cookie_keepalives",
        "schedule": 15 * 60,
        "options": {"queue": HERMES_MAINTENANCE_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "flow_account_auto_reauthorization",
    {
        "task": "hermes_agent.reconcile_flow_auto_reauth",
        "schedule": 2 * 60,
        "options": {"queue": HERMES_MAINTENANCE_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "ai_provider_model_discovery",
    {
        "task": "ai_provider.discover_all",
        "schedule": 6 * 60 * 60,
        "options": {"queue": default_queue_name},
    },
)
beat_schedule.setdefault(
    "tiktok_shop_fast_syncs",
    {
        "task": "tiktok_shop.dispatch_fast_syncs",
        "schedule": int(getattr(settings, "TT_SHOP_FAST_SYNC_INTERVAL_SECONDS", 5 * 60)),
        "options": {"queue": TIKTOK_SHOP_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "tiktok_shop_catalog_syncs",
    {
        "task": "tiktok_shop.dispatch_catalog_syncs",
        "schedule": int(getattr(settings, "TT_SHOP_CATALOG_SYNC_INTERVAL_SECONDS", 15 * 60)),
        "options": {"queue": TIKTOK_SHOP_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "tiktok_shop_finance_syncs",
    {
        "task": "tiktok_shop.dispatch_finance_syncs",
        "schedule": int(getattr(settings, "TT_SHOP_FINANCE_SYNC_INTERVAL_SECONDS", 60 * 60)),
        "options": {"queue": TIKTOK_SHOP_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "tiktok_shop_refresh_tokens",
    {
        "task": "tiktok_shop.refresh_tokens",
        "schedule": int(getattr(settings, "TT_SHOP_TOKEN_REFRESH_INTERVAL_SECONDS", 6 * 60 * 60)),
        "options": {"queue": TIKTOK_SHOP_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "tiktok_shop_flash_sale_reconciliation",
    {
        "task": "tiktok_shop.reconcile_flash_sales",
        "schedule": int(
            getattr(
                settings,
                "TT_SHOP_FLASH_SALE_AUTOMATION_INTERVAL_SECONDS",
                15 * 60,
            )
        ),
        "options": {"queue": TIKTOK_SHOP_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "website_ads_monitor_cycle",
    {
        "task": "website_ads.monitor_cycle",
        "schedule": int(getattr(settings, "WEBSITE_ADS_MONITOR_INTERVAL_SECONDS", 180)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "website_ads_daily_report_cycle",
    {
        "task": "website_ads.daily_report_cycle",
        "schedule": int(getattr(settings, "WEBSITE_ADS_DAILY_REPORT_INTERVAL_SECONDS", 10 * 60)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "website_ads_asset_library_cycle",
    {
        "task": "website_ads.asset_library_cycle",
        "schedule": int(getattr(settings, "WEBSITE_ADS_ASSET_SYNC_INTERVAL_SECONDS", 10 * 60)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "website_ads_targeting_catalog_sync",
    {
        "task": "website_ads.targeting_catalog_sync",
        "schedule": int(getattr(settings, "WEBSITE_ADS_TARGETING_CATALOG_SYNC_INTERVAL_SECONDS", 24 * 60 * 60)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "website_ads_asset_media_cache_dispatch",
    {
        "task": "website_ads.asset_media_cache_dispatch",
        "schedule": int(getattr(settings, "WEBSITE_ADS_MEDIA_CACHE_INTERVAL_SECONDS", 2 * 60)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "gmvmax_creative_asset_media_cache_dispatch",
    {
        "task": "gmvmax.creative_asset_media_cache_dispatch",
        "schedule": int(getattr(settings, "GMVMAX_MEDIA_CACHE_INTERVAL_SECONDS", 2 * 60)),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "website_ads_asset_analysis_dispatch",
    {
        "task": "website_ads.asset_analysis_dispatch",
        "schedule": int(getattr(settings, "WEBSITE_ADS_ASSET_ANALYSIS_INTERVAL_SECONDS", 2 * 60)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "website_ads_asset_expansion_cycle",
    {
        "task": "website_ads.asset_expansion_cycle",
        "schedule": int(getattr(settings, "WEBSITE_ADS_ASSET_EXPANSION_INTERVAL_SECONDS", 5 * 60)),
        "options": {"queue": WEBSITE_ADS_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "gmvmax_creative_heating_cycle",
    {
        "task": "gmvmax.creative_heating_cycle",
        "schedule": int(getattr(settings, "GMVMAX_HEATING_CYCLE_INTERVAL", 15 * 60)),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_smart_guard_cycle",
    {
        "task": "gmvmax.smart_guard_cycle",
        "schedule": int(getattr(settings, "GMVMAX_SMART_GUARD_INTERVAL", 60)),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_creative_guard_cycle",
    {
        "task": "gmvmax.creative_guard_cycle",
        "schedule": int(getattr(settings, "GMVMAX_CREATIVE_GUARD_INTERVAL", 60)),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_hermes_advisor_cycle",
    {
        "task": "gmvmax.hermes_advisor_cycle",
        "schedule": int(getattr(settings, "GMVMAX_HERMES_ADVISOR_INTERVAL", 10 * 60)),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_hermes_daily_report",
    {
        "task": "gmvmax.hermes_daily_report",
        "schedule": crontab(
            minute=int(getattr(settings, "GMVMAX_HERMES_DAILY_REPORT_LOCAL_MINUTE", 30)),
        ),
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_creative_metrics_10min",
    {
        "task": "gmvmax.sync_creative_metrics_10min",
        "schedule": int(getattr(settings, "GMVMAX_CREATIVE_METRICS_10MIN_INTERVAL", 60)),
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
    "doubao_provider_auth_probe",
    {
        "task": "doubao_provider.dispatch_auth_probes",
        "schedule": 15 * 60,
        "options": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "doubao_provider_capability_probe",
    {
        "task": "doubao_provider.dispatch_capability_probes",
        "schedule": int(
            settings.DOUBAO_CAPABILITY_PROBE_DISPATCH_INTERVAL_SECONDS
        ),
        "options": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "ai_video_recover_stale_result_downloads",
    {
        "task": "ai_video.result.recover_stale_downloads",
        "schedule": 5 * 60,
        "options": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "ai_video_recover_stale_polling",
    {
        "task": "ai_video.video.recover_stale_polling",
        "schedule": 5 * 60,
        "options": {"queue": AI_VIDEO_MAINTENANCE_TASK_QUEUE},
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
beat_schedule.setdefault(
    "openai_whisper_recover_content_producer_references",
    {
        "task": "openai_whisper.recover_content_producer_reference_analyses",
        "schedule": 60,
        "options": {"queue": WHISPER_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "hermes_content_factory_self_heal",
    {
        "task": "hermes_content_factory.self_heal",
        "schedule": 60,
        "options": {"queue": HERMES_AGENT_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "hermes_content_factory_runtime_outbox_reconciliation",
    {
        "task": "hermes_content_factory.reconcile_runtime_ledger",
        "schedule": 5 * 60,
        "options": {"queue": HERMES_AGENT_TASK_QUEUE},
    },
)
beat_schedule.setdefault(
    "gmvmax_account_sync_dispatch",
    {
        "task": "gmvmax.dispatch_account_syncs",
        "schedule": 60,
        "options": {"queue": "gmvmax"},
    },
)
beat_schedule.setdefault(
    "gmvmax_recover_campaign_pause_intents",
    {
        "task": "gmvmax.recover_campaign_pause_intents",
        "schedule": 30,
        "options": {"queue": "gmvmax_control"},
    },
)
celery_app.conf.beat_schedule = beat_schedule

GMVMAX_SYNC_INTERVAL_OPTIONS = (10, 15, 20, 30)

_CORE_TASK_MODULES = (
    "app.tasks.ai_provider_tasks",
    "app.tasks.oauth_tasks",
    "app.tasks.tiktok_shop_tasks",
    "app.tasks.tiktok_shop_video_analysis_tasks",
    "app.tasks.ttb_sync_tasks",
    "app.tasks.ai_video.result_download_tasks",
    "app.tasks.ai_video.video_tasks",
    "app.tasks.globalaiopc.video_tasks",
    "app.tasks.jimeng_lab_tasks",
    "app.tasks.doubao_lab_tasks",
    "app.tasks.ttb_gmvmax_tasks",
    "app.tasks.website_ads_tasks",
    "app.tasks.hermes_agent.tasks",
    "app.tasks.hermes_agent.content_runtime_tasks",
    "app.tasks.hermes_agent.content_factory_tasks",
    "app.gmvmax.tasks_sync",
)
_TIKTOK_SHOP_TASK_MODULE = "app.tasks.tiktok_shop_tasks"
_VIDEO_ANALYSIS_TASK_MODULE = "app.tasks.tiktok_shop_video_analysis_tasks"
_HERMES_TASK_MODULES = (
    "app.tasks.hermes_agent.tasks",
    "app.tasks.hermes_agent.content_runtime_tasks",
    "app.tasks.hermes_agent.content_factory_tasks",
)
_HERMES_MAINTENANCE_TASK_MODULES = ("app.tasks.hermes_agent.tasks",)
_AI_VIDEO_PRODUCTION_TASK_MODULES = (
    "app.tasks.ai_video.result_download_tasks",
    "app.tasks.ai_video.video_tasks",
    "app.tasks.globalaiopc.video_tasks",
)
_AI_VIDEO_DOWNLOAD_TASK_MODULES = ("app.tasks.ai_video.result_download_tasks",)
_AI_VIDEO_MAINTENANCE_TASK_MODULES = (
    "app.tasks.ai_video.result_download_tasks",
    "app.tasks.ai_video.video_tasks",
    "app.tasks.jimeng_lab_tasks",
    "app.tasks.doubao_lab_tasks",
)
_WHISPER_TASK_MODULE = "app.features.tenants.openai_whisper.tasks"
_VIDEO_TRANSCRIPT_TASK_MODULE = "app.tasks.tiktok_shop_video_transcript_tasks"
_WHISPER_TASK_MODULES = (_WHISPER_TASK_MODULE, _VIDEO_TRANSCRIPT_TASK_MODULE)


def task_modules_for_worker_queue(worker_queue: str | None) -> tuple[str, ...]:
    """Return the smallest task registry needed by one queue-owned worker.

    An empty queue means API, Beat, tests, or an unspecialized worker and keeps
    the complete registry for backwards compatibility. Queue names themselves
    remain deployment configuration, not campaign behavior.
    """

    queue = str(worker_queue or "").strip()
    hermes_queue = str(HERMES_AGENT_TASK_QUEUE)
    if queue == hermes_queue or queue.startswith(f"{hermes_queue}.slot"):
        return _HERMES_TASK_MODULES
    if queue == str(HERMES_MAINTENANCE_TASK_QUEUE):
        return _HERMES_MAINTENANCE_TASK_MODULES
    if queue in {
        str(AI_VIDEO_API_TASK_QUEUE),
        str(AI_VIDEO_BROWSER_TASK_QUEUE),
        str(AI_VIDEO_BROWSER_POLL_TASK_QUEUE),
    }:
        return _AI_VIDEO_PRODUCTION_TASK_MODULES
    if queue == str(AI_VIDEO_DOWNLOAD_TASK_QUEUE):
        return _AI_VIDEO_DOWNLOAD_TASK_MODULES
    if queue == str(AI_VIDEO_MAINTENANCE_TASK_QUEUE):
        return _AI_VIDEO_MAINTENANCE_TASK_MODULES
    if queue == str(WHISPER_TASK_QUEUE):
        return _WHISPER_TASK_MODULES
    if queue == TIKTOK_SHOP_TASK_QUEUE:
        return (_TIKTOK_SHOP_TASK_MODULE,)
    if queue == str(VIDEO_ANALYSIS_TASK_QUEUE):
        return (_VIDEO_ANALYSIS_TASK_MODULE,)
    return _CORE_TASK_MODULES + _WHISPER_TASK_MODULES


def task_modules_for_runtime(
    worker_queue: str | None,
    runtime_role: str | None,
) -> tuple[str, ...]:
    """Avoid consumer registries in API producers and the Beat scheduler."""
    role = str(runtime_role or "").strip().lower()
    if role in {"producer", "beat", "scheduler"}:
        return ()
    return task_modules_for_worker_queue(worker_queue)


def _register_task_modules(
    worker_queue: str | None,
    runtime_role: str | None,
) -> None:
    modules = task_modules_for_runtime(worker_queue, runtime_role)
    for module_name in modules:
        if module_name != _WHISPER_TASK_MODULE:
            importlib.import_module(module_name)
            continue
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == "yt_dlp":
                logging.getLogger(__name__).warning(
                    "skip registering Whisper tasks: missing optional dependency %s",
                    exc.name,
                )
                continue
            raise


_register_task_modules(
    os.getenv("GMV_CELERY_WORKER_QUEUE"),
    os.getenv("GMV_CELERY_RUNTIME_ROLE"),
)
