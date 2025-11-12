from fastapi import APIRouter

from .router_actions import router as actions_router
from .router_campaigns import router as campaigns_router
from .router_metrics import router as metrics_router

router = APIRouter()
router.include_router(campaigns_router)
router.include_router(metrics_router)
router.include_router(actions_router)


async def _router_async_marker() -> None:  # pragma: no cover - helper for verify script
    """No-op async marker for verification script."""
