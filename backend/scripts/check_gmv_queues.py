#!/opt/gmv/python3.13/bin/python

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
os.chdir(BACKEND_ROOT)

from app.celery_app import (
    AI_VIDEO_API_TASK_QUEUE,
    AI_VIDEO_BROWSER_TASK_QUEUE,
    AI_VIDEO_BROWSER_POLL_TASK_QUEUE,
    AI_VIDEO_DOWNLOAD_TASK_QUEUE,
    AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    HERMES_MAINTENANCE_TASK_QUEUE,
    TIKTOK_SHOP_TASK_QUEUE,
    VIDEO_ANALYSIS_TASK_QUEUE,
    WEBSITE_ADS_MEDIA_TASK_QUEUE,
    WEBSITE_ADS_TASK_QUEUE,
    WHISPER_TASK_QUEUE,
    celery_app,
)


REQUIRED_QUEUES = (
    "gmvmax",
    "gmvmax_control",
    "gmvmax_sync",
    "gmv.tasks.hermes_agent",
    AI_VIDEO_API_TASK_QUEUE,
    AI_VIDEO_BROWSER_TASK_QUEUE,
    AI_VIDEO_BROWSER_POLL_TASK_QUEUE,
    AI_VIDEO_DOWNLOAD_TASK_QUEUE,
    AI_VIDEO_MAINTENANCE_TASK_QUEUE,
    HERMES_MAINTENANCE_TASK_QUEUE,
    TIKTOK_SHOP_TASK_QUEUE,
    WEBSITE_ADS_TASK_QUEUE,
    WEBSITE_ADS_MEDIA_TASK_QUEUE,
    VIDEO_ANALYSIS_TASK_QUEUE,
    WHISPER_TASK_QUEUE,
)


def _management_url(broker_port: int | None, host: str) -> str:
    configured = os.getenv("GMV_RABBITMQ_MANAGEMENT_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    management_port = 15679 if broker_port == 5679 else 15672
    return f"http://{host}:{management_port}"


def main() -> int:
    broker = urlsplit(str(celery_app.conf.broker_url or ""))
    if broker.scheme not in {"amqp", "amqps"} or not broker.hostname:
        print(json.dumps({"ok": False, "error": "unsupported_broker"}))
        return 2

    vhost = broker.path.lstrip("/") or "/"
    endpoint = f"{_management_url(broker.port, broker.hostname)}/api/queues/{quote(vhost, safe='')}"
    try:
        response = httpx.get(
            endpoint,
            auth=(broker.username or "", broker.password or ""),
            timeout=10.0,
        )
        response.raise_for_status()
        queues = {str(row.get("name")): row for row in response.json()}
    except httpx.HTTPStatusError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "management_http_error",
                    "status_code": exc.response.status_code,
                    "broker": {
                        "host": broker.hostname,
                        "port": broker.port,
                        "vhost": vhost,
                    },
                }
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 2

    result = {}
    ok = True
    for name in REQUIRED_QUEUES:
        row = queues.get(name) or {}
        consumers = int(row.get("consumers") or 0)
        ready = int(row.get("messages_ready") or 0)
        unacknowledged = int(row.get("messages_unacknowledged") or 0)
        healthy = bool(row) and consumers > 0
        ok = ok and healthy
        result[name] = {
            "healthy": healthy,
            "consumers": consumers,
            "ready": ready,
            "unacknowledged": unacknowledged,
        }

    print(
        json.dumps(
            {
                "ok": ok,
                "broker": {
                    "host": broker.hostname,
                    "port": broker.port,
                    "vhost": vhost,
                },
                "queues": result,
            },
            ensure_ascii=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
