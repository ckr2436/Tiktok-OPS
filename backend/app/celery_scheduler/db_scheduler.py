# backend/app/celery_scheduler/db_scheduler.py
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from croniter import croniter
from zoneinfo import ZoneInfo

from celery.beat import Scheduler, ScheduleEntry
from celery.schedules import schedule as CelerySchedule, maybe_schedule
from celery import uuid as celery_uuid

from sqlalchemy import select, update, and_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.data.db import SessionLocal
from app.data.models.scheduling import Schedule, TaskCatalog, ScheduleRun
from app.core.config import settings
from app.celery_app import celery_app

logger = logging.getLogger("gmv.beat")

MIN_INTERVAL = int(getattr(settings, "SCHEDULE_MIN_INTERVAL_SECONDS", 60))
DB_REFRESH_SECS = int(getattr(settings, "CELERY_BEAT_DB_REFRESH_SECS", 15))
BATCH_LIMIT = 500  # 一次扫描的计划数量上限

_GMVMAX_TASKS = {
    "gmvmax.sync_campaigns",
    "gmvmax.sync_creative_metrics_10min",
    "gmvmax.sync_creative_metrics_10min_for_campaign",
    "gmvmax.creative_heating_cycle",
}


def _isoformat(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _build_gmvmax_kwargs(row: Schedule, idem: str, now_utc: datetime) -> dict:
    params = dict(row.params_json or {})
    kwargs = dict(params)
    kwargs.setdefault("workspace_id", int(row.workspace_id))
    kwargs.setdefault("schedule_id", int(row.id))
    kwargs.setdefault("idempotency_key", idem)

    task_name = row.task_name

    if task_name == "gmvmax.sync_campaigns":
        kwargs.setdefault("filters", params.get("filters") or {})

    return kwargs


def _build_ttb_sync_kwargs(row: Schedule, idem: str) -> dict:
    params = dict(row.params_json or {})
    raw_envelope = params.get("envelope")
    envelope = dict(raw_envelope) if isinstance(raw_envelope, dict) else {}
    envelope.setdefault("workspace_id", int(params.get("workspace_id") or row.workspace_id))
    envelope.setdefault("auth_id", params.get("auth_id"))
    envelope.setdefault("scope", params.get("scope") or row.task_name.rsplit(".", 1)[-1])
    envelope.setdefault("provider", params.get("provider") or "tiktok-business")
    envelope.setdefault("options", dict(params.get("options") or {}))
    envelope.setdefault("envelope_version", 1)
    meta = dict(envelope.get("meta") or {})
    meta.setdefault("schedule_id", int(row.id))
    meta.setdefault("idempotency_key", idem)
    envelope["meta"] = meta
    return {
        "workspace_id": int(envelope["workspace_id"]),
        "auth_id": int(envelope["auth_id"]),
        "scope": str(envelope["scope"]),
        "params": {"envelope": envelope},
        "idempotency_key": idem,
    }


def _now_utc() -> datetime:
    # 统一返回「UTC aware」时间
    return datetime.now(timezone.utc)


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """
    把任意 datetime 统一转成「UTC 的 naive datetime」（tzinfo=None）：

    - 如果原本有 tzinfo，就先转成 UTC，再去掉 tzinfo；
    - 如果原本没有 tzinfo，就直接当作 UTC。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _idempotency_key(
    task_name: str,
    workspace_id: int,
    scheduled_for: datetime,
    params: dict | None,
) -> str:
    base = f"{task_name}|{workspace_id}|{int(scheduled_for.timestamp())}|{params or ''}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:64]


def _calc_next_fire(row: Schedule, start: datetime) -> datetime | None:
    """
    计算下次触发时间，返回的是「UTC aware」时间（tzinfo=UTC）。
    """
    tz = ZoneInfo(row.timezone or "UTC")
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    # 这里用 aware → aware 的转换逻辑
    start_local = start.astimezone(tz)

    if row.schedule_type == "interval":
        if not row.interval_seconds or row.interval_seconds < MIN_INTERVAL:
            return None
        return (start_local + timedelta(seconds=row.interval_seconds)).astimezone(
            timezone.utc,
        )

    if row.schedule_type == "crontab":
        if not row.crontab_expr:
            return None
        itr = croniter(row.crontab_expr, start_local)
        nxt = itr.get_next(datetime)
        return nxt.astimezone(timezone.utc)

    if row.schedule_type == "oneoff":
        return None  # oneoff 触发后不再计算

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
        # 兼容 app.conf.beat_schedule 的静态计划（DB 以外的固定节拍）
        self._static_entries: dict[str, ScheduleEntry] = {}
        self._static_fingerprint: str | None = None
        self._reload_static_entries(force=True)

    def _beat_schedule_fingerprint(self, schedule_conf: dict) -> str:
        try:
            dumped = json.dumps(schedule_conf, sort_keys=True, default=str)
        except TypeError:
            dumped = repr(schedule_conf)
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()

    def _reload_static_entries(self, *, force: bool = False) -> None:
        schedule_conf = getattr(self.app.conf, "beat_schedule", {}) or {}
        fingerprint = self._beat_schedule_fingerprint(schedule_conf)
        if not force and fingerprint == self._static_fingerprint:
            return

        self._static_entries = self._load_static_entries(schedule_conf)
        self._static_fingerprint = fingerprint

    def _load_static_entries(self, schedule_conf: dict) -> dict[str, ScheduleEntry]:
        entries: dict[str, ScheduleEntry] = {}

        for name, payload in schedule_conf.items():
            try:
                entries[name] = self.Entry(
                    name,
                    task=payload["task"],
                    schedule=maybe_schedule(payload["schedule"]),
                    args=payload.get("args", ()),
                    kwargs=payload.get("kwargs", {}),
                    options=payload.get("options", {}),
                    last_run_at=payload.get("last_run_at"),
                    total_run_count=payload.get("total_run_count", 0),
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to load static beat entry", extra={"name": name})

        return entries

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

        static_wait = self._tick_static()

        # 返回下次 tick 的最大等待秒数
        base_wait = min(DB_REFRESH_SECS, 5)
        if static_wait is None:
            return base_wait
        return min(base_wait, static_wait)

    def _tick_static(self) -> float | None:
        """调度 app.conf.beat_schedule 中配置的固定任务。

        返回值为下一次静态计划的等待秒数；若无静态计划则返回 None。
        """

        self._reload_static_entries()

        if not self._static_entries:
            return None

        next_due: list[float] = []
        for name, entry in list(self._static_entries.items()):
            is_due, next_call_in = entry.is_due()
            if is_due:
                try:
                    self.apply_entry(entry, producer=self.producer)
                except Exception:  # noqa: BLE001
                    logger.exception("static beat task failed", extra={"name": name, "task": entry.task})
                self._static_entries[name] = entry.next()

            if isinstance(next_call_in, (int, float)):
                next_due.append(float(next_call_in))

        return min(next_due) if next_due else None

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

    def _already_enqueued(self, db: Session, row: Schedule, idem: str) -> bool:
        """
        幂等去重：同一 schedule + idempotency_key 若已存在有效 run，则避免重复入队。
        将以下状态视为“已消耗/在途”：enqueued / running / success / partial
        """
        stmt = (
            select(ScheduleRun.id, ScheduleRun.status)
            .where(
                and_(
                    ScheduleRun.schedule_id == int(row.id),
                    ScheduleRun.idempotency_key == idem,
                )
            )
            .order_by(ScheduleRun.id.desc())
            .limit(1)
        )
        rec = db.execute(stmt).first()
        if not rec:
            return False
        status = rec[1]
        return status in ("enqueued", "running", "success", "partial")

    def _handle_row(self, db: Session, row: Schedule, now_utc: datetime) -> None:
        tz = ZoneInfo(row.timezone or "UTC")
        mis_grace = int(row.misfire_grace_s or 0)
        jitter = int(row.jitter_s or 0)

        # 对 interval 计划做基础校验，避免 interval_seconds < MIN_INTERVAL 时陷入高频触发
        if row.schedule_type == "interval" and (
            not row.interval_seconds or row.interval_seconds < MIN_INTERVAL
        ):
            logger.warning(
                "schedule interval_seconds below MIN_INTERVAL; disabling to avoid tight loop",
                extra={
                    "schedule_id": int(row.id),
                    "interval_seconds": row.interval_seconds,
                    "min_interval": MIN_INTERVAL,
                },
            )
            db.execute(
                update(Schedule)
                .where(Schedule.id == row.id)
                .values(next_fire_at=None, enabled=False)
            )
            return

        # 计算“本次应触发的时刻”（fire_at 使用 aware UTC 或 DB 原值）
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
            db.execute(
                update(Schedule)
                .where(Schedule.id == row.id)
                .values(next_fire_at=next_fire),
            )
            return

        # ---- 统一时间类型：全部转成「UTC naive」用于比较 ----
        fire_at_cmp = _to_naive_utc(fire_at)
        now_cmp = _to_naive_utc(now_utc)

        if fire_at_cmp is None or now_cmp is None:
            # 理论上不会发生，兜底防御
            return

        # 误触发判断（宕机补偿窗口）
        if mis_grace > 0 and fire_at_cmp < (
            now_cmp - timedelta(seconds=mis_grace)
        ):
            # 超过容忍窗口，跳过这个触发窗口，推进 next
            if row.schedule_type == "interval":
                interval_seconds = int(row.interval_seconds or 0)
                elapsed_seconds = max(0, int((now_cmp - fire_at_cmp).total_seconds()))
                steps = (elapsed_seconds // interval_seconds) + 1
                next_fire = (
                    fire_at_cmp.replace(tzinfo=timezone.utc)
                    + timedelta(seconds=steps * interval_seconds)
                )
                db.execute(
                    update(Schedule)
                    .where(Schedule.id == row.id)
                    .values(next_fire_at=next_fire),
                )
            elif row.schedule_type == "crontab":
                next_fire = _calc_next_fire(row, now_utc)
                db.execute(
                    update(Schedule)
                    .where(Schedule.id == row.id)
                    .values(next_fire_at=next_fire),
                )
            else:
                db.execute(
                    update(Schedule)
                    .where(Schedule.id == row.id)
                    .values(next_fire_at=None, enabled=False),
                )
            self._append_run(
                db,
                row,
                fire_at,
                status="failed",
                reason="misfire_exceeded",
            )
            return

        if fire_at_cmp > now_cmp:
            # 未到触发点，稍后再说
            return

        # 抖动（削峰）
        if jitter > 0:
            delay = random.randint(0, jitter)
            fire_effective = now_cmp + timedelta(seconds=delay)
        else:
            fire_effective = now_cmp

        # 幂等键（这里 scheduled_for 仍然用原始 fire_at，保留精度/时区信息）
        idem = _idempotency_key(
            row.task_name,
            int(row.workspace_id),
            fire_at,
            row.params_json,
        )

        # 幂等去重：避免重复入队
        if self._already_enqueued(db, row, idem):
            # 若 next_fire_at 还是过去时间，推进一下避免卡住
            if row.schedule_type != "oneoff":
                next_fire = _calc_next_fire(row, fire_at)
                db.execute(
                    update(Schedule)
                    .where(Schedule.id == row.id)
                    .values(next_fire_at=next_fire),
                )
            return

        # 入队 Celery
        if row.task_name in _GMVMAX_TASKS:
            payload = _build_gmvmax_kwargs(row, idem, now_utc)
        elif row.task_name.startswith("ttb.sync."):
            payload = _build_ttb_sync_kwargs(row, idem)
        else:
            payload = {
                "workspace_id": int(row.workspace_id),
                "schedule_id": int(row.id),
                "idempotency_key": idem,
                "params": row.params_json or {},
            }
        task_name = row.task_name  # 目录中的标准任务名

        # 选择队列：目录默认队列 > 全局默认
        queue = (
            row.catalog.default_queue
            if getattr(row, "catalog", None)
            else None
        ) or settings.CELERY_TASK_DEFAULT_QUEUE

        task_id = celery_uuid()
        self._append_run(
            db,
            row,
            fire_at,
            status="enqueued",
            broker_msg_id=str(task_id),
            idem=idem,
        )
        db.flush()
        # Workers resolve the run by idempotency key. Commit before publishing
        # so a fast worker cannot race an uncommitted ScheduleRun.
        db.commit()
        r = celery_app.send_task(
            task_name,
            args=(),
            kwargs=payload,
            queue=queue,
            task_id=task_id,
            countdown=max(
                0,
                int((fire_effective - now_cmp).total_seconds()),
            ),
        )

        # 记录 run（并发下可能撞唯一约束，需兜底处理）
        try:
            self._append_run(
                db,
                row,
                fire_at,
                status="enqueued",
                broker_msg_id=str(r.id),
                idem=idem,
            )
        except IntegrityError:
            db.rollback()
            # 已有同 (schedule_id, idempotency_key) 的 run 被并发创建；推进 next 即可
            if row.schedule_type != "oneoff":
                next_fire = _calc_next_fire(row, fire_at)
                db.execute(
                    update(Schedule)
                    .where(Schedule.id == row.id)
                    .values(next_fire_at=next_fire),
                )
            logger.info(
                "duplicate schedule_run ignored (unique hit)",
                extra={
                    "schedule_id": int(row.id),
                    "idempotency_key": idem,
                },
            )

        # 推进 next_fire_at（interval/crontab）；oneoff 则清空并禁用
        if row.schedule_type == "oneoff":
            db.execute(
                update(Schedule)
                .where(Schedule.id == row.id)
                .values(
                    next_fire_at=None,
                    enabled=False,  # oneoff 触发后自动停用
                ),
            )
        else:
            next_fire = _calc_next_fire(row, fire_at)
            db.execute(
                update(Schedule)
                .where(Schedule.id == row.id)
                .values(next_fire_at=next_fire),
            )

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
        if idem:
            existing = db.execute(
                select(ScheduleRun)
                .where(
                    ScheduleRun.schedule_id == int(row.id),
                    ScheduleRun.idempotency_key == idem,
                )
                .order_by(ScheduleRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
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
            or _idempotency_key(
                row.task_name,
                int(row.workspace_id),
                scheduled_for,
                row.params_json,
            ),
        )
        db.add(run)
        return run
        # 让调用者决定何时 commit（上层有批量 commit）
