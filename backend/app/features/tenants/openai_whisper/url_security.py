"""Fail-closed network boundary for user supplied Whisper share links."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse, urlunparse

import httpx


SUPPORTED_SHARE_HOST_SUFFIXES = (
    "douyin.com",
    "iesdouyin.com",
    "amemv.com",
    "snssdk.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "kuaishou.com",
    "gifshow.com",
    "kwai.com",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "x.com",
    "twitter.com",
    "bilibili.com",
    "b23.tv",
    "xiaohongshu.com",
    "xhslink.com",
    "weibo.com",
    "weibo.cn",
    "vimeo.com",
    "vimeo.app.link",
    "reddit.com",
    "redd.it",
    "twitch.tv",
    "dailymotion.com",
    "dai.ly",
    "pinterest.com",
    "pin.it",
    "linkedin.com",
    "nicovideo.jp",
    "nico.ms",
    "youku.com",
    "iqiyi.com",
    "iq.com",
)
MAX_REDIRECTS = 6
HTTPS_UPGRADE_MEDIA_HOST_SUFFIXES = ("xhscdn.com",)


class UnsafeShareURLError(ValueError):
    """Raised when a share URL can reach outside the approved public boundary."""


def _host_matches(host: str, suffix: str) -> bool:
    normalized = host.rstrip(".").lower()
    expected = suffix.rstrip(".").lower()
    return normalized == expected or normalized.endswith(f".{expected}")


def _resolved_public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = {literal}
    else:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeShareURLError("分享链接域名暂时无法解析。") from exc
        addresses = set()
        for answer in answers:
            try:
                addresses.add(ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0]))
            except (ValueError, IndexError):
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeShareURLError("分享链接不能访问内网、回环或保留地址。")
    return tuple(sorted(str(address) for address in addresses))


def validate_share_url(url: str, *, require_supported_host: bool = True) -> str:
    value = str(url or "").strip()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeShareURLError("分享链接格式不正确。") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UnsafeShareURLError("分享链接必须使用 HTTPS。")
    if parsed.username or parsed.password:
        raise UnsafeShareURLError("分享链接不能包含登录凭据。")
    if port not in (None, 443):
        raise UnsafeShareURLError("分享链接端口不受支持。")
    host = parsed.hostname.rstrip(".").lower()
    if require_supported_host and not any(
        _host_matches(host, suffix) for suffix in SUPPORTED_SHARE_HOST_SUFFIXES
    ):
        raise UnsafeShareURLError(
            "该分享链接平台尚未加入安全下载白名单。"
        )
    _resolved_public_addresses(host, port or 443)
    return value


def resolve_safe_share_url(url: str) -> str:
    """Validate the initial URL and every HTTP redirect before yt-dlp sees it."""
    current = validate_share_url(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
        "Range": "bytes=0-0",
    }
    with httpx.Client(follow_redirects=False, timeout=12.0, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            # Validation is repeated immediately before every outbound request.
            current = validate_share_url(current)
            with client.stream("GET", current, headers=headers) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return current
                location = str(response.headers.get("location") or "").strip()
            if not location:
                raise UnsafeShareURLError("分享链接重定向缺少目标地址。")
            current = validate_share_url(urljoin(current, location))
    raise UnsafeShareURLError("分享链接重定向次数过多。")


def _secure_extracted_media_url(media_url: str) -> str:
    """Upgrade only known HTTPS-capable CDNs, then apply the public-address boundary."""
    value = str(media_url or "").strip()
    parsed = urlparse(value)
    host = str(parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() == "http" and any(
        _host_matches(host, suffix) for suffix in HTTPS_UPGRADE_MEDIA_HOST_SUFFIXES
    ):
        value = urlunparse(parsed._replace(scheme="https"))
    return validate_share_url(value, require_supported_host=False)


def validate_extracted_media_urls(info: dict) -> None:
    """Reject unsafe media endpoints and pin approved HTTP CDN URLs to HTTPS."""
    if info.get("url"):
        info["url"] = _secure_extracted_media_url(str(info["url"]))
    for item in list(info.get("formats") or []):
        if isinstance(item, dict) and item.get("url"):
            item["url"] = _secure_extracted_media_url(str(item["url"]))
