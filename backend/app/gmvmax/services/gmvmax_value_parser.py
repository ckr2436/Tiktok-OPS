"""Centralized helpers for safe GMV Max numeric parsing.

All monetary/ratio parsing must avoid floats, guard against non-finite values,
and normalize stat time semantics before writing facts and snapshots.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
from typing import Any

from dateutil import parser


logger = logging.getLogger("gmv.gmvmax.value_parser")


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str, float)):
        try:
            dec = Decimal(str(value))
        except InvalidOperation:
            return None
        try:
            if not dec.is_finite():
                return None
        except InvalidOperation:
            return None
        return dec
    return None


def money_to_cents(value: Any) -> int | None:
    """Convert a money value to integer cents.

    TikTok API may return metrics as strings or decimals. We always round to the
    nearest cent using ``ROUND_HALF_UP`` to align with MySQL ``DECIMAL(18, 2)``
    semantics before converting to integer cents.
    """

    dec = _coerce_decimal(value)
    if dec is None:
        return None
    try:
        quantized = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return int(quantized * 100)


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_decimal(value: Any, scale: int = 4) -> Decimal | None:
    dec = _coerce_decimal(value)
    if dec is None:
        return None
    quant = Decimal((0, (1,), -scale))  # e.g. scale 4 => Decimal("0.0001")
    try:
        return dec.quantize(quant, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def parse_stat_time_day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        parsed = parser.parse(str(value))
        return parsed.date()
    except (ValueError, TypeError, OverflowError):
        return None


def parse_stat_time_hour(value: Any) -> datetime | None:
    """Parse an hourly timestamp and normalize to naive UTC on the hour.

    * tz-aware inputs are converted to UTC then stripped of tzinfo
    * tz-naive inputs are treated as already UTC
    * minute/second/microsecond components are floored to the hour to avoid
      creating multiple keys within the same hour when upstream sends partial
      timestamps
    """

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo:
            parsed = value.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            parsed = value
        if parsed.minute or parsed.second or parsed.microsecond:
            logger.debug(
                "gmvmax hour parsed with sub-hour precision; flooring",
                extra={"value": value},
            )
            parsed = parsed.replace(minute=0, second=0, microsecond=0)
        return parsed
    try:
        parsed = parser.parse(str(value))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc)
        parsed = parsed.replace(tzinfo=None)
        if parsed.minute or parsed.second or parsed.microsecond:
            logger.debug(
                "gmvmax hour parsed with sub-hour precision; flooring",
                extra={"value": value},
            )
            parsed = parsed.replace(minute=0, second=0, microsecond=0)
        return parsed
    except (ValueError, TypeError, OverflowError):
        return None


__all__ = [
    "money_to_cents",
    "to_decimal",
    "to_int",
    "parse_stat_time_day",
    "parse_stat_time_hour",
]
