# backend/app/services/ttb_http.py
from __future__ import annotations

import logging
import re
from typing import List, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

# 目标 API 版本（冻结为 v1.3）
_API_VERSION = "open_api/v1.3"
_MAX_PAGE_SIZE = 50  # 官方上限，超出回落到 50

def _normalize_base(raw: str | None) -> str:
    """
    规范化业务 API 基址：
    - 默认 https://business-api.tiktok.com
    - 去掉末尾斜杠
    - 去掉尾部的 /open_api/<ver> 以避免重复拼接
    """
    base = (raw or "https://business-api.tiktok.com").strip().rstrip("/")
    base = re.sub(r"/open_api/\d+\.\d+$", "", base)
    return base

def _validate_api_base_or_raise(base: str) -> None:
    """
    严格校验 API 基址，确保：
    - scheme 为 https
    - 不携带路径（path 必须为空）
    - 域名存在
    """
    u = urlparse(base)
    if u.scheme != "https":
        raise ValueError(f"TT_BIZ_API_BASE must use https scheme, got: {base}")
    if not u.netloc:
        raise ValueError(f"TT_BIZ_API_BASE missing host, got: {base}")
    # 允许端口（如自建代理），但不允许 path
    if (u.path or "").strip("/"):
        raise ValueError(
            f"TT_BIZ_API_BASE must not include path segments (got path='{u.path}'). "
            f"Use pure origin like 'https://business-api.tiktok.com'."
        )

def _validate_event_track_or_raise(url: str) -> None:
    """
    校验 Events Track URL 是否严格以 /open_api/v1.3/event/track/ 结尾。
    违反则直接抛错（fail-fast）。
    """
    expected_suffix = "/open_api/v1.3/event/track/"
    if not url.endswith(expected_suffix):
        raise ValueError(
            f"TT_EVENT_API_URL must end with '{expected_suffix}', got: {url}"
        )

# ---- 读取与校验配置 ----
_API_BASE = _normalize_base(getattr(settings, "TT_BIZ_API_BASE", None))
_validate_api_base_or_raise(_API_BASE)

# 如果配置了 Events URL，则做冻结规则校验
_event_url = getattr(settings, "TT_EVENT_API_URL", None)
if _event_url:
    _validate_event_track_or_raise(_event_url)

def _clamp_page_size(qs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """将 page_size>50 的值钳制到 50。保留其它参数与顺序。"""
    out: List[Tuple[str, str]] = []
    for k, v in qs:
        if k == "page_size":
            try:
                n = int(v)
            except Exception:
                n = _MAX_PAGE_SIZE
            if n > _MAX_PAGE_SIZE:
                n = _MAX_PAGE_SIZE
            v = str(n)
        out.append((k, v))
    return out

def build_url(path: str) -> str:
    """
    规范化 TTB 业务接口 URL，确保 open_api/v1.3 只出现一次，并对 page_size 做上限钳制。
    - 接受路径形式：
        "bc/get/" | "/bc/get/" | "open_api/v1.3/bc/get/" | "/open_api/v1.3/bc/get/"
        含查询串（...?page_size=...）
        或绝对 URL（以 http/https 开头）→ 原样返回（不做钳制，以免误改第三方域名）
    """
    p = (path or "").strip()
    if not p:
        raise ValueError("empty path")

    # 绝对 URL 直接返回（不做任何改写）
    if p.startswith("http://") or p.startswith("https://"):
        return p

    # 拆分查询串
    if "?" in p:
        raw_path, raw_qs = p.split("?", 1)
    else:
        raw_path, raw_qs = p, ""

    # 清理路径前缀与多余斜杠
    base_path = raw_path.lstrip("/")
    if base_path.startswith(_API_VERSION):
        # 去掉前置的 open_api/v1.3
        base_path = base_path[len(_API_VERSION):].lstrip("/")
    else:
        # 去掉任意 open_api/<ver>/ 前缀，统一用冻结版本
        base_path = re.sub(r"^open_api/\d+\.\d+/?", "", base_path)

    # 解析并钳制查询参数
    qs_pairs = parse_qsl(raw_qs, keep_blank_values=True)
    if qs_pairs:
        qs_pairs = _clamp_page_size(qs_pairs)
        qs = "?" + urlencode(qs_pairs, doseq=True)
    else:
        qs = ""

    return f"{_API_BASE}/{_API_VERSION}/{base_path}{qs}"

