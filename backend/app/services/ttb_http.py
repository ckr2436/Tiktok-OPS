# backend/app/services/ttb_http.py
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode
from app.core.config import settings

# 目标 API 版本
_API_VERSION = "open_api/v1.3"

def _normalize_base(raw: str | None) -> str:
    base = (raw or "https://business-api.tiktok.com").strip().rstrip("/")
    # 去掉自带的 open_api/<ver> 尾段，统一由 build_url 拼接
    base = re.sub(r"/open_api/\d+\.\d+$", "", base)
    return base

_API_BASE = _normalize_base(
    getattr(settings, "TT_BIZ_API_BASE", None) or getattr(settings, "TT_BIZ_TOKEN_URL", None)
)

_MAX_PAGE_SIZE = 50  # 官方上限，超出一律回落到 50

def _clamp_page_size(qs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """将 page_size>50 的值钳制到 50。保留其它参数与顺序。"""
    out: list[tuple[str, str]] = []
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
    - path 可为：
        "bc/get/" | "/bc/get/" | "open_api/v1.3/bc/get/" | "/open_api/v1.3/bc/get/" | 含查询串 | 绝对URL
    - 若为绝对 URL（以 http 开头），原样返回（不二次拼接）
    """
    p = (path or "").strip()
    if not p:
        raise ValueError("empty path")

    # 绝对 URL 直接返回（此分支不做钳制，避免误改其它域名）
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
        base_path = base_path[len(_API_VERSION):].lstrip("/")
    else:
        base_path = re.sub(r"^open_api/\d+\.\d+/?", "", base_path)

    # 解析并钳制查询参数
    qs_pairs = parse_qsl(raw_qs, keep_blank_values=True)
    if qs_pairs:
        qs_pairs = _clamp_page_size(qs_pairs)
        qs = "?" + urlencode(qs_pairs, doseq=True)
    else:
        qs = ""

    return f"{_API_BASE}/{_API_VERSION}/{base_path}{qs}"

