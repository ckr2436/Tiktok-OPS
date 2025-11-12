from importlib import import_module

from fastapi import APIRouter


def test_import_router_ok() -> None:
    module = import_module("app.features.tenants.ttb.gmvmax.router")
    router = getattr(module, "router", None)
    assert isinstance(router, APIRouter)
    paths = {route.path for route in router.routes}
    assert any(path.endswith("/campaigns") for path in paths)
    assert any("/campaigns/{campaign_id}" in path for path in paths)
    assert any("/campaigns/{campaign_id}/metrics/sync" in path for path in paths)
    assert any(path.endswith("/campaigns/actions") for path in paths)
