# app/features/platform/router_tasks.py
from __future__ import annotations

import json
import time
import uuid
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
import redis

from app.core.config import settings
from app.core.errors import APIError, RateLimitExceeded
from app.core.deps import SessionUser, require_platform_admin
from app.core.metrics import get_counter, get_histogram
from app.celery_app import celery_app  # ✅ 统一使用 app.celery_app
from celery.result import AsyncResult

# （可选）审计：若不可用不阻塞
try:
    from app.services.audit import log_event  # type: ignore
except Exception:  # pragma: no cover
    log_event = None  # type: ignore


# =========================
# Redis 连接（幂等 & 限流 & 任务元数据）
# =========================
_redis_client: Optional[redis.Redis] = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,  # 存 JSON、字符串更顺手
        )
    return _redis_client


# =========================
# Action 白名单（仅平台暴露“动作”，不暴露 Celery 任务名/队列）
# =========================
@dataclass(frozen=True)
class ActionSpec:
    # 对外动作名（路由变量）
    name: str
    # 内部 Celery 任务名（模块路径）
    task_name: str
    # 任务投递队列
    queue: str
    # 动作域
    domain: Literal["platform", "tenant"] = "platform"
    # 是否需要 workspace（绝大多数需要）
    require_workspace: bool = True
    # 并发粒度
    concurrency_scope: Literal["global", "workspace"] = "workspace"
    # 是否允许被取消
    cancellable: bool = True
    # 每租户并发上限（平台侧约束；None 表示用默认）
    max_concurrency_per_workspace: Optional[int] = None
    # 速率限制（window 秒内 max_calls 次）
    rate_limit_window_sec: int = 60
    rate_limit_max_calls: int = 60


# 只保留当前实际存在的任务：tenant.oauth.health_check
# 其它示例（pull_orders / rebuild_metrics）先删除，避免“未知任务名”。
_ACTIONS: Dict[str, ActionSpec] = {
    "oauth_health_check": ActionSpec(
        name="oauth_health_check",
        task_name="tenant.oauth.health_check",  # ✅ 和 app/tasks/oauth_tasks.py 对齐
        queue=settings.CELERY_TASK_DEFAULT_QUEUE,  # 用你的默认队列
        domain="tenant",
        require_workspace=True,
        concurrency_scope="workspace",
        cancellable=False,
        max_concurrency_per_workspace=1,
        rate_limit_window_sec=30,
        rate_limit_max_calls=10,
    ),
}

_TASK_TRIGGER_COUNTER = get_counter(
    "platform_task_runs_total",
    "Count of platform task trigger attempts",
    labelnames=("task_key", "status"),
)
_TASK_TRIGGER_DURATION = get_histogram(
    "platform_task_run_duration_seconds",
    "Latency of platform task trigger API",
    labelnames=("task_key",),
)
_RATE_LIMIT_COUNTER = get_counter(
    "rate_limit_hits_total",
    "Rate limit hits by scope",
    labelnames=("scope",),
)


def _require_action(action: str) -> ActionSpec:
    spec = _ACTIONS.get(action)
    if not spec:
        raise APIError("INVALID_ARGUMENT", f"Unknown action: {action}", 400)
    return spec


# =========================
# Pydantic Schemas
# =========================
class TriggerRequest(BaseModel):
    workspace_id: Optional[int] = Field(default=None, ge=1)
    args: Dict[str, Any] = Field(default_factory=dict)
    priority: Optional[str] = Field(default="normal", pattern="^(low|normal|high)$")
    delay_seconds: Optional[int] = Field(default=0, ge=0, le=86400)
    dedupe_key: Optional[str] = Field(default=None, max_length=256)


class TriggerResponse(BaseModel):
    task_id: str
    action: str
    workspace_id: Optional[int] = None
    state: str = "ENQUEUED"
    enqueued_at: str
    status_url: str


class TaskStateResponse(BaseModel):
    task_id: str
    action: Optional[str] = None
    workspace_id: Optional[int] = None
    state: str
    attempts: int = 0
    eta: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class CancelResponse(BaseModel):
    task_id: str
    state: str


# =========================
# 工具：时间 & 序列化
# =========================
def _now_utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_iso(dt) -> Optional[str]:
    try:
        if not dt:
            return None
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


# =========================
# 幂等键 & 限流 & 并发 键
# =========================
def _idem_key(action: str, workspace_id: Optional[int], raw_key: str) -> str:
    # 用 SHA1 归一化，避免超长 header 直接拼接
    digest = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()
    wid = str(workspace_id) if workspace_id is not None else "-"
    return f"gmv:tasks:idempotency:{action}:{wid}:{digest}"


def _rate_key(action: str, workspace_id: Optional[int], user_id: int) -> str:
    wid = str(workspace_id) if workspace_id is not None else "-"
    return f"gmv:tasks:ratelimit:{action}:{wid}:user:{user_id}"


def _conc_key(action: str, workspace_id: Optional[int]) -> str:
    wid = str(workspace_id) if workspace_id is not None else "-"
    return f"gmv:tasks:concurrency:{action}:{wid}"


def _meta_key(task_id: str) -> str:
    return f"gmv:tasks:meta:{task_id}"


# =========================
# 权限/上下文解析
# =========================
def _resolve_workspace_id(
    path_workspace_id: Optional[int],
    body_workspace_id: Optional[int],
    me: SessionUser,
    require_workspace: bool,
) -> Optional[int]:
    provided = {k: v for k, v in {
        "path": path_workspace_id,
        "body": body_workspace_id,
        "session": me.workspace_id if require_workspace else None,
    }.items() if v is not None}

    if len(provided) > 1 and len(set(provided.values())) > 1:
        # 多来源冲突
        raise APIError("INVALID_ARGUMENT", "workspace_id conflicted from multiple sources.", 400)

    wid = (
        path_workspace_id
        if path_workspace_id is not None
        else (body_workspace_id if body_workspace_id is not None else (me.workspace_id if require_workspace else None))
    )

    if require_workspace and wid is None:
        raise APIError("INVALID_ARGUMENT", "workspace_id is required for this action.", 400)

    if wid is not None and me.workspace_id != int(wid) and not me.is_platform_admin:
        # 平台管理员可跨租户；普通用户必须属于该租户
        raise APIError("FORBIDDEN", "Not allowed to access this workspace.", 403)

    return wid


# =========================
# 并发控制（每租户每动作）
# =========================
def _check_concurrency(spec: ActionSpec, workspace_id: Optional[int]) -> None:
    max_c = spec.max_concurrency_per_workspace or 3
    scope_id = workspace_id if spec.concurrency_scope == "workspace" else 0

    r = _get_redis()
    key = _conc_key(spec.name, scope_id)
    # 使用计数器 + TTL 作为软并发（硬并发 Worker 侧再控）
    current = r.get(key)
    cur = int(current) if current and str(current).isdigit() else 0
    if cur >= int(max_c):
        raise APIError("CONFLICT", "There is already a running task for this action in the workspace.", 409)


def _inc_concurrency(spec: ActionSpec, workspace_id: Optional[int]) -> None:
    scope_id = workspace_id if spec.concurrency_scope == "workspace" else 0
    r = _get_redis()
    key = _conc_key(spec.name, scope_id)
    # 计数 + 安全 TTL（10分钟无心跳则回落；Worker 结束时会主动减）
    pipe = r.pipeline()
    pipe.incr(key, 1)
    pipe.expire(key, 600)
    pipe.execute()


def _dec_concurrency(spec: ActionSpec, workspace_id: Optional[int]) -> None:
    scope_id = workspace_id if spec.concurrency_scope == "workspace" else 0
    r = _get_redis()
    key = _conc_key(spec.name, scope_id)
    try:
        with r.pipeline() as p:
            p.watch(key)
            raw = p.get(key)
            val = int(raw) if raw and str(raw).isdigit() else 0
            val = max(0, val - 1)
            p.multi()
            if val == 0:
                p.delete(key)
            else:
                p.set(key, val, ex=600)
            p.execute()
    except redis.WatchError:
        # 竞争下忽略
        pass


# =========================
# 速率限制（用户维度 + 租户维度）
# =========================
def _rate_limit(spec: ActionSpec, workspace_id: Optional[int], user_id: int) -> Tuple[int, int, int]:
    """返回 (limit, remaining, reset_ts) 或在命中频控时抛出异常。"""

    r = _get_redis()
    window = int(spec.rate_limit_window_sec)
    limit = int(spec.rate_limit_max_calls)
    key = _rate_key(spec.name, workspace_id if spec.require_workspace else 0, user_id)

    with r.pipeline() as p:
        p.incr(key, 1)
        p.expire(key, window)
        cur, _ = p.execute()

    cur = int(cur)
    ttl = r.ttl(key)
    ttl = int(ttl) if ttl and int(ttl) > 0 else window
    reset_ts = int(time.time()) + ttl
    remaining = max(0, limit - cur)

    if cur > limit:
        _RATE_LIMIT_COUNTER.labels(scope="auth").inc()
        next_allowed = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
        raise RateLimitExceeded(
            "Too many requests.",
            next_allowed_at=next_allowed,
            limit=limit,
            remaining=remaining,
            reset_ts=reset_ts,
        )

    return (limit, remaining, reset_ts)


# =========================
# 幂等
# =========================
def _idempotent_get_or_set(
    action: str,
    workspace_id: Optional[int],
    idempotency_key: str,
    payload_hash: str,
    response_payload_factory: Callable[[], Dict[str, Any]],
    ttl_seconds: int = 24 * 3600,
) -> Dict[str, Any]:
    """缓存幂等响应，并在请求负载不一致时抛出冲突。"""

    r = _get_redis()
    key = _idem_key(action, workspace_id if workspace_id is not None else 0, idempotency_key)
    old = r.get(key)
    if old:
        try:
            data = json.loads(old)
            stored_payload = data.get("payload") if isinstance(data, dict) else None
            stored_hash = data.get("payload_hash") if isinstance(data, dict) else None
            if stored_payload is None and isinstance(data, dict):
                stored_hash = data.get("_payload_hash")
                stored_payload = {k: v for k, v in data.items() if k != "_payload_hash"}
            if stored_hash and stored_hash != payload_hash:
                raise APIError(
                    "IDEMPOTENCY_CONFLICT",
                    "Payload differs for the same Idempotency-Key.",
                    409,
                    data={
                        "task_id": (stored_payload or {}).get("task_id"),
                        "payload_hash": stored_hash,
                    },
                )
            payload = stored_payload or data
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["_idempotent_hit"] = True
                return payload
        except APIError:
            raise
        except Exception:
            # 存档损坏则继续生成
            pass

    new_payload = response_payload_factory()
    stored = {"payload": new_payload, "payload_hash": payload_hash}
    r.set(key, json.dumps(stored, separators=(",", ":"), ensure_ascii=False), ex=ttl_seconds)
    return new_payload


# =========================
# 任务投递
# =========================
def _enqueue_task(
    spec: ActionSpec,
    workspace_id: Optional[int],
    args: Dict[str, Any],
    priority: str,
    delay_seconds: int,
    request_id: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    """
    返回 (task_id, meta)
    """
    # 将 workspace_id 注入任务参数（Worker 再做严格校验）
    payload = dict(args or {})
    if spec.require_workspace:
        payload["workspace_id"] = int(workspace_id)  # 强转，保证类型

    # health_check 任务合同需要的固定参数：schedule_id / idempotency_key / params
    if spec.task_name == "tenant.oauth.health_check":
        payload.setdefault("schedule_id", 0)
        payload.setdefault("idempotency_key", f"manual:{uuid.uuid4()}")
        payload.setdefault("params", None)

    headers = {}
    if request_id:
        headers["x-request-id"] = str(request_id)

    # priority → Celery 的 priority 0..9（可自定义映射）
    pri_map = {"low": 0, "normal": 5, "high": 9}
    pri = pri_map.get(priority, 5)

    options: Dict[str, Any] = {
        "queue": spec.queue,
        "priority": pri,
        "headers": headers or None,
    }
    if delay_seconds and delay_seconds > 0:
        options["countdown"] = int(delay_seconds)

    # 发送任务
    async_res = celery_app.send_task(
        spec.task_name,
        kwargs=payload,
        **options,  # type: ignore[arg-type]
    )
    task_id = async_res.id or str(uuid.uuid4())

    # 元数据存一份（状态查询用）
    r = _get_redis()
    meta = {
        "task_id": task_id,
        "action": spec.name,
        "workspace_id": (int(workspace_id) if workspace_id is not None else None),
        "enqueued_at": _now_utc_iso(),
        "priority": priority,
        "queue": spec.queue,
        "delay_seconds": delay_seconds,
        "request_id": request_id,
    }
    r.set(_meta_key(task_id), json.dumps(meta, separators=(",", ":"), ensure_ascii=False), ex=7 * 24 * 3600)

    # 并发 +1
    _inc_concurrency(spec, workspace_id)

    return task_id, meta


# =========================
# Router
# =========================
router = APIRouter(
    prefix=f"{settings.API_PREFIX}/platform/tasks",
    tags=["Platform / Tasks"],
)


@router.post("/{action}", response_model=TriggerResponse)
@router.post("/{action}/workspaces/{path_workspace_id}", response_model=TriggerResponse)
def trigger_task(
    action: str,
    req: TriggerRequest,
    http: Request,
    response: Response,
    me: SessionUser = Depends(require_platform_admin),
    path_workspace_id: Optional[int] = None,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-ID"),
):
    spec = _require_action(action)
    if spec.domain != "platform":
        raise APIError("FORBIDDEN", "Action not available on the platform domain.", 403)

    request_id = getattr(http.state, "request_id", None) or x_request_id

    start = time.perf_counter()
    status_label = "error"
    wid: Optional[int] = None

    try:
        # workspace 解析与权限
        wid = _resolve_workspace_id(path_workspace_id, req.workspace_id, me, require_workspace=spec.require_workspace)

        # 限流（用户维度 + 租户维度）
        limit, remaining, reset_ts = _rate_limit(spec, wid, me.id)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_ts)
        next_allowed_iso = datetime.fromtimestamp(reset_ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        response.headers["X-Next-Allowed-At"] = next_allowed_iso

        # 并发检测（软限）
        _check_concurrency(spec, wid)

        # 幂等键（强制要求）
        if not idempotency_key:
            raise APIError("INVALID_ARGUMENT", "Missing Idempotency-Key header.", 400)

        payload_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "workspace_id": wid,
                    "args": req.args or {},
                    "priority": req.priority or "normal",
                    "delay_seconds": int(req.delay_seconds or 0),
                    "dedupe_key": req.dedupe_key or None,
                    "action": spec.name,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        def _produce() -> Dict[str, Any]:
            task_id, meta = _enqueue_task(
                spec=spec,
                workspace_id=wid,
                args=req.args or {},
                priority=(req.priority or "normal"),
                delay_seconds=int(req.delay_seconds or 0),
                request_id=request_id,
            )

            if log_event:
                try:
                    args_digest = hashlib.sha1(
                        json.dumps(req.args or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    log_event(  # type: ignore[misc]
                        db=None,
                        action="task.trigger",
                        resource_type="task",
                        resource_id=None,
                        actor_user_id=int(me.id),
                        actor_workspace_id=(int(wid) if wid is not None else None),
                        actor_ip=http.client.host if http.client else None,
                        user_agent=http.headers.get("user-agent"),
                        details={
                            "action": spec.name,
                            "task_id": task_id,
                            "args_sha1": args_digest,
                            "priority": req.priority or "normal",
                            "delay_seconds": int(req.delay_seconds or 0),
                            "idempotency_key_sha1": hashlib.sha1((idempotency_key or "").encode()).hexdigest(),
                            "x_request_id": request_id,
                            "status": "ENQUEUED",
                            "affected_workspaces": [int(wid)] if wid is not None else [],
                        },
                    )
                except Exception:
                    pass

            return {
                "task_id": task_id,
                "action": spec.name,
                "workspace_id": (int(wid) if wid is not None else None),
                "state": "ENQUEUED",
                "enqueued_at": meta["enqueued_at"],
                "status_url": f"{settings.API_PREFIX}/platform/tasks/{task_id}",
            }

        payload = _idempotent_get_or_set(
            action=spec.name,
            workspace_id=(int(wid) if wid is not None else None),
            idempotency_key=idempotency_key,
            payload_hash=payload_fingerprint,
            response_payload_factory=_produce,
            ttl_seconds=24 * 3600,
        )

        payload["_rate"] = {
            "limit": limit,
            "remaining": remaining,
            "reset": reset_ts,
        }

        status_label = "idempotent" if payload.get("_idempotent_hit") else "accepted"

        return TriggerResponse(**{k: v for k, v in payload.items() if k in TriggerResponse.model_fields})
    except RateLimitExceeded:
        status_label = "rate_limited"
        raise
    except APIError as exc:
        status_label = exc.code.lower()
        raise
    finally:
        duration = time.perf_counter() - start
        _TASK_TRIGGER_COUNTER.labels(task_key=spec.name, status=status_label).inc()
        _TASK_TRIGGER_DURATION.labels(task_key=spec.name).observe(duration)


@router.get("/{task_id}", response_model=TaskStateResponse)
def get_task_state(
    task_id: str,
    me: SessionUser = Depends(require_platform_admin),
):
    r = _get_redis()
    meta_raw = r.get(_meta_key(task_id))
    action_name: Optional[str] = None
    wid: Optional[int] = None
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            action_name = meta.get("action")
            wid = meta.get("workspace_id")
        except Exception:
            pass

    # 若 meta 存在，校验归属（防越权）
    if wid is not None and me.workspace_id != int(wid) and not me.is_platform_admin:
        raise APIError("FORBIDDEN", "Not allowed to access this task.", 403)

    res: AsyncResult = AsyncResult(task_id, app=celery_app)
    state = str(res.state)

    info = res.info if isinstance(res.info, dict) else ({} if res.info is None else {"_repr": str(res.info)})
    # 约定 Worker 在 info 中可填这些键：attempts、eta、started_at、finished_at、progress、result、error
    attempts = int(info.get("attempts") or 0)

    return TaskStateResponse(
        task_id=task_id,
        action=action_name,
        workspace_id=(int(wid) if wid is not None else None),
        state=state,
        attempts=attempts,
        eta=_to_iso(info.get("eta")),
        started_at=_to_iso(info.get("started_at")),
        finished_at=_to_iso(info.get("finished_at")),
        progress=info.get("progress"),
        result=(info.get("result") if state == "SUCCESS" else None),
        error=(info.get("error") if state in ("FAILURE", "RETRY") else None),
    )


@router.post("/{task_id}/cancel", response_model=CancelResponse)
def cancel_task(
    task_id: str,
    me: SessionUser = Depends(require_platform_admin),
):
    # 查 meta，判断是否允许取消 & 所属校验
    r = _get_redis()
    meta_raw = r.get(_meta_key(task_id))
    if not meta_raw:
        # 允许取消未知任务，但结果不可知：返回 404 更合理
        raise APIError("NOT_FOUND", "Task not found.", 404)

    try:
        meta = json.loads(meta_raw)
    except Exception:
        raise APIError("NOT_FOUND", "Task not found.", 404)

    action_name: str = meta.get("action")
    wid: Optional[int] = meta.get("workspace_id")
    spec = _ACTIONS.get(action_name)
    if not spec:
        # 已下线的动作，按不可取消处理
        raise APIError("FORBIDDEN", "Task action not cancellable.", 403)

    if not spec.cancellable:
        raise APIError("FORBIDDEN", "This task cannot be cancelled.", 403)

    if wid is not None and me.workspace_id != int(wid) and not me.is_platform_admin:
        raise APIError("FORBIDDEN", "Not allowed to access this task.", 403)

    # 取消（不强杀，避免资源泄露；如需强杀可 terminate=True 并指定 signal）
    celery_app.control.revoke(task_id, terminate=False)

    # 并发 -1（提前释放；即便 Worker 侧会再减，这里也没关系）
    _dec_concurrency(spec, wid)

    return CancelResponse(task_id=task_id, state="REVOKED")

