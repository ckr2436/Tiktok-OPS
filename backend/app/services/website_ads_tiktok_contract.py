from __future__ import annotations

import re
from typing import Any


TIKTOK_US_COUNTRY_LOCATION_ID = "6252001"
TIKTOK_US_EXCLUDED_LOCATION_IDS = frozenset({
    "5855797",  # Hawaii
    "5879092",  # Alaska
})

# TikTok /search/region/ ADMIN provinces verified for advertiser 7642678413060538386
# on 2026-07-14. This is the contiguous 48 states plus District of Columbia.
TIKTOK_CONTIGUOUS_US_LOCATION_IDS = (
    "4829764",  # Alabama
    "5551752",  # Arizona
    "4099753",  # Arkansas
    "5332921",  # California
    "5417618",  # Colorado
    "4831725",  # Connecticut
    "4142224",  # Delaware
    "4138106",  # District of Columbia
    "4155751",  # Florida
    "4197000",  # Georgia
    "5596512",  # Idaho
    "4896861",  # Illinois
    "4921868",  # Indiana
    "4862182",  # Iowa
    "4273857",  # Kansas
    "6254925",  # Kentucky
    "4331987",  # Louisiana
    "4971068",  # Maine
    "4361885",  # Maryland
    "6254926",  # Massachusetts
    "5001836",  # Michigan
    "5037779",  # Minnesota
    "4436296",  # Mississippi
    "4398678",  # Missouri
    "5667009",  # Montana
    "5073708",  # Nebraska
    "5509151",  # Nevada
    "5090174",  # New Hampshire
    "5101760",  # New Jersey
    "5481136",  # New Mexico
    "5128638",  # New York
    "4482348",  # North Carolina
    "5690763",  # North Dakota
    "5165418",  # Ohio
    "4544379",  # Oklahoma
    "5744337",  # Oregon
    "6254927",  # Pennsylvania
    "5224323",  # Rhode Island
    "4597040",  # South Carolina
    "5769223",  # South Dakota
    "4662168",  # Tennessee
    "4736286",  # Texas
    "5549030",  # Utah
    "5242283",  # Vermont
    "6254928",  # Virginia
    "5815135",  # Washington
    "4826850",  # West Virginia
    "5279468",  # Wisconsin
    "5843591",  # Wyoming
)

WEBSITE_ADS_PLACEMENT_TYPE = "PLACEMENT_TYPE_NORMAL"
WEBSITE_ADS_PLACEMENTS = ("PLACEMENT_TIKTOK",)
WEBSITE_ADS_OPTIMIZATION_EVENT = "ON_WEB_DETAIL"
WEBSITE_ADS_OPTIMIZATION_EVENT_LABEL = "View Content"


def website_ads_optimization_fields() -> dict[str, str]:
    """Return the single production contract for web-link ad-group optimization."""
    return {
        "optimization_goal": "CONVERT",
        "optimization_event": WEBSITE_ADS_OPTIMIZATION_EVENT,
        "billing_event": "OCPM",
    }


def select_website_ads_pixel(
    payload: Any,
    *,
    preferred_pixel_id: str | None = None,
) -> dict[str, Any]:
    """Select an active Pixel that exposes the View Content ad-creation enum."""
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if isinstance(data, dict):
        candidates = data.get("pixels") or data.get("list") or []
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []

    preferred = str(preferred_pixel_id or "").strip()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        pixel_id = str(raw.get("pixel_id") or raw.get("pixel_code") or raw.get("id") or "").strip()
        if not pixel_id or str(raw.get("activity_status") or "ACTIVE").upper() not in {
            "ACTIVE", "NO_RECENT_ACTIVITY"
        }:
            continue
        events = raw.get("events") if isinstance(raw.get("events"), list) else []
        supports_event = any(
            isinstance(event, dict)
            and str(event.get("optimization_event") or "").upper() == WEBSITE_ADS_OPTIMIZATION_EVENT
            and event.get("deprecated") is not True
            for event in events
        )
        if not supports_event:
            continue
        ranked.append((100 if pixel_id == preferred else 0, raw))
    if not ranked:
        raise ValueError(
            f"No active TikTok Pixel supports {WEBSITE_ADS_OPTIMIZATION_EVENT_LABEL} "
            f"({WEBSITE_ADS_OPTIMIZATION_EVENT})"
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def enforce_website_ads_placement_policy(
    placement_type: object = None,
    placements: object = None,
) -> tuple[str, list[str]]:
    """Keep Website Ads delivery on TikTok regardless of legacy plan data.

    TikTok ignores ``placements`` when automatic placement is selected. This
    policy is enforced again at launch so plans created before the correction
    cannot silently expand delivery to Pangle or Global App Bundle.
    """
    del placement_type, placements
    return WEBSITE_ADS_PLACEMENT_TYPE, list(WEBSITE_ADS_PLACEMENTS)


def enforce_website_ads_location_policy(location_ids: object) -> list[str]:
    """Apply the production US shipping boundary to TikTok location IDs.

    TikTok does not expose an excluded-state field and rejects overlapping
    country and state locations. A US country target is therefore expanded to
    the supported contiguous-US state list, while Alaska and Hawaii are always
    removed from explicit state selections.
    """
    if isinstance(location_ids, (str, int)):
        raw_ids = [location_ids]
    elif isinstance(location_ids, (list, tuple, set, frozenset)):
        raw_ids = list(location_ids)
    else:
        raw_ids = []

    cleaned = list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))
    expanded: list[str] = []
    if TIKTOK_US_COUNTRY_LOCATION_ID in cleaned:
        expanded.extend(TIKTOK_CONTIGUOUS_US_LOCATION_IDS)
    expanded.extend(
        location_id
        for location_id in cleaned
        if location_id != TIKTOK_US_COUNTRY_LOCATION_ID
        and location_id not in TIKTOK_US_EXCLUDED_LOCATION_IDS
    )
    effective = list(dict.fromkeys(expanded))
    if not effective:
        raise ValueError("Website Ads targeting cannot include only Alaska or Hawaii")
    return effective


TIKTOK_CALL_TO_ACTIONS = frozenset({
    "APPLY_NOW",
    "BOOK_NOW",
    "CALL_NOW",
    "CHECK_AVAILABILITY",
    "CONTACT_US",
    "DOWNLOAD_NOW",
    "EXPERIENCE_NOW",
    "GET_QUOTE",
    "GET_SHOWTIMES",
    "GET_TICKETS_NOW",
    "INSTALL_NOW",
    "INTERESTED",
    "JOIN_THIS_HASHTAG",
    "LEARN_MORE",
    "LISTEN_NOW",
    "ORDER_NOW",
    "PLAY_GAME",
    "PREORDER_NOW",
    "READ_MORE",
    "SEND_MESSAGE",
    "SHOOT_WITH_THIS_EFFECT",
    "SHOP_NOW",
    "SIGN_UP",
    "SUBSCRIBE",
    "VIEW_NOW",
    "VIEW_PROFILE",
    "VIEW_VIDEO_WITH_THIS_EFFECT",
    "VISIT_STORE",
    "WATCH_LIVE",
    "WATCH_NOW",
})

_CALL_TO_ACTION_ALIASES = {
    "BUY": "SHOP_NOW",
    "BUY_NOW": "SHOP_NOW",
    "GET_STARTED": "LEARN_MORE",
    "LEARNMORE": "LEARN_MORE",
    "ORDER": "ORDER_NOW",
    "PURCHASE": "SHOP_NOW",
    "PURCHASE_NOW": "SHOP_NOW",
    "SHOP": "SHOP_NOW",
    "SHOPNOW": "SHOP_NOW",
}


def normalize_tiktok_call_to_action(value: object, *, default: str = "SHOP_NOW") -> str:
    token = re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")
    token = _CALL_TO_ACTION_ALIASES.get(token, token)
    if token in TIKTOK_CALL_TO_ACTIONS:
        return token
    fallback = re.sub(r"[^A-Z0-9]+", "_", str(default or "SHOP_NOW").strip().upper()).strip("_")
    return fallback if fallback in TIKTOK_CALL_TO_ACTIONS else "SHOP_NOW"


WEBSITE_SALES_CALL_TO_ACTIONS = frozenset({
    "LEARN_MORE",
    "ORDER_NOW",
    "PREORDER_NOW",
    "SHOP_NOW",
})
EDUCATIONAL_FUNNEL_RATIONALE_PREFIX = "[EDUCATIONAL_FUNNEL]"


def normalize_website_sales_call_to_action(
    value: object,
    *,
    rationale: object = None,
) -> str:
    """Keep direct-response plans on SHOP_NOW unless education is explicit."""
    token = normalize_tiktok_call_to_action(value, default="SHOP_NOW")
    if token not in WEBSITE_SALES_CALL_TO_ACTIONS:
        return "SHOP_NOW"
    if token == "LEARN_MORE" and not str(rationale or "").strip().upper().startswith(
        EDUCATIONAL_FUNNEL_RATIONALE_PREFIX
    ):
        return "SHOP_NOW"
    return token


def select_tiktok_video_identity(
    payload: Any,
    *,
    preferred_identity_id: str | None = None,
) -> dict[str, Any]:
    """Select an identity that can push advertiser-library videos.

    AUTH_CODE identities represent authorized Spark posts and cannot be paired
    with an uploaded advertiser video_id. TikTok returns those identities first
    for some accounts, so list order is not a compatibility signal.
    """
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if isinstance(data, dict):
        candidates = data.get("identity_list") or data.get("list") or []
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []

    preferred = str(preferred_identity_id or "")
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    priorities = {"BC_AUTH_TT": 300, "TT_USER": 200, "CUSTOMIZED_USER": 100}
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            continue
        identity = raw.get("identity_info") if isinstance(raw.get("identity_info"), dict) else raw
        identity_id = str(identity.get("identity_id") or identity.get("id") or "")
        identity_type = str(identity.get("identity_type") or "").upper()
        if not identity_id or identity_type not in priorities:
            continue
        bc_id = identity.get("identity_authorized_bc_id")
        if identity_type == "BC_AUTH_TT" and not bc_id:
            continue
        if str(identity.get("available_status") or "AVAILABLE").upper() not in {"", "AVAILABLE"}:
            continue
        if identity.get("can_push_video") is False:
            continue
        score = priorities[identity_type]
        if identity.get("can_push_video") is True:
            score += 20
        if identity_id == preferred:
            score += 1000
        ranked.append((score, -index, identity))

    if not ranked:
        raise ValueError(
            "No TikTok identity can push advertiser-library videos; authorize an available "
            "BC_AUTH_TT, TT_USER, or CUSTOMIZED_USER identity"
        )
    identity = max(ranked, key=lambda item: (item[0], item[1]))[2]
    return {
        "identity_id": str(identity.get("identity_id") or identity.get("id")),
        "identity_type": str(identity.get("identity_type") or "").upper(),
        "identity_authorized_bc_id": identity.get("identity_authorized_bc_id"),
    }


async def compensate_created_campaign(
    api: Any,
    *,
    advertiser_id: str,
    campaign_id: str,
    before_mutation=None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "campaign_id": str(campaign_id),
        "operation_status": None,
        "delete_response": None,
        "disable_response": None,
        "delete_error": None,
        "disable_error": None,
    }
    if before_mutation is not None:
        before_mutation()
    try:
        result["delete_response"] = await api.update_campaign_status(
            str(advertiser_id), [str(campaign_id)], "DELETE"
        )
        result["operation_status"] = "DELETE"
        return result
    except Exception as exc:
        if "has been deleted" in str(exc).lower() or "already deleted" in str(exc).lower():
            result["operation_status"] = "DELETE"
            result["delete_response"] = {"idempotent": True, "message": str(exc)}
            return result
        result["delete_error"] = f"{type(exc).__name__}: {exc}"[:2000]

    if before_mutation is not None:
        before_mutation()
    try:
        result["disable_response"] = await api.update_campaign_status(
            str(advertiser_id), [str(campaign_id)], "DISABLE"
        )
        result["operation_status"] = "DISABLE"
    except Exception as exc:
        result["disable_error"] = f"{type(exc).__name__}: {exc}"[:2000]
    return result
