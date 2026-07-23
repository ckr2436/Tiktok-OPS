"""TikTok Business tenant feature package.

Routers are intentionally imported from ``app.features.tenants.ttb.router``.
Keeping this package initializer side-effect free prevents service-layer
imports (for example the GMV mutation control plane) from recursively loading
the Celery task registry.
"""

__all__: list[str] = []
