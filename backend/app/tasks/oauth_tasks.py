# app/tasks/oauth_tasks.py
from __future__ import annotations

import time
from typing import Any
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.oauth_ttb import OAuthAccountTTB
from app.data.models.scheduling import ScheduleRun


def _finish(
    db: Session,
    idem: str,
    broker_id: str | None,
    status: str,
    dur_ms: int | None = None,
    err: str | None = None,
) -> None:
    """
    将最近一次匹配该幂等键的 run 更新为最终状态。
    （也可使用 schedule_id + scheduled_for 复合键，这里用幂等键简化。）
    """
    row = db.execute(
        select(ScheduleRun).where(ScheduleRun.idempotency_key == idem).order_by(ScheduleRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row:
        row.status = status
        row.duration_ms = dur_ms
        row.error_code = err
        db.add(row)


@shared_task(name="tenant.oauth.health_check", bind=True, max_retries=0)
def tenant_oauth_health_check(
    self,
    *,
    workspace_id: int,
    schedule_id: int,
    idempotency_key: str,
    params: dict[str, Any] | None = None,
):
    """
    健康检查：仅检查该租户是否至少存在一个 active 的 TikTok Business 账号。
    由 DB-Beat 调度器触发，带有 schedule_id / idempotency_key。
    """
    ts0 = time.perf_counter_ns()
    with SessionLocal() as db:
        try:
            exists = db.scalar(
                select(OAuthAccountTTB.id).where(
                    OAuthAccountTTB.workspace_id == int(workspace_id),
                    OAuthAccountTTB.status == "active",
                ).limit(1)
            )
            ok = bool(exists)
            dur_ms = int((time.perf_counter_ns() - ts0) / 1_000_000)
            _finish(db, idempotency_key, broker_id=self.request.id, status="success", dur_ms=dur_ms)
            db.commit()
            return {"ok": ok}
        except Exception as e:  # noqa: BLE001
            dur_ms = int((time.perf_counter_ns() - ts0) / 1_000_000)
            _finish(db, idempotency_key, broker_id=self.request.id, status="failed", dur_ms=dur_ms, err=str(e)[:64])
            db.commit()
            raise

