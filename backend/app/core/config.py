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
    ISSUER: Optional[str] = None  # e.g. https://gmv.myupona.com

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
    WEBSHELL_ENABLED: bool = False
    WEBSHELL_MAX_SESSIONS: int = 2
    WEBSHELL_MAX_SESSIONS_PER_USER: int = 1
    WEBSHELL_INIT_TIMEOUT_SECONDS: float = 5.0
    WEBSHELL_IDLE_TIMEOUT_SECONDS: float = 600.0
    WEBSHELL_SESSION_TIMEOUT_SECONDS: float = 1800.0
    WEBSHELL_PING_INTERVAL_SECONDS: float = 25.0
    WEBSHELL_MAX_INPUT_BYTES: int = 8192
    WEBSHELL_READ_CHUNK_BYTES: int = 4096
    WEBSHELL_ALLOWED_SHELLS: Any = ["/bin/bash", "/bin/sh"]
    WEBSHELL_DEFAULT_SHELL: str = "/bin/bash -li"
    WEBSHELL_CWD: str = ""
    WEBSHELL_TERM: str = "xterm-256color"

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
    TT_BIZ_TOKEN_PATH: str = "/oauth2/access_token/"
    TT_BIZ_REVOKE_PATH: str = "/oauth2/revoke_token/"
    TT_BIZ_ADVERTISER_LIST_PATH: str = "/oauth2/advertiser/get/"
    TT_BIZ_TIKTOK_ACCOUNT_CLIENT_KEY: str = ""
    OAUTH_SESSION_TTL_SECONDS: _TTLSeconds = _TTLSeconds(3600)
    TTB_API_DEFAULT_QPS: float = 5.0
    TTB_ADVERTISER_INFO_BATCH_SIZE: int = 50

    # TikTok Shop Open Platform uses an independent app, authorization domain,
    # token lifecycle, and request-signing scheme.
    TT_SHOP_US_AUTH_URL: str = "https://services.tiktokshops.us/open/authorize"
    TT_SHOP_TOKEN_URL: str = "https://auth.tiktok-shops.com/api/v2/token/get"
    TT_SHOP_REFRESH_URL: str = "https://auth.tiktok-shops.com/api/v2/token/refresh"
    TT_SHOP_API_BASE: str = "https://open-api.tiktokglobalshop.com"
    TT_SHOP_CALLBACK_PATH: str = "/api/oauth/tiktok-shop/callback"
    TT_SHOP_TOKEN_REFRESH_LEEWAY_SECONDS: int = 24 * 3600
    # Merchant-confirmed source timezone. Etc/GMT+8 is fixed UTC-8 and does not
    # apply daylight-saving transitions.
    TT_SHOP_DEFAULT_TIMEZONE: str = "Etc/GMT+8"
    TT_SHOP_PROMOTION_WRITES_ENABLED: bool = False
    TT_SHOP_FLASH_SALE_AUTOMATION_INTERVAL_SECONDS: int = 15 * 60
    TT_SHOP_FLASH_SALE_DURATION_SECONDS: int = 72 * 60 * 60 - 60
    TT_SHOP_FLASH_SALE_MIN_COVERAGE_SECONDS: int = 48 * 60 * 60
    TT_SHOP_FLASH_SALE_START_DELAY_SECONDS: int = 3 * 60
    TT_SHOP_FLASH_SALE_GAP_SECONDS: int = 60
    TT_SHOP_FAST_SYNC_INTERVAL_SECONDS: int = 5 * 60
    TT_SHOP_CATALOG_SYNC_INTERVAL_SECONDS: int = 15 * 60
    TT_SHOP_FINANCE_SYNC_INTERVAL_SECONDS: int = 60 * 60
    TT_SHOP_TOKEN_REFRESH_INTERVAL_SECONDS: int = 6 * 60 * 60

    # =========================
    # HTTP Client
    # =========================
    HTTP_CLIENT_TIMEOUT_SECONDS: float = 15.0

    # =========================
    # Bandianwa AI
    # =========================
    BANDIANWA_API_BASE_URL: str = "https://api.hellobabygo.com"
    BANDIANWA_HTTP_TIMEOUT_SECONDS: float = 60.0
    BANDIANWA_POLL_INTERVAL_SECONDS: int = 15
    BANDIANWA_POLL_TIMEOUT_SECONDS: int = 10 * 60
    BANDIANWA_BATCH_LIMIT: int = 50
    BANDIANWA_UPLOAD_STORAGE_DIR: str = "/data/gmv_ops/bandianwa_uploads"
    BANDIANWA_UPLOAD_MAX_IMAGE_BYTES: int = 20 * 1024 * 1024

    # =========================
    # GlobalAiOpc / Omni Flash
    # =========================
    GLOBALAIOPC_OMNI_FLASH_API_BASE_URL: str = "https://zcbservice.aizfw.cn/kyyReactApiServer"
    GLOBALAIOPC_OMNI_FLASH_HTTP_TIMEOUT_SECONDS: float = 60.0
    GLOBALAIOPC_OMNI_FLASH_POLL_INTERVAL_SECONDS: int = 30
    GLOBALAIOPC_OMNI_FLASH_POLL_TIMEOUT_SECONDS: int = 20 * 60
    GLOBALAIOPC_OMNI_FLASH_BATCH_LIMIT: int = 50
    GLOBALAIOPC_OMNI_FLASH_UPLOAD_STORAGE_DIR: str = "/data/gmv_ops/globalaiopc_uploads"
    GLOBALAIOPC_OMNI_FLASH_UPLOAD_MAX_IMAGE_BYTES: int = 15 * 1024 * 1024
    GLOBALAIOPC_OMNI_FLASH_PUBLIC_BASE_URL: str = ""

    # =========================
    # AI video local artifacts
    # =========================
    AI_VIDEO_RESULT_STORAGE_DIR: str = "/data/gmv_ops/ai_video_results"
    AI_VIDEO_RESULT_DOWNLOAD_TIMEOUT_SECONDS: float = 300.0
    AI_VIDEO_RESULT_MAX_BYTES: int = 2 * 1024 * 1024 * 1024

    # =========================
    # Redis
    # =========================
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_SSL: bool = False

    # TikTok Business API quotas. Values keep operational headroom below the
    # Advanced tier and its stricter GMV Max report endpoint limits.
    TTB_API_DEFAULT_QPS: float = 5.0
    TTB_API_GLOBAL_QPS: int = 18
    TTB_API_GLOBAL_QPM: int = 1000
    TTB_API_GLOBAL_QPD: int = 1_600_000
    TTB_API_GMVMAX_REPORT_QPS: int = 10
    TTB_API_GMVMAX_REPORT_QPM: int = 330
    TTB_API_GMVMAX_REPORT_QPD: int = 28_000
    TTB_API_RATE_LIMIT_MAX_WAIT_SECONDS: float = 8.0
    # When TikTok reports an upstream quota error, publish a shared cooldown
    # so every API/worker process stops retrying the same quota simultaneously.
    TTB_API_UPSTREAM_COOLDOWN_SECONDS: float = 30.0
    TTB_API_UPSTREAM_COOLDOWN_MAX_SECONDS: float = 300.0

    # Website Ads runtime cadence and bounded automatic creative expansion.
    WEBSITE_ADS_MONITOR_INTERVAL_SECONDS: int = 60
    WEBSITE_ADS_ASSET_SYNC_INTERVAL_SECONDS: int = 10 * 60
    WEBSITE_ADS_ASSET_ANALYSIS_INTERVAL_SECONDS: int = 2 * 60
    WEBSITE_ADS_MEDIA_CACHE_INTERVAL_SECONDS: int = 2 * 60
    WEBSITE_ADS_MEDIA_CACHE_BATCH_SIZE: int = 12
    WEBSITE_ADS_MEDIA_CACHE_RETRY_MINUTES: int = 6 * 60
    WEBSITE_ADS_MEDIA_TASK_QUEUE: str = "website_ads_media"
    # Website Ads control-plane work must never contend with the GMV Max
    # automation queue.  Media transfer and Whisper analysis retain their
    # dedicated queues below/elsewhere.
    WEBSITE_ADS_TASK_QUEUE: str = "website_ads"
    WEBSITE_ADS_MEDIA_STORAGE_DIR: str = "/data/gmv_ops/website_ads_media"
    WEBSITE_ADS_MEDIA_DOWNLOAD_TIMEOUT_SECONDS: float = 300.0
    WEBSITE_ADS_MEDIA_DOWNLOAD_ATTEMPTS: int = 4
    WEBSITE_ADS_MEDIA_PARTIAL_RETENTION_MINUTES: int = 120
    WEBSITE_ADS_TARGETING_CATALOG_DIR: str = "/data/gmv_ops/website_ads_targeting"
    WEBSITE_ADS_TARGETING_CATALOG_MAX_AGE_SECONDS: int = 24 * 60 * 60
    WEBSITE_ADS_TARGETING_CATALOG_SYNC_INTERVAL_SECONDS: int = 24 * 60 * 60

    # GMV Max creative media is mirrored to RAID storage because TikTok CDN
    # preview links are signed and expire. Downloads run on the media worker,
    # never in an API request or the guard decision loop.
    GMVMAX_MEDIA_STORAGE_DIR: str = "/data/gmv_ops/gmvmax_media"
    GMVMAX_MEDIA_CACHE_INTERVAL_SECONDS: int = 2 * 60
    GMVMAX_MEDIA_CACHE_BATCH_SIZE: int = 12
    WEBSITE_ADS_VIDEO_UPLOAD_TIMEOUT_SECONDS: float = 600.0
    WEBSITE_ADS_UPLOAD_STALE_MINUTES: int = 60
    WEBSITE_ADS_ASSET_EXPANSION_ENABLED: bool = True
    WEBSITE_ADS_ASSET_EXPANSION_INTERVAL_SECONDS: int = 5 * 60
    WEBSITE_ADS_ASSET_EXPANSION_MAX_ASSETS_PER_CYCLE: int = 1
    WEBSITE_ADS_ASSET_EXPANSION_TARGET_GROUPS: int = 2
    WEBSITE_ADS_ASSET_EXPANSION_ALLOW_CLONE: bool = False
    WEBSITE_ADS_MAX_ADGROUPS_PER_PLAN: int = 6
    WEBSITE_ADS_MAX_ACTIVE_ADS_PER_GROUP: int = 4
    WEBSITE_ADS_MAX_TOTAL_ADS_PER_GROUP: int = 20
    WEBSITE_ADS_TARGET_ACTIVE_ADS_PER_GROUP: int = 4
    WEBSITE_ADS_REPLACEMENT_MAX_ADS_PER_CYCLE: int = 6
    WEBSITE_ADS_PLATFORM_REJECTION_STRIKES: int = 2
    WEBSITE_ADS_GROUP_RACING_ENABLED: bool = True
    WEBSITE_ADS_GROUP_RACING_WIN_MIN_IMPRESSIONS: int = 100
    WEBSITE_ADS_GROUP_RACING_MIN_IMPRESSIONS: int = 300
    WEBSITE_ADS_GROUP_RACING_MIN_CLICKS: int = 8
    WEBSITE_ADS_GROUP_RACING_MIN_SPEND: float = 2.40
    WEBSITE_ADS_GROUP_RACING_WIN_CTR: float = 0.04
    WEBSITE_ADS_GROUP_RACING_WIN_MAX_CPC: float = 0.30
    WEBSITE_ADS_GROUP_RACING_LOSE_CTR: float = 0.03
    WEBSITE_ADS_GROUP_RACING_LOSE_CPC: float = 0.45
    WEBSITE_ADS_GROUP_RACING_COOLDOWN_MINUTES: int = 60
    WEBSITE_ADS_GROUP_RACING_MAX_HISTORY_GROUPS: int = 30
    WEBSITE_ADS_AUDIENCE_MIN_INTEREST_IDS: int = 2
    WEBSITE_ADS_AUDIENCE_MAX_INTEREST_IDS: int = 4
    WEBSITE_ADS_AUDIENCE_ESTIMATE_TARGET_GRADE: int = 3
    WEBSITE_ADS_AUDIENCE_ESTIMATE_MIN_GRADE: int = 2
    WEBSITE_ADS_AUDIENCE_EXPLORATION_ENABLED: bool = True
    WEBSITE_ADS_AUDIENCE_EXPLORATION_MAX_CANDIDATES: int = 12
    WEBSITE_ADS_CONVERSION_GUARD_ENABLED: bool = True
    WEBSITE_ADS_CONVERSION_GUARD_EVALUATION_MINUTES: int = 1
    WEBSITE_ADS_CONVERSION_GUARD_SOURCE_MAX_LAG_MINUTES: int = 180
    WEBSITE_ADS_CONVERSION_GUARD_LOOKBACK_DAYS: int = 14
    WEBSITE_ADS_CONVERSION_GUARD_DEFAULT_OBSERVATION_MINUTES: int = 240
    WEBSITE_ADS_CONVERSION_GUARD_MIN_OBSERVATION_MINUTES: int = 180
    WEBSITE_ADS_CONVERSION_GUARD_MAX_OBSERVATION_MINUTES: int = 480
    WEBSITE_ADS_CONVERSION_GUARD_MIN_COOLDOWN_MINUTES: int = 60
    WEBSITE_ADS_CONVERSION_GUARD_MAX_COOLDOWN_MINUTES: int = 360
    WEBSITE_ADS_CONVERSION_GUARD_EARLY_RESUME_MINUTES: int = 30
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_INTERVAL_MINUTES: int = 60
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_MIN_RUNTIME_MINUTES: int = 15
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_MAX_RUNTIME_MINUTES: int = 45
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_PRICE_RATIO: float = 0.45
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_MIN_SPEND: float = 3.0
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_MAX_SPEND: float = 12.0
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_MIN_CLICKS: int = 8
    WEBSITE_ADS_CONVERSION_GUARD_PROBE_MAX_CLICKS: int = 16
    WEBSITE_ADS_DAILY_REPORT_INTERVAL_SECONDS: int = 10 * 60
    WEBSITE_ADS_DAILY_REPORT_LOCAL_MINUTE: int = 30
    WEBSITE_ADS_DAILY_REPORT_FINAL_REFRESH_HOUR: int = 3
    WEBSITE_ADS_EXECUTION_LOCK_TTL_SECONDS: int = 25 * 60
    WEBSITE_ADS_EXECUTION_LOCK_HEARTBEAT_SECONDS: int = 20
    WEBSITE_ADS_EXECUTION_LOCK_ACQUIRE_TIMEOUT_SECONDS: float = 0.2

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
    CELERY_BROKER_URL: str = "amqp://guest:guest@127.0.0.1:5672/%2F"
    CELERY_RESULT_BACKEND: Optional[str] = None
    CELERY_BACKEND_URL: Optional[str] = None

    CELERY_TIMEZONE: str = "UTC"
    CELERY_TASK_DEFAULT_QUEUE: str = "gmv.tasks.default"
    CELERY_TASK_QUEUES: Any = ["gmv.tasks.default", "gmv.tasks.events", "gmv.tasks.maintenance"]
    CELERY_TASK_ACKS_LATE: bool = True
    CELERY_TASK_REJECT_ON_WORKER_LOST: bool = True
    CELERY_WORKER_CONCURRENCY: int = 4
    CELERY_WORKER_PREFETCH: int = 1
    CELERY_BEAT_ENABLE: bool = True
    CELERY_DEFAULT_QUEUE: Optional[str] = None
    CELERY_TASK_TRACK_STARTED: bool = True
    CELERY_TASK_HARD_TIME_LIMIT: int = 60 * 30
    CELERY_TASK_SOFT_TIME_LIMIT: int = 60 * 25
    CELERY_RESULT_EXPIRES: int = 60 * 60 * 24 * 3

    # Content Factory work is not uniformly sized.  A whole-series Showrunner
    # pass performs one coverage review plus a Director/Critic pair per page,
    # while an ordinary provider stage is a single bounded request.  These are
    # infrastructure budgets only; projects can supply narrower/longer stage
    # budgets in ``stage_execution_budgets`` without changing source code.
    HERMES_CONTENT_STAGE_SOFT_LIMIT_SECONDS: int = 15 * 60
    HERMES_CONTENT_STAGE_HARD_LIMIT_GRACE_SECONDS: int = 60
    HERMES_CONTENT_CONTROL_STAGE_SOFT_LIMIT_SECONDS: int = 30 * 60
    HERMES_CONTENT_SERIES_BASE_BUDGET_SECONDS: int = 5 * 60
    HERMES_CONTENT_MODEL_CALL_BUDGET_SECONDS: int = 150
    HERMES_CONTENT_MAX_STAGE_SOFT_LIMIT_SECONDS: int = 2 * 60 * 60
    HERMES_CONTENT_EXECUTION_LEASE_GRACE_SECONDS: int = 5 * 60

    # OpenAI-compatible relay routing. A logical request keeps one stable
    # idempotency key while the gateway rotates routes and retries transient
    # failures. Explicit balance exhaustion and prompt-policy rejection are
    # handled separately by the router and never consume the retry budget.
    AI_ROUTING_RETRY_MAX_ATTEMPTS: int = 8
    AI_ROUTING_RETRY_TOTAL_BUDGET_SECONDS: float = 165.0
    AI_ROUTING_RETRY_ATTEMPT_TIMEOUT_SECONDS: float = 90.0
    AI_ROUTING_RETRY_BASE_DELAY_SECONDS: float = 0.5
    AI_ROUTING_RETRY_MAX_DELAY_SECONDS: float = 4.0

    CELERY_WORKER_ENABLE_REMOTE_CONTROL: bool = False
    CELERY_WORKER_SEND_TASK_EVENTS: bool = False
    CELERY_TASK_SEND_SENT_EVENT: bool = False
    CELERY_TASK_CREATE_MISSING_QUEUES: bool = False

    CELERY_BEAT_DB_REFRESH_SECS: int = 15
    SCHEDULE_MIN_INTERVAL_SECONDS: int = 60

    # =========================
    # GMV Max Options
    # =========================
    GMV_MAX_OPTIONS_POLL_TIMEOUT_SECONDS: float = 3.0
    GMV_MAX_OPTIONS_POLL_INTERVAL_SECONDS: float = 0.3
    GMVMAX_OVERVIEW_SNAPSHOT_TTL_DAYS: int = 90
    GMVMAX_CAMPAIGN_METRICS_HOURLY_TTL_DAYS: int = 90
    GMVMAX_CAMPAIGN_METRICS_DAILY_TTL_DAYS: int = 730
    GMVMAX_CAMPAIGN_SNAPSHOT_TTL_DAYS: int = 90
    GMVMAX_CREATIVE_10MIN_TTL_DAYS: int = 90
    # Realtime creative collection spans the advertiser's current report day
    # plus exactly the prior day for timezone/day-boundary handoff.
    GMVMAX_CREATIVE_10MIN_LOOKBACK_DAYS: int = 1

    # =========================
    # Sync wait helpers
    # =========================
    TTB_SYNC_WAIT_TIMEOUT_SECONDS: float = 30.0
    TTB_SYNC_WAIT_INTERVAL_SECONDS: float = 0.5

    # =========================
    # Whisper / Subtitle tools
    # =========================
    WHISPER_MODEL_NAME: str = "small"
    OPENAI_WHISPER_FFMPEG_BIN: str = "ffmpeg"
    OPENAI_WHISPER_STORAGE_DIR: str = "/data/gmv_ops/openai_whisper"
    OPENAI_WHISPER_TASK_QUEUE: Optional[str] = None

    # Production lifecycle policy for generated video/subtitle artifacts.
    OPENAI_WHISPER_FAILED_RETENTION_DAYS: int = 7
    OPENAI_WHISPER_SUCCESS_RETENTION_DAYS: int = 90
    OPENAI_WHISPER_LARGE_ARTIFACT_RETENTION_DAYS: int = 30
    OPENAI_WHISPER_UPLOAD_RETENTION_HOURS: int = 24
    OPENAI_WHISPER_STALE_ACTIVE_HOURS: int = 24
    OPENAI_WHISPER_CLEANUP_BATCH_SIZE: int = 500
    OPENAI_WHISPER_MANUAL_DELETE_ACTIVE_ALLOWED: bool = False

    # =========================
    # Hermes Agent / AI Growth tools
    # =========================
    HERMES_AGENT_ENABLED: bool = False
    HERMES_AGENT_BASE_URL: str = "http://127.0.0.1:8642/v1"
    HERMES_AGENT_API_KEY: str = ""
    HERMES_AGENT_MODEL: str = "gmv-ops-hermes"
    HERMES_AGENT_TIMEOUT_SECONDS: float = 120.0
    HERMES_AGENT_TASK_QUEUE: str = "gmv.tasks.hermes_agent"
    AI_VIDEO_TASK_QUEUE: str = "gmv.tasks.ai_video"
    HERMES_AGENT_ALLOW_MEMBER: bool = True
    HERMES_AGENT_REQUIRE_EXPLICIT_PERMISSION: bool = True
    HERMES_AGENT_MAX_INPUT_CHARS: int = 30000
    HERMES_AGENT_MAX_RESULT_CHARS: int = 200000
    HERMES_AGENT_RUN_SYNC_TIMEOUT_SECONDS: float = 120.0
    # Isolated content roles. Director and Critic remain stateless and fail
    # closed. Producer is a separate conversational gateway whose persisted
    # response chain is scoped by workspace, user and intake session; the
    # database working brief remains authoritative.
    HERMES_CONTENT_PRODUCER_AGENT_ENABLED: bool = False
    HERMES_CONTENT_PRODUCER_AGENT_BASE_URL: str = "http://127.0.0.1:8648/v1"
    HERMES_CONTENT_PRODUCER_AGENT_MODEL: str = "gmv-ops-hermes-content-producer"
    HERMES_CONTENT_PRODUCER_AGENT_TIMEOUT_SECONDS: float = 180.0
    HERMES_CONTENT_DIRECTOR_AGENT_ENABLED: bool = False
    HERMES_CONTENT_DIRECTOR_AGENT_BASE_URL: str = "http://127.0.0.1:8645/v1"
    HERMES_CONTENT_DIRECTOR_AGENT_MODEL: str = "gmv-ops-hermes-content-director"
    HERMES_CONTENT_DIRECTOR_AGENT_TIMEOUT_SECONDS: float = 300.0
    HERMES_CONTENT_CRITIC_AGENT_ENABLED: bool = False
    HERMES_CONTENT_CRITIC_AGENT_BASE_URL: str = "http://127.0.0.1:8646/v1"
    HERMES_CONTENT_CRITIC_AGENT_MODEL: str = "gmv-ops-hermes-content-critic"
    HERMES_CONTENT_CRITIC_AGENT_TIMEOUT_SECONDS: float = 300.0
    # Stateless multimodal analyst for TikTok Shop / GMV Max video content.
    # It has its own gateway and queue so visual inference never shares content
    # factory state or blocks the one-minute advertising control loops.
    HERMES_VIDEO_ANALYST_AGENT_ENABLED: bool = False
    HERMES_VIDEO_ANALYST_AGENT_BASE_URL: str = "http://127.0.0.1:8647/v1"
    HERMES_VIDEO_ANALYST_AGENT_MODEL: str = "gmv-ops-hermes-video-analyst"
    HERMES_VIDEO_ANALYST_AGENT_TIMEOUT_SECONDS: float = 180.0
    HERMES_VIDEO_ANALYSIS_TASK_QUEUE: str = "gmv.tasks.video_analysis"
    HERMES_VIDEO_ANALYSIS_MAX_FRAMES: int = 8
    HERMES_VIDEO_ANALYSIS_FFMPEG_TIMEOUT_SECONDS: int = 90
    HERMES_VIDEO_ANALYSIS_LEASE_SECONDS: int = 10 * 60
    # Isolated advertising agents. Realtime approves bounded parameter changes;
    # review handles long-context daily reporting. Neither may inherit primary tools.
    HERMES_ADS_AGENT_ENABLED: bool = True
    HERMES_ADS_AGENT_BASE_URL: str = "http://127.0.0.1:8643/v1"
    HERMES_ADS_AGENT_MODEL: str = "gmv-ops-hermes-ads-realtime"
    HERMES_ADS_AGENT_TIMEOUT_SECONDS: float = 90.0
    HERMES_ADS_AGENT_FALLBACK_TO_PRIMARY: bool = False
    HERMES_ADS_REALTIME_AGENT_ENABLED: bool = True
    HERMES_ADS_REALTIME_AGENT_BASE_URL: str = "http://127.0.0.1:8643/v1"
    HERMES_ADS_REALTIME_AGENT_MODEL: str = "gmv-ops-hermes-ads-realtime"
    HERMES_ADS_REALTIME_AGENT_TIMEOUT_SECONDS: float = 90.0
    HERMES_ADS_REVIEW_AGENT_ENABLED: bool = True
    HERMES_ADS_REVIEW_AGENT_BASE_URL: str = "http://127.0.0.1:8644/v1"
    HERMES_ADS_REVIEW_AGENT_MODEL: str = "gmv-ops-hermes-ads-review"
    HERMES_ADS_REVIEW_AGENT_TIMEOUT_SECONDS: float = 600.0
    GMVMAX_HERMES_DAILY_REPORT_LOCAL_HOUR: int = 0
    GMVMAX_HERMES_DAILY_REPORT_LOCAL_MINUTE: int = 30
    GMVMAX_HERMES_DAILY_REPORT_FINAL_CUTOFF_HOUR: int = 1
    GMVMAX_HERMES_DAILY_REPORT_DETAIL_TOLERANCE: float = 0.05

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
