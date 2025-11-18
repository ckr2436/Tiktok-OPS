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
from app.features.tenants.openai_whisper import tasks as openai_whisper_tasks  # noqa: F401

