from __future__ import annotations

from typing import Any

from app.core.config import settings


AI_VIDEO_API_TASK_QUEUE = str(settings.AI_VIDEO_API_TASK_QUEUE)
AI_VIDEO_BROWSER_TASK_QUEUE = str(settings.AI_VIDEO_BROWSER_TASK_QUEUE)
AI_VIDEO_BROWSER_POLL_TASK_QUEUE = str(settings.AI_VIDEO_BROWSER_POLL_TASK_QUEUE)
AI_VIDEO_DOWNLOAD_TASK_QUEUE = str(settings.AI_VIDEO_DOWNLOAD_TASK_QUEUE)
AI_VIDEO_MAINTENANCE_TASK_QUEUE = str(settings.AI_VIDEO_MAINTENANCE_TASK_QUEUE)
HERMES_MAINTENANCE_TASK_QUEUE = str(settings.HERMES_MAINTENANCE_TASK_QUEUE)

DOUBAO_PROVIDER_KEY = "doubao"


def _task_local_meta(task: Any) -> dict[str, Any]:
    result = dict(getattr(task, "result_json", None) or {})
    local = result.get("__local")
    return dict(local) if isinstance(local, dict) else {}


def active_provider_for_queue(task: Any) -> str:
    """Return the durable provider route without importing worker task graphs."""

    params = dict(getattr(task, "input_json", None) or {})
    routing_mode = str(params.get("routing_mode") or "").strip().lower()
    if routing_mode == "pinned":
        requested = str(params.get("requested_service_provider") or "").strip().lower()
        if requested and requested != "auto":
            return requested
    service_provider = str(params.get("service_provider") or "").strip().lower()
    if service_provider and service_provider != "auto":
        return service_provider
    return str(_task_local_meta(task).get("active_provider") or "").strip().lower()


def production_video_queue(task: Any) -> str:
    """Route browser-backed generation away from ordinary provider API work."""

    if active_provider_for_queue(task) == DOUBAO_PROVIDER_KEY:
        return AI_VIDEO_BROWSER_TASK_QUEUE
    return AI_VIDEO_API_TASK_QUEUE


def polling_video_queue(task: Any) -> str:
    """Keep browser result polling from delaying fresh paid submissions."""

    if active_provider_for_queue(task) == DOUBAO_PROVIDER_KEY:
        return AI_VIDEO_BROWSER_POLL_TASK_QUEUE
    return production_video_queue(task)
