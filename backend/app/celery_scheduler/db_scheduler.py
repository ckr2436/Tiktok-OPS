# app/celery_scheduler/db_scheduler.py
from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from croniter import croniter
from zoneinfo import ZoneInfo

from celery.beat import Scheduler, ScheduleEntry
from celery.schedules import schedule as CelerySchedule
from celery import uuid as celery_uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.data.db import SessionLocal
from app.data.models.scheduling import Schedule, TaskCatalog, ScheduleRun
from app.core.config import settings
from app.celery_app import celery_app

logger = logging.getLogger("gmv.beat")

MIN_INTERVAL = int(getattr(settings, "SCHEDULE_MIN_INTERVAL_SECONDS", 60))
DB_REFRESH_SECS = int(getattr(settings, "CELERY_BEAT_DB_REFRESH_SECS", 15))
BATCH_LIMIT = 500  # 一次扫描的计划数量上限


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _idempotency_key(task_name: str, workspace_id: int, scheduled_for: datetime, params: dict | None) -> str:
    base = f"{task_name}|{workspace_id}|{int(scheduled_for.timestamp())}|{params or ''}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:64]


def _calc_next_fire(row: Schedule, start: datetime) -> datetime | None:
    tz = ZoneInfo(row.timezone or "UTC")
    start_local = start.astimezone(tz)

    if row.schedule_type == "interval":
        if not row.interval_seconds or row.interval_seconds < MIN_INTERVAL:
            return None
        return (start_local + timedelta(seconds=row.interval_seconds)).astimezone(timezone.utc)

    if row.schedule_type == "crontab":
        if not row.crontab_expr:
            return None
        itr = croniter(row.crontab_expr, start_local)
        nxt = itr.get_next(datetime)
        return nxt.astimezone(timezone.utc)

    if row.schedule_type == "oneoff":
        return None  # oneoff 触发后不再计算，这里返回 None

    return None


class DBScheduleEntry(ScheduleEntry):
    """轻量占位；真实调度在 DB 里，Celery 仅需要个壳。"""

    def __init__(
        self,
        name: str,
        task: str,
        schedule: CelerySchedule,
        args=None,
        kwargs=None,
        options=None,
        last_run_at=None,
        total_run_count=None,
    ):
        super().__init__(
            name,
            task,
            schedule,
            args=args or (),
            kwargs=kwargs or {},
            options=options or {},
            last_run_at=last_run_at,
            total_run_count=total_run_count,
        )

    def is_due(self):
        # 我们不依赖 Celery 的 due 计算，这里保持默认行为
        return self.schedule.is_due(self.last_run_at)


class DBScheduler(Scheduler):
    """
    从数据库拉取计划，根据 next_fire_at/类型决定触发，入队并记录 schedule_runs。

    重要：不要改写 Celery 的 self._last_sync（它是 float 单调时钟）。
    本类用 self._last_db_refresh（float, monotonic）做自己的刷新节流，避免
    触发 “float - datetime” 的类型错误。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 自己的刷新时间戳（使用单调时钟）
        self._last_db_refresh: float = 0.0

    @property
    def schedule(self) -> dict[str, DBScheduleEntry]:
        # 返回一个哑 schedule，Celery 需要一个 dict，但我们不使用它的触发逻辑
        return {}

    def tick(self) -> float:
        # 按需刷新 DB（用单调时钟控制频率）
        now_mono = time.monotonic()
        if (now_mono - self._last_db_refresh) >= DB_REFRESH_SECS:
            try:
                self._sync_and_fire(_now_utc())
            except Exception:  # noqa: BLE001
                logger.exception("DB beat sync failed")
            finally:
                self._last_db_refresh = now_mono

        # 返回下次 tick 的最大等待秒数
        return min(DB_REFRESH_SECS, 5)

    # ---- 核心逻辑：扫描可触发计划并入队 ----
    def _sync_and_fire(self, now_utc: datetime) -> None:
        with SessionLocal() as db:
            # 仅扫描有效计划（目录启用 & 计划启用）
            q = (
                select(Schedule)
                .join(TaskCatalog, Schedule.task_name == TaskCatalog.task_name)
                .where(
                    TaskCatalog.is_enabled.is_(True),
                    Schedule.enabled.is_(True),
                )
                .limit(BATCH_LIMIT)
            )
            rows: list[Schedule] = db.execute(q).scalars().all()

            for row in rows:
                try:
                    self._handle_row(db, row, now_utc)
                except Exception:
                    logger.exception("beat handle schedule failed id=%s", row.id)

            db.commit()

    def _handle_row(self, db: Session, row: Schedule, now_utc: datetime) -> None:
        tz = ZoneInfo(row.timezone or "UTC")
        mis_grace = int(row.misfire_grace_s or 0)
        jitter = int(row.jitter_s or 0)

        # 计算“本次应触发的时刻”
        if row.schedule_type == "oneoff":
            fire_at = row.oneoff_run_at
        else:
            fire_at = row.next_fire_at

        # 首次没有 next_fire_at 时，初始化一次（使其尽快触发/对齐最近周期）
        if not fire_at:
            if row.schedule_type == "interval":
                # 立即触发一次
                fire_at = now_utc
            elif row.schedule_type == "crontab":
                itr = croniter(row.crontab_expr or "* * * * *", now_utc.astimezone(tz))
                fire_at = itr.get_next(datetime).astimezone(timezone.utc)
            elif row.schedule_type == "oneoff":
                fire_at = row.oneoff_run_at

        if not fire_at:
            # 不可触发，写 next 再走
            next_fire = _calc_next_fire(row, now_utc)
            db.execute(update(Schedule).where(Schedule.id == row.id).values(next_fire_at=next_fire))
            return

        # 误触发判断（宕机补偿窗口）
        if mis_grace > 0 and fire_at < (now_utc - timedelta(seconds=mis_grace)):
            # 超过容忍窗口，跳过这个触发窗口，推进 next
            next_fire = _calc_next_fire(row, fire_at)
            db.execute(update(Schedule).where(Schedule.id == row.id).values(next_fire_at=next_fire))
            self._append_run(db, row, fire_at, status="skipped", reason="misfire_exceeded")
            return

        if fire_at > now_utc:
            # 未到触发点，稍后再说
            return

        # 抖动（削峰）
        if jitter > 0:
            delay = random.randint(0, jitter)
            fire_effective = now_utc + timedelta(seconds=delay)
        else:
            fire_effective = now_utc

        # 幂等键
        idem = _idempotency_key(row.task_name, int(row.workspace_id), fire_at, row.params_json)

        # 入队 Celery
        payload = {
            "workspace_id": int(row.workspace_id),
            "schedule_id": int(row.id),
            "idempotency_key": idem,
            "params": row.params_json or {},
        }
        task_name = row.task_name  # 目录中的标准任务名
        # 选择队列：目录默认队列 > 全局默认
        queue = (row.catalog.default_queue if getattr(row, "catalog", None) else None) or settings.CELERY_TASK_DEFAULT_QUEUE

        task_id = celery_uuid()
        r = celery_app.send_task(
            task_name,
            args=(),
            kwargs=payload,
            queue=queue,
            task_id=task_id,
            countdown=max(0, (fire_effective - now_utc).total_seconds()),
        )

        # 记录 run
        self._append_run(db, row, fire_at, status="enqueued", broker_msg_id=str(r.id), idem=idem)

        # 推进 next_fire_at（interval/crontab）；oneoff 则清空并禁用
        if row.schedule_type == "oneoff":
            db.execute(
                update(Schedule)
                .where(Schedule.id == row.id)
                .values(next_fire_at=None, enabled=False)  # oneoff 触发后自动停用
            )
        else:
            next_fire = _calc_next_fire(row, fire_at)
            db.execute(update(Schedule).where(Schedule.id == row.id).values(next_fire_at=next_fire))

    def _append_run(
        self,
        db: Session,
        row: Schedule,
        scheduled_for: datetime,
        status: str,
        broker_msg_id: str | None = None,
        idem: str | None = None,
        reason: str | None = None,
    ):
        run = ScheduleRun(
            schedule_id=int(row.id),
            workspace_id=int(row.workspace_id),
            scheduled_for=scheduled_for,
            enqueued_at=_now_utc() if status == "enqueued" else None,
            broker_msg_id=broker_msg_id,
            status=status,
            duration_ms=None,
            error_code=reason,
            error_message=None,
            idempotency_key=idem
            or _idempotency_key(row.task_name, int(row.workspace_id), scheduled_for, row.params_json),
        )
        db.add(run)

