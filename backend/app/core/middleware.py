# app/core/middleware.py
from __future__ import annotations

import os
import json
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # 改为从 uvicorn 引入

from app.core.config import settings


def _parse_list_like(value: object) -> List[str]:
    """
    支持三种形式：
    1) Python 列表（若 settings 字段已是 List[str]）
    2) JSON 数组字符串：'["https://a.com","https://b.com"]'
    3) 逗号分隔字符串：'https://a.com, https://b.com'
    """
    if value is None:
        return []
    # 已是列表
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]

    s = str(value).strip()
    if not s:
        return []
    # JSON 数组
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    # 逗号分隔
    return [x.strip() for x in s.split(",") if x.strip()]


def install_middleware(app: FastAPI) -> None:
    """
    统一安装：
    - ProxyHeadersMiddleware：尊重 X-Forwarded-*（Nginx/FRP 后可得到真实 scheme/ip）
    - TrustedHostMiddleware：Host 白名单（防 Host 头伪造）
    - CORSMiddleware：跨域白名单（支持 Cookie）
    - GZipMiddleware：压缩响应
    """
    # 1) 代理头（置前）
    # 若你的前置代理（Nginx / frps）已正确设置 X-Forwarded-For / -Proto，则这里按真实协议拼接 URL。
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    # 2) Host 白名单（从环境变量读取；未在 Settings 中定义也没关系）
    #   .env 建议：ALLOWED_HOSTS=["gmv.drafyn.com","drafyn.com","www.drafyn.com","127.0.0.1","localhost"]
    allowed_hosts = _parse_list_like(os.getenv("ALLOWED_HOSTS", ""))

    # 默认兜底（生产尽量在 .env 填写，避免走默认）
    if not allowed_hosts:
        if settings.DEBUG:
            allowed_hosts = ["*", "127.0.0.1", "localhost"]
        else:
            allowed_hosts = ["gmv.drafyn.com", "drafyn.com", "www.drafyn.com", "127.0.0.1", "localhost"]

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    # 3) CORS 白名单
    #   .env 已有：CORS_ORIGINS=["https://gmv.drafyn.com","https://drafyn.com","https://www.drafyn.com"]
    cors_origins = _parse_list_like(getattr(settings, "CORS_ORIGINS", []))

    # 若允许带 Cookie，不能使用 "*"
    if settings.CORS_ALLOW_CREDENTIALS and ("*" in cors_origins):
        cors_origins = [o for o in cors_origins if o != "*"]

    # 兜底：未配置时，开发与生产给出合理默认集合，避免被全拦或全放
    if not cors_origins:
        if settings.DEBUG:
            cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
        else:
            cors_origins = ["https://gmv.drafyn.com", "https://drafyn.com", "https://www.drafyn.com"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=bool(getattr(settings, "CORS_ALLOW_CREDENTIALS", True)),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],   # 前端下载 CSV 时需要
        max_age=86400,
    )

    # 4) 压缩
    app.add_middleware(GZipMiddleware, minimum_size=1024)

