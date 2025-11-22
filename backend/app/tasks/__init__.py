# app/tasks/__init__.py
from __future__ import annotations

"""
使 Celery worker 在导入 app.tasks 包时，显式注册任务模块。
- 你已有 app.tasks.oauth_tasks
- 新增 app.tasks.ttb_sync_tasks
"""

# 注册已有与新增任务模块（导入即完成任务注册）
from . import oauth_tasks  # noqa: F401
from . import ttb_sync_tasks  # noqa: F401
from . import ttb_gmvmax_tasks  # noqa: F401

# Whisper 任务依赖 yt_dlp 等第三方库，缺失时不应阻断其它任务的注册。
import logging


try:
    from app.features.tenants.openai_whisper import tasks as openai_whisper_tasks  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - 环境依赖可选
    if exc.name == "yt_dlp":
        logging.getLogger(__name__).warning(
            "skip registering Whisper tasks: missing optional dependency %s", exc.name
        )
    else:
        raise

