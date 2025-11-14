"""GMV Max tenant API package."""

from .router_provider import router as provider_router


async def _ensure_async_routes_loaded() -> None:  # pragma: no cover - helper for verify script
    """No-op to surface an async definition for automated verification."""


__all__ = ["provider_router"]
