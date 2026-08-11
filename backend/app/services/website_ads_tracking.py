from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# TikTok Ads API v1.3 treats __CID__ / __CID_NAME__ as the delivered ad ID/name.
# Keep this list within the API's 14-parameter limit.
TRACKING_PARAMS = [
    ("utm_source", "tiktok"),
    ("utm_medium", "paid"),
    ("utm_id", "__CAMPAIGN_ID__"),
    ("utm_campaign", "__CAMPAIGN_NAME__"),
    ("utm_term", "__AID_NAME__"),
    ("utm_content", "__CID__"),
    ("campaign_id", "__CAMPAIGN_ID__"),
    ("campaign_name", "__CAMPAIGN_NAME__"),
    ("adgroup_id", "__AID__"),
    ("adgroup_name", "__AID_NAME__"),
    ("ad_id", "__CID__"),
    ("creative_id", "__CID__"),
    ("creative_name", "__CID_NAME__"),
    ("placement", "__PLACEMENT__"),
]


def build_tracking_url(base_url: str) -> tuple[str, list[dict[str, str]]]:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in TRACKING_PARAMS:
        query[key] = value
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, [{"key": key, "value": value} for key, value in TRACKING_PARAMS]


__all__ = ["TRACKING_PARAMS", "build_tracking_url"]
