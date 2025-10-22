# app/core/errors.py
from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


class RateLimitExceeded(APIError):
    """Specialised APIError variant carrying rate-limit metadata."""

    def __init__(
        self,
        message: str,
        *,
        next_allowed_at: datetime | str | int | float | None,
        limit: Optional[int],
        remaining: Optional[int],
        reset_ts: Optional[int],
    ) -> None:
        iso = _coerce_iso8601(next_allowed_at if next_allowed_at is not None else reset_ts)
        super().__init__(
            "TOO_FREQUENT",
            message,
            status_code=429,
            data={"next_allowed_at": iso},
        )
        self.next_allowed_at = iso
        self.limit = limit
        self.remaining = remaining
        self.reset_ts = reset_ts


def _coerce_iso8601(value: datetime | str | int | float | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=UTC)
    else:  # pragma: no cover - defensive
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _retry_after_seconds(next_allowed_at: Optional[str]) -> Optional[int]:
    if not next_allowed_at:
        return None
    try:
        dt = datetime.fromisoformat(next_allowed_at.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return None
    now = datetime.now(tz=timezone.utc)
    delta = int((dt - now).total_seconds())
    return max(delta, 0)


def rate_limit_response(
    message: str,
    *,
    next_allowed_at: datetime | str | int | float | None,
    limit: Optional[int],
    remaining: Optional[int],
    reset_ts: Optional[int],
) -> JSONResponse:
    iso = _coerce_iso8601(next_allowed_at if next_allowed_at is not None else reset_ts)
    headers: Dict[str, str] = {}
    retry_after = _retry_after_seconds(iso)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    if iso:
        headers["X-Next-Allowed-At"] = iso
    if limit is not None:
        headers["X-RateLimit-Limit"] = str(limit)
    if remaining is not None:
        headers["X-RateLimit-Remaining"] = str(max(remaining, 0))
    if reset_ts is not None:
        headers["X-RateLimit-Reset"] = str(reset_ts)

    payload: Dict[str, Any] = {
        "error": {
            "code": "TOO_FREQUENT",
            "message": message,
            "data": {"next_allowed_at": iso},
        }
    }
    return JSONResponse(status_code=429, content=payload, headers=headers)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error_handler(_: Request, exc: APIError):
        if isinstance(exc, RateLimitExceeded):
            return rate_limit_response(
                exc.message,
                next_allowed_at=exc.next_allowed_at,
                limit=exc.limit,
                remaining=exc.remaining,
                reset_ts=exc.reset_ts,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "data": exc.data}},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error."}},
        )

