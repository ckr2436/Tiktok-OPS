"""Time semantics shared by GMV Max report fact writers.

TikTok documents that cost can be revised for up to eleven hours.  A report
day is therefore only considered settled after 11:00 on the following day in
the advertiser's timezone.  Unknown timezones deliberately remain unsettled;
guessing UTC would move the boundary for non-UTC advertisers.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


OFFICIAL_SETTLEMENT_DELAY_HOURS = 11


def utc_now_naive() -> datetime:
    """Return a UTC timestamp suitable for MySQL ``DATETIME(6)`` columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def settlement_metadata(
    stat_day: date,
    *,
    source_observed_at: datetime,
    advertiser_timezone: str | None,
    delay_hours: int = OFFICIAL_SETTLEMENT_DELAY_HOURS,
) -> tuple[bool, datetime | None]:
    """Return ``(is_final, settled_at_utc)`` for one advertiser report day."""

    timezone_name = str(advertiser_timezone or "").strip()
    if not timezone_name:
        return False, None
    try:
        reporting_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return False, None

    observed_utc = (
        source_observed_at.astimezone(timezone.utc)
        if source_observed_at.tzinfo is not None
        else source_observed_at.replace(tzinfo=timezone.utc)
    )
    deadline_local = datetime.combine(
        stat_day + timedelta(days=1),
        time(hour=0),
        tzinfo=reporting_zone,
    ) + timedelta(hours=max(0, int(delay_hours)))
    is_final = observed_utc >= deadline_local.astimezone(timezone.utc)
    return is_final, source_observed_at if is_final else None


__all__ = [
    "OFFICIAL_SETTLEMENT_DELAY_HOURS",
    "settlement_metadata",
    "utc_now_naive",
]
