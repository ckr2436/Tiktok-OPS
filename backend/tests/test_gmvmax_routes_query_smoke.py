from importlib import import_module

from fastapi import APIRouter


def test_routes_registered():
    module = import_module("app.features.tenants.ttb.gmvmax.router")
    router = getattr(module, "router", None)
    assert isinstance(router, APIRouter)
    paths = {route.path for route in router.routes}
    assert any("/campaigns/{campaign_id}/metrics" in path for path in paths)
    assert any("/campaigns/{campaign_id}/actions" in path for path in paths)
