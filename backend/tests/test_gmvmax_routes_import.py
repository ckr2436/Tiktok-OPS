from importlib import import_module

from fastapi import APIRouter


def test_import_router_ok() -> None:
    module = import_module("app.features.tenants.ttb.gmv_max.router_provider")
    router = getattr(module, "router", None)
    assert isinstance(router, APIRouter)
    paths = {route.path for route in router.routes}
    assert any(path.endswith("/gmvmax") for path in paths)
    assert any(path.endswith("/gmvmax/{campaign_id}") for path in paths)
    assert any("/{campaign_id}/metrics/sync" in path for path in paths)
    assert any("/{campaign_id}/actions" in path for path in paths)
    assert any("/{campaign_id}/strategy" in path for path in paths)
