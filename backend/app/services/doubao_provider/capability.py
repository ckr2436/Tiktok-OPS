from __future__ import annotations

from datetime import datetime
from typing import Any


READY = "ready"
UNKNOWN = "unknown"
_TRANSIENT_PROBE_ERRORS = {
    "doubao_failed",
    "doubao_timeout",
    "doubao_browser_unstable",
    "doubao_composer_unavailable",
    "doubao_region_restricted",
    "doubao_risk_rate_limited",
    # Historical builds incorrectly stored request/conversation outcomes as
    # account capability failures.  A successful composer probe is sufficient
    # to clear those stale pool markers.
    "doubao_submit_unconfirmed",
    "doubao_silent_timeout",
    "doubao_text_only_response",
}
_NON_DESTRUCTIVE_PROBE_ERRORS = {
    "doubao_failed",
    "doubao_timeout",
    "doubao_browser_unstable",
    "doubao_composer_unavailable",
    "doubao_risk_rate_limited",
    "doubao_capability_probe_failed",
    "probe_dispatch_failed",
}


def seedance_capability_state(meta: dict[str, Any]) -> str:
    """Return a durable capability verdict, with a safe legacy upgrade.

    A captured cookie only proves login.  Historical successful generations
    are acceptable proof for legacy rows; every other row must pass an
    explicit composer probe before production routing may select it.
    """
    explicit = str(meta.get("doubao_seedance_capability_state") or "").strip()
    if (
        explicit == UNKNOWN
        and str(meta.get("doubao_seedance_capability_error") or "")
        in _NON_DESTRUCTIVE_PROBE_ERRORS
        and (
            meta.get("doubao_pool_last_success_at")
            or int(meta.get("doubao_pool_success_count") or 0) > 0
        )
    ):
        # Older code allowed one cold/unstable browser probe to erase a
        # capability proven by a downloaded production video. Recover those
        # rows from durable evidence without fabricating capability for a new
        # account that has never generated successfully.
        return READY
    if explicit:
        return explicit
    if meta.get("doubao_pool_last_success_at") or int(
        meta.get("doubao_pool_success_count") or 0
    ) > 0:
        return READY
    return UNKNOWN


def seedance_capability_ready(meta: dict[str, Any]) -> bool:
    return seedance_capability_state(meta) == READY


def apply_seedance_capability_result(
    meta: dict[str, Any], *, success: bool, error_code: str | None = None
) -> dict[str, Any]:
    now = datetime.now().isoformat()
    result = dict(meta)
    result["doubao_seedance_capability_checked_at"] = now
    previous_state = str(
        result.pop("doubao_seedance_capability_previous_state", "") or ""
    ).strip()
    if success:
        result["doubao_seedance_capability_state"] = READY
        result["doubao_seedance_capability_error"] = None
        result["doubao_seedance_capability_message"] = None
        # A successful live composer probe is stronger evidence than a stale
        # transient browser/region verdict.  Clear that cooldown atomically so
        # the account is routable immediately after the operator fixes its
        # network or the page recovers.  Do not erase quota/membership/auth
        # verdicts because the quota-free probe cannot prove those recovered.
        if str(result.get("doubao_pool_last_error") or "") in _TRANSIENT_PROBE_ERRORS:
            result["doubao_pool_last_error"] = None
            result["doubao_pool_cooldown_until"] = None
            result["doubao_pool_consecutive_errors"] = 0
            result["doubao_pool_enabled"] = True
        return result
    code = str(error_code or "doubao_capability_unknown")[:64]
    last_confirmed_state = previous_state or seedance_capability_state(result)
    if last_confirmed_state == READY and code in _NON_DESTRUCTIVE_PROBE_ERRORS:
        # A timeout, cold React page, rate limit or dispatch failure says only
        # that this probe was inconclusive. It must not erase the last positive
        # composer/production observation. Account cooldown and last-error
        # fields still control immediate routing independently.
        result["doubao_seedance_capability_state"] = READY
        result["doubao_seedance_capability_error"] = code
        result["doubao_seedance_capability_message"] = (
            "最近一次能力复检未完成；继续沿用已确认的视频能力。"
        )
        return result
    state = {
        "doubao_captcha_required": "captcha_required",
        "doubao_auth_required": "auth_required",
        "doubao_account_context_invalid": "auth_required",
        "doubao_region_restricted": "region_restricted",
        "doubao_composer_unavailable": "unavailable",
        "doubao_risk_rate_limited": "rate_limited",
        "doubao_browser_unstable": UNKNOWN,
        "doubao_timeout": UNKNOWN,
    }.get(code, UNKNOWN)
    result["doubao_seedance_capability_state"] = state
    result["doubao_seedance_capability_error"] = code
    return result


__all__ = [
    "READY",
    "UNKNOWN",
    "apply_seedance_capability_result",
    "seedance_capability_ready",
    "seedance_capability_state",
]
