# app/core/config.py
from __future__ import annotations

from functools import lru_cache
from typing import Optional, Any, List

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


class _TTLSeconds(int):
    """可序列化 TTL 辅助类型：环境变量读进来后变成 int，但保留便捷方法。"""
    def __new__(cls, value: int):
        return super().__new__(cls, int(value))

    def to_datetime(self):
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone.utc) + timedelta(seconds=int(self))


def _as_list(value: Any) -> List[str]:
    """
    列表型环境变量的健壮解析：
    - 已是 list -> 逐项 str().strip()
    - JSON 数组字符串 -> json.loads
    - 逗号分隔字符串 -> split(",")
    - 其它/None/空串 -> []
    * 永不抛异常 *
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    if not s:
        return []
    # JSON 数组
    if s.startswith("[") and s.endswith("]"):
        try:
            import json
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            # 回退到逗号分隔
            pass
    # 逗号分隔
    return [x.strip() for x in s.split(",") if x.strip()]


class Settings(BaseSettings):
    # =========================
    # App / 基础
    # =========================
    APP_NAME: str = "GMV API"
    APP_VERSION: str = "1.0.0"
    API_PREFIX: str = "/api/v1"
    DEBUG: bool = False
    ENV: str = "prod"
    ISSUER: Optional[str] = None  # e.g. https://gmv.drafyn.com

    # =========================
    # 数据库
    # =========================
    DATABASE_URL: str = "sqlite:///./gmv.db"
    DB_POOL_PRE_PING: bool = True
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # =========================
    # CORS / Host 白名单
    # =========================
    # 用 Any 避免 EnvSettingsSource 直接做 JSON 解码，统一交给 validator 处理
    CORS_ORIGINS: Any = []
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: Any = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    CORS_ALLOW_HEADERS: Any = ["*"]

    ALLOWED_HOSTS: Any = ["*"]  # 生产建议配置为具体域名数组

    # =========================
    # Cookie / Session
    # =========================
    SECRET_KEY: str = "change-me"
    COOKIE_NAME: str = "gmv_session"
    COOKIE_DOMAIN: Optional[str] = None
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: str = "lax"
    SESSION_MAX_AGE_SECONDS: int = 86400
    SESSION_REMEMBER_MAX_AGE_SECONDS: int = 30 * 24 * 3600

    # =========================
    # 安全 / 密码
    # =========================
    PBKDF2_ITERATIONS: int = 240_000

    # =========================
    # Admin Docs
    # =========================
    ADMIN_DOCS_ENABLE: bool = False
    ADMIN_DOCS_DIR: Optional[str] = None

    # =========================
    # Crypto（主密钥）
    # =========================
    CRYPTO_MASTER_KEY_B64: str = ""  # Base64URL（无 '='），建议 32 字节

    # =========================
    # OAuth · TikTok Business
    # =========================
    TT_BIZ_PORTAL_AUTH_URL: str = "https://business-api.tiktok.com/portal"
    TT_BIZ_TOKEN_URL: str = "https://business-api.tiktok.com/open_api/v1.3"
    # 下面几个 path 给默认值，便于服务内拼接/复用（你代码里也做了兜底）
    TT_BIZ_TOKEN_PATH: str = "/oauth/token"
    TT_BIZ_REVOKE_PATH: str = "/oauth/revoke"
    TT_BIZ_ADVERTISER_LIST_PATH: str = "/oauth/advertiser/list/"
    OAUTH_SESSION_TTL_SECONDS: _TTLSeconds = _TTLSeconds(3600)
    TTB_API_DEFAULT_QPS: float = 5.0
    TTB_ADVERTISER_INFO_BATCH_SIZE: int = 50

    # =========================
    # HTTP Client
    # =========================
    HTTP_CLIENT_TIMEOUT_SECONDS: float = 15.0

    # =========================
    # Redis
    # =========================
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_SSL: bool = False

    # =========================
    # Redis Locks / TTB Sync
    # =========================
    LOCK_ENV: str = "local"
    TTB_SYNC_USE_DB_LOCKS: bool = False
    TTB_SYNC_LOCK_PREFIX: str = "gmv:locks:"
    TTB_SYNC_LOCK_TTL_SECONDS: int = 15 * 60
    TTB_SYNC_LOCK_HEARTBEAT_SECONDS: int = 60

    # =========================
    # RabbitMQ (AMQP)
    # =========================
    RABBITMQ_AMQP_URL: str = "amqp://guest:guest@127.0.0.1:5672/%2F"
    RABBITMQ_VHOST: str = "gmv-ops"
    # 下列用于你其它服务；当前 Celery 调度不依赖也保留以兼容
    RABBITMQ_EXCHANGE_SYNC: str = "gmv.sync"
    RABBITMQ_EXCHANGE_DLX: str = "gmv.dlx"
    RABBITMQ_ROUTING_KEY_PREFIX: str = "sync"

    # =========================
    # Celery（与 .env 对齐）
    # =========================
    # 统一：Broker 用 RabbitMQ，Result Backend 用 Redis
    CELERY_BROKER_URL: str = "amqp://guest:guest@127.0.0.1:5672/%2F"
    CELERY_RESULT_BACKEND: Optional[str] = None  # 若为空，启动时在 app/celery_app.py 会回退到 REDIS_URL
    # 兼容旧名（有些代码用 BACKEND_URL）
    CELERY_BACKEND_URL: Optional[str] = None

    CELERY_TIMEZONE: str = "UTC"  # 你在 .env 里是 Asia/Shanghai，会覆盖这里
    CELERY_TASK_DEFAULT_QUEUE: str = "gmv.tasks.default"
    CELERY_TASK_QUEUES: Any = ["gmv.tasks.default", "gmv.tasks.events", "gmv.tasks.maintenance"]
    CELERY_TASK_ACKS_LATE: bool = True
    CELERY_TASK_REJECT_ON_WORKER_LOST: bool = True
    CELERY_WORKER_CONCURRENCY: int = 4
    CELERY_BEAT_ENABLE: bool = True
    CELERY_DEFAULT_QUEUE: Optional[str] = None

    # DB 调度器的刷新周期 & 业务侧可用的最小粒度（供调度路由/校验使用）
    CELERY_BEAT_DB_REFRESH_SECS: int = 15
    SCHEDULE_MIN_INTERVAL_SECONDS: int = 60

    # =========================
    # GMV Max Options
    # =========================
    GMV_MAX_OPTIONS_POLL_TIMEOUT_SECONDS: float = 3.0
    GMV_MAX_OPTIONS_POLL_INTERVAL_SECONDS: float = 0.3

    # =========================
    # Whisper / Subtitle tools
    # =========================
    WHISPER_MODEL_NAME: str = "small"
    OPENAI_WHISPER_STORAGE_DIR: str = "/data/gmv_ops/openai_whisper"
    OPENAI_WHISPER_TASK_QUEUE: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 校正器 ----------
    @field_validator("OAUTH_SESSION_TTL_SECONDS", mode="before")
    @classmethod
    def _coerce_ttl(cls, v: Any) -> _TTLSeconds:
        if isinstance(v, _TTLSeconds):
            return v
        if v is None or v == "":
            return _TTLSeconds(3600)
        try:
            return _TTLSeconds(int(v))
        except Exception:
            raise ValueError("OAUTH_SESSION_TTL_SECONDS must be an integer number of seconds")

    @field_validator(
        "CORS_ORIGINS",
        "ALLOWED_HOSTS",
        "CORS_ALLOW_METHODS",
        "CORS_ALLOW_HEADERS",
        "CELERY_TASK_QUEUES",
        mode="before",
    )
    @classmethod
    def _coerce_list_like(cls, v: Any) -> List[str]:
        return _as_list(v)

    @field_validator("LOCK_ENV", mode="before")
    @classmethod
    def _derive_lock_env(cls, value: Any) -> str:
        candidate = str(value).strip() if value is not None else ""
        if candidate:
            return candidate
        import os

        for env_name in ("APP_ENV", "ENV", "DEPLOY_ENV"):
            env_value = os.getenv(env_name)
            if env_value and str(env_value).strip():
                return str(env_value).strip()
        return "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

