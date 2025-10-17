# scripts/init_task_catalog.py
from __future__ import annotations

import json

from app.services.scheduler_catalog import CATALOG


def main():
    # 仅输出 JSON，供运维查看或二次写入 Beat 配置
    out = []
    for spec in CATALOG:
        out.append({
            "name": spec.name,
            "task": spec.task,
            "crontab": spec.crontab,
            "interval_seconds": spec.interval_seconds,
            "args": spec.args or [],
            "kwargs": spec.kwargs or {},
            "queue": spec.queue,
            "description": spec.description,
        })
    print(json.dumps({"periodic_tasks": out}, indent=2))


if __name__ == "__main__":
    main()

