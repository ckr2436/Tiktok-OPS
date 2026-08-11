from __future__ import annotations

import json
from typing import Any, Mapping


POLICY_READINESS_VALUES = frozenset({"APPROVED", "REVIEW", "BLOCKED"})

# These rules protect legacy analyses that predate the structured readiness
# field. They intentionally match concrete present-tense claim problems rather
# than generic advice such as "avoid medical claims".
_LEGACY_BLOCKING_RULES = (
    ("medical endorsement", "implied medical endorsement"),
    ("doctor friend", "doctor endorsement or advice"),
    ("doctor-reference", "doctor endorsement or advice"),
    ("did not prescribe", "prescription or medical-advice reference"),
    ("contradictory or malformed", "contradictory dosage or formula claim"),
    ("conflicts with the catalog", "claim conflicts with verified catalog data"),
    ("conflicts with catalog", "claim conflicts with verified catalog data"),
    ("not an approved catalog", "unapproved catalog claim"),
    ("should be verified against", "unverified product claim"),
    ("unverified dosage", "unverified dosage claim"),
    ("missing supplement facts verification", "missing supplement facts verification"),
    ("guaranteed outcome", "guaranteed outcome claim"),
)


def assess_website_ads_creative_policy(analysis: object) -> dict[str, Any]:
    """Return advisory risk labels without replacing TikTok's review decision.

    Internal analysis is useful for ranking and for explaining risk, but it is
    not authoritative enough to reject a creative before TikTok reviews it.
    Only assets without usable analysis remain ineligible here. Official audit
    rejection is persisted separately by the delivery monitor.
    """
    if not isinstance(analysis, Mapping) or not analysis:
        return {
            "readiness": "REVIEW",
            "eligible_for_automatic_launch": False,
            "flags": ["missing structured creative analysis"],
            "risk_only": True,
            "submission_mode": "WAIT_FOR_ANALYSIS",
        }

    explicit = str(analysis.get("policy_readiness") or "").strip().upper()
    model_flags = analysis.get("policy_flags")
    flags = list(dict.fromkeys(
        str(value).strip()[:240]
        for value in (model_flags if isinstance(model_flags, list) else [])
        if str(value).strip()
    ))
    evidence = json.dumps(
        {
            "risks": analysis.get("risks"),
            "spoken_claims": analysis.get("spoken_claims"),
            "testing_notes": analysis.get("testing_notes"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()
    for marker, reason in _LEGACY_BLOCKING_RULES:
        if marker in evidence and reason not in flags:
            flags.append(reason)

    if flags and any(marker in evidence for marker, _ in _LEGACY_BLOCKING_RULES):
        readiness = "BLOCKED"
    elif explicit in POLICY_READINESS_VALUES:
        readiness = explicit
    else:
        # Legacy v2 analyses with complete evidence remain usable unless a
        # concrete blocking claim was detected above.
        readiness = "APPROVED"

    return {
        "readiness": readiness,
        "eligible_for_automatic_launch": True,
        "flags": flags,
        "risk_only": readiness != "APPROVED" or bool(flags),
        "submission_mode": "TIKTOK_PLATFORM_REVIEW",
    }
