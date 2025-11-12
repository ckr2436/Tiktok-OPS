from importlib import import_module
from fastapi import APIRouter


def test_strategy_routes_registered():
    mod = import_module("app.features.tenants.ttb.gmvmax.router")
    router = getattr(mod, "router", None)
    assert isinstance(router, APIRouter)
    paths = {route.path for route in router.routes}
    assert any("/campaigns/{campaign_id}/strategy" in path for path in paths)
    assert any("/campaigns/{campaign_id}/strategy/preview" in path for path in paths)
