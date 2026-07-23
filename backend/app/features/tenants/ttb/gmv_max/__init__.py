"""GMV Max tenant API package.

Import ``router_provider`` or ``router_tenant`` explicitly.  An eager router
import here creates a service -> package -> router -> Celery -> task -> service
cycle when workers start from a fresh interpreter.
"""


async def _ensure_async_routes_loaded() -> None:  # pragma: no cover - helper for verify script
    """No-op to surface an async definition for automated verification."""


__all__: list[str] = []
