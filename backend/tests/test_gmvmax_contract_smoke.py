from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import install_exception_handlers
from app.features.tenants.ttb.router import router as ttb_router


def _build_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(ttb_router)
    return app

client = TestClient(_build_app())


def test_routes_exist_shape_only():
    base = "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax"
    for path in [
        base,
        f"{base}/999",
        f"{base}/999/metrics",
        f"{base}/999/actions",
        f"{base}/999/strategy",
    ]:
        response = client.get(path)
        assert response.status_code in (200, 401, 403, 404)


def test_no_get_requires_body():
    response = client.get(
        "/api/v1/tenants/1/providers/tiktok-business/accounts/1/gmvmax/999/metrics"
    )
    assert "Field required" not in response.text
